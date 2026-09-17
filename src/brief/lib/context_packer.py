"""Pack ranked text items into a character budget, capping each, and say what was left out.

The situation: one LLM call has to carry as many candidate items as fit, there is no
tokenizer (CLI and Ollama providers expose only a context-window hint), and the
caller must be able to tell the reader "n of m candidates" honestly. This module is
the arithmetic and the loop, nothing else.

Contract:
- ``context_budget_chars(window_tokens, chars_per_token=4, prompt_overhead_chars=0,
  output_reserve_tokens=4096)`` -> chars available for the item block, floored at 0.
- ``pack_items(items, budget_chars, *, max_item_chars=None, per_item_overhead_chars=0,
  max_items=None, truncation_marker="…")`` -> ``PackResult``. ``items`` are
  ``(key, text)`` pairs already in rank order; keys are opaque and returned as given.
- Fitting is first-fit-then-stop: the packed list is a strict prefix of the input, so
  rank order is honored and ``left_out`` is everything after the first item that did
  not fit. It does not skip ahead to squeeze in a smaller lower-ranked item; that
  would silently reorder by size, not rank.
- Capping happens before fitting; a capped text ends with ``truncation_marker`` and
  its key is listed in ``truncated``. An item that exceeds the whole budget even after
  capping is left out, not partially packed.
- Pure and deterministic. Same inputs, same result, no I/O, no logging.

Deliberately not here: ranking or relevance filtering (the caller decides order),
rendering an item to prompt text (the caller passes the final string), token
counting (chars_per_token is a stated assumption, not a measurement), and any
notion of provider, profile, or database. Zero dependencies: stdlib dataclasses only.

Notes that the contract above leaves implicit:

- ``chars_per_token`` defaults to 4 to match ``DEFAULT_CHARS_PER_TOKEN`` in the
  library's hybrid-rag-retrieval feature. The two features estimate the same way on
  purpose; change one and the other stops agreeing.
- ``truncation_marker`` defaults to ``"…"``, a single non-ASCII character, and it
  **counts toward** ``max_item_chars``: a capped text is never longer than the cap,
  marker included. When the cap is too small to hold the marker, the text is cut to
  the cap and no marker is added.
- ``PackResult.packed`` carries the **capped** text, not the original. A caller that
  cites by key must send the string this module returned; re-rendering from the
  source record would silently ship the uncapped body the budget was meant to
  exclude.
- ``truncated`` lists only keys that were actually packed. An item that was capped
  and then did not fit is reported in ``left_out`` alone — nothing about it reached
  the model.
- Lengths are Python ``str`` lengths (code points), not bytes and not grapheme
  clusters. Budgets are estimates; this module is exact about the estimate it states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Sequence, TypeVar

K = TypeVar("K")

DEFAULT_CHARS_PER_TOKEN = 4
DEFAULT_OUTPUT_RESERVE_TOKENS = 4096
DEFAULT_TRUNCATION_MARKER = "…"


def context_budget_chars(
    context_window_tokens: int,
    *,
    chars_per_token: int = DEFAULT_CHARS_PER_TOKEN,
    prompt_overhead_chars: int = 0,
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
) -> int:
    """Characters available for the item block, floored at 0.

    ``(context_window_tokens - output_reserve_tokens) * chars_per_token`` minus
    ``prompt_overhead_chars``, never below 0. Raises ``ValueError`` if
    ``chars_per_token`` is not positive or either of the other two is negative.
    """
    if chars_per_token < 1:
        raise ValueError(f"chars_per_token must be >= 1, got {chars_per_token}")
    if prompt_overhead_chars < 0:
        raise ValueError(
            f"prompt_overhead_chars must be >= 0, got {prompt_overhead_chars}"
        )
    if output_reserve_tokens < 0:
        raise ValueError(
            f"output_reserve_tokens must be >= 0, got {output_reserve_tokens}"
        )
    usable_tokens = context_window_tokens - output_reserve_tokens
    return max(0, usable_tokens * chars_per_token - prompt_overhead_chars)


@dataclass(frozen=True)
class PackResult(Generic[K]):
    """What fit, what did not, and what was cut on the way in.

    ``frozen=True`` blocks rebinding a field, not mutation of the three lists;
    they are fresh lists built by ``pack_items`` and never alias the caller's
    input, so a caller that mutates one only damages its own copy.
    """

    packed: list[tuple[K, str]] = field(default_factory=list)
    left_out: list[K] = field(default_factory=list)
    truncated: list[K] = field(default_factory=list)
    chars_used: int = 0
    budget_chars: int = 0


def cap_text(
    text: str, max_chars: int, marker: str = DEFAULT_TRUNCATION_MARKER
) -> tuple[str, bool]:
    """Return (text capped to max_chars including marker, whether it was capped).

    The returned text is never longer than ``max_chars`` code points, marker
    included. A cap too small to hold the marker cuts the text and omits the
    marker rather than returning something longer than the cap. A negative
    ``max_chars`` is treated as 0, so empty text is still reported as not
    capped: the flag means "characters were removed", never "a cap was in
    force". Raises ``TypeError`` if ``text`` is not a ``str``.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")
    if max_chars < 0:
        max_chars = 0
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 0:
        return "", True
    if max_chars <= len(marker):
        return text[:max_chars], True
    return text[: max_chars - len(marker)] + marker, True


def pack_items(
    items: Sequence[tuple[K, str]],
    budget_chars: int,
    *,
    max_item_chars: int | None = None,
    per_item_overhead_chars: int = 0,
    max_items: int | None = None,
    truncation_marker: str = DEFAULT_TRUNCATION_MARKER,
) -> PackResult[K]:
    """Fit a ranked prefix of items into budget_chars and report the remainder.

    Walks ``items`` in order, caps each text to ``max_item_chars`` when given, and
    stops at the first item whose capped length plus ``per_item_overhead_chars``
    would take ``chars_used`` past ``budget_chars`` — or at ``max_items``. That item
    and everything after it go to ``left_out`` in input order. ``truncated`` names
    only the keys that were both capped and packed.

    A ``budget_chars`` below 0 packs nothing, the same as 0; it is echoed into
    ``PackResult.budget_chars`` exactly as given rather than clamped, so the
    caller sees the number it passed. Raises ``ValueError`` for a negative
    ``per_item_overhead_chars``, ``max_items`` or ``max_item_chars``, and
    ``TypeError`` if any item's text is not a ``str``.
    """
    if per_item_overhead_chars < 0:
        raise ValueError(
            f"per_item_overhead_chars must be >= 0, got {per_item_overhead_chars}"
        )
    if max_items is not None and max_items < 0:
        raise ValueError(f"max_items must be >= 0, got {max_items}")
    if max_item_chars is not None and max_item_chars < 0:
        raise ValueError(f"max_item_chars must be >= 0, got {max_item_chars}")

    packed: list[tuple[K, str]] = []
    left_out: list[K] = []
    truncated: list[K] = []
    chars_used = 0
    stopped = False

    for key, text in items:
        if stopped:
            left_out.append(key)
            continue
        if max_items is not None and len(packed) >= max_items:
            stopped = True
            left_out.append(key)
            continue
        if max_item_chars is None:
            if not isinstance(text, str):
                raise TypeError(f"text must be str, got {type(text).__name__}")
            was_capped = False
        else:
            text, was_capped = cap_text(text, max_item_chars, truncation_marker)
        cost = len(text) + per_item_overhead_chars
        if chars_used + cost > budget_chars:
            stopped = True
            left_out.append(key)
            continue
        packed.append((key, text))
        chars_used += cost
        if was_capped:
            truncated.append(key)

    return PackResult(
        packed=packed,
        left_out=left_out,
        truncated=truncated,
        chars_used=chars_used,
        budget_chars=budget_chars,
    )
