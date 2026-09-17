# Seed boundary: page-main-text

Added by the design critic as the second half of the `feed-fetch` split. `feed-fetch` owns
feedparser and cache validators; this owns HTML and trafilatura. Each has one dependency,
one harness, and one reason to change.

## Purpose
Fetch one HTML page with a browser-like User-Agent and return its main article text, capped.

## when_to_use (draft for FEATURE.toml)
You have a URL whose page body you need as plain text — a feed entry too short to be useful, a
link you want to read rather than render — and the site answers a default Python User-Agent with
a rejection page.

## Inputs
- `url: str` — the page to fetch, fully resolved
- `user_agent: str = DEFAULT_USER_AGENT` — same browser-like default as `feed-fetch`; keep the two constants textually identical and say so in both docstrings
- `timeout_s: float = 30.0`
- `max_chars: int | None = None` — cap applied to the extracted text; `None` means no cap. The caller owns the number (this app uses 8 kB at the storage layer, not here)
- `open_url: Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]] | None = None` — transport seam, same signature as `feed-fetch`'s (url, headers, timeout) so one fake serves both seeds' tests

## Outputs
- `dict {"text": str, "title": str | None, "status": int, "truncated": bool}` — `text` is main article text with boilerplate removed, whitespace collapsed; `truncated` says whether `max_chars` cut it
- raises `PageFetchError` (subclass of `RuntimeError`) on network failure, any status other than 200, or an extraction that yields no text; the message names the URL and the cause

## Dependencies
- **trafilatura** — boilerplate-stripped main-text extraction. Imported lazily inside the extract function so a caller that only fetches feeds never loads it (it pulls lxml, htmldate, courlan, justext — the largest install in the project). Its own `fetch_url` is deliberately NOT used: headers, timeout, and transport stay under this module's control and remain testable against a stub server.
- stdlib `urllib.request`, `typing`

## Must NOT know about
- the 200-character threshold that decides when a caller wants this — that policy lives in the app's rss adapter
- `config.yaml`, `sources.yaml`, profiles, or any source name
- the `Item` dataclass, SQLite, `content_hash`, or the 8 kB storage cap
- logging configuration — it raises or returns
- feedparser, ETag/Last-Modified, or anything about feeds

## Public API
```python
DEFAULT_USER_AGENT: str  # identical string to feed-fetch's constant, by intent
class PageFetchError(RuntimeError): ...
def page_main_text(url: str, *, user_agent: str = DEFAULT_USER_AGENT, timeout_s: float = 30.0, max_chars: int | None = None, open_url: Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]] | None = None) -> dict[str, Any]
def extract_main_text(html: bytes, *, url: str = "", max_chars: int | None = None) -> dict[str, Any]  # pure; extraction on bytes already fetched, so fixture tests need no socket
```

## Test harness
stub_http

## Approved module docstring (write this verbatim into the module)
```
Fetch one web page and return its main article text.

Situation: you hold a URL — a feed entry whose summary is a single line, a
link worth reading rather than rendering — and you want the article body as
plain text. Two things make that harder than one GET: many sites answer a
default Python User-Agent with a rejection page, and the raw HTML is mostly
navigation, cookie banners, and footers.

Contract: ``page_main_text(url)`` returns ``{"text", "title", "status",
"truncated"}``. The text is main content with boilerplate removed and
whitespace collapsed; ``max_chars`` caps it and sets ``truncated``. Any
network error, a status other than 200, or an extraction that finds no text
raises ``PageFetchError`` naming the URL and the cause — an empty string is
never returned as if it were an answer.

Deliberately not here: deciding *when* a page is worth fetching (a summary
length threshold is the caller's policy), feed parsing and cache validators
(a sibling concern), retries, rate limiting, persistence, and hashing.

Dependencies: ``trafilatura`` for extraction, imported lazily inside the
extract path so callers that never extract do not pay for lxml and its
friends. Its own ``fetch_url`` is bypassed on purpose: the User-Agent,
timeout, and transport stay here, where a stub HTTP server can exercise
them. The ``open_url`` seam takes ``(url, headers, timeout)`` — the same
shape a feed fetcher uses — so one fake transport serves both.
```

## Prior art
verdict: seed

Searched with the situation wording used for `feed-fetch` ("a site rejects plain HTTP requests
and needs a browser-like user agent", "feed entry summary is too short so fetch the article page
and extract its main text", "download a web page with retries and caching headers"). No feature
or part in the library fetches HTTP or extracts article text; the 45 Python features are PDF,
LLM-provider, config, and desktop concerns. The only hit is the harness,
`templates/python-feature/tests/harness/stub_http.py`, which is what this seed's tests need.

## Boundary fixes to apply at write time
- Keep `DEFAULT_USER_AGENT` textually identical to `feed-fetch`'s and cross-reference it in both docstrings, so a future UA bump is one obvious pair of edits rather than a silent divergence.
- `open_url` takes `(url, headers, timeout)`, never a `urllib.request.Request`, so a future Playwright transport does not have to build a urllib object.
- Pin the empty-extraction path with a test: a page that is all navigation must raise, not return `""`.
- Do not name a real site in the docstring.
