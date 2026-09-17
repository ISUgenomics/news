# Seed boundary: keyword-relevance

## Purpose
Keep the text records that pass any_of / none_of keyword lists, each tagged with its distinct-hit count.

## when_to_use (draft for FEATURE.toml)
You have a pile of fetched text records and a profile-style list of must-hit and must-not-hit terms, and need the survivors ordered by how strongly they hit before something expensive (an LLM call, a human) reads them.

## Inputs
- records: an iterable of any type T (the caller's rows, dataclasses, dicts); the module never inspects them
- text_of: Callable[[T], str | None] that yields the text to match (the caller joins title + body); None is treated as empty
- any_of: Iterable[str] of keep terms; single words or multi-word phrases; empty means the filter is off and every record survives with 0 hits
- none_of: Iterable[str] of hard-exclude terms, checked before any_of
- whole_word: bool = True; match at word boundaries so 'AI' does not hit 'said'; False falls back to plain substring matching

## Outputs
- filter_ranked -> list[tuple[T, int]]: surviving records paired with their distinct-hit count, sorted by hits descending with a stable sort (input order preserved within equal counts)
- score_text -> int | None: None when a none_of term hits or no any_of term hits (with a non-empty any_of); otherwise the number of distinct any_of terms found
- compile_terms -> list[re.Pattern[str]]: case-insensitive patterns, one per distinct term, for callers that want to reuse a profile's compiled lists across many calls

## Dependencies
- none

## Must NOT know about
- the Item dataclass or any brief.models type (records are opaque T reached only through text_of)
- the Profile dataclass or the profile YAML schema (relevance.any_of / none_of arrive as plain iterables of str)
- config.yaml, layered-config-overlay, or any app config object
- SQLite, db.py, or how candidates were queried
- published_at ordering, max_items, body caps, or the provider context budget (packing is the caller's job)
- logging setup; the function returns counts, the caller decides what to log
- hardcoded paths, source names, or anything about Iowa State or AI

## Public API
```python
def compile_terms(terms: Iterable[str], *, whole_word: bool = True) -> list[re.Pattern[str]]
def score_text(text: str | None, any_of: Sequence[re.Pattern[str]] | Iterable[str], none_of: Sequence[re.Pattern[str]] | Iterable[str] = (), *, whole_word: bool = True) -> int | None
def filter_ranked(records: Iterable[T], text_of: Callable[[T], str | None], any_of: Iterable[str], none_of: Iterable[str] = (), *, whole_word: bool = True) -> list[tuple[T, int]]
```

## Test harness
none

## Approved module docstring (write this verbatim into the module)
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

## Prior art
verdict: seed

Nothing in the library does allowlist/denylist phrase filtering with a distinct-hit count. hybrid-rag-retrieval is the nearest neighbour but solves a different situation: ranking a corpus against a free-text query with IDF weights and a top_k that always returns something, whereas this seed needs literal multi-word phrases, a hard exclude list, whole-word matching so 'AI' does not hit 'said', and zero survivors when nothing matches. Its terms() helper word-tokenizes, which breaks phrase terms, so nothing is copied. The seed is ~40 lines on stdlib re.

## Critic verdict: keep
One job (filter with a hit count; the sort is a stable sort of the count already computed). Opaque T + text_of keeps Item out. Small but not trivial: whole-word lookarounds, phrase whitespace, distinct-hit semantics are exactly what select.py would get wrong.

### Boundary fixes to apply at write time
- score_text accepts patterns-or-strings; take compiled patterns only (compile_terms is public) — the union type hides double compilation in a loop over 80 items
- state the whole_word rule for terms that start/end with non-word chars ('C++', 'R&D'): lookarounds on \w, not \b, and pin it with a test
- the 'AI does not hit said' example is fine; keep the docstring free of Iowa State/topic names (it is)
