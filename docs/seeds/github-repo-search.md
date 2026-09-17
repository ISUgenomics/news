# Seed boundary: github-repo-search

## Purpose
Return repositories matching a GitHub search query as plain dicts, surfacing the two things that quietly go wrong with this API: a truncated result set that reports success, and a rate limit that answers 403 rather than 429.

## when_to_use (draft for FEATURE.toml)
You want to see software as it appears rather than as it is later written up, and the registry for its ecosystem does not cover the ones nobody published. GitHub search reaches both — `created:` for what is new, `pushed:` for what is alive.

## Inputs
- query: str — a GitHub search query, verbatim (`language:rust topic:bioinformatics created:>2026-08-18`). Not assembled from parts: the qualifier grammar is the API's, and rebuilding it here would be a second, worse grammar
- sort: str | None = None — `updated`, `stars`, `forks`, `help-wanted-issues`, or None for best match. Validated
- order: str = "desc"
- per_page: int = 100 — the API maximum
- max_pages: int = 3 — GitHub caps search at 1,000 results however far you page
- token: str | None = None — optional. Unauthenticated is 10 requests/minute, authenticated 30; a weekly job fits comfortably in either
- user_agent: str — GitHub requires one and rejects requests without it
- timeout_s, open_url — as the other fetching seeds

## Outputs
- `list[dict]` per repo: `full_name`, `owner`, `url`, `description`, `homepage`, `language`, `topics`, `stars`, `forks`, `open_issues`, `license`, `archived`, `fork`, `created_at`, `pushed_at`, `updated_at`, `published_at`, `raw`
- `GitHubSearchError`; `GitHubRateLimitError` naming the reset time; `GitHubIncompleteResultsError`

## Must NOT know about
- `Item`, the database, `source_key`, profiles, buckets
- which language, topic or organisation this deployment cares about
- how a token is stored or read — it arrives as a value
- filtering forks or archived repos: both flags are returned and the CALLER decides

## Deliberate decisions

**`incomplete_results: true` raises.** GitHub answers 200 with a partial
result set and a flag most callers never read. Treating that as success is
how a digest silently reports a quiet week that was not quiet. A caller that
genuinely wants partial data can catch the error and use `.results` on it.

**A rate limit is its own error, with the reset time.** GitHub answers 403,
not 429, when the search limit is spent — indistinguishable from a
permissions problem unless the headers are read. The error says which it was
and when the window reopens.

**No retry, no waiting for the reset.** A seed that sleeps turns a fast
failure into a hung job. The caller decides.

**`published_at` is `created_at`.** Unlike crates.io there is no ambiguity:
the query itself chooses between `created:` and `pushed:`, so the record's
own creation date is the honest one and `pushed_at` is kept alongside.

**The query string is passed through, not built.** GitHub's qualifier
grammar is large and changes; a helper that assembled it would be a second
grammar to maintain and to get subtly wrong.

## Dependencies
- **stdlib only**

## Transport contract
As the other fetching seeds: `Content-Encoding` undone before decoding; a bot
wall detected structurally and named, never retried. Duplicated rather than
imported because seeds graduate individually.

## Public API
```python
class GitHubSearchError(RuntimeError): ...
class GitHubRateLimitError(GitHubSearchError): ...
class GitHubIncompleteResultsError(GitHubSearchError): ...
SORTS: frozenset[str]

def normalize_repo(raw: dict) -> dict
def search_repositories(
    query: str, *, sort: str | None = None, order: str = "desc",
    per_page: int = 100, max_pages: int = 3, token: str | None = None,
    user_agent: str, base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = 30.0, open_url: OpenUrl | None = None,
) -> list[dict]
```

## Prior art
verdict: seed

Nothing in codeLibrary queries a code host. Measured before proposing it: for
one language and one topic, 125 repositories pushed in 30 days and 10 created
in the same window — including two created the same day — none of which
appears in crates.io, OpenAlex or bioRxiv.

## Test harness
`tests/harness/stub_http.py` with `tests/fixtures/github_repo_search.json` — a
real trimmed response including a repo with no description, one with no
licence, one fork and one archived.
