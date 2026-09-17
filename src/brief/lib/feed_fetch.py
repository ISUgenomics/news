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
"""

from __future__ import annotations

import calendar
import http.client
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
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        # 304 and 4xx/5xx arrive here; the caller decides what they mean.
        body = exc.read()
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
