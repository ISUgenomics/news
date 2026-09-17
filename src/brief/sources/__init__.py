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

Fetch = Callable[..., list[Item]]

ADAPTERS: dict[str, Fetch] = {
    "rss": rss.fetch,
    "nsf": nsf.fetch,
    "nih": nih.fetch,
    "usaspending": usaspending.fetch,
    "pubmed": pubmed.fetch,
}


class UnknownSourceKind(ValueError):
    """`sources.yaml` names an adapter that does not exist."""


def fetch(
    source_name: str,
    spec: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
) -> list[Item]:
    """Dispatch one source to its adapter.

    `spec` is the entry from `sources.yaml`; `params` is what the profile
    supplied inline. Profile params win, which is what lets one `nsf` source
    serve a campus profile and a field profile.
    """
    kind = str(spec.get("kind", ""))
    adapter = ADAPTERS.get(kind)
    if adapter is None:
        raise UnknownSourceKind(
            f"source {source_name!r} has kind {kind!r}; known kinds are {sorted(ADAPTERS)}"
        )
    merged = {**{k: v for k, v in spec.items() if k != "kind"}, **params}
    return adapter(source_name, merged, since=since, now=now)


__all__ = ["ADAPTERS", "Item", "UnknownSourceKind", "fetch"]
