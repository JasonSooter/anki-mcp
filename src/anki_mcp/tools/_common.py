"""Shared lookups and serialisers for the tool modules.

Decks and note types are addressed by *name* everywhere in the tool API --
that is what a model naturally has in hand, and ids are meaningless to it.
Resolution happens here, and a miss produces an error that lists the valid
names so the caller can correct itself in one turn.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from typing import Any

from anki.collection import Collection
from anki.utils import ids2str

from ..errors import ToolError

# cards.queue values, per anki's schema.
QUEUE_NAMES = {
    -3: "buried (user)",
    -2: "buried (sibling)",
    -1: "suspended",
    0: "new",
    1: "learning",
    2: "review",
    3: "day learning",
    4: "preview",
}

# cards.type values.
TYPE_NAMES = {0: "new", 1: "learning", 2: "review", 3: "relearning"}

# Field text is HTML and can be long. Search results truncate; get_note doesn't.
FIELD_PREVIEW_CHARS = 200



# How long a selection stays usable. Short on purpose: the point is that a
# destructive call is executed against a set the caller looked at *just now*,
# and re-running search_notes costs nothing. Fifteen minutes leaves room for a
# search, a summary, and a human saying yes, while refusing a plan made
# yesterday -- which is the failure that actually happened here: a delete
# planned on the 15th was still perfectly accurate as a *query* on the 17th,
# and by then deleting was the wrong thing to do.
SELECTION_MAX_AGE_SECONDS = 900

# Enough to make an accidental collision impossible in a collection of any
# plausible size, and short enough to read back in a log line.
_SELECTION_DIGEST_CHARS = 16


def _digest(col: Collection, note_ids: Sequence[int]) -> str:
    """A fingerprint of what these notes ARE, not merely which ids they have.

    Ids alone are not enough, and this was found the hard way: Anki hands out
    note ids from a millisecond clock and reuses a freed one, so deleting the
    newest note and adding another in the same millisecond produces a DIFFERENT
    note wearing the SAME id. A digest over ids called that unchanged -- which
    is precisely the swap the token exists to catch.

    Hashing the fields and tags also means an EDIT to a matched note breaks the
    selection. That is deliberate rather than incidental: a caller who reviewed
    a note and is about to destroy it should be stopped if its contents changed
    since they looked.

    Ordered by id in SQL because find_notes promises no order, and a
    fingerprint that moved when the same notes came back shuffled would refuse
    every honest call.
    """
    # Fed to the hash row by row rather than joined first: the selection this
    # token is meant to protect is a BIG one, and materialising every matched
    # note's fields as a single string just to hash it doubles the peak for no
    # benefit. The separators keep the framing unambiguous, so the digest is
    # the same as the joined form would have produced.
    digest = hashlib.sha256()
    for note_id, flds, tags in col.db.all(
        f"select id, flds, tags from notes where id in {ids2str(note_ids)} "
        "order by id"
    ):
        digest.update(f"{note_id}\x1e{flds}\x1e{tags}\x1f".encode())
    return digest.hexdigest()[:_SELECTION_DIGEST_CHARS]


def selection_token(
    col: Collection, note_ids: Sequence[int], now: float | None = None
) -> str:
    """Mint the token search_notes hands out and delete_notes demands back.

    It carries a fingerprint of the matched notes and the moment they were
    matched -- the two things `expect_count` cannot tell you. The count proves
    how many; this proves *which*, and *when*.
    """
    stamp = int(time.time() if now is None else now)
    return f"{_digest(col, note_ids)}:{stamp}"


def verify_selection(
    token: str, col: Collection, note_ids: Sequence[int], now: float | None = None
) -> None:
    """Raise unless `token` describes exactly these notes, recently.

    Three distinct refusals, because they call for three different fixes:
    a malformed token means the caller invented it, a stale one means re-run
    the search, and a mismatch means the collection moved underneath them.
    """
    digest, _, stamp = token.partition(":")
    if not digest or not stamp.lstrip("-").isdigit():
        raise ToolError(
            f"{token!r} is not a selection token. Pass the `selection` value "
            "from the search_notes call whose results you are acting on -- do "
            "not construct one."
        )

    age = int(time.time() if now is None else now) - int(stamp)
    if age > SELECTION_MAX_AGE_SECONDS:
        raise ToolError(
            f"This selection is {age // 60} minutes old, past the "
            f"{SELECTION_MAX_AGE_SECONDS // 60}-minute limit, so it no longer "
            "says anything about what the query matches now. Run search_notes "
            "again and act on what it returns. A query can stay perfectly "
            "accurate while the decision to delete stops being the right one."
        )

    if _digest(col, note_ids) != digest:
        raise ToolError(
            "The notes this query matches are not the ones the selection was "
            "taken from -- they were added, removed or edited in between, so "
            "some of what would be deleted was never reviewed. Nothing was "
            "deleted. Run search_notes again and look at what it returns now."
        )


def resolve_deck_id(col: Collection, name: str) -> int:
    """Look up a deck by exact name, or raise a ToolError listing the options."""
    deck_id = col.decks.id_for_name(name)
    if deck_id is None:
        available = sorted(d.name for d in col.decks.all_names_and_ids())
        raise ToolError(
            f"No deck named {name!r}. Deck names are case-sensitive and use "
            f"'::' for subdecks. Available decks: {', '.join(available)}"
        )
    return deck_id


def resolve_notetype(col: Collection, name: str) -> dict[str, Any]:
    """Look up a note type by exact name, or raise a ToolError listing the options."""
    notetype = col.models.by_name(name)
    if notetype is None:
        available = sorted(m.name for m in col.models.all_names_and_ids())
        raise ToolError(
            f"No note type named {name!r}. Available note types: "
            f"{', '.join(available)}"
        )
    return notetype


def deck_ids_for_filter(col: Collection, deck: str | None) -> list[int] | None:
    """Deck id plus its subdeck ids, or None meaning 'the whole collection'."""
    if deck is None:
        return None
    deck_id = resolve_deck_id(col, deck)
    return list(col.decks.deck_and_child_ids(deck_id))


def _truncate(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + "..."


def serialize_note(
    col: Collection,
    note_id: int,
    *,
    field_limit: int | None = None,
    include_cards: bool = True,
) -> dict[str, Any]:
    """Render a note as plain JSON-able data."""
    note = col.get_note(note_id)
    notetype = note.note_type()
    result: dict[str, Any] = {
        "note_id": note.id,
        "note_type": notetype["name"] if notetype else "unknown",
        "tags": list(note.tags),
        "fields": {
            name: _truncate(value, field_limit)
            for name, value in zip(note.keys(), note.values(), strict=True)
        },
    }
    if include_cards:
        cards = [col.get_card(cid) for cid in col.card_ids_of_note(note.id)]
        result["cards"] = [
            {
                "card_id": card.id,
                "deck": col.decks.name(card.did),
                "template": card.template()["name"],
                "queue": QUEUE_NAMES.get(card.queue, str(card.queue)),
                "type": TYPE_NAMES.get(card.type, str(card.type)),
                "interval_days": card.ivl,
                "reps": card.reps,
                "lapses": card.lapses,
            }
            for card in cards
        ]
        result["decks"] = sorted({c["deck"] for c in result["cards"]})
    return result
