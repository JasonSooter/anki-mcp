"""Pronunciation: IPA and spoken audio for a card.

Fluent Forever puts pronunciation first -- it is the first of the three keys in
the book, ahead of vocabulary -- so a card without it is missing the part the
method considers foundational.

Two sources, in this order:

* German Wiktionary, which carries an IPA transcription and, for most single
  words, a link to a recording by an actual German speaker hosted on Wikimedia
  Commons. Real speakers are what the method wants, so these win.
* ElevenLabs text-to-speech, for everything Wiktionary has no entry for. Measured against
  a real collection that is essentially all phrases: "Vielen Dank fuer Ihre
  Hilfe", "Wir sehen uns!", "Herzlich willkommen!" have no dictionary entry and
  never will, and phrases are where connected speech and prosody matter most.

Wiktionary gives no IPA for phrases either. Nothing here invents one -- a
hand-assembled transcription would be a guess presented as fact.

There is one derivation, and it is not an invention: German builds compounds
freely, so a word can be entirely ordinary and still have no dictionary entry
(`das Budgetgeld`). When the whole word misses, `compound_ipa` looks up its
PARTS and joins their real transcriptions, moving the later element's stress
mark to secondary -- which is the actual rule for German compound stress. Every
symbol still comes from Wiktionary; only the seam is ours, and the source
string says so.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import lameenc

from . import audio

log = logging.getLogger(__name__)

WIKTIONARY = "https://de.wiktionary.org/w/api.php"
COMMONS = "https://commons.wikimedia.org/w/api.php"
ELEVENLABS = "https://api.elevenlabs.io/v1"
GOOGLE_TTS = "https://texttospeech.googleapis.com/v1/text:synthesize"

USER_AGENT = "anki-mcp/0.1 (personal flashcard tool)"
HTTP_TIMEOUT = 25
MAX_AUDIO_BYTES = 5 * 1024 * 1024

# Wikimedia rate-limits an unauthenticated caller quickly -- a bulk run tripped
# 429 after a handful of requests. Its own limiter, separate from the image
# providers': these are different services with different budgets, and a shared
# one would either be too slow or too loose for both.
MIN_INTERVAL_SECONDS = 1.2
MAX_RETRIES = 4

_ARTICLES = {"der", "die", "das"}
_lock = threading.Lock()
_last_request_at = 0.0


class PronunciationError(RuntimeError):
    """No pronunciation could be obtained."""


@dataclass(frozen=True)
class Pronunciation:
    ipa: str | None = None
    audio: bytes | None = None
    audio_ext: str = ""
    source: str = ""
    looked_up_as: str | None = None


def _wiki_get(url: str) -> dict:
    global _last_request_at
    for attempt in range(MAX_RETRIES):
        with _lock:
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                # Callers only catch PronunciationError, so a raw HTTPError --
                # a transient 5xx, say -- would escape add_note entirely and
                # surface as a crash rather than a handled failure.
                raise PronunciationError(
                    f"Wikimedia returned {exc.code}: "
                    f"{exc.read().decode(errors='replace')[:120]}"
                ) from exc
            # Distinguishing this from "no entry" matters: swallowing it once
            # made a coverage measurement report 4/17 instead of 8/16.
            delay = float(exc.headers.get("Retry-After") or 0) or 3 * (attempt + 1)
            log.info("wikimedia rate limited; retrying in %.0fs", delay)
            time.sleep(min(delay, 20))
        except (urllib.error.URLError, TimeoutError) as exc:
            log.info("wikimedia request failed (%s); retrying", exc)
            time.sleep(2 ** attempt)
    raise PronunciationError("Wikimedia kept rate-limiting the request")


def _throttled_bytes(url: str, limit: int) -> bytes:
    """Fetch binary content under the same limiter as the API calls.

    The media download has to share the throttle: Wikimedia counts it against
    the same budget, and fetching files with a bare urlopen while carefully
    rate-limiting the metadata calls 429s on the very first bulk run.
    """
    global _last_request_at
    for attempt in range(MAX_RETRIES):
        with _lock:
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise PronunciationError(
                    f"Commons returned {exc.code} for the audio download: "
                    f"{exc.read().decode(errors='replace')[:120]}"
                ) from exc
            delay = float(exc.headers.get("Retry-After") or 0) or 3 * (attempt + 1)
            log.info("commons download rate limited; retrying in %.0fs", delay)
            time.sleep(min(delay, 20))
        except (urllib.error.URLError, TimeoutError) as exc:
            log.info("commons download failed (%s); retrying", exc)
            time.sleep(2 ** attempt)
    raise PronunciationError("Wikimedia kept rate-limiting the audio download")


def headword_candidates(word: str) -> list[str]:
    """Forms to try against Wiktionary, best first.

    Cards carry "das Heft" and "Servus!"; Wiktionary indexes "Heft" and
    "servus". Case matters -- "Servus" is a miss and "servus" is a hit -- so
    both are tried.
    """
    cleaned = re.sub(r"<[^>]+>", "", word).strip().rstrip("!?.,;:").strip()
    if " / " in cleaned:
        cleaned = cleaned.split(" / ")[0].strip()
    parts = cleaned.split()
    if parts and parts[0].lower() in _ARTICLES:
        parts = parts[1:]
    head = " ".join(parts).strip()
    out: list[str] = []
    for candidate in (head, head.capitalize(), head.lower()):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def lookup_wiktionary(word: str) -> tuple[str | None, str | None, str | None]:
    """Return (ipa, commons audio filename, the form it was found under)."""
    for candidate in headword_candidates(word):
        params = urllib.parse.urlencode({
            "action": "parse", "page": candidate, "prop": "wikitext",
            "format": "json", "formatversion": "2",
        })
        data = _wiki_get(f"{WIKTIONARY}?{params}")
        if "error" in data:
            continue                     # genuinely no such entry
        wikitext = data["parse"]["wikitext"]
        ipa = re.findall(r"\{\{Lautschrift\|([^}|]+)\}\}", wikitext)
        audio = re.findall(r"\{\{Audio\|([^}|]+)", wikitext)
        if ipa or audio:
            return (ipa[0].strip() if ipa else None,
                    audio[0].strip() if audio else None,
                    candidate)
    return None, None, None


# Compound parts shorter than this are too likely to be a coincidental
# substring that happens to have an entry ("Ge", "ld").
MIN_PART_LENGTH = 3
# Ceiling on part lookups for one word, so a long unsplittable compound cannot
# turn into a minute of throttled requests.
MAX_SPLIT_LOOKUPS = 12
# Linking elements German inserts between compound parts (Fugenlaute). The
# first part is looked up without them: "Taschen|geld" is Tasche + Geld.
_LINKERS = ("es", "en", "er", "s", "n")


def _stress_as_secondary(ipa: str) -> str:
    """Mark a part as the non-first element of a compound.

    German compounds carry primary stress on the first element and secondary
    on the rest, so a primary mark is demoted. A monosyllable is transcribed
    with no mark at all on its own page ("Geld" is ɡɛlt), but does take
    secondary stress inside a compound -- which is exactly how Wiktionary
    writes the compounds it does have: Taschengeld is ˈtaʃn̩ˌɡɛlt, not
    ˈtaʃn̩ɡɛlt. So an unmarked part gains one.
    """
    demoted = ipa.replace("ˈ", "ˌ")
    return demoted if "ˌ" in demoted else "ˌ" + demoted


def _with_primary_stress(ipa: str) -> str:
    """Ensure the first element carries the compound's primary stress.

    A monosyllable is transcribed with no stress mark on its own page -- Orts
    is ɔʁt͡s, Sprach is ʃpʁaːx -- but as the first element of a compound it
    takes the primary stress: Wiktionary's own Ortsgruppe is ˈɔʁt͡sˌɡʁʊpə.
    Without this the compound came out with a secondary mark and no primary,
    which is not a possible German word.
    """
    return ipa if "ˈ" in ipa else "ˈ" + ipa.lstrip("ˌ")


def compound_ipa(
    word: str, lookup=None
) -> tuple[str | None, str | None]:
    """IPA for a compound, assembled from its parts' own transcriptions.

    Returns (ipa, "Budget + Geld") or (None, None). `lookup` is the
    Wiktionary call, injectable so the tests do not touch the network.

    Only a split where BOTH parts have real IPA is accepted, which is what
    keeps this honest: "Budgetgeld" resolves because Budget and Geld are both
    real entries, while a nonsense split simply finds nothing and is dropped.
    """
    lookup = lookup or (lambda candidate: lookup_wiktionary(candidate)[0])
    candidates = headword_candidates(word)
    if not candidates:
        return None, None
    whole = candidates[0]
    if len(whole) < 2 * MIN_PART_LENGTH or " " in whole:
        return None, None          # phrases are not compounds

    cache: dict[str, str | None] = {}
    budget = MAX_SPLIT_LOOKUPS

    def ipa_of(part: str) -> str | None:
        nonlocal budget
        if part in cache:
            return cache[part]
        if budget <= 0:
            return None
        budget -= 1
        cache[part] = lookup(part)
        return cache[part]

    # Longest first element first: German heads are usually the short tail
    # (-geld, -zeit, -haus), so this reaches the real seam early and stops.
    for cut in range(len(whole) - MIN_PART_LENGTH, MIN_PART_LENGTH - 1, -1):
        head, tail = whole[:cut], whole[cut:]
        tail_ipa = ipa_of(tail.capitalize()) or ipa_of(tail.lower())
        if not tail_ipa:
            continue
        # The unstripped head is tried FIRST and a linking element is only
        # peeled when it has no entry, because the linker is often not one:
        # Kleidergeld is Kleider + Geld (the plural), not Kleid + Geld, and
        # stripping first would silently transcribe the wrong word.
        for stem in (head, *(head[: -len(l)] for l in _LINKERS
                             if head.endswith(l) and len(head) - len(l) >= MIN_PART_LENGTH)):
            # Lowercase too: a first element is not always a noun -- schnell in
            # Schnellzug, fahr- in Fahrrad -- and those headwords are lowercase.
            head_ipa = ipa_of(stem.capitalize()) or ipa_of(stem.lower())
            if head_ipa:
                joined = _with_primary_stress(
                    head_ipa.strip()) + _stress_as_secondary(tail_ipa.strip())
                return joined, f"{stem.capitalize()} + {tail.capitalize()}"
    return None, None


def download_commons_file(filename: str) -> tuple[bytes, str]:
    params = urllib.parse.urlencode({
        "action": "query", "titles": f"File:{filename}", "prop": "imageinfo",
        "iiprop": "url|mime", "format": "json", "formatversion": "2",
    })
    data = _wiki_get(f"{COMMONS}?{params}")
    pages = (data.get("query") or {}).get("pages") or []
    info = (pages[0].get("imageinfo") or [{}])[0] if pages else {}
    url = info.get("url")
    if not url:
        raise PronunciationError(f"Commons has no file named {filename!r}")
    audio = _throttled_bytes(url, MAX_AUDIO_BYTES)
    if len(audio) > MAX_AUDIO_BYTES:
        raise PronunciationError(f"{filename} is larger than 5MB")
    ext = "." + filename.rsplit(".", 1)[-1].lower()
    return audio, ext


def list_voices(api_key: str) -> list[dict]:
    """Available voices, so a German one can be chosen deliberately.

    The default voice on any TTS service is an English speaker; using it for
    German produces an anglophone accent, which is the opposite of what a
    pronunciation card is for.
    """
    req = urllib.request.Request(
        f"{ELEVENLABS}/voices",
        headers={"xi-api-key": api_key, "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise PronunciationError(
            f"ElevenLabs rejected the voices request ({exc.code}): "
            f"{exc.read().decode(errors='replace')[:200]}"
        ) from exc
    return payload.get("voices", [])


def synthesize_elevenlabs(
    text: str, api_key: str, voice_id: str, model: str,
    previous_text: str = "", next_text: str = "",
) -> tuple[bytes, str]:
    """ElevenLabs text-to-speech. Returns (mp3 bytes, ".mp3").

    The model must be a multilingual one -- the English-only models will read
    German text with an English accent, which would teach the wrong thing.

    `previous_text` and `next_text` are CONTEXT, not content: the API uses them
    to decide how to read `text` and does not speak them. A word alone gives
    the model nothing to disambiguate with -- "neun" is one phoneme from
    "nein", and it read the wrong one -- while the sentence around it settles
    the reading. So the word card gets audio of the word alone, pronounced as
    it would be in the learner's own example sentence.
    """
    # A bare word is read as a fragment and the tail gets clipped -- "neun"
    # came back cut off mid-vowel. A full stop gives it a cadence to finish on.
    # Measured against the alternatives: an ellipsis left a pause too long to
    # drill against, and keeping next_text (rather than ending here) kept the
    # clipping, because the model still had somewhere to run on to.
    if text and text[-1] not in ".!?…:;,":
        text = text + "."

    body = json.dumps({
        "text": text,
        "model_id": model,
        "previous_text": previous_text or None,
        "next_text": next_text or None,
        "voice_settings": {
            # Higher stability than the default: study audio should be
            # consistent and plainly articulated rather than expressive, since
            # it is going to be imitated.
            "stability": 0.7,
            "similarity_boost": 0.75,
            "speed": 0.92,
        },
    }).encode()
    clip = _elevenlabs_post(
        f"{ELEVENLABS}/text-to-speech/{urllib.parse.quote(voice_id)}",
        body, api_key, accept="audio/mpeg",
    )
    if len(clip) > MAX_AUDIO_BYTES:
        raise PronunciationError("synthesised audio exceeded 5MB")
    return clip, ".mp3"


# The plan behind this key allows 3 requests in flight at once -- ElevenLabs
# says so in the 429: "maximum of 3 concurrent requests". Best-of-N makes
# takes in parallel and two tool calls can overlap, so one server-wide cap
# keeps every caller inside the limit instead of each round tripping it.
ELEVENLABS_MAX_CONCURRENT = 3
_elevenlabs_slots = threading.BoundedSemaphore(ELEVENLABS_MAX_CONCURRENT)
# The cap covers this process only; anything else on the same key (a
# diagnostic run, another client) can still push it over, so that particular
# 429 is retried after a short wait rather than treated as a quota failure.
CONCURRENCY_RETRIES = 3


def _elevenlabs_post(
    url: str, body: bytes, api_key: str, accept: str, attempt: int = 0,
) -> bytes:
    """POST to ElevenLabs, mapping its failures onto PronunciationError."""
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": accept,
            "User-Agent": USER_AGENT,
        },
    )
    # Twice the audio cap: /with-timestamps wraps the audio in base64 JSON,
    # which is a third larger than the audio it carries.
    limit = MAX_AUDIO_BYTES * 2
    try:
        with _elevenlabs_slots, urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            content = resp.read(limit + 1)
        # Refused here rather than returned: a truncated JSON body would fail
        # in json.loads as a JSONDecodeError, which no caller catches.
        if len(content) > limit:
            raise PronunciationError(
                f"ElevenLabs response exceeded {limit // (1024 * 1024)}MB")
        return content
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 401:
            raise PronunciationError(
                "ElevenLabs rejected the key. Check ELEVENLABS_API_KEY."
            ) from exc
        if exc.code == 422:
            raise PronunciationError(
                f"ElevenLabs rejected the request -- usually an unknown "
                f"voice id or model. ({detail})"
            ) from exc
        if exc.code == 429 and "concurrent_limit_exceeded" in detail:
            if attempt < CONCURRENCY_RETRIES:
                time.sleep(1.5 * (attempt + 1))
                return _elevenlabs_post(url, body, api_key, accept, attempt + 1)
            raise PronunciationError(
                "ElevenLabs refused: too many requests in flight on this key "
                f"(the plan allows {ELEVENLABS_MAX_CONCURRENT}). Something else is "
                "using the same key at the same time."
            ) from exc
        if exc.code == 429:
            raise PronunciationError(
                "ElevenLabs quota exhausted for this period. The free tier is "
                "10k characters/month; a card is roughly 30, so this is a lot "
                f"of cards. ({detail})"
            ) from exc
        raise PronunciationError(f"ElevenLabs failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PronunciationError(f"ElevenLabs unreachable: {exc}") from exc


# Word audio is cut out of a carrier phrase rather than synthesised alone.
#
# Said alone, a word ends the utterance, and the model says an utterance-final
# stop the way a tired speaker does: a long closure, then a faint release. For
# "der Berg" that measured a 60 ms closure and a burst 24 dB below the vowel,
# where a native recording (Commons De-Berg2.ogg) has a 20 ms closure and a
# burst 9 dB below it.
# The learner heard [bɛʁ] and no [k] -- fatal on a deck teaching final
# devoicing, where the final [p t k s f] is the whole point.
#
# Inside a carrier, with the phrase running on after it, the closure came back
# native-length (10-30 ms) on every word measured. The burst did not reliably
# follow: identical requests differed by 8 dB. That spread is per-take
# randomness, not anything in the request (language_code, other models and a
# vowel-initial continuation were all tried), so the fix for it is selection:
# several takes are made and the one with the strongest release is kept.
CARRIER_OPEN = "\u201e"                      # „
CARRIER_CLOSE = "\u201c, sagte sie."        # “, sagte sie.
WORD_OUTPUT_FORMAT = f"pcm_{audio.SAMPLE_RATE}"
# Kept before the first letter, in case the alignment marks the onset late.
SLICE_LEAD_SECONDS = 0.05
# Kept after the last letter: the release burst and its aspiration land after
# the aligner's end-of-letter time.
SLICE_RELEASE_SECONDS = 0.12
# Takes are requested in parallel rounds until one's release reaches
# ACCEPT_RELEASE_DB (see audio.Release: a native [k] in "Berg" measures -9.4),
# or MAX_TAKES is reached -- then the best seen is kept. Measured over 12-take
# runs, the median take sits near -20 whatever the voice settings (stability,
# speed and style were each tried) and the good ones are the tail, so the lever
# is the number of draws: 12 topped out around -12 to -16.
TAKES_PER_ROUND = ELEVENLABS_MAX_CONCURRENT
MAX_TAKES = 24
ACCEPT_RELEASE_DB = -13.0
# Spellings that end in an obstruent -- a stop or fricative whose release the
# selection can measure. Anything else ("die Blume") gets a single take:
# ranking takes by a burst the word does not have would pick at random.
_FINAL_OBSTRUENT = re.compile(r"(?:[bdgptkfvszßx]|ch)$", re.IGNORECASE)
# Multi-letter spellings of one final sound, longest first. The release is
# measured from the first letter of the unit.
_FINAL_SPELLING_UNITS = ("sch", "ch")
MP3_BITRATE_KBPS = 128


def carrier_text(word: str) -> str:
    return f"{CARRIER_OPEN}{word}{CARRIER_CLOSE}"


# Final -ig is not devoiced to [k] in the standard: it is spirantized to [ɪç]
# (König [ˈkøːnɪç]; the south says [ɪk]). Ranking takes by release strength
# could favour a [k] take over the standard [ç], so these words get one take.
# "ei" + g is ordinary devoicing (Zweig [tsvaɪ̯k], Teig) and keeps selection.
_FINAL_IG = re.compile(r"(?<![eE])ig$", re.IGNORECASE)


# A final -ig take must end in the [ç] hiss, not a [k] burst. Calibrated on
# live takes with the longest-unbroken-run measure: 30 -ig takes (wenig,
# König, billig, ruhig, wichtig) held the hiss 110-170 ms, one outlier at 80;
# real [k] (Zweig, Tag) ran 30-70 ms. 90 is the midpoint of that gap: 20 ms
# clear of every [k] measured, and a weak [ç] merely costs another take.
ACCEPT_HISS_MS = 90
MAX_IG_TAKES = 6


def ends_in_ig(word: str) -> bool:
    return bool(_FINAL_IG.search(re.sub(r"\W+$", "", word)))


def ends_in_obstruent(word: str) -> bool:
    """Should this word's takes be ranked by final release strength?"""
    bare = re.sub(r"\W+$", "", word)
    return bool(_FINAL_OBSTRUENT.search(bare)) and not _FINAL_IG.search(bare)


def word_slice_seconds(alignment: dict, word: str) -> tuple[float, float, float]:
    """(start, end, final spelling-unit start) of `word` inside its carrier.

    Starts a little before the first spoken character and ends
    SLICE_RELEASE_SECONDS after the last one, but never past the first spoken
    character of the carrier's continuation. Punctuation inside the word
    ("Servus!") is not spoken and does not move any edge. The third value is
    where the final sound's spelling begins -- the release is measured from
    there -- which is the "c" of a final "ch" and the "s" of "sch": the
    fricative of "Buch" starts at the c, and measuring from the h can miss it.
    """
    characters = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    if "".join(characters) != carrier_text(word):
        raise PronunciationError(
            "ElevenLabs' alignment does not match the carrier text, so the word "
            "cannot be located in it"
        )
    # Checked here, where the timings enter, rather than by catching whatever
    # each one breaks later: a short list is an IndexError, and JSON's 1e309
    # parses to inf, which round() turns into OverflowError.
    if not all(
        len(times) == len(characters)
        and all(isinstance(time, (int, float)) and math.isfinite(time) for time in times)
        for times in (starts, ends)
    ):
        raise PronunciationError(
            "ElevenLabs' alignment timings are incomplete or not finite")
    word_indices = range(len(CARRIER_OPEN), len(CARRIER_OPEN) + len(word))
    spoken_in_word = [index for index in word_indices if characters[index].isalnum()]
    if not spoken_in_word:
        raise PronunciationError(f"{word!r} has nothing to pronounce")
    continuation_starts = [
        starts[index]
        for index in range(word_indices.stop, len(characters))
        if characters[index].isalnum()
    ]
    last_end = ends[spoken_in_word[-1]]
    release_end = min([last_end + SLICE_RELEASE_SECONDS, *continuation_starts[:1]])
    spoken_tail = "".join(characters[index] for index in spoken_in_word[-3:]).lower()
    final_unit_length = next(
        (len(unit) for unit in _FINAL_SPELLING_UNITS if spoken_tail.endswith(unit)), 1)
    return (max(0.0, starts[spoken_in_word[0]] - SLICE_LEAD_SECONDS),
            max(last_end, release_end),
            starts[spoken_in_word[-min(final_unit_length, len(spoken_in_word))]])


@dataclass(frozen=True)
class Take:
    samples: array
    release: audio.Release | None
    hiss_ms: int | None = None

    @property
    def score(self) -> float:
        if self.release:
            return self.release.release_db
        return float(self.hiss_ms) if self.hiss_ms is not None else -math.inf


def best_take(make_take, rounds_left: int | None = None,
              taken: tuple[Take, ...] = (), made: int = 0,
              accept: float = ACCEPT_RELEASE_DB, max_takes: int = MAX_TAKES,
              ) -> tuple[Take, int]:
    """Make takes in parallel rounds; return (the best, how many were made).

    Stops as soon as a take's score reaches `accept`. A take that fails is
    dropped rather than failing the word, so a rejected request in a round
    costs nothing; only a round in which every take fails, with nothing from
    earlier rounds to fall back on, raises.
    """
    rounds_left = max_takes // TAKES_PER_ROUND if rounds_left is None else rounds_left

    def attempt(_: int) -> Take | PronunciationError:
        try:
            return make_take()
        except PronunciationError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=TAKES_PER_ROUND) as pool:
        outcomes = list(pool.map(attempt, range(TAKES_PER_ROUND)))
    succeeded = [outcome for outcome in outcomes if isinstance(outcome, Take)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, PronunciationError)]
    all_takes = (*taken, *succeeded)
    if not all_takes:
        raise failures[0]
    best = max(all_takes, key=lambda take: take.score)
    made = made + len(outcomes)
    if best.score >= accept or rounds_left <= 1:
        return best, made
    return best_take(make_take, rounds_left - 1, all_takes, made, accept, max_takes)


def encode_mp3(pcm: bytes) -> bytes:
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(MP3_BITRATE_KBPS)
    encoder.set_in_sample_rate(audio.SAMPLE_RATE)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return bytes(encoder.encode(pcm) + encoder.flush())


def synthesize_word(
    word: str, api_key: str, voice_id: str, model: str, previous_text: str = "",
) -> tuple[bytes, str, str]:
    """A word's audio, cut from the best of several carrier-phrase takes.

    Returns (mp3, ".mp3", a note on the selection for the tool's report).

    `previous_text` is sent only for words that are NOT selected on. It is what
    settled "neun" versus "nein", but for an obstruent-final word it weakens
    the release: across 12 takes each of Berg, Tag and gelb, sending it (with
    language_code) lowered the median release by 2-4 dB and left some gelb
    takes with no release at all. The carrier already supplies the German
    context those words need.
    """
    selecting = ends_in_obstruent(word)
    checking_hiss = ends_in_ig(word)
    body = json.dumps({
        "text": carrier_text(word),
        "model_id": model,
        "previous_text": None if selecting else (previous_text or None),
        "voice_settings": {"stability": 0.7, "similarity_boost": 0.75, "speed": 0.92},
    }).encode()
    url = (f"{ELEVENLABS}/text-to-speech/{urllib.parse.quote(voice_id)}/with-timestamps"
           f"?output_format={WORD_OUTPUT_FORMAT}")

    def make_take() -> Take:
        response = _elevenlabs_post(url, body, api_key, accept="application/json")
        # A malformed body is one failed take, not a failed word: best_take
        # drops takes that raise PronunciationError and nothing else, so a
        # KeyError, bad base64, odd-length PCM or a timing list shorter than
        # its characters would abort the other takes with it.
        try:
            payload = json.loads(response)
            start, end, final_unit = word_slice_seconds(payload["alignment"], word)
            samples = audio.cut(base64.b64decode(payload["audio_base64"], validate=True),
                                start, end)
            release = audio.final_release(samples, final_unit - start) if selecting else None
            hiss = audio.final_hiss_ms(samples, final_unit - start) if checking_hiss else None
        except (ValueError, KeyError, TypeError, IndexError, ArithmeticError) as exc:
            raise PronunciationError(
                f"ElevenLabs returned an unusable /with-timestamps body ({exc!r:.120})"
            ) from exc
        return Take(samples, release, hiss)

    if selecting:
        chosen, made = best_take(make_take)
        note = (f"best of {made} takes, final release {chosen.score:+.1f} dB"
                if chosen.release else f"best of {made} takes, no final release found")
    elif checking_hiss:
        chosen, made = best_take(make_take, accept=ACCEPT_HISS_MS, max_takes=MAX_IG_TAKES)
        verdict = "[ç]" if chosen.score >= ACCEPT_HISS_MS else "no take reached [ç] length"
        note = f"{made} takes, final hiss {chosen.hiss_ms} ms ({verdict})"
    else:
        chosen, note = make_take(), "single take"
    log.info("word audio for %r: %s", word, note)
    encoded = encode_mp3(audio.finish(chosen.samples))
    if len(encoded) > MAX_AUDIO_BYTES:
        raise PronunciationError("synthesised audio exceeded 5MB")
    return encoded, ".mp3", note


def synthesize_google(
    text: str, api_key: str, voice: str
) -> tuple[bytes, str]:
    """Google Cloud Text-to-Speech. Returns (mp3 bytes, ".mp3").

    Google's de-DE voices are recorded by German speakers, and the free tier
    covers far more than a flashcard habit will use. That combination is why
    this exists alongside ElevenLabs: ElevenLabs has better voices, but its
    German ones are library voices, and library voices are paid-only over the
    API -- a free key can reach only its English-accented premade set, which is
    the wrong tool for a pronunciation card.
    """
    body = json.dumps({
        "input": {"text": text},
        "voice": {"languageCode": "de-DE", "name": voice},
        # A little under natural pace: this is audio to be imitated, not
        # listened to passively.
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": 0.92},
    }).encode()
    req = urllib.request.Request(
        f"{GOOGLE_TTS}?key={urllib.parse.quote(api_key)}",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 403:
            raise PronunciationError(
                "Google rejected the request. Enable the Cloud Text-to-Speech "
                "API on the project at console.cloud.google.com, and check the "
                f"key is not restricted to a different API. ({detail})"
            ) from exc
        raise PronunciationError(f"Google TTS failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PronunciationError(f"Google TTS unreachable: {exc}") from exc

    return base64.b64decode(payload["audioContent"]), ".mp3"


def fetch(
    word: str,
    *,
    tts_key: str | None = None,
    tts_voice: str = "",
    tts_model: str = "eleven_multilingual_v2",
    tts_provider: str = "elevenlabs",
    allow_tts: bool = True,
    prefer_tts: bool = False,
    context: tuple[str, str] = ("", ""),
    headword: bool = False,
) -> Pronunciation:
    """Best available pronunciation for a headword or phrase.

    `prefer_tts` synthesises the audio even when Wiktionary has a recording.
    That sounds like a downgrade -- the method wants real speakers -- but
    Wiktionary's recordings are contributed by different volunteers on
    different microphones, so a deck mixing them with synthesis varies in
    voice, volume and recording quality from card to card. One consistent
    voice is easier to imitate.

    Either way the IPA still comes from Wiktionary, which is the only source
    for it here; ElevenLabs returns audio and nothing else.

    `headword` marks the note's Word, as opposed to its example sentence: an
    ElevenLabs headword is sliced out of a carrier phrase (see
    synthesize_word) so a final stop keeps its release. Sentences end on
    whatever the sentence ends on and are synthesised as they are.
    """
    ipa = audio_file = found_as = None
    compound_of: str | None = None
    try:
        ipa, audio_file, found_as = lookup_wiktionary(word)
    except PronunciationError as exc:
        log.info("wiktionary lookup failed for %r: %s", word, exc)

    # A compound can be perfectly ordinary German and still have no entry of
    # its own. Assemble one from the parts before giving up on IPA entirely.
    if not ipa:
        try:
            ipa, compound_of = compound_ipa(word)
        except PronunciationError as exc:
            log.info("compound lookup failed for %r: %s", word, exc)
        if compound_of:
            log.info("derived IPA for %r from %s", word, compound_of)

    if audio_file and not (prefer_tts and allow_tts and tts_key and tts_voice):
        try:
            data, ext = download_commons_file(audio_file)
            return Pronunciation(ipa=ipa, audio=data, audio_ext=ext,
                                 source=f"Wiktionary/Commons ({audio_file})",
                                 looked_up_as=found_as)
        except PronunciationError as exc:
            log.info("commons audio unavailable for %r: %s", word, exc)

    if allow_tts and tts_key and tts_voice:
        spoken = re.sub(r"<[^>]+>", "", word).strip()
        if tts_provider == "google":
            data, ext = synthesize_google(spoken, tts_key, tts_voice)
            source = f"Google TTS ({tts_voice})"
        else:
            before, after = context
            if headword:
                data, ext, selection = synthesize_word(
                    spoken, tts_key, tts_voice, tts_model, previous_text=before)
                source = f"ElevenLabs ({tts_voice}), cut from a carrier phrase ({selection})"
                # synthesize_word withholds the context from an obstruent-final
                # word, so the report must not claim it was used.
                context_sent = bool(before) and not ends_in_obstruent(spoken)
            else:
                data, ext = synthesize_elevenlabs(
                    spoken, tts_key, tts_voice, tts_model,
                    previous_text=before, next_text=after,
                )
                source = f"ElevenLabs ({tts_voice})"
                context_sent = bool(before or after)
            if context_sent:
                source += " with sentence context"
        # IPA may still be None here; nothing fabricates one.
        if compound_of:
            source += f"; IPA from parts {compound_of}"
        return Pronunciation(ipa=ipa, audio=data, audio_ext=ext,
                             source=source, looked_up_as=found_as)

    if ipa:
        return Pronunciation(
            ipa=ipa,
            source=(f"Wiktionary parts ({compound_of}), IPA only"
                    if compound_of else "Wiktionary (IPA only)"),
            looked_up_as=found_as,
        )
    needed = (
        "GOOGLE_TTS_API_KEY and ANKI_MCP_TTS_VOICE"
        if tts_provider == "google"
        else "ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID"
    )
    raise PronunciationError(
        f"No pronunciation for {word!r}. Wiktionary has no entry for it -- "
        f"usual for phrases -- and no {tts_provider} key/voice is configured, "
        f"so nothing can be synthesised. Set {needed} to cover these."
    )


def store_audio(col, word: str, data: bytes, ext: str) -> str:
    """Write into collection.media and return Anki's sound reference.

    Anki plays media through [sound:name], NOT an <audio> tag -- an <audio>
    element renders as nothing on AnkiDroid.
    """
    slug = re.sub(r"[^\w]+", "-", word.strip().lower(), flags=re.UNICODE).strip("-")[:60]
    filename = col.media.write_data(f"anki-mcp-{slug or 'audio'}{ext}", data)
    return f"[sound:{filename}]"
