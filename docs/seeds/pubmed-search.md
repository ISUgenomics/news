# Seed boundary: pubmed-search

## Purpose
Turn a PubMed query string into a list of plain article dicts keyed by PMID, using NCBI E-utilities (esearch + esummary JSON).

## when_to_use (draft for FEATURE.toml)
You have a PubMed search expression and want the matching articles as plain records with their PMIDs, without a browser session, a saved-search feed id, or a login.

## Inputs
- query: str — a PubMed search expression, e.g. 'maize[Title] AND (genome OR GWAS)'
- since: datetime | None — lower bound on Entrez date (edat); converted to reldate days; None means no date filter
- max_results: int = 200 — esearch retmax; also caps esummary batch
- base_url: str = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils' — overridable so stub_http can serve it
- tool: str, email: str — NCBI etiquette parameters sent on every request; caller-supplied strings
- api_key: str | None — optional NCBI key; raises the rate limit from 3 to 10 req/s, never read from env by the seed
- timeout_s: float = 30.0
- user_agent: str | None — optional header override

## Outputs
- list[dict] — one dict per PMID, keys: external_id (PMID as str), url ('https://pubmed.ncbi.nlm.nih.gov/<pmid>/'), title, body (journal, authors, volume/pages, DOI when present, joined as one plain-text line), published_at (ISO 8601 date or None, parsed from esummary pubdate/epubdate), raw (the esummary record dict, for the caller's raw_json column)
- Empty list when esearch returns zero ids
- Raises RuntimeError with the HTTP status or NCBI error string on transport failure or an 'error'/'ERROR' key in either JSON envelope; never returns partial results silently

## Dependencies
- **urllib.request / urllib.parse (stdlib)** — two GETs against E-utilities; base_url is a parameter so the stub_http harness exercises the real socket path
- **json (stdlib)** — esearch and esummary both answer retmode=json; no XML parsing needed
- **datetime (stdlib)** — since -> reldate day count; pubdate strings ('2026 Sep 3', '2026 Sep', '2026') -> ISO date
- **feedparser — NOT used** — the E-utilities path returns JSON, and the only RSS PubMed offers needs a server-minted feed id; a minted feed is a plain rss source, not this seed

## Must NOT know about
- the app's config.yaml / sources.yaml / profile YAML or the profile schema (query, since, tool, email, api_key arrive as explicit arguments)
- the Item dataclass or models.py — it returns plain dicts the adapter maps onto Item
- SQLite, db.py, content_hash, fetched_at — it never writes or hashes
- logging setup — it raises; the caller logs
- hardcoded paths, dotenv, os.environ — api_key is a parameter, never looked up
- the relevance any_of/none_of filter — everything esearch returns comes back; filtering is downstream
- feedparser and the rss adapter

## Public API
```python
def search(query: str, *, since: datetime | None = None, max_results: int = 200, base_url: str = DEFAULT_BASE_URL, tool: str = 'pubmed-search-feed', email: str = '', api_key: str | None = None, timeout_s: float = 30.0, user_agent: str | None = None) -> list[dict]
def esearch_ids(query: str, *, since: datetime | None = None, max_results: int = 200, base_url: str = DEFAULT_BASE_URL, tool: str = 'pubmed-search-feed', email: str = '', api_key: str | None = None, timeout_s: float = 30.0, user_agent: str | None = None) -> list[str]
def esummary_records(pmids: list[str], *, base_url: str = DEFAULT_BASE_URL, tool: str = 'pubmed-search-feed', email: str = '', api_key: str | None = None, timeout_s: float = 30.0, user_agent: str | None = None) -> list[dict]
def record_to_item(record: dict) -> dict  # pure: esummary record -> {external_id, url, title, body, published_at, raw}
def parse_pubdate(text: str) -> str | None  # pure: '2026 Sep 3' | '2026 Sep' | '2026' -> ISO date or None
DEFAULT_BASE_URL: str = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'
```

## Test harness
stub_http

## Approved module docstring (write this verbatim into the module)
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

## Prior art
verdict: seed

Nothing in the library fetches a feed, talks to NCBI, or parses RSS/Atom; all whole-feature and part hits are PDF, RAG, or transcript keyword noise (max overlap 3 on the two relevant phrasings, 0 on the E-utilities phrasing). No feature to use and no function to copy. The only reusable thing is the stub_http harness, which fits the seed's two-GET transport exactly (route /esearch.fcgi and /esummary.fcgi, assert on stub_http.requests[-1].path and query). Seed from scratch, stdlib only. Open risk to confirm before writing: my claim that PubMed's saved-search RSS id is server-minted and not query-derivable was not checked against the live site this session; if a query-parameter RSS form exists, the seed still stands (E-utilities is the documented path) but the spec's 'query string becomes the feed URL' note could be kept as a second transport.

## Granularity note
The draft purpose joins two jobs with "and": (1) query -> saved-search RSS feed URL, (2) fetch a feed into item dicts. Job 2 is the generic feed fetcher the spec already assigns to the `rss` adapter ("every literature feed with no special cases"), so a PubMed seed must not re-own it. Job 1 has a worse problem: PubMed's saved-search RSS URL (`https://pubmed.ncbi.nlm.nih.gov/rss/search/<32-hex>/?limit=N&fc=<ts>`) is minted server-side when a user clicks "Create RSS"; the hex id is not derivable from the query string, so "the query string becomes the feed URL" (spec line 151) cannot be done offline. Not verified against the live site this session (no network), but I am fairly confident. Proposed split: (a) a pre-minted PubMed RSS URL is just a plain `rss` source in sources.yaml, no code; (b) THIS seed becomes the single job "query -> PMID-keyed item dicts" over the documented, stateless E-utilities path (esearch with term+reldate, then esummary), stdlib-only. The boundary below describes (b). One job, one transport, one fallback (esearch returns zero ids -> empty list, no esummary call).

## Critic verdict: keep
The E-utilities rewrite is correct: PubMed saved-search RSS ids are minted server-side (rss/search/<hex>/), not derivable from a query; a minted URL is an ordinary rss source. The redrawn boundary is one job (esearch -> esummary -> dicts). The spec's line 139/151 ('query string becomes the feed URL') needs correcting.

### Boundary fixes to apply at write time
- rename slug: the '-feed' suffix now lies; use pubmed-search (or pubmed-esearch)
- since: datetime -> reldate days reads the clock inside the seed; use E-utilities mindate/maxdate (YYYY/MM/DD) with datetype='edat' as explicit parameters and let the adapter convert its datetime — deterministic and pinnable
- body composed as 'one plain-text citation line' is presentation policy; return journal, authors, volume, pages, doi as fields (plus raw) and expose citation_line(record) as a pure helper the adapter may call
- email='' default: NCBI asks for a contact; make email required (no default) so a caller cannot forget it
- esummary with >~200 ids should POST (URL length); state the limit or switch to POST when len(pmids) > 200
- rename search() -> search_pubmed(); a bare search leaks into every namespace
- must_not_know says 'never read from env' for api_key — good; also state tool/email are caller strings so no app name leaks into the default tool value ('pubmed-search-feed' as default is fine once renamed)
