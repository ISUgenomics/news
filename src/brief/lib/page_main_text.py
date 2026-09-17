"""Fetch one web page and return its main article text.

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

Details the contract above leaves open, pinned here because tests pin them:

* ``DEFAULT_USER_AGENT`` is textually identical to the constant in the feed
  fetcher seed (``feed_fetch.DEFAULT_USER_AGENT``) by intent, not by
  accident. Bump one and bump the other; the two modules never import each
  other.
* "Whitespace collapsed" means: runs of spaces and tabs become one space,
  every line is stripped, runs of blank lines become one newline, and the
  whole string is stripped. Paragraph boundaries survive as single
  newlines; nothing else does.
* ``max_chars`` is a hard character cut, not a word-boundary cut, so the
  same input always yields the same bytes. ``max_chars`` below 1 is a
  caller bug and raises ``ValueError``, not ``PageFetchError``.
* Only ``http`` and ``https`` URLs are fetched; anything else raises
  ``PageFetchError`` before a transport is called, so a ``file://`` string
  arriving from a feed cannot read the disk.
* ``PageFetchError`` carries ``.url`` and ``.status`` (``None`` when the
  failure happened before a response) for callers that branch on them. It
  subclasses ``RuntimeError``, so a caller that catches ``RuntimeError``
  keeps working.
* ``extract_main_text`` reports ``"status": 200``. Extraction only ever
  runs on a body a server answered 200 with, and the pure path returns the
  same four-key shape rather than inventing a second one.
* The title is the one best-effort field: if metadata extraction fails or
  finds nothing, ``title`` is ``None`` and the text is still returned. A
  missing title never turns a successful extraction into an error.
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

import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

#: Keep textually identical to ``feed_fetch.DEFAULT_USER_AGENT``.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

#: Sent alongside the User-Agent so servers that content-negotiate return HTML.
DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

OpenUrl = Callable[
    [str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]
]


class PageFetchError(RuntimeError):
    """A page could not be fetched, or held no extractable text."""

    def __init__(self, message: str, *, url: str = "", status: int | None = None):
        super().__init__(message)
        self.url = url
        self.status = status


class PageBlockedError(PageFetchError):
    """The fetch was refused by bot detection, not by the page being absent.

    A subclass so existing handlers keep working, and a distinct type so a
    caller that wants to report "fetch this one by hand" can tell it apart
    from a 404 or a transport failure.
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


def page_main_text(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_s: float = 30.0,
    max_chars: int | None = None,
    open_url: OpenUrl | None = None,
) -> dict[str, Any]:
    """Fetch ``url`` and return ``{"text", "title", "status", "truncated"}``.

    Raises ``PageFetchError`` on an unsupported scheme, a transport failure,
    any status other than 200, or a page with no extractable main text.
    Raises ``ValueError`` if ``max_chars`` is below 1.
    """
    _check_max_chars(max_chars)

    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise PageFetchError(
            f"refusing to fetch {url!r}: unsupported URL scheme {scheme or '(none)'!r}",
            url=url,
        )

    headers = {"User-Agent": user_agent, "Accept": DEFAULT_ACCEPT}
    transport = open_url if open_url is not None else _open_url_urllib

    try:
        status, response_headers, body = transport(url, headers, timeout_s)
    except PageFetchError:
        raise
    except Exception as exc:  # any transport, not just urllib's
        raise PageFetchError(
            f"fetching {url!r} failed: {exc.__class__.__name__}: {exc}", url=url
        ) from exc

    # Before the status check: a wall answers 403 as readily as 200, and
    # "returned HTTP 403" sends the operator to look for a broken URL.
    reason = detect_block(status, response_headers, body)
    if reason is not None:
        raise PageBlockedError(_blocked_message(url, reason), url=url, status=status)

    if status != 200:
        raise PageFetchError(
            f"fetching {url!r} returned HTTP {status}, not 200", url=url, status=status
        )

    return extract_main_text(body, url=url, max_chars=max_chars)


def extract_main_text(
    html: bytes,
    *,
    url: str = "",
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Extract main text from bytes already in hand. Pure; touches no socket.

    Returns the same ``{"text", "title", "status", "truncated"}`` shape as
    ``page_main_text``; ``status`` is always 200 (see module docstring).
    Raises ``PageFetchError`` when no main text can be extracted, and
    ``ValueError`` if ``max_chars`` is below 1.

    ``trafilatura`` is imported here, not at module import, so a caller that
    only ever fetches pays nothing for lxml and its friends.
    """
    _check_max_chars(max_chars)

    import trafilatura  # noqa: PLC0415  (lazy on purpose; see module docstring)

    payload = html if isinstance(html, bytes) else str(html).encode("utf-8")

    raw = None
    if payload.strip():
        raw = trafilatura.extract(
            payload,
            url=url or None,
            output_format="txt",
            favor_precision=True,
            include_comments=False,
            include_tables=True,
        )

    text = _collapse_whitespace(raw or "")
    if not text:
        where = f" of {url!r}" if url else ""
        raise PageFetchError(
            f"no main text could be extracted from the page{where}", url=url, status=200
        )

    title = _title_from(payload)

    truncated = max_chars is not None and len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    return {"text": text, "title": title, "status": 200, "truncated": truncated}


# ------------------------------------------------------------- internals ---


def _check_max_chars(max_chars: int | None) -> None:
    if max_chars is not None and max_chars < 1:
        raise ValueError(f"max_chars must be None or at least 1, got {max_chars!r}")


def _title_from(
    payload: bytes,
    extract_metadata: Callable[[bytes], Any] | None = None,
) -> str | None:
    """Best-effort page title: ``None`` when absent, blank, or unobtainable.

    The title is the only field allowed to fail softly, so a metadata
    extractor that raises must not lose a body that extracted fine. The
    ``extract_metadata`` seam is here so a test can pin that decision with a
    real callable that raises, rather than leaving the branch unexercised.
    """
    if extract_metadata is None:
        from trafilatura.metadata import (  # noqa: PLC0415  (lazy on purpose)
            extract_metadata as _impl,
        )

        extract_metadata = _impl

    try:
        metadata = extract_metadata(payload)
    except Exception:
        return None
    return (getattr(metadata, "title", None) or "").strip() or None


def _collapse_whitespace(text: str) -> str:
    """Spaces and tabs to one space, blank-line runs to one newline, stripped."""
    lines = [_HORIZONTAL_WS.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _open_url_urllib(
    url: str, headers: Mapping[str, str], timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    """Default transport: one GET, no redirect suppression, no retries.

    An HTTP error status comes back as a value rather than an exception, so
    the caller reports the status it got instead of a urllib type name.
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                int(response.status),
                dict(response.headers),
                _decompressed(response.headers, response.read()),
            )
    except urllib.error.HTTPError as exc:
        with exc:
            return (
                int(exc.code),
                dict(exc.headers or {}),
                _decompressed(exc.headers or {}, exc.read()),
            )


_HORIZONTAL_WS = re.compile(r"[^\S\n]+")
