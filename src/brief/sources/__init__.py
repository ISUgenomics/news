"""Adapters: the only place that knows both a seed's API and the `Item` shape.

Each adapter is thin by rule (CLAUDE.md 5). It maps a profile's parameters and
a `since` datetime onto one seed call and turns the plain dicts that come back
into `Item`s. It holds no HTTP, no parsing, and no retry logic — all of that
belongs to the seed, where it is tested against a stub server — and it never
writes to the database.

`ADAPTERS` maps the `kind` field in `sources.yaml` to a fetch function. Adding
a source kind is a new module here plus one line in this table.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from brief.models import Item
from brief.sources import nih, nsf, pubmed, rss, usaspending
from brief.sources._format import money

Fetch = Callable[..., list[Item]]

ADAPTERS: dict[str, Fetch] = {
    "rss": rss.fetch,
    "nsf": nsf.fetch,
    "nih": nih.fetch,
    "usaspending": usaspending.fetch,
    "pubmed": pubmed.fetch,
}


def supports_history(kind: str) -> bool:
    """Can this adapter reach further back than its default window?

    The award and literature APIs take a date range and genuinely can. A feed
    cannot: it serves its most recent entries and has no date parameter, so a
    deep ingest that silently included feeds would look like it had fetched
    history it never reached.
    """
    module = {
        "rss": rss, "nsf": nsf, "nih": nih,
        "usaspending": usaspending, "pubmed": pubmed,
    }.get(kind)
    return bool(getattr(module, "SUPPORTS_HISTORY", False))


class UnknownSourceKind(ValueError):
    """`sources.yaml` names an adapter that does not exist."""


def fetch(
    source_name: str,
    spec: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
    state: Mapping[str, Any] | None = None,
) -> tuple[list[Item], dict[str, Any]]:
    """Dispatch one source to its adapter.

    `spec` is the entry from `sources.yaml`; `params` is what the profile
    supplied inline. Profile params win, which is what lets one `nsf` source
    serve a campus profile and a field profile.

    Returns `(items, state)`. `state` is whatever the adapter wants remembered
    until next run — cache validators for a feed, nothing for the award APIs.
    The caller persists it; no adapter touches the database.
    """
    kind = str(spec.get("kind", ""))
    adapter = ADAPTERS.get(kind)
    if adapter is None:
        raise UnknownSourceKind(
            f"source {source_name!r} has kind {kind!r}; known kinds are {sorted(ADAPTERS)}"
        )
    merged = {**{k: v for k, v in spec.items() if k != "kind"}, **params}
    if kind == "rss":
        return adapter(source_name, merged, since=since, now=now, state=state or {})
    return adapter(source_name, merged, since=since, now=now), {}


__all__ = ["ADAPTERS", "Item", "UnknownSourceKind", "fetch", "money", "supports_history"]
