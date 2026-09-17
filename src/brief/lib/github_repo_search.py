"""github_repo_search — software as it appears, not as it is later written up.

A tool exists on a code host before it reaches a package registry, and long
before a paper describes it. Measured before this module was proposed: for
one language and one topic, 125 repositories pushed in a 30-day window and
10 created in it, two of them the same day — none appearing in crates.io,
OpenAlex or bioRxiv.

Two things about this API go wrong quietly, and both are why this module
exists rather than three lines of urllib at a call site:

  - **``incomplete_results: true``.** GitHub answers 200 with a partial
    result set and a flag most callers never read. Treated as success it
    reports a quiet week that was not quiet. Here it raises, carrying what
    did arrive, so a caller can decide rather than not know.
  - **A spent rate limit answers 403, not 429.** Indistinguishable from a
    permissions failure unless the headers are read. This module reads them
    and raises a distinct error naming the reset time.

Nothing here retries or sleeps until the window reopens: a seed that sleeps
turns a fast failure into a hung job.

The query is passed through verbatim. GitHub's qualifier grammar is large and
moves; assembling it from keyword arguments would be a second grammar to
maintain and to get subtly wrong. The caller writes
``language:rust topic:bioinformatics created:>2026-08-18`` because that is the
language the API actually speaks.

Contract:
  - Every returned dict carries the same keys, ``""``/``[]``/``False`` for
    anything the record lacked.
  - ``fork`` and ``archived`` are RETURNED, never filtered. A digest usually
    wants neither, a survey of activity wants both, and that is the caller's
    judgement.
  - ``published_at`` is ``created_at``; the query decides whether it is
    asking about creation or activity, and ``pushed_at`` is kept alongside.
  - ``max_pages`` bounds the walk. GitHub caps search at 1,000 results
    regardless.

Deliberately not here: how a token is stored, which language or topic
matters, relevance filtering, dedup, retry, persistence, logging.

Dependencies: stdlib only.

Transport, shared with the other fetching seeds and duplicated rather than
imported because seeds graduate individually: ``Content-Encoding`` is undone
before decoding, and a bot wall is detected structurally and named.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

__all__ = [
    "DEFAULT_BASE_URL",
    "SORTS",
    "GitHubSearchError",
    "GitHubRateLimitError",
    "GitHubIncompleteResultsError",
    "GitHubBlockedError",
    "normalize_repo",
    "search_repositories",
]

DEFAULT_BASE_URL = "https://api.github.com"
MAX_PER_PAGE = 100

SORTS = frozenset({"updated", "stars", "forks", "help-wanted-issues"})

OpenUrl = Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]


class GitHubSearchError(RuntimeError):
    """The search could not be completed."""


class GitHubRateLimitError(GitHubSearchError):
    """The search rate limit is spent. GitHub reports this as 403."""

    def __init__(self, message: str, *, reset_at: str = "") -> None:
        super().__init__(message)
        self.reset_at = reset_at


class GitHubIncompleteResultsError(GitHubSearchError):
    """GitHub truncated the search and said so in a field, not a status."""

    def __init__(self, message: str, *, results: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.results = results or []


class GitHubBlockedError(GitHubSearchError):
    """Refused by bot detection."""


# --------------------------------------------------------------- transport ---


def _decompressed(headers, raw: bytes) -> bytes:
    """Undo `Content-Encoding`. `urllib` does not, and `requests` does."""
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
    "are you a robot?",
    "please verify you are a human",
    "checking your browser before accessing",
    "security check",
})

_CHALLENGE_SRC = re.compile(
    rb"""(?:src|action)\s*=\s*["']?[^"'>\s]*"""
    rb"""(challenges\.cloudflare\.com|www\.google\.com/recaptcha|hcaptcha\.com)""",
    re.IGNORECASE,
)

_TITLE_TAG = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _detect_block(status: int, headers: Mapping[str, str], body: bytes) -> str | None:
    """Structural signals only; never a substring search over the body."""
    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if "cf-mitigated" in lower:
        return "Cloudflare challenge (cf-mitigated header)"
    if status in (403, 429, 503) and "cloudflare" in lower.get("server", "").lower():
        return f"Cloudflare block (HTTP {status} from a Cloudflare edge)"
    match = _TITLE_TAG.search(body or b"")
    if match:
        title = " ".join(match.group(1).decode("utf-8", "replace").strip().lower().split())
        if title in _BLOCK_TITLES:
            return f"interstitial page titled {title!r}"
    found = _CHALLENGE_SRC.search(body or b"")
    if found:
        return f"challenge widget from {found.group(1).decode()}"
    return None


def _open_url_urllib(
    url: str, headers: Mapping[str, str], timeout_s: float
) -> tuple[int, Mapping[str, str], bytes]:
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise GitHubSearchError(f"refusing to fetch {url!r}: scheme {scheme or '(none)'!r}")
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
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


def _reset_at(headers: Mapping[str, str]) -> str:
    """The rate-limit reset, as an ISO string. Empty if absent or unparseable."""
    import datetime

    raw = {str(k).lower(): v for k, v in (headers or {}).items()}.get("x-ratelimit-reset")
    try:
        return (
            datetime.datetime.fromtimestamp(int(raw), tz=datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError):
        return ""


def _get_json(
    url: str, *, token: str | None, user_agent: str, timeout_s: float,
    open_url: OpenUrl | None,
) -> dict[str, Any]:
    if not user_agent:
        raise ValueError("user_agent is required: GitHub rejects requests without one")
    transport = open_url if open_url is not None else _open_url_urllib
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        status, response_headers, body = transport(url, headers, timeout_s)
    except GitHubSearchError:
        raise
    except Exception as exc:
        raise GitHubSearchError(
            f"fetching {url!r} failed: {type(exc).__name__}: {exc}"
        ) from exc

    reason = _detect_block(status, response_headers, body)
    if reason is not None:
        raise GitHubBlockedError(f"{url} is behind a bot wall: {reason}. Not retried.")

    lower = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
    remaining = lower.get("x-ratelimit-remaining")
    # GitHub reports a spent SEARCH limit as 403, which is otherwise
    # indistinguishable from a permissions problem. The header is the tell.
    if status == 403 and remaining == "0":
        reset = _reset_at(response_headers)
        raise GitHubRateLimitError(
            "GitHub search rate limit is spent"
            + (f"; it resets at {reset}" if reset else "")
            + ". Unauthenticated search allows 10 requests/minute and "
            "authenticated 30. Not retried and not slept through.",
            reset_at=reset,
        )
    if status != 200:
        raise GitHubSearchError(f"GitHub returned HTTP {status} for {url}")

    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise GitHubSearchError(f"GitHub returned unparseable JSON for {url}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GitHubSearchError(f"GitHub returned {type(parsed).__name__}, not an object")
    return parsed


# ------------------------------------------------------------------ shaping ---


def normalize_repo(raw: Mapping[str, Any]) -> dict[str, Any]:
    """One repository as a flat dict. Every key always present."""
    license_info = raw.get("license") or {}
    return {
        "full_name": str(raw.get("full_name") or ""),
        "owner": str((raw.get("owner") or {}).get("login") or ""),
        "url": str(raw.get("html_url") or ""),
        "description": str(raw.get("description") or ""),
        "homepage": str(raw.get("homepage") or ""),
        "language": str(raw.get("language") or ""),
        "topics": [str(t) for t in (raw.get("topics") or [])],
        "stars": int(raw.get("stargazers_count") or 0),
        "forks": int(raw.get("forks_count") or 0),
        "open_issues": int(raw.get("open_issues_count") or 0),
        "license": str(license_info.get("spdx_id") or "") if license_info else "",
        "archived": bool(raw.get("archived")),
        "fork": bool(raw.get("fork")),
        "created_at": str(raw.get("created_at") or ""),
        "pushed_at": str(raw.get("pushed_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "published_at": str(raw.get("created_at") or ""),
        "raw": dict(raw),
    }


def search_repositories(
    query: str,
    *,
    sort: str | None = None,
    order: str = "desc",
    per_page: int = MAX_PER_PAGE,
    max_pages: int = 3,
    token: str | None = None,
    user_agent: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = 30.0,
    open_url: OpenUrl | None = None,
) -> list[dict[str, Any]]:
    """Repositories matching ``query``, in GitHub's order.

    ``query`` is GitHub's own search syntax, passed through unchanged.
    """
    if not (query or "").strip():
        raise ValueError("query is required")
    if sort is not None and sort not in SORTS:
        raise ValueError(f"sort must be None or one of {sorted(SORTS)}, got {sort!r}")
    if order not in ("asc", "desc"):
        raise ValueError(f"order must be 'asc' or 'desc', got {order!r}")
    if per_page < 1 or per_page > MAX_PER_PAGE:
        raise ValueError(f"per_page must be 1..{MAX_PER_PAGE}, got {per_page}")
    if max_pages < 1:
        raise ValueError(f"max_pages must be >= 1, got {max_pages}")

    repos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        params: dict[str, str] = {
            "q": query, "order": order, "per_page": str(per_page), "page": str(page)
        }
        if sort:
            params["sort"] = sort
        payload = _get_json(
            f"{base_url}/search/repositories?{urllib.parse.urlencode(params)}",
            token=token, user_agent=user_agent, timeout_s=timeout_s, open_url=open_url,
        )

        items = payload.get("items") or []
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            repo = normalize_repo(raw)
            if repo["full_name"] and repo["full_name"] in seen:
                continue
            seen.add(repo["full_name"])
            repos.append(repo)

        if payload.get("incomplete_results"):
            raise GitHubIncompleteResultsError(
                "GitHub truncated this search and reported it in "
                "`incomplete_results`, not in the status. "
                f"{len(repos)} result(s) arrived out of "
                f"{payload.get('total_count', 'an unknown number')}. Narrow the "
                "query rather than trusting the count.",
                results=repos,
            )

        if len(items) < per_page:
            break

    return repos
