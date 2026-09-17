"""Search USAspending.gov awards for a recipient or a keyword set within a date range.

Wraps POST /api/v2/search/spending_by_award/, the only public federal source that
covers USDA/NIFA, DOE, and DOD awards alongside NSF and NIH. USAspending refuses a
request that mixes award type groups (contracts A-D vs grants 02-05), so
``search_awards`` issues one paged request per requested group and concatenates.
Time filtering defaults to ``action_date``, which is what "new this week" means
to a reader; awards lag their announcement by weeks, and this module does not hide
that. ``date_type`` selects one of the other three values the API accepts.

Contract: ``search_awards(start_date=..., end_date=..., recipient=... | keywords=...)``
returns a list of plain dicts with stable keys (external_id, internal_id, url, title,
body, published_at, amount, recipient, awarding_agency, awarding_sub_agency,
award_type_group) plus ``raw``, the untouched API record, so a caller can store the
whole thing and never needs to re-fetch. ``build_request`` and ``normalize_award``
are pure and tested from fixtures; only ``search_awards`` touches the network, and
its ``base_url`` is injected so tests run against a stub HTTP server on localhost.
Any non-2xx, unreachable host, timeout, or response without ``results`` raises
``UsaspendingError``; nothing is returned partially or silently.

Deliberately not here: converting a datetime into date strings, mapping dicts onto
an app's record type, dedup, retries, logging, or any knowledge of which recipient
or agencies matter. Those belong to the adapter that calls this.

Dependencies: none beyond the standard library. One JSON POST and a page loop do
not justify ``requests``; urllib keeps the seed pinned to nothing.

Notes confirmed against the live API (2026-09-17):

* ``date_type`` is per time-period and selectable. ``action_date`` (the default)
  matches the *latest* transaction on an award, so it re-surfaces modifications to
  old awards; ``new_awards_only`` matches the base transaction and is what a
  "new this week" reader usually wants. ``last_modified_date`` and ``date_signed``
  are the other two values the API accepts.
* ``published_at`` is the award's **Start Date** (period of performance), which is
  *not* the field the time filter is applied to. A caller that needs the filtered
  date must read it out of ``raw``.
* Mixing codes from two groups is rejected with HTTP 422 and the message
  ``'award_type_codes' must only contain types from one group.`` — hence one
  request per group. ``AWARD_TYPE_GROUPS`` mirrors the groups the API itself
  returns in that error body.
* ``fields`` is required and validated per group, so ``DEFAULT_FIELDS`` is pinned
  here rather than left to the server's defaults. ``generated_internal_id`` is
  returned whether or not it is requested; it is the id in a usaspending.gov URL.
* ``fields`` and ``sort`` were verified against the *contracts* and *grants*
  mappings only. The other four groups have their own field mappings, so both are
  parameters on ``build_request`` and ``search_awards`` rather than constants —
  a caller searching ``loans`` or ``idvs`` passes names from that group's mapping.
* Paging is ``page``/``limit`` with ``page_metadata.hasNext`` as the stop signal.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Sequence

DEFAULT_BASE_URL = "https://api.usaspending.gov"
DEFAULT_SITE_URL = "https://www.usaspending.gov"
SEARCH_PATH = "/api/v2/search/spending_by_award/"

#: Award type codes grouped exactly as the API groups them; a request may only
#: carry codes from one of these. Mirrors the ``award_type_groups`` map the API
#: returns in its own 422 body when a request mixes two groups.
AWARD_TYPE_GROUPS: dict[str, tuple[str, ...]] = {
    "contracts": ("A", "B", "C", "D"),
    "grants": ("02", "03", "04", "05", "F001", "F002"),
    "idvs": (
        "IDV_A",
        "IDV_B",
        "IDV_B_A",
        "IDV_B_B",
        "IDV_B_C",
        "IDV_C",
        "IDV_D",
        "IDV_E",
    ),
    "loans": ("07", "08", "F003", "F004"),
    "other_financial_assistance": ("06", "10", "F006", "F007"),
    "direct_payments": ("09", "F005", "11", "-1", "F008", "F009", "F010"),
}

#: Values the API accepts for ``time_period[].date_type``.
DATE_TYPES: tuple[str, ...] = (
    "action_date",
    "date_signed",
    "last_modified_date",
    "new_awards_only",
)

#: Requested response fields, pinned because the server's defaults vary and
#: because ``fields`` is a required parameter. Every name here is accepted by
#: both the contract and the assistance field mappings.
DEFAULT_FIELDS: tuple[str, ...] = (
    "Award ID",
    "Recipient Name",
    "Start Date",
    "End Date",
    "Award Amount",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Award Type",
    "Description",
)

#: Result ordering pinned so a repeated search is reproducible. The API
#: validates this against the requested group's field mapping, exactly as it
#: validates ``fields``.
DEFAULT_SORT = "Award Amount"

MAX_LIMIT = 100


class UsaspendingError(RuntimeError):
    """The API was unreachable, refused the request, or answered nonsense."""


def build_request(
    *,
    start_date: str,
    end_date: str,
    award_type_codes: Sequence[str],
    recipient: str | None = None,
    keywords: Sequence[str] | None = None,
    date_type: str = "action_date",
    page: int = 1,
    limit: int = MAX_LIMIT,
    fields: Sequence[str] | None = None,
    sort: str = DEFAULT_SORT,
) -> dict:
    """Build the JSON body for one spending_by_award page.

    Pure: no network, no clock. ``award_type_codes`` must come from a single
    group (see ``AWARD_TYPE_GROUPS``) or the API answers 422. ``fields`` and
    ``sort`` are both validated by the API against the requested group's own
    mapping; the defaults are the ones verified for contracts and grants.

    Raises ``ValueError`` — never a request the API is certain to reject — for
    an empty ``award_type_codes`` or ``fields``, a ``date_type`` outside
    ``DATE_TYPES``, a ``limit`` outside 1..``MAX_LIMIT``, or a ``page`` below 1.
    """
    codes = list(award_type_codes)
    if not codes:
        raise ValueError("award_type_codes must not be empty")
    field_names = list(fields) if fields is not None else list(DEFAULT_FIELDS)
    if not field_names:
        raise ValueError("fields must not be empty; the API requires at least one")
    if date_type not in DATE_TYPES:
        raise ValueError(f"date_type {date_type!r} is not one of {DATE_TYPES}")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")
    if page < 1:
        raise ValueError(f"page must be 1 or greater, got {page}")

    filters: dict[str, Any] = {
        "time_period": [
            {
                "start_date": start_date,
                "end_date": end_date,
                "date_type": date_type,
            }
        ],
        "award_type_codes": codes,
    }
    if recipient:
        filters["recipient_search_text"] = [recipient]
    if keywords:
        filters["keywords"] = list(keywords)

    return {
        "filters": filters,
        "fields": field_names,
        "page": page,
        "limit": limit,
        "sort": sort,
        "order": "desc",
        "subawards": False,
    }


def normalize_award(
    record: dict,
    *,
    award_type_group: str,
    site_url: str = DEFAULT_SITE_URL,
    title_of: Callable[[dict], str] | None = None,
) -> dict:
    """Map one API record onto this module's stable dict shape.

    Pure, total, and forgiving: a record missing a field yields ``None`` for it
    rather than raising, because the API's field set varies by award type group.
    ``amount`` is the JSON value verbatim, as are ``recipient`` and the two
    agency names. ``published_at`` is the award's **Start Date**, which is not
    necessarily the date the search filtered on — read ``raw`` for that.
    ``title`` defaults to the description, falling back to the award id; pass
    ``title_of`` to compose something richer, which is the caller's policy.
    ``url`` is ``None`` when the record carries no ``generated_internal_id``,
    and the id is percent-encoded into the path — it is the server's string,
    not ours, and it ends up in a link.
    """
    internal_id = record.get("generated_internal_id")
    description = record.get("Description") or ""
    external_id = record.get("Award ID")

    if title_of is not None:
        title = title_of(record)
    else:
        title = description or external_id or ""

    return {
        "external_id": external_id,
        "internal_id": internal_id,
        "url": _award_url(site_url, internal_id),
        "title": title,
        "body": description,
        "published_at": record.get("Start Date"),
        "amount": record.get("Award Amount"),
        "recipient": record.get("Recipient Name"),
        "awarding_agency": record.get("Awarding Agency"),
        "awarding_sub_agency": record.get("Awarding Sub Agency"),
        "award_type_group": award_type_group,
        "raw": record,
    }


def search_awards(
    *,
    start_date: str,
    end_date: str,
    recipient: str | None = None,
    keywords: Sequence[str] | None = None,
    award_types: Sequence[str] = ("grants", "contracts"),
    date_type: str = "action_date",
    base_url: str = DEFAULT_BASE_URL,
    site_url: str = DEFAULT_SITE_URL,
    timeout_s: float = 30.0,
    max_pages: int = 10,
    fields: Sequence[str] | None = None,
    sort: str = DEFAULT_SORT,
    title_of: Callable[[dict], str] | None = None,
) -> list[dict]:
    """One paged request per award type group; concatenated normalized awards.

    Groups are requested in the order given, and each group is paged until the
    API says ``page_metadata.hasNext`` is false, a page comes back empty, or
    ``max_pages`` pages have been fetched. Pages are requested at the API
    maximum of ``MAX_LIMIT`` records.

    ``fields`` and ``sort`` reach every group's request unchanged; the defaults
    are verified for contracts and grants, and a caller searching one of the
    other four groups passes names from that group's mapping. ``site_url`` and
    ``title_of`` are handed to ``normalize_award`` untouched, so the injection
    points of the pure normalizer stay reachable from here.

    Any failure anywhere raises ``UsaspendingError`` — a non-2xx, an unreachable
    host, a timeout, a body that is not JSON, or a body whose ``results`` is
    missing, is not a list, or holds something that is not a record. There is no
    partial return: a group that fails on page 2 discards page 1 as well.
    ``ValueError`` (not ``UsaspendingError``) reports a caller mistake — an
    unknown or empty group name, or ``max_pages`` below 1 — before any request.
    """
    groups = list(award_types)
    if not groups:
        raise ValueError("award_types must name at least one group")
    unknown = [g for g in groups if g not in AWARD_TYPE_GROUPS]
    if unknown:
        raise ValueError(
            f"unknown award type group(s) {unknown!r}; "
            f"known groups are {sorted(AWARD_TYPE_GROUPS)}"
        )
    if max_pages < 1:
        raise ValueError(f"max_pages must be 1 or greater, got {max_pages}")

    url = base_url.rstrip("/") + SEARCH_PATH
    awards: list[dict] = []

    for group in groups:
        for page in range(1, max_pages + 1):
            payload = _post_json(
                url,
                build_request(
                    start_date=start_date,
                    end_date=end_date,
                    award_type_codes=AWARD_TYPE_GROUPS[group],
                    recipient=recipient,
                    keywords=keywords,
                    date_type=date_type,
                    page=page,
                    fields=fields,
                    sort=sort,
                ),
                timeout_s,
            )
            if not isinstance(payload, dict) or "results" not in payload:
                raise UsaspendingError(
                    f"USAspending response for group {group!r} page {page} "
                    "has no 'results' key"
                )
            results = payload["results"]
            if not isinstance(results, list):
                raise UsaspendingError(
                    f"USAspending 'results' for group {group!r} page {page} "
                    f"is {type(results).__name__}, not a list"
                )
            for record in results:
                if not isinstance(record, dict):
                    raise UsaspendingError(
                        f"USAspending 'results' for group {group!r} page {page} "
                        f"holds a {type(record).__name__}, not an award record"
                    )
            awards.extend(
                normalize_award(
                    record,
                    award_type_group=group,
                    site_url=site_url,
                    title_of=title_of,
                )
                for record in results
            )
            metadata = payload.get("page_metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            if not results or not metadata.get("hasNext"):
                break

    return awards


def _award_url(site_url: str, internal_id: Any) -> str | None:
    """``<site_url>/award/<internal_id>``, percent-encoded, or ``None``."""
    if not internal_id:
        return None
    quoted = urllib.parse.quote(str(internal_id), safe="")
    return f"{site_url.rstrip('/')}/award/{quoted}"


def _post_json(url: str, body: dict, timeout_s: float) -> Any:
    """POST ``body`` as JSON and return the decoded response.

    Every transport and decoding failure becomes ``UsaspendingError``; callers
    never see a urllib exception.
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Length": str(len(data)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # pragma: no cover - body already consumed
            pass
        raise UsaspendingError(
            f"USAspending returned HTTP {exc.code} for {url}: {detail}"
        ) from exc
    except OSError as exc:  # URLError, socket timeout, refused connection
        raise UsaspendingError(f"USAspending request to {url} failed: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsaspendingError(
            f"USAspending response from {url} is not JSON: {raw[:200]!r}"
        ) from exc
