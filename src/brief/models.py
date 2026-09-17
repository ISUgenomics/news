"""The two shapes the app passes around: a stored `Item` and a loaded `Profile`.

This module is app code, not a seed. It is the one place that knows both the
database columns and the profile file's field names, which is exactly why no
module under ``brief/lib/`` may import it: a seed that accepts an ``Item``
cannot graduate.

Adapters build ``Item``s. ``db.py`` stamps ``fetched_at`` and computes
``content_hash``; an adapter that sets either of those is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class Item:
    """One thing a source produced. Plain data; no behavior, no db handle."""

    source: str
    url: str
    title: str
    body: str = ""
    external_id: str | None = None
    published_at: str | None = None
    raw: dict[str, Any] | None = None

    def text(self) -> str:
        """Title and body as one blob, for keyword matching and packing."""
        return f"{self.title}\n\n{self.body}".strip()


@dataclass(frozen=True, slots=True)
class Bucket:
    """One section of the brief, in the order the profile lists it."""

    name: str
    ask: str


@dataclass(frozen=True, slots=True)
class Relevance:
    any_of: tuple[str, ...] = ()
    none_of: tuple[str, ...] = ()
    max_items: int = 80


@dataclass(frozen=True, slots=True)
class Delivery:
    sender: str
    to: tuple[str, ...]
    subject: str = "{title} — week of {week_start} ({n} items)"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A profile's reference to a source in sources.yaml, plus its overrides.

    ``params`` is what the profile supplied inline (``nsf: {awardee: "..."}``).
    Two profiles naming the same source with different params are two fetches;
    naming it with identical params is one.
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        """Identity for fetch deduplication across profiles."""
        import json

        return (self.name, json.dumps(self.params, sort_keys=True))


@dataclass(frozen=True, slots=True)
class Profile:
    """One brief: its topic, sources, shape, recipients, and provider."""

    name: str
    title: str
    audience: str
    persona: str
    sources: tuple[SourceRef, ...]
    relevance: Relevance
    buckets: tuple[Bucket, ...]
    delivery: Delivery
    llm: dict[str, Any] = field(default_factory=dict)
    extra_rules: tuple[str, ...] = ()
    cadence: str = "weekly"
    config: dict[str, Any] = field(default_factory=dict)

    def with_config(self, config: dict[str, Any]) -> Profile:
        return replace(self, config=config)
