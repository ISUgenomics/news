"""Keyword relevance: keep the records that pass any_of / none_of term lists, tagged with a distinct-hit count.

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

Boundary notes added at write time:
- The whole-word guard is a pair of lookarounds on \\w -- ``(?<!\\w)term(?!\\w)`` -- not ``\\b``.
  ``\\b`` is a transition assertion, so it would refuse a term that already ends in a non-word
  character: ``\\bC\\+\\+\\b`` never matches "C++ toolchain". The lookarounds only forbid a word
  character touching the match, so "C++", "R&D" and ".NET" behave like any other term, while "AI"
  still misses "said".
- ``score_text`` takes compiled patterns only, never raw strings. Compiling is the caller's decision
  (once per profile via ``compile_terms``); a string/pattern union would hide a recompile per record
  inside a loop over hundreds of records.
- "Deduplicated case-insensitively" is implemented as a ``str.lower()`` key on the
  whitespace-collapsed term, deliberately **not** ``str.casefold()``. ``casefold`` is *full* case
  folding and merges pairs that ``re.IGNORECASE`` keeps apart -- "Straße".casefold() ==
  "STRASSE".casefold(), yet ``re.compile("Straße", re.I)`` does not match "STRASSE". Deduping on
  casefold would therefore throw one of those two terms away and silently stop matching its text,
  which is the invisible failure (a record quietly not surviving). ``lower`` errs the other way:
  a pair it fails to merge costs at most one extra hit in a rank, never a lost term.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Sequence, TypeVar

__all__ = ["compile_terms", "score_text", "filter_ranked"]

T = TypeVar("T")


def compile_terms(
    terms: Iterable[str], *, whole_word: bool = True
) -> list[re.Pattern[str]]:
    r"""Compile ``terms`` to case-insensitive patterns, one per distinct non-blank term.

    Blank and whitespace-only terms are dropped; the rest are deduplicated on their
    lowercased, whitespace-collapsed form, keeping first-seen order -- so "  Machine
    Learning" and "machine\n learning" are one term, but "Straße" and "STRASSE" are two
    (see the module docstring for why the key is ``lower`` and not ``casefold``). Terms are
    literals: regex metacharacters are escaped. A whitespace run inside a term matches any
    whitespace run in the text. With ``whole_word`` the pattern is wrapped in ``\w``
    lookarounds (see the module docstring for why not ``\b``).
    """
    patterns: list[re.Pattern[str]] = []
    seen: set[str] = set()
    for term in terms:
        words = term.split() if term else []
        if not words:
            continue
        key = " ".join(words).lower()
        if key in seen:
            continue
        seen.add(key)
        body = r"\s+".join(re.escape(word) for word in words)
        if whole_word:
            body = r"(?<!\w)" + body + r"(?!\w)"
        patterns.append(re.compile(body, re.IGNORECASE))
    return patterns


def score_text(
    text: str | None,
    any_of: Sequence[re.Pattern[str]],
    none_of: Sequence[re.Pattern[str]] = (),
) -> int | None:
    """Distinct ``any_of`` hits in ``text``, or ``None`` when the record does not survive.

    ``none_of`` is checked first: one hit returns ``None``. Then, with a non-empty
    ``any_of``, the result is the number of distinct patterns that match, or ``None`` when
    none do. An empty ``any_of`` turns the keep-filter off and scores 0. ``text`` may be
    ``None``, which is treated as ``""``. Both lists are already-compiled patterns.
    """
    haystack = text or ""
    for pattern in none_of:
        if pattern.search(haystack):
            return None
    if not any_of:
        return 0
    hits = sum(1 for pattern in any_of if pattern.search(haystack))
    return hits or None


def filter_ranked(
    records: Iterable[T],
    text_of: Callable[[T], str | None],
    any_of: Iterable[str],
    none_of: Iterable[str] = (),
    *,
    whole_word: bool = True,
) -> list[tuple[T, int]]:
    """Surviving records paired with their distinct-hit count, highest count first.

    ``text_of`` is called exactly once per record and may return ``None``. Terms are
    compiled once for the whole call. The sort is stable, so records with equal counts
    keep the order they arrived in.
    """
    keep = compile_terms(any_of, whole_word=whole_word)
    drop = compile_terms(none_of, whole_word=whole_word)
    scored: list[tuple[T, int]] = []
    for record in records:
        hits = score_text(text_of(record), keep, drop)
        if hits is not None:
            scored.append((record, hits))
    scored.sort(key=lambda pair: -pair[1])
    return scored
