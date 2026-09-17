"""Scan and scrub credential-shaped strings.

Two operations over one rule table:

* ``scan(text)``  -> where the secrets are (a scanner wants locations)
* ``redact(text)`` -> the text with them replaced (a writer wants clean output)

``redact`` is implemented on top of ``scan``, so the two can never disagree
about what counts as a secret.

Both make a **single pass over the original text**. That is not an optimization,
it is the reason offsets are usable: applying one regex substitution after
another rewrites the string under your feet, so every reported position after
the first replacement points at the wrong place.

No runtime dependencies.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .rules import DEFAULT_RULES, Rule

__all__ = [
    "DEFAULT_MARKER",
    "Redactor",
    "SecretMatch",
    "redact",
    "scan",
]

DEFAULT_MARKER = "[REDACTED]"

#: A marker may be a fixed string or a function of the match, e.g.
#: ``lambda m: f"[REDACTED:{m.rule}]"``.
Marker = str | Callable[["SecretMatch"], str]


@dataclass(frozen=True)
class SecretMatch:
    """One detected secret.

    Attributes:
        rule: the ``Rule.name`` that fired.
        start: offset of the secret in the *original* text.
        end: offset one past the secret. ``text[start:end]`` is what
            :meth:`Redactor.redact` replaces — for an assignment rule that is
            the value only, not the ``password =`` part.
        line: 1-based line number of ``start``.
        column: 1-based column of ``start``.
        value: the raw matched text. **This is a live credential.** It is here
            because a scanner needs to fingerprint or diff findings; do not put
            it in the report you were trying to make safe.
        preview: a masked form safe to log. Keeps the first few characters
            (the provider tag, which is not itself secret) and the length.
    """

    rule: str
    start: int
    end: int
    line: int
    column: int
    value: str
    preview: str


def _preview(value: str, keep: int = 4) -> str:
    """Mask ``value`` for display.

    Shows a *leading* fragment only. Trailing characters are the conventional
    choice ("...9f2c") but they are the high-entropy end of the token; the
    prefix identifies the provider and gives away nothing.
    """
    flat = value.replace("\n", " ").replace("\r", " ")
    head = flat[:keep]
    return f"{head}...({len(value)} chars)"


class Redactor:
    """A reusable scanner/scrubber over a fixed rule table.

    Construct once and reuse — the regexes are compiled at construction.

    Args:
        rules: replaces the default table entirely. Pass this when you want
            *only* your own rules.
        extra_rules: appended to ``rules``. This is the usual extension point.
        marker: replacement text, or a callable taking the
            :class:`SecretMatch` and returning the replacement.
    """

    def __init__(
        self,
        rules: Sequence[Rule] | None = None,
        *,
        extra_rules: Iterable[Rule] = (),
        marker: Marker = DEFAULT_MARKER,
    ) -> None:
        self.rules: tuple[Rule, ...] = tuple(
            DEFAULT_RULES if rules is None else rules
        ) + tuple(extra_rules)
        self.marker = marker

    # -- scanning ----------------------------------------------------------

    def scan(self, text: str) -> list[SecretMatch]:
        """Find every secret in ``text``, ordered by position.

        Overlaps are resolved deterministically: leftmost wins, then longest,
        then earliest rule in the table. That last tie-break is why the
        Anthropic rule precedes the OpenAI rule — both match ``sk-ant-...``
        over the identical span, and the table order decides which name is
        reported.

        A winner suppresses later candidates only up to **the end of the span
        it actually redacts**, not the end of its whole match. The two differ
        only for a rule with a narrow ``group`` inside a wide match, and there
        the difference is a leak: a custom rule that matches a whole block but
        redacts one field inside it would otherwise shadow every default rule
        firing on the rest of that block, and those tokens would survive.
        """
        if not text:
            return []

        # (match_start, -match_end, rule_index) is the sort key; the match span
        # is used for overlap resolution while the *group* span is what gets
        # replaced. The group is always inside the match, so non-overlapping
        # matches yield non-overlapping replacements.
        candidates: list[tuple[int, int, int, Rule, re.Match[str]]] = []
        for index, item in enumerate(self.rules):
            for match in item.regex.finditer(text):
                start, end = match.span(item.group)
                if start < 0 or start == end:
                    continue  # optional group did not participate, or empty
                candidates.append((match.start(), -match.end(), index, item, match))
        if not candidates:
            return []
        candidates.sort(key=lambda c: (c[0], c[1], c[2]))

        line_starts = _line_starts(text)
        found: list[SecretMatch] = []
        consumed = 0  # end offset of the last accepted *replacement*
        for match_start, _neg_end, _index, item, match in candidates:
            if match_start < consumed:
                continue
            start, end = match.span(item.group)
            # The group is inside the match and the next accepted candidate
            # starts at or after `consumed`, so the emitted spans stay ordered
            # and non-overlapping — which is what lets redact() splice them in
            # one left-to-right pass.
            consumed = end
            value = text[start:end]
            line, column = _position(line_starts, start)
            found.append(SecretMatch(
                rule=item.name,
                start=start,
                end=end,
                line=line,
                column=column,
                value=value,
                preview=_preview(value),
            ))
        return found

    # -- redacting ---------------------------------------------------------

    def redact(self, text: str, *, marker: Marker | None = None) -> str:
        """Return ``text`` with every detected secret replaced.

        Args:
            marker: overrides the instance marker for this call.
        """
        if not text:
            return text
        chosen = self.marker if marker is None else marker
        out: list[str] = []
        cursor = 0
        for found in self.scan(text):
            out.append(text[cursor:found.start])
            out.append(chosen(found) if callable(chosen) else chosen)
            cursor = found.end
        out.append(text[cursor:])
        return "".join(out)

    def has_secrets(self, text: str) -> bool:
        """True if anything at all matched. Cheap gate before an expensive path."""
        return bool(self.scan(text))


# --------------------------------------------------------------------------- #
# Position helpers
# --------------------------------------------------------------------------- #

def _line_starts(text: str) -> list[int]:
    """Offsets at which each line begins. Computed once per scan."""
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _position(line_starts: Sequence[int], offset: int) -> tuple[int, int]:
    """Map a character offset to 1-based (line, column)."""
    line = bisect.bisect_right(line_starts, offset)
    return line, offset - line_starts[line - 1] + 1


# --------------------------------------------------------------------------- #
# Module-level convenience
#
# A default Redactor is built once at import. The one-shot functions below are
# for call sites that never need a custom table; anything that redacts in a
# loop should build its own Redactor and keep it.
# --------------------------------------------------------------------------- #

_DEFAULT = Redactor()


def redact(
    text: str,
    *,
    marker: Marker = DEFAULT_MARKER,
    rules: Sequence[Rule] | None = None,
    extra_rules: Iterable[Rule] = (),
) -> str:
    """Scrub ``text``. See :class:`Redactor` for the arguments."""
    extra_rules = tuple(extra_rules)
    if rules is None and not extra_rules:
        return _DEFAULT.redact(text, marker=marker)
    return Redactor(rules, extra_rules=extra_rules, marker=marker).redact(text)


def scan(
    text: str,
    *,
    rules: Sequence[Rule] | None = None,
    extra_rules: Iterable[Rule] = (),
) -> list[SecretMatch]:
    """Locate secrets in ``text`` without changing it."""
    extra_rules = tuple(extra_rules)
    if rules is None and not extra_rules:
        return _DEFAULT.scan(text)
    return Redactor(rules, extra_rules=extra_rules).scan(text)
