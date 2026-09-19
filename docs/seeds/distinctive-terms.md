# Seed boundary: distinctive-terms

## Purpose
Given texts that lack something and texts that have it, return the words far more common in the first set, so a person can turn them into match phrases.

## when_to_use (draft for FEATURE.toml)
You have a corpus split into "covered" and "not covered" by some rule — a tag vocabulary, a classifier, a filter — and want to know, from the texts themselves, which words the uncovered ones are made of.

## Inputs
- foreground: Iterable[str] — the texts lacking coverage
- background: Iterable[str] — the texts that have it
- min_count: int — a word must appear in this many foreground texts (default 10)
- min_ratio: float — foreground share over background share must reach this (default 1.5)
- stopwords: Collection[str] — words never reported (default DEFAULT_STOPWORDS)
- min_len: int — shortest word reported (default 4)
- top: int — rows returned (default 60)

## Outputs
- list[Term] where Term is a TypedDict {term, foreground, background, ratio}; document frequencies; sorted by foreground count desc then term; a word absent from the background is scored as present in one background document

## Dependencies
- none beyond the stdlib (re, collections)

## Must NOT know about
- Item, the database, item_tags, or how "untagged" was decided — it receives two iterables of strings
- tags.yaml or the resolver — it reports words; a person turns them into phrases
- stemming or bigrams — deliberately single words; add both surface forms as phrases

## Public API
```python
def distinctive_terms(foreground, background, *, min_count=10, min_ratio=1.5, stopwords=DEFAULT_STOPWORDS, min_len=4, top=60) -> list[Term]
def tokenize(text, *, stopwords=DEFAULT_STOPWORDS, min_len=4) -> set[str]
DEFAULT_STOPWORDS: frozenset[str]
class Term(TypedDict): term: str; foreground: int; background: int; ratio: float
```

## Harness
none — pure functions.

## Prior art
`lib_search.py "distinctive terms frequent words unlabelled documents suggest phrases vocabulary tf-idf"` returned only lexical overlaps (tag-vocabulary-resolve, pdf-native-words, structure-aware-chunking on "words"/"documents"). Nothing in the library suggests terms from a corpus. `keyword_relevance` is the matcher these terms feed, not a duplicate. Seeded new.
