"""Fetch one RSS or Atom feed and hand back its entries as plain dicts.

Situation: a scheduled poller hits the same public feeds every day. Most days
nothing changed, and some campus CMSs answer a default Python User-Agent with
"Request Rejected". This module does the one HTTP request correctly: it sends
a browser-like User-Agent, sends If-None-Match / If-Modified-Since when the
caller supplies the previous ETag / Last-Modified, and returns those
validators so the caller can store them wherever it likes.

Contract: ``fetch_feed(url, etag=None, last_modified=None, ...)`` returns
``{"status", "etag", "last_modified", "entries"}``. A 304 yields status 304
and an empty entry list; the caller treats that as "nothing new". Each entry
is ``{"id", "url", "title", "summary", "published_at"}`` with the summary
HTML-stripped to text and ``published_at`` as ISO 8601 UTC or None. Any
network error, non-200/304 status, or a body with no parseable entries
raises ``FeedFetchError`` naming the URL and cause; the caller decides
whether to log and continue.

Deliberately not here: fetching linked article pages or extracting main
text (see the page-main-text seed), persistence of validators, retry or
backoff, body-length caps, hashing, dedup, and any notion of a source name,
profile, or relevance. This module knows a URL and the last validators.

Dependencies: ``feedparser`` for dialect-tolerant parsing of bytes already
fetched (its own HTTP path is bypassed so headers stay ours). Everything
else is stdlib. The optional ``open_url`` callable is the transport seam:
tests hit a real stub HTTP server through the default urllib path, and a
future Playwright-backed fetcher can be dropped in without touching parsing.

Details the contract above leaves implicit:

* A 304 echoes the *input* ``etag`` and ``last_modified`` back, never None,
  so a caller that writes the result straight to its store cannot erase the
  validators it just used.
* A 200 reports the validators from the response headers, or None when the
  server sent none — the old ones are stale by definition.
* ``published_at`` prefers the entry's published date and falls back to its
  updated date (Atom feeds often carry only ``<updated>``), formatted as
  ``YYYY-MM-DDTHH:MM:SSZ``.
* ``url`` falls back to the entry id when there is no link; an entry with
  neither a link nor an id is dropped, being neither fetchable nor
  identifiable. A relative link is resolved against ``base_url``, which
  ``fetch_feed`` sets to the feed's own URL.
* ``parse_feed_bytes`` takes bytes and only bytes. ``feedparser.parse``
  treats a *string* as a resource to open and would fetch it over the
  network with its own headers; a ``TypeError`` here keeps that function
  pure and keeps the HTTP request in one place.
* The transport seam takes ``(url, headers, timeout_s)`` and returns
  ``(status, headers, body)`` — plain values, so an implementation never has
  to build a ``urllib`` object. The default transport speaks http and https
  only: ``urllib`` would otherwise open ``file://`` and read local disk for
  any URL that reached this module from a config file.
* ``Content-Encoding`` is undone before the body leaves the transport, so a
  caller always receives plain bytes. ``urllib`` does not decompress and
  ``requests`` does, which is why this is easy to miss: ``.decode()`` on a
  gzip body yields replacement characters rather than raising, and the
  failure then surfaces as nonsense content far from its cause. Nothing here
  sends ``Accept-Encoding``, so a compressed reply is not expected — this is
  for the proxy, the CDN, and the next header someone adds. A body that does
  not match the encoding it declares raises rather than being passed through.
* A bot wall is named rather than debugged. A challenge page answers 200 with
  HTML as readily as 403, and both previously surfaced as something else — a
  parse failure, or a status the operator would chase as a broken URL. It
  raises a distinct blocked error carrying the URL and one instruction: fetch
  it by hand. Nothing retries and nothing rotates headers, which neither
  defeats real bot detection nor costs less than stopping.
* Detection is STRUCTURAL and never a substring search over the body. Only a
  response header, the exact ``<title>`` of a known interstitial, or a
  ``src=``/``action=`` naming a challenge provider counts. Measured on this
  project's own corpus, "cloudflare" and "captcha" appeared in 0 of 1,360
  stored items and "challenge" in 267: a detector that reads prose refuses
  real articles, and a control that cries wolf gets switched off.
"""

from __future__ import annotations

import calendar
import http.client
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

import feedparser


#: A current desktop-browser User-Agent. Sites that reject "Python-urllib"
#: accept this; callers may override per call.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

#: Transport seam: ``(url, headers, timeout_s) -> (status, headers, body)``.
OpenURL = Callable[
    [str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]
]


class FeedFetchError(RuntimeError):
    """The feed could not be fetched or yielded no parseable entries."""


class FeedBlockedError(FeedFetchError):
    """The feed was refused by bot detection, not missing or malformed.

    A subclass so existing handlers keep working; a distinct type so the
    operator is told to fetch by hand rather than sent to debug the parser.
    """


def _decompressed(headers, raw: bytes) -> bytes:
    """Undo `Content-Encoding`. `urllib` does not, and `requests` does.

    Left undone, `.decode()` turns a gzip body into replacement characters
    rather than raising, so the failure surfaces far downstream as nonsense
    content instead of an error. Nothing here sends `Accept-Encoding`, so a
    compressed reply is not expected — but a proxy or a server that
    compresses anyway must not become garbage in the output.

    Raises rather than returning the compressed bytes when the body does not
    match what the header claims: returning them is exactly the silent-garbage
    path this exists to remove, and the caller reports a failed source.
    """
    encoding = (headers.get("Content-Encoding") or "").strip().lower()
    if encoding in ("", "identity"):
        return raw
    if encoding == "gzip":
        import gzip

        return gzip.decompress(raw)
    if encoding == "deflate":
        import zlib

        try:
            return zlib.decompress(raw)
        except zlib.error:  # raw deflate, no zlib wrapper
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    raise ValueError(f"unsupported Content-Encoding {encoding!r}")


_BLOCK_TITLES = frozenset({
    "just a moment...",
    "just a moment",
    "attention required! | cloudflare",
    "access denied",
    "access to this page has been denied",
    "are you a robot?",
    "are you a human?",
    "please verify you are a human",
    "checking your browser before accessing",
    "one more step",
    "security check",
    "verifying you are human",
})

_CHALLENGE_SRC = re.compile(
    rb"""(?:src|action)\s*=\s*["']?[^"'>\s]*"""
    rb"""(challenges\.cloudflare\.com"""
    rb"""|www\.google\.com/recaptcha"""
    rb"""|hcaptcha\.com"""
    rb"""|js\.hcaptcha\.com"""
    rb"""|geo\.captcha-delivery\.com)""",
    re.IGNORECASE,
)

_TITLE_TAG = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def detect_block(status: int, headers: Mapping[str, str], body: bytes) -> str | None:
    """Name the bot wall, or None if this looks like a real page.

    STRUCTURAL SIGNALS ONLY — never a substring search over the body. That is
    the obvious implementation and it is wrong for any caller that ingests
    text about the web: measured against this project's own corpus, "captcha"
    and "cloudflare" appeared in 0 of 1,360 stored items but "challenge"
    appeared in 267. A detector that reads prose refuses real articles, and a
    control that cries wolf gets turned off.

    So: a response header the origin cannot fake through content, the exact
    `<title>` of a known interstitial, or a script/form pointing at a
    challenge provider. An article *about* Cloudflare has the word in its
    prose, not in a `src=` attribute.
    """
    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    if "cf-mitigated" in lower:
        return "Cloudflare challenge (cf-mitigated header)"
    server = lower.get("server", "").lower()
    if status in (403, 429, 503) and "cloudflare" in server:
        return f"Cloudflare block (HTTP {status} from a Cloudflare edge)"

    match = _TITLE_TAG.search(body or b"")
    if match:
        title = " ".join(
            match.group(1).decode("utf-8", "replace").strip().lower().split()
        )
        if title in _BLOCK_TITLES:
            return f"interstitial page titled {title!r}"

    provider = _CHALLENGE_SRC.search(body or b"")
    if provider:
        return f"challenge widget from {provider.group(1).decode()}"

    return None


def _blocked_message(url: str, reason: str) -> str:
    """One message, because the operator's next step is always the same."""
    return (
        f"{url} is behind a bot wall: {reason}. Not retried, and no header "
        "rotation attempted — neither defeats real bot detection, both waste "
        "time and risk the IP. Fetch the page in a browser and supply the "
        "text locally if it is needed."
    )


def fetch_feed(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_s: float = 30.0,
    open_url: OpenURL | None = None,
) -> dict[str, Any]:
    """Fetch ``url`` conditionally and return status, validators, entries.

    Raises ``FeedFetchError`` on a transport failure, on any status other
    than 200 or 304, and on a 200 whose body yields no entries.
    """
    headers: dict[str, str] = {
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/atom+xml, "
        "application/xml;q=0.9, */*;q=0.8",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    transport = open_url or _urllib_open
    try:
        status, response_headers, body = transport(url, headers, timeout_s)
    except (OSError, http.client.HTTPException) as exc:
        raise FeedFetchError(f"fetch failed for {url}: {exc}") from exc

    # Before anything else: a wall answers 200 with HTML as readily as 403,
    # and "no parseable entries" sends the operator to debug the parser.
    reason = detect_block(status, response_headers, body)
    if reason is not None:
        raise FeedBlockedError(_blocked_message(url, reason))

    if status == 304:
        # Echo the validators back: a caller that writes this result to its
        # store must not erase the very validators that earned the 304.
        return {
            "status": 304,
            "etag": etag,
            "last_modified": last_modified,
            "entries": [],
        }
    if status != 200:
        raise FeedFetchError(f"{url} returned HTTP {status}")

    entries = parse_feed_bytes(body, base_url=url)
    if not entries:
        raise FeedFetchError(f"no parseable entries in the feed at {url}")

    return {
        "status": 200,
        "etag": _header(response_headers, "ETag"),
        "last_modified": _header(response_headers, "Last-Modified"),
        "entries": entries,
    }


def parse_feed_bytes(body: bytes, *, base_url: str = "") -> list[dict[str, Any]]:
    """Normalize already-fetched feed bytes into entry dicts. Pure.

    Returns ``[]`` for a body no dialect of RSS or Atom can be read out of;
    ``fetch_feed`` turns that into an error, callers with bytes of their own
    decide for themselves. Entries carrying neither a link nor an id are
    dropped: they can be neither fetched nor recognized again. A relative
    entry link is resolved against ``base_url`` when one is given and left
    as the feed wrote it when it is not.

    ``body`` must be bytes. A string is rejected with ``TypeError`` rather
    than passed on: ``feedparser.parse`` would treat it as a URL or path to
    open, which would make this function neither pure nor header-controlled.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError(
            "parse_feed_bytes takes the feed body as bytes, not "
            f"{type(body).__name__}; a string would be opened as a resource"
        )
    parsed = feedparser.parse(bytes(body))
    entries: list[dict[str, Any]] = []
    for entry in parsed.get("entries", []):
        link = entry.get("link") or ""
        entry_id = entry.get("id") or None
        if link:
            url = urljoin(base_url, link) if base_url else link
        elif entry_id:
            url = entry_id
        else:
            continue
        entries.append(
            {
                "id": entry_id,
                "url": url,
                "title": strip_html(entry.get("title") or ""),
                "summary": strip_html(entry.get("summary") or ""),
                "published_at": _published_at(entry),
            }
        )
    return entries


def strip_html(text: str) -> str:
    """Return ``text`` with tags removed, entities decoded, spaces collapsed.

    Block-level tags are a word break — ``<p>one</p><p>two</p>`` is
    ``"one two"``, not ``"onetwo"`` — while inline tags are not:
    ``con<b>cat</b>enate`` stays one word. The contents of ``<script>`` and
    ``<style>`` are dropped: they are source code, not the prose a summary
    is supposed to be.
    """
    extractor = _TextExtractor()
    extractor.feed(text)
    extractor.close()
    return " ".join("".join(extractor.chunks).split())


# --------------------------------------------------------------- internals ---

#: Tags whose text content is code, not prose, and is dropped entirely.
_RAW_TEXT_TAGS = frozenset(("script", "style"))

#: Tags whose boundaries are a word break even with no whitespace around them.
_BLOCK_TAGS = frozenset(
    "address article aside blockquote br div dl dd dt figure footer h1 h2 h3 "
    "h4 h5 h6 header hr li main nav ol p pre section table td th tr ul".split()
)


class _TextExtractor(HTMLParser):
    """Collect text, inserting a space at every block-level tag boundary."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._raw_depth = 0

    def handle_data(self, data: str) -> None:
        if self._raw_depth:
            return
        self.chunks.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _RAW_TEXT_TAGS:
            self._raw_depth += 1
        if tag in _BLOCK_TAGS:
            self.chunks.append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        # ``<script/>`` opens and closes in one token: no raw-text region.
        if tag in _BLOCK_TAGS:
            self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _RAW_TEXT_TAGS and self._raw_depth:
            self._raw_depth -= 1
            self.chunks.append(" ")
        if tag in _BLOCK_TAGS:
            self.chunks.append(" ")


def _urllib_open(
    url: str, headers: Mapping[str, str], timeout_s: float
) -> tuple[int, Mapping[str, str], bytes]:
    """Default transport. The only place a ``urllib`` object is constructed.

    Refuses any scheme but http and https: ``urllib``'s default opener also
    serves ``file://`` and ``ftp://``, so a URL that arrived from a config
    file could otherwise read local disk. A caller that wants another
    transport passes ``open_url``.
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise FeedFetchError(
            f"refusing to fetch {url}: scheme {scheme or '(none)'} is not http or https"
        )
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return (
                response.status,
                dict(response.headers),
                _decompressed(response.headers, response.read()),
            )
    except urllib.error.HTTPError as exc:
        # 304 and 4xx/5xx arrive here; the caller decides what they mean.
        body = _decompressed(exc.headers or {}, exc.read())
        exc.close()
        return exc.code, dict(exc.headers), body


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive lookup — header case is the server's choice, not ours."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _published_at(entry: Mapping[str, Any]) -> str | None:
    """ISO 8601 UTC from the entry's published date, else its updated date."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            moment = datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
            return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None
