"""Choose which of the week's items the model gets to see.

App code: the ordering policy is this application's, and it composes four
seeds plus one vendored feature in a fixed order. Each step is somebody
else's tested module; what lives here is the sequence and the reasons for it.

1. **Window** — rows fetched in the last seven days from this profile's
   sources (`db.items_fetched_since`). `fetched_at`, not `published_at`:
   the reader wants what is new to us.
2. **Filter and rank** — `keyword_relevance` drops anything matching
   `none_of`, keeps anything matching `any_of`, and scores each survivor by
   how many distinct terms it hit.
3. **Order** — by hit count, then by recency. Two items that are equally
   on-topic are separated by which is newer, not by row id.
4. **Redact** — `secret-redaction` over every body before it enters a prompt
   or a stored response. Scraped pages are text we did not write, and the
   prompt is persisted in `briefs.raw_response`.
5. **Pack** — `context_packer` fits the ranked list into a character budget
   derived from the provider's own `context_window()`. No provider here
   exposes a tokenizer, so the budget is a documented heuristic, and what did
   not fit is reported rather than silently dropped.

The count that did not fit reaches the reader in the brief's footer. A local
model with an 8k window produces a thin brief, and the footer is what makes
that visible as thinness rather than as the week being quiet.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from brief.lib.context_packer import context_budget_chars, pack_items
from brief.lib.keyword_relevance import filter_ranked
from brief.models import Profile
from brief.vendor.secret_redaction import redact

DEFAULT_CHARS_PER_TOKEN = 4
DEFAULT_OUTPUT_RESERVE_TOKENS = 4000
DEFAULT_PROMPT_OVERHEAD_TOKENS = 1500
DEFAULT_MAX_ITEM_CHARS = 6000

# Bump when `_render_for_prompt` changes shape. It feeds `briefs.prompt_hash`,
# so that two briefs with the same hash really did see the same input. Without
# it, changing how an item is presented to the model leaves the hash identical
# over genuinely different input, and a regeneration diff would be misread as a
# model difference.
ITEM_RENDER_VERSION = 2  # v2 adds the labelled facts lines


@dataclass(frozen=True, slots=True)
class Candidate:
    """One item that survived the filter, with the score that ranked it."""

    id: int
    source: str
    url: str
    title: str
    body: str
    published_at: str | None
    hits: int
    facts: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class Selection:
    """What synthesis will send, and what it had to leave behind."""

    candidates: list[Candidate] = field(default_factory=list)
    rendered: list[tuple[int, str]] = field(default_factory=list)
    considered: int = 0
    matched: int = 0
    left_out: list[int] = field(default_factory=list)
    truncated: list[int] = field(default_factory=list)
    budget_chars: int = 0
    chars_used: int = 0

    @property
    def sent(self) -> int:
        return len(self.rendered)

    def numbered_list(self) -> str:
        """The user turn: items numbered from 1, in the order sent.

        The numbers the model cites are positions in this list, not database
        ids. `citation_map` translates back.
        """
        blocks = []
        for position, (_item_id, text) in enumerate(self.rendered, start=1):
            blocks.append(f"[{position}] {text}")
        return "\n\n".join(blocks)

    def citation_map(self) -> dict[int, int]:
        """Position in the prompt -> database id."""
        return {
            position: item_id
            for position, (item_id, _) in enumerate(self.rendered, start=1)
        }


def select(
    rows: Sequence[Mapping[str, Any]],
    profile: Profile,
    *,
    context_window_tokens: int,
    settings: Mapping[str, Any] | None = None,
) -> Selection:
    """Filter, rank, redact, and pack one week of rows for one profile."""
    cfg = dict(settings or profile.config.get("select") or {})
    chars_per_token = int(cfg.get("chars_per_token", DEFAULT_CHARS_PER_TOKEN))
    overhead_tokens = int(
        cfg.get("prompt_overhead_tokens", DEFAULT_PROMPT_OVERHEAD_TOKENS)
    )
    reserve_tokens = int(
        cfg.get("output_reserve_tokens", DEFAULT_OUTPUT_RESERVE_TOKENS)
    )
    max_item_chars = int(cfg.get("max_item_chars", DEFAULT_MAX_ITEM_CHARS))

    ranked = filter_ranked(
        rows,
        _text_of,
        profile.relevance.any_of,
        profile.relevance.none_of,
    )
    # Two stable passes rather than one clever key: newest first, then by hit
    # count. Python's sort is stable, so the second pass keeps recency as the
    # tiebreak within an equal score.
    ranked.sort(key=lambda pair: _stamp(pair[0]), reverse=True)
    ranked.sort(key=lambda pair: pair[1], reverse=True)

    candidates = [
        Candidate(
            id=int(row["id"]),
            source=str(row["source"]),
            url=str(row["url"]),
            title=str(row["title"] or ""),
            body=str(row["body"] or ""),
            published_at=row.get("published_at"),
            hits=hits,
            facts=_facts_of(row),
        )
        for row, hits in ranked
    ]

    budget = context_budget_chars(
        context_window_tokens,
        chars_per_token=chars_per_token,
        prompt_overhead_chars=overhead_tokens * chars_per_token,
        output_reserve_tokens=reserve_tokens,
    )

    packed = pack_items(
        [(c.id, _render_for_prompt(c)) for c in candidates],
        budget,
        max_item_chars=max_item_chars,
        per_item_overhead_chars=8,  # the "[12] " marker and the blank line between items
        max_items=profile.relevance.max_items,
    )

    return Selection(
        candidates=candidates,
        rendered=list(packed.packed),
        considered=len(rows),
        matched=len(candidates),
        left_out=list(packed.left_out),
        truncated=list(packed.truncated),
        budget_chars=packed.budget_chars,
        chars_used=packed.chars_used,
    )


def _facts_of(row: Mapping[str, Any]) -> dict[str, str] | None:
    """The stored facts mapping, or None. A malformed one is simply absent."""
    raw = row.get("facts_json")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def _text_of(row: Mapping[str, Any]) -> str:
    return f"{row.get('title') or ''}\n\n{row.get('body') or ''}"


def _stamp(row: Mapping[str, Any]) -> str:
    """The date to rank by: what the source published, else when we saw it.

    ISO 8601 sorts lexicographically, so no parsing is needed here. An item
    with neither date sorts oldest, which is the right default for something
    whose age we cannot establish.
    """
    return str(row.get("published_at") or row.get("fetched_at") or "")


def _render_for_prompt(candidate: Candidate) -> str:
    """One item as the model sees it: source, title, date, url, then body.

    Redaction happens here, at the single point where item text becomes
    prompt text, so no caller can route around it.
    """
    header = (
        f"{candidate.source} · {candidate.published_at or 'undated'}\n"
        f"{candidate.title}\n{candidate.url}"
    )
    if candidate.facts:
        # One line per named value, in the order the adapter set them. These are
        # what the prompt tells the model to quote verbatim — the PI, the amount,
        # the sponsor — so they are presented as labelled values rather than
        # buried in prose it would have to infer them from.
        header += "\n" + "\n".join(f"{k}: {v}" for k, v in candidate.facts.items())
    body = candidate.body.strip()
    text = f"{header}\n\n{body}" if body else header
    return redact(text)
