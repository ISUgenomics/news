"""crates_io_search — what someone just published to the Rust registry.

A package registry is where a tool exists first. A paper describing it lands
months later, if ever, so a digest that watches only the literature reports
software long after the people who would use it have moved on. Measured
before this module was proposed: 1,031 crates match "bioinformatics", and
``sort=new`` surfaces several created in the past week that appear in no
other source this project has.

Two decisions that are judgement rather than defaults:

  - **``published_at`` follows the sort.** With ``sort="new"`` it is the
    creation date; with any other sort it is the update date. A caller asking
    "what is new" and a caller asking "what moved" want different dates out
    of the same record, and picking one silently makes the other wrong. Both
    raw dates are kept as well, so a caller can disagree.
  - **An unknown sort is refused.** crates.io accepts one and quietly falls
    back to relevance, which returns old popular crates — indistinguishable
    from a genuinely quiet week. A typo should cost an exception, not a
    season of wrong briefs.

``user_agent`` is required because the registry's crawler policy asks callers
to identify themselves and give a contact. Optional politeness lapses.

Contract:
  - Every returned dict carries the same keys, ``""`` or ``[]`` for anything
    the record lacked.
  - ``raw`` is the untouched record.
  - Ordering is whatever the registry returned for the requested sort; this
    module does not re-rank.
  - ``max_pages`` bounds the walk. Reaching it returns what was collected.

Deliberately not here: relevance filtering, dedup against other sources,
which ecosystem or topic matters, retry or backoff, persistence, logging.

Dependencies: stdlib only.

Transport, shared with the other fetching seeds in this family and duplicated
rather than imported because seeds graduate individually:
* ``Content-Encoding`` is undone before decoding; ``urllib`` does not.
* A bot wall is named rather than debugged, detected structurally, never by
  searching the body for words like "captcha".
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
    "CratesIoError",
    "CratesIoBlockedError",
    "normalize_crate",
    "search_crates",
]

DEFAULT_BASE_URL = "https://crates.io/api/v1"
MAX_PER_PAGE = 100

#: crates.io accepts an unknown sort and falls back to relevance. Pinned so a
#: typo raises instead of quietly returning old popular crates.
SORTS = frozenset({"new", "recent-update", "recent-downloads", "downloads", "relevance"})

OpenUrl = Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]


class CratesIoError(RuntimeError):
    """The query could not be completed."""


class CratesIoBlockedError(CratesIoError):
    """Refused by bot detection, not by the resource being absent."""


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
        raise CratesIoError(f"refusing to fetch {url!r}: scheme {scheme or '(none)'!r}")
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


def _get_json(
    url: str, *, user_agent: str, timeout_s: float, open_url: OpenUrl | None
) -> dict[str, Any]:
    if not user_agent or "@" not in user_agent:
        raise ValueError(
            "user_agent must identify the caller and carry a contact address: "
            "the crates.io crawler policy asks for one"
        )
    transport = open_url if open_url is not None else _open_url_urllib
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    try:
        status, response_headers, body = transport(url, headers, timeout_s)
    except CratesIoError:
        raise
    except Exception as exc:
        raise CratesIoError(f"fetching {url!r} failed: {type(exc).__name__}: {exc}") from exc

    reason = _detect_block(status, response_headers, body)
    if reason is not None:
        raise CratesIoBlockedError(
            f"{url} is behind a bot wall: {reason}. Not retried, and no header "
            "rotation attempted — neither defeats real bot detection. Fetch it "
            "by hand if it is needed."
        )
    if status != 200:
        raise CratesIoError(f"crates.io returned HTTP {status} for {url}")
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise CratesIoError(f"crates.io returned unparseable JSON for {url}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CratesIoError(f"crates.io returned {type(parsed).__name__}, not an object")
    return parsed


# ------------------------------------------------------------------ shaping ---


def normalize_crate(raw: Mapping[str, Any], *, sort: str = "new") -> dict[str, Any]:
    """One crate as a flat dict. Every key always present.

    ``published_at`` is the creation date under ``sort="new"`` and the update
    date otherwise, because that is the date the caller's window is about.
    """
    name = str(raw.get("name") or raw.get("id") or "")
    created = str(raw.get("created_at") or "")
    updated = str(raw.get("updated_at") or "")
    return {
        "name": name,
        "description": str(raw.get("description") or ""),
        "url": f"https://crates.io/crates/{urllib.parse.quote(name)}" if name else "",
        "repository": str(raw.get("repository") or ""),
        "homepage": str(raw.get("homepage") or ""),
        "documentation": str(raw.get("documentation") or ""),
        "version": str(raw.get("newest_version") or raw.get("max_stable_version") or ""),
        "downloads": int(raw.get("downloads") or 0),
        "recent_downloads": int(raw.get("recent_downloads") or 0),
        "keywords": [str(k) for k in (raw.get("keywords") or [])],
        "categories": [str(c) for c in (raw.get("categories") or [])],
        "created_at": created,
        "updated_at": updated,
        "published_at": (created if sort == "new" else updated) or created or updated,
        "raw": dict(raw),
    }


def search_crates(
    query: str,
    *,
    sort: str = "new",
    per_page: int = MAX_PER_PAGE,
    max_pages: int = 5,
    user_agent: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = 30.0,
    open_url: OpenUrl | None = None,
) -> list[dict[str, Any]]:
    """Crates matching ``query`` in the registry's order for ``sort``.

    Pages by following ``meta.next_page``, which the registry hands back as a
    ready-made query string, rather than by incrementing an offset — the
    registry deprecated offset paging past the first pages.
    """
    if sort not in SORTS:
        raise ValueError(
            f"sort must be one of {sorted(SORTS)}, got {sort!r}. crates.io "
            "accepts an unknown value and silently falls back to relevance, "
            "which reads as a quiet week."
        )
    if per_page < 1 or per_page > MAX_PER_PAGE:
        raise ValueError(f"per_page must be 1..{MAX_PER_PAGE}, got {per_page}")
    if max_pages < 1:
        raise ValueError(f"max_pages must be >= 1, got {max_pages}")

    crates: list[dict[str, Any]] = []
    seen: set[str] = set()
    target = f"{base_url}/crates?" + urllib.parse.urlencode(
        {"q": query, "sort": sort, "per_page": per_page}
    )
    for _page in range(max_pages):
        payload = _get_json(
            target, user_agent=user_agent, timeout_s=timeout_s, open_url=open_url
        )
        batch = payload.get("crates") or []
        for raw in batch:
            if not isinstance(raw, Mapping):
                continue
            crate = normalize_crate(raw, sort=sort)
            if crate["name"] and crate["name"] in seen:
                continue
            seen.add(crate["name"])
            crates.append(crate)

        next_page = (payload.get("meta") or {}).get("next_page")
        if not batch or not next_page:
            break
        # The registry returns a full query string, leading '?' included.
        target = f"{base_url}/crates{next_page}"

    return crates
