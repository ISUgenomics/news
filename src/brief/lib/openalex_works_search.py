"""openalex_works_search — an institution's recent works, from OpenAlex.

The question this answers is "what did this institution publish lately", and
the obvious way to ask it is wrong. An affiliation-string search matches the
text an author happened to type, and each literature database covers one
subject scope. OpenAlex filters on a *resolved institution id* and indexes
every discipline. Measured over one 30-day window at one university: PubMed
returned 99 works and 3 matching a topic filter, OpenAlex 338 and 21.

What this module does: builds one filtered query, walks the cursor until the
window is covered or ``max_pages`` is spent, and returns plain dicts with the
abstract already reconstructed. No app types, no database, no institution
baked in.

Three decisions that are judgement, not defaults:

  - **`lineage`, not `id`.** The filter is
    ``authorships.institutions.lineage``, so a work credited to a department,
    institute or hospital *within* the institution counts. Filtering on
    ``institutions.id`` silently drops them.
  - **A bare name is refused.** ``search_openalex_works`` takes an id.
    Resolving a name costs a request whose answer can change and whose
    near-matches are plausible — a search for a university commonly returns
    the university, its press, and an institute inside it, all as separate
    records. Picking the first silently is how a report ends up about a
    different organisation. ``resolve_institution`` is here so a human looks
    it up once and pins the id in config.
  - **`mailto` is required, not optional.** It is what OpenAlex asks callers
    for, and it buys the polite pool. Optional politeness is politeness that
    silently lapses.

Abstracts arrive as an inverted index (``{word: [positions]}``) and must be
rebuilt. That is the one genuinely non-obvious part of this API, so
``reconstruct_abstract`` is exported rather than hidden.

Contract:
  - Every returned dict carries the same keys, with ``""`` for anything the
    record lacked. A caller never has to test for a missing field, only for
    an empty one.
  - ``raw`` is the untouched record, so a caller can derive a field this
    module chose not to without refetching.
  - Ordering is whatever OpenAlex returned; this module does not rank.
  - ``max_pages`` is a bound, not a promise: reaching it returns what was
    collected. The caller compares ``len(results)`` against its own window if
    completeness matters.

Deliberately not here: relevance filtering, dedup against other sources,
ranking, body caps, retry or backoff, persistence, logging, and any notion of
which institution matters.

Dependencies: stdlib only.

Transport, shared with the other fetching seeds in this family and duplicated
rather than imported because seeds graduate individually:
* ``Content-Encoding`` is undone before decoding. ``urllib`` does not
  decompress and ``requests`` does, and ``.decode()`` on a gzip body yields
  replacement characters rather than raising.
* A bot wall is named rather than debugged, detected structurally — a
  response header, an exact interstitial ``<title>``, or a ``src=`` naming a
  challenge provider — never by searching the body for words like "captcha",
  which refuses real content.
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
    "OpenAlexError",
    "OpenAlexBlockedError",
    "normalize_institution_id",
    "reconstruct_abstract",
    "normalize_work",
    "resolve_institution",
    "search_openalex_works",
]

DEFAULT_BASE_URL = "https://api.openalex.org"
MAX_PER_PAGE = 200

OpenUrl = Callable[[str, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]


class OpenAlexError(RuntimeError):
    """The query could not be completed."""


class OpenAlexBlockedError(OpenAlexError):
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
        raise OpenAlexError(f"refusing to fetch {url!r}: scheme {scheme or '(none)'!r}")
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
    url: str, *, mailto: str, timeout_s: float, open_url: OpenUrl | None
) -> dict[str, Any]:
    if not mailto or "@" not in mailto:
        raise ValueError(
            "mailto must be a contact address: OpenAlex asks for one and it "
            "buys the polite pool"
        )
    transport = open_url if open_url is not None else _open_url_urllib
    headers = {"User-Agent": f"topic-brief (mailto:{mailto})", "Accept": "application/json"}
    try:
        status, response_headers, body = transport(url, headers, timeout_s)
    except OpenAlexError:
        raise
    except Exception as exc:
        raise OpenAlexError(f"fetching {url!r} failed: {type(exc).__name__}: {exc}") from exc

    reason = _detect_block(status, response_headers, body)
    if reason is not None:
        raise OpenAlexBlockedError(
            f"{url} is behind a bot wall: {reason}. Not retried, and no header "
            "rotation attempted — neither defeats real bot detection. Fetch it "
            "by hand if it is needed."
        )
    if status != 200:
        raise OpenAlexError(f"OpenAlex returned HTTP {status} for {url}")
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise OpenAlexError(f"OpenAlex returned unparseable JSON for {url}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise OpenAlexError(f"OpenAlex returned {type(parsed).__name__}, not an object")
    return parsed


# ------------------------------------------------------------------ shaping ---


_ID_RE = re.compile(r"^(?:https?://openalex\.org/)?(I\d+)$", re.IGNORECASE)
_ROR_RE = re.compile(r"^(?:https?://ror\.org/)?(0[a-z0-9]{8})$", re.IGNORECASE)


def normalize_institution_id(value: str) -> str:
    """Accept an OpenAlex id or a ROR id, in bare or URL form.

    Returns the filter value OpenAlex wants. A bare name raises: resolving one
    is a separate, human-reviewed step, because a near-match is plausible and
    silent.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("institution_id is required")
    match = _ID_RE.match(text)
    if match:
        return match.group(1).upper()
    match = _ROR_RE.match(text)
    if match:
        return f"https://ror.org/{match.group(1).lower()}"
    raise ValueError(
        f"{value!r} is not an OpenAlex institution id (I followed by digits) "
        "or a ROR id (0 followed by eight alphanumerics). "
        "Look it up once with resolve_institution() and pin the id in config; "
        "searching by name at fetch time can silently match a different body."
    )


def reconstruct_abstract(inverted_index: Mapping[str, Any] | None) -> str:
    """Rebuild plain text from OpenAlex's ``{word: [positions]}`` form.

    The one genuinely non-obvious part of this API. Positions may be sparse
    and are not guaranteed ordered, so the words are placed by index rather
    than by iteration order.
    """
    if not inverted_index:
        return ""
    placed: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if not isinstance(positions, (list, tuple)):
            continue
        for position in positions:
            if isinstance(position, int):
                placed.append((position, str(word)))
    placed.sort()
    return " ".join(word for _, word in placed)


def _bare_id(value: Any) -> str:
    text = str(value or "")
    return text.rsplit("/", 1)[-1] if text else ""


def normalize_work(raw: Mapping[str, Any]) -> dict[str, Any]:
    """One OpenAlex work as a flat dict. Every key always present."""
    location = raw.get("primary_location") or {}
    source = location.get("source") or {}
    ids = raw.get("ids") or {}

    authors: list[str] = []
    for authorship in raw.get("authorships") or []:
        name = ((authorship or {}).get("author") or {}).get("display_name")
        if name:
            authors.append(str(name))

    doi = str(raw.get("doi") or "")
    url = str(location.get("landing_page_url") or "") or doi or str(raw.get("id") or "")

    return {
        "external_id": _bare_id(raw.get("id")),
        "url": url,
        "title": str(raw.get("title") or raw.get("display_name") or ""),
        "abstract": reconstruct_abstract(raw.get("abstract_inverted_index")),
        "journal": str(source.get("display_name") or ""),
        "authors": authors,
        "first_author": authors[0] if authors else "",
        "doi": doi,
        "pmid": _bare_id(ids.get("pmid")) if ids.get("pmid") else "",
        "published_at": str(raw.get("publication_date") or ""),
        "type": str(raw.get("type") or ""),
        "raw": dict(raw),
    }


# ------------------------------------------------------------------ queries ---


def resolve_institution(
    name: str,
    *,
    mailto: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = 30.0,
    open_url: OpenUrl | None = None,
) -> list[dict[str, Any]]:
    """Candidate institutions for a name, for a human to choose from once.

    Returns every candidate, ranked as OpenAlex ranked them, and picks none.
    That is the point: the caller is expected to read the list and pin an id,
    not to take ``[0]``.
    """
    query = urllib.parse.urlencode({"search": name, "per_page": 10})
    payload = _get_json(
        f"{base_url}/institutions?{query}",
        mailto=mailto,
        timeout_s=timeout_s,
        open_url=open_url,
    )
    return [
        {
            "id": _bare_id(r.get("id")),
            "ror": str(r.get("ror") or ""),
            "display_name": str(r.get("display_name") or ""),
            "type": str(r.get("type") or ""),
            "works_count": int(r.get("works_count") or 0),
            "country_code": str(r.get("country_code") or ""),
        }
        for r in payload.get("results") or []
    ]


def search_openalex_works(
    institution_id: str | None = None,
    *,
    from_date: str,
    to_date: str,
    search: str | None = None,
    mailto: str,
    per_page: int = MAX_PER_PAGE,
    max_pages: int = 10,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = 30.0,
    open_url: OpenUrl | None = None,
) -> list[dict[str, Any]]:
    """Works published between two dates, narrowed by institution or search.

    AT LEAST ONE of ``institution_id`` or ``search`` is required. Neither is
    refused rather than run, because the resulting query is every work
    OpenAlex holds in the window — a quarter of a million for a single month.
    That is not a wide search, it is a mistake, and left to run it surfaces
    as a timeout or a truncated page rather than as an error.

    With an institution the filter is exact and a caller's own keywords can
    do the rest; without one, ``search`` is the only thing between the caller
    and the whole corpus. That asymmetry is why ``search`` is discouraged for
    institution queries and required without one.

    Pages with a cursor rather than an offset, because offset paging caps out
    and silently truncates a wide window. Stops at ``max_pages``; reaching it
    returns what was collected rather than raising, and the caller compares
    against its own expectation if completeness matters.
    """
    if per_page < 1 or per_page > MAX_PER_PAGE:
        raise ValueError(f"per_page must be 1..{MAX_PER_PAGE}, got {per_page}")
    if max_pages < 1:
        raise ValueError(f"max_pages must be >= 1, got {max_pages}")
    if not (institution_id or "").strip() and not (search or "").strip():
        raise ValueError(
            "pass institution_id, search, or both. With neither, the query is "
            "every work OpenAlex holds in the window — hundreds of thousands "
            "for a single month — which fails as a timeout rather than as an "
            "error."
        )

    filters = [
        f"from_publication_date:{from_date}",
        f"to_publication_date:{to_date}",
    ]
    if (institution_id or "").strip():
        filters.insert(
            0,
            f"authorships.institutions.lineage:"
            f"{normalize_institution_id(institution_id)}",
        )

    works: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = "*"
    for _page in range(max_pages):
        params = {"filter": ",".join(filters), "per-page": str(per_page), "cursor": cursor}
        if search:
            params["search"] = search
        payload = _get_json(
            f"{base_url}/works?{urllib.parse.urlencode(params)}",
            mailto=mailto,
            timeout_s=timeout_s,
            open_url=open_url,
        )
        results = payload.get("results") or []
        for raw in results:
            if not isinstance(raw, Mapping):
                continue
            work = normalize_work(raw)
            # A cursor walk can repeat a record when the index shifts under it.
            if work["external_id"] and work["external_id"] in seen:
                continue
            seen.add(work["external_id"])
            works.append(work)

        cursor = (payload.get("meta") or {}).get("next_cursor") or ""
        if not results or not cursor:
            break

    return works
