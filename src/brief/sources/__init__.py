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
from brief.sources import crates, github, nih, nsf, openalex, pubmed, rss, usaspending
from brief.sources._format import money

Fetch = Callable[..., list[Item]]

#: kind -> the module that implements it. ONE registry, deliberately.
#: There were two — this table and a second copy inside supports_history —
#: and adding `openalex` to the first alone left backfill silently skipping
#: it, because the second had never heard of it. A kind is one line here.
MODULES = {
    "rss": rss,
    "openalex": openalex,
    "crates": crates,
    "github": github,
    "nsf": nsf,
    "nih": nih,
    "usaspending": usaspending,
    "pubmed": pubmed,
}

ADAPTERS: dict[str, Fetch] = {kind: module.fetch for kind, module in MODULES.items()}


def supports_history(kind: str) -> bool:
    """Can this adapter reach further back than its default window?

    The award and literature APIs take a date range and genuinely can. A feed
    cannot: it serves its most recent entries and has no date parameter, so a
    deep ingest that silently included feeds would look like it had fetched
    history it never reached.
    """
    return bool(getattr(MODULES.get(kind), "SUPPORTS_HISTORY", False))


#: How to recompute an item's derived fields from the raw record a source
#: returned. Each entry re-runs the seed's own normalizer and the adapter's own
#: facts mapping, so reindex cannot drift from ingest: they call the same code.
def rederive(source: str, raw: dict) -> tuple[dict[str, str] | None, str | None]:
    """Return ``(facts, published_at)`` recomputed from a stored raw record.

    Returns ``(None, None)`` for a source with no normalizer — a feed entry has
    no raw record to re-derive from, and guessing would be worse than leaving it.
    """
    if source == "nsf":
        from brief.lib.nsf_award_search import normalize_award

        record = normalize_award(raw)
        return nsf._facts(record), record.get("published_at")
    if source == "nih":
        from brief.lib.nih_reporter_search import normalize_project

        record = normalize_project(raw)
        return nih._facts(record), record.get("published_at")
    if source == "usaspending":
        from brief.lib.usaspending_award_search import normalize_award

        record = normalize_award(raw, award_type_group="grants")
        return usaspending._facts(record), record.get("published_at")
    if source.startswith("github"):
        from brief.lib.github_repo_search import normalize_repo

        record = normalize_repo(raw)
        return github._facts(record), record.get("published_at")
    if source.startswith("openalex"):
        from brief.lib.openalex_works_search import normalize_work

        record = normalize_work(raw)
        return openalex._facts(record), record.get("published_at")
    if source.startswith("pubmed"):
        from brief.lib.pubmed_search import record_to_item

        record = record_to_item(raw)
        return pubmed._facts(record), record.get("published_at")
    return None, None


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


__all__ = ["ADAPTERS", "MODULES", "Item", "UnknownSourceKind", "fetch", "money", "rederive", "supports_history"]
