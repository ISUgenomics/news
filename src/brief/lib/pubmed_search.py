"""PubMed search to plain article dicts via NCBI E-utilities.

Given a PubMed query string, returns one dict per matching article, keyed by
PMID, using two public JSON calls: ``esearch`` (term + optional Entrez-date
window) to get PMIDs, then ``esummary`` to get title, journal, authors, dates
and DOI. No login, no API key required (3 req/s unkeyed; pass ``api_key`` for
10).

Contract: ``search_pubmed(query, email=..., mindate=..., maxdate=...,
base_url=..., tool=...)`` -> ``list[dict]`` with keys ``external_id`` (PMID
str), ``url``, ``title``, ``journal``, ``authors`` (list of str), ``volume``,
``issue``, ``pages``, ``doi``, ``published_at`` (ISO date or None) and ``raw``
(the esummary record). Zero hits -> ``[]`` without a second call. Any HTTP
failure or an ``error``/``ERROR`` key in either envelope -> ``RuntimeError``
naming the status or NCBI message; never a silent partial list -- a per-UID
``error`` in the esummary result raises too, rather than yielding a blank
article.

Dates are explicit ``YYYY/MM/DD`` (or ``YYYY/MM``, ``YYYY``) strings with
``datetype`` (default ``edat``, the Entrez date), not a ``datetime`` and not
``reldate``: the caller converts its own clock so a request is reproducible and
a golden fixture is pinnable. E-utilities silently ignores a one-sided window,
so passing only one of ``mindate``/``maxdate`` raises ``ValueError`` rather
than quietly returning an unfiltered set. Likewise ``retmax`` above 10000 is
silently clamped server-side, so ``max_results`` outside 1..10000 raises.

``esummary`` is sent as GET up to 200 PMIDs and as a form-encoded POST above
that, which is NCBI's documented threshold for URL length.

``published_at`` comes from ``pubdate``, falling back to ``epubdate`` when
``pubdate`` is absent or unparseable. A partial PubMed date fills the missing
parts with 1: ``'2026 Sep'`` -> ``2026-09-01``, ``'2026'`` -> ``2026-01-01``. A
month range takes the first month it names (``'2024 Nov-Dec'`` ->
``2024-11-01``). A season or any other unrecognized month token degrades to the
year and drops the day with it (``'2024 Winter'`` -> ``2024-01-01``), rather
than dating the record against a guessed January.

Errors name the request but never its credentials: the ``api_key`` value is
masked in any URL a ``RuntimeError`` carries, because the caller logs what this
module raises. ``esummary_records`` rejects a bare string in place of a list of
PMIDs (it would otherwise be iterated one character per id) and
``record_to_item`` rejects a record with no ``uid``.

Deliberately not done: building a PubMed saved-search RSS URL. PubMed mints
that feed id server-side when a user clicks "Create RSS"; it cannot be derived
from the query, and a minted feed URL is an ordinary RSS source anyway. Also
not done: composing a citation line into a ``body`` field (presentation policy
-- ``citation_line(record)`` is offered as a pure helper the caller may call
with either an esummary record or the item dict built from one, NLM-style: up
to six authors listed, more truncated to three plus "et al."),
relevance filtering, dedup, hashing, storage, logging, reading config or the
environment. Those belong to the caller.

Dependencies: stdlib only (``urllib``, ``json``, ``datetime``). ``base_url``
is a parameter so tests run against a real stub HTTP server on an ephemeral
port, never a mocked ``urllib``.
* ``Content-Encoding`` is undone before the body leaves the transport, so a
  caller always receives plain bytes. ``urllib`` does not decompress and
  ``requests`` does, which is why this is easy to miss: ``.decode()`` on a
  gzip body yields replacement characters rather than raising, and the
  failure then surfaces as nonsense content far from its cause. Nothing here
  sends ``Accept-Encoding``, so a compressed reply is not expected — this is
  for the proxy, the CDN, and the next header someone adds. A body that does
  not match the encoding it declares raises rather than being passed through.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_TOOL = "pubmed-search"
#: E-utilities silently clamps ``retmax`` above this.
MAX_RETMAX = 10000
#: NCBI's documented threshold for switching ESummary from GET to POST.
GET_ID_LIMIT = 200
#: Authors listed in full by :func:`citation_line` before "et al." kicks in.
CITATION_AUTHOR_LIMIT = 6
CITATION_AUTHORS_WHEN_TRUNCATED = 3

_ARTICLE_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
_DATE_RE = re.compile(r"^\d{4}(/\d{2}(/\d{2})?)?$")
_PUBDATE_RE = re.compile(r"^(\d{4})(?!\d)(?:\s+([A-Za-z]{3,})(?:\s+(\d{1,2}))?)?")
_API_KEY_IN_URL_RE = re.compile(r"(?i)(api_key=)[^&]*")
_DOI_ELOCATION_RE = re.compile(r"^doi:\s*(\S+)", re.IGNORECASE)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


# ----------------------------------------------------------- public API ---


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


def search_pubmed(
    query: str,
    *,
    email: str,
    mindate: str | None = None,
    maxdate: str | None = None,
    datetype: str = "edat",
    max_results: int = 200,
    base_url: str = DEFAULT_BASE_URL,
    tool: str = DEFAULT_TOOL,
    api_key: str | None = None,
    timeout_s: float = 30.0,
    user_agent: str | None = None,
) -> list[dict]:
    """Run ``query`` and return one article dict per matching PMID."""
    pmids = esearch_ids(
        query,
        email=email,
        mindate=mindate,
        maxdate=maxdate,
        datetype=datetype,
        max_results=max_results,
        base_url=base_url,
        tool=tool,
        api_key=api_key,
        timeout_s=timeout_s,
        user_agent=user_agent,
    )
    # No guard on an empty pmids list here on purpose: esummary_records is the
    # single place that decides "no ids, no request", so that rule has one
    # enforcement point and one test.
    records = esummary_records(
        pmids,
        email=email,
        base_url=base_url,
        tool=tool,
        api_key=api_key,
        timeout_s=timeout_s,
        user_agent=user_agent,
    )
    return [record_to_item(record) for record in records]


def esearch_ids(
    query: str,
    *,
    email: str,
    mindate: str | None = None,
    maxdate: str | None = None,
    datetype: str = "edat",
    max_results: int = 200,
    base_url: str = DEFAULT_BASE_URL,
    tool: str = DEFAULT_TOOL,
    api_key: str | None = None,
    timeout_s: float = 30.0,
    user_agent: str | None = None,
) -> list[str]:
    """Return up to ``max_results`` PMIDs matching ``query``, newest first."""
    if not query or not query.strip():
        raise ValueError("query must be a non-empty PubMed search expression")
    if not email or not email.strip():
        raise ValueError("email is required: NCBI asks every caller to identify itself")
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise ValueError("max_results must be an int")
    if not 1 <= max_results <= MAX_RETMAX:
        raise ValueError(f"max_results must be between 1 and {MAX_RETMAX}")
    if (mindate is None) != (maxdate is None):
        raise ValueError(
            "mindate and maxdate must be given together; E-utilities silently "
            "ignores a one-sided date window"
        )

    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": str(max_results),
        "retstart": "0",
        "tool": tool,
        "email": email,
    }
    if mindate is not None:
        params["datetype"] = datetype
        params["mindate"] = _validate_date(mindate, "mindate")
        params["maxdate"] = _validate_date(maxdate, "maxdate")
    if api_key is not None:
        params["api_key"] = api_key

    payload = _get_json(
        _endpoint(base_url, "esearch.fcgi"),
        params,
        timeout_s=timeout_s,
        user_agent=user_agent,
        what="esearch",
    )
    _raise_for_envelope_error(payload, "esearch")

    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        raise RuntimeError("esearch: response has no esearchresult object")
    _raise_for_envelope_error(result, "esearch")

    idlist = result.get("idlist")
    if not isinstance(idlist, list):
        raise RuntimeError("esearch: esearchresult has no idlist")
    return [str(pmid) for pmid in idlist][:max_results]


def esummary_records(
    pmids: list[str],
    *,
    email: str,
    base_url: str = DEFAULT_BASE_URL,
    tool: str = DEFAULT_TOOL,
    api_key: str | None = None,
    timeout_s: float = 30.0,
    user_agent: str | None = None,
) -> list[dict]:
    """Return the esummary record for each PMID, in the server's ``uids`` order."""
    if not email or not email.strip():
        raise ValueError("email is required: NCBI asks every caller to identify itself")
    if isinstance(pmids, (str, bytes)):
        # A bare string is iterable, so this would otherwise become one request
        # per character with a nonsense id list.
        raise TypeError("pmids must be a sequence of PMIDs, not a single string")
    pmids = [str(pmid) for pmid in pmids]
    if not pmids:
        return []

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
        "tool": tool,
        "email": email,
    }
    if api_key is not None:
        params["api_key"] = api_key

    url = _endpoint(base_url, "esummary.fcgi")
    if len(pmids) > GET_ID_LIMIT:
        payload = _post_json(
            url, params, timeout_s=timeout_s, user_agent=user_agent, what="esummary"
        )
    else:
        payload = _get_json(
            url, params, timeout_s=timeout_s, user_agent=user_agent, what="esummary"
        )
    _raise_for_envelope_error(payload, "esummary")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("esummary: response has no result object")
    _raise_for_envelope_error(result, "esummary")

    uids = result.get("uids")
    if not isinstance(uids, list):
        raise RuntimeError("esummary: result has no uids list")

    records = []
    for uid in uids:
        record = result.get(str(uid))
        if not isinstance(record, dict):
            raise RuntimeError(f"esummary: no record for PMID {uid}")
        error = record.get("error")
        if error:
            raise RuntimeError(f"esummary: PMID {uid}: {error}")
        records.append(record)
    return records


def record_to_item(record: dict) -> dict:
    """Map one esummary record onto a plain article dict. Pure."""
    pmid = str(record.get("uid") or "").strip()
    if not pmid:
        raise ValueError("esummary record has no uid")

    published_at = parse_pubdate(str(record.get("pubdate") or "")) or parse_pubdate(
        str(record.get("epubdate") or "")
    )
    return {
        "external_id": pmid,
        "url": _ARTICLE_URL.format(pmid=pmid),
        "title": str(record.get("title") or ""),
        "journal": str(record.get("source") or ""),
        "authors": _author_names(record),
        "volume": str(record.get("volume") or ""),
        "issue": str(record.get("issue") or ""),
        "pages": str(record.get("pages") or ""),
        "doi": _doi(record),
        "published_at": published_at,
        "raw": record,
    }


def citation_line(record: dict) -> str:
    """Render one esummary record as a single plain-text citation line. Pure.

    Accepts either an esummary record or the dict :func:`record_to_item` built
    from one -- an item dict carries the record under ``raw`` and is what a
    caller of :func:`search_pubmed` actually holds. Passing the item dict to a
    record-only reader would quietly drop the journal, the date and the DOI,
    which is the kind of silent partial this module refuses elsewhere.
    """
    raw = record.get("raw")
    if isinstance(raw, dict):
        record = raw

    parts = []

    authors = _author_names(record)
    if len(authors) > CITATION_AUTHOR_LIMIT:
        authors = authors[:CITATION_AUTHORS_WHEN_TRUNCATED] + ["et al."]
    if authors:
        parts.append(", ".join(authors))

    journal = str(record.get("source") or "").strip()
    if journal:
        parts.append(journal)

    locator = str(record.get("pubdate") or "").strip()
    volume = str(record.get("volume") or "").strip()
    issue = str(record.get("issue") or "").strip()
    pages = str(record.get("pages") or "").strip()
    if volume:
        locator += f";{volume}" if locator else volume
        if issue:
            locator += f"({issue})"
    if pages:
        locator += f":{pages}" if locator else pages
    if locator:
        parts.append(locator)

    doi = _doi(record)
    if doi:
        parts.append(f"doi:{doi}")

    # ". " is the separator, so a part that already ends in one ("et al.",
    # "Proc. Natl. Acad. Sci.") must not produce a doubled stop.
    return ". ".join(part.rstrip(". ") for part in parts).replace("\n", " ")


def parse_pubdate(text: str) -> str | None:
    """``'2026 Sep 3'`` | ``'2026 Sep'`` | ``'2026'`` -> ISO date, else None. Pure.

    An unrecognised month token degrades to the year; a month range takes its
    first month.
    """
    match = _PUBDATE_RE.match((text or "").strip())
    if not match:
        return None
    year = int(match.group(1))
    month = _MONTHS.get((match.group(2) or "")[:3].lower())
    if month is None:
        # A season, a bare year, or anything else this does not recognise: give
        # back the year, not January plus whatever day followed the token.
        return _dt.date(year, 1, 1).isoformat()
    day = int(match.group(3) or 1)
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


# ------------------------------------------------------------ internals ---


def _safe_url(url: str) -> str:
    """``url`` with the ``api_key`` value masked, for messages the caller logs."""
    return _API_KEY_IN_URL_RE.sub(r"\1REDACTED", url)


def _endpoint(base_url: str, name: str) -> str:
    return f"{base_url.rstrip('/')}/{name}"


def _validate_date(value: str, field: str) -> str:
    value = (value or "").strip()
    if not _DATE_RE.match(value):
        raise ValueError(
            f"{field} must be YYYY, YYYY/MM or YYYY/MM/DD (E-utilities format), "
            f"got {value!r}"
        )
    return value


def _request(url: str, data: bytes | None, user_agent: str | None):
    headers = {"Accept": "application/json"}
    if user_agent:
        headers["User-Agent"] = user_agent
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return urllib.request.Request(url, data=data, headers=headers)


def _read_json(request, *, timeout_s: float, what: str) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = _decompressed(response.headers, response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{what}: HTTP {exc.code} from {_safe_url(request.full_url)}"
        ) from exc
    except OSError as exc:  # URLError, socket.timeout, connection refused
        raise RuntimeError(f"{what}: request failed: {exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{what}: response was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{what}: response JSON was not an object")
    return payload


def _get_json(
    url: str, params: dict, *, timeout_s: float, user_agent: str | None, what: str
) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    return _read_json(_request(full, None, user_agent), timeout_s=timeout_s, what=what)


def _post_json(
    url: str, params: dict, *, timeout_s: float, user_agent: str | None, what: str
) -> dict:
    body = urllib.parse.urlencode(params).encode("utf-8")
    return _read_json(_request(url, body, user_agent), timeout_s=timeout_s, what=what)


def _raise_for_envelope_error(payload: dict, what: str) -> None:
    for key in ("error", "ERROR"):
        message = payload.get(key)
        if message:
            raise RuntimeError(f"{what}: {message}")


def _author_names(record: dict) -> list[str]:
    authors = record.get("authors")
    if not isinstance(authors, list):
        return []
    names = []
    for author in authors:
        if isinstance(author, dict):
            name = str(author.get("name") or "").strip()
        else:
            name = str(author).strip()
        if name:
            names.append(name)
    return names


def _doi(record: dict) -> str | None:
    articleids = record.get("articleids")
    if isinstance(articleids, list):
        for entry in articleids:
            if isinstance(entry, dict) and str(entry.get("idtype")).lower() == "doi":
                value = str(entry.get("value") or "").strip()
                if value:
                    return value
    match = _DOI_ELOCATION_RE.match(str(record.get("elocationid") or "").strip())
    return match.group(1) if match else None
