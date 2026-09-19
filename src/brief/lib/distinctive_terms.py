"""Which words mark the texts that lack something, so a person can act on them.

Situation: a vocabulary tags what it knows, and the items it leaves untagged
are the evidence of what it does not. Reading eight hundred award
descriptions to find out is not going to happen; counting which words those
descriptions use far more often than the tagged ones do takes a second and
puts the answer in a list. That list is how a phrase table grows from what
the corpus actually says rather than from memory.

Contract: ``distinctive_terms(foreground, background, *, min_count=10,
min_ratio=1.5, stopwords=DEFAULT_STOPWORDS, min_len=4, top=60)`` returns a
list of ``Term`` records ``{"term", "foreground", "background", "ratio"}``
for words that appear in at least ``min_count`` foreground texts and whose
share of the foreground is at least ``min_ratio`` times their share of the
background. Counts are DOCUMENT frequencies — a word counts once per text
however often it repeats — so one verbose description cannot dominate. A
word absent from the background is scored as if it appeared in one
background document, so nothing divides by zero and an absent word does not
outrank everything by being infinite. Sorted by foreground count descending,
then term. ``tokenize(text, *, stopwords, min_len)`` is the one tokenizer,
exported so a caller can show what a term matched: lowercase, ``[a-z]``
followed by letters or hyphens to ``min_len`` characters, minus stopwords.

Deliberately not here: stemming (``educate``/``education`` are two terms;
the caller adds both phrases), bigrams, any notion of tags, items or
thresholds beyond the two numbers passed in, and any reading of files.

Dependencies: standard library only (``re``, ``collections``).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable
from typing import TypedDict

__all__ = ["DEFAULT_STOPWORDS", "Term", "distinctive_terms", "tokenize"]

#: Function words and the prose an award or abstract is made of. Not a
#: linguistics resource — the words that topped the first run and said
#: nothing ("purpose", "overcome", "anticipated").
DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    """
    this that with from will have been were their which these there those
    project research proposal award supports support work using also into
    such than other more each both between under over about after before
    during while would could should through study studies provide provides
    program period funding grant grants the and for are not but its can may
    all any our your they them then when where what who whom whose here
    because however therefore thus toward towards within without upon
    purpose overcome anticipated appropriate larger explored leverages
    develop development developing developed novel new improved improve
    understanding understand approach approaches results result based
    proposed propose specific broader impacts impact goal goals aims aim
    objective objectives include including includes address addresses
    role roles system systems data effort efforts
    """.split()
)

_WORD = re.compile(r"[a-z][a-z-]*")


class Term(TypedDict):
    term: str
    foreground: int
    background: int
    ratio: float


def tokenize(
    text: str | None,
    *,
    stopwords: Collection[str] = DEFAULT_STOPWORDS,
    min_len: int = 4,
) -> set[str]:
    """The distinct words of one text, lowercased, at least ``min_len`` long,
    minus ``stopwords``. A set: document frequency is what the caller counts."""
    if not text:
        return set()
    return {
        w
        for w in _WORD.findall(text.lower())
        if len(w) >= min_len and w not in stopwords and not w.endswith("-")
    }


def distinctive_terms(
    foreground: Iterable[str],
    background: Iterable[str],
    *,
    min_count: int = 10,
    min_ratio: float = 1.5,
    stopwords: Collection[str] = DEFAULT_STOPWORDS,
    min_len: int = 4,
    top: int = 60,
) -> list[Term]:
    """Words frequent in ``foreground`` and rarer in ``background``.

    ``foreground`` and ``background`` are iterated once each and may be
    generators. An empty foreground yields ``[]``; an empty background makes
    every foreground word "absent from the background" and scored as such.
    """
    fg: Counter[str] = Counter()
    n_fg = 0
    for text in foreground:
        n_fg += 1
        fg.update(tokenize(text, stopwords=stopwords, min_len=min_len))
    bg: Counter[str] = Counter()
    n_bg = 0
    for text in background:
        n_bg += 1
        bg.update(tokenize(text, stopwords=stopwords, min_len=min_len))
    if n_fg == 0:
        return []
    out: list[Term] = []
    for term, count in fg.items():
        if count < min_count:
            continue
        fg_share = count / n_fg
        # An absent word is treated as present in one background document:
        # finite, and still clearly distinctive, without an infinity that
        # would rank every rare typo above every real signal.
        bg_share = max(bg.get(term, 0), 1) / max(n_bg, 1)
        ratio = fg_share / bg_share
        if ratio < min_ratio:
            continue
        out.append(
            {"term": term, "foreground": count, "background": bg.get(term, 0), "ratio": round(ratio, 2)}
        )
    out.sort(key=lambda t: (-t["foreground"], t["term"]))
    return out[:top]
