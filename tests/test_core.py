"""Regression tests pinning bugs found in review.

The OAuth package here and obsidian-mcp's are ports of the same original, so
they shared these bugs. These tests are adapted from that project's
tests/test_core.py, kept deliberately close to it so the two copies cannot
drift apart on the fixes the way they drifted into the bugs.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import pytest


# --- Copilot review of anki-mcp PR #2 ---------------------------------------

def test_client_ip_uses_the_last_forwarded_hop() -> None:
    """X-Forwarded-For is client-appendable.

    A caller who sends their own header, in front of a proxy that appends
    rather than replaces, leaves a forged value at the HEAD of the list. Keying
    the login rate limiter on that lets an attacker mint a fresh bucket per
    request by rotating it.

    Measured against the live deployment, Tailscale REPLACES this header, so
    the bug was not exploitable as deployed -- but a change of proxy would make
    it live, which is exactly what this test is here to catch.
    """
    from anki_mcp.oauth.login import _client_ip

    class _Req:
        def __init__(self, xff=None, peer="9.9.9.9"):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": peer})()

    assert _client_ip(_Req("evil, 1.2.3.4")) == "1.2.3.4"
    assert _client_ip(_Req("1.2.3.4")) == "1.2.3.4"
    # Whitespace around hops: a naive split keeps the padding.
    assert _client_ip(_Req("a , b , 1.2.3.4 ")) == "1.2.3.4"
    assert _client_ip(_Req(None)) == "9.9.9.9"
    # All-empty must fall through to the socket peer. Returning "" would bucket
    # every caller together -- failing in the opposite direction, and quietly.
    assert _client_ip(_Req(" , , ")) == "9.9.9.9"


def _build_redirect(redirect_uri: str, code: str, state: str | None) -> str:
    """The construction under test, kept local so this file does not depend on
    provider internals and stays portable between the two forks."""
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("code", code))
    if state:
        query.append(("state", state))
    return urlunsplit(parts._replace(query=urlencode(query)))


def test_redirect_url_encodes_state_and_respects_fragments() -> None:
    """`state` is client-chosen so it must be encoded, and the old
    `"?" in uri` test put parameters inside a fragment."""
    nasty = 'a&b=c#frag "quoted"'
    url = _build_redirect("https://claude.ai/cb?x=1", "CODE123", nasty)
    q = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert q["state"] == [nasty]
    assert q["code"] == ["CODE123"]
    assert q["x"] == ["1"]
    assert "#frag" not in urlsplit(url).fragment

    url = _build_redirect("https://claude.ai/cb#section", "C", "s")
    parts = urlsplit(url)
    assert parts.fragment == "section"
    assert parse_qs(parts.query)["code"] == ["C"]


def test_provider_builds_the_same_redirect_as_the_helper(tmp_path) -> None:
    """The helper above pins BEHAVIOUR, not this implementation -- refactor the
    provider and it would not notice. This ties the two together."""
    from anki_mcp.oauth.provider import AnkiOAuthProvider
    from anki_mcp.oauth.store import Store

    provider = AnkiOAuthProvider(Store(tmp_path / "oauth.db"))
    login_id = "L"
    nasty = 'a&b=c#frag "quoted"'
    provider._store.put_pending(
        login_id,
        {
            "client_id": "c1",
            "redirect_uri": "https://claude.ai/cb?x=1",
            "redirect_uri_provided_explicitly": True,
            "code_challenge": "ch",
            "state": nasty,
            "scopes": ["anki"],
            "resource": None,
        },
        600,
    )
    url = provider.complete_login(login_id)
    q = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert q["state"] == [nasty]
    assert q["x"] == ["1"]
    assert q["code"]


def test_malformed_totp_secret_exits_cleanly(tmp_path, monkeypatch) -> None:
    """The bug was not that normalise_secret raises -- it is that the exception
    escaped build() as a stack trace, skipping worker.close(). So assert on the
    exit code from main(), which is the behaviour that actually broke."""
    from anki_mcp.oauth.totp import InvalidTOTPSecret, normalise_secret

    with pytest.raises(InvalidTOTPSecret) as exc:
        normalise_secret("not-valid-base32!!!")
    assert "base32" in str(exc.value)
    assert normalise_secret("jbsw y3dp ehpk 3pxp") == normalise_secret("JBSWY3DPEHPK3PXP")

    from anki_mcp.__main__ import main

    for key, value in {
        "ANKI_MCP_DATA_DIR": str(tmp_path / "data"),
        "ANKI_MCP_STATE_DIR": str(tmp_path / "config"),
        "ANKI_MCP_AUTH_MODE": "oauth",
        "ANKI_MCP_LOGIN_PASSWORD": "correct-horse-battery-staple",
        "ANKI_MCP_TOTP_SECRET": "!!!not-base32!!!",
        "ANKI_MCP_PUBLIC_URL": "http://127.0.0.1:8999",
        "ANKI_MCP_PORT": "8999",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    # Exits 2 rather than raising, and never reaches uvicorn.
    assert main() == 2
    # The collection was released on the way out: it reopens without a lock error.
    from anki.collection import Collection

    col = Collection(str(tmp_path / "data" / "collection.anki2"))
    col.close()


# --- Adapted from obsidian-mcp's move-ordering bug ---------------------------

def test_oauth_mode_requires_a_public_url(tmp_path, monkeypatch) -> None:
    """There is no built-in default host: OAuth redirects must carry the
    operator's own public address, so a missing one stops startup with an
    error naming the variable rather than advertising someone else's URL."""
    from anki_mcp import config

    for key, value in {
        "ANKI_MCP_DATA_DIR": str(tmp_path / "data"),
        "ANKI_MCP_STATE_DIR": str(tmp_path / "config"),
        "ANKI_MCP_AUTH_MODE": "oauth",
        "ANKI_MCP_LOGIN_PASSWORD": "correct-horse-battery-staple",
        "ANKI_MCP_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ANKI_MCP_PUBLIC_URL", raising=False)

    with pytest.raises(config.ConfigError, match="ANKI_MCP_PUBLIC_URL"):
        config.load()

    monkeypatch.setenv("ANKI_MCP_PUBLIC_URL", "https://anki.example.ts.net")
    assert config.load().public_url == "https://anki.example.ts.net"


def test_store_image_can_share_a_filename_between_notes(tmp_path) -> None:
    """Media files are named from the search query, so two notes can end up
    pointing at ONE file.

    Nothing in the server deletes media, so this is latent rather than live.
    It is pinned here because the collection currently has no shared generated
    images by accident of naming, not by construction -- and the next thing
    that deletes media by filename must resolve references first rather than
    trusting that. This is the same shape as the obsidian-mcp bug where a move
    unlinked the source before resolving which links pointed at it.
    """
    from anki.collection import Collection

    from anki_mcp.images import store_image

    col = Collection(str(tmp_path / "collection.anki2"))
    try:
        data = b"\xff\xd8\xff" + b"identical bytes" * 10
        first = store_image(col, "Bahnhof", data, ".jpg")
        second = store_image(col, "Bahnhof", data, ".jpg")
        assert first == second, (
            "same query and same bytes yield one file: deleting it for one "
            "note would break the other"
        )
    finally:
        col.close()


def test_gloss_lines_splits_on_breaks_and_ignores_blanks() -> None:
    """The English field is one field holding several lines.

    Adding "Definition (EN)" and "Example (EN)" fields would be a note type
    schema change, and AnkiWeb answers that with a full sync -- so the lines
    live in one field, separated by <br>, and the count is what the add_note
    check counts.
    """
    from anki_mcp.tools.write import gloss_lines

    assert gloss_lines("pocket money") == ["pocket money"]
    assert gloss_lines(
        "pocket money<br>Money children get regularly.<br />"
        "My daughter gets 25 euros a month.\n"
    ) == [
        "pocket money",
        "Money children get regularly.",
        "My daughter gets 25 euros a month.",
    ]
    assert gloss_lines("<br><br>") == []


def test_compound_ipa_joins_real_parts_and_demotes_the_second_stress() -> None:
    """German compounds are productive, so a real word can have no entry.

    "Budgetgeld" is not in Wiktionary but Budget and Geld both are, and the
    compound's transcription is theirs joined with the second element's stress
    demoted to secondary. Nothing is invented: a split whose parts have no
    entry finds nothing and is dropped.
    """
    from anki_mcp.pronunciation import compound_ipa

    entries = {"Budget": "byˈdʒeː", "Geld": "ɡɛlt"}
    seen: list[str] = []

    def fake_lookup(part: str) -> str | None:
        seen.append(part)
        return entries.get(part)

    ipa, parts = compound_ipa("das Budgetgeld", lookup=fake_lookup)
    assert ipa == "byˈdʒeːˌɡɛlt"
    assert parts == "Budget + Geld"

    # Longest first element first, so the wrong seams are tried and rejected
    # before the real one -- and the whole search stays inside its budget.
    from anki_mcp.pronunciation import MAX_SPLIT_LOOKUPS

    assert seen.index("Eld") < seen.index("Geld")
    assert len(seen) <= MAX_SPLIT_LOOKUPS

    # A word whose parts are not entries stays unresolved rather than guessed.
    assert compound_ipa("Quatschwurstel", lookup=lambda part: None) == (None, None)

    # Phrases are not compounds and must not be split -- not merely fail to
    # resolve, but never reach a lookup at all.
    seen.clear()
    assert compound_ipa("Wir sehen uns", lookup=fake_lookup) == (None, None)
    assert seen == []


def test_compound_ipa_strips_the_linking_element() -> None:
    """Taschengeld is Tasche + Geld: the "n" is a Fugenlaut, not part of the stem."""
    from anki_mcp.pronunciation import compound_ipa

    entries = {"Tasche": "ˈtaʃə", "Geld": "ɡɛlt"}
    ipa, parts = compound_ipa("das Taschengeld", lookup=entries.get)
    assert parts == "Tasche + Geld"
    assert ipa == "ˈtaʃəˌɡɛlt"


def test_compound_ipa_gives_an_unmarked_head_the_primary_stress() -> None:
    """An unmarked head must still end up carrying the primary stress.

    Orts is a monosyllable and its page transcribes it bare (ɔʁt͡s), since
    nothing there competes for stress. Gruppe carries its own primary mark,
    which compounding demotes to secondary -- so joined naively the result had
    a secondary mark and no primary at all, which is not a possible German
    word. Wiktionary's own entry is ˈɔʁt͡sˌɡʁʊpə.
    """
    from anki_mcp.pronunciation import compound_ipa

    entries = {"Orts": "ɔʁt͡s", "Gruppe": "ˈɡʁʊpə"}
    ipa, parts = compound_ipa("die Ortsgruppe", lookup=entries.get)
    assert parts == "Orts + Gruppe"
    assert ipa == "ˈɔʁt͡sˌɡʁʊpə"


def test_compound_ipa_prefers_the_unstripped_head() -> None:
    """A trailing "er" is usually a plural, not a linking element.

    Kleidergeld is Kleider + Geld. Peeling the "er" first would find Kleid,
    which is a real entry and the wrong word, so the unstripped form wins when
    it resolves.
    """
    from anki_mcp.pronunciation import compound_ipa

    entries = {"Kleider": "ˈklaɪ̯dɐ", "Kleid": "klaɪ̯t", "Geld": "ɡɛlt"}
    ipa, parts = compound_ipa("das Kleidergeld", lookup=entries.get)
    assert parts == "Kleider + Geld"
    assert ipa == "ˈklaɪ̯dɐˌɡɛlt"


def test_only_fields_with_letters_need_an_english_line() -> None:
    """A digit is not German, so it does not need translating.

    The numbers deck puts "9" in Definition (DE) -- language-neutral, and
    already understood by anyone reading the card. Requiring its English
    produced "nine / 9 / My daughter is nine years old.", where the middle
    line says nothing.
    """
    from anki_mcp.tools.write import needs_english_line

    assert needs_english_line("Geld, das Kinder bekommen.") is True
    assert needs_english_line("1.000.000.000 — tausend Millionen") is True
    assert needs_english_line("9") is False
    assert needs_english_line("1.000.000") is False
    assert needs_english_line("<div>235</div>") is False

def test_example_audio_is_appended_not_given_its_own_field(tmp_path) -> None:
    """The sentence recording rides in "Example (DE)" as a [sound:] tag.

    A field of its own would be a note type schema change, and AnkiWeb answers
    a schema change by demanding a full sync -- which sync_to_ankiweb refuses.
    Anki plays a [sound:] tag from any field, so appending costs nothing and
    keeps the audio on the answer sides only, where Example (DE) is shown.
    """
    import anki.collection
    from anki_mcp.pronunciation import store_audio

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        sentence = "Meine Tochter ist neun Jahre alt."
        tag = store_audio(col, "neun-beispiel", b"ID3fake-mp3-bytes", ".mp3")
        assert tag.startswith("[sound:") and tag.endswith("]")

        combined = f"{sentence} {tag}".strip()
        # The sentence text survives intact -- a card that lost its example to
        # the audio tag would be worse than having no audio at all.
        assert combined.startswith(sentence)
        assert combined.count("[sound:") == 1

        # Re-running must not stack a second recording. That is decided by the
        # guard, so test the guard rather than asserting it about a string this
        # test built itself.
        from anki_mcp.tools.write import needs_example_audio

        assert needs_example_audio(sentence) is True
        assert needs_example_audio(combined) is False      # already recorded
        assert needs_example_audio("") is False
        assert needs_example_audio("   ") is False
    finally:
        col.close()


def test_sentence_context_splits_around_the_word() -> None:
    """TTS context comes from the card's own example, or not at all.

    A one-syllable word synthesised alone has nothing to disambiguate it --
    "neun" came back sounding like "nein". The sentence around it is handed to
    ElevenLabs as previous_text/next_text, which it reads but does not speak.
    """
    from anki_mcp.tools.write import sentence_context

    assert sentence_context("neun", "Meine Tochter ist neun Jahre alt.") == (
        "Meine Tochter ist", "Jahre alt.")
    # The article is stripped before matching, so the headword is found in the
    # sentence even though the sentence never contains "die Million".
    assert sentence_context("die Million", "München hat eineinhalb Millionen Einwohner.") == (
        "München hat eineinhalb", "Einwohner.")
    # An audio tag already in the field is not context.
    assert sentence_context("zehn", "Ich habe zehn Euro. [sound:x.mp3]") == (
        "Ich habe", "Euro.")
    # No occurrence, no invented context -- guessing how it should sound is
    # worse than giving the model nothing.
    assert sentence_context("acht", "Der Zug faehrt um vier Uhr.") == ("", "")
    assert sentence_context("acht", "") == ("", "")
    # HTML on either side is stripped before matching: a word bolded in the
    # editor must not silently lose its context.
    assert sentence_context("<b>neun</b>", "Meine Tochter ist neun Jahre alt.") == (
        "Meine Tochter ist", "Jahre alt.")
    assert sentence_context("neun", "<i>Meine Tochter ist neun Jahre alt.</i>") == (
        "Meine Tochter ist", "Jahre alt.")


def test_synthesis_terminates_a_bare_word() -> None:
    """A word with no terminal punctuation is read as a fragment and clipped.

    "neun" came back cut off mid-vowel. A full stop gives the model a cadence
    to finish on. Compared against an ellipsis (pause too long to drill
    against) and against keeping the following context (still clipped, because
    the model had somewhere to run on to).
    """
    import anki_mcp.pronunciation as pr

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["text"] = __import__("json").loads(req.data)["text"]
        raise TimeoutError("stop here -- the request body is what is under test")

    original = pr.urllib.request.urlopen
    pr.urllib.request.urlopen = fake_urlopen
    try:
        for given, expected in (("neun", "neun."),
                                ("neun.", "neun."),
                                ("Wie geht's?", "Wie geht's?"),
                                ("Ich zähle …", "Ich zähle …")):
            try:
                pr.synthesize_elevenlabs(given, "k", "v", "m")
            except pr.PronunciationError:
                pass
            assert seen["text"] == expected, f"{given!r} -> {seen['text']!r}"
    finally:
        pr.urllib.request.urlopen = original


def test_sentence_context_tolerates_inflection_and_compounds() -> None:
    """The example holds an inflected form, not the headword.

    An exact match skipped exactly the cards that needed context most --
    Pantoffel/Pantoffeln, lügen/lügt, Heft/Hefte, hunderttausend inside
    zweihunderttausend.
    """
    from anki_mcp.tools.write import sentence_context

    assert sentence_context("der Pantoffel", "Er läuft in Pantoffeln durch die Wohnung.")[0] == "Er läuft in"
    assert sentence_context("lügen", "Er lügt, wenn er das sagt.")[0] == "Er"
    assert sentence_context("das Heft", "Die Schüler schreiben in ihre Hefte.")[0] == "Die Schüler schreiben in ihre"
    assert sentence_context("die Million", "München hat eineinhalb Millionen Einwohner.")[0] == "München hat eineinhalb"
    # Inside a longer word, only once the word-initial form has failed.
    assert sentence_context("hunderttausend", "Freiburg hat mehr als zweihunderttausend Einwohner.")[0] == "Freiburg hat mehr als"
    # A word that opens its example has no leading context; add_note supplies
    # the generic lead-in rather than inventing a sentence.
    assert sentence_context("Servus", "Servus, schön dich zu sehen!")[0] == ""


def test_spoken_text_ignores_audio_tags_and_markup() -> None:
    """Deciding "did the text change" must compare what is SAID.

    The stored example carries its own [sound:] tag, so a caller re-sending the
    same sentence in plain form would otherwise look like an edit and re-record
    audio that was already right.
    """
    from anki_mcp.tools.write import spoken_text

    stored = "Meine Tochter ist neun Jahre alt. [sound:anki-mcp-neun-beispiel.mp3]"
    assert spoken_text(stored) == "Meine Tochter ist neun Jahre alt."
    assert spoken_text(stored) == spoken_text("Meine Tochter ist neun Jahre alt.")
    # Formatting is not a change in what is spoken.
    assert spoken_text("<b>neun</b>") == spoken_text("neun")
    # A real edit still registers.
    assert spoken_text("Meine Tochter ist zehn Jahre alt.") != spoken_text(stored)
    assert spoken_text(None) == ""


def test_create_deck_is_idempotent_and_nests(tmp_path) -> None:
    """"Make sure this deck exists" is what every caller actually means.

    Failing on the second call would make a retry after a dropped connection
    destructive to the workflow rather than harmless.
    """
    import anki.collection
    from anki_mcp.errors import ToolError
    from anki_mcp.tools.write import create_deck_op

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        first = create_deck_op(col, "German Numbers", "Zahlen von null bis Billion.")
        assert first["created"] is True
        assert first["description"] == "Zahlen von null bis Billion."

        again = create_deck_op(col, "German Numbers")
        assert again["created"] is False
        assert again["deck_id"] == first["deck_id"]
        # An existing description is reported back, not blanked by a second call
        # that happened not to pass one.
        assert again["description"] == "Zahlen von null bis Billion."

        # Nesting creates the parent too, so a subdeck needs no setup.
        child = create_deck_op(col, "German Topics::Banking & Money")
        assert child["created"] is True
        assert col.decks.by_name("German Topics") is not None

        # Every level must be a real name, including levels in the MIDDLE:
        # checking only the ends let "German::::Banking" through, and Anki
        # would have made a nameless deck between the two.
        for bad in ("", "   ", "::", "German::", "::German",
                    "German::::Banking", "German::  ::Banking", "a::::::b"):
            try:
                create_deck_op(col, bad)
            except ToolError:
                pass
            else:
                raise AssertionError(f"{bad!r} should have been refused")
    finally:
        col.close()


def test_emit_survives_a_reserved_logrecord_key() -> None:
    """A telemetry attribute must never be able to fail the tool it describes.

    create_deck emitted `created=`, which is the LogRecord's own timestamp.
    logging.makeRecord raises KeyError rather than shadowing it, so the deck was
    made, committed, and the caller still got a bare "Error executing tool
    create_deck" with no message -- the failure looked like it came from Anki,
    and the tool worked fine in the unit tests because they call
    create_deck_op() directly and never go through emit().
    """
    import logging

    from anki_mcp.telemetry import emit

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("anki_mcp.telemetry")
    handler = Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        # The guard is load-bearing: plain logging really does reject this.
        with pytest.raises(KeyError):
            logger.info("raw", extra={"created": True})

        emit("mutation", tool="create_deck", created=True, module="x", deck="A")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert [r.getMessage() for r in records] == ["mutation"]
    record = records[0]
    # Renamed, not dropped -- the value is the point of the event.
    assert record.attr_created == "true"
    assert record.attr_module == "x"
    # The record keeps its own meaning for the reserved names.
    assert isinstance(record.created, float)
    # Ordinary keys are untouched, so existing dashboard queries still match.
    assert record.tool == "create_deck"
    assert record.deck == "A"


def test_emit_covers_reserved_keys_from_inside_an_asyncio_task() -> None:
    """The reserved set is built at import time; emit() runs inside a task.

    Raised in review: 3.12's `taskName` might be added to a LogRecord only when
    one is created inside a running task, which would leave it out of a set
    derived at import and let it raise from the async `instrumented` wrapper --
    the only context emit() is ever actually called from.

    Measured, that is not how CPython behaves: LogRecord.__init__ assigns
    `self.taskName = None` unconditionally and only fills it in when there is a
    task, so the attribute is present either way and the set already covers it.
    This test pins that, because the reasoning is not obvious from the source
    and a future Python could make the assignment conditional.
    """
    import asyncio
    import logging

    from anki_mcp.telemetry import _RESERVED_LOGRECORD_KEYS, emit

    # The import-time set and one built inside a task must agree -- that
    # equality is the whole assumption.
    async def keys_in_task() -> set[str]:
        return set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)

    assert asyncio.run(keys_in_task()) <= set(_RESERVED_LOGRECORD_KEYS)
    assert "taskName" in _RESERVED_LOGRECORD_KEYS

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("anki_mcp.telemetry")
    handler = Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        async def emit_in_task() -> None:
            emit("mutation", tool="create_deck", taskName="boom", created=True)

        asyncio.run(emit_in_task())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert len(records) == 1
    record = records[0]
    assert record.attr_taskName == "boom"
    assert record.attr_created == "true"
    # The record keeps the real task name for its own field -- the point is
    # that our attribute did not displace it. Not pinned to "Task-1": the
    # asyncio task counter is process-global, so the number depends on what
    # else ran first in the session.
    assert record.taskName != "boom"
    assert record.taskName.startswith("Task-")

def test_delete_deck_refuses_to_destroy_cards_by_default(tmp_path) -> None:
    """The dangerous default here is Anki's, not ours.

    col.decks.remove() deletes the deck's cards and every subdeck without
    asking. For a tool a model calls off a one-line name, a typo must not be
    able to destroy reviewed cards, so a non-empty deck is refused unless the
    caller says delete_cards=True about that specific deck.
    """
    import anki.collection
    from anki_mcp.errors import ToolError
    from anki_mcp.tools.write import create_deck_op, delete_deck_op

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        # An empty deck goes without ceremony -- the tidy-up case.
        create_deck_op(col, "German Time &amp; Dates")
        gone = delete_deck_op(col, "German Time &amp; Dates")
        assert gone["cards_deleted"] == 0
        assert col.decks.by_name("German Time &amp; Dates") is None

        # A deck with a card in it is refused, and says how many are at stake.
        create_deck_op(col, "German Numbers")
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "neun", "nine"
        col.add_note(note, col.decks.by_name("German Numbers")["id"])

        with pytest.raises(ToolError) as caught:
            delete_deck_op(col, "German Numbers")
        assert "1 card" in str(caught.value)
        assert col.decks.by_name("German Numbers") is not None

        # Explicit consent gets it through.
        forced = delete_deck_op(col, "German Numbers", delete_cards=True)
        assert forced["cards_deleted"] == 1
        assert col.decks.by_name("German Numbers") is None

        # A parent's count INCLUDES its subdecks, because remove() takes them
        # too. Counting only the parent's own cards would call this "empty" and
        # delete the child's card without ever mentioning it.
        create_deck_op(col, "German Topics::Banking & Money")
        child = col.new_note(col.models.by_name("Basic"))
        child["Front"], child["Back"] = "das Konto", "account"
        col.add_note(child, col.decks.by_name("German Topics::Banking & Money")["id"])

        with pytest.raises(ToolError) as caught:
            delete_deck_op(col, "German Topics")
        assert "1 card" in str(caught.value)
        assert "subdeck" in str(caught.value)

        removed = delete_deck_op(col, "German Topics", delete_cards=True)
        assert removed["cards_deleted"] == 1
        assert removed["subdecks_deleted"] == ["German Topics::Banking & Money"]
        assert col.decks.by_name("German Topics::Banking & Money") is None

        # Default survives deletion -- Anki recreates it, so claiming success
        # would be a lie.
        with pytest.raises(ToolError):
            delete_deck_op(col, "Default")

        # An unknown name is an error, not a silent no-op: a caller that
        # misspelled the deck should hear about it rather than believe the
        # cleanup happened.
        with pytest.raises(ToolError):
            delete_deck_op(col, "German Time & Dates")

        # Same normalisation as create, so a name create accepted can be
        # deleted by that same string.
        for bad in ("", "   ", "::", "German::"):
            with pytest.raises(ToolError):
                delete_deck_op(col, bad)
    finally:
        col.close()


# Selection tokens are minted by search_notes in production. These tests call
# delete_notes_op directly, so they mint their own from the same helper.
_STALE_TOKEN = "0" * 16 + ":0"


def _sel(col, query: str) -> str:
    """The token search_notes would hand out for this query, right now."""
    from anki_mcp.tools._common import selection_token

    return selection_token(col, col.find_notes(query))


def test_delete_notes_requires_the_count_to_match(tmp_path) -> None:
    """A search is the most dangerous way to choose what to destroy.

    `tag:zeit` and `tag:zeit*` and a typo'd `tag:ziet` all look equally
    plausible in a tool call, and in a collection that has `zeit::3-monate`
    the first two differ by every month note. Requiring the caller to state
    the count means they had to run search_notes and look; it also turns a
    collection that moved underneath them into a refusal rather than a
    surprise.
    """
    import anki.collection
    from anki_mcp.errors import ToolError
    from anki_mcp.tools.write import create_deck_op, delete_notes_op

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        create_deck_op(col, "German Numbers")
        deck_id = col.decks.by_name("German Numbers")["id"]
        basic = col.models.by_name("Basic")

        def add(front: str, *tags: str) -> None:
            note = col.new_note(basic)
            note["Front"], note["Back"] = front, front.upper()
            note.tags = list(tags)
            col.add_note(note, deck_id)

        for word in ("der Januar", "der Februar", "der Maerz"):
            add(word, "zeit", "zeit::3-monate")
        add("am dritten Mai", "zeit", "zeit::4-datum")
        # The notes that must survive every query below.
        for word in ("null", "eins"):
            add(word, "zahlen")

        # Wrong count refuses and reports the real one, changing nothing.
        with pytest.raises(ToolError) as caught:
            delete_notes_op(col, "tag:zeit", expect_count=3, selection=_STALE_TOKEN)
        assert "matches 4" in str(caught.value)
        assert len(col.find_notes("tag:zeit")) == 4

        # A query matching nothing is an error, not a silent success: a
        # misspelled tag matches nothing rather than erroring, and "deleted 0"
        # would read as "the cleanup happened".
        with pytest.raises(ToolError) as caught:
            delete_notes_op(col, "tag:ziet", expect_count=0, selection=_STALE_TOKEN)
        assert "no notes" in str(caught.value)

        # An empty query would match the whole collection.
        for blank in ("", "   "):
            with pytest.raises(ToolError):
                delete_notes_op(col, blank, expect_count=6, selection=_STALE_TOKEN)
        assert col.note_count() == 6

        # Invalid search syntax surfaces as a ToolError, not a raw anki error.
        with pytest.raises(ToolError) as caught:
            delete_notes_op(col, '"unclosed', expect_count=1, selection=_STALE_TOKEN)
        assert "not a valid Anki search" in str(caught.value)

        # The narrow query deletes only its own notes, and reports what went.
        result = delete_notes_op(
            col, "tag:zeit::4-datum", expect_count=1,
            selection=_sel(col, "tag:zeit::4-datum"),
        )
        assert result["notes_deleted"] == 1
        assert result["cards_deleted"] == 1
        assert [d["word"] for d in result["deleted"]] == ["am dritten Mai"]

        # Correct count on the remainder goes through.
        result = delete_notes_op(
            col, "tag:zeit", expect_count=3,
            selection=_sel(col, "tag:zeit"),
        )
        assert result["notes_deleted"] == 3
        assert sorted(d["word"] for d in result["deleted"]) == [
            "der Februar",
            "der Januar",
            "der Maerz",
        ]

        # The untagged notes were never in scope and are still here.
        assert col.note_count() == 2
        assert len(col.find_notes("tag:zahlen")) == 2
    finally:
        col.close()


def test_delete_notes_names_what_it_deleted_for_any_note_type(tmp_path) -> None:
    """The deletion list has to be able to name a note it did not design.

    Raised in review on #38: "Word" and "Front" cover this collection and stock
    Anki, but add_note accepts any note type. One whose first field is "Term"
    reported an empty word, so the list that exists to show a caller what they
    just destroyed could not identify a single note in it.
    """
    import anki.collection
    from anki_mcp.tools.write import create_deck_op, delete_notes_op

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        notetype = col.models.new("Glossary")
        for field in ("Term", "Meaning"):
            col.models.add_field(notetype, col.models.new_field(field))
        template = col.models.new_template("Card 1")
        template["qfmt"], template["afmt"] = "{{Term}}", "{{Meaning}}"
        col.models.add_template(notetype, template)
        col.models.add_dict(notetype)
        notetype = col.models.by_name("Glossary")

        create_deck_op(col, "Glossar")
        deck_id = col.decks.by_name("Glossar")["id"]
        note = col.new_note(notetype)
        note["Term"], note["Meaning"] = "der Vollstreckungsbescheid", "enforcement order"
        note.tags = ["stray"]
        col.add_note(note, deck_id)

        result = delete_notes_op(
            col, "tag:stray", expect_count=1,
            selection=_sel(col, "tag:stray"),
        )
        # Neither "Word" nor "Front" exists on this note type.
        assert [d["word"] for d in result["deleted"]] == ["der Vollstreckungsbescheid"]
    finally:
        col.close()


def test_delete_notes_does_not_relabel_a_database_error(tmp_path) -> None:
    """Only SearchError means "your query is wrong".

    Raised in review on #38: catching bare Exception around find_notes turned a
    DBError into "not a valid Anki search", which sends the caller off to
    rewrite a query that was fine while the real fault -- the one
    CollectionWorker already knows how to describe -- goes unreported.
    """
    import anki.collection
    from anki.errors import DBError
    from anki_mcp.errors import ToolError
    from anki_mcp.tools.write import delete_notes_op

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        def boom(_query: str) -> None:
            raise DBError("database is locked", None, "", "")

        col.find_notes = boom  # type: ignore[method-assign]

        with pytest.raises(DBError):
            delete_notes_op(col, "tag:zeit", expect_count=1, selection=_STALE_TOKEN)

        # And a genuine search error still becomes a ToolError.
        del col.find_notes
        with pytest.raises(ToolError) as caught:
            delete_notes_op(col, '"unclosed', expect_count=1, selection=_STALE_TOKEN)
        assert "not a valid Anki search" in str(caught.value)
    finally:
        col.close()


def test_delete_notes_telemetry_does_not_carry_the_query() -> None:
    """A search string is collection content, and emit() ships to Loki.

    Raised in review on #38: a query carries exact field text -- "der Hund",
    front:Kontoauszug* -- so logging it verbatim publishes what is in the
    collection to the telemetry backend. The digest distinguishes two deletions
    and spots one query being retried without carrying what it said.
    """
    import hashlib
    import logging

    from anki_mcp.telemetry import emit

    query = "front:Kontoauszug* tag:privat"
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("anki_mcp.telemetry")
    handler = Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        emit(
            "mutation",
            tool="delete_notes",
            query_digest=hashlib.sha256(query.encode()).hexdigest()[:12],
            query_length=len(query),
            notes_deleted=3,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    record = records[0]
    assert not hasattr(record, "query")
    # Nothing on the record repeats the query text, in any attribute.
    assert all(
        query not in str(value)
        for key, value in vars(record).items()
        if key not in ("args", "exc_info")
    )
    assert record.query_digest == hashlib.sha256(query.encode()).hexdigest()[:12]
    assert record.query_length == len(query)


def test_moving_a_note_keeps_its_scheduling(tmp_path) -> None:
    """Moving is the repair for a misfiled note; deleting and re-adding is not.

    A mobile session filed 18 time notes into German Numbers. They had audio and
    review history, so the fix had to preserve both -- delete-and-recreate would
    have reset every card to new and thrown the history away, which is why
    update_note grew a `deck` argument rather than the cleanup being done with
    delete_notes.

    This exercises the same anki calls the tool's op makes; the tool itself is
    an async closure over the MCP server and cannot be called directly here.
    """
    import anki.collection
    from anki_mcp.errors import ToolError
    from anki_mcp.tools._common import resolve_deck_id
    from anki_mcp.tools.write import create_deck_op

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        create_deck_op(col, "German Numbers")
        create_deck_op(col, "German Time & Dates::1 Bausteine")
        source = col.decks.by_name("German Numbers")["id"]

        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "der Januar", "January"
        col.add_note(note, source)

        # Give the card a review history, which is the thing at risk.
        card = col.get_card(note.card_ids()[0])
        card.type, card.queue, card.ivl, card.reps, card.lapses = 2, 2, 21, 7, 1
        col.update_card(card)

        target = resolve_deck_id(col, "German Time & Dates::1 Bausteine")
        card_ids = note.card_ids()
        moved_from = sorted({col.decks.name(col.get_card(c).did) for c in card_ids})
        assert moved_from == ["German Numbers"]

        col.set_deck(card_ids, target)

        moved = col.get_card(card_ids[0])
        assert col.decks.name(moved.did) == "German Time & Dates::1 Bausteine"
        # The whole point: scheduling survived the move.
        assert (moved.ivl, moved.reps, moved.lapses) == (21, 7, 1)
        assert (moved.type, moved.queue) == (2, 2)

        # An unknown deck is refused with the real names, not invented --
        # a typo would otherwise file the note where nobody reviews it.
        with pytest.raises(ToolError) as caught:
            resolve_deck_id(col, "German Time &amp; Dates::1 Bausteine")
        assert "German Time & Dates::1 Bausteine" in str(caught.value)
    finally:
        col.close()


def test_update_note_guard_counts_deck_as_a_change() -> None:
    """`deck` has to count as a change, or a pure move is refused.

    The guard listed fields/tags/image_url/regenerate_audio. Adding `deck`
    without adding it here would make `update_note(note_id, deck=...)` -- the
    exact call this feature exists for -- fail as "nothing to update". Every
    optional argument has to be able to stand alone.
    """
    from anki_mcp.tools.write import update_changes_something

    def asks(**kwargs) -> bool:
        args = dict(
            fields=None, tags=None, deck=None, image_url=None, regenerate_audio=None
        )
        args.update(kwargs)
        return update_changes_something(**args)

    # A call naming nothing is the mistake the guard is for.
    assert not asks()

    # Each argument stands on its own, deck included.
    assert asks(deck="German Time & Dates::1 Bausteine")
    assert asks(fields={"Word": "der Januar"})
    assert asks(tags=[])          # clearing tags IS a change
    assert asks(image_url="https://example.invalid/x.jpg")
    assert asks(regenerate_audio=True)

    # regenerate_audio=False means "do not re-record" -- on its own it asks for
    # nothing, so it must NOT satisfy the guard.
    assert not asks(regenerate_audio=False)
    # ...but it is fine alongside a real change.
    assert asks(fields={"Word": "x"}, regenerate_audio=False)


def test_update_refuses_before_it_writes_anything(tmp_path) -> None:
    """A rejected update must leave the note exactly as it was.

    Raised in review on #39: the deck was resolved AFTER col.update_note() had
    already committed the field and tag edits, so a typo'd deck name returned
    an error on a note that had nonetheless been changed. The caller sees a
    failure and has no reason to suspect a partial write happened behind it --
    and with '&' being literal, a typo'd deck name is exactly the mistake this
    collection keeps making.
    """
    import anki.collection
    from anki_mcp.errors import ToolError
    from anki_mcp.tools.write import create_deck_op, validate_update

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        create_deck_op(col, "German Numbers")
        create_deck_op(col, "German Time & Dates::1 Bausteine")
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "der Januar", "January"
        note.tags = ["zeit"]
        col.add_note(note, col.decks.by_name("German Numbers")["id"])

        # A deck that does not exist is refused, and says what does exist.
        with pytest.raises(ToolError) as caught:
            validate_update(
                col, note,
                fields={"Front": "der Februar"},
                deck="German Time &amp; Dates::1 Bausteine",
            )
        assert "German Time & Dates::1 Bausteine" in str(caught.value)

        # An unknown FIELD is refused too, and before the deck is even looked
        # at -- both rejections live ahead of the first write.
        with pytest.raises(ToolError) as caught:
            validate_update(col, note, fields={"Vorderseite": "x"}, deck=None)
        assert "Vorderseite" in str(caught.value)

        # Nothing above touched the stored note.
        stored = col.get_note(note.id)
        assert stored["Front"] == "der Januar"
        assert stored.tags == ["zeit"]
        assert col.decks.name(col.get_card(stored.card_ids()[0]).did) == "German Numbers"

        # A valid pair returns the resolved deck id for the caller to use, so
        # the move does not re-resolve a name that has already been checked.
        target = validate_update(
            col, note,
            fields={"Front": "der Februar"},
            deck="German Time & Dates::1 Bausteine",
        )
        assert target == col.decks.by_name("German Time & Dates::1 Bausteine")["id"]

        # Not moving resolves to None rather than erroring.
        assert validate_update(col, note, fields=None, deck=None) is None
    finally:
        col.close()


def _test_server(tmp_path):
    """A real MCPServer with the write tools registered, over a temp collection.

    Everything else in this file tests the module-level `*_op` helpers, which
    leaves the wiring between the tool and its op untested -- and that wiring is
    where #39's partial-write bug lived: the op validated the deck in the wrong
    ORDER, which no test of the validator alone can see.
    """
    from mcp.server.mcpserver import MCPServer

    from anki_mcp.collection import CollectionWorker
    from anki_mcp.config import AuthMode, Config, Profile
    from anki_mcp.tools import write

    config = Config(
        collection_path=tmp_path / "c.anki2", state_dir=tmp_path,
        profile=Profile.FULL, auth_mode=AuthMode.BEARER, bearer_token="t",
        login_password=None, totp_secret=None, host="127.0.0.1", port=1,
        public_url="http://x", environment="test", image_provider="none",
        google_cse_key=None, google_cse_engine_id=None, pixabay_key=None,
        tts_key=None, tts_voice="v", tts_provider="none", prefer_tts=False,
        ankiweb_username=None, ankiweb_password=None, ankiweb_endpoint=None,
    )
    worker = CollectionWorker(config.collection_path)
    worker.open()
    mcp = MCPServer(name="test")
    write.register(mcp, worker, config)
    return mcp, worker


def test_a_failed_move_leaves_the_note_untouched(tmp_path) -> None:
    """Raised in review on #39, and caught here through the real tool.

    The deck was resolved AFTER col.update_note() had committed the field and
    tag edits, so a typo'd deck name returned an error on a note that had
    nonetheless been changed -- the caller sees a failure with no reason to
    suspect a partial write behind it. With '&' being literal, a typo'd deck
    name is precisely the mistake this collection keeps making.

    Reverting the fix makes this test read "der Februar" instead of
    "der Januar", which is the bug exactly.
    """
    import asyncio

    from anki_mcp.tools.write import create_deck_op

    mcp, worker = _test_server(tmp_path)

    def seed(col):
        create_deck_op(col, "German Numbers")
        create_deck_op(col, "German Time & Dates::1 Bausteine")
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "der Januar", "January"
        note.tags = ["zeit"]
        col.add_note(note, col.decks.by_name("German Numbers")["id"])
        return note.id

    def stored(col, note_id):
        note = col.get_note(note_id)
        return {
            "front": note["Front"],
            "tags": list(note.tags),
            "deck": col.decks.name(col.get_card(note.card_ids()[0]).did),
        }

    async def scenario():
        note_id = await worker.run(seed)
        before = await worker.run(lambda col: stored(col, note_id))

        # '&amp;' is a different deck name from '&', and this one does not exist.
        with pytest.raises(Exception) as caught:
            await mcp.call_tool("update_note", {
                "note_id": note_id,
                "fields": {"Front": "der Februar"},
                "tags": ["zeit", "monat"],
                "deck": "German Time &amp; Dates::1 Bausteine",
            })
        assert "No deck named" in str(caught.value)

        # The whole point: the failed call wrote nothing.
        assert await worker.run(lambda col: stored(col, note_id)) == before

        # The same call against the real deck goes through, and moves it.
        await mcp.call_tool("update_note", {
            "note_id": note_id,
            "fields": {"Front": "der Februar"},
            "deck": "German Time & Dates::1 Bausteine",
        })
        after = await worker.run(lambda col: stored(col, note_id))
        assert after["front"] == "der Februar"
        assert after["deck"] == "German Time & Dates::1 Bausteine"

    try:
        asyncio.run(scenario())
    finally:
        worker.close()


def test_delete_notes_selection_must_describe_these_notes_now(tmp_path) -> None:
    """The count says how many. The selection says which, and when.

    Two failures the count cannot see, from PR #38's review and from this
    collection's own history:

    - A set SWAPPED underneath a stable count. Copilot raised this: remove one
      match and add another between the search and the delete, and the count
      is unchanged while the caller destroys something they never reviewed.
    - A set that did not change at all, acted on far too late. This one really
      happened: a delete planned on the 15th still matched the same 16 notes
      with the same ids on the 17th, and by then deleting them was wrong. No
      check on WHICH notes would have caught it -- only WHEN.
    """
    import time

    import anki.collection
    from anki_mcp.errors import ToolError
    from anki_mcp.tools._common import SELECTION_MAX_AGE_SECONDS, selection_token
    from anki_mcp.tools.write import create_deck_op, delete_notes_op

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        create_deck_op(col, "German Numbers")
        deck_id = col.decks.by_name("German Numbers")["id"]
        basic = col.models.by_name("Basic")

        def add(front: str, *tags: str) -> int:
            note = col.new_note(basic)
            note["Front"], note["Back"] = front, front.upper()
            note.tags = list(tags)
            col.add_note(note, deck_id)
            return note.id

        ids = {word: add(word, "zeit")
               for word in ("der Januar", "der Februar", "der Maerz")}

        token = _sel(col, "tag:zeit")

        # SWAP: one note out, one in. The count still says 3.
        col.remove_notes([ids["der Maerz"]])
        add("der April", "zeit")
        assert len(col.find_notes("tag:zeit")) == 3

        with pytest.raises(ToolError) as caught:
            delete_notes_op(col, "tag:zeit", expect_count=3, selection=token)
        assert "not the ones the selection was taken from" in str(caught.value)
        # Refused means refused: nothing went.
        assert len(col.find_notes("tag:zeit")) == 3

        # AGE: the right notes, looked at too long ago. This is the one that
        # actually bit -- same notes, same ids, stale decision.
        fresh = _sel(col, "tag:zeit")
        stale = selection_token(
            col, col.find_notes("tag:zeit"),
            now=time.time() - SELECTION_MAX_AGE_SECONDS - 60,
        )
        with pytest.raises(ToolError) as caught:
            delete_notes_op(col, "tag:zeit", expect_count=3, selection=stale)
        assert "minutes old" in str(caught.value)
        assert len(col.find_notes("tag:zeit")) == 3

        # A token the caller made up is refused rather than trusted.
        for forged in ("", "abc", "nothexbutlong:notanumber"):
            with pytest.raises(ToolError):
                delete_notes_op(
                    col, "tag:zeit", expect_count=3, selection=forged
                )
        assert len(col.find_notes("tag:zeit")) == 3

        # The honest path still works.
        result = delete_notes_op(col, "tag:zeit", expect_count=3, selection=fresh)
        assert result["notes_deleted"] == 3
        assert col.find_notes("tag:zeit") == []
    finally:
        col.close()


def test_selection_token_covers_every_match_not_just_the_page(tmp_path) -> None:
    """A token over one page would stop protecting the rest of a big cleanup.

    search_notes paginates; delete_notes does not. If the token described only
    the rendered page, a large deletion would be verified against a fraction of
    itself and the remainder could change freely.
    """
    import anki.collection
    from anki_mcp.errors import ToolError
    from anki_mcp.tools._common import selection_token, verify_selection
    from anki_mcp.tools.write import create_deck_op

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        create_deck_op(col, "Big")
        deck_id = col.decks.by_name("Big")["id"]
        basic = col.models.by_name("Basic")
        for n in range(60):
            note = col.new_note(basic)
            note["Front"], note["Back"] = f"wort {n}", str(n)
            note.tags = ["bulk"]
            col.add_note(note, deck_id)

        everything = list(col.find_notes("tag:bulk"))
        assert len(everything) == 60
        page = everything[:50]          # what search_notes would render

        # The token search_notes actually mints covers all 60...
        verify_selection(selection_token(col, everything), col, everything)

        # ...whereas a page-only token does not, which is what makes covering
        # every match load-bearing rather than cosmetic.
        with pytest.raises(ToolError):
            verify_selection(selection_token(col, page), col, everything)

        # Order is not identity: find_notes promises none, and the same notes
        # returned shuffled must still verify.
        verify_selection(selection_token(col, everything), col,
                         list(reversed(everything)))

        # An edit to any matched note invalidates the selection -- the caller
        # is no longer about to delete what they reviewed.
        token = selection_token(col, everything)
        edited = col.get_note(everything[0])
        edited["Front"] = "etwas anderes"
        col.update_note(edited)
        with pytest.raises(ToolError) as caught:
            verify_selection(token, col, everything)
        assert "added, removed or edited" in str(caught.value)
    finally:
        col.close()


def test_a_refused_selection_does_no_work(tmp_path) -> None:
    """Raised in review on #40: gate before the work, not after.

    The `deleted` list reads every matched note and strips its HTML. Building
    it before verify_selection meant a refused call paid for a list it would
    never return -- and the comment above it claimed it ran "after every gate
    has passed" while a gate ran later, which is the kind of comment that
    teaches the next reader something false.
    """
    import anki.collection
    from anki_mcp.errors import ToolError
    from anki_mcp.tools.write import create_deck_op, delete_notes_op

    col = anki.collection.Collection(str(tmp_path / "c.anki2"))
    try:
        create_deck_op(col, "Bulk")
        deck_id = col.decks.by_name("Bulk")["id"]
        basic = col.models.by_name("Basic")
        for n in range(3):
            note = col.new_note(basic)
            note["Front"], note["Back"] = f"wort {n}", str(n)
            note.tags = ["bulk"]
            col.add_note(note, deck_id)

        # get_note is only called to build the `deleted` list. If a refused
        # call reaches it, the gate is in the wrong place.
        real_get_note = col.get_note
        calls: list[int] = []

        def spy(note_id):
            calls.append(note_id)
            return real_get_note(note_id)

        col.get_note = spy  # type: ignore[method-assign]

        with pytest.raises(ToolError):
            delete_notes_op(
                col, "tag:bulk", expect_count=3, selection=_STALE_TOKEN
            )
        assert calls == [], "refused call read notes it was never going to delete"

        # The accepted path still builds the list.
        delete_notes_op(
            col, "tag:bulk", expect_count=3, selection=_sel(col, "tag:bulk")
        )
        assert len(calls) == 3
    finally:
        col.close()


# --- Word-final stops: word audio sliced from a carrier phrase --------------

# Real alignment ElevenLabs returned for „der Berg“, sagte sie. -- a fixture so
# the slicing is tested against the shape the API actually produces.
BERG_CARRIER_ALIGNMENT = {
    "characters": list("„der Berg“, sagte sie."),
    "character_start_times_seconds": [
        0.0, 0.151, 0.232, 0.279, 0.313, 0.372, 0.406, 0.534, 0.58, 0.708, 0.766,
        0.789, 0.848, 0.929, 1.033, 1.091, 1.149, 1.196, 1.231, 1.277, 1.324, 1.451],
    "character_end_times_seconds": [
        0.151, 0.232, 0.279, 0.313, 0.372, 0.406, 0.534, 0.58, 0.708, 0.766, 0.789,
        0.848, 0.929, 1.033, 1.091, 1.149, 1.196, 1.231, 1.277, 1.324, 1.451, 1.858],
}


def _alignment_for(word: str, starts: list[float], ends: list[float]) -> dict:
    from anki_mcp.pronunciation import carrier_text

    return {"characters": list(carrier_text(word)),
            "character_start_times_seconds": starts,
            "character_end_times_seconds": ends}


def test_word_slice_comes_from_the_alignment() -> None:
    """The cut is placed by the aligner, and runs past the last letter.

    "g" ends at 0.708 s, but its release burst lands after that -- measured at
    0.66-0.73 s in this very clip. Ending the slice at the letter would cut the
    [k] this deck exists to teach, so the slice runs 120 ms beyond it.
    """
    from anki_mcp.pronunciation import word_slice_seconds

    start, end, final_unit = word_slice_seconds(BERG_CARRIER_ALIGNMENT, "der Berg")
    assert start == pytest.approx(0.151 - 0.05)
    assert end == pytest.approx(0.708 + 0.12)
    assert final_unit == pytest.approx(0.58)


def test_word_slice_never_reaches_into_the_carrier() -> None:
    """A fast read must not leak the start of "sagte" into the word clip."""
    from anki_mcp.pronunciation import carrier_text, word_slice_seconds

    word = "gelb"
    count = len(carrier_text(word))
    # g e l b end at 0.40; the carrier's "s" starts at 0.45, inside the 120 ms.
    starts = [0.0, 0.10, 0.20, 0.30, 0.35, 0.40, 0.42, 0.44, 0.45] + [0.5] * (count - 9)
    ends = [0.10, 0.20, 0.30, 0.35, 0.40, 0.42, 0.44, 0.45, 0.5] + [0.6] * (count - 9)
    assert word_slice_seconds(_alignment_for(word, starts, ends), word)[1] == pytest.approx(0.45)


def test_word_slice_ignores_punctuation_inside_the_word() -> None:
    """ "Servus!" ends on the s, not on the unspoken "!"."""
    from anki_mcp.pronunciation import carrier_text, word_slice_seconds

    word = "Servus!"
    count = len(carrier_text(word))
    starts = [round(0.1 * index, 3) for index in range(count)]
    ends = [round(0.1 * index + 0.1, 3) for index in range(count)]
    # "s" is index 6 (after „ S e r v u), ending at 0.7; "!" would end at 0.8.
    start, end, final_unit = word_slice_seconds(_alignment_for(word, starts, ends), word)
    assert start == pytest.approx(0.1 - 0.05)
    assert end == pytest.approx(0.7 + 0.12)
    assert final_unit == pytest.approx(0.6)


def test_word_slice_refuses_an_alignment_for_different_text() -> None:
    """Indices into the wrong text would cut an arbitrary piece of audio."""
    from anki_mcp.pronunciation import PronunciationError, word_slice_seconds

    with pytest.raises(PronunciationError):
        word_slice_seconds(BERG_CARRIER_ALIGNMENT, "der Tag")


def test_slice_keeps_the_tail_and_pads_it() -> None:
    """Nothing after the cut point is trimmed, and 300 ms of silence follows.

    The fake PCM is silence with a burst just before the end of the slice --
    where a final [k] lives. Anything that trims trailing low-energy audio, or
    ends the clip at the burst, would fail here.
    """
    from array import array

    from anki_mcp import audio

    rate = audio.SAMPLE_RATE
    burst = array("h", [8000, -8000] * (rate // 100))            # 20 ms
    source = (array("h", [3000] * rate)                          # 1 s "vowel"
              + array("h", bytes(2 * rate // 10))                # 100 ms closure
              + burst
              + array("h", bytes(2 * rate // 20))                # 50 ms tail
              + array("h", [5000] * rate))                       # the carrier
    end_seconds = 1.0 + 0.1 + 0.02 + 0.05
    out = array("h", audio.finish(audio.cut(source.tobytes(), 0.5, end_seconds)))

    padding = round(audio.TAIL_PADDING_SECONDS * rate)
    assert len(out) == round((end_seconds - 0.5) * rate) + padding
    assert not any(out[-padding:]), "tail padding is not silent"
    # The burst survives untouched: it is well clear of the fade.
    burst_at = round(0.6 * rate)
    assert out[burst_at:burst_at + len(burst)] == burst
    # Nothing of the carrier leaked in.
    assert 5000 not in out
    # Edges are faded, so the cut cannot click like a stop.
    fade = round(audio.FADE_SECONDS * rate)
    assert abs(out[0]) < 3000 and abs(out[len(out) - padding - 1]) < fade


# Where the synthetic word's final consonant (closure + burst) begins.
SYNTHETIC_FINAL_CONSONANT_SECONDS = 0.1 + 0.01 + 0.5


def _synthetic_word(burst_amplitude: int):
    """A vowel, a closure, then a burst: the shape of "Berg" -> [..ʁk]."""
    import math
    from array import array

    from anki_mcp import audio

    rate = audio.SAMPLE_RATE
    vowel = array("h", (round(12000 * math.sin(2 * math.pi * 150 * index / rate))
                        for index in range(rate // 2)))                 # 500 ms, low
    # Levels as measured on real takes: a burst sits well below the vowel in
    # broadband terms but dominates once voicing is filtered out.
    onset = array("h", [1500, -1500] * (rate // 200))                   # 10 ms [b]-ish
    closure = array("h", bytes(2 * rate * 3 // 100))                    # 30 ms
    burst = array("h", [burst_amplitude, -burst_amplitude] * (rate // 50))  # 40 ms
    silence = array("h", bytes(2 * rate // 10))
    return silence + onset + vowel + closure + burst + silence


def test_release_measure_ranks_a_strong_burst_above_a_weak_one() -> None:
    """The measurement the selection trusts must order takes the right way."""
    from anki_mcp import audio

    final_consonant = SYNTHETIC_FINAL_CONSONANT_SECONDS
    strong = audio.final_release(_synthetic_word(2500), final_consonant)
    weak = audio.final_release(_synthetic_word(200), final_consonant)
    assert strong is not None and weak is not None
    assert strong.release_db > -3                # a burst as strong as the vowel
    assert weak.release_db < -15
    # The closure found is the one AFTER the vowel, not the silence before the
    # word -- mistaking the lead-in for the closure once scored whole words.
    assert strong.closure_ms == 30


def test_a_loud_burst_is_not_mistaken_for_the_vowel() -> None:
    """The best takes must not score as "no release".

    Guessing the vowel's end by loudness counts a burst within 12 dB of the
    vowel as vowel -- and a native [k] is 14 dB down, so the guess sits right
    at the edge of the takes the selection most wants. With the aligner's
    final-letter time the burst is found however loud it is.
    """
    from anki_mcp import audio

    loud = _synthetic_word(4000)                  # ~8 dB below the vowel
    assert audio.final_release(loud) is None      # the guess fails ...
    found = audio.final_release(loud, SYNTHETIC_FINAL_CONSONANT_SECONDS)
    assert found is not None and found.release_db > -3    # ... alignment does not


def test_release_measure_finds_nothing_in_a_word_with_no_final_obstruent() -> None:
    from anki_mcp import audio
    from array import array
    import math

    rate = audio.SAMPLE_RATE
    vowel_only = array("h", bytes(2 * rate // 10)) + array(
        "h", (round(12000 * math.sin(2 * math.pi * 150 * index / rate)) for index in range(rate // 2)))
    assert audio.final_release(vowel_only) is None


def _fake_take(score: float | None):
    from array import array

    from anki_mcp import audio
    from anki_mcp.pronunciation import Take

    release = None if score is None else audio.Release(40.0, 20, score, 30)
    return Take(array("h", [int(score or 0)]), release)


def test_best_take_stops_at_the_first_round_with_a_good_take() -> None:
    import itertools

    from anki_mcp.pronunciation import TAKES_PER_ROUND, best_take

    scores = itertools.chain([-24.0, -9.0, -21.0], itertools.repeat(-30.0))
    chosen, made = best_take(lambda: _fake_take(next(scores)))
    assert made == TAKES_PER_ROUND
    assert chosen.score == -9.0


def test_best_take_keeps_going_until_accepted_or_the_cap() -> None:
    import itertools

    from anki_mcp.pronunciation import MAX_TAKES, TAKES_PER_ROUND, best_take

    # Accepted in round two.
    first_round = [-24.0, -19.0] + [-30.0] * (TAKES_PER_ROUND - 2)
    scores = itertools.chain(first_round, [-22.0, -10.5], itertools.repeat(-30.0))
    chosen, made = best_take(lambda: _fake_take(next(scores)))
    assert (made, chosen.score) == (2 * TAKES_PER_ROUND, -10.5)

    # Never accepted: stops at the cap and keeps the best it saw.
    scores = itertools.chain([-24.0, -18.5], itertools.repeat(-30.0))
    chosen, made = best_take(lambda: _fake_take(next(scores)))
    assert (made, chosen.score) == (MAX_TAKES, -18.5)


def test_best_take_survives_failed_takes_but_not_total_failure() -> None:
    import itertools

    from anki_mcp.pronunciation import PronunciationError, TAKES_PER_ROUND, best_take

    outcomes = itertools.chain(["fail", -4.0, "fail"], itertools.repeat(-30.0))

    def make():
        outcome = next(outcomes)
        if outcome == "fail":
            raise PronunciationError("rejected")
        return _fake_take(outcome)

    chosen, made = best_take(make)
    assert chosen.score == -4.0 and made == TAKES_PER_ROUND

    def always_fails():
        raise PronunciationError("quota")

    with pytest.raises(PronunciationError, match="quota"):
        best_take(always_fails)


def test_only_obstruent_final_words_are_selected_on() -> None:
    from anki_mcp.pronunciation import ends_in_obstruent

    assert all(map(ends_in_obstruent, ["der Berg", "das Rad", "gelb", "das Los",
                                       "das Obst", "der Standard", "das Buch", "Servus!"]))
    assert not any(map(ends_in_obstruent, ["die Blume", "der Ball", "neun", "Hallo!"]))


def test_final_ig_words_get_a_single_take() -> None:
    """-ig is spirantized to [ɪç], not devoiced to [k]: ranking by release
    strength could pick a [k] take. "ei" + g is ordinary devoicing and keeps
    selection."""
    from anki_mcp.pronunciation import ends_in_obstruent

    assert not any(map(ends_in_obstruent, ["der König", "wenig", "zwanzig", "Leipzig",
                                           "der Honig", "Ludwig", "WICHTIG", "fertig!"]))
    assert all(map(ends_in_obstruent, ["der Zweig", "der Teig", "der Tag", "der Berg"]))


def test_synthesize_word_requests_the_carrier_and_returns_mp3() -> None:
    import base64
    import io
    import json

    import anki_mcp.pronunciation as pr
    from anki_mcp import audio

    requests = []
    # Place a word with a strong final burst inside the carrier's word span
    # (0.101-0.828 s in the fixture alignment), so round one is accepted.
    lead = bytes(2 * round(0.12 * audio.SAMPLE_RATE))
    fake_pcm = lead + _synthetic_word(2500).tobytes() + bytes(2 * audio.SAMPLE_RATE)

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        requests.append((req.full_url, json.loads(req.data)))
        return _Response(json.dumps({
            "audio_base64": base64.b64encode(fake_pcm).decode(),
            "alignment": BERG_CARRIER_ALIGNMENT,
        }).encode())

    original = pr.urllib.request.urlopen
    pr.urllib.request.urlopen = fake_urlopen
    try:
        mp3, ext, note = pr.synthesize_word("der Berg", "k", "voice", "m", previous_text="Der")
    finally:
        pr.urllib.request.urlopen = original

    assert ext == ".mp3"
    assert mp3[:2] == b"\xff\xf3" or mp3[:3] == b"ID3"          # an MPEG audio frame
    assert len(requests) == pr.TAKES_PER_ROUND
    assert note.startswith(f"best of {pr.TAKES_PER_ROUND} takes")
    url, body = requests[0]
    assert "/with-timestamps" in url and "output_format=pcm_24000" in url
    assert body["text"] == "\u201eder Berg\u201c, sagte sie."
    # Leading context weakens a final release; it is withheld for such words.
    assert body["previous_text"] is None


def test_a_word_without_a_final_obstruent_keeps_one_take_and_its_context() -> None:
    """ "neun" needs its sentence to be read as neun and not nein; it has no
    final burst to select on, so it costs one request, as before."""
    import base64
    import io
    import json

    import anki_mcp.pronunciation as pr

    requests = []
    alignment = _alignment_for("neun", [0.1 * index for index in range(len(pr.carrier_text("neun")))],
                               [0.1 * index + 0.1 for index in range(len(pr.carrier_text("neun")))])

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        requests.append(json.loads(req.data))
        return _Response(json.dumps({
            "audio_base64": base64.b64encode(bytes(2 * 24000 * 3)).decode(),
            "alignment": alignment,
        }).encode())

    original = pr.urllib.request.urlopen
    pr.urllib.request.urlopen = fake_urlopen
    try:
        _, ext, note = pr.synthesize_word("neun", "k", "v", "m", previous_text="Meine Tochter ist")
    finally:
        pr.urllib.request.urlopen = original
    assert (len(requests), ext, note) == (1, ".mp3", "single take")
    assert requests[0]["previous_text"] == "Meine Tochter ist"


def test_only_the_headword_is_sliced_from_a_carrier() -> None:
    """Example sentences keep their existing synthesis; only Word changes."""
    import anki_mcp.pronunciation as pr

    calls = []
    originals = (pr.lookup_wiktionary, pr.compound_ipa,
                 pr.synthesize_word, pr.synthesize_elevenlabs)
    pr.lookup_wiktionary = lambda word: (None, None, None)
    pr.compound_ipa = lambda word: (None, None)
    pr.synthesize_word = lambda *a, **k: (calls.append("word") or (b"w", ".mp3", "best of 3 takes"))
    pr.synthesize_elevenlabs = lambda *a, **k: (calls.append("sentence") or (b"s", ".mp3"))
    try:
        said = pr.fetch("der Berg", tts_key="k", tts_voice="v",
                        tts_provider="elevenlabs", prefer_tts=True, headword=True)
        pr.fetch("Der Berg ist hoch.", tts_key="k", tts_voice="v",
                 tts_provider="elevenlabs", prefer_tts=True)
    finally:
        (pr.lookup_wiktionary, pr.compound_ipa,
         pr.synthesize_word, pr.synthesize_elevenlabs) = originals
    assert calls == ["word", "sentence"]
    assert "best of 3 takes" in said.source


# --- Copilot review of anki-mcp PR #63 ---------------------------------------

class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.content if limit < 0 else self.content[:limit]


def test_an_oversized_response_is_refused_not_truncated() -> None:
    """A body cut at the read limit would reach json.loads as broken JSON."""
    import anki_mcp.pronunciation as pr

    original = pr.urllib.request.urlopen
    pr.urllib.request.urlopen = lambda req, timeout=None: _FakeResponse(
        b"{" + b"x" * (pr.MAX_AUDIO_BYTES * 2 + 10))
    try:
        with pytest.raises(pr.PronunciationError, match="exceeded"):
            pr._elevenlabs_post("https://example.invalid", b"{}", "k", "application/json")
    finally:
        pr.urllib.request.urlopen = original


def test_a_malformed_take_is_dropped_and_the_rest_are_used() -> None:
    """One junk /with-timestamps body must not sink the other takes."""
    import base64
    import itertools
    import json
    import threading

    import anki_mcp.pronunciation as pr
    from anki_mcp import audio

    lead = bytes(2 * round(0.12 * audio.SAMPLE_RATE))
    good = json.dumps({
        "audio_base64": base64.b64encode(
            lead + _synthetic_word(2500).tobytes() + bytes(2 * audio.SAMPLE_RATE)).decode(),
        "alignment": BERG_CARRIER_ALIGNMENT,
    }).encode()
    junk = [b"not json", b'{"alignment": {}}', b'{"alignment": null, "audio_base64": "x"}']
    # Every take but one in the first round is junk, whatever the round size.
    bodies = itertools.chain(junk[:pr.TAKES_PER_ROUND - 1], itertools.repeat(good))
    lock = threading.Lock()

    def fake_urlopen(req, timeout=None):
        with lock:
            return _FakeResponse(next(bodies))

    original = pr.urllib.request.urlopen
    pr.urllib.request.urlopen = fake_urlopen
    try:
        mp3, ext, note = pr.synthesize_word("der Berg", "k", "v", "m")
    finally:
        pr.urllib.request.urlopen = original
    assert ext == ".mp3" and note.startswith(f"best of {pr.TAKES_PER_ROUND} takes")


def test_a_word_whose_every_take_is_malformed_reports_a_pronunciation_error() -> None:
    import anki_mcp.pronunciation as pr

    original = pr.urllib.request.urlopen
    pr.urllib.request.urlopen = lambda req, timeout=None: _FakeResponse(b"not json")
    try:
        with pytest.raises(pr.PronunciationError, match="unusable"):
            pr.synthesize_word("der Berg", "k", "v", "m")
    finally:
        pr.urllib.request.urlopen = original


def test_the_release_is_measured_from_the_start_of_a_multi_letter_final_sound() -> None:
    """The [x] of "Buch" starts at the c, the [ʃ] of "Fisch" at the s."""
    from anki_mcp.pronunciation import carrier_text, word_slice_seconds

    def final_start(word: str) -> float:
        count = len(carrier_text(word))
        alignment = _alignment_for(word, [round(0.1 * index, 3) for index in range(count)],
                                   [round(0.1 * index + 0.1, 3) for index in range(count)])
        return word_slice_seconds(alignment, word)[2]

    # Carrier index 0 is the opening quote, so a word's letter i is index i + 1.
    assert final_start("das Buch") == pytest.approx(0.1 * (1 + len("das Bu")))    # the c
    assert final_start("der Fisch") == pytest.approx(0.1 * (1 + len("der Fi")))   # the s
    assert final_start("der Tag") == pytest.approx(0.1 * (1 + len("der Ta")))     # the g
    assert final_start("Ach!") == pytest.approx(0.1 * (1 + len("A")))             # "!" unspoken


def test_the_report_claims_sentence_context_only_when_it_was_sent() -> None:
    import anki_mcp.pronunciation as pr

    originals = (pr.lookup_wiktionary, pr.compound_ipa, pr.synthesize_word)
    pr.lookup_wiktionary = lambda word: (None, None, None)
    pr.compound_ipa = lambda word: (None, None)
    pr.synthesize_word = lambda *a, **k: (b"w", ".mp3", "note")
    try:
        def source(word: str) -> str:
            return pr.fetch(word, tts_key="k", tts_voice="v", tts_provider="elevenlabs",
                            prefer_tts=True, headword=True,
                            context=("Meine Tochter ist", "")).source
        # Obstruent-final: synthesize_word withholds the context.
        assert "sentence context" not in source("der Berg")
        # Anything else still gets it, and says so.
        assert source("neun").endswith("with sentence context")
    finally:
        pr.lookup_wiktionary, pr.compound_ipa, pr.synthesize_word = originals


# --- Copilot review of anki-mcp PR #64 ---------------------------------------

def test_well_formed_json_with_unusable_audio_or_timing_is_one_dropped_take() -> None:
    """Odd-length PCM and a short timing list fail inside the take, not the word."""
    import base64
    import json

    import anki_mcp.pronunciation as pr

    short_timing = dict(BERG_CARRIER_ALIGNMENT,
                        character_end_times_seconds=BERG_CARRIER_ALIGNMENT[
                            "character_end_times_seconds"][:5])
    bodies = {
        "odd-length PCM": {"audio_base64": base64.b64encode(b"\x00" * 24001).decode(),
                           "alignment": BERG_CARRIER_ALIGNMENT},
        "short timing list": {"audio_base64": base64.b64encode(bytes(48000)).decode(),
                              "alignment": short_timing},
    }
    original = pr.urllib.request.urlopen
    try:
        for label, body in bodies.items():
            pr.urllib.request.urlopen = (
                lambda req, timeout=None, content=json.dumps(body).encode(): _FakeResponse(content))
            with pytest.raises(pr.PronunciationError, match="unusable|incomplete"):
                pr.synthesize_word("der Berg", "k", "v", "m")
    finally:
        pr.urllib.request.urlopen = original


# --- Copilot review of anki-mcp PR #65 ---------------------------------------

def test_non_finite_alignment_timings_are_refused_where_they_enter() -> None:
    """JSON's 1e309 parses to inf; round() would raise OverflowError later."""
    import math

    from anki_mcp.pronunciation import PronunciationError, word_slice_seconds

    for poison in (math.inf, math.nan):
        ends = list(BERG_CARRIER_ALIGNMENT["character_end_times_seconds"])
        ends[8] = poison                                    # the "g" of Berg
        with pytest.raises(PronunciationError, match="not finite"):
            word_slice_seconds(dict(BERG_CARRIER_ALIGNMENT, character_end_times_seconds=ends),
                               "der Berg")


def test_an_infinite_timestamp_in_the_response_is_one_dropped_take() -> None:
    import base64

    import anki_mcp.pronunciation as pr

    starts = BERG_CARRIER_ALIGNMENT["character_start_times_seconds"]
    ends = BERG_CARRIER_ALIGNMENT["character_end_times_seconds"]
    body = ('{"audio_base64": "%s", "alignment": {"characters": %s, '
            '"character_start_times_seconds": %s, "character_end_times_seconds": [%s]}}' % (
                base64.b64encode(bytes(48000)).decode(),
                __import__("json").dumps(BERG_CARRIER_ALIGNMENT["characters"]),
                starts, ", ".join(["1e309"] + [str(end) for end in ends[1:]]))).encode()
    original = pr.urllib.request.urlopen
    pr.urllib.request.urlopen = lambda req, timeout=None: _FakeResponse(body)
    try:
        with pytest.raises(pr.PronunciationError, match="not finite"):
            pr.synthesize_word("der Berg", "k", "v", "m")
    finally:
        pr.urllib.request.urlopen = original


def test_rerecording_audio_keeps_hand_written_ipa() -> None:
    """update_note once replaced "[deːɐ̯ ˈʃtandaʁt]" with Wiktionary's
    "ˈstandaʁt" just because the audio was re-recorded."""
    from anki_mcp.tools.write import ipa_after_rerecording

    curated = "[deːɐ̯ ˈʃtandaʁt]"
    # Same word, audio re-recorded: the curated transcription stays.
    assert ipa_after_rerecording(curated, "ˈstandaʁt", word_changed=False,
                                 caller_set_ipa=False) == curated
    # An empty field is filled.
    assert ipa_after_rerecording("", "bɛʁk", word_changed=False, caller_set_ipa=False) == "bɛʁk"
    # The word itself changed: the old IPA describes a different word.
    assert ipa_after_rerecording("bɛʁk", "taːk", word_changed=True, caller_set_ipa=False) == "taːk"
    # IPA passed in the same call always wins, even with a new word.
    assert ipa_after_rerecording("[taːk]", "taːk", word_changed=True, caller_set_ipa=True) == "[taːk]"
    # Nothing fetched: nothing changes.
    assert ipa_after_rerecording(curated, None, word_changed=True, caller_set_ipa=False) == curated


def test_update_note_rerecording_keeps_curated_ipa_through_the_real_tool(tmp_path) -> None:
    """The wiring, not just the rule: regenerate_audio=True on an unchanged
    Word must leave "[deːɐ̯ ˈʃtandaʁt]" alone; changing the Word refreshes it."""
    import asyncio

    from anki_mcp.pronunciation import Pronunciation
    from anki_mcp.tools import write

    mcp, worker = _test_server(tmp_path)

    def seed(col):
        models = col.models
        notetype = models.new("IPA test")
        for name in ("Word", "IPA", "Audio"):
            models.add_field(notetype, models.new_field(name))
        template = models.new_template("Card 1")
        template["qfmt"], template["afmt"] = "{{Word}}", "{{IPA}} {{Audio}}"
        models.add_template(notetype, template)
        models.add(notetype)
        note = col.new_note(models.by_name("IPA test"))
        note["Word"], note["IPA"] = "der Standard", "[deːɐ̯ ˈʃtandaʁt]"
        col.add_note(note, col.decks.id("Default"))
        return note.id

    fetched = {"der Standard": "ˈstandaʁt", "der Berg": "bɛʁk"}
    original = write.fetch_pronunciation
    write.fetch_pronunciation = lambda word, **kwargs: Pronunciation(
        ipa=fetched[word], audio=b"ID3fake", audio_ext=".mp3", source="stub")

    async def scenario():
        note_id = await worker.run(seed)
        ipa = lambda: worker.run(lambda col: col.get_note(note_id)["IPA"])

        await mcp.call_tool("update_note", {"note_id": note_id, "regenerate_audio": True})
        assert await ipa() == "[deːɐ̯ ˈʃtandaʁt]"

        await mcp.call_tool("update_note", {"note_id": note_id, "fields": {"Word": "der Berg"}})
        assert await ipa() == "bɛʁk"

    try:
        asyncio.run(scenario())
    finally:
        write.fetch_pronunciation = original
        worker.close()


# --- ElevenLabs concurrency limit ---------------------------------------------

def test_elevenlabs_requests_never_exceed_the_plans_concurrency_limit() -> None:
    """The plan allows 3 in flight; best-of-N and overlapping tool calls must
    stay inside it however many threads ask at once."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import anki_mcp.pronunciation as pr

    in_flight = []
    peak = []
    lock = threading.Lock()

    def fake_urlopen(req, timeout=None):
        with lock:
            in_flight.append(1)
            peak.append(len(in_flight))
        time.sleep(0.05)
        with lock:
            in_flight.pop()
        return _FakeResponse(b"{}")

    original = pr.urllib.request.urlopen
    pr.urllib.request.urlopen = fake_urlopen
    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(lambda _: pr._elevenlabs_post("https://x.invalid", b"{}", "k", "a"),
                          range(10)))
    finally:
        pr.urllib.request.urlopen = original
    assert max(peak) == pr.ELEVENLABS_MAX_CONCURRENT


def _http_429(detail: bytes):
    import io
    import urllib.error

    return urllib.error.HTTPError("https://x.invalid", 429, "Too Many Requests", {}, io.BytesIO(detail))


def test_a_concurrency_429_is_retried_not_reported_as_quota() -> None:
    import anki_mcp.pronunciation as pr

    calls = []
    concurrency = b'{"detail":{"code":"concurrent_limit_exceeded"}}'

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise _http_429(concurrency)
        return _FakeResponse(b"ok")

    original_urlopen, original_sleep = pr.urllib.request.urlopen, pr.time.sleep
    pr.urllib.request.urlopen, pr.time.sleep = fake_urlopen, lambda seconds: None
    try:
        assert pr._elevenlabs_post("https://x.invalid", b"{}", "k", "a") == b"ok"
        assert len(calls) == 3

        # Still refused after every retry: say what actually happened.
        pr.urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(
            _http_429(concurrency))
        with pytest.raises(pr.PronunciationError, match="in flight"):
            pr._elevenlabs_post("https://x.invalid", b"{}", "k", "a")

        # A real quota 429 is not retried and keeps its message.
        pr.urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(
            _http_429(b'{"detail":{"code":"quota_exceeded"}}'))
        with pytest.raises(pr.PronunciationError, match="quota exhausted"):
            pr._elevenlabs_post("https://x.invalid", b"{}", "k", "a")
    finally:
        pr.urllib.request.urlopen, pr.time.sleep = original_urlopen, original_sleep


# --- -ig: the final sound must be the [ç] hiss, not a [k] burst ---------------

# Where the synthetic -ig word's final consonant begins (after lead-in + vowel).
SYNTHETIC_IG_FINAL_SECONDS = 0.1 + 0.4


def _synthetic_ig_word(final: str):
    """A vowel followed by either a sustained fricative [ç] or a stop [k]."""
    import math
    from array import array

    from anki_mcp import audio

    rate = audio.SAMPLE_RATE
    lead = array("h", bytes(2 * rate // 10))
    vowel = array("h", (round(12000 * math.sin(2 * math.pi * 150 * index / rate))
                        for index in range(rate * 4 // 10)))
    seed = [12345]

    def noise(count: int, amplitude: int) -> array:
        # Deterministic pseudo-random hiss (LCG), so the test never flakes.
        def next_value(_: int) -> int:
            seed[0] = (seed[0] * 1103515245 + 12345) % 2 ** 31
            return round(amplitude * (seed[0] / 2 ** 30 - 1))
        return array("h", map(next_value, range(count)))

    tail = (noise(rate * 15 // 100, 2500) if final == "ç"                    # 150 ms hiss
            else array("h", bytes(2 * rate * 4 // 100))                      # 40 ms closure
            + array("h", [2500, -2500] * (rate // 100)))                     # 20 ms burst
    return lead + vowel + tail + array("h", bytes(2 * rate // 10))


def test_final_hiss_separates_a_fricative_from_a_stop() -> None:
    from anki_mcp import audio
    from anki_mcp.pronunciation import ACCEPT_HISS_MS

    fricative = audio.final_hiss_ms(_synthetic_ig_word("ç"), SYNTHETIC_IG_FINAL_SECONDS)
    stop = audio.final_hiss_ms(_synthetic_ig_word("k"), SYNTHETIC_IG_FINAL_SECONDS)
    assert fricative >= ACCEPT_HISS_MS > stop
    assert stop <= 30


def test_an_ig_word_keeps_the_first_take_that_ends_in_the_hiss() -> None:
    import base64
    import itertools
    import json
    import threading

    import anki_mcp.pronunciation as pr
    from anki_mcp import audio

    word = "wenig"
    count = len(pr.carrier_text(word))
    # „ w e n i g “ ...: the g (index 5) starts where the synthetic tail starts.
    starts = [0.0, 0.1, 0.2, 0.3, 0.4, SYNTHETIC_IG_FINAL_SECONDS] + [
        0.75 + 0.05 * index for index in range(count - 6)]
    ends = starts[1:] + [starts[-1] + 0.05]
    ends[5] = 0.65
    alignment = _alignment_for(word, starts, ends)

    def body(final: str) -> bytes:
        pcm = _synthetic_ig_word(final).tobytes() + bytes(2 * audio.SAMPLE_RATE)
        return json.dumps({"audio_base64": base64.b64encode(pcm).decode(),
                           "alignment": alignment}).encode()

    requests = []
    lock = threading.Lock()

    def serve(bodies):
        def fake_urlopen(req, timeout=None):
            with lock:
                requests.append(json.loads(req.data))
                return _FakeResponse(next(bodies))
        return fake_urlopen

    original = pr.urllib.request.urlopen
    try:
        # One [ç] take among [k] takes in the first round: accepted there.
        pr.urllib.request.urlopen = serve(itertools.chain(
            [body("k"), body("ç")], itertools.repeat(body("k"))))
        _, _, note = pr.synthesize_word(word, "k", "v", "m", previous_text="Das ist")
        assert note.startswith(f"{pr.TAKES_PER_ROUND} takes") and "([ç])" in note
        # -ig words keep their sentence context (they are not release-selected).
        assert requests[0]["previous_text"] == "Das ist"

        # Only [k] takes: stops at the cap and says no take reached [ç] length.
        requests.clear()
        pr.urllib.request.urlopen = serve(itertools.repeat(body("k")))
        _, _, note = pr.synthesize_word(word, "k", "v", "m")
        assert len(requests) == pr.MAX_IG_TAKES
        assert "no take reached [ç] length" in note
    finally:
        pr.urllib.request.urlopen = original


def test_separated_bursts_do_not_add_up_to_a_hiss() -> None:
    """Copilot on #69: summing windows let a stop with several short puffs
    reach [ç] length without ever sustaining a hiss."""
    from array import array

    from anki_mcp import audio
    from anki_mcp.pronunciation import ACCEPT_HISS_MS

    rate = audio.SAMPLE_RATE
    word = _synthetic_ig_word("k")[: round(SYNTHETIC_IG_FINAL_SECONDS * rate)]
    puff = array("h", [2500, -2500] * (rate // 100))            # 20 ms
    gap = array("h", bytes(2 * rate * 2 // 100))                # 20 ms
    # Six 20 ms puffs: 120 ms in total, never more than 20 ms unbroken.
    puffs = sum((puff + gap for _ in range(6)), array("h"))
    measured = audio.final_hiss_ms(word + puffs + array("h", bytes(2 * rate // 10)),
                                   SYNTHETIC_IG_FINAL_SECONDS)
    assert measured == 20 < ACCEPT_HISS_MS
