# Seed boundary: nsf-award-search

## Purpose
Fetch NSF awards matching an awardee, keyword, or PI-name filter within an award-date range, returned as normalized plain dicts that each carry the raw record.

## when_to_use (draft for FEATURE.toml)
You need the list of NSF awards to an institution, on a topic, or to a named PI since a date, in a shape you can store, and do not want to learn the Award Search API's field names, date format, or 25-per-page offset walk.

## Inputs
- awardee: str | None — institution name, matched by NSF's awardeeName filter
- keyword: str | None — free text; NSF's keyword filter searches titles and abstracts, supports trailing * wildcard
- pi_name: str | None — passed as NSF's pdPIName filter
- date_start: datetime.date — inclusive lower bound on award date
- date_end: datetime.date | None — inclusive upper bound; None means today (UTC)
- base_url: str — defaults to https://api.nsf.gov/services/v1/awards.json; tests point it at the stub server
- timeout_s: float — per-request socket timeout, default 30
- max_pages: int — pagination safety cap, default 40 (1000 records at rpp=25)
- user_agent: str — sent on every request; NSF rejects empty agents intermittently

## Outputs
- list[dict] in API order, one dict per award, with fixed keys: external_id (NSF award id, str), url (https://www.nsf.gov/awardsearch/showAward?AWD_ID=<id>), title, body (abstractText, may be empty), published_at (award date as ISO-8601 YYYY-MM-DD or None), pi_name (first + last), awardee, amount (fundsObligatedAmt verbatim as str, never converted), agency, program, raw (the untouched API record)
- NSFSearchError(RuntimeError) on HTTP failure, non-JSON body, or an error payload from the API; ValueError when no filter is given or date_end < date_start
- build_query_url(...) -> str, a pure function so the exact query string is pinned by golden tests
- normalize_award(raw: dict) -> dict, pure, tolerant of missing fields (never KeyError; missing -> '' or None)

## Dependencies
- none

## Must NOT know about
- config.yaml, sources.yaml, or the profile schema — the caller unpacks the profile's {awardee|keyword|pi_name} into keyword arguments
- the Item dataclass or brief.models — returns plain dicts; brief/sources/nsf.py does the Item mapping
- SQLite, db.py, content_hash, INSERT OR IGNORE, or the fetched_at/since semantics of brief ingest — the caller derives date_start from since
- logging configuration — raises exceptions and returns values; no logger of its own
- hardcoded ISU, 'Iowa State University', AI keywords, or any topic vocabulary
- relevance filtering, ranking, or dedup across sources
- the changedetection.io feed, RSS, NIH RePORTER, or USAspending shapes — one endpoint only

## Public API

Revised 2026-09-17, after a live run. This is a **boundary change**, recorded here
in the same commit as the code per CLAUDE.md rule 3.

**What changed and why.** The drafted boundary treated `awardee` as a filter the API
applies. It is not one. NSF splits `awardeeName` into words and matches any of them, so
asking for "Iowa State University" returns awards from every institution with
"University" in its name. Measured against the live API: 308 awards across 159
institutions in one month, with Iowa State not among the eight most frequent. The saved
fixture in `tests/fixtures/nsf_award_search_page.json` is itself an instance — a real
response to a Tuskegee query containing a University of Colorado award.

So `awardee` now means **the institution the caller gets, not a hint sent to NSF**. It is
applied twice: sent to the API, where it narrows nothing reliably, and enforced again on
every record returned, where it decides.

`awardee_state` is the new companion parameter and the pairing is the point.
`awardeeStateCode` *is* exact. Over one 90-day window, state "IA" returned 68 awards
across 4 Iowa institutions, 44 of them Iowa State. Without it the answer is still
correct, just after fetching and discarding far more.

```python
class NSFSearchError(RuntimeError): ...

def normalize_institution(name: str | None) -> str
    # casefold, strip punctuation, collapse whitespace; comparison only

def matches_awardee(record_name: str | None, wanted: str | None) -> bool
    # substring on the normalized form, so "Iowa State University" also matches a
    # longer recorded legal name. wanted=None matches everything.

def build_query_url(base_url: str, *, awardee: str | None, keyword: str | None,
                    pi_name: str | None, date_start: date, date_end: date,
                    awardee_state: str | None = None,
                    offset: int = 0, rpp: int = 25) -> str

def normalize_award(raw: dict) -> dict
    # external_id, url, title, body, published_at, pi_name, awardee, amount,
    # agency, program, raw. amount is the JSON value verbatim.

def search_nsf_awards(*, awardee: str | None = None, awardee_state: str | None = None,
                      keyword: str | None = None, pi_name: str | None = None,
                      date_start: date, date_end: date | None = None,
                      today: date | None = None, base_url: str = DEFAULT_BASE_URL,
                      timeout_s: float = 30.0, max_pages: int = 40,
                      user_agent: str = "nsf-award-search/0.1") -> list[dict]
```

**Kept from the original boundary.** `printFields` is still set explicitly, because the
default field set omits `abstractText` and the body would always be empty. Dates are
still explicit parameters with `today` injectable, so goldens stay deterministic.
`dateStart`/`dateEnd` still filter on the award's *effective* date, not the project start
date. `amount` is still the JSON value verbatim, never parsed.

## Test harness
stub_http

## Approved module docstring (write this verbatim into the module)
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

## Prior art
verdict: seed

Four phrasings, zero whole-feature or part-level hits for award/grants search, paginated JSON GET, or record normalization; the library has 44 python features and none talks to a public data API. The only reusable material is the stub_http harness template (copy into tests/harness/) and the urllib-only convention that ollama-local-llm establishes. Seed it new.

## Critic verdict: keep
One endpoint, one paged GET, one normalized shape; pagination and normalize_award are the mechanism and the return contract, not second jobs.

### Boundary fixes to apply at write time
- build_query_url must set printFields explicitly: the NSF v1 default field set omits abstractText (and pdPIName, fundsObligatedAmt in some responses); without it body is always empty. Verify against the live API before pinning the golden URL
- date_end=None -> 'today (UTC)' reads the clock inside a pure-looking seed; keep the convenience but route it through a today: date | None parameter (or document that tests must pass date_end) so goldens are deterministic
- NSF dateStart/dateEnd filter on award effective date, not 'award date'; name the field the seed filters on in the docstring so 'new this week' semantics are honest
- Rename NSFSearchError -> keep, but align naming across the four fetcher seeds (<Source>SearchError, search_<source>(), build_*, normalize_*) so sources/*.py adapters are copy-paste identical
- amount: state 'JSON value verbatim, no conversion' (str here, number elsewhere) as the shared rule for all three award seeds
