"""Mutating tools.

`register_add_note` is separate from `register` on purpose: the public profile
exposes note *creation* (the phone-capture use case) without exposing edits or
the ability to push to AnkiWeb. A tool that is never registered does not exist
on the wire, so the public endpoint cannot be talked into a write it shouldn't
do.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

from anki.collection import Collection
from anki.decks import DeckId
from anki.errors import SearchError
from anki.notes import Note, NoteFieldsCheckResult
from anki.sync_pb2 import SyncCollectionResponse
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..collection import CollectionWorker
from ..config import Config
from ..telemetry import (
    EVENT_MUTATION,
    EVENT_SYNC,
    emit,
    instrumented,
)
from ..errors import ToolError
from ..pronunciation import (
    PronunciationError,
    fetch as fetch_pronunciation,
    store_audio,
)
from ..images import (
    ImageError,
    build_provider,
    download,
    fetch_image,
    store_image,
)
from ..sync import SyncCredentialsError, resolve_auth, store_hkey
from ._common import (
    resolve_deck_id,
    resolve_notetype,
    serialize_note,
    verify_selection,
)

WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
# delete_deck and delete_notes destroy work rather than adding it, and a client
# that dims or confirms destructive tools should be told so.
DESTROYS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

# How long to wait for the media sync to finish before returning anyway. The
# collection sync is already committed at that point; media catches up on the
# next run.
# The field whose presence makes a picture mandatory.
IMAGE_FIELD = "Image"
AUDIO_FIELD = "Audio"
IPA_FIELD = "IPA"
GLOSS_FIELD = "English"
# Used as TTS context when the example gives none -- because the word opens its
# own example ("Servus, schön dich zu sehen!"), so there is nothing before it.
# It supplies the language and register without asserting anything about how the
# word itself should sound, which is what a carrier phrase must not do.
GENERIC_LEAD_IN = "Auf Deutsch:"

# Carries the meaning when there is no picture.
DEFINITION_FIELD = "Definition (DE)"
EXAMPLE_FIELD = "Example (DE)"
# Gates the dictation card. Filled by default: the three-card design is what
# this collection wants, and a new note that silently made only two of them
# would leave the deck inconsistent in a way nothing reports.
SPELLING_FIELD = "Test Spelling"

MEDIA_SYNC_TIMEOUT_SECONDS = 300
MEDIA_POLL_INTERVAL_SECONDS = 1.0

log = logging.getLogger(__name__)


def sentence_context(word: str, example: str) -> tuple[str, str]:
    """The example sentence split around `word`, for TTS context.

    A one-syllable word synthesised alone has nothing to disambiguate it, and
    "neun" came back sounding like "nein". Handing the surrounding sentence to
    the model as context -- which it reads but does not speak -- makes it
    pronounce the word the way it would inside that sentence.

    Returns ("", "") when the word does not occur in the example, since
    inventing a context would be guessing at how the word should sound.
    """
    plain = re.sub(r"<[^>]+>", "", example)
    plain = re.sub(r"\[sound:[^]]*\]", "", plain).strip()
    # The headword is stripped of HTML too. Anki fields hold markup -- a word
    # bolded in the editor arrives as "<b>neun</b>" -- and matching that against
    # plain text would find nothing, silently dropping the context on exactly
    # the note somebody had cared enough about to format.
    head = re.sub(r"<[^>]+>", "", word).strip()
    head = re.sub(r"^(der|die|das)\s+", "", head, flags=re.IGNORECASE)
    if not head or not plain:
        return "", ""
    # The example almost never contains the bare headword: it contains an
    # inflected form (Pantoffel -> Pantoffeln, lügen -> lügt, Heft -> Hefte) or
    # the word inside a compound (hunderttausend -> zweihunderttausend). An
    # exact match found none of those and skipped the cards most in need of
    # context, so match the stem and let the ending vary.
    stem = head[:-2] if len(head) > 4 and head.lower().endswith("en") else head
    match = (
        re.search(rf"\b{re.escape(stem)}\w*", plain, flags=re.IGNORECASE)
        # Last resort: inside a longer word. Only after the word-initial form
        # fails, so "acht" cannot match "achtzig" while "acht" itself is present.
        or re.search(rf"\w*{re.escape(stem)}\w*", plain, flags=re.IGNORECASE)
    )
    if not match:
        return "", ""
    return plain[: match.start()].strip(), plain[match.end() :].strip()


def spoken_text(value: str | None) -> str:
    """What a field actually says, for deciding whether it changed.

    The stored "Example (DE)" carries its own [sound:] tag, so comparing it
    raw against a caller's plain sentence would report a change on every edit
    and re-record audio that was already correct. Markup is dropped for the
    same reason: re-bolding a word does not change how it is spoken.
    """
    text = re.sub(r"\[sound:[^]]*\]", "", value or "")
    return re.sub(r"<[^>]+>", "", text).strip()


def ipa_after_rerecording(
    current_ipa: str, fetched_ipa: str | None, word_changed: bool, caller_set_ipa: bool,
) -> str:
    """The IPA a note should carry after update_note re-records its audio.

    Re-recording used to overwrite the field with Wiktionary's transcription
    every time, and Wiktionary gives the bare headword: "[das ʁaːt]" became
    "ʁaːt", and "[deːɐ̯ ˈʃtandaʁt]" became "ˈstandaʁt", a different
    pronunciation. So the fetched IPA is only used where it is actually news:
    the field is empty, or the Word itself was changed in this edit (so the old
    transcription describes a different word). IPA the caller passes in the
    same call always wins.
    """
    if caller_set_ipa or not fetched_ipa:
        return current_ipa
    if word_changed or not current_ipa.strip():
        return fetched_ipa
    return current_ipa


def needs_example_audio(example: str) -> bool:
    """Should this example sentence be recorded?

    No if it is empty, and no if it already carries a [sound:] tag -- the tag
    is appended to the field itself, so re-running add_note or a backfill over
    the same note must not stack a second recording onto it.
    """
    return bool(example.strip()) and "[sound:" not in example


def needs_english_line(value: str) -> bool:
    """Does this German field need a line of its own in `English`?

    Only if it contains letters. The rule exists because German prose is
    unreadable to the learner it is written for -- but "9" is not German, it
    is language-neutral, and asking for its English produced a gloss reading
    "nine / 9 / My daughter is nine years old." A line that translates a digit
    into itself is noise on the one part of the card meant to resolve doubt.
    """
    return any(ch.isalpha() for ch in re.sub(r"<[^>]+>", "", value))


def gloss_lines(gloss: str) -> list[str]:
    """Split the English field into its lines.

    The field is one Anki field holding several lines rather than three
    fields, because adding fields to the note type is a schema change and
    AnkiWeb answers a schema change with a full sync -- which this server
    refuses to perform on its own.
    """
    parts = re.split(r"<br\s*/?>|\n", gloss)
    return [part for part in (p.strip() for p in parts) if part]


def clean_deck_name(name: str) -> str:
    """The deck name with every level trimmed, or a ToolError explaining why not.

    Shared by create and delete so the two cannot disagree about what a name
    means: a delete that normalised differently from the create would report
    "no such deck" for a deck plainly visible in list_decks.
    """
    # Every level must be a real name. Checking the joined string for a leading
    # or trailing "::" missed an empty level in the MIDDLE -- "German::::Banking"
    # survived, and Anki would make a nameless deck between the two.
    levels = [part.strip() for part in name.split("::")]
    if not levels or any(not level for level in levels):
        raise ToolError(
            f"{name!r} is not a usable deck name. Use '::' to nest -- "
            "'German Topics::Banking & Money' -- with a name on every level of "
            "it, including between two separators."
        )
    return "::".join(levels)


def create_deck_op(col: Collection, name: str, description: str = "") -> dict[str, Any]:
    """Create a deck, or report the one already there.

    Idempotent on purpose: "make sure this deck exists" is the actual intent
    every caller has, and failing on a second call would make a retry after a
    dropped connection destructive to the workflow rather than harmless.

    Anki creates the intermediate levels of a "A::B::C" name itself, so a
    subdeck can be made without creating its parents first.
    """
    cleaned = clean_deck_name(name)

    existing = col.decks.by_name(cleaned)
    if existing is not None:
        return {
            "deck": cleaned,
            "deck_id": existing["id"],
            "created": False,
            "description": existing.get("desc", ""),
        }

    deck_id = col.decks.id(cleaned)
    if description:
        deck = col.decks.get(deck_id)
        # The description is what list_decks shows, and add_note's own guidance
        # tells the caller to read it before choosing a deck -- so a deck made
        # without one is a deck nobody can route to correctly later.
        deck["desc"] = description
        col.decks.save(deck)
    return {
        "deck": cleaned,
        "deck_id": deck_id,
        "created": True,
        "description": description,
    }


def delete_deck_op(
    col: Collection, name: str, delete_cards: bool = False
) -> dict[str, Any]:
    """Delete a deck, refusing by default to take any cards down with it.

    Anki's own remove() deletes the deck's cards and every subdeck without
    asking. That is the right primitive and the wrong default for a tool a model
    calls: the usual reason to delete a deck here is tidying up empties, and a
    one-character typo in the name should not be able to destroy a deck full of
    reviewed cards. So a deck holding cards is refused unless the caller says
    `delete_cards=True` about that specific deck.

    Subdecks count toward that total, because deleting a parent deletes them.
    """
    cleaned = clean_deck_name(name)

    deck = col.decks.by_name(cleaned)
    if deck is None:
        raise ToolError(
            f"There is no deck named {cleaned!r}. Deck names are case-sensitive "
            "and '&' is literal -- call list_decks and copy the name exactly."
        )

    deck_id = DeckId(deck["id"])
    if deck_id == 1:
        raise ToolError(
            "The Default deck cannot be deleted -- Anki recreates it "
            "immediately. Leave it empty instead."
        )

    # include_subdecks because remove() takes them too; counting only the
    # parent's own cards would call a tree holding hundreds of cards "empty".
    cards = col.decks.card_count(deck_id, include_subdecks=True)
    subdecks = [child for child, _ in col.decks.children(deck_id)]

    if cards and not delete_cards:
        detail = f"{cards} card{'s' if cards != 1 else ''}"
        if subdecks:
            detail += f" across it and {len(subdecks)} subdeck(s)"
        raise ToolError(
            f"{cleaned!r} holds {detail}, so deleting it would destroy them "
            "along with their review history. Pass delete_cards=True if that is "
            "really what you want, or move the cards out first."
        )

    removed = col.decks.remove([deck_id]).count
    return {
        "deck": cleaned,
        "deck_id": deck_id,
        "cards_deleted": removed,
        "subdecks_deleted": subdecks,
    }


def validate_update(
    col: Collection,
    note: Note,
    fields: dict[str, str] | None,
    deck: str | None,
) -> int | None:
    """Every rejection an update can make, made BEFORE anything is written.

    Returns the resolved target deck id, or None when the call is not moving.

    This exists as one function so the invariant is enforceable rather than
    remembered. The deck used to be resolved after col.update_note() had
    already committed the field and tag edits, so a typo'd deck name returned
    an error on a note that had nonetheless been changed -- the caller sees a
    failure and has no reason to suspect a partial write happened behind it.
    Anything that can say no has to say it up here.
    """
    if fields:
        valid = set(note.keys())
        unknown = set(fields) - valid
        if unknown:
            notetype = note.note_type()
            name = notetype["name"] if notetype else "this note type"
            raise ToolError(
                f"Note type {name!r} has no field(s) "
                f"{', '.join(sorted(unknown))}. Its fields are: "
                f"{', '.join(note.keys())}"
            )

    if deck is None:
        return None
    return resolve_deck_id(col, deck)


def update_changes_something(
    fields: dict[str, str] | None,
    tags: list[str] | None,
    deck: str | None,
    image_url: str | None,
    regenerate_audio: bool | None,
) -> bool:
    """Whether an update_note call asks for any change at all.

    Pulled out of the tool so it can be tested without standing up an MCP
    server. It exists because every argument is optional and partial, so a call
    that names none of them is almost always a mistake -- and it has to be
    kept in step as arguments are added: when `deck` arrived, a guard that did
    not know about it would have refused a pure move, which is the exact call
    the argument was added for.

    `regenerate_audio=False` is deliberately NOT a change: it means "do not
    re-record", so on its own it asks for nothing.
    """
    return (
        fields is not None
        or tags is not None
        or deck is not None
        or image_url is not None
        or bool(regenerate_audio)
    )


def delete_notes_op(
    col: Collection, query: str, expect_count: int, selection: str
) -> dict[str, Any]:
    """Delete every note matching `query`, but only if there are exactly
    `expect_count` of them.

    The count is required rather than optional, and that is the whole design.
    A search is the most dangerous way to choose what to destroy: `tag:zeit`
    and `tag:zeit*` and a typo'd `tag:ziet` all look equally plausible in a
    tool call, and the first two differ by thousands of notes in a collection
    that happens to have `zeit::3-monate`. Forcing the caller to state the
    number means they must have run search_notes first and looked at the
    result, and it turns a collection that moved underneath them -- a phone
    sync landing between the search and the delete -- into a refusal rather
    than a surprise.

    `selection` is what the count cannot be: proof that these are the notes the
    caller actually looked at, and that they looked recently. The count says
    how many; the token says which, and when. Both are required, and they fail
    differently on purpose -- a count mismatch means the query is broader than
    intended, a digest mismatch means the collection moved underneath it, and
    an expired token means the decision is older than the facts it rested on.

    That last one is not hypothetical. A delete planned here on the 15th was
    still an accurate *query* on the 17th -- same count, same note ids -- but
    by then the notes had been superseded and deleting them was wrong. No
    check on the selection's identity would have caught that; only its age.

    Nothing here is recoverable from AnkiWeb afterwards: a synced deletion is
    a deletion everywhere.
    """
    if not query.strip():
        raise ToolError(
            "An empty query would match the entire collection. Pass the same "
            "search you ran with search_notes."
        )

    try:
        note_ids = col.find_notes(query)
    except SearchError as exc:
        # SearchError ONLY. find_notes also raises DBError and friends, and
        # relabelling a database fault as "invalid search" would send the
        # caller to rewrite a query that was fine while the real failure --
        # which CollectionWorker already knows how to describe -- went
        # unreported. Anki's own message names the position of the problem,
        # which is the useful part, so it is passed through verbatim.
        raise ToolError(f"{query!r} is not a valid Anki search: {exc}") from exc

    found = len(note_ids)
    if found == 0:
        # NOT a success. A misspelled tag matches nothing, and reporting
        # "deleted 0" would read as "the cleanup happened" when it did not.
        raise ToolError(
            f"{query!r} matches no notes, so there is nothing to delete. "
            "Check the query with search_notes -- a misspelled tag matches "
            "nothing rather than erroring."
        )

    if found != expect_count:
        raise ToolError(
            f"{query!r} matches {found} notes, but expect_count said "
            f"{expect_count}. Nothing was deleted. Re-run search_notes to see "
            "what it matches now: either the query is broader than you meant, "
            "or the collection changed since you counted."
        )

    # Last gate, and after the count so the friendlier message wins when both
    # would fire: a caller whose query got broader should hear that rather than
    # a digest mismatch. Before the loop below, not after: a refused call should
    # not first read every matched note and strip its HTML for a list it will
    # never return.
    verify_selection(selection, col, note_ids)

    # Captured BEFORE the delete: afterwards there is nothing left to read, and
    # a caller who got the count wrong in their head should be able to see from
    # the result exactly which notes went.
    deleted = []
    for note_id in note_ids:
        note = col.get_note(note_id)
        # "Word" and "Front" cover this collection and stock Anki, but add_note
        # accepts any note type: one whose first field is "Term" would report an
        # empty word, and a deletion list that cannot name what it deleted is
        # exactly the thing this list exists to prevent. Fall back to the first
        # field, which is what Anki itself shows in the browser.
        headword = next(
            (note[field] for field in ("Word", "Front") if field in note),
            note.fields[0] if note.fields else "",
        )
        deleted.append({"note_id": note_id, "word": re.sub(r"<[^>]+>", "", headword)})

    removed = col.remove_notes(note_ids).count
    return {"query": query, "notes_deleted": found, "cards_deleted": removed,
            "deleted": deleted}


def register_add_note(
    mcp: MCPServer, worker: CollectionWorker, config: Config
) -> None:
    """Attach add_note. Registered by both profiles.

    Takes config because image fetching needs the provider settings; the public
    profile registers this tool too, so it cannot live in register() below.
    """

    @mcp.tool(annotations=WRITES)
    @instrumented("add_note", mutation=True)
    async def add_note(
        deck: str,
        note_type: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
        allow_duplicate: bool = False,
        image_query: str | None = None,
        image_url: str | None = None,
        abstract: bool = False,
    ) -> dict[str, Any]:
        """Add a note to a deck.

        `fields` keys must match the note type's field names exactly -- call
        list_note_types to get them. Field values may contain HTML. Call
        list_decks first: each deck's description says what belongs in it.

        IMAGES. If the note type has an "Image" field, a picture is required by
        default -- this collection follows the Fluent Forever method, where the
        picture carries the meaning instead of an English translation.

        PREFER `image_url`: call find_images first, look at the candidates, and
        pass the url of the one that actually depicts the word. Tags lie -- a
        cartoon frog is tagged "winken, abschied" and paperclips get tagged
        "feierabend" -- so a picture chosen by looking is much better than one
        chosen by matching words.

        `image_query` is the one-shot fallback: it searches and takes the best
        match sight-unseen, which is quicker but regularly wrong.

        Prefer a query anchored in something the learner knows -- their own
        city, their own commute -- over an anonymous one, as long as the picture
        still depicts the word. See find_images for how far to take that.

        ENGLISH. The "English" field is required on every note. It renders on
        the answer side of both cards and nowhere else, collapsed behind a tap
        ({{hint:English}}), so retrieval still happens in German and no
        translate-to-produce habit forms -- but a wrong-sense recall can be
        checked. "das Heft" is booklet, issue and notebook, and neither the
        picture nor the German definition can tell those apart.

        It holds one line per piece of German on the answer side, separated by
        <br>, in this order:

          1. the gloss -- a few words, e.g. "pocket money"
          2. the English of "Definition (DE)", when that field is filled
          3. the English of "Example (DE)", when that field is filled

        A field with no letters in it needs no line: on a numbers deck the
        definition is "9", and its English is "9". Translating a digit into
        itself only clutters the one part of the card meant to resolve doubt.

        The definition and the example are written in German the learner
        cannot yet read, so a gloss alone leaves most of the answer opaque.
        Translate them naturally, not word for word.

        CARDS. Three per note: recognise the word, produce it (typed), and
        take it down from audio alone (typed). The third is gated on
        "Test Spelling", which is filled automatically -- pass it as "" to skip
        that card, which is worth doing for a phrase or a very long compound
        where dictation is keystrokes rather than learning.

        AUDIO. The word is recorded into "Audio". If "Example (DE)" is filled,
        the sentence is recorded too and its [sound:] tag is appended to that
        field -- so both play, and the sentence audio appears only on the answer
        sides, never on the prompt that asks you to produce the word.

        ABSTRACT WORDS. A picture only works when it names one obvious thing. A
        photo of vegetables does not mean "Gesundheit" -- it means vegetables,
        and the card would accept several different answers. For words that
        cannot be pictured unambiguously (Gesundheit, Entmystifizierung, zum),
        pass abstract=True: the picture is then skipped and the German
        definition carries the meaning instead, which is how Wyner's own
        All-Purpose card works. abstract=True REQUIRES "Definition (DE)".

        By default a note whose first field duplicates an existing note of the
        same type is rejected; pass allow_duplicate=True to add it anyway.

        The new note is saved locally straight away but is NOT pushed to
        AnkiWeb -- call sync_to_ankiweb when you want it on other devices.
        """
        # Fetching happens off the collection thread: it is network-bound and
        # would otherwise block every other tool call for its duration.
        image_data: bytes | None = None
        image_ext = ""
        image_credit: dict[str, Any] | None = None
        if image_url:
            # Already chosen by looking at it; just fetch that one.
            try:
                image_data, image_ext = await asyncio.to_thread(download, image_url)
            except ImageError as exc:
                raise ToolError(str(exc)) from exc
            image_credit = {"chosen_url": image_url, "selected_by": "caller"}
        elif image_query:
            try:
                provider = build_provider(
                    config.image_provider,
                    config.google_cse_key,
                    config.google_cse_engine_id,
                    config.pixabay_key,
                )
                image_data, image_ext, chosen = await asyncio.to_thread(
                    fetch_image, provider, image_query
                )
            except ImageError as exc:
                raise ToolError(str(exc)) from exc
            image_credit = {
                "query": image_query,
                "provider": provider.name,
                "selected_by": "auto (sight-unseen)",
                "title": chosen.title,
                "license": chosen.license,
                "source_url": chosen.url,
            }

        # Pronunciation is fetched OFF the collection thread, like the image
        # above. It makes several network calls and sleeps for rate-limit
        # backoff; doing that inside op() would hold the single collection
        # thread for its whole duration and stall every other tool call.
        # Resolving the field names first costs one short hop, and avoids
        # fetching audio for a note type that has nowhere to put it.
        field_names = await worker.run(
            lambda col: col.models.field_names(resolve_notetype(col, note_type))
        )
        said = None
        pronunciation_note: str | None = None
        if AUDIO_FIELD in field_names and not (fields.get(AUDIO_FIELD) or "").strip():
            spoken = fields.get("Word") or next(iter(fields.values()), "")
            try:
                said = await asyncio.to_thread(
                    fetch_pronunciation, spoken,
                    tts_key=config.tts_key, tts_voice=config.tts_voice,
                    tts_provider=config.tts_provider,
                    prefer_tts=config.prefer_tts,
                    # Leading context only. What precedes the word settles how
                    # it is read; giving the model what FOLLOWS makes it treat
                    # the word as a fragment and clip the tail.
                    context=(sentence_context(
                        spoken, fields.get(EXAMPLE_FIELD) or "")[0]
                        or GENERIC_LEAD_IN, ""),
                    headword=True,
                )
                pronunciation_note = said.source
            except PronunciationError as exc:
                # Not fatal: a card without audio still teaches. Reported so
                # the gap is visible rather than silent.
                pronunciation_note = f"none: {exc}"

        # The example sentence gets its own recording. A word said in isolation
        # is not how it is said in a sentence -- German binds, reduces and moves
        # stress across a phrase -- so the sentence is the part worth imitating.
        #
        # The [sound:] tag is appended to "Example (DE)" itself rather than
        # given a field of its own: adding a field is a note type SCHEMA change,
        # and AnkiWeb answers a schema change by demanding a full sync, which
        # sync_to_ankiweb refuses to perform. Anki plays a [sound:] tag from any
        # field, so this costs nothing and keeps the audio where the sentence
        # is. It also lands only on the ANSWER sides, because that is where
        # "Example (DE)" appears -- card 2's prompt shows "Example (blanked)",
        # so the sentence audio can never give away the word being asked for.
        example_said = None
        example_note: str | None = None
        example_text = (fields.get(EXAMPLE_FIELD) or "").strip()
        # Guarded on the note type having the field at all, like the word-audio
        # path above: without it, a note type with no "Example (DE)" would spend
        # a TTS call before add_note rejected the unknown field anyway.
        if EXAMPLE_FIELD in field_names and needs_example_audio(example_text):
            try:
                example_said = await asyncio.to_thread(
                    fetch_pronunciation, example_text,
                    tts_key=config.tts_key, tts_voice=config.tts_voice,
                    tts_provider=config.tts_provider,
                    # Always synthesised: Wiktionary has recordings of words,
                    # never of sentences, and a lookup would only cost a
                    # throttled round trip to find nothing.
                    prefer_tts=True,
                )
                example_note = example_said.source
            except PronunciationError as exc:
                example_note = f"none: {exc}"

        def op(col: Collection) -> dict[str, Any]:
            notetype = resolve_notetype(col, note_type)
            deck_id = resolve_deck_id(col, deck)

            note = col.new_note(notetype)
            valid = set(note.keys())
            unknown = set(fields) - valid
            if unknown:
                raise ToolError(
                    f"Note type {note_type!r} has no field(s) "
                    f"{', '.join(sorted(unknown))}. Its fields are: "
                    f"{', '.join(note.keys())}"
                )
            for name, value in fields.items():
                note[name] = value

            if IMAGE_FIELD in valid:
                if image_data is not None:
                    note[IMAGE_FIELD] = store_image(
                        col, image_query or fields.get("Word") or "image",
                        image_data, image_ext,
                    )
                elif not (fields.get(IMAGE_FIELD) or "").strip():
                    if not abstract:
                        raise ToolError(
                            f"Note type {note_type!r} requires an image and none "
                            "was given. Pass image_query with a concrete German "
                            f"search phrase, set {IMAGE_FIELD!r} yourself, or pass "
                            "abstract=true if this word cannot be pictured "
                            "unambiguously -- in which case the German definition "
                            "carries the meaning instead."
                        )
                    # No picture, so the definition is the only thing conveying
                    # meaning. Without it the card would ask an unanswerable
                    # question.
                    if not (fields.get(DEFINITION_FIELD) or "").strip():
                        raise ToolError(
                            f"abstract=true skips the picture, so {DEFINITION_FIELD!r} "
                            "is required -- it becomes the only thing carrying the "
                            "meaning. Keep it under ten words, in German."
                        )


            # Applied here, but FETCHED off this thread -- see above.
            if said is not None:
                if said.audio:
                    note[AUDIO_FIELD] = store_audio(
                        col, fields.get("Word") or "audio",
                        said.audio, said.audio_ext,
                    )
                if said.ipa and IPA_FIELD in valid and not fields.get(IPA_FIELD):
                    note[IPA_FIELD] = said.ipa

            if example_said is not None and example_said.audio and EXAMPLE_FIELD in valid:
                tag = store_audio(
                    col, f"{fields.get('Word') or 'audio'}-beispiel",
                    example_said.audio, example_said.audio_ext,
                )
                note[EXAMPLE_FIELD] = f"{note[EXAMPLE_FIELD]} {tag}".strip()

            # Required on every note, not only pictureless ones: the gloss is
            # rendered on both answer sides and catches a wrong-sense recall
            # that neither the picture nor the German definition can -- "das
            # Heft" is booklet, issue and notebook. It was optional in practice
            # once and four consecutive captures skipped it, so it is enforced.
            if GLOSS_FIELD in valid:
                lines = gloss_lines(fields.get(GLOSS_FIELD) or "")
                if not lines:
                    raise ToolError(
                        f"{GLOSS_FIELD!r} is required. It appears on the answer "
                        "side of both cards -- never on a prompt -- and is the "
                        "only thing that confirms which sense of the word was "
                        "recalled."
                    )
                # The German definition and example are themselves unreadable
                # to a learner at this level, so a bare gloss leaves most of
                # the answer side untranslated. Whatever German the answer
                # shows must have an English line here to match.
                german = [
                    name for name in (DEFINITION_FIELD, EXAMPLE_FIELD)
                    if name in valid and needs_english_line(fields.get(name) or "")
                ]
                if len(lines) < 1 + len(german):
                    wanted = ", then ".join(
                        ["the gloss"]
                        + [f"the English of {name!r}" for name in german]
                    )
                    raise ToolError(
                        f"{GLOSS_FIELD!r} needs {1 + len(german)} lines "
                        f"separated by <br>, in this order: {wanted}. It has "
                        f"{len(lines)}. The answer side shows that German to a "
                        "learner who cannot yet read it."
                    )

            # Opt-OUT rather than opt-in: pass "" explicitly to skip the
            # dictation card for a note where typing it is not worth the
            # keystrokes -- a long compound, or a phrase.
            if SPELLING_FIELD in valid and SPELLING_FIELD not in fields:
                note[SPELLING_FIELD] = "y"

            if tags:
                note.tags = list(tags)

            check = note.fields_check()
            if check == NoteFieldsCheckResult.EMPTY:
                raise ToolError(
                    f"The first field ({note.keys()[0]!r}) is empty, so Anki "
                    "would not generate any cards. Give it a value."
                )
            duplicate = check == NoteFieldsCheckResult.DUPLICATE
            if duplicate and not allow_duplicate:
                raise ToolError(
                    f"A {note_type!r} note with first field "
                    f"{fields.get(note.keys()[0], '')!r} already exists. Use "
                    "search_notes to find it, or pass allow_duplicate=true to "
                    "add this one anyway."
                )

            changes = col.add_note(note, deck_id)
            emit(
                EVENT_MUTATION,
                action="add_note",
                tool="add_note",
                deck=deck,
                note_type=note_type,
                note_id=note.id,
                cards_created=changes.count,
                tag_count=len(tags or []),
                duplicate=duplicate,
            )
            return {
                "note_id": note.id,
                "deck": deck,
                "note_type": note_type,
                "cards_created": changes.count,
                "duplicate_of_existing_note": duplicate,
                "image": image_credit,
                "pronunciation": pronunciation_note,
                "example_audio": example_note,
                "synced": False,
            }

        return await worker.run(op)


def register(mcp: MCPServer, worker: CollectionWorker, config: Config) -> None:
    """Attach the remaining write tools. Full profile only."""

    @mcp.tool(annotations=WRITES)
    @instrumented("create_deck", mutation=True)
    async def create_deck(name: str, description: str = "") -> dict[str, Any]:
        """Create a deck, so notes have somewhere to go.

        add_note refuses an unknown deck rather than inventing one -- a typo
        would otherwise scatter cards into a deck nobody reviews -- so a new
        topic starts here.

        Use "::" to nest: "German Topics::Banking & Money" creates the child,
        and its parents if they do not exist yet.

        `description` is worth filling in. list_decks shows it, and add_note's
        guidance is to read it before choosing where a note belongs; a deck
        without one is a deck the next caller has to guess about.

        Doing this twice is safe: an existing deck is reported back with
        created=false rather than raising.
        """
        def op(col: Collection) -> dict[str, Any]:
            result = create_deck_op(col, name, description)
            emit(
                EVENT_MUTATION,
                action="create_deck",
                tool="create_deck",
                deck=result["deck"],
                deck_id=result["deck_id"],
                # NOT `created`: that is a reserved LogRecord attribute (the
                # record's own timestamp), and logging refuses to let `extra`
                # overwrite it -- which took the whole tool down. emit() now
                # guards against this, but the query-facing name stays explicit.
                deck_created=result["created"],
            )
            return result

        return await worker.run(op)

    @mcp.tool(annotations=DESTROYS)
    @instrumented("delete_deck", mutation=True)
    async def delete_deck(name: str, delete_cards: bool = False) -> dict[str, Any]:
        """Delete a deck, and every card in it. Destroys work permanently.

        Its intended use is tidying up: an empty deck made by a typo, or two
        decks that turned out to be the same topic under different spellings.

        SUBDECKS GO TOO. Deleting "German Topics" deletes every deck under it.
        Name the child if the child is what you meant.

        A deck holding cards is REFUSED unless you pass delete_cards=True, and
        the refusal tells you how many cards are at stake. Do not pass it
        reflexively to make an error go away -- those cards carry review history
        that no sync can bring back. Call list_decks first and check the count
        against the deck you actually meant.

        Deck names are case-sensitive and '&' is literal: "German Time & Dates"
        and "German Time &amp; Dates" are two different decks.
        """
        def op(col: Collection) -> dict[str, Any]:
            result = delete_deck_op(col, name, delete_cards)
            emit(
                EVENT_MUTATION,
                action="delete_deck",
                tool="delete_deck",
                deck=result["deck"],
                deck_id=result["deck_id"],
                cards_deleted=result["cards_deleted"],
                subdecks_deleted=len(result["subdecks_deleted"]),
            )
            return result

        return await worker.run(op)

    @mcp.tool(annotations=DESTROYS)
    @instrumented("delete_notes", mutation=True)
    async def delete_notes(
        query: str, expect_count: int, selection: str
    ) -> dict[str, Any]:
        """Delete every note matching an Anki search. Destroys work permanently.

        For cleaning up a batch that went in wrong -- a session that filed notes
        into the wrong deck, or an import that should not have happened.

        RUN search_notes FIRST, WITH THIS EXACT QUERY. Take `total_matches` from
        its result and pass it as `expect_count`; take `selection` from the same
        result and pass it through unchanged. Both are required. Never build a
        selection token yourself -- it is the proof you looked, and forging it
        defeats the only thing standing between a wrong query and lost work.

        The selection expires in 15 minutes. If it has, that is not an obstacle
        to route around: search again and look at what comes back. A query can
        stay perfectly accurate while the decision to delete quietly stops being
        the right one -- that is a real failure this collection has already had,
        with the same count and the same note ids two days apart.

        If `expect_count` does not match, nothing is deleted and you get told
        the real count.

        This is not a formality. A search is the most dangerous way to pick what
        to destroy: `tag:zeit`, `tag:zeit*` and a typo'd `tag:ziet` all look
        equally reasonable in a tool call and can differ by thousands of notes.
        Do NOT re-issue the call with the number from the error message without
        first looking at what that query actually matches -- the mismatch is
        usually telling you the query is wrong, not the count.

        Deleting a note deletes all of its cards and their review history.
        Once synced, it is gone from every device; AnkiWeb has no undo.
        """
        def op(col: Collection) -> dict[str, Any]:
            result = delete_notes_op(col, query, expect_count, selection)
            emit(
                EVENT_MUTATION,
                action="delete_notes",
                tool="delete_notes",
                # NOT the query itself. A search carries exact field text
                # ("der Hund", front:Kontoauszug*), and emit() ships attributes
                # to Loki as structured metadata -- so logging it would publish
                # collection contents to the telemetry backend. The digest is
                # enough to tell two deletions apart and to spot one query being
                # retried, without carrying what it said.
                query_digest=hashlib.sha256(query.encode()).hexdigest()[:12],
                query_length=len(query),
                notes_deleted=result["notes_deleted"],
                cards_deleted=result["cards_deleted"],
            )
            return result

        return await worker.run(op)

    @mcp.tool(annotations=WRITES)
    @instrumented("update_note", mutation=True)
    async def update_note(
        note_id: int,
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
        deck: str | None = None,
        image_url: str | None = None,
        regenerate_audio: bool | None = None,
    ) -> dict[str, Any]:
        """Edit an existing note.

        All arguments are partial: only the fields you name are changed, and
        omitting `tags` leaves the existing tags alone. Pass an empty list to
        clear all tags.

        `deck` MOVES the note's cards to another deck, which is the repair for
        a note that went into the wrong one. The deck must already exist --
        like add_note, this refuses an unknown name rather than inventing it,
        since a typo would otherwise file the note somewhere nobody reviews.
        Call create_deck first if it does not exist yet.

        Moving keeps each card's scheduling and review history, so it is the
        right fix for a misfiled note that has already been studied -- deleting
        and re-adding would reset it to new and throw that history away.

        `image_url` replaces the picture: call find_images, look at what comes
        back, and pass the url of the one that actually depicts the word. This
        is how a card with a poor picture gets repaired. Passing an empty
        "Image" field instead removes the picture altogether, which is the right
        move when nothing depicts the word and the German definition should
        carry it alone.

        `regenerate_audio` keeps the recordings in step with the text:

          None (default) -- re-record when this edit changes "Word" or
              "Example (DE)", and not otherwise. Text and audio cannot drift
              apart, and editing a tag or swapping a picture spends nothing.
          True  -- re-record regardless. For repairing a bad recording when the
              text is already correct.
          False -- never. For a deliberate edit that should keep its audio.
        """
        if not update_changes_something(
            fields, tags, deck, image_url, regenerate_audio
        ):
            raise ToolError(
                "Nothing to update -- pass `fields`, `tags`, `deck`, "
                "`image_url`, `regenerate_audio`, or a combination."
            )

        new_image: bytes | None = None
        new_ext = ""
        if image_url:
            try:
                new_image, new_ext = await asyncio.to_thread(download, image_url)
            except ImageError as exc:
                raise ToolError(str(exc)) from exc

        # Recording happens off the collection thread, like add_note's, and
        # against the note as it will read AFTER this edit -- otherwise a
        # corrected sentence would be re-recorded in its old wording.
        said = example_said = None
        audio_note: dict[str, str] = {}
        if regenerate_audio is not False:
            current = await worker.run(
                lambda col: dict(col.get_note(note_id).items())
            )
            merged = {**current, **(fields or {})}
            # Default: follow the text. Re-record only when this edit actually
            # changes what is spoken -- comparing against the note as it stands,
            # so re-sending an unchanged field is not mistaken for an edit.
            if regenerate_audio is None:
                spoken_changed = any(
                    name in (fields or {})
                    and spoken_text((fields or {})[name]) != spoken_text(current.get(name))
                    for name in ("Word", EXAMPLE_FIELD)
                )
                if not spoken_changed:
                    merged = None
            word = "" if merged is None else re.sub(
                r"<[^>]+>", "", merged.get("Word") or "").strip()
            # Any existing tag is dropped first: re-recording must replace the
            # old audio, not leave the note carrying both.
            example = "" if merged is None else re.sub(
                r"\[sound:[^]]*\]", "", merged.get(EXAMPLE_FIELD) or ""
            ).strip()
            if word:
                try:
                    said = await asyncio.to_thread(
                        fetch_pronunciation, word,
                        tts_key=config.tts_key, tts_voice=config.tts_voice,
                        tts_provider=config.tts_provider,
                        prefer_tts=config.prefer_tts,
                        context=(sentence_context(word, example)[0]
                                 or GENERIC_LEAD_IN, ""),
                        headword=True,
                    )
                    audio_note["word"] = said.source
                except PronunciationError as exc:
                    audio_note["word"] = f"none: {exc}"
            if example:
                try:
                    example_said = await asyncio.to_thread(
                        fetch_pronunciation, example,
                        tts_key=config.tts_key, tts_voice=config.tts_voice,
                        tts_provider=config.tts_provider, prefer_tts=True,
                    )
                    audio_note["example"] = example_said.source
                except PronunciationError as exc:
                    audio_note["example"] = f"none: {exc}"

        def op(col: Collection) -> dict[str, Any]:
            note = col.get_note(note_id)
            changed: list[str] = []

            # Everything that can be refused is refused here, before the first
            # write. Resolving the deck after col.update_note() meant a typo'd
            # deck name returned an error on a note that had already been
            # changed.
            target = validate_update(col, note, fields, deck)
            previous_word = note["Word"] if "Word" in note.keys() else ""

            if fields:
                for field_name, value in fields.items():
                    note[field_name] = value
                changed.extend(sorted(fields))

            if new_image is not None:
                if IMAGE_FIELD not in note.keys():
                    raise ToolError(
                        f"Note type has no {IMAGE_FIELD!r} field, so an image "
                        "cannot be attached to this note."
                    )
                note[IMAGE_FIELD] = store_image(
                    col, note.keys()[0] and note[note.keys()[0]] or "image",
                    new_image, new_ext,
                )
                changed.append(IMAGE_FIELD)

            if said is not None and said.audio and AUDIO_FIELD in note.keys():
                note[AUDIO_FIELD] = store_audio(
                    col, note["Word"] or "audio", said.audio, said.audio_ext)
                changed.append(AUDIO_FIELD)
                if IPA_FIELD in note.keys():
                    note[IPA_FIELD] = ipa_after_rerecording(
                        note[IPA_FIELD], said.ipa,
                        word_changed=spoken_text(note["Word"]) != spoken_text(previous_word),
                        caller_set_ipa=IPA_FIELD in (fields or {}),
                    )

            if example_said is not None and example_said.audio and EXAMPLE_FIELD in note.keys():
                stripped = re.sub(r"\[sound:[^]]*\]", "", note[EXAMPLE_FIELD]).strip()
                tag = store_audio(
                    col, f"{note['Word'] or 'satz'}-beispiel",
                    example_said.audio, example_said.audio_ext)
                note[EXAMPLE_FIELD] = f"{stripped} {tag}".strip()
                changed.append(EXAMPLE_FIELD)

            if tags is not None:
                note.tags = list(tags)
                changed.append("tags")

            col.update_note(note)

            moved_from: list[str] = []
            if target is not None:
                card_ids = note.card_ids()
                # Read BEFORE the move, and reported back: a caller who named
                # the wrong note should be able to see where its cards actually
                # came from instead of inferring it from a bare success.
                moved_from = sorted(
                    {col.decks.name(col.get_card(cid).did) for cid in card_ids}
                )
                # set_deck preserves each card's scheduling and review history.
                col.set_deck(card_ids, target)
                if moved_from != [deck]:
                    changed.append("deck")

            emit(
                EVENT_MUTATION,
                action="update_note",
                tool="update_note",
                note_id=note_id,
                fields_changed=len(fields or {}),
                tags_changed="tags" in changed,
                deck_changed="deck" in changed,
            )
            result = serialize_note(col, note_id)
            result["updated"] = sorted(set(changed))
            if deck is not None:
                result["moved"] = {
                    "from": moved_from,
                    "to": deck,
                    "cards": len(note.card_ids()),
                }
            if audio_note:
                result["audio"] = audio_note
            result["synced"] = False
            return result

        return await worker.run(op)

    @mcp.tool(annotations=WRITES)
    @instrumented("sync_to_ankiweb", mutation=True)
    async def sync_to_ankiweb(include_media: bool = True) -> dict[str, Any]:
        """Push local changes to AnkiWeb so other devices can pull them.

        This is deliberately explicit -- adding or editing notes does not sync.

        If AnkiWeb requires a *full* sync (one side must overwrite the other),
        this tool refuses and reports which direction is needed rather than
        guessing. Resolving that destroys one side's data, so it is a human
        decision; see the README.
        """
        # Login and sync happen in one hop onto the worker thread: sync_login
        # drives the same backend as everything else and must not be called
        # from the event loop.
        def op(col: Collection) -> tuple[SyncCollectionResponse, str]:
            auth = resolve_auth(config, col)
            return col.sync_collection(auth, include_media), auth.hkey

        try:
            output, hkey = await worker.run(op)
        except SyncCredentialsError as exc:
            raise ToolError(str(exc)) from exc

        # AnkiWeb shards accounts and can hand back a new endpoint; persist it
        # with the existing key so the next sync goes straight to the right host.
        if output.new_endpoint:
            store_hkey(config, hkey, output.new_endpoint)

        required = output.required
        Required = SyncCollectionResponse.ChangesRequired

        if required in (Required.FULL_SYNC, Required.FULL_UPLOAD, Required.FULL_DOWNLOAD):
            direction = {
                Required.FULL_UPLOAD: "a full UPLOAD (local overwrites AnkiWeb)",
                Required.FULL_DOWNLOAD: "a full DOWNLOAD (AnkiWeb overwrites local)",
                Required.FULL_SYNC: "a full sync in a direction AnkiWeb did not specify",
            }[required]
            emit(
                EVENT_SYNC,
                level=logging.WARNING,
                outcome="full_sync_required",
                direction=Required.Name(required),
            )
            raise ToolError(
                f"AnkiWeb requires {direction}. Nothing was synced. A full sync "
                "discards one side's changes entirely, so this server will not "
                "perform one automatically -- see the 'Full sync' section of the "
                "README to resolve it deliberately. "
                f"AnkiWeb said: {output.server_message or '(no message)'}"
            )

        result: dict[str, Any] = {
            # The sync has already run by this point; `required` says what is
            # still outstanding, so NO_CHANGES means it finished cleanly -- not
            # that nothing was uploaded.
            "collection_sync": "completed"
            if required in (Required.NO_CHANGES, Required.NORMAL_SYNC)
            else f"unexpected state: {Required.Name(required)}",
            "server_message": output.server_message or None,
        }

        if include_media:
            result["media_sync"] = await _await_media_sync(worker)
        emit(
            EVENT_SYNC,
            outcome=result["collection_sync"].replace(" ", "_"),
            media=result.get("media_sync"),
        )
        return result


async def _await_media_sync(worker: CollectionWorker) -> str:
    """Wait for the background media sync that sync_collection kicked off.

    Media sync is asynchronous and separate from the collection sync, so it is
    polled here. Each poll is a tiny call onto the worker thread, with the wait
    happening on the event loop, so other tools are not blocked meanwhile.
    """
    deadline = asyncio.get_running_loop().time() + MEDIA_SYNC_TIMEOUT_SECONDS
    while True:
        status = await worker.run(lambda col: col.media_sync_status())
        if not status.active:
            return "completed"
        if asyncio.get_running_loop().time() >= deadline:
            log.warning("media sync still running after %ss", MEDIA_SYNC_TIMEOUT_SECONDS)
            return (
                "still running -- the collection sync finished; media will "
                "catch up in the background"
            )
        await asyncio.sleep(MEDIA_POLL_INTERVAL_SECONDS)
