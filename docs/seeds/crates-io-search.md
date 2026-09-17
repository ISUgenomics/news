# Seed boundary: crates-io-search

## Purpose
Return Rust crates matching a query, newest first, from the crates.io registry, as plain dicts — so a caller can see packages that appeared or changed in a window without scraping the site.

## when_to_use (draft for FEATURE.toml)
You want to know what someone just published in a language ecosystem, and the literature will not tell you for months. A package registry is where a tool exists first; crates.io exposes search with a `new` and a `recent-update` sort, which is the difference between "created this week" and "touched this week".

## Inputs
- query: str — full-text over name, description and keywords
- sort: str = "new" — `new` (creation), `recent-update`, `recent-downloads`, `downloads`, `relevance`. Validated against that set, because a typo silently returns relevance order and looks like a quiet week
- per_page: int = 100 — the registry's maximum
- max_pages: int = 5 — bound on the seek-paged walk
- user_agent: str — crates.io's crawler policy asks for a UA that identifies the caller and carries a contact. Required, not optional
- timeout_s, open_url — as the other fetching seeds

## Outputs
- `list[dict]`, one per crate: `name`, `description`, `url` (the crates.io page), `repository`, `homepage`, `documentation`, `version`, `downloads`, `recent_downloads`, `keywords`, `categories`, `created_at`, `updated_at`, `published_at` (whichever of created/updated the chosen sort means), `raw`
- `CratesIoError` on transport failure, a non-200, or an unparseable body

## Must NOT know about
- `Item`, the database, `source_key`, profiles, keywords config, buckets
- which ecosystem or topic this deployment cares about
- ranking beyond passing `sort` through; relevance filtering; dedup against other sources
- logging or persistence

## Deliberate decisions

**`published_at` follows the sort.** With `sort=new` it is `created_at`; with
any other sort it is `updated_at`. A caller windowing on "what is new" and a
caller windowing on "what moved" want different dates from the same record,
and silently returning one of them makes the other wrong. The field the sort
implies is the field the caller gets, and both raw dates are kept too.

**An unknown `sort` is refused.** crates.io accepts an unknown value and
falls back to relevance, which returns old, popular crates and reads as "no
new packages this week" — a quiet wrong answer.

**No category browse.** crates.io has categories, but the useful ones here
(`science`) are far too broad, and the narrow ones do not exist. Search is
the honest interface.

## Dependencies
- **stdlib only**

## Transport contract
Shared with the other fetching seeds, duplicated because seeds graduate
individually: `Content-Encoding` undone before decoding; a bot wall detected
structurally and named, never retried.

## Public API
```python
class CratesIoError(RuntimeError): ...
SORTS: frozenset[str]

def normalize_crate(raw: dict, *, sort: str = "new") -> dict
def search_crates(
    query: str, *, sort: str = "new", per_page: int = 100, max_pages: int = 5,
    user_agent: str, base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = 30.0, open_url: OpenUrl | None = None,
) -> list[dict]
```

## Prior art
verdict: seed

Nothing in codeLibrary queries a package registry. Within this repo the
fetching seeds all speak to literature or award APIs; none has a notion of a
software artifact. Measured before proposing it: 1,031 crates match
"bioinformatics", and `sort=new` surfaces four created in the past week that
appear in no other source this project has.

## Test harness
`tests/harness/stub_http.py` with `tests/fixtures/crates_io_search.json` — a
real trimmed response including a crate with no description and one with no
repository, both of which occur live.
