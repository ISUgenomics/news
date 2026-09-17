# Seed boundary: feed-fetch

## Purpose
Fetch an RSS/Atom feed by URL with conditional GET and a browser-like User-Agent, returning its entries as plain dicts plus the validators to send next time.

## when_to_use (draft for FEATURE.toml)
You poll public RSS or Atom feeds on a schedule and need unchanged feeds to cost one cheap request, bot-hostile sites to answer, and entries handed back as plain dicts your own store can keep.

## Inputs
- url: str — the feed URL, fully resolved (secrets already substituted by the caller)
- etag: str | None — validator from the previous successful fetch, or None on first fetch
- last_modified: str | None — HTTP-date string from the previous fetch, or None
- user_agent: str — defaults to a browser-like DEFAULT_USER_AGENT constant; caller may override
- timeout_s: float — socket timeout, default 30
- open_url: Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]] | None — optional transport override; default uses urllib. Exists so a caller can route through a Playwright fetcher later without changing this module

## Outputs
- dict {"status": int, "etag": str | None, "last_modified": str | None, "entries": list[dict]} — status is 200 (or 304 with entries == [] and the input validators echoed back)
- each entry dict: {"id": str | None, "url": str, "title": str, "summary": str, "published_at": str | None} — summary is HTML-stripped plain text; published_at is ISO 8601 UTC or None; id is the feed GUID or None; url falls back to id when link is absent
- raises FeedFetchError (subclass of RuntimeError) on network failure, HTTP status other than 200/304, or a body feedparser cannot parse into any entries; the message names the URL and the cause

## Dependencies
- **feedparser** — One parser for RSS 0.9x/1.0/2.0 and Atom with tolerant handling of malformed XML and normalized date parsing (published_parsed). Hand-rolling this with ElementTree is the classic trap: every campus CMS emits a slightly different dialect. Used only on bytes already fetched; its own HTTP path is not used, so UA, validators, and timeouts stay under this module's control and testable against stub_http.
- **urllib.request / html.parser / email.utils / calendar (stdlib)** — HTTP with explicit headers, HTML tag stripping for summaries, and date conversion. No requests/httpx: nothing here needs sessions, pooling, or retries.

## Must NOT know about
- config.yaml, sources.yaml, or any profile — it receives one URL and the validators, nothing named
- the Item dataclass or brief.models — it returns plain dicts; the adapter maps them
- SQLite or where validators are persisted — etag/last_modified go in and come out as strings
- logging configuration — it raises or returns; the caller logs
- hardcoded feed URLs, tokens, or ${CD_TOKEN} substitution
- the profile schema, relevance keywords, or which profiles use the feed
- the 200-character expansion threshold and trafilatura — that is the page-main-text sibling and the adapter's policy
- the 8 kB body cap and content_hash — storage concerns owned by db.py

## Public API
```python
DEFAULT_USER_AGENT: str  # a current desktop-browser UA string, one constant, overridable per call
class FeedFetchError(RuntimeError): ...
def fetch_feed(url: str, *, etag: str | None = None, last_modified: str | None = None, user_agent: str = DEFAULT_USER_AGENT, timeout_s: float = 30.0, open_url: Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]] | None = None) -> dict[str, Any]
def parse_feed_bytes(body: bytes, *, base_url: str = "") -> list[dict[str, Any]]  # pure; the entry normalization on already-fetched bytes, exposed so fixture tests need no socket
def strip_html(text: str) -> str  # stdlib html.parser; collapses whitespace, decodes entities
```

## Test harness
stub_http

## Approved module docstring (write this verbatim into the module)
```
Fetch one RSS or Atom feed and hand back its entries as plain dicts.

Situation: a scheduled poller hits the same public feeds every day. Most days
nothing changed, and some sites (inside.iastate.edu among them) answer a
default Python User-Agent with "Request Rejected". This module does the one
HTTP request correctly: it sends a browser-like User-Agent, sends
If-None-Match / If-Modified-Since when the caller supplies the previous
ETag / Last-Modified, and returns those validators so the caller can store
them wherever it likes.

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
```

## Prior art
verdict: seed

No match at the feature or part layer. All four queries returned only the PDF, LLM-provider, and desktop-app features (pdf-garbage-text-sweeps, localhost-web-guard, cli-llm-providers, secret-redaction, etc.) on incidental token overlap (page, feed, request, changed); none fetches HTTP, parses feeds, or handles cache validators. The parts layer surfaced only PDF helpers and the ollama-local-llm test conftest's requests() property. The 45 features under /Users/andrewseverin/AI/codeLibrary/features/python/ contain no HTTP client, feed, or scraping feature. The one real hit is the harness: stub_http is exactly the fixture this seed's tests need (route the feed body, assert on User-Agent and If-None-Match headers in stub_http.requests, return 304 from a callable handler).

## Granularity note
The draft purpose joins two jobs with "and": (1) fetch and parse a feed conditionally, (2) expand thin entries by fetching the linked HTML page and extracting main text. The second is not a fallback path on the first: it is a different HTTP path (HTML, no validators), a different dependency (trafilatura vs feedparser), and a policy decision (the 200-character threshold) that belongs to the app. Split: `feed-fetch` (this boundary; feedparser only) and a sibling seed `page-main-text` (fetch a URL with the same UA, extract main text with trafilatura, cap length). The app's `sources/rss.py` composes them: `for e in result["entries"]: body = e["summary"] if len(e["summary"]) >= 200 else page_main_text(e["url"])`. Each seed then has one dependency, one harness, and one reason to change.

## Critic verdict: split
The drafted boundary is already the post-split half (feedparser only) and is one job. Accept the split; the other half, page-main-text (trafilatura), is not in the list yet and must be seeded alongside or sources/rss.py will inline it.

### Boundary fixes to apply at write time
- open_url seam is typed on urllib.request.Request, which forces a future Playwright/requests transport to build a urllib object; type it as Callable[[str, Mapping[str,str], float], tuple[int, Mapping[str,str], bytes]] (url, headers, timeout) and build the Request inside the default only
- docstring names inside.iastate.edu; replace with 'some campus CMSs' (domain leak into a module that will graduate)
- published_at: fall back to updated_parsed when published_parsed is absent (Atom feeds often carry only <updated>); state that in the contract
- 304 path: return the input validators, not None, so the caller's store is not overwritten with nulls (draft says so; make it a test)
- entry dict is missing content the adapter needs for raw_json (spec: raw_json 'for APIs' so acceptable); confirm raw is intentionally omitted or add raw: dict of the feedparser entry
