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
    """One thing a source produced. Plain data; no behavior, no db handle.

    ``facts`` is the short, named detail a brief quotes rather than summarises:
    the PI, the amount, the sponsor. The seeds already extract these — the app
    dropped them for a while, so the model was asked for a PI it had never been
    shown. Kept separate from ``body`` because they are values to be repeated
    verbatim, not prose to be read, and insertion order is the display order.
    """

    source: str
    url: str
    title: str
    body: str = ""
    external_id: str | None = None
    published_at: str | None = None
    raw: dict[str, Any] | None = None
    facts: dict[str, str] | None = None

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


DEFAULT_SUBJECT = "{title} — week of {week_start} ({n} items)"

#: A ceiling on the brief, not a target. Measured: a 600-word cap made the model
#: drop items on a busy week, and a reader would rather hear about an award than
#: have it rationed away. Generous enough that a heavy week is covered; present
#: at all so a runaway answer is still bounded.
DEFAULT_MAX_WORDS = 1500


@dataclass(frozen=True, slots=True)
class Delivery:
    """Who receives the brief, and under what subject line.

    ``DEFAULT_SUBJECT`` is a module constant rather than only a field default
    because ``slots=True`` turns the class attribute into a member descriptor:
    ``Delivery.subject`` is not the string, and reading it as one emails
    ``<member 'subject' of 'Delivery' objects>`` to a real person.
    """

    sender: str
    to: tuple[str, ...]
    subject: str = DEFAULT_SUBJECT


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

    def source_key(self) -> str:
        """What the rows this reference produces are stored under.

        The source *name* is not enough. Two profiles may reference the same
        source with different parameters — a campus brief asking NSF for one
        awardee, a faculty brief asking NSF for a keyword — and both would
        otherwise write rows under `nsf`, so each profile's window would pick
        up the other's awards. The key carries the parameters, so the two
        fetches stay separate in storage as well as at fetch time.

        Bare `name` when there are no parameters, so the common case stays
        readable in the database and in a log line.
        """
        import hashlib
        import json

        if not self.params:
            return self.name
        blob = json.dumps(self.params, sort_keys=True).encode("utf-8")
        return f"{self.name}#{hashlib.sha256(blob).hexdigest()[:8]}"


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
    max_words: int = DEFAULT_MAX_WORDS
    cadence: str = "weekly"
    config: dict[str, Any] = field(default_factory=dict)

    def with_config(self, config: dict[str, Any]) -> Profile:
        return replace(self, config=config)
