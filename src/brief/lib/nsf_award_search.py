"""Query the NSF Award Search API and return awards as plain dicts.

Situation: a poller or brief needs "every NSF award to institution X",
"every award mentioning Y", or "every award to PI Z" since a date, and
wants the result in a stable shape it can store without learning NSF's
field names (awardeeName, pdPIName, fundsObligatedAmt, MM/DD/YYYY dates).

Contract: search_nsf_awards() takes explicit values only — at least one
of awardee / keyword / pi_name, a date_start (inclusive, on award date),
an optional date_end, and a base_url that defaults to api.nsf.gov and is
the seam tests point at a stub server. It walks the API's offset/rpp
pagination (rpp=25, offset a 0-based record index) until a short page or
max_pages, and returns one dict per award with fixed keys: external_id,
url, title, body, published_at (ISO date), pi_name, awardee, amount,
agency, program, raw. amount is the API's JSON value verbatim; nothing is
parsed into a number because a brief quotes dollar figures, it never
computes them. raw is the untouched record so a caller can store it and
re-normalize later. build_query_url(), normalize_award() and matches_awardee() are pure and
exported so the query string, the field mapping and the institution rule are
each pinned by fixtures.

**awardeeName is not an exact filter, and this module corrects for that.**
NSF splits the value into words and matches any of them, so a search for
a university by name comes back with awards from every institution
sharing any word in it — typically the word "University". Measured
against the live API: one month's search for a single named university
returned 308 awards across 159 distinct institutions, and the one asked
for was not among the eight most frequent. So ``awardee`` is enforced
twice: sent to the API, where it narrows nothing reliably, and applied
again to every record that comes back, where it decides. A caller asking
for one institution gets that institution or nothing.

``awardee_state`` is the companion, and the pairing is the point.
``awardeeStateCode`` *is* exact, so passing both turns the same search from
hundreds of unrelated awards into a small set that is cheap to page
through: over one 90-day window a single state code returned 68 awards
across 4 institutions rather than several hundred across 159. Without it
the local filter still gives a correct answer, just after fetching and
discarding far more.

The date filter is NSF's dateStart/dateEnd pair, which filters on the
award's *effective date* — the `date` field of a record, the one mapped to
published_at. It is not the project start date (`startDate`) and not the
date the record was last amended, so "new this week" means "took effect
this week", not "announced this week".

date_end=None means "through today", and today is a parameter, not a clock
read: pass today=date(...) for a deterministic query, or leave both None
and the caller gets the current UTC date — the only clock read in the
module, made once, outside every pure function. UTC, not the local date,
so the same call made from two machines builds the same query.

base_url must be http or https. The seam is there so tests can point at a
stub server, not so a caller can hand urllib a file:// or ftp:// URL it
found in a config file and have this function read a local path.

Any serviceNotification entry in the envelope aborts the walk, whatever
its notificationType says. NSF emits them only when it is unhappy with a
request, and a search that quietly returns "no awards" because a notice
was filed reads exactly like a week with no awards; the error the caller
can see is the cheaper failure.

Deliberately not done: no retry or backoff (the caller's schedule owns
that); no mapping to an app dataclass; no dedup, ranking, or relevance;
no caching; no logging. Errors surface as NSFSearchError (network, HTTP,
non-JSON, API error payload) or ValueError (bad arguments).

Dependencies: none. urllib.request + json + datetime, matching the
library's ollama-local-llm convention; one paginated GET does not earn
requests. Tests use the copied tests/harness/stub_http.py, never a mock.

Verified against the live API on 2026-09-17 (two facts that the drafted
boundary got wrong, kept here so the next reader does not re-derive them):

* `offset` is a 0-BASED record index, despite the published docs saying
  "default 1". offset=0 and an omitted offset both return record 1;
  offset=1 returns record 2. Starting the walk at 1 would silently drop
  the first award of every search, so the walk starts at 0.
* The default response already contains every field, abstractText
  included, and `printFields` is currently IGNORED by the service (asking
  for `id,title` still returns all ~60 fields). It is still sent, both
  because the boundary calls for an explicit field set and because the
  request then keeps working if NSF restores the parameter — but nothing
  here depends on it, and normalize_award() is tolerant of any field
  being absent.
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://api.nsf.gov/services/v1/awards.json"

#: NSF caps rpp at 25 and ignores larger values.
PAGE_SIZE = 25

#: Sent explicitly so the request names what it needs. The live service
#: currently returns every field regardless; see the module docstring.
PRINT_FIELDS = (
    "id,title,date,startDate,abstractText,piFirstName,piLastName,"
    "pdPIName,awardeeName,fundsObligatedAmt,agency,program,primaryProgram"
)

AWARD_URL_TEMPLATE = "https://www.nsf.gov/awardsearch/showAward?AWD_ID={id}"


class NSFSearchError(RuntimeError):
    """A request to the NSF Award Search API did not yield usable awards."""


def normalize_institution(name: str | None) -> str:
    """Casefold, strip punctuation, collapse whitespace. For comparison only."""
    return " ".join(re.sub(r"[^\w\s]", " ", (name or "")).casefold().split())


def matches_awardee(record_name: str | None, wanted: str | None) -> bool:
    """Does this award's awardee actually match what was asked for?

    Exported and pure because it is the correction for a real NSF behaviour and
    therefore needs its own tests: ``awardeeName`` is not an exact filter. NSF
    splits it into words and matches any of them, so a search for one named
    university returns awards from every institution sharing a word with it —
    measured at 159 distinct institutions in one month's window, the one asked
    for not among the most frequent.

    Matching is substring on the normalized form, so a short institution name
    also matches the longer legal name an agency may record ("… University"
    against "… University of Science and Technology"). ``wanted`` of None
    matches everything, which is what "no awardee filter" means.
    """
    if not wanted or not wanted.strip():
        return True
    return normalize_institution(wanted) in normalize_institution(record_name)


def build_query_url(
    base_url: str,
    *,
    awardee: str | None,
    keyword: str | None,
    pi_name: str | None,
    date_start: datetime.date,
    date_end: datetime.date,
    awardee_state: str | None = None,
    offset: int = 0,
    rpp: int = PAGE_SIZE,
) -> str:
    """Return the full GET URL for one page of results.

    Pure, and the parameter order is fixed, so a golden test can pin the exact
    query string. Filters that are None or blank are left out entirely — NSF
    treats an empty ``keyword=`` as a filter that matches nothing.

    ``awardee_state`` becomes ``awardeeStateCode``, which unlike ``awardeeName``
    *is* an exact filter. Pairing the two is what turns an institution search
    from hundreds of unrelated awards into a small exact set.
    """
    params: list[tuple[str, str]] = []
    for name, value in (
        ("keyword", keyword),
        ("awardeeName", awardee),
        ("pdPIName", pi_name),
        ("awardeeStateCode", awardee_state),
    ):
        if value and value.strip():
            params.append((name, value))
    params.append(("dateStart", _nsf_date(date_start)))
    params.append(("dateEnd", _nsf_date(date_end)))
    params.append(("offset", str(offset)))
    params.append(("rpp", str(rpp)))
    params.append(("printFields", PRINT_FIELDS))

    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)
    separator = "&" if urllib.parse.urlparse(base_url).query else "?"
    return f"{base_url}{separator}{query}"


def normalize_award(raw: dict) -> dict:
    """Map one API record onto the fixed key set.

    Tolerant by contract: any field may be missing, None, or of an unexpected
    type, and the result still has all eleven keys. ``amount`` is the JSON
    value verbatim — no conversion in either direction — because a brief
    quotes a dollar figure, it never computes one.
    """
    award_id = _text(raw.get("id"))
    pi_name = " ".join(
        part
        for part in (_text(raw.get("piFirstName")), _text(raw.get("piLastName")))
        if part
    )
    return {
        "external_id": award_id,
        "url": AWARD_URL_TEMPLATE.format(id=award_id) if award_id else "",
        "title": _text(raw.get("title")),
        "body": _text(raw.get("abstractText")),
        "published_at": _iso_date(raw.get("date")),
        "pi_name": pi_name,
        "awardee": _text(raw.get("awardeeName")),
        "amount": raw.get("fundsObligatedAmt", ""),
        "agency": _text(raw.get("agency")),
        "program": _text(raw.get("program")),
        "raw": raw,
    }


def search_nsf_awards(
    *,
    awardee: str | None = None,
    awardee_state: str | None = None,
    keyword: str | None = None,
    pi_name: str | None = None,
    date_start: datetime.date,
    date_end: datetime.date | None = None,
    today: datetime.date | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = 30.0,
    max_pages: int = 40,
    user_agent: str = "nsf-award-search/0.1",
) -> list[dict]:
    """Return every matching award, normalized, in the order the API sent them.

    Raises ValueError for arguments that cannot produce a sensible query --- no
    filter, an inverted date range, max_pages below 1, or a base_url that is
    not http/https --- and NSFSearchError for anything that goes wrong between
    here and NSF. A page that fails aborts the whole walk: a partial list that
    looks complete is worse than an error the caller can retry on its own
    schedule.

    The walk advances by the number of records the page actually returned, not
    by a fixed 25, so a service that ignores rpp cannot make consecutive pages
    overlap and duplicate awards. It stops on a page shorter than rpp, or after
    max_pages requests.
    """
    if not any(value and value.strip() for value in (awardee, keyword, pi_name)):
        raise ValueError("give at least one of awardee, keyword, pi_name")
    if max_pages < 1:
        raise ValueError(f"max_pages must be at least 1, got {max_pages}")
    scheme = urllib.parse.urlparse(base_url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"base_url must be http or https, got {scheme or 'no'} scheme")
    if date_end is None:
        date_end = today if today is not None else _utc_today()
    if date_end < date_start:
        raise ValueError(f"date_end {date_end} is before date_start {date_start}")

    awards: list[dict] = []
    offset = 0
    for _page in range(max_pages):
        url = build_query_url(
            base_url,
            awardee=awardee,
            awardee_state=awardee_state,
            keyword=keyword,
            pi_name=pi_name,
            date_start=date_start,
            date_end=date_end,
            offset=offset,
            rpp=PAGE_SIZE,
        )
        records = _fetch_page(url, timeout_s=timeout_s, user_agent=user_agent)
        # NSF's awardeeName is not an exact filter, so the institution the
        # caller asked for is enforced here, on what actually came back.
        awards.extend(
            normalize_award(record)
            for record in records
            if matches_awardee(record.get("awardeeName"), awardee)
        )
        if len(records) < PAGE_SIZE:
            break
        offset += len(records)
    return awards


# ------------------------------------------------------------------ private ---


def _utc_today() -> datetime.date:
    """Today in UTC. The module's only clock read, and never inside a pure
    function --- date_end is resolved once, at the edge, before any query is
    built, so build_query_url() and normalize_award() stay golden-testable."""
    return datetime.datetime.now(datetime.timezone.utc).date()


def _nsf_date(value: datetime.date) -> str:
    """NSF's dateStart/dateEnd format, which is mm/dd/yyyy and nothing else."""
    return f"{value.month:02d}/{value.day:02d}/{value.year:04d}"


def _text(value: object) -> str:
    """A string for a field that should be text; '' for missing or null."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _iso_date(value: object) -> str | None:
    """NSF's mm/dd/yyyy award date as ISO-8601, or None if it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _fetch_page(url: str, *, timeout_s: float, user_agent: str) -> list[dict]:
    """GET one page and return its award records. Raises NSFSearchError."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise NSFSearchError(f"NSF returned HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NSFSearchError(f"NSF request failed for {url}: {exc}") from exc

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise NSFSearchError(f"NSF returned a body that is not JSON: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise NSFSearchError("NSF response has no 'response' object")
    envelope = payload["response"]

    notifications = envelope.get("serviceNotification")
    if isinstance(notifications, list) and notifications:
        messages = "; ".join(
            text
            for text in (
                _text(note.get("notificationMessage"))
                for note in notifications
                if isinstance(note, dict)
            )
            if text
        )
        raise NSFSearchError(
            f"NSF service notification: {messages or 'no message given'}"
        )

    records = envelope.get("award")
    if not isinstance(records, list):
        raise NSFSearchError("NSF response has no 'award' list")
    if not all(isinstance(record, dict) for record in records):
        raise NSFSearchError("NSF 'award' list contains a non-object entry")
    return records
