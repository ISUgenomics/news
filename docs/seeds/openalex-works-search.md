# Seed boundary: openalex-works-search

## Purpose
Return the scholarly works an institution published in a date window, from the OpenAlex API, as plain dicts — resolving the abstract from OpenAlex's inverted index and paging with a cursor until the window is covered.

## when_to_use (draft for FEATURE.toml)
You want an institution's recent publications and the affiliation-string search you reached for first is missing most of them. OpenAlex filters on a resolved institution id, so the query is exact rather than a string match, and it spans every discipline instead of one database's subject scope.

## Inputs
- institution_id: str — an OpenAlex institution id (`I173911158`), a ROR id (`04rswrd78`), or either as a URL. Normalised internally; a bare name is REFUSED, see below
- from_date / to_date: str — ISO dates bounding `publication_date`
- search: str | None — OpenAlex's relevance search across title, abstract and fulltext. Optional: a caller filtering locally (this app does) should leave it unset
- mailto: str — the contact address OpenAlex asks for; it buys the faster, more reliable "polite pool" and is required rather than optional so a caller cannot silently be rude
- per_page: int = 200 — OpenAlex's maximum
- max_pages: int = 10 — a bound on the cursor walk, so a wide window cannot page forever
- timeout_s: float
- open_url: Callable | None — the transport seam, so tests drive a real socket rather than a mock

## Outputs
- `list[dict]`, one per work, each with: `external_id` (the bare OpenAlex id), `url`, `title`, `abstract`, `journal`, `authors` (list of display names, in order), `first_author`, `doi`, `pmid`, `published_at` (ISO date), `type`, `raw` (the untouched record)
- `reconstruct_abstract(inverted_index) -> str` — exposed because it is the one genuinely non-obvious part of this API
- `resolve_institution(name, *, mailto, ...) -> list[dict]` — a lookup helper, NOT called by the search path (see below)
- `OpenAlexError` on transport failure, a non-200, or an unparseable body

## Must NOT know about
- the `Item` dataclass, the database, `source_key`, or any app type
- `config.yaml`, `sources.yaml`, profiles, keywords, buckets
- which institution this deployment cares about — no ROR id, no name, no default
- relevance filtering, ranking, dedup against other sources, body caps
- logging or persistence

## Deliberate refusals

**A bare institution name is refused.** The search path takes an id only.
Resolving "Iowa State University" costs a second request whose answer can
change, and a name search returns near-matches — OpenAlex lists both `Iowa
State University` and `Iowa State University Digital Press`. Silently picking
the first is the `validate-identity-when-relaxing-a-filter` practice inverted:
a wrong-institution match is confident, complete, and about somebody else.
`resolve_institution` exists so a human can look the id up once and paste it
into config, where it is then a fixed, reviewable fact.

**`lineage`, not `id`.** The filter is `authorships.institutions.lineage`, so
a work credited to a department, institute or hospital *within* the
institution is included. Filtering on `institutions.id` alone silently drops
them. Stated here because it is a judgement call, not an obvious default.

**No retry.** One request per page. A caller that wants backoff wraps this.

**`search` is offered but not recommended for this app.** OpenAlex relevance
ranking is opaque and changes; this app already filters locally against a
profile's `any_of`, where the rule is visible and versioned. Passing both
means two filters, one of which nobody can inspect.

## Dependencies
- **stdlib only** — `urllib.request`, `json`, `datetime`. No third-party HTTP client, matching the other fetching seeds in this repo.

## Transport contract
Shared with the other fetching seeds here, and duplicated rather than imported
because seeds graduate individually:
- `Content-Encoding` is undone before decoding (`urllib` does not; `requests` does)
- a bot wall is detected structurally and named, never retried

## Public API
```python
class OpenAlexError(RuntimeError): ...

def reconstruct_abstract(inverted_index: dict | None) -> str
def normalize_institution_id(value: str) -> str
def normalize_work(raw: dict) -> dict
def resolve_institution(name: str, *, mailto: str, ...) -> list[dict]
def search_openalex_works(
    institution_id: str, *, from_date: str, to_date: str,
    search: str | None = None, mailto: str,
    per_page: int = 200, max_pages: int = 10,
    timeout_s: float = 30.0, open_url: OpenUrl | None = None,
) -> list[dict]
```

## Prior art
verdict: seed

Nothing in codeLibrary queries a scholarly API. Within this repo,
`pubmed_search` is the closest sibling and is deliberately not extended: it
speaks E-utilities, returns summary records with no abstract, and is scoped by
affiliation string. The two answer the same question badly and well
respectively — measured over one 30-day window at one institution, PubMed
found 99 works and 3 topical matches where OpenAlex found 338 and 21.

`research_person.py` in the knowledge_graph repo is the working prior art for
the API shape, the polite-pool convention, and the inverted-index
reconstruction. It is person-scoped (`authorships.author.id`), so the query
differs, but the abstract trick is lifted from it rather than rediscovered.

## Test harness
`tests/harness/stub_http.py`, with `tests/fixtures/openalex_works.json` — a
real trimmed response carrying works with an abstract, without one, and
without a DOI.
