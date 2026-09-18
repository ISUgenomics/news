"""Match a proposed tag against a controlled vocabulary.

Situation
---------
Labels arrive as free text — typed by a person, invented by an LLM, or pulled
out of an import. Left alone, a vocabulary grows ``genomic``, ``genomics``,
``Genomics`` and ``genome-analysis`` as four separate terms, and every query
over it is then wrong in a way nobody notices.

Contract
--------
``resolve_tags`` walks a four-rung ladder per candidate, in this order:

1. **Normalise** to kebab-case. A candidate that normalises to nothing is
   dropped.
2. **Alias** — an exact hit in the synonym map wins outright.
3. **Exact** — an exact hit in the registry wins next.
4. **Fuzzy** — the *best*-scoring registry term wins, if any scores at all.
5. **New** — nothing matched, so the normalised candidate is returned and
   also reported as new.

Fuzzy scoring, in descending strength: containment (both terms longer than
three characters), edit distance within a length-scaled threshold (1 when the
shorter term is 5 characters or fewer, else 2), or more than 60% overlap of
hyphen-separated parts.

Resolution is deduplicated on **every** rung: a candidate resolving to a term
already produced in the same call is not emitted twice, and a new term is
reported once.

Deliberately not here
---------------------
- Loading a registry or an alias map from markdown, JSON, a database or
  anywhere else. This takes two dicts.
- Persisting new terms, or deciding whether a new term is *allowed*. It
  reports; the caller decides.
- Suggesting tags from prose. That is keyword search, a different problem.

Two behaviours of the code this replaced in its origin repo are deliberately
**not** carried over (see README, Gotchas):

- The old ``_fuzzy_find`` returned the first registry key that scored,
  iterating in dict order, so its answer depended on insertion order. This
  scores every term and returns the best, ties broken by the shorter term and
  then lexicographically — so the result is deterministic for any registry.
- The old ``resolve_tags`` deduplicated on the alias rung but not the fuzzy
  one, so two candidates resolving to the same term could both be emitted.

Dependencies
------------
Standard library only.
"""

from __future__ import annotations

import re

__all__ = ["resolve_tags", "fuzzy_find", "edit_distance", "kebab_case"]

#: Below this length, containment is too weak a signal to trust.
_MIN_CONTAINMENT_LEN = 3
#: Edit-distance budget, by the length of the shorter term.
_SHORT_TERM_LEN = 5
_MAX_EDIT_SHORT = 1
_MAX_EDIT_LONG = 2
#: Part overlap must *exceed* this to count.
_MIN_PART_OVERLAP = 0.6

_NON_TAG_CHARS = re.compile(r"[^a-z0-9\s-]")
_WHITESPACE = re.compile(r"\s+")
_DASH_RUN = re.compile(r"-+")


def kebab_case(text: str) -> str:
    """Normalise a label to the kebab-case form tags are stored in."""
    s = (text or "").lower().strip()
    s = _NON_TAG_CHARS.sub("", s)
    s = _WHITESPACE.sub("-", s)
    s = _DASH_RUN.sub("-", s)
    return s.strip("-")


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two strings."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        current = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            current.append(min(current[j] + 1, previous[j + 1] + 1, previous[j] + cost))
        previous = current
    return previous[-1]


def _score(candidate: str, existing: str) -> int | None:
    """How strongly `existing` matches `candidate`. Higher is better.

    Returns None when they do not match at all. The tiers are ranked so a
    containment hit always beats an edit-distance hit, which always beats a
    shared-stem hit — the same precedence the tiers were written in.
    """
    if candidate == existing:
        return 100

    if len(candidate) > _MIN_CONTAINMENT_LEN and len(existing) > _MIN_CONTAINMENT_LEN:
        if candidate in existing or existing in candidate:
            return 90

    shorter = min(len(candidate), len(existing))
    budget = _MAX_EDIT_SHORT if shorter <= _SHORT_TERM_LEN else _MAX_EDIT_LONG
    distance = edit_distance(candidate, existing)
    if distance <= budget:
        return 80 - distance

    candidate_parts = set(candidate.split("-"))
    existing_parts = set(existing.split("-"))
    union = candidate_parts | existing_parts
    if union:
        overlap = len(candidate_parts & existing_parts) / len(union)
        if overlap > _MIN_PART_OVERLAP:
            return 50 + int(overlap * 10)

    return None


def fuzzy_find(candidate: str, registry: dict[str, dict]) -> str | None:
    """Return the best-matching registry term, or None.

    Unlike the code this replaces, the answer does not depend on the
    registry's iteration order: every term is scored and the best wins, with
    ties broken by the shorter term and then lexicographically.
    """
    best: tuple[int, int, str] | None = None
    for existing in registry:
        score = _score(candidate, existing)
        if score is None:
            continue
        key = (-score, len(existing), existing)
        if best is None or key < best:
            best = key
    return best[2] if best else None


def resolve_tags(
    candidates: list[str],
    registry: dict[str, dict],
    aliases: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve candidate labels against a controlled vocabulary.

    Args:
        candidates: raw labels, in the order the caller supplied them.
        registry: the vocabulary. Only its keys are read.
        aliases: known synonym -> canonical term.

    Returns:
        ``(resolved, new_terms)``. ``resolved`` preserves candidate order and
        contains no duplicates. ``new_terms`` is the subset of ``resolved``
        that the registry did not already account for.
    """
    aliases = aliases or {}

    resolved: list[str] = []
    new_terms: list[str] = []
    seen: set[str] = set()

    def emit(term: str, *, is_new: bool = False) -> None:
        if term in seen:
            return
        seen.add(term)
        resolved.append(term)
        if is_new:
            new_terms.append(term)

    for candidate in candidates:
        normalized = kebab_case(candidate)
        if not normalized:
            continue

        if normalized in aliases:
            emit(aliases[normalized])
            continue

        if normalized in registry:
            emit(normalized)
            continue

        match = fuzzy_find(normalized, registry)
        if match:
            emit(match)
            continue

        emit(normalized, is_new=True)

    return resolved, new_terms
