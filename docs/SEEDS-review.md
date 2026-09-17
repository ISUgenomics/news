# Seed boundaries for review
Drafted by the design workflow; critic fixes listed under each seed will be applied at write time.

## feed-fetch  —  critic: split
**Purpose.** Fetch an RSS/Atom feed by URL with conditional GET and a browser-like User-Agent, returning its entries as plain dicts plus the validators to send next time.
**when_to_use.** You poll public RSS or Atom feeds on a schedule and need unchanged feeds to cost one cheap request, bot-hostile sites to answer, and entries handed back as plain dicts your own store can keep.
**Inputs.** url: str — the feed URL, fully resolved (secrets already substituted by the caller); etag: str | None — validator from the previous successful fetch, or None on first fetch; last_modified: str | None — HTTP-date string from the previous fetch, or None; user_agent: str — defaults to a browser-like DEFAULT_USER_AGENT constant; caller may override; timeout_s: float — socket timeout, default 30; open_url: Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]] | None — optional transport override; default uses urllib. Exists so a caller can route through a Playwright fetcher later without changing this module
**Outputs.** dict {"status": int, "etag": str | None, "last_modified": str | None, "entries": list[dict]} — status is 200 (or 304 with entries == [] and the input validators echoed back); each entry dict: {"id": str | None, "url": str, "title": str, "summary": str, "published_at": str | None} — summary is HTML-stripped plain text; published_at is ISO 8601 UTC or None; id is the feed GUID or None; url falls back to id when link is absent; raises FeedFetchError (subclass of RuntimeError) on network failure, HTTP status other than 200/304, or a body feedparser cannot parse into any entries; the message names the URL and the cause
**Dependencies.** feedparser: One parser for RSS 0.9x/1.0/2.0 and Atom with tolerant handling of malformed XML and normalized date parsing (published_parsed). Hand-rolling this with ElementTree is the classic trap: every campus CMS emits a slightly different dialect. Used only on bytes already fetched; its own HTTP path is not used, so UA, validators, and timeouts stay under this module's control and testable against stub_http.; urllib.request / html.parser / email.utils / calendar (stdlib): HTTP with explicit headers, HTML tag stripping for summaries, and date conversion. No requests/httpx: nothing here needs sessions, pooling, or retries.
**Must not know.** config.yaml, sources.yaml, or any profile — it receives one URL and the validators, nothing named; the Item dataclass or brief.models — it returns plain dicts; the adapter maps them; SQLite or where validators are persisted — etag/last_modified go in and come out as strings; logging configuration — it raises or returns; the caller logs; hardcoded feed URLs, tokens, or ${CD_TOKEN} substitution; the profile schema, relevance keywords, or which profiles use the feed; the 200-character expansion threshold and trafilatura — that is the page-main-text sibling and the adapter's policy; the 8 kB body cap and content_hash — storage concerns owned by db.py
**Public API.**
```python
DEFAULT_USER_AGENT: str  # a current desktop-browser UA string, one constant, overridable per call
class FeedFetchError(RuntimeError): ...
def fetch_feed(url: str, *, etag: str | None = None, last_modified: str | None = None, user_agent: str = DEFAULT_USER_AGENT, timeout_s: float = 30.0, open_url: Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]] | None = None) -> dict[str, Any]
def parse_feed_bytes(body: bytes, *, base_url: str = "") -> list[dict[str, Any]]  # pure; the entry normalization on already-fetched bytes, exposed so fixture tests need no socket
def strip_html(text: str) -> str  # stdlib html.parser; collapses whitespace, decodes entities
```
**Harness.** stub_http  ·  **Prior art.** seed: No match at the feature or part layer. All four queries returned only the PDF, LLM-provider, and desktop-app features (pdf-garbage-text-sweeps, localhost-web-guard, cli-llm-providers, secret-redaction, etc.) on incidental token overlap (page, feed, request, changed); none fetches HTTP, parses feeds, or handles cache validators. The parts layer surfaced only PDF helpers and the ollama-local-llm test conftest's requests() property. The 45 features under /Users/andrewseverin/AI/codeLibrary/features/python/ contain no HTTP client, feed, or scraping feature. The one real hit is the harness: stub_ht
**Docstring.**
```
Fetch one RSS or Atom feed and hand back its entries as plain dicts.

Situation: a scheduled poller hits the same public feeds every day. Most days
nothing changed, and some sites (inside.iastate.edu among them) answer a
default Python User-Agent with "Request Rejected". This module does the one
HTTP request correctly: it sends a browser-like User-Agent, sends
If-None-Match / If-Modified-Since when the caller supplies the previous
ETag / Last-Modified, and returns those validators so the caller can store
them wherever it likes.

Contract: ``fetch_feed(url, etag=None, last_modified=None, ...)`` returns
``{"status", "etag", "last_modified", "entries"}``. A 304 yields status 304
and an empty entry list; the caller treats that as "nothing new". Each entry
is ``{"id", "url", "title", "summary", "published_at"}`` with the summary
HTML-stripped to text and ``published_at`` as ISO 8601 UTC or None. Any
network error, non-200/304 status, or a body with no parseable entries
raises ``FeedFetchError`` naming the URL and cause; the caller decides
whether to log and continue.

Deliberately not here: fetching linked article pages or extracting main
text (see the page-main-text seed), persistence of validators, retry or
backoff, body-length caps, hashing, dedup, and any notion of a source name,
profile, or relevance. This module knows a URL and the last validators.

Dependencies: ``feedparser`` for dialect-tolerant parsing of bytes already
fetched (its own HTTP path is bypassed so headers stay ours). Everything
else is stdlib. The optional ``open_url`` callable is the transport seam:
tests hit a real stub HTTP server through the default urllib path, and a
future Playwright-backed fetcher can be dropped in without touching parsing.
```
**Critic.** The drafted boundary is already the post-split half (feedparser only) and is one job. Accept the split; the other half, page-main-text (trafilatura), is not in the list yet and must be seeded alongside or sources/rss.py will inline it.
- fix: open_url seam is typed on urllib.request.Request, which forces a future Playwright/requests transport to build a urllib object; type it as Callable[[str, Mapping[str,str], float], tuple[int, Mapping[str,str], bytes]] (url, headers, timeout) and build the Request inside the default only
- fix: docstring names inside.iastate.edu; replace with 'some campus CMSs' (domain leak into a module that will graduate)
- fix: published_at: fall back to updated_parsed when published_parsed is absent (Atom feeds often carry only <updated>); state that in the contract
- fix: 304 path: return the input validators, not None, so the caller's store is not overwritten with nulls (draft says so; make it a test)
- fix: entry dict is missing content the adapter needs for raw_json (spec: raw_json 'for APIs' so acceptable); confirm raw is intentionally omitted or add raw: dict of the feedparser entry

## nsf-award-search  —  critic: keep
**Purpose.** Fetch NSF awards matching an awardee, keyword, or PI-name filter within an award-date range, returned as normalized plain dicts that each carry the raw record.
**when_to_use.** You need the list of NSF awards to an institution, on a topic, or to a named PI since a date, in a shape you can store, and do not want to learn the Award Search API's field names, date format, or 25-per-page offset walk.
**Inputs.** awardee: str | None — institution name, matched by NSF's awardeeName filter; keyword: str | None — free text; NSF's keyword filter searches titles and abstracts, supports trailing * wildcard; pi_name: str | None — passed as NSF's pdPIName filter; date_start: datetime.date — inclusive lower bound on award date; date_end: datetime.date | None — inclusive upper bound; None means today (UTC); base_url: str — defaults to https://api.nsf.gov/services/v1/awards.json; tests point it at the stub server; timeout_s: float — per-request socket timeout, default 30; max_pages: int — pagination safety cap, default 40 (1000 records at rpp=25); user_agent: str — sent on every request; NSF rejects empty agents intermittently
**Outputs.** list[dict] in API order, one dict per award, with fixed keys: external_id (NSF award id, str), url (https://www.nsf.gov/awardsearch/showAward?AWD_ID=<id>), title, body (abstractText, may be empty), published_at (award date as ISO-8601 YYYY-MM-DD or None), pi_name (first + last), awardee, amount (fundsObligatedAmt verbatim as str, never converted), agency, program, raw (the untouched API record); NSFSearchError(RuntimeError) on HTTP failure, non-JSON body, or an error payload from the API; ValueError when no filter is given or date_end < date_start; build_query_url(...) -> str, a pure function so the exact query string is pinned by golden tests; normalize_award(raw: dict) -> dict, pure, tolerant of missing fields (never KeyError; missing -> '' or None)
**Dependencies.** none
**Must not know.** config.yaml, sources.yaml, or the profile schema — the caller unpacks the profile's {awardee|keyword|pi_name} into keyword arguments; the Item dataclass or brief.models — returns plain dicts; brief/sources/nsf.py does the Item mapping; SQLite, db.py, content_hash, INSERT OR IGNORE, or the fetched_at/since semantics of brief ingest — the caller derives date_start from since; logging configuration — raises exceptions and returns values; no logger of its own; hardcoded ISU, 'Iowa State University', AI keywords, or any topic vocabulary; relevance filtering, ranking, or dedup across sources; the changedetection.io feed, RSS, NIH RePORTER, or USAspending shapes — one endpoint only
**Public API.**
```python
def search_nsf_awards(*, awardee: str | None = None, keyword: str | None = None, pi_name: str | None = None, date_start: datetime.date, date_end: datetime.date | None = None, base_url: str = "https://api.nsf.gov/services/v1/awards.json", timeout_s: float = 30.0, max_pages: int = 40, user_agent: str = "nsf-award-search/0.1") -> list[dict]
def build_query_url(base_url: str, *, awardee: str | None, keyword: str | None, pi_name: str | None, date_start: datetime.date, date_end: datetime.date, offset: int = 1, rpp: int = 25) -> str
def normalize_award(raw: dict) -> dict
class NSFSearchError(RuntimeError): ...
```
**Harness.** stub_http  ·  **Prior art.** seed: Four phrasings, zero whole-feature or part-level hits for award/grants search, paginated JSON GET, or record normalization; the library has 44 python features and none talks to a public data API. The only reusable material is the stub_http harness template (copy into tests/harness/) and the urllib-only convention that ollama-local-llm establishes. Seed it new.
**Docstring.**
```
Query the NSF Award Search API and return awards as plain dicts.

Situation: a poller or brief needs "every NSF award to institution X",
"every award mentioning Y", or "every award to PI Z" since a date, and
wants the result in a stable shape it can store without learning NSF's
field names (awardeeName, pdPIName, fundsObligatedAmt, MM/DD/YYYY dates).

Contract: search_nsf_awards() takes explicit values only — at least one
of awardee / keyword / pi_name, a date_start (inclusive, on award date),
an optional date_end, and a base_url that defaults to api.nsf.gov and is
the seam tests point at a stub server. It walks the API's offset/rpp
pagination (rpp=25, offset 1-based) until a short page or max_pages,
and returns one dict per award with fixed keys: external_id, url, title,
body, published_at (ISO date), pi_name, awardee, amount, agency, program,
raw. amount is the API's string verbatim; nothing is parsed into a number
because a brief quotes dollar figures, it never computes them. raw is the
untouched record so a caller can store it and re-normalize later.
build_query_url() and normalize_award() are pure and exported so the
query string and the field mapping are pinned by fixtures.

Deliberately not done: no retry or backoff (the caller's schedule owns
that); no mapping to an app dataclass; no dedup, ranking, or relevance;
no caching; no logging. Errors surface as NSFSearchError (network, HTTP,
non-JSON, API error payload) or ValueError (bad arguments).

Dependencies: none. urllib.request + json + datetime, matching the
library's ollama-local-llm convention; one paginated GET does not earn
requests. Tests use the copied tests/harness/stub_http.py, never a mock.
```
**Critic.** One endpoint, one paged GET, one normalized shape; pagination and normalize_award are the mechanism and the return contract, not second jobs.
- fix: build_query_url must set printFields explicitly: the NSF v1 default field set omits abstractText (and pdPIName, fundsObligatedAmt in some responses); without it body is always empty. Verify against the live API before pinning the golden URL
- fix: date_end=None -> 'today (UTC)' reads the clock inside a pure-looking seed; keep the convenience but route it through a today: date | None parameter (or document that tests must pass date_end) so goldens are deterministic
- fix: NSF dateStart/dateEnd filter on award effective date, not 'award date'; name the field the seed filters on in the docstring so 'new this week' semantics are honest
- fix: Rename NSFSearchError -> keep, but align naming across the four fetcher seeds (<Source>SearchError, search_<source>(), build_*, normalize_*) so sources/*.py adapters are copy-paste identical
- fix: amount: state 'JSON value verbatim, no conversion' (str here, number elsewhere) as the shared rule for all three award seeds

## nih-reporter-search  —  critic: keep
**Purpose.** Search NIH RePORTER API v2 for projects matching an institution or a text query within a project-start window, returning each hit as a normalized plain dict alongside its raw record.
**when_to_use.** You need this week's new NIH projects for one institution, or for a field regardless of institution, and want them as plain dicts with paging and the 15,000-record ceiling already handled rather than a hand-rolled POST to RePORTER.
**Inputs.** org_names: list[str] | None — exact RePORTER organization names (RePORTER matches case-insensitively; pass them as the profile writes them); advanced_text_search: str | None — free text searched across project title, terms and abstract (operator=and, search_field=projecttitle,terms,abstracttext); start_from / start_to: str (YYYY-MM-DD) — inclusive project_start_date window; the caller converts its own datetime; base_url: str — defaults to https://api.reporter.nih.gov; tests point it at the stub; page_size: int (<=500), max_records: int (<=15000) — paging bounds; timeout_s: float, user_agent: str, sleep: Callable[[float], None] — network plumbing, injectable for tests; extra_criteria: dict | None — passthrough merged into the RePORTER `criteria` object for fields the seed does not model (agencies, fiscal_years, include_active_projects)
**Outputs.** list[dict], one per project, newest project_start_date first, each with keys: external_id (project_num), appl_id, url (project_detail_url or https://reporter.nih.gov/project-details/<appl_id>), title, body (abstract_text, may be empty), published_at (award_notice_date, else project_start_date, ISO date), start_date, end_date, org_name, pi_names (list[str] full_name), agency (agency_ic_admin.name), amount (award_amount, int|None), fiscal_year, raw (the untouched RePORTER record); build_criteria() returns the exact JSON-ready `criteria` dict so callers can log or hash the query; ReporterError (RuntimeError subclass) on non-2xx, malformed JSON, or a page whose `results` key is missing; never a silent empty list on failure
**Dependencies.** urllib.request / json / time (stdlib): A single POST with a JSON body and a JSON reply needs nothing beyond stdlib; `requests` would be the module's only third-party import and would block graduation to the library's zero-dep tier. The same urllib POST shape already lives in ollama-local-llm/provider.py `_post()`.
**Must not know.** brief config.yaml / sources.yaml / profiles — the seed takes strings and lists, never a Profile or a source-params dict shaped by the app; the Item dataclass — returns plain dicts; nih.py adapter maps them to Item; SQLite, content_hash, external_id dedup — storage is the adapter's and db.py's concern; logging setup — raises ReporterError; the ingest loop decides to log-and-continue; hardcoded paths or dotenv/secrets — RePORTER needs no auth; there is nothing to read from the environment; the profile relevance schema (any_of/none_of) — filtering is downstream; `since: datetime` semantics — the adapter turns its datetime into start_from/start_to date strings; the ~1 request/second RePORTER courtesy limit is honored by a sleep between pages, but the seed does not know the app's cron cadence or retry policy
**Public API.**
```python
class ReporterError(RuntimeError): ...
def build_criteria(*, org_names: list[str] | None = None, advanced_text_search: str | None = None, start_from: str | None = None, start_to: str | None = None, extra_criteria: dict | None = None) -> dict
def search_projects(criteria: dict, *, base_url: str = "https://api.reporter.nih.gov", page_size: int = 500, max_records: int = 15_000, timeout_s: float = 30.0, user_agent: str = "nih-reporter-search/0.1", sleep: Callable[[float], None] = time.sleep) -> list[dict]
def normalize_project(record: dict, *, detail_url_base: str = "https://reporter.nih.gov/project-details/") -> dict
def fetch_projects(*, org_names: list[str] | None = None, advanced_text_search: str | None = None, start_from: str | None = None, start_to: str | None = None, base_url: str = "https://api.reporter.nih.gov", page_size: int = 500, max_records: int = 15_000, timeout_s: float = 30.0, sleep: Callable[[float], None] = time.sleep) -> list[dict]
```
**Harness.** stub_http  ·  **Prior art.** copy-part-then-seed: No whole feature searches an award database or wraps a generic JSON POST client; the library's only HTTP client is ollama-local-llm. Copy the 7-line urllib POST shape from features/python/ollama-local-llm/src/ollama_local_llm/provider.py `_post()` (JSON body, Content-Type header, timeout) into the seed rather than importing it (rule 2: parts are copied, not imported), and vendor templates/python-feature/tests/harness/stub_http.py into tests/harness/. Everything RePORTER-specific — criteria shape, meta.total paging, the 500/15,000 caps, the courtesy sleep, record normalization — is new and is t
**Docstring.**
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
**Critic.** One POST endpoint with paging and a fixed output shape; org_names vs advanced_text_search are two filter shapes for the same criteria object.
- fix: public_api has two entry points (search_projects(criteria) and fetch_projects(*, org_names...)) and fetch_projects lacks user_agent; keep build_criteria + search_projects, drop fetch_projects or make it a 3-line passthrough with the same kwargs
- fix: the ~1 req/s courtesy delay is baked in as a fixed sleep; expose page_delay_s: float = 1.0 (sleep callable already injected)
- fix: operator='and' and search_field='projecttitle,terms,abstracttext' are hardcoded inside build_criteria; expose as parameters with those defaults
- fix: add date_field: Literal['project_start_date','award_notice_date'] = 'project_start_date' — award_notice_date is what 'newly funded this week' actually means; the spec's project_start_date is a choice the adapter should be able to make
- fix: rename ReporterError -> NIHReporterError (consistency with the other fetchers; 'Reporter' alone is ambiguous in a repo full of reports)
- fix: amount typed int|None converts nothing (JSON int) — fine, but say 'verbatim JSON value' to match the NSF/USAspending rule

## usaspending-award-search  —  critic: keep
**Purpose.** Fetch USAspending.gov award records for one recipient or one keyword set within a date range, returned as normalized plain dicts that keep the raw record.
**when_to_use.** You need federal award records for one institution or one research topic over a date range, including the agencies the NSF and NIH APIs do not cover, and you want plain dicts to store rather than a lesson in USAspending's filter grammar.
**Inputs.** start_date: str (ISO date, inclusive; filters on action_date, which is what 'new this week' means); end_date: str (ISO date, inclusive); recipient: str | None (recipient_search_text; e.g. an institution name); keywords: list[str] | None (free-text match over award descriptions; the field-profile path); award_types: Sequence[str] = ('grants', 'contracts') (names of USAspending type groups; one request per group); base_url: str = 'https://api.usaspending.gov' (injected so tests point at stub_http); timeout_s: float = 30.0; max_pages: int = 10 (hard stop; 100 records per page)
**Outputs.** list[dict], one per award, each with keys: external_id (Award ID), internal_id (generated_internal_id), url (https://www.usaspending.gov/award/<internal_id>), title (recipient + agency + amount, or description head), body (description), published_at (Start Date, ISO), amount (float | None), recipient, awarding_agency, awarding_sub_agency, award_type_group, raw (the untouched API record); Raises UsaspendingError (RuntimeError subclass) on non-2xx, unreachable host, timeout, or a response missing 'results'; never returns partial silently; build_request() and normalize_award() are pure and importable on their own for fixture-driven tests
**Dependencies.** stdlib only (urllib.request, json, datetime): The API is one JSON POST with a page loop; urllib does that in a dozen lines, the same idiom ollama-local-llm already uses, and a zero-dependency seed graduates without a pin.
**Must not know.** config.yaml, sources.yaml, or the profile schema (recipient/keywords arrive as explicit arguments, never a params dict keyed by profile field names); the Item dataclass or any brief.models import; the adapter converts dicts to Item; the poller's `since: datetime` contract; the seed takes ISO date strings; SQLite, items.db, content_hash, or any dedup rule; logging configuration; it raises or returns, never logs; the literal 'IOWA STATE UNIVERSITY' or any relevance keyword; which USAspending agencies matter to a given brief (NIFA, DOE, DOD); it filters by what it is told; retry, backoff, or the silent-source alert; those are the ingest loop's job
**Public API.**
```python
AWARD_TYPE_GROUPS: dict[str, tuple[str, ...]]  # {'grants': ('02','03','04','05'), 'contracts': ('A','B','C','D'), 'loans': ('07','08'), 'other': ('06','09','10','11')}
DEFAULT_BASE_URL: str = 'https://api.usaspending.gov'
class UsaspendingError(RuntimeError): ...
def build_request(*, start_date: str, end_date: str, award_type_codes: Sequence[str], recipient: str | None = None, keywords: Sequence[str] | None = None, page: int = 1, limit: int = 100) -> dict
def normalize_award(record: dict, *, award_type_group: str, site_url: str = 'https://www.usaspending.gov') -> dict
def search_awards(*, start_date: str, end_date: str, recipient: str | None = None, keywords: Sequence[str] | None = None, award_types: Sequence[str] = ('grants', 'contracts'), base_url: str = DEFAULT_BASE_URL, timeout_s: float = 30.0, max_pages: int = 10) -> list[dict]
```
**Harness.** stub_http  ·  **Prior art.** seed: No feature in the library fetches external records from a public API; the library is PDF, LLM-provider, config, and secrets tooling. The only part-level overlap is the urllib POST idiom in ollama-local-llm's `_post()`, six lines that any Python author would write the same way, so copying it would record provenance for nothing. The stub_http harness is the real prior art and is a template meant to be vendored into tests/harness/. Seed fresh, stdlib only, with base_url injection so stub_http drives the tests.
**Docstring.**
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
**Critic.** One endpoint; the per-type-group loop is a required path of the API, not a feature; normalization is the return shape.
- fix: title synthesized as 'recipient + agency + amount' is presentation policy; return description (fallback: award id) as title and let the adapter compose a richer one, or take title_of: Callable[[dict], str] | None
- fix: time_period filtering hardcodes action_date; expose date_type: Literal['action_date','new_awards_only','last_modified_date'] = 'action_date' — new_awards_only is literally the spec's intent
- fix: published_at = 'Start Date' while the filter is on action_date; document which date is which, and request the fields list explicitly in build_request (pin it in the golden) since default fields vary
- fix: AWARD_TYPE_GROUPS default ('grants','contracts') is a policy default; fine as a parameter, but the adapter should pass it from sources.yaml params rather than relying on the seed default
- fix: amount float|None: say 'JSON value verbatim' like the other two

## pubmed-search-feed  —  critic: keep
**Purpose.** Turn a PubMed query string into a list of plain article dicts keyed by PMID, using NCBI E-utilities (esearch + esummary JSON).
**when_to_use.** You have a PubMed search expression and want the matching articles as plain records with their PMIDs, without a browser session, a saved-search feed id, or a login.
**Inputs.** query: str — a PubMed search expression, e.g. 'maize[Title] AND (genome OR GWAS)'; since: datetime | None — lower bound on Entrez date (edat); converted to reldate days; None means no date filter; max_results: int = 200 — esearch retmax; also caps esummary batch; base_url: str = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils' — overridable so stub_http can serve it; tool: str, email: str — NCBI etiquette parameters sent on every request; caller-supplied strings; api_key: str | None — optional NCBI key; raises the rate limit from 3 to 10 req/s, never read from env by the seed; timeout_s: float = 30.0; user_agent: str | None — optional header override
**Outputs.** list[dict] — one dict per PMID, keys: external_id (PMID as str), url ('https://pubmed.ncbi.nlm.nih.gov/<pmid>/'), title, body (journal, authors, volume/pages, DOI when present, joined as one plain-text line), published_at (ISO 8601 date or None, parsed from esummary pubdate/epubdate), raw (the esummary record dict, for the caller's raw_json column); Empty list when esearch returns zero ids; Raises RuntimeError with the HTTP status or NCBI error string on transport failure or an 'error'/'ERROR' key in either JSON envelope; never returns partial results silently
**Dependencies.** urllib.request / urllib.parse (stdlib): two GETs against E-utilities; base_url is a parameter so the stub_http harness exercises the real socket path; json (stdlib): esearch and esummary both answer retmode=json; no XML parsing needed; datetime (stdlib): since -> reldate day count; pubdate strings ('2026 Sep 3', '2026 Sep', '2026') -> ISO date; feedparser — NOT used: the E-utilities path returns JSON, and the only RSS PubMed offers needs a server-minted feed id; a minted feed is a plain rss source, not this seed
**Must not know.** the app's config.yaml / sources.yaml / profile YAML or the profile schema (query, since, tool, email, api_key arrive as explicit arguments); the Item dataclass or models.py — it returns plain dicts the adapter maps onto Item; SQLite, db.py, content_hash, fetched_at — it never writes or hashes; logging setup — it raises; the caller logs; hardcoded paths, dotenv, os.environ — api_key is a parameter, never looked up; the relevance any_of/none_of filter — everything esearch returns comes back; filtering is downstream; feedparser and the rss adapter
**Public API.**
```python
def search(query: str, *, since: datetime | None = None, max_results: int = 200, base_url: str = DEFAULT_BASE_URL, tool: str = 'pubmed-search-feed', email: str = '', api_key: str | None = None, timeout_s: float = 30.0, user_agent: str | None = None) -> list[dict]
def esearch_ids(query: str, *, since: datetime | None = None, max_results: int = 200, base_url: str = DEFAULT_BASE_URL, tool: str = 'pubmed-search-feed', email: str = '', api_key: str | None = None, timeout_s: float = 30.0, user_agent: str | None = None) -> list[str]
def esummary_records(pmids: list[str], *, base_url: str = DEFAULT_BASE_URL, tool: str = 'pubmed-search-feed', email: str = '', api_key: str | None = None, timeout_s: float = 30.0, user_agent: str | None = None) -> list[dict]
def record_to_item(record: dict) -> dict  # pure: esummary record -> {external_id, url, title, body, published_at, raw}
def parse_pubdate(text: str) -> str | None  # pure: '2026 Sep 3' | '2026 Sep' | '2026' -> ISO date or None
DEFAULT_BASE_URL: str = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'
```
**Harness.** stub_http  ·  **Prior art.** seed: Nothing in the library fetches a feed, talks to NCBI, or parses RSS/Atom; all whole-feature and part hits are PDF, RAG, or transcript keyword noise (max overlap 3 on the two relevant phrasings, 0 on the E-utilities phrasing). No feature to use and no function to copy. The only reusable thing is the stub_http harness, which fits the seed's two-GET transport exactly (route /esearch.fcgi and /esummary.fcgi, assert on stub_http.requests[-1].path and query). Seed from scratch, stdlib only. Open risk to confirm before writing: my claim that PubMed's saved-search RSS id is server-minted and not query
**Docstring.**
```
PubMed search to plain article dicts via NCBI E-utilities.

Given a PubMed query string, returns one dict per matching article, keyed by
PMID, using two public JSON calls: ``esearch`` (term + optional reldate) to get
PMIDs, then ``esummary`` to get title, journal, authors, dates and DOI. No
login, no API key required (3 req/s unkeyed; pass ``api_key`` for 10).

Contract: ``search(query, since=..., base_url=..., tool=..., email=...)`` ->
``list[dict]`` with keys ``external_id`` (PMID str), ``url``, ``title``,
``body`` (one plain-text citation line), ``published_at`` (ISO date or None)
and ``raw`` (the esummary record). Zero hits -> ``[]`` without a second call.
Any HTTP failure or an ``error`` key in either envelope -> ``RuntimeError``
naming the status or NCBI message; never a silent partial list.

Deliberately not done: building a PubMed saved-search RSS URL. PubMed mints
that feed id server-side when a user clicks "Create RSS"; it cannot be derived
from the query, and a minted feed URL is an ordinary RSS source anyway. Also
not done: relevance filtering, dedup, hashing, storage, logging, reading
config or the environment. Those belong to the caller.

Dependencies: stdlib only (``urllib``, ``json``, ``datetime``). ``base_url``
is a parameter so tests run against a real stub HTTP server on an ephemeral
port, never a mocked ``urllib``.
```
**Critic.** The E-utilities rewrite is correct: PubMed saved-search RSS ids are minted server-side (rss/search/<hex>/), not derivable from a query; a minted URL is an ordinary rss source. The redrawn boundary is one job (esearch -> esummary -> dicts). The spec's line 139/151 ('query string becomes the feed URL') needs correcting.
- fix: rename slug: the '-feed' suffix now lies; use pubmed-search (or pubmed-esearch)
- fix: since: datetime -> reldate days reads the clock inside the seed; use E-utilities mindate/maxdate (YYYY/MM/DD) with datetype='edat' as explicit parameters and let the adapter convert its datetime — deterministic and pinnable
- fix: body composed as 'one plain-text citation line' is presentation policy; return journal, authors, volume, pages, doi as fields (plus raw) and expose citation_line(record) as a pure helper the adapter may call
- fix: email='' default: NCBI asks for a contact; make email required (no default) so a caller cannot forget it
- fix: esummary with >~200 ids should POST (URL length); state the limit or switch to POST when len(pmids) > 200
- fix: rename search() -> search_pubmed(); a bare search leaks into every namespace
- fix: must_not_know says 'never read from env' for api_key — good; also state tool/email are caller strings so no app name leaks into the default tool value ('pubmed-search-feed' as default is fine once renamed)

## keyword-relevance  —  critic: keep
**Purpose.** Keep the text records that pass any_of / none_of keyword lists, each tagged with its distinct-hit count.
**when_to_use.** You have a pile of fetched text records and a profile-style list of must-hit and must-not-hit terms, and need the survivors ordered by how strongly they hit before something expensive (an LLM call, a human) reads them.
**Inputs.** records: an iterable of any type T (the caller's rows, dataclasses, dicts); the module never inspects them; text_of: Callable[[T], str | None] that yields the text to match (the caller joins title + body); None is treated as empty; any_of: Iterable[str] of keep terms; single words or multi-word phrases; empty means the filter is off and every record survives with 0 hits; none_of: Iterable[str] of hard-exclude terms, checked before any_of; whole_word: bool = True; match at word boundaries so 'AI' does not hit 'said'; False falls back to plain substring matching
**Outputs.** filter_ranked -> list[tuple[T, int]]: surviving records paired with their distinct-hit count, sorted by hits descending with a stable sort (input order preserved within equal counts); score_text -> int | None: None when a none_of term hits or no any_of term hits (with a non-empty any_of); otherwise the number of distinct any_of terms found; compile_terms -> list[re.Pattern[str]]: case-insensitive patterns, one per distinct term, for callers that want to reuse a profile's compiled lists across many calls
**Dependencies.** none
**Must not know.** the Item dataclass or any brief.models type (records are opaque T reached only through text_of); the Profile dataclass or the profile YAML schema (relevance.any_of / none_of arrive as plain iterables of str); config.yaml, layered-config-overlay, or any app config object; SQLite, db.py, or how candidates were queried; published_at ordering, max_items, body caps, or the provider context budget (packing is the caller's job); logging setup; the function returns counts, the caller decides what to log; hardcoded paths, source names, or anything about Iowa State or AI
**Public API.**
```python
def compile_terms(terms: Iterable[str], *, whole_word: bool = True) -> list[re.Pattern[str]]
def score_text(text: str | None, any_of: Sequence[re.Pattern[str]] | Iterable[str], none_of: Sequence[re.Pattern[str]] | Iterable[str] = (), *, whole_word: bool = True) -> int | None
def filter_ranked(records: Iterable[T], text_of: Callable[[T], str | None], any_of: Iterable[str], none_of: Iterable[str] = (), *, whole_word: bool = True) -> list[tuple[T, int]]
```
**Harness.** none  ·  **Prior art.** seed: Nothing in the library does allowlist/denylist phrase filtering with a distinct-hit count. hybrid-rag-retrieval is the nearest neighbour but solves a different situation: ranking a corpus against a free-text query with IDF weights and a top_k that always returns something, whereas this seed needs literal multi-word phrases, a hard exclude list, whole-word matching so 'AI' does not hit 'said', and zero survivors when nothing matches. Its terms() helper word-tokenizes, which breaks phrase terms, so nothing is copied. The seed is ~40 lines on stdlib re.
**Docstring.**
```
Keyword relevance: keep the records that pass any_of / none_of term lists, tagged with a distinct-hit count.

Given opaque records and a callable that yields each one's text, drop every record that hits a none_of term, then keep the records that hit at least one any_of term, and return the survivors as (record, hits) pairs sorted by hits descending. Hits counts distinct terms matched, not occurrences: a story that says "machine learning" nine times and nothing else scores 1. The sort is stable, so a caller that pre-orders records (newest first, say) gets that order as the tie-break for free.

Contract:
- Matching is case-insensitive. Terms are deduplicated case-insensitively; blank terms are ignored.
- whole_word=True (default) matches at word boundaries via lookarounds, so "AI" does not hit "said" and "C++" still works; whole_word=False is plain substring matching.
- A run of whitespace inside a multi-word term matches any whitespace run in the text, so a phrase split across a line break still hits.
- none_of is checked first and is absolute; one hit removes the record regardless of any_of.
- An empty any_of turns the keep-filter off: every record not excluded survives with 0 hits. This is the switch for auditing prefilter false negatives on a sample.
- text_of may return None; it is treated as "".

Deliberately not done here: no ordering by date or any other record field, no body truncation, no item caps, no context-budget packing, no stemming or fuzzy matching (a term is a literal phrase; add plural forms to the list), no reading of profile or config objects.

Dependencies: none. Only the stdlib re module; patterns are compiled once per call, or once per profile via compile_terms when the caller wants to reuse them.
```
**Critic.** One job (filter with a hit count; the sort is a stable sort of the count already computed). Opaque T + text_of keeps Item out. Small but not trivial: whole-word lookarounds, phrase whitespace, distinct-hit semantics are exactly what select.py would get wrong.
- fix: score_text accepts patterns-or-strings; take compiled patterns only (compile_terms is public) — the union type hides double compilation in a loop over 80 items
- fix: state the whole_word rule for terms that start/end with non-word chars ('C++', 'R&D'): lookarounds on \w, not \b, and pin it with a test
- fix: the 'AI does not hit said' example is fine; keep the docstring free of Iowa State/topic names (it is)

## context-packer  —  critic: keep
**Purpose.** Pack already-ranked text items, each capped, into a character budget and report which were left out.
**when_to_use.** You have a ranked list of text snippets and a model whose context window you only know as a number, and you need to send as many as fit while telling the reader how many did not.
**Inputs.** items: Sequence[tuple[K, str]] — (opaque key, rendered text) already in rank order; key is whatever the caller uses to cite the item (an int row id in this app); budget_chars: int — the total character budget for the item block; max_item_chars: int | None — per-item cap applied before fitting (6000 in this app); per_item_overhead_chars: int — chars the caller will add per item for numbering/separators, counted against the budget; max_items: int | None — upper bound on count (profile max_items); truncation_marker: str — appended to a capped text so the model sees it was cut; context_window_tokens, chars_per_token, prompt_overhead_chars, output_reserve_tokens — ints for context_budget_chars()
**Outputs.** PackResult(packed: list[tuple[K, str]], left_out: list[K], chars_used: int, budget_chars: int, truncated: list[K]) — packed is a strict prefix of the input in the same order; left_out is the remaining keys in order; truncated lists keys whose text was capped; context_budget_chars(...) -> int — never negative; 0 when overhead and reserve exhaust the window
**Dependencies.** none
**Must not know.** the Item or Profile dataclasses (takes (key, text) pairs); config.yaml / profile YAML / the profile schema (max_items and cap arrive as ints); the LLMProvider Protocol or any provider object (context_window() is called by the caller, the int is passed in); SQLite, items.db, row shapes; logging setup (returns counts; the caller logs); how an item is rendered into the prompt (the caller renders title/url/body to one string first); secret redaction (caller redacts before packing); hardcoded paths, prompts/system.md, the previous-week result (that is just prompt_overhead_chars)
**Public API.**
```python
def context_budget_chars(context_window_tokens: int, *, chars_per_token: int = 4, prompt_overhead_chars: int = 0, output_reserve_tokens: int = 4096) -> int
@dataclass(frozen=True)
class PackResult(Generic[K]):
    packed: list[tuple[K, str]]
    left_out: list[K]
    truncated: list[K]
    chars_used: int
    budget_chars: int
def cap_text(text: str, max_chars: int, marker: str = "…") -> tuple[str, bool]
def pack_items(items: Sequence[tuple[K, str]], budget_chars: int, *, max_item_chars: int | None = None, per_item_overhead_chars: int = 0, max_items: int | None = None, truncation_marker: str = "…") -> PackResult[K]
```
**Harness.** none  ·  **Prior art.** seed: No whole feature packs ranked items into a budget or reports leftovers. The only real part hit is estimate_tokens() in hybrid-rag-retrieval, a one-line character/4 estimate; it is too small to copy as a part and goes the other direction (corpus -> tokens, not window -> chars). The seed adopts its convention (chars_per_token default 4) so the two features agree, and stays a seed. hybrid-rag-retrieval was already considered and rejected by the spec for this pipeline (nothing is retrieved; the week's items are the whole input).
**Docstring.**
```
Pack ranked text items into a character budget, capping each, and say what was left out.

The situation: one LLM call has to carry as many candidate items as fit, there is no
tokenizer (CLI and Ollama providers expose only a context-window hint), and the
caller must be able to tell the reader "n of m candidates" honestly. This module is
the arithmetic and the loop, nothing else.

Contract:
- ``context_budget_chars(window_tokens, chars_per_token=4, prompt_overhead_chars=0,
  output_reserve_tokens=4096)`` -> chars available for the item block, floored at 0.
- ``pack_items(items, budget_chars, *, max_item_chars=None, per_item_overhead_chars=0,
  max_items=None, truncation_marker="…")`` -> ``PackResult``. ``items`` are
  ``(key, text)`` pairs already in rank order; keys are opaque and returned as given.
- Fitting is first-fit-then-stop: the packed list is a strict prefix of the input, so
  rank order is honored and ``left_out`` is everything after the first item that did
  not fit. It does not skip ahead to squeeze in a smaller lower-ranked item; that
  would silently reorder by size, not rank.
- Capping happens before fitting; a capped text ends with ``truncation_marker`` and
  its key is listed in ``truncated``. An item that exceeds the whole budget even after
  capping is left out, not partially packed.
- Pure and deterministic. Same inputs, same result, no I/O, no logging.

Deliberately not here: ranking or relevance filtering (the caller decides order),
rendering an item to prompt text (the caller passes the final string), token
counting (chars_per_token is a stated assumption, not a measurement), and any
notion of provider, profile, or database. Zero dependencies: stdlib dataclasses only.
```
**Critic.** One job: fit a ranked list into a limit and report the remainder. Budget arithmetic is a pure helper in the same module, not a second seed. Nothing app-shaped in the inputs.
- fix: chars_per_token default 4 matches hybrid-rag-retrieval's DEFAULT_CHARS_PER_TOKEN; say so in the docstring so the two features agree deliberately
- fix: truncation_marker default '…' (non-ASCII) is fine but document it counts toward max_item_chars
- fix: PackResult.packed carries the capped text; make explicit that callers citing by key must not re-render from the original (or return both) — otherwise synthesize.py silently sends uncapped bodies

## llm-json-contract  —  critic: keep
**Purpose.** Turn a text-only completion callable into one that returns a schema-valid JSON object, retrying once with the validation error when the first reply fails.
**when_to_use.** You asked a text-only model (a coding CLI, a local Ollama box) for JSON and need a validated object back rather than a string that usually parses, with the raw replies kept for the week it does not.
**Inputs.** complete: Callable[[list[dict]], str] — any text-in/text-out completion; the seed never sees a provider object, only this callable; messages: list[dict] — OpenAI-style role/content dicts already rendered by the caller (system prompt with schema inline, user turn with items); not mutated; validate: Callable[[Any], str | None] — returns None when the object is acceptable, otherwise a human-readable error string that is appended to the retry prompt; schema: dict — only for the jsonschema_validator(schema) helper, which builds a validate callable from a JSON Schema document; retries: int = 1 — how many corrective turns to append after the first failure; 0 disables the fallback; retry_template: str — format string with {error} and {rule}, the user turn appended on retry; a default is provided; text: str — for extract_json_object alone: any model reply, possibly wrapped in code fences or prose
**Outputs.** ContractResult — frozen dataclass: obj (the validated dict), responses (tuple[str, ...] of every raw reply in order, so the caller can store raw_response verbatim), errors (tuple[str, ...] of the failure text per failed attempt, for a parse-retry log line), attempts (int); JsonContractError(RuntimeError) — raised when every attempt fails; carries the same responses and errors tuples so raw evidence survives the failure; extract_json_object(text) -> dict — the first decodable JSON object in the text; raises ValueError naming the reason (no object found / decoded value is not an object); Exceptions raised by complete() propagate unchanged; the seed does not classify provider failures
**Dependencies.** json (stdlib): json.JSONDecoder.raw_decode tried at each '{' position finds the first well-formed object inside fences or prose without a regex or a brace-balancing parser; dataclasses (stdlib): ContractResult is a frozen value object; jsonschema (optional, imported lazily inside jsonschema_validator only): the app's contract is a JSON Schema document (prompts/schema.json) and hand-rolling required/enum/array checks is a known source of silent gaps; kept optional so complete_json and extract_json_object run and test with zero third-party packages, and an ImportError from the helper names the pip install
**Must not know.** the LLMProvider Protocol or any provider class; it receives a bare callable; config.yaml / the profile YAML / the llm block (provider name, model, timeout, retry counts); the Item dataclass, item ids, or the citation rule (that is render.py's check); prompts/schema.json or prompts/system.md as paths; the schema arrives as a dict, the prompt as messages; SQLite, the briefs table, raw_response/result_json columns; logging configuration; it returns errors and attempts as data and never logs; secret redaction; the caller redacts before storing responses; how the retry turn is worded for a given app beyond the overridable template; no Iowa State, no buckets, no persona
**Public API.**
```python
DEFAULT_RETRY_TEMPLATE: str  # uses {error} and {rule}; rule defaults to 'Reply with only the corrected JSON object.'
class JsonContractError(RuntimeError):
    responses: tuple[str, ...]
    errors: tuple[str, ...]
@dataclass(frozen=True)
class ContractResult:
    obj: dict
    responses: tuple[str, ...]
    errors: tuple[str, ...]
    attempts: int
def extract_json_object(text: str) -> dict
def jsonschema_validator(schema: dict) -> Callable[[Any], str | None]
def complete_json(
    complete: Callable[[list[dict]], str],
    messages: list[dict],
    validate: Callable[[Any], str | None],
    *,
    retries: int = 1,
    retry_template: str = DEFAULT_RETRY_TEMPLATE,
) -> ContractResult
```
**Harness.** none  ·  **Prior art.** seed: No feature or part in the library extracts a JSON object from model text, validates it, or retries with the error. A repo-wide grep of features/python src (excluding tests) for json.loads / JSONDecodeError / jsonschema / 'code fence' found only file and JSONL parsing. The two LLM provider features stop at text out and their READMEs say so. Seed it; the `complete` callable signature deliberately matches LLMProvider.complete from cli-llm-providers so the app passes provider.complete directly.
**Docstring.**
```
llm_json_contract — get a schema-valid JSON object out of a text-only model.

Coding CLIs (`claude -p`, `codex exec`) and Ollama return prose, not structured
output. This module enforces the contract after the call: it takes any
``complete(messages) -> str`` callable, finds the first JSON object in the reply
(inside ```json fences, after "Here is the result:", or bare), hands it to an
injected ``validate(obj) -> str | None``, and on failure appends the reply and
the error as a corrective turn and asks once more. The caller gets the object
plus every raw reply, in order, so the evidence is kept when it matters.

Contract:
  - ``messages`` is never mutated; retries operate on a copy.
  - The first object that ``json.JSONDecoder.raw_decode`` accepts at a ``{``
    wins; arrays, scalars, and stray braces in prose are skipped.
  - ``validate`` returning None means accepted; any string means rejected, and
    that string is what the model sees on retry.
  - Exhausting ``retries`` raises ``JsonContractError`` carrying ``responses``
    and ``errors``; exceptions from ``complete`` propagate untouched.

Deliberately not here: provider selection or availability, prompt rendering,
domain checks on the object's content (citations, word counts), logging, and
persistence. Those belong to the caller, which knows the app.

Dependencies: stdlib only for the loop and extraction. ``jsonschema`` is
imported lazily by ``jsonschema_validator`` alone, because a JSON Schema file
is the usual contract and a hand-rolled checker is where fields go unchecked.
```
**Critic.** One contract with a fallback branch; extraction is exposed separately but does not earn its own seed. Takes a bare callable that matches LLMProvider.complete, so the app passes provider.complete directly.
- fix: retry turn: public_api only shows retry_template({error},{rule}); the docstring says the failed reply is appended too — specify that retry appends [{'role':'assistant','content':reply},{'role':'user','content':template}] so the model sees what it wrote (roles are flattened by cli-llm-providers anyway)
- fix: jsonschema stays optional and lazily imported; in this app it is required (prompts/schema.json), so pyproject pins it — note that in the seed docstring as 'optional for the library, required by any caller using jsonschema_validator'
- fix: extract_json_object: 'first raw_decode-able object at a {' can pick a small object embedded in prose before the real one; document 'first well-formed object wins' and add a test with a stray {} in preamble so the behavior is pinned, not discovered

## cited-digest-render  —  critic: keep
**Purpose.** Render an LLM-produced, bucketed digest to Markdown in which every entry links to the input items it cites, dropping (and reporting) any entry whose citations are empty or point at an id the model was never given.
**when_to_use.** An LLM handed you a structured summary whose entries cite the ids of the inputs it saw, and the page you are about to publish must contain nothing that does not link back to a real source — with a count of what was cut, so a loosely citing model shows up in the numbers instead of silently thinning the page.
**Inputs.** result: Mapping — the parsed digest: {'buckets': [{'name': str, 'entries': [{'text': str, 'item_ids': [int, ...]}]}], 'watch_list': [{'text', 'item_ids'}], ...}; unknown top-level keys (e.g. 'merged') are ignored; a missing 'buckets' or 'watch_list' is treated as empty; urls: Mapping[int, str] — item id -> URL for every item that was sent to the model; its key set IS the set of known ids; title: str — page heading (already formatted by the caller, e.g. 'AI at ISU'); subtitle: str = '' — one line under the title, e.g. 'Week of 2026-09-14'; omitted when empty; bucket_order: Sequence[str] | None — bucket names in the order the page should show them; buckets not named are appended in the order the model returned them; buckets left empty after drops are omitted; watch_list_heading: str = 'Watch list'; footer: str = '' — verbatim trailing paragraph (provenance line, provider name, 'n of m candidates'); the caller composes it, the seed only places it; link_label: Callable[[int], str] = str — text shown for each citation link, given the item id
**Outputs.** RenderResult dataclass: markdown: str (the page; '' only when nothing survived), kept: int (entries rendered, buckets + watch list), dropped: list[DroppedEntry]; DroppedEntry dataclass: section: str (bucket name or the watch-list heading), text: str, item_ids: list (as received), reason: Literal['no_ids', 'unknown_id'], unknown: tuple[int, ...] (the offending ids, empty for 'no_ids'); enforce_citations returns (clean_result: dict, dropped: list[DroppedEntry]) — same shape as the input result with offending entries removed and now-empty buckets removed, so the caller can store or diff the clean structure; Markdown shape is fixed: '# title', optional subtitle line, '## <bucket>' sections of '- text [label](url) [label](url)' bullets in bucket_order, '## Watch list' section, footer paragraph; entry text has internal newlines collapsed to single spaces so one entry is always one bullet; duplicate ids within an entry link once, in first-seen order
**Dependencies.** none
**Must not know.** The app's config.yaml, Profile or the profile YAML schema — bucket order, title, footer arrive as plain values; The Item dataclass, SQLite, the items or briefs tables — the id->url map is a plain Mapping the caller builds; prompts/schema.json or jsonschema — the seed assumes the shape and raises TypeError/ValueError on a malformed entry instead of validating; The LLMProvider, provider name, or retry logic — the footer is a string the caller already composed; Logging setup — drops are returned, never logged; the caller decides what a drop count means; Markdown-to-HTML conversion, SMTP, file paths under briefs/ — the seed returns a string; Type coercion of ids — '12' is not 12; if a model returns string ids that is a schema failure upstream, not a rendering fallback
**Public API.**
```python
@dataclass(frozen=True)
class DroppedEntry:
    section: str
    text: str
    item_ids: list
    reason: Literal['no_ids', 'unknown_id']
    unknown: tuple[int, ...] = ()
@dataclass(frozen=True)
class RenderResult:
    markdown: str
    kept: int
    dropped: list[DroppedEntry]
def enforce_citations(result: Mapping[str, Any], known_ids: Collection[int], *, watch_list_heading: str = 'Watch list') -> tuple[dict, list[DroppedEntry]]:
    """Copy of result with uncited or mis-cited entries removed and empty buckets dropped, plus the drops."""
def render_markdown(result: Mapping[str, Any], urls: Mapping[int, str], *, title: str, subtitle: str = '', bucket_order: Sequence[str] | None = None, watch_list_heading: str = 'Watch list', footer: str = '', link_label: Callable[[int], str] = str) -> str:
    """Markdown for an already-clean result; raises KeyError on an id missing from urls."""
def render_digest(result: Mapping[str, Any], urls: Mapping[int, str], *, title: str, subtitle: str = '', bucket_order: Sequence[str] | None = None, watch_list_heading: str = 'Watch list', footer: str = '', link_label: Callable[[int], str] = str) -> RenderResult:
    """enforce_citations then render_markdown; the one call the app makes."""
```
**Harness.** none  ·  **Prior art.** seed: Five phrasings, none matched. No feature in the library renders a structured result to Markdown, and none enforces citations on an LLM answer in code: rag-chat-service requests '(page N)' citations in the prompt only. The nearest part, secret-scanner's render_report, shares only the design stance (rendering kept separate from computation, report the true count even when output is truncated), which this seed adopts as a principle but cannot copy as code. Plant a new seed.
**Docstring.**
```
Render a bucketed, citation-carrying digest to Markdown, keeping only entries that link to a real source.

An LLM was given a numbered list of items and asked for JSON: named buckets of entries, a watch
list, and on every entry an ``item_ids`` list naming the inputs it drew on. This module turns that
JSON into the page a reader sees, and it is where the citation rule is enforced by code rather than
by the prompt. An entry with no ``item_ids``, or with any id absent from the caller's id->url map,
is dropped and reported; it never reaches the page. Dropping the whole entry on one bad id is
deliberate: a claim that cites one real source and one invented one cannot be trusted either.

Contract:
- ``urls`` (id -> URL) is the whole set of known ids. Ids are compared as given; no coercion.
- Buckets come out in ``bucket_order`` first, then any others in model order; a bucket left empty
  after drops is omitted. Entry text is emitted verbatim except that newlines collapse to spaces.
- ``enforce_citations`` and ``render_markdown`` are usable alone; ``render_digest`` composes them
  and returns the page plus every drop with its reason, so the caller can count, log, or refuse
  to send when too much was cut.

Deliberately not done here: JSON extraction and schema validation (upstream), Markdown-to-HTML
and delivery (downstream), logging, and any knowledge of where ids or URLs come from.

No runtime dependencies: dataclasses and typing only. Markdown is emitted as text; a Markdown
library would add nothing but a way for formatting to drift.
```
**Critic.** Filter and render are one situation (a page that must not contain uncited claims); splitting would make the renderer trust its input. Both halves stay public. Pure, no deps.
- fix: 'watch_list' is the app schema's top-level key baked into the seed; generalize to extra_sections: Mapping[str, str] = {'watch_list': 'Watch list'} (result key -> heading) so a second app with 'open_questions' needs no fork
- fix: link_label default str renders '[12](url)' — legible but ugly; keep default but note the caller can pass an ordinal or domain label; no change to boundary
- fix: enforce_citations takes known_ids: Collection[int] while render_markdown takes urls: Mapping — good; make render_digest derive known_ids from urls.keys() and say so
- fix: ids compared as given (no '12' -> 12 coercion) is right; the schema.json must enforce integer item_ids so a string id fails validation upstream, not silently drops here — cross-reference in the docstring

## markdown-email  —  critic: keep
**Purpose.** Deliver a Markdown document as a multipart/alternative email (styled HTML plus the raw Markdown as plain text) through an SMTP relay.
**when_to_use.** A scheduled job produced a Markdown document and the people who need it read email, not repositories — it has to land in a mail client looking like a page while the raw Markdown stays in the message for anyone who greps, quotes, or replies.
**Inputs.** markdown_text: str — the document; also becomes the text/plain part verbatim; subject: str — already rendered by the caller (no template expansion here); sender: str — RFC 5322 address, e.g. 'brief@facility.iastate.edu'; recipients: Sequence[str] — To: addresses; host: str, port: int — SMTP relay; no default host, port defaults to 25; stylesheet: str — CSS placed in a <style> block in <head>; DEFAULT_STYLESHEET if omitted; extra_headers: Mapping[str, str] | None — e.g. Reply-To, List-Id, X-Brief-Provider; timeout: float, starttls: bool, username/password: str | None — connection knobs, all explicit values
**Outputs.** build_message -> email.message.EmailMessage with text/plain (the Markdown, utf-8) and text/html alternatives, Subject/From/To/Date/Message-ID set; markdown_to_html -> str, a complete HTML document with the stylesheet inlined in <head>; send_message / send_markdown_email -> dict[str, tuple[int, bytes]] of refused recipients (empty on full success), as smtplib.send_message returns; raises smtplib.SMTPException (or OSError on connect/timeout) unchanged; raises ValueError before any socket is opened when recipients is empty or sender is blank
**Dependencies.** markdown: The spec names Python `markdown` for the HTML conversion; hand-rolling a CommonMark subset (links, lists, headings, emphasis) is where a 'small' converter grows into hundreds of lines and silently mis-renders a citation link. Imported lazily inside markdown_to_html so build/send of a pre-rendered HTML body never needs it.; stdlib: email.message, email.utils, smtplib, html: EmailMessage builds the multipart/alternative correctly (charset, transfer encoding, boundaries); smtplib is the only relay client needed; html.escape guards the <title>.
**Must not know.** config.yaml or the `smtp:` block — host/port arrive as arguments; the profile schema or its `delivery:` block — from/to/subject are already-resolved strings; the `{title} — week of {week_start}` subject template — the caller renders it; the Item dataclass, the briefs table, sent_at, or the --resend idempotency rule; SQLite, the git archive, briefs/<profile>/ paths, or any filesystem path; logging configuration — it returns/raises; the caller logs; the footer text, provider name, or 'n of m candidates' line — those are already inside the Markdown; environment variables or dotenv — credentials, if any, are passed in; the stub-email fallback for an unavailable provider — that is a different Markdown body, same function
**Public API.**
```python
DEFAULT_STYLESHEET: str  # ~40 lines: body font/width, headings, links, lists, footer; no images
def markdown_to_html(markdown_text: str, *, stylesheet: str = DEFAULT_STYLESHEET, title: str = "") -> str
def build_message(markdown_text: str, *, subject: str, sender: str, recipients: Sequence[str], stylesheet: str = DEFAULT_STYLESHEET, extra_headers: Mapping[str, str] | None = None) -> EmailMessage
def send_message(message: EmailMessage, *, host: str, port: int = 25, timeout: float = 30.0, starttls: bool = False, username: str | None = None, password: str | None = None) -> dict[str, tuple[int, bytes]]
def send_markdown_email(markdown_text: str, *, subject: str, sender: str, recipients: Sequence[str], host: str, port: int = 25, stylesheet: str = DEFAULT_STYLESHEET, extra_headers: Mapping[str, str] | None = None, timeout: float = 30.0, starttls: bool = False, username: str | None = None, password: str | None = None) -> dict[str, tuple[int, bytes]]
```
**Harness.** none  ·  **Prior art.** seed: Nothing in the library sends email or converts Markdown to HTML. Four semantic searches plus an rg over every feature for smtplib/email.mime/EmailMessage returned no whole feature and no part; the only 'markdown' hits are features that emit Markdown text. The closest reusable thing is the stub_http harness pattern (threaded server on an ephemeral port), which the seed's tests should imitate for a stub SMTP server rather than mock smtplib.
**Docstring.**
```
Send a Markdown document as a multipart/alternative email through an SMTP relay.

The Markdown is the canonical form: it is sent verbatim as the text/plain part, and a
rendered HTML document with a small inline stylesheet is the text/html alternative, so
a mail client shows a page and a grep, a reply, or a text-only reader still gets the
source. Build and send are separate steps: `build_message` is pure and returns an
`EmailMessage` you can inspect or serialize; `send_message` opens the socket. The
convenience `send_markdown_email` does both.

Contract: every value is explicit (addresses, subject, host, port, stylesheet,
headers) — no config object, no environment lookup, no default relay host. Subject
and body arrive already rendered; this module does no templating. Recipients that the
relay refuses come back as the dict smtplib returns; transport errors propagate
unchanged so the caller decides whether to retry or record a failed send. An empty
recipient list or blank sender raises ValueError before any connection is attempted.

Deliberately not done: no attachments, no inline images, no tracking pixels, no
HTML sanitization (the Markdown is text the caller already trusts), no retry or
queueing, no idempotency (the caller owns "already sent" state), no address-book
or template expansion.

Dependencies: `markdown` (PyPI) for Markdown -> HTML, imported lazily inside
`markdown_to_html`; a hand-rolled converter is where a small module grows past its
worth and mis-renders the links that make the document trustworthy. Everything
else is stdlib (`email.message.EmailMessage`, `email.utils`, `smtplib`, `html`).

Tests: `build_message` is covered without a network; `send_message` runs against a
~40-line threaded stub SMTP socket server on an ephemeral port (tests/harness/stub_smtp.py,
modelled on the library's stub_http harness) that records the DATA it receives.
```
**Critic.** One job (email this Markdown); build vs send is the test seam, not a second seed. markdown dep is justified and lazy.
- fix: test_harness says 'none' but the docstring promises a stub SMTP socket server; set test_harness to stub_smtp and plan the ~40-line harness (smtpd was removed in Python 3.12; do not reach for aiosmtpd, write the minimal EHLO/MAIL/RCPT/DATA/QUIT responder)
- fix: inputs example 'brief@facility.iastate.edu' — drop the domain from the seed's docs
- fix: Date/Message-ID are set in build_message; make Message-ID domain derive from sender (email.utils.make_msgid(domain=...)) so no hostname leaks and goldens are stable with an injectable msgid: str | None
- fix: stylesheet default is ~40 lines inside the module; fine, but keep it a constant a caller can replace wholesale, not appended to

## silent-source-check  —  critic: merge
**Purpose.** Given last-seen timestamps per source and a threshold in days, return the sources that have been silent longer than the threshold.
**when_to_use.** A scheduled job has been finishing cleanly for weeks while one of its inputs quietly stopped producing, and you need a way to notice that a source went dark instead of mistaking it for a slow week.
**Inputs.** last_seen: Mapping[str, datetime | str | None] — the expected sources by name (the keys) and when each last yielded, as an aware/naive datetime or an ISO 8601 string; None means the source has never been seen; threshold_days: float — how long a source may go without yielding before it counts as silent; must be >= 0; now: datetime | None (keyword) — the reference instant, injected so tests are deterministic; defaults to datetime.now(timezone.utc)
**Outputs.** list[SilentSource] — one frozen dataclass per silent source: name, last_seen (aware UTC datetime or None), silent_for (timedelta or None when never seen); sorted never-seen first, then longest silence first, then by name; empty list when nothing is silent; ValueError on a negative threshold, an unparseable timestamp string, or a timestamp in the future relative to now (a clock skew symptom the caller should see, not have silently swallowed); TypeError when a timestamp value is neither datetime, str, nor None
**Dependencies.** none
**Must not know.** config.yaml, sources.yaml, or any profile object — the caller passes the mapping, the seed never reads YAML or decides which sources are 'enabled'; the Item dataclass or the items table — the caller computes MAX(fetched_at) GROUP BY source; the seed never touches SQLite or knows the column names; logging configuration — it returns a value; the caller logs or emails; SMTP, the operator address, or any alert rendering — the spec's 'alert email' is deliver.py's job; the 14-day figure from the spec — threshold_days is a parameter, never a default baked in; hardcoded paths, the DB path, or the environment
**Public API.**
```python
@dataclass(frozen=True)
class SilentSource:
    name: str
    last_seen: datetime | None   # aware UTC, or None when never seen
    silent_for: timedelta | None # None when never seen
def silent_sources(
    last_seen: Mapping[str, datetime | str | None],
    threshold_days: float,
    *,
    now: datetime | None = None,
) -> list[SilentSource]:
    """Sources whose last_seen is more than threshold_days before now, never-seen first, then longest silence first, then by name."""
```
**Harness.** none  ·  **Prior art.** seed: Five phrasings, all hits are single-keyword noise ('source', 'last', 'feed', 'missing'). The full feature list (45 Python features) contains nothing about liveness, staleness, heartbeats, or timestamp-threshold checks; the closest neighbours are PDF and LLM-provider features. No whole feature and no part matches, so this is a fresh seed. It should be stdlib-only like secret-redaction and sqlite-versioned-schema, which is the library's house style for small verified features.
**Docstring.**
```
Report which sources have gone silent for longer than a threshold.

A scheduled poller that keeps exiting 0 hides a dead feed: a source that
stopped yielding looks exactly like a quiet fortnight. This module answers
one question — "which of these sources have not yielded anything in more
than N days?" — from values the caller already has, so the check is a pure
function that any ingest loop can call after a run.

Contract:
    silent_sources({"nsf": "2026-09-01T06:00:00+00:00", "inside_isu": None},
                   threshold_days=14, now=<aware datetime>)
    -> [SilentSource("inside_isu", last_seen=None, silent_for=None),
        SilentSource("nsf", last_seen=<dt>, silent_for=<timedelta>)]

The mapping's keys are the expected sources; a value of None means never
seen and is always reported first. Timestamps may be datetimes or ISO 8601
strings (a trailing "Z" is accepted); naive values are taken as UTC. A source
is silent when now - last_seen is strictly greater than the threshold.
A negative threshold, an unparseable string, or a timestamp after ``now``
raises ValueError rather than being hidden — a future timestamp is a clock
problem the operator needs to hear about. The result is sorted never-seen
first, then longest silence first, then by name, so output is stable across
runs and diffable in a log.

Deliberately not done here: reading the database (the caller computes
MAX(fetched_at) per source), deciding which sources are enabled, formatting
or sending the alert, and logging. Those belong to the app; this module
returns a list and nothing else.

No dependencies: stdlib datetime and dataclasses suffice, and ISO parsing is
done with fromisoformat plus a "Z" shim so Python 3.10 works without dateutil.
```
**Critic.** So small the module is a function: the job is `now - t > threshold` over a mapping; ISO parsing and sort order are decoration. Fold into brief/db.py (last_seen_by_source(): MAX(fetched_at) GROUP BY source) plus a ~12-line silent_sources() in the ingest command, with the never-seen-first ordering tests kept. Keep as a seed only if the owner wants it in the library for its own sake; it will band strong but adds a file, a test module and a seeds.toml row for one list comprehension.
- fix: if kept: drop the ValueError on a future timestamp — fetched_at is written by us, so a future value means an NTP/DST step, and aborting the alert pass for it hides the silent sources it was asked about; treat future as 'seen now'
- fix: if kept: threshold_days float is fine; add allowed-silence-per-source override (Mapping[str, float]) since weekly APIs vs daily feeds have different normal gaps — otherwise the 14-day figure ends up hardcoded in the caller anyway

## Missing seeds (critic)
- page-main-text — the other half of the feed-fetch split: fetch one URL with a browser-like UA (own urllib GET, not trafilatura.fetch_url, so headers stay ours), extract main text with trafilatura.extract on the bytes, cap length, return {'text', 'title', 'status'}; raises on non-200 or empty extraction. Harness: stub_http. Dependency: trafilatura (lazy import). Without it sources/rss.py inlines the 200-char policy and the trafilatura call.
- config-env-interpolate — walk a nested mapping/list and substitute ${VAR} (and ${VAR:-default}) from an explicit env: Mapping[str,str] (never os.environ inside), raising an error that names the missing variable and the key path. Spec line 226 requires it; layered-config-overlay explicitly does not do interpolation; profile.py would otherwise reinvent it with a regex and a silent miss. ~40 lines, stdlib, no harness. Borderline on size but the error-path behavior is where hand-rolled versions fail.
- (deferred, not v1) anthropic-api-provider — the ~40-line LLMProvider implementation on the anthropic SDK, only if the metered provider is wanted; seed it when written, since it is provider code the library already has two siblings of.
- (not a seed, a harness template) stub_smtp — the threaded minimal-SMTP responder markdown-email's tests need; belongs in tests/harness/ now and in codeLibrary templates/python-feature/tests/harness/ at graduation.

## Stays app code
- brief/cli.py — typer wiring of ingest|synthesize|deliver|doctor, run ordering, per-profile try/except, JSON-line logging; pure glue
- brief/models.py — Item and Profile dataclasses; the app's schema, referenced by every adapter and query
- brief/profile.py — profile/sources YAML loading, layered-config-overlay call, config-env-interpolate call, profile schema validation; knows every field name in the spec
- brief/db.py — DDL (via sqlite-versioned-schema), content_hash = sha256(source+url+normalized body), INSERT OR IGNORE upsert, fetched_at window query, MAX(fetched_at) per source; the table names and columns are the app
- brief/sources/{rss,nsf,nih,usaspending,pubmed}.py — thin adapters: params dict + since -> seed kwargs -> list[Item]; each is 20-40 lines and knows the Item shape and the profile's parameter names
- brief/select.py — SQL query + keyword-relevance + published_at pre-sort + secret-redaction + render-to-text + context-packer composition; the ordering policy is the app's
- brief/synthesize.py — prompts/system.md template render from persona/audience/buckets/extra_rules, prompt_hash, provider call via llm-json-contract, briefs row write; template variables are the profile schema
- brief/render.py — footer composition ('n of m candidates', provider, provenance line), week subtitle, then cited-digest-render; the footer text is the app's voice
- brief/deliver.py — subject template expansion, markdown-email call, briefs/<profile>/date.md write + git commit, sent_at/--resend idempotency, stub email on provider unavailable; all app state
- brief/llm/__init__.py — get_provider(config) dispatch over the vendored providers; knows config.yaml's llm block
- brief doctor — provider.available()/status() per profile; trivial loop over the above

## Dependency budget
- feedparser — feed-fetch (only third-party import; used on bytes already fetched)
- trafilatura — page-main-text (new seed from the split); heavy transitive set (lxml, htmldate, courlan, justext, charset-normalizer) — the single largest install in the project; lazy import so feed-fetch tests never load it
- markdown — markdown-email (lazy import inside markdown_to_html)
- jsonschema — llm-json-contract (optional for the seed, lazy import in jsonschema_validator; required by this app because prompts/schema.json is the contract)
- All other seeds (nsf-award-search, nih-reporter-search, usaspending-award-search, pubmed-search, keyword-relevance, context-packer, cited-digest-render, silent-source-check) — stdlib only
- App-level, not seeds: typer (cli.py), PyYAML (config/profile loading; layered-config-overlay uses it when importable), pytest (dev). Vendored library features add zero runtime deps.

## Vendored library features (drift)
- **cli-llm-providers** tier=verified drift=`ok  cli-llm-providers  current
       · repo HEAD moved: ed89609 -> 5695f13

1 current` deps=[] entry=`from cli_llm_providers import get_provider; provider = get_provider("claude-cli") (or "codex-cli"/"copilot-cli"); check provider.available() / provider.status(); text = provider.complete(messages) with OpenAI-style message dicts. Also available_providers(), PROVIDERS, LLMProvider Protocol, messages_to_prompt, DEFAULT_TIMEOUT.`
  - `available()` only checks PATH. A CLI that is installed but not signed in reports available and fails at call time — an unattended cron cannot re-authenticate, so the job must handle RuntimeError per profile, not assume availability means working.
  - Errors surface as RuntimeError with the CLI's stderr truncated to 300 characters. Not-signed-in, rate-limited, and offline all look the same to the caller; log status() alongside the exception so a failed run is diagnosable.
  - Timeout defaults to 240s (DEFAULT_TIMEOUT, per-provider parameter) and raises rather than returning partial output. A hung CLI blocks the cron for 4 minutes per profile.
  - Subprocess latency is per call — process startup on every request. Fine interactively, wasteful in a loop; one call per profile is a loop.
  - Responses arrive whole, not streamed; chat_stream yields exactly once. Irrelevant for batch cron, but do not build on streaming.
  - Prompts go over stdin (claude, codex) to avoid MAX_ARG_STRLEN; copilot takes the prompt as argv and has a 16k declared context — scraped web text in the prompt may exceed it.
  - context_window() values are hardcoded declarations (claude 100k, codex 100k, copilot 16k), not queried; treat as budgeting hints when packing scraped text into a prompt.
  - Message roles are flattened into one labelled prompt — no true system-role separation.
  - No embeddings, ever: embed() returns None on every provider.
  - Runs under the user's own CLI login (claude/codex/copilot). A cron running in a different environment (launchd/cron with a minimal PATH and no interactive session) may not find the binary or its credentials — verify PATH in the cron environment.
- **ollama-local-llm** tier=verified drift=`ok  ollama-local-llm  current
       · repo HEAD moved: 1b3fbd7 -> 5695f13

1 current` deps=[] entry=`from ollama_local_llm import OllamaProvider; provider = OllamaProvider() (default http://localhost:11434; optional chat_model=, max_params_b=, embed_preferences=, timeouts); gate on provider.available() / provider.status(); text = provider.complete(messages) or iterate provider.chat_stream(messages).`
  - Every discovery call returns None or [] rather than raising; a missing server or missing model is a state to report, not an exception. An unattended cron must check available()/status() and log the exact `ollama pull` hint rather than silently producing empty output.
  - Model selection is cached per instance and is heuristic (largest chat model <= 15B params by default, ties broken by name). Construct a new provider per run; pass chat_model= explicitly if you want a fixed, reproducible model week over week rather than whatever was most recently pulled.
  - available() triggers discovery (an HTTP call) — cheap but not free; call it once per run, not per profile.
  - chat_stream sends think: false to suppress reasoning-model chain of thought, retrying without the flag if the server rejects it.
  - context_window() is capped at 32,768 regardless of what the model declares — budget scraped web text accordingly.
  - embed() returns None, never a partial list; batched at 64. Only relevant if the project adds retrieval.
  - No authentication. Assumes a trusted local (or LAN) Ollama server; do not point base_url at anything untrusted.
  - Parameter sizes come from server metadata and are absent for some models; those sort as unknown and are chosen last.
  - Ollama must be running when cron fires — it is a separate daemon, and a laptop asleep or a server not started yields available() == False.
- **sqlite-versioned-schema** tier=verified drift=`~  sqlite-versioned-schema  changed
       · repo HEAD moved: ab3e50f -> cba4050
       · changed: APP/kgx/db/schema.py

1 changed

To review one: /lib-refresh sqlite-versioned-schema` deps=[] entry=`from sqlite_versioned_schema import init_schema; version = init_schema(conn, ddl=MY_DDL, version=N) on a caller-owned open sqlite3.Connection. Also exports SCHEMA_VERSION_TABLE.`
  - No migration. A database already carrying a schema_version row keeps it: newer DDL is still applied (new tables appear via IF NOT EXISTS), but the recorded and returned version stays old. A cron that runs weekly for months will outlive schema changes — ship your own upgrade step (e.g. UPDATE schema_version when stored < current, ALTER TABLE for new columns) if the number must move. Adding a column to an existing table is NOT handled by IF NOT EXISTS DDL.
  - Unordered version read: the stored version is read with LIMIT 1 and no ORDER BY. With multiple rows SQLite returns the smallest — an implementation detail the tests pin, not a contract. Never INSERT a second schema_version row.
  - Commit behavior: conn.commit() runs only on the first-creation path. executescript implicitly commits any transaction already pending on the connection before it executes — call init_schema right after opening, before any writes.
  - The mechanism sets no PRAGMAs; if your DDL uses ON DELETE CASCADE, enable PRAGMA foreign_keys = ON yourself (SQLite defaults it off per connection).
  - Every DDL statement must be IF NOT EXISTS-safe because the whole script re-runs on every call (every weekly run).
  - Drift status is 'changed' — the origin APP/kgx/db/schema.py has moved since extraction (the origin's findings on stale version / commit were marked fixed). Review with /lib-refresh before vendoring if you want the upstream guard behavior; the library copy preserves the origin's no-migration behavior deliberately.
- **layered-config-overlay** tier=verified drift=`ok  layered-config-overlay  current

1 current` deps=[] entry=`from layered_config_overlay import apply_overlays; merged = apply_overlays(base, config, key="overlay", list_key="overlays", envelope_key=None, base_dir=config_path.parent, strict=True). Pieces: deep_merge(base, overlay), load_overlay_file(path, loader=, strict=), load_overlays, overlay_paths(config, ...), merge_all(seq), OverlayError.`
  - A failed overlay is silent by default: a missing file, an unparseable file, and a non-mapping top level all return {}. In an unattended cron a typo'd path or truncated file means your override quietly does not apply, with no log line. Pass strict=True to raise OverlayError instead — strongly recommended for unattended use.
  - Relative paths resolve against the current working directory unless you pass base_dir. Cron jobs run from an unpredictable cwd (often $HOME or /), so always pass base_dir=config_file.parent.
  - Lists replace, they do not concatenate. An overlay `profiles: [x]` drops every profile the base list contributed — per-profile config lists in overlays will silently truncate the profile set.
  - A scalar replaces a whole subtree — `{"db": "disabled"}` over `{"db": {...}}` yields the scalar.
  - No runtime dependency: uses PyYAML when importable, falls back to json. The library suite only exercises the JSON fallback and injected loader= seam, not the YAML branch. Use loader=tomllib.loads for TOML.
  - Does not do schema validation or environment-variable interpolation.
- **secret-redaction** tier=verified drift=`~  secret-redaction  changed
       · repo HEAD moved: cb36469 -> 6ba136e
       · changed: claude/skills/session-journal/journal.py
       · changed: claude/skills/story-beat/append_beat.py

1 changed

To review one: /lib-refresh secret-redaction` deps=[] entry=`from secret_redaction import redact, scan, Redactor, rule; clean = redact(text) or redactor = Redactor(extra_rules=[rule(name, pattern)], marker=...) then redactor.redact(text) in a loop; scan(text) yields SecretMatch (rule, offsets, line, column, preview, value). Also DEFAULT_RULES, DEFAULT_MARKER, Rule.`
  - Call redact() at both boundaries: before scraped text is sent to the LLM (prompt) and before it is written to SQLite/files. Redact whole records, not fields.
  - It is a net, not a wall. Catches known provider prefixes (15 rules: Anthropic, OpenAI, GitHub, GitLab, AWS, Google, Slack, Stripe, HuggingFace, npm, PyPI, JWT, PEM, quoted/bare assignments) and explicit key=value assignments only. A secret with no recognisable prefix in prose passes through. Do not make it the only control.
  - Reuse a configured Redactor instance when redacting in a loop (per profile) rather than calling module-level redact() repeatedly with custom rules.
  - Short values escape: assignment values must be >= 12 chars (_MIN_VALUE in rules.py). Bare `secret` and `token` are not keywords. Assignments need an operator (`=` or `:`) and do not cross a newline.
  - A variable reference is redacted like a value: `api_key = ${PROD_API_KEY}` becomes `api_key = [REDACTED]` — scraped docs/config pages will lose variable names. Over-redaction is the safe direction.
  - An unterminated PEM block over-redacts to the next blank line or end of text — scraped web text containing a `-----BEGIN ... PRIVATE KEY-----` header without an END marker will eat the rest of the record.
  - Provider rules have no trailing anchor, so a token followed by more alphanumerics is redacted a few characters long rather than not at all.
  - Stripe test keys are redacted too — irrelevant unless scraping Stripe docs and wanting them verbatim.
  - SecretMatch.value (from scan()) is a live credential; use .preview in any log or report, never .value.
  - Redaction is not reversible and not a hash; two secrets collapse to the same [REDACTED] marker unless you pass a callable marker.
  - Drift status is 'changed' — both origin files moved (f3bc789 fixed upstream bugs the library already had fixed); the library copy is the more complete one. Vendor the library copy, not the origin.
- **secret-scanner** tier=verified drift=`~  secret-scanner  changed
       · repo HEAD moved: cb36469 -> 6ba136e
       · changed: claude/hooks/verify-no-secrets.sh

1 changed

To review one: /lib-refresh secret-scanner` deps=[] entry=`from secret_scanner import scan_text, scan_path, ScanConfig, render_report, to_jsonable; result = scan_text(text, path="label") or scan_path(root, config=ScanConfig(...), relative=True); triage result.real (and result.benign), check result.clean; CLI: python -m secret_scanner [--strict] [--json] <path>... (exit 1 on real findings).`
  - Not a redactor: it reports locations, it never rewrites. For a cron that stores scraped text, the in-memory pattern is scan_text(rendered, path=...).real and refuse to write on findings — pair with secret-redaction if you want to store a scrubbed copy instead.
  - Over-long lines are skipped (default max_line_chars=100_000). The skip is recorded and makes result.clean False, but it is a blind spot; scraped HTML/JS often has huge single lines. Raise max_line_chars via ScanConfig when scanning scraped output.
  - The benign filter can hide a real key: a genuine credential within +-40 chars of a word like `sample`, `example`, `<tag>` is filed under placeholders — counted and printed but not in result.real. Scraped web text (docs, tutorials) is full of such words. Set benign=None for a paranoid pass, or triage result.benign too. Do not set benign_context_chars=None on scraped/minified content — it suppresses nearly everything.
  - A clean exit code is not a clean scan: the CLI exits 1 on real findings only; skipped files do not fail unless --strict. In an unattended job, pass --strict or check result.clean, not just result.real.
  - It only finds shapes it knows (14 provider prefixes + one assignment shape); no entropy scoring. A bare random string is invisible.
  - Pruned directories (.git, .venv, venv, env, node_modules, __pycache__, cache dirs, .tox, .gradle, .terraform) are the one silent gap — no Skipped entry. Build outputs (dist, build, target, .next) are NOT pruned. If the project keeps config in env/, remove it from skip_dirs.
  - One line can produce several findings (generic-assign overlaps provider patterns); counts are per match, collapse by digest for per-secret numbers.
  - Digests are salted per scan, so two runs produce different digests; set correlation_salt for reproducible week-over-week output, knowing a fixed published salt weakens the digest.
  - The fingerprint discloses prefix_chars leading characters; set prefix_chars=0 if reports go somewhere you do not control (e.g. an email or LLM prompt).
  - Binary detection is a NUL-byte sniff; UTF-16 text files are classified binary and skipped (recorded). Symlinks are not followed by default. No git awareness — scans the filesystem, not the index.
  - Keep ScanConfig.patterns on a default_factory — a plain mappingproxy default raises ValueError at import on Python 3.11 only.
  - Do not build your own report from raw regex matches; the redaction guarantee is structural (Finding has no value field). Use render_report()/to_jsonable().
  - Drift status is 'changed' — origin claude/hooks/verify-no-secrets.sh moved since extraction; the library copy already fixes the origin's first-match-only and worktree-vs-index bugs. Vendor the library copy.

## Critic notes
Net: 9 keep, 1 split (feed-fetch, with page-main-text added), 1 merge (silent-source-check into db.py/ingest). Two seeds are genuinely missing (page-main-text, config-env-interpolate). Spec corrections to make before code: (1) lines 139/151 — PubMed saved-search RSS URLs are server-minted and cannot be built from a query; the pubmed adapter uses E-utilities esearch+esummary, and a hand-minted PubMed RSS URL is a plain rss source. (2) NSF v1 API needs printFields to return abstractText — verify live before pinning the golden URL. Cross-seed convention to fix now, cheaply: the four fetcher seeds should share one shape — search_<source>(...), build_<query|criteria|request>(), normalize_<record>(), <Source>SearchError, base_url + timeout_s + user_agent kwargs, normalized keys external_id/url/title/body/published_at/raw plus source-specific extras, amount = JSON value verbatim — so sources/*.py adapters are interchangeable and a future grants_gov seed has a template. Seeds that currently read the clock internally (nsf date_end=None, pubmed since->reldate) should take explicit dates so goldens are deterministic. Size warning: 11 seeds at 60-150 lines each plus docstrings and tests is ~1,000 lines before app glue; the spec's 800/1,500-line guidance was written assuming these live inline. Seeds are not vendored code, so either accept that the line budget excludes seeds or trim (silent-source-check merge is the first cut; config-env-interpolate is the next candidate to inline if the owner wants the count down). Not verified this session (no network): PubMed RSS minting, NSF printFields behavior, USAspending date_type values — all three should be checked against live docs at seed time, not assumed from the boundaries.
