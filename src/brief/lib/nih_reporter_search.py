"""Search NIH RePORTER API v2 and return projects as plain dicts.

One POST to ``{base_url}/v2/projects/search`` per page, with a ``criteria``
object built from either exact organization names (``org_names``) or a free
text query (``advanced_text_search`` over title, terms and abstract), bounded
by an inclusive ``project_start_date`` window. Pages of up to 500 are walked
until ``meta.total`` is exhausted or ``max_records`` is reached; RePORTER
refuses offsets past 14,999, so ``max_records`` is capped at 15,000 and a
short ``sleep`` between pages respects the documented one-request-per-second
courtesy limit.

Contract: every hit comes back as a flat dict — ``external_id`` (project_num),
``appl_id``, ``url``, ``title``, ``body`` (abstract), ``published_at``,
``start_date``, ``end_date``, ``org_name``, ``pi_names``, ``agency``,
``amount``, ``fiscal_year`` — plus ``raw``, the untouched RePORTER record, so
nothing the API said is lost. Missing fields are ``None`` or ``[]``, never a
KeyError. ``build_criteria()`` is public so a caller can log or hash the exact
query it sent. Failures (non-2xx, bad JSON, no ``results`` key) raise
``NIHReporterError``; an empty list means the search genuinely matched
nothing.

Deliberately not done: no ``since`` datetime arithmetic (pass date strings),
no relevance filtering, no dedup, no storage, no logging, no retries — the
caller owns cadence and failure policy. No auth: RePORTER is public.

Dependencies: none. ``urllib.request`` + ``json`` cover a JSON POST; taking
``requests`` would make this the module's only third-party import for no gain.
Tests run against a real stub HTTP server (``tests/harness/stub_http.py``),
never a mocked ``urlopen``.

----

Notes added at write time, from the live API documentation and the boundary's
"fixes to apply":

* The error class is ``NIHReporterError``; the approved boundary text called it
  ``ReporterError`` and its own "fixes to apply" list then renamed it
  (``Reporter`` alone is ambiguous in a repo full of reports). The contract
  paragraph above was corrected to the real name rather than left contradicting
  the code.
* ``search_projects`` sends RePORTER's ``sort_field``/``sort_order`` (defaults
  ``"project_start_date"``/``"desc"``); ``sort_field=None`` omits both and
  takes whatever order RePORTER chooses. ``fetch_projects`` sets
  ``sort_field=date_field`` *and* re-sorts locally, so its newest-first
  guarantee never depends on the API honoring the hint.
* ``fetch_projects`` orders on the raw ``date_field`` value, not on
  ``published_at``: a record with no ``award_notice_date`` sorts last in an
  ``award_notice_date`` search instead of borrowing its start date.
* ``date_field`` selects which inclusive window the criteria carries —
  ``project_start_date`` (the default) or ``award_notice_date``, which is what
  "newly funded this week" actually means. Both are sent as RePORTER's
  ``{"from_date": ..., "to_date": ...}`` range object.
* ``operator`` (``"and"``) and ``search_field``
  (``"projecttitle,terms,abstracttext"``) are parameters, not constants.
* The courtesy delay is ``page_delay_s`` (default 1.0) passed to the injected
  ``sleep``; it is slept *between* pages, never after the last one.
* ``amount`` is the verbatim JSON value of ``award_amount`` — no coercion, no
  currency parsing.
* RePORTER's ``org_names`` matches with implicit wildcards and ignores case;
  ``extra_criteria={"org_names_exact_match": [...]}`` is the escape hatch when
  a caller needs literal equality. ``extra_criteria`` is merged last and wins,
  so any RePORTER criterion this module does not model is still reachable.
* Dates in and out are ``YYYY-MM-DD``. RePORTER answers with
  ``"2023-05-15T00:00:00"``; normalization trims the time part so goldens and
  ``published_at`` stay date-shaped.
* Empty criteria are rejected with ``ValueError`` before any socket is opened:
  RePORTER answers a criteria-less search with HTTP 500.
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

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

#: RePORTER refuses an offset greater than this.
MAX_OFFSET = 14_999
#: RePORTER refuses a limit greater than this.
MAX_PAGE_SIZE = 500
#: The most records a paged search can reach, given ``MAX_OFFSET``.
MAX_RECORDS = MAX_OFFSET + 1
#: The two RePORTER date ranges this module models directly.
DATE_FIELDS = ("project_start_date", "award_notice_date")

DEFAULT_BASE_URL = "https://api.reporter.nih.gov"
SEARCH_ENDPOINT = "/v2/projects/search"
DEFAULT_SEARCH_FIELD = "projecttitle,terms,abstracttext"
DEFAULT_DETAIL_URL_BASE = "https://reporter.nih.gov/project-details/"
DEFAULT_USER_AGENT = "nih-reporter-search/0.1"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class NIHReporterError(RuntimeError):
    """A RePORTER call failed, or answered with something unusable."""


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


def build_criteria(
    *,
    org_names: list[str] | None = None,
    advanced_text_search: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    date_field: str = "project_start_date",
    operator: str = "and",
    search_field: str = DEFAULT_SEARCH_FIELD,
    extra_criteria: dict | None = None,
) -> dict:
    """Build the RePORTER ``criteria`` object for one search.

    Returns a plain JSON-ready dict, so a caller can log or hash the exact
    query. Empty inputs are omitted rather than sent as empty filters, and
    ``extra_criteria`` is merged last, so it wins on a key collision.

    Raises ``ValueError`` for a date that is not ``YYYY-MM-DD`` or a
    ``date_field`` RePORTER does not offer as a range.
    """
    if date_field not in DATE_FIELDS:
        raise ValueError(f"date_field must be one of {DATE_FIELDS}, not {date_field!r}")

    criteria: dict[str, Any] = {}

    if org_names:
        criteria["org_names"] = [str(name) for name in org_names]

    if advanced_text_search and advanced_text_search.strip():
        criteria["advanced_text_search"] = {
            "operator": operator,
            "search_field": search_field,
            "search_text": advanced_text_search.strip(),
        }

    window = {}
    if start_from is not None:
        window["from_date"] = _check_date("start_from", start_from)
    if start_to is not None:
        window["to_date"] = _check_date("start_to", start_to)
    if window:
        criteria[date_field] = window

    if extra_criteria:
        criteria.update(extra_criteria)

    return criteria


def search_projects(
    criteria: dict,
    *,
    base_url: str = DEFAULT_BASE_URL,
    page_size: int = MAX_PAGE_SIZE,
    max_records: int = MAX_RECORDS,
    timeout_s: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
    sleep: Callable[[float], None] = time.sleep,
    page_delay_s: float = 1.0,
    sort_field: str | None = "project_start_date",
    sort_order: str = "desc",
) -> list[dict]:
    """Walk every page of one RePORTER search and return the raw records.

    Pages of ``page_size`` (clamped to 1..500) are requested until
    ``meta.total`` is reached, a short page arrives, ``max_records`` (clamped
    to 15,000) is collected, or the next offset would exceed 14,999 — the
    offset RePORTER refuses. ``sleep(page_delay_s)`` runs *between* pages only.
    The returned list is never longer than that clamped ``max_records``, even
    if a page over-delivers, and a ``max_records`` of zero or less returns
    ``[]`` without opening a socket.

    ``sort_field``/``sort_order`` are passed to RePORTER as sent; a falsy
    ``sort_field`` omits both and accepts whatever order the API returns.

    Raises ``ValueError`` on empty criteria (RePORTER answers those with a
    500) and ``NIHReporterError`` on any transport, status, or body problem —
    a failed page is never quietly returned as a short list.
    """
    if not criteria:
        raise ValueError(
            "criteria must not be empty; RePORTER rejects a criteria-less search"
        )

    url = base_url.rstrip("/") + SEARCH_ENDPOINT
    limit_cap = max(1, min(int(page_size), MAX_PAGE_SIZE))
    wanted = max(0, min(int(max_records), MAX_RECORDS))

    records: list[dict] = []
    offset = 0
    # ``offset`` always equals ``len(records)``, so clamping ``wanted`` to
    # MAX_RECORDS is what keeps every offset at or under RePORTER's 14,999.
    while len(records) < wanted:
        payload: dict[str, Any] = {
            "criteria": dict(criteria),
            "offset": offset,
            "limit": min(limit_cap, wanted - len(records)),
        }
        if sort_field:
            payload["sort_field"] = sort_field
            payload["sort_order"] = sort_order

        body = _post_json(url, payload, timeout_s=timeout_s, user_agent=user_agent)
        if not isinstance(body, dict):
            raise NIHReporterError(
                f"RePORTER answered with {type(body).__name__}, not an object"
            )
        if "results" not in body:
            raise NIHReporterError("RePORTER answer has no 'results' key")
        page = body["results"]
        if not isinstance(page, list):
            raise NIHReporterError(f"'results' is {type(page).__name__}, not a list")

        records.extend(page)
        if not page or len(page) < payload["limit"]:
            break

        meta = body.get("meta")
        total = meta.get("total") if isinstance(meta, dict) else None
        offset += len(page)
        if isinstance(total, int) and offset >= total:
            break
        if len(records) >= wanted:
            break
        sleep(page_delay_s)

    # A page that over-delivers (more records than the limit asked for) must
    # not push the result past the cap the caller set.
    return records[:wanted]


def normalize_project(
    record: dict, *, detail_url_base: str = DEFAULT_DETAIL_URL_BASE
) -> dict:
    """Flatten one RePORTER record into this module's output dict.

    Every documented key is always present: missing fields, and nested objects
    that are not the shape RePORTER documents, become ``None`` (or ``""`` for
    ``body``, ``[]`` for ``pi_names``), never a KeyError. ``raw`` is the same
    object that came in, unmodified. ``amount`` is the verbatim JSON value of
    ``award_amount``. ``agency`` is ``agency_ic_admin.name``, or the value
    itself when RePORTER sends that field as a bare string.
    """
    if not isinstance(record, dict):
        raise NIHReporterError(
            f"expected a RePORTER record object, got {type(record).__name__}"
        )

    appl_id = record.get("appl_id")
    url = record.get("project_detail_url")
    if not url and appl_id is not None:
        url = f"{detail_url_base}{appl_id}"

    start_date = _as_date(record.get("project_start_date"))
    notice_date = _as_date(record.get("award_notice_date"))

    organization = record.get("organization")
    agency = record.get("agency_ic_admin")
    abstract = record.get("abstract_text")

    investigators = record.get("principal_investigators")
    pi_names: list[str] = []
    if isinstance(investigators, list):
        for person in investigators:
            if isinstance(person, dict):
                full_name = person.get("full_name")
                if isinstance(full_name, str) and full_name.strip():
                    pi_names.append(full_name)

    return {
        "external_id": record.get("project_num"),
        "appl_id": appl_id,
        "url": url or None,
        "title": record.get("project_title"),
        "body": abstract if isinstance(abstract, str) else "",
        "published_at": notice_date or start_date,
        "start_date": start_date,
        "end_date": _as_date(record.get("project_end_date")),
        "org_name": organization.get("org_name")
        if isinstance(organization, dict)
        else None,
        "pi_names": pi_names,
        "agency": agency.get("name") if isinstance(agency, dict) else agency,
        "amount": record.get("award_amount"),
        "fiscal_year": record.get("fiscal_year"),
        "raw": record,
    }


def fetch_projects(
    *,
    org_names: list[str] | None = None,
    advanced_text_search: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    date_field: str = "project_start_date",
    operator: str = "and",
    search_field: str = DEFAULT_SEARCH_FIELD,
    extra_criteria: dict | None = None,
    base_url: str = DEFAULT_BASE_URL,
    page_size: int = MAX_PAGE_SIZE,
    max_records: int = MAX_RECORDS,
    timeout_s: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
    sleep: Callable[[float], None] = time.sleep,
    page_delay_s: float = 1.0,
    detail_url_base: str = DEFAULT_DETAIL_URL_BASE,
) -> list[dict]:
    """build_criteria + search_projects + normalize_project, newest first.

    The convenience entry point: results are sorted descending by
    ``date_field`` (records missing that date sort last), so the caller does
    not depend on RePORTER's own ordering.
    """
    criteria = build_criteria(
        org_names=org_names,
        advanced_text_search=advanced_text_search,
        start_from=start_from,
        start_to=start_to,
        date_field=date_field,
        operator=operator,
        search_field=search_field,
        extra_criteria=extra_criteria,
    )
    records = search_projects(
        criteria,
        base_url=base_url,
        page_size=page_size,
        max_records=max_records,
        timeout_s=timeout_s,
        user_agent=user_agent,
        sleep=sleep,
        page_delay_s=page_delay_s,
        sort_field=date_field,
    )
    projects = [normalize_project(r, detail_url_base=detail_url_base) for r in records]

    def order(project: dict) -> tuple[bool, str]:
        # The chosen field as RePORTER sent it, never ``published_at``'s
        # fallback — a record with no date of that kind sorts last.
        when = _as_date(project["raw"].get(date_field))
        return (when is not None, when or "")

    projects.sort(key=order, reverse=True)
    return projects


# ------------------------------------------------------------- internals ---


def _check_date(name: str, value: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"{name} must be a YYYY-MM-DD date, not {value!r}")
    return value


def _as_date(value: Any) -> str | None:
    """``"2024-09-01T00:00:00"`` -> ``"2024-09-01"``; anything else -> None."""
    if isinstance(value, str) and _DATE_RE.match(value[:10]):
        return value[:10]
    return None


def _post_json(url: str, payload: dict, *, timeout_s: float, user_agent: str) -> Any:
    """POST ``payload`` as JSON and return the parsed reply.

    The whole transport: one urllib request, no session, no retries. Every
    failure mode — status, socket, encoding, JSON — leaves as NIHReporterError.
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = _decompressed(response.headers, response.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = _decompressed(exc.headers or {}, exc.read()).decode(
                "utf-8", "replace"
            )[:200]
        except Exception:  # pragma: no cover - body already consumed
            pass
        raise NIHReporterError(
            f"RePORTER returned HTTP {exc.code} for {url}: {detail}"
        ) from exc
    except OSError as exc:  # URLError, timeout, DNS, refused connection
        raise NIHReporterError(f"RePORTER request to {url} failed: {exc}") from exc

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NIHReporterError(f"RePORTER answer was not JSON: {raw[:200]!r}") from exc
