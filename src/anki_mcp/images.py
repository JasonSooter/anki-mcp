"""Fetching a picture for a card.

Fluent Forever's whole argument is that meaning should arrive as a picture
rather than an English gloss, so on this collection an image is not decoration
-- it is the side of the card that carries the meaning. That is why add_note
refuses a note it cannot illustrate.

There is deliberately no Google Images scraper here. Google publishes no
public image-search API, and scraping the results page violates their terms and
breaks whenever the markup shifts. Two real providers instead:

Provider choice is about RELEVANCE first, availability second. Wikimedia is
consistently fast, but its full-text search matches words inside scanned
document titles: asking it for "Wir sehen uns" returns a 1914 photograph of a
troop train, and a wrong picture is worse than none on a card whose whole job
is to carry meaning through the image. Openverse indexes actual photography and
returns usable pictures, but is intermittently slow.

So the default is a chain: Openverse for relevance, falling back to Wikimedia
only when Openverse is unreachable.

* Pixabay    -- a free key, and a library built for exactly this: everyday
               objects and scenes, searchable in German. Best relevance, so it
               leads the chain when a key is configured.
* Openverse  -- no credentials, real photography. Tried first when no key.
* Wikimedia  -- no credentials, fast, but skews to scanned documents. Fallback.
* Google CSE -- the Custom Search JSON API, if a key and engine id are set.
                The best results by a distance, and what Wyner actually
                suggests; searched with a German interface so the pictures
                match how the word is used here.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

# Anki has to be able to render it, and a card should not carry a 5MB photo.
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                 "image/gif": ".gif"}
MAX_BYTES = 3 * 1024 * 1024
HTTP_TIMEOUT = 30
# A handful of candidates, because the first hit is often a logo or a stock
# watermark; the caller takes the first that actually downloads.
CANDIDATES = 5

USER_AGENT = "anki-mcp/0.1 (personal flashcard tool)"


class ImageError(RuntimeError):
    """No usable image could be obtained."""


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str | None = None
    source: str | None = None
    license: str | None = None
    # A small render, for showing several candidates to a picker without
    # shipping several full-size photographs.
    preview_url: str | None = None


class ImageProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int = CANDIDATES) -> list[Candidate]:
        """Return candidate image URLs, best first."""


# Both free APIs throttle. A bulk run hit Wikimedia's limit after ~10 rapid
# calls, so requests are spaced and 429s are retried rather than surfaced.
MIN_INTERVAL_SECONDS = 1.0
MAX_RETRIES = 3

_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _get(url: str, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 503):
                raise
            # Honour Retry-After when offered, else back off exponentially.
            delay = float(exc.headers.get("Retry-After") or 0) or 2 ** (attempt + 1)
            log.warning("rate limited (%s); retrying in %.0fs", exc.code, delay)
            time.sleep(min(delay, 20))
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise last if last else RuntimeError("request failed")


class OpenverseProvider:
    """openverse.org -- no API key required."""

    name = "openverse"
    ENDPOINT = "https://api.openverse.org/v1/images/"

    def search(self, query: str, *, limit: int = CANDIDATES) -> list[Candidate]:
        params = urllib.parse.urlencode({
            "q": query,
            "page_size": limit,
            # Only licences that allow reuse, so the collection stays clean even
            # though it is private.
            "license_type": "all-cc",
            "mature": "false",
        })
        try:
            data = json.loads(_get(f"{self.ENDPOINT}?{params}"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise ImageError(f"Openverse search failed: {exc}") from exc
        return [
            Candidate(url=r["url"], title=r.get("title"),
                      source=r.get("source"), license=r.get("license"),
                      preview_url=r.get("thumbnail"))
            for r in data.get("results", []) if r.get("url")
        ]


class GoogleCSEProvider:
    """Google Custom Search JSON API in image mode."""

    name = "google"
    ENDPOINT = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, engine_id: str) -> None:
        self._key = api_key
        self._cx = engine_id

    def search(self, query: str, *, limit: int = CANDIDATES) -> list[Candidate]:
        params = urllib.parse.urlencode({
            "key": self._key, "cx": self._cx, "q": query,
            "searchType": "image", "num": min(limit, 10),
            "safe": "active",
            # German interface, so results match how the word is actually used
            # here rather than an American stock-photo reading of it.
            "hl": "de", "gl": "de",
        })
        try:
            data = json.loads(_get(f"{self.ENDPOINT}?{params}"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:200]
            if exc.code == 429:
                raise ImageError(
                    "Google Custom Search daily quota exhausted (free tier is 100 "
                    "queries/day). Set ANKI_MCP_IMAGE_PROVIDER=openverse to fall back."
                ) from exc
            raise ImageError(f"Google Custom Search failed ({exc.code}): {body}") from exc
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise ImageError(f"Google Custom Search failed: {exc}") from exc
        return [
            Candidate(url=i["link"], title=i.get("title"),
                      source=(i.get("displayLink")))
            for i in data.get("items", []) if i.get("link")
        ]


class WikimediaProvider:
    """Wikimedia Commons search. No API key, and reliably fast."""

    name = "wikimedia"
    ENDPOINT = "https://commons.wikimedia.org/w/api.php"

    def search(self, query: str, *, limit: int = CANDIDATES) -> list[Candidate]:
        params = urllib.parse.urlencode({
            "action": "query", "format": "json", "generator": "search",
            "gsrnamespace": "6",            # File: namespace only
            "gsrsearch": query, "gsrlimit": limit,
            "prop": "imageinfo", "iiprop": "url|mime|extmetadata",
            # A scaled thumbnail rather than the original, which on Commons is
            # routinely a 20MB TIFF. Small, because a scan shows many at once.
            "iiurlwidth": "320",
        })
        try:
            data = json.loads(_get(f"{self.ENDPOINT}?{params}"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise ImageError(f"Wikimedia search failed: {exc}") from exc

        results: list[Candidate] = []
        for page in (data.get("query") or {}).get("pages", {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            url = info.get("thumburl") or info.get("url")
            if not url:
                continue
            # Commons' File: namespace is full of digitised books and
            # newspapers. A scan of a as PDF page is never the picture we want,
            # and it crowds out real photographs -- five of eighteen slots in one
            # search were newspaper front pages.
            mime = (info.get("mime") or "").lower()
            title = page.get("title", "")
            if not mime.startswith("image/"):
                continue
            if title.lower().endswith((".pdf", ".djvu", ".tif", ".tiff", ".svg")):
                continue
            meta = info.get("extmetadata") or {}
            results.append(Candidate(
                url=url,
                title=page.get("title", "").removeprefix("File:"),
                source="Wikimedia Commons",
                license=(meta.get("LicenseShortName") or {}).get("value"),
                preview_url=info.get("thumburl"),
            ))
        return results


class PixabayProvider:
    """pixabay.com -- free key, curated everyday photography.

    Wikimedia and Openverse both index whatever exists; Pixabay indexes what
    people photograph on purpose, which is much closer to what a vocabulary
    card needs.
    """

    name = "pixabay"
    ENDPOINT = "https://pixabay.com/api/"

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def search(self, query: str, *, limit: int = CANDIDATES) -> list[Candidate]:
        params = urllib.parse.urlencode({
            "key": self._key,
            "q": query,
            "image_type": "photo",     # not vectors or illustrations
            "lang": "de",              # match how the word is used here
            "safesearch": "true",
            "per_page": max(limit, 3), # the API rejects per_page < 3
            "order": "popular",
        })
        try:
            data = json.loads(_get(f"{self.ENDPOINT}?{params}"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403):
                raise ImageError(
                    "Pixabay rejected the request; check PIXABAY_API_KEY. "
                    f"({exc.code})"
                ) from exc
            raise ImageError(f"Pixabay search failed ({exc.code})") from exc
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise ImageError(f"Pixabay search failed: {exc}") from exc

        return [
            Candidate(
                # webformatURL is the ~640px render: big enough for a card,
                # small enough not to bloat the media folder.
                url=hit["webformatURL"],
                title=(hit.get("tags") or "").strip() or None,
                source="Pixabay",
                license="Pixabay Content License",
                preview_url=hit.get("previewURL"),
            )
            for hit in data.get("hits", []) if hit.get("webformatURL")
        ]


class ChainProvider:
    """Try providers in order; first one returning candidates wins.

    Lets a relevance-first provider lead while still producing a picture when
    that provider is having a bad day.
    """

    def __init__(self, *providers: ImageProvider) -> None:
        self._providers = providers
        self.name = "+".join(p.name for p in providers)

    def search(self, query: str, *, limit: int = CANDIDATES) -> list[Candidate]:
        problems = []
        for provider in self._providers:
            try:
                found = provider.search(query, limit=limit)
            except ImageError as exc:
                problems.append(f"{provider.name}: {exc}")
                continue
            if found:
                self.name = provider.name
                return found
            problems.append(f"{provider.name}: no results")
        raise ImageError("; ".join(problems))


PROVIDERS = {"wikimedia": WikimediaProvider, "openverse": OpenverseProvider}


def build_provider(
    kind: str,
    google_key: str | None = None,
    google_cx: str | None = None,
    pixabay_key: str | None = None,
) -> ImageProvider:
    if kind == "google":
        if not (google_key and google_cx):
            raise ImageError(
                "ANKI_MCP_IMAGE_PROVIDER=google needs GOOGLE_CSE_API_KEY and "
                "GOOGLE_CSE_ENGINE_ID. Create both at console.cloud.google.com "
                "(Custom Search API) and programmablesearchengine.google.com, "
                "with image search enabled."
            )
        return GoogleCSEProvider(google_key, google_cx)
    if kind == "pixabay":
        if not pixabay_key:
            raise ImageError(
                "ANKI_MCP_IMAGE_PROVIDER=pixabay needs PIXABAY_API_KEY. Get a "
                "free one at https://pixabay.com/api/docs/ (sign in, the key is "
                "shown on that page)."
            )
        return PixabayProvider(pixabay_key)
    if kind == "auto":
        # Best relevance first, then the keyless fallbacks.
        chain: list[ImageProvider] = []
        if pixabay_key:
            chain.append(PixabayProvider(pixabay_key))
        chain += [OpenverseProvider(), WikimediaProvider()]
        return ChainProvider(*chain)
    factory = PROVIDERS.get(kind)
    if factory is None:
        # Listed explicitly: pixabay and google are handled above rather than
        # via PROVIDERS, and leaving them out of this message sent someone
        # debugging config looking for a bug that was not there.
        valid = ", ".join(["auto", "pixabay", *PROVIDERS, "google"])
        raise ImageError(f"ANKI_MCP_IMAGE_PROVIDER={kind!r} is not valid. Use one of: {valid}")
    return factory()


# Words that carry no signal when matching a query against image tags.
_STOPWORDS = {"der", "die", "das", "ein", "eine", "zum", "zur", "und", "im", "in",
              "am", "an", "auf", "mit", "von", "the", "a", "of"}


def _terms(text: str) -> set[str]:
    return {
        w for w in re.split(r"[^\w]+", (text or "").lower(), flags=re.UNICODE)
        if len(w) > 2 and w not in _STOPWORDS
    }


def score_candidate(query: str, candidate: Candidate) -> float:
    """How well a candidate's own words match the search terms, 0..1.

    Providers rank by popularity, not by relevance to the phrase asked for:
    searching "Buero Feierabend" returned a photograph of paperclips first while
    four images actually tagged 'feierabend' sat below it. Ranking by term
    overlap puts those first instead.

    This measures WORDS, not pictures. A cartoon frog tagged "winken, abschied"
    scores perfectly, so a zero score is meaningful but a high one is not proof.
    """
    wanted = _terms(query)
    if not wanted:
        return 0.0
    have = _terms(candidate.title or "") | _terms(candidate.source or "")
    return len(wanted & have) / len(wanted)


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^\w]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-")
    return cleaned[:60] or "image"


def fetch_image(provider: ImageProvider, query: str) -> tuple[bytes, str, Candidate]:
    """Find and download a picture. Returns (data, extension, chosen candidate).

    Tries each candidate in turn: search hits routinely 404, redirect to an HTML
    page, or serve something that is not an image at all.
    """
    candidates = provider.search(query)
    # Re-rank before downloading: the provider's own order is popularity-based.
    scored = sorted(
        ((score_candidate(query, c), i, c) for i, c in enumerate(candidates)),
        key=lambda t: (-t[0], t[1]),
    )
    if scored and scored[0][0] == 0.0:
        raise ImageError(
            f"Nothing matching {query!r} -- the {len(candidates)} candidate(s) "
            "returned share no words with the search. Try naming the object "
            "itself in German, or pass abstract=true if this word cannot be "
            "pictured (the German definition then carries the meaning)."
        )
    candidates = [c for _, _, c in scored]
    if not candidates:
        raise ImageError(
            f"No image found for {query!r} via {provider.name}. Try a more "
            "concrete search term -- a picture of the thing, not the abstract "
            "idea (e.g. 'Kellner Restaurant' rather than 'service')."
        )

    problems: list[str] = []
    for cand in candidates:
        try:
            req = urllib.request.Request(cand.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in ALLOWED_TYPES:
                    problems.append(f"{cand.url[:60]}: {ctype or 'unknown type'}")
                    continue
                data = resp.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                problems.append(f"{cand.url[:60]}: larger than {MAX_BYTES // 1024 // 1024}MB")
                continue
            return data, ALLOWED_TYPES[ctype], cand
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            problems.append(f"{cand.url[:60]}: {exc}")
            continue

    raise ImageError(
        f"Found {len(candidates)} candidate(s) for {query!r} but none could be "
        f"downloaded as an image. Tried: " + "; ".join(problems[:3])
    )


def all_providers(
    google_key: str | None,
    google_cx: str | None,
    pixabay_key: str | None,
) -> list[ImageProvider]:
    """Every provider we can actually reach, for pooled searching.

    Distinct from build_provider's chain, which stops at the first source that
    answers. When a caller is going to LOOK at the results, more variety beats
    fewer round trips -- the libraries have very different content, and the one
    that happens to answer first is not necessarily the one holding the right
    picture.
    """
    out: list[ImageProvider] = []
    if pixabay_key:
        out.append(PixabayProvider(pixabay_key))
    out.append(OpenverseProvider())
    out.append(WikimediaProvider())
    if google_key and google_cx:
        out.append(GoogleCSEProvider(google_key, google_cx))
    return out


def _dedupe_key(cand: Candidate) -> str:
    """Collapse near-duplicates.

    A single search returned the same ceramic frog four times out of five,
    burning slots that could have shown genuinely different pictures. Identical
    tag strings are the reliable signal for that.
    """
    return (cand.title or cand.url).strip().lower()[:120]


def gather(
    providers: list[ImageProvider],
    queries: list[str],
    *,
    per_search: int = 12,
    limit: int = 24,
    max_searches: int = 12,
) -> list[tuple[Candidate, str, str]]:
    """Search every provider for every query, pooled and deduplicated.

    Returns (candidate, provider_name, query), interleaved so the first results
    a caller sees come from different sources and different queries rather than
    eight variations of one photograph.
    """
    buckets: list[list[tuple[Candidate, str, str]]] = []
    searches = 0
    for query in queries:
        for provider in providers:
            if searches >= max_searches:
                break
            searches += 1
            try:
                found = provider.search(query, limit=per_search)
            except ImageError as exc:
                log.info("image search failed on %s for %r: %s",
                         provider.name, query, exc)
                continue
            if found:
                buckets.append([(c, provider.name, query) for c in found])

    # Round-robin the buckets so no single provider or query monopolises.
    pooled: list[tuple[Candidate, str, str]] = []
    seen: set[str] = set()
    for row in range(per_search):
        for bucket in buckets:
            if row >= len(bucket):
                continue
            cand, pname, q = bucket[row]
            key = _dedupe_key(cand)
            if key in seen:
                continue
            seen.add(key)
            pooled.append((cand, pname, q))
            if len(pooled) >= limit:
                return pooled
    return pooled


def download_many(urls: list[str], workers: int = 8) -> dict[str, bytes | None]:
    """Fetch many previews at once.

    Scanning thirty thumbnails one after another would take longer than the
    searches did. These are static CDN assets on many hosts, so unlike the
    search APIs they neither need nor deserve the shared rate limiter.
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(u: str) -> tuple[str, bytes | None]:
        try:
            return u, download(u)[0]
        except ImageError:
            return u, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, urls))


def download(url: str) -> tuple[bytes, str]:
    """Fetch one specific image URL, returning (data, extension).

    Used when a caller has already chosen a candidate and does not want the
    provider's own pick.
    """
    # Shares the throttle and 429 backoff with the search calls. Downloading
    # with a bare urlopen while carefully rate-limiting the searches is how the
    # audio fetch broke first; the same oversight was here.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in ALLOWED_TYPES:
                    raise ImageError(f"{url} is {ctype or 'an unknown type'}, not an image")
                data = resp.read(MAX_BYTES + 1)
            break
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 503):
                raise ImageError(f"could not download {url}: {exc}") from exc
            delay = float(exc.headers.get("Retry-After") or 0) or 2 ** (attempt + 1)
            log.info("image download rate limited; retrying in %.0fs", delay)
            time.sleep(min(delay, 20))
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    else:
        raise ImageError(f"could not download {url}: {last}")
    if len(data) > MAX_BYTES:
        raise ImageError(f"{url} is larger than {MAX_BYTES // 1024 // 1024}MB")
    return data, ALLOWED_TYPES[ctype]


def store_image(col, query: str, data: bytes, ext: str) -> str:
    """Write into collection.media and return the <img> tag to embed.

    NOTE FOR ANYTHING THAT LATER DELETES MEDIA. The filename is derived from
    the search query, and Anki returns the existing name when the bytes match,
    so two notes can legitimately end up pointing at ONE file. Nothing in this
    server deletes media, so that is harmless today -- but a cleanup that
    removes a superseded image *by filename* would silently blank the picture
    on every other note sharing it.

    Resolve references before unlinking, never after. The collection already
    contains 23 files shared between notes (all from an imported deck), so this
    is not hypothetical; the generated images have avoided it by accident of
    naming rather than by design. Pinned by
    tests/test_core.py::test_store_image_can_share_a_filename_between_notes.
    """
    filename = col.media.write_data(f"anki-mcp-{_slug(query)}{ext}", data)
    # Anki references media by bare filename; the media sync ships the file.
    return f'<img src="{filename}">'
