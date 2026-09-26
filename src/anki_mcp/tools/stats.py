"""Review statistics, computed from the revlog.

There is no high-level API for these aggregates, so they are raw SQL over
``revlog`` via the collection's DB proxy. Keeping every query in this one
module means the schema knowledge lives in exactly one place.

revlog columns (anki 26.8.1): id, cid, usn, ease, ivl, lastIvl, factor, time, type
  id      -- epoch milliseconds of the review
  ease    -- 1=again, 2=hard, 3=good, 4=easy; 0 for manual entries
  lastIvl -- interval *before* this review, in days; >= 21 means "mature"
  time    -- milliseconds spent on the answer
  type    -- 0=learn, 1=review, 2=relearn, 3=filtered, 4=manual, 5=rescheduled
"""

from __future__ import annotations

from typing import Any

from anki.collection import Collection

SECONDS_PER_DAY = 86400

# A card is "mature" once its interval reaches three weeks; retention on mature
# cards is the number people actually care about, since young-card accuracy is
# dominated by cards still being learned.
MATURE_INTERVAL_DAYS = 21

# type 4 is a manual reschedule and type 5 a rescheduling entry: neither is a
# review the user actually did, so both are excluded from every count.
REAL_REVIEW_TYPES = (0, 1, 2, 3)


def _deck_clause(deck_ids: list[int] | None) -> tuple[str, list[Any]]:
    """SQL fragment restricting to a deck subtree, plus its bound parameters.

    Filtered decks move a card's real deck into ``odid``, so both columns are
    checked -- otherwise a card being crammed would vanish from its own stats.
    """
    if deck_ids is None:
        return "", []
    placeholders = ",".join("?" * len(deck_ids))
    clause = (
        " join cards on cards.id = revlog.cid"
        f" and (cards.did in ({placeholders}) or cards.odid in ({placeholders}))"
    )
    return clause, [*deck_ids, *deck_ids]


def review_stats(col: Collection, days: int, deck_ids: list[int] | None) -> dict[str, Any]:
    """Aggregate the last ``days`` days of reviews."""
    cutoff = col.sched.day_cutoff
    since_ms = (cutoff - days * SECONDS_PER_DAY) * 1000
    join, params = _deck_clause(deck_ids)
    types = ",".join(str(t) for t in REAL_REVIEW_TYPES)

    # Day 0 is today. int() truncates toward zero, so a review earlier today
    # (id/1000 just below the cutoff) lands on 0 and yesterday on -1.
    rows = col.db.all(
        f"""
        select
            cast((revlog.id / 1000.0 - ?) / ? as integer) as day,
            count(),
            sum(revlog.time),
            sum(revlog.ease = 1),
            sum(revlog.lastIvl >= ?),
            sum(revlog.lastIvl >= ? and revlog.ease > 1)
        from revlog{join}
        where revlog.id >= ? and revlog.type in ({types}) and revlog.ease > 0
        group by day
        order by day
        """,
        cutoff,
        SECONDS_PER_DAY,
        MATURE_INTERVAL_DAYS,
        MATURE_INTERVAL_DAYS,
        *params,
        since_ms,
    )

    daily = [
        {
            "days_ago": -int(day),
            "reviews": int(count),
            "seconds": round((time_ms or 0) / 1000),
            "again": int(again or 0),
        }
        for day, count, time_ms, again, _mature, _mature_ok in rows
    ]

    reviews = sum(int(r[1]) for r in rows)
    total_ms = sum(int(r[2] or 0) for r in rows)
    again = sum(int(r[3] or 0) for r in rows)
    mature = sum(int(r[4] or 0) for r in rows)
    mature_ok = sum(int(r[5] or 0) for r in rows)

    return {
        "period_days": days,
        "reviews": reviews,
        "days_studied": len(daily),
        "minutes": round(total_ms / 60000, 1),
        "seconds_per_review": round(total_ms / 1000 / reviews, 1) if reviews else 0.0,
        "reviews_per_studied_day": round(reviews / len(daily), 1) if daily else 0.0,
        # Share of reviews answered "again" -- the lapse rate.
        "again_rate": round(again / reviews, 3) if reviews else None,
        "mature_reviews": mature,
        # Retention on cards with an interval >= 21 days.
        "mature_retention": round(mature_ok / mature, 3) if mature else None,
        "daily": daily,
    }


def collection_totals(col: Collection, deck_ids: list[int] | None) -> dict[str, int]:
    """Card and note counts by state, for the same deck scope."""
    if deck_ids is None:
        where, params = "", []
    else:
        placeholders = ",".join("?" * len(deck_ids))
        where = f" where did in ({placeholders}) or odid in ({placeholders})"
        params = [*deck_ids, *deck_ids]

    counts = col.db.all(
        f"select queue, count() from cards{where} group by queue", *params
    )
    by_queue = {int(q): int(n) for q, n in counts}
    notes = col.db.scalar(
        f"select count(distinct nid) from cards{where}", *params
    )
    return {
        "notes": int(notes or 0),
        "cards": sum(by_queue.values()),
        "new": by_queue.get(0, 0),
        "learning": by_queue.get(1, 0) + by_queue.get(3, 0),
        "review": by_queue.get(2, 0),
        "suspended": by_queue.get(-1, 0),
        "buried": by_queue.get(-2, 0) + by_queue.get(-3, 0),
    }
