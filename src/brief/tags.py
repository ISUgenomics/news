"""Tags for items: the glue between source labels, phrase matchers, and the resolver.

Situation: the database is flat. Topical signal lives in free text and in
labels the sources already send — OpenAlex concepts, GitHub topics, crates
categories, agency program lines — that no column surfaces. Left as they
arrive, those labels grow a vocabulary of near-duplicates; left unread, they
are wasted. This module reads them, folds them into the vocabulary in
``tags.yaml``, and adds the tags whose phrases the text itself hits.

Contract: ``load_vocabulary(path)`` reads the YAML into a ``Vocabulary`` — a
registry of kebab-case terms, an alias map whose targets must be registry
terms, and one compiled phrase matcher per term that declares ``match``. A
missing file is an empty vocabulary, which switches tagging off; a malformed
one raises ``TagsError`` naming the key. ``derive_tags(labels, text, vocab)``
returns ``([(tag, origin), ...], new_terms)``: only registry terms are ever
returned as tags, each with origin ``"source"`` (a label resolved to it) or
``"phrase"`` (its ``match`` phrases hit the text); ``new_terms`` are the
labels the resolver could not place, reported for curation and never stored.
``unknown_tags(wanted, vocab)`` is the check a profile's ``relevance.tags``
runs against.

Deliberately not here: the resolution ladder (vendored, see
``brief.vendor.tag_vocabulary_resolve``), whole-word matching (the
``keyword_relevance`` seed), knowing which source sends which label
(``brief.sources.relabel``), and storage (``brief.db.replace_tags``). This
module is glue and imports from the app; it is not a seed.

Dependencies: the vendored resolver, the ``keyword_relevance`` seed, and
``profile.load_yaml``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brief.lib.keyword_relevance import compile_terms, score_text
from brief.profile import ProfileError, load_yaml
from brief.vendor.tag_vocabulary_resolve import kebab_case, resolve_tags

__all__ = [
    "DEFAULT_PATH",
    "ORIGIN_PHRASE",
    "ORIGIN_SOURCE",
    "TagsError",
    "Vocabulary",
    "derive_tags",
    "load_vocabulary",
    "unknown_tags",
]

DEFAULT_PATH = "tags.yaml"
ORIGIN_SOURCE = "source"
ORIGIN_PHRASE = "phrase"

#: A label shorter than this after normalisation is noise: "a", "c" (from
#: "C++"), "ml" is handled by an alias instead. The resolver does not filter
#: these — its README says the caller must — so it happens here.
MIN_LABEL_LEN = 3


class TagsError(ValueError):
    """``tags.yaml`` is present but cannot be used as written."""


@dataclass(frozen=True, slots=True)
class Vocabulary:
    registry: dict[str, dict[str, Any]]
    aliases: dict[str, str]
    matchers: dict[str, list[re.Pattern[str]]]

    def __bool__(self) -> bool:
        return bool(self.registry)


def load_vocabulary(path: str | Path) -> Vocabulary:
    """Read ``tags.yaml``. Absent means tagging is off; malformed is an error."""
    p = Path(path)
    if not p.is_file():
        return Vocabulary({}, {}, {})
    try:
        raw = load_yaml(p)
    except ProfileError as exc:  # unreadable or not YAML; same shape of failure
        raise TagsError(str(exc)) from exc
    if raw is None:
        return Vocabulary({}, {}, {})
    if not isinstance(raw, Mapping):
        raise TagsError(f"{p}: must be a mapping with `registry` and `aliases`")

    registry: dict[str, dict[str, Any]] = {}
    matchers: dict[str, list[re.Pattern[str]]] = {}
    for term, spec in (raw.get("registry") or {}).items():
        term = str(term)
        if term != kebab_case(term):
            raise TagsError(f"{p}: registry term {term!r} is not kebab-case ({kebab_case(term)!r})")
        if spec is None:
            spec = {}
        if not isinstance(spec, Mapping):
            raise TagsError(f"{p}: registry.{term} must be a mapping or empty")
        phrases = spec.get("match") or []
        if isinstance(phrases, str) or not all(isinstance(x, str) and x.strip() for x in phrases):
            raise TagsError(f"{p}: registry.{term}.match must be a list of non-blank phrases")
        registry[term] = dict(spec)
        if phrases:
            matchers[term] = compile_terms(phrases)

    aliases: dict[str, str] = {}
    for alias, target in (raw.get("aliases") or {}).items():
        alias, target = str(alias), str(target)
        if alias != kebab_case(alias):
            raise TagsError(f"{p}: alias {alias!r} is not kebab-case ({kebab_case(alias)!r})")
        if target not in registry:
            raise TagsError(f"{p}: alias {alias!r} points at {target!r}, which is not a registry term")
        if alias in registry:
            raise TagsError(f"{p}: {alias!r} is both a registry term and an alias")
        aliases[alias] = target
    return Vocabulary(registry, aliases, matchers)


def derive_tags(
    labels: Iterable[str], text: str | None, vocab: Vocabulary
) -> tuple[list[tuple[str, str]], list[str]]:
    """Registry terms for one item, with where each came from, plus the labels
    that resolved to nothing.

    Source labels win the origin when both a label and a phrase point at the
    same term: the label is the stronger evidence, and one tag is stored once.
    """
    if not vocab:
        return [], []
    candidates = [str(x) for x in labels if x and len(kebab_case(str(x))) >= MIN_LABEL_LEN]
    resolved, new = resolve_tags(candidates, vocab.registry, vocab.aliases)
    unplaced = set(new)
    tags: list[tuple[str, str]] = [(t, ORIGIN_SOURCE) for t in resolved if t not in unplaced]
    have = {t for t, _ in tags}
    for term, patterns in vocab.matchers.items():
        if term in have:
            continue
        if score_text(text, patterns):
            tags.append((term, ORIGIN_PHRASE))
            have.add(term)
    return tags, new


def unknown_tags(wanted: Iterable[str], vocab: Vocabulary) -> list[str]:
    """Which of ``wanted`` are neither registry terms nor aliases."""
    return sorted(t for t in set(wanted) if t not in vocab.registry and t not in vocab.aliases)


def canonical(tag: str, vocab: Vocabulary) -> str:
    """The registry term a profile's tag names, following one alias hop."""
    return vocab.aliases.get(tag, tag)
