# Seed boundary: nih-reporter-search

## Purpose
Search NIH RePORTER API v2 for projects matching an institution or a text query within a project-start window, returning each hit as a normalized plain dict alongside its raw record.

## when_to_use (draft for FEATURE.toml)
You need this week's new NIH projects for one institution, or for a field regardless of institution, and want them as plain dicts with paging and the 15,000-record ceiling already handled rather than a hand-rolled POST to RePORTER.

## Inputs
- org_names: list[str] | None — exact RePORTER organization names (RePORTER matches case-insensitively; pass them as the profile writes them)
- advanced_text_search: str | None — free text searched across project title, terms and abstract (operator=and, search_field=projecttitle,terms,abstracttext)
- start_from / start_to: str (YYYY-MM-DD) — inclusive project_start_date window; the caller converts its own datetime
- base_url: str — defaults to https://api.reporter.nih.gov; tests point it at the stub
- page_size: int (<=500), max_records: int (<=15000) — paging bounds
- timeout_s: float, user_agent: str, sleep: Callable[[float], None] — network plumbing, injectable for tests
- extra_criteria: dict | None — passthrough merged into the RePORTER `criteria` object for fields the seed does not model (agencies, fiscal_years, include_active_projects)

## Outputs
- list[dict], one per project, newest project_start_date first, each with keys: external_id (project_num), appl_id, url (project_detail_url or https://reporter.nih.gov/project-details/<appl_id>), title, body (abstract_text, may be empty), published_at (award_notice_date, else project_start_date, ISO date), start_date, end_date, org_name, pi_names (list[str] full_name), agency (agency_ic_admin.name), amount (award_amount, int|None), fiscal_year, raw (the untouched RePORTER record)
- build_criteria() returns the exact JSON-ready `criteria` dict so callers can log or hash the query
- ReporterError (RuntimeError subclass) on non-2xx, malformed JSON, or a page whose `results` key is missing; never a silent empty list on failure

## Dependencies
- **urllib.request / json / time (stdlib)** — A single POST with a JSON body and a JSON reply needs nothing beyond stdlib; `requests` would be the module's only third-party import and would block graduation to the library's zero-dep tier. The same urllib POST shape already lives in ollama-local-llm/provider.py `_post()`.

## Must NOT know about
- brief config.yaml / sources.yaml / profiles — the seed takes strings and lists, never a Profile or a source-params dict shaped by the app
- the Item dataclass — returns plain dicts; nih.py adapter maps them to Item
- SQLite, content_hash, external_id dedup — storage is the adapter's and db.py's concern
- logging setup — raises ReporterError; the ingest loop decides to log-and-continue
- hardcoded paths or dotenv/secrets — RePORTER needs no auth; there is nothing to read from the environment
- the profile relevance schema (any_of/none_of) — filtering is downstream
- `since: datetime` semantics — the adapter turns its datetime into start_from/start_to date strings
- the ~1 request/second RePORTER courtesy limit is honored by a sleep between pages, but the seed does not know the app's cron cadence or retry policy

## Public API
```python
class ReporterError(RuntimeError): ...
def build_criteria(*, org_names: list[str] | None = None, advanced_text_search: str | None = None, start_from: str | None = None, start_to: str | None = None, extra_criteria: dict | None = None) -> dict
def search_projects(criteria: dict, *, base_url: str = "https://api.reporter.nih.gov", page_size: int = 500, max_records: int = 15_000, timeout_s: float = 30.0, user_agent: str = "nih-reporter-search/0.1", sleep: Callable[[float], None] = time.sleep) -> list[dict]
def normalize_project(record: dict, *, detail_url_base: str = "https://reporter.nih.gov/project-details/") -> dict
def fetch_projects(*, org_names: list[str] | None = None, advanced_text_search: str | None = None, start_from: str | None = None, start_to: str | None = None, base_url: str = "https://api.reporter.nih.gov", page_size: int = 500, max_records: int = 15_000, timeout_s: float = 30.0, sleep: Callable[[float], None] = time.sleep) -> list[dict]
```

## Test harness
stub_http

## Approved module docstring (write this verbatim into the module)
```
Search NIH RePORTER API v2 and return projects as plain dicts.

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
``ReporterError``; an empty list means the search genuinely matched nothing.

Deliberately not done: no ``since`` datetime arithmetic (pass date strings),
no relevance filtering, no dedup, no storage, no logging, no retries — the
caller owns cadence and failure policy. No auth: RePORTER is public.

Dependencies: none. ``urllib.request`` + ``json`` cover a JSON POST; taking
``requests`` would make this the module's only third-party import for no gain.
Tests run against a real stub HTTP server (``tests/harness/stub_http.py``),
never a mocked ``urlopen``.
```

## Prior art
verdict: copy-part-then-seed

No whole feature searches an award database or wraps a generic JSON POST client; the library's only HTTP client is ollama-local-llm. Copy the 7-line urllib POST shape from features/python/ollama-local-llm/src/ollama_local_llm/provider.py `_post()` (JSON body, Content-Type header, timeout) into the seed rather than importing it (rule 2: parts are copied, not imported), and vendor templates/python-feature/tests/harness/stub_http.py into tests/harness/. Everything RePORTER-specific — criteria shape, meta.total paging, the 500/15,000 caps, the courtesy sleep, record normalization — is new and is the seed.

## Critic verdict: keep
One POST endpoint with paging and a fixed output shape; org_names vs advanced_text_search are two filter shapes for the same criteria object.

### Boundary fixes to apply at write time
- public_api has two entry points (search_projects(criteria) and fetch_projects(*, org_names...)) and fetch_projects lacks user_agent; keep build_criteria + search_projects, drop fetch_projects or make it a 3-line passthrough with the same kwargs
- the ~1 req/s courtesy delay is baked in as a fixed sleep; expose page_delay_s: float = 1.0 (sleep callable already injected)
- operator='and' and search_field='projecttitle,terms,abstracttext' are hardcoded inside build_criteria; expose as parameters with those defaults
- add date_field: Literal['project_start_date','award_notice_date'] = 'project_start_date' — award_notice_date is what 'newly funded this week' actually means; the spec's project_start_date is a choice the adapter should be able to make
- rename ReporterError -> NIHReporterError (consistency with the other fetchers; 'Reporter' alone is ambiguous in a repo full of reports)
- amount typed int|None converts nothing (JSON int) — fine, but say 'verbatim JSON value' to match the NSF/USAspending rule
