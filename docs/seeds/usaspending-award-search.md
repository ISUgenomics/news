# Seed boundary: usaspending-award-search

## Purpose
Fetch USAspending.gov award records for one recipient or one keyword set within a date range, returned as normalized plain dicts that keep the raw record.

## when_to_use (draft for FEATURE.toml)
You need federal award records for one institution or one research topic over a date range, including the agencies the NSF and NIH APIs do not cover, and you want plain dicts to store rather than a lesson in USAspending's filter grammar.

## Inputs
- start_date: str (ISO date, inclusive; filters on action_date, which is what 'new this week' means)
- end_date: str (ISO date, inclusive)
- recipient: str | None (recipient_search_text; e.g. an institution name)
- keywords: list[str] | None (free-text match over award descriptions; the field-profile path)
- award_types: Sequence[str] = ('grants', 'contracts') (names of USAspending type groups; one request per group)
- base_url: str = 'https://api.usaspending.gov' (injected so tests point at stub_http)
- timeout_s: float = 30.0
- max_pages: int = 10 (hard stop; 100 records per page)

## Outputs
- list[dict], one per award, each with keys: external_id (Award ID), internal_id (generated_internal_id), url (https://www.usaspending.gov/award/<internal_id>), title (recipient + agency + amount, or description head), body (description), published_at (Start Date, ISO), amount (float | None), recipient, awarding_agency, awarding_sub_agency, award_type_group, raw (the untouched API record)
- Raises UsaspendingError (RuntimeError subclass) on non-2xx, unreachable host, timeout, or a response missing 'results'; never returns partial silently
- build_request() and normalize_award() are pure and importable on their own for fixture-driven tests

## Dependencies
- **stdlib only (urllib.request, json, datetime)** — The API is one JSON POST with a page loop; urllib does that in a dozen lines, the same idiom ollama-local-llm already uses, and a zero-dependency seed graduates without a pin.

## Must NOT know about
- config.yaml, sources.yaml, or the profile schema (recipient/keywords arrive as explicit arguments, never a params dict keyed by profile field names)
- the Item dataclass or any brief.models import; the adapter converts dicts to Item
- the poller's `since: datetime` contract; the seed takes ISO date strings
- SQLite, items.db, content_hash, or any dedup rule
- logging configuration; it raises or returns, never logs
- the literal 'IOWA STATE UNIVERSITY' or any relevance keyword
- which USAspending agencies matter to a given brief (NIFA, DOE, DOD); it filters by what it is told
- retry, backoff, or the silent-source alert; those are the ingest loop's job

## Public API
```python
AWARD_TYPE_GROUPS: dict[str, tuple[str, ...]]  # {'grants': ('02','03','04','05'), 'contracts': ('A','B','C','D'), 'loans': ('07','08'), 'other': ('06','09','10','11')}
DEFAULT_BASE_URL: str = 'https://api.usaspending.gov'
class UsaspendingError(RuntimeError): ...
def build_request(*, start_date: str, end_date: str, award_type_codes: Sequence[str], recipient: str | None = None, keywords: Sequence[str] | None = None, page: int = 1, limit: int = 100) -> dict
def normalize_award(record: dict, *, award_type_group: str, site_url: str = 'https://www.usaspending.gov') -> dict
def search_awards(*, start_date: str, end_date: str, recipient: str | None = None, keywords: Sequence[str] | None = None, award_types: Sequence[str] = ('grants', 'contracts'), base_url: str = DEFAULT_BASE_URL, timeout_s: float = 30.0, max_pages: int = 10) -> list[dict]
```

## Test harness
stub_http

## Approved module docstring (write this verbatim into the module)
```
Search USAspending.gov awards for a recipient or a keyword set within a date range.

Wraps POST /api/v2/search/spending_by_award/, the only public federal source that
covers USDA/NIFA, DOE, and DOD awards alongside NSF and NIH. USAspending refuses a
request that mixes award type groups (contracts A-D vs grants 02-05), so
``search_awards`` issues one paged request per requested group and concatenates.
Time filtering is on ``action_date``, which is what "new this week" means to a
reader; awards lag their announcement by weeks, and this module does not hide that.

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
```

## Prior art
verdict: seed

No feature in the library fetches external records from a public API; the library is PDF, LLM-provider, config, and secrets tooling. The only part-level overlap is the urllib POST idiom in ollama-local-llm's `_post()`, six lines that any Python author would write the same way, so copying it would record provenance for nothing. The stub_http harness is the real prior art and is a template meant to be vendored into tests/harness/. Seed fresh, stdlib only, with base_url injection so stub_http drives the tests.

## Critic verdict: keep
One endpoint; the per-type-group loop is a required path of the API, not a feature; normalization is the return shape.

### Boundary fixes to apply at write time
- title synthesized as 'recipient + agency + amount' is presentation policy; return description (fallback: award id) as title and let the adapter compose a richer one, or take title_of: Callable[[dict], str] | None
- time_period filtering hardcodes action_date; expose date_type: Literal['action_date','new_awards_only','last_modified_date'] = 'action_date' — new_awards_only is literally the spec's intent
- published_at = 'Start Date' while the filter is on action_date; document which date is which, and request the fields list explicitly in build_request (pin it in the golden) since default fields vary
- AWARD_TYPE_GROUPS default ('grants','contracts') is a policy default; fine as a parameter, but the adapter should pass it from sources.yaml params rather than relying on the seed default
- amount float|None: say 'JSON value verbatim' like the other two
