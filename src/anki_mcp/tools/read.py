"""Read-only tools.

Every tool here is safe to expose on the public profile: none of them mutate
the collection or talk to AnkiWeb.
"""

from __future__ import annotations

import asyncio

from typing import Any

from anki.collection import Collection
from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

from ..collection import CollectionWorker
from ..config import Config
from ..errors import ToolError
from ..images import all_providers, download_many, gather
from ..telemetry import instrumented
from ._common import (
    FIELD_PREVIEW_CHARS,
    deck_ids_for_filter,
    selection_token,
    serialize_note,
)
from .stats import collection_totals, review_stats

# Advertised to clients so they can tell at a glance that these tools are safe
# to call speculatively.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

# search_notes must not be able to drag the whole collection into context.
MAX_SEARCH_LIMIT = 200


# Scan properly rather than glancing: a person choosing a picture looks through
# dozens of thumbnails, and the failures so far came from settling for the first
# plausible hit. Previews are small and fetched in parallel, so a wide scan is
# cheap.
MAX_IMAGE_CANDIDATES = 30
DEFAULT_IMAGE_CANDIDATES = 15
# Each query is run against every library, so this bounds the request fan-out.
MAX_QUERIES = 5


def register(mcp: MCPServer, worker: CollectionWorker, config: Config) -> None:
    """Attach the read-only tools to the server."""

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("list_decks")
    async def list_decks() -> list[dict[str, Any]]:
        """List every deck with its description, card count and cards due today.

        Use this to discover the exact deck names accepted by add_note and by
        the `deck` argument of the other tools. The description says what
        belongs in each deck -- read it before choosing where a note goes.
        """

        def op(col: Collection) -> list[dict[str, Any]]:
            due = {}
            # deck_due_tree is a nested tree; flatten it so callers get a plain
            # list they can scan, keeping the '::' full names.
            def walk(node: Any) -> None:
                for child in node.children:
                    due[child.deck_id] = {
                        "new": child.new_count,
                        "learning": child.learn_count,
                        "review": child.review_count,
                    }
                    walk(child)

            walk(col.sched.deck_due_tree())
            decks = []
            for entry in col.decks.all_names_and_ids():
                counts = due.get(entry.id, {"new": 0, "learning": 0, "review": 0})
                deck = col.decks.get(entry.id)
                decks.append(
                    {
                        "name": entry.name,
                        # What belongs in this deck. Set on the topic decks so a
                        # capture can be routed without guessing from the name.
                        "description": (deck or {}).get("desc") or None,
                        "deck_id": entry.id,
                        "cards": col.decks.card_count(entry.id, include_subdecks=True),
                        "due_today": counts,
                    }
                )
            return sorted(decks, key=lambda d: d["name"])

        return await worker.run(op)

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("list_note_types")
    async def list_note_types() -> list[dict[str, Any]]:
        """List note types with their field names, in order.

        add_note requires field names that match a note type exactly, so call
        this first when adding a note of an unfamiliar type.
        """

        def op(col: Collection) -> list[dict[str, Any]]:
            use_counts = {m.name: m.use_count for m in col.models.all_use_counts()}
            result = []
            for entry in col.models.all_names_and_ids():
                notetype = col.models.get(entry.id)
                if notetype is None:
                    continue
                result.append(
                    {
                        "name": entry.name,
                        "note_type_id": entry.id,
                        "fields": col.models.field_names(notetype),
                        "templates": [t["name"] for t in notetype["tmpls"]],
                        "notes": use_counts.get(entry.name, 0),
                    }
                )
            return sorted(result, key=lambda m: m["name"])

        return await worker.run(op)

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("search_notes")
    async def search_notes(
        query: str, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Find notes using Anki search syntax.

        The result carries a `selection` token describing exactly which notes
        matched, and when. delete_notes requires it: it is what proves a
        destructive call is acting on the set you actually looked at, rather
        than on whatever the same query happens to match later. Pass it
        through unchanged; never build one.

        Field text is truncated in the results; call get_note for a note's full
        contents. Examples of valid queries:
          deck:German                 -- one deck and its subdecks
          tag:noun -is:suspended      -- has a tag, excluding suspended
          "der Hund"                  -- exact phrase
          front:der*                  -- a field starting with 'der'
          added:7                     -- added in the last 7 days
          is:due                      -- due for review now
        """
        capped = max(1, min(limit, MAX_SEARCH_LIMIT))

        def op(col: Collection) -> dict[str, Any]:
            note_ids = col.find_notes(query)
            page = note_ids[offset : offset + capped]
            return {
                "query": query,
                "total_matches": len(note_ids),
                # Over EVERY match, not the page: `limit` controls how much is
                # rendered, and a token that only covered the first 50 would
                # quietly stop protecting the rest of a large cleanup.
                "selection": selection_token(col, note_ids),
                "offset": offset,
                "returned": len(page),
                "notes": [
                    serialize_note(
                        col,
                        nid,
                        field_limit=FIELD_PREVIEW_CHARS,
                        include_cards=False,
                    )
                    for nid in page
                ],
            }

        return await worker.run(op)

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("get_note")
    async def get_note(note_id: int) -> dict[str, Any]:
        """Fetch one note in full: every field untruncated, tags, and its cards."""

        def op(col: Collection) -> dict[str, Any]:
            return serialize_note(col, note_id)

        return await worker.run(op)

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("due_counts")
    async def due_counts(deck: str | None = None) -> dict[str, Any]:
        """Cards due today, for one deck (including its subdecks) or all decks.

        These are the counts after the deck's daily limits are applied -- the
        same numbers the Anki app shows on its deck list.
        """

        def op(col: Collection) -> dict[str, Any]:
            tree = col.sched.deck_due_tree()
            if deck is None:
                node = tree
                scope = "entire collection"
            else:
                deck_id = deck_ids_for_filter(col, deck)[0]
                node = col.decks.find_deck_in_tree(tree, deck_id)
                scope = deck
                if node is None:
                    return {"scope": scope, "new": 0, "learning": 0, "review": 0,
                            "total_due": 0, "cards_in_scope": 0}
            return {
                "scope": scope,
                "new": node.new_count,
                "learning": node.learn_count,
                "review": node.review_count,
                "total_due": node.new_count + node.learn_count + node.review_count,
                "cards_in_scope": node.total_including_children,
            }

        return await worker.run(op)

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("review_summary")
    async def review_summary(days: int = 30, deck: str | None = None) -> dict[str, Any]:
        """Summarise recent review activity and retention.

        Covers the last `days` days: how many reviews, time spent, the share
        answered 'again', and retention on mature cards (interval >= 21 days).
        """
        window = max(1, min(days, 365))

        def op(col: Collection) -> dict[str, Any]:
            deck_ids = deck_ids_for_filter(col, deck)
            return {
                "scope": deck or "entire collection",
                "activity": review_stats(col, window, deck_ids),
                "collection": collection_totals(col, deck_ids),
            }

        return await worker.run(op)

    # structured_output=False because the return mixes text with Image content
    # blocks, which the structured-output serialiser cannot represent.
    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    @instrumented("find_images")
    async def find_images(
        queries: list[str], count: int = DEFAULT_IMAGE_CANDIDATES
    ) -> list[Any]:
        """Search for candidate pictures and return them to look at.

        Use this before add_note whenever the picture matters, which on this
        collection is always: the image carries the meaning instead of an
        English translation.

        PASS SEVERAL QUERIES AT ONCE. Every query is run against every image
        library and the results are pooled, deduplicated and interleaved, so
        three well-chosen queries in one call beat three separate calls. Vary
        them deliberately:
          - the thing itself      ["Hausschuh", "Pantoffel Filz"]
          - the scene it lives in ["Feierabend Sonnenuntergang", "Büro verlassen"]
          - what someone DOES     ["Frau winkt", "Freunde umarmen sich"]

        Avoid words that drag in the wrong register: searching "Abschied"
        returns funerals and mourning roses, because that is what people tag
        with it. Prefer the concrete action or object.

        MAKE IT PERSONAL. A picture of a place the learner actually knows beats
        an anonymous one, and Fluent Forever's whole argument for personal
        connection says so. Two sources, in this order:

          1. The note's own tags and "Personal Connection" field, which record
             where the word was actually met. Call get_note or search_notes
             first when re-illustrating an existing card.
          2. What is known about the learner generally -- where they live, how
             they get around, what they do.

        An anchor SHARPENS a depicting image, it never replaces depiction. For
        "der Bahnsteig" prefer their own city's tram platform over an anonymous
        one -- but it must still be a platform. A photograph of a museum for
        "Gesundheit" anchors a memory while depicting nothing, and a picture
        that needs explaining has failed.

        Never invent an association. A fabricated memory is worse than a
        generic photo, because the learner is then trying to recall something
        that never happened.

        Then LOOK at what comes back. Word matching cannot judge a picture -- a
        ceramic frog is tagged "winken, abschied" and a photograph of paperclips
        is tagged "feierabend". Choose the one that actually depicts the word
        and pass its url to add_note or update_note as `image_url`.

        Only if several genuinely varied searches all fail should you fall back
        to add_note(abstract=true). That is the last resort, not the first: a
        real picture is worth much more than a definition alone.

        Returns a text entry per candidate (index, url, which query and library
        found it, tags, licence) followed by the previews in the same order.
        """
        capped = max(1, min(count, MAX_IMAGE_CANDIDATES))
        wanted = [q.strip() for q in queries if q and q.strip()][:MAX_QUERIES]
        if not wanted:
            raise ToolError("Pass at least one non-empty search query.")

        def search() -> list[Any]:
            providers = all_providers(
                config.google_cse_key,
                config.google_cse_engine_id,
                config.pixabay_key,
            )
            return gather(providers, wanted, limit=capped)

        pooled = await asyncio.to_thread(search)
        if not pooled:
            raise ToolError(
                f"Nothing found across {len(wanted)} quer(y/ies) and every image "
                "library. Try naming the object or action concretely in German, "
                "or -- if the word genuinely cannot be pictured -- use "
                "add_note(abstract=true)."
            )

        preview_urls = [c.preview_url or c.url for c, _, _ in pooled]
        fetched = await asyncio.to_thread(download_many, preview_urls)

        out: list[Any] = []
        previews: list[Any] = []
        for i, (cand, pname, q) in enumerate(pooled, 1):
            line = (
                f"{i}. url={cand.url}"
                f"\n   found by: {pname} for {q!r}"
                f"\n   tags: {(cand.title or '(none)')[:150]}"
                f"\n   licence: {cand.license or '?'}"
            )
            data = fetched.get(cand.preview_url or cand.url)
            if data is None:
                out.append(line + "\n   (preview unavailable)")
                continue
            out.append(line)
            # Format is inferred by the client from the bytes; jpeg covers the
            # overwhelming majority and a wrong hint here is harmless.
            previews.append(Image(data=data, format="jpeg"))
        return out + previews
