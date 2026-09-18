"""Rust crates matching a query, from the crates.io registry.

A package registry is where a tool exists first; the paper describing it
lands months later or never. `since` bounds the window locally rather than in
the query, because the registry search has no date filter — it sorts, and the
adapter stops reading once the sort has walked past the window.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brief.lib.crates_io_search import search_crates
from brief.models import Item

#: The registry sorts by date but does not filter by one, so history is only
#: reachable by reading further back — which `max_pages` bounds. Treated as
#: no history rather than pretending to a window it cannot honour.
SUPPORTS_HISTORY = False


def fetch(
    source: str,
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
) -> list[Item]:
    sort = str(params.get("sort", "new"))
    crates = search_crates(
        str(params["query"]),
        sort=sort,
        per_page=int(params.get("per_page", 100)),
        max_pages=int(params.get("max_pages", 3)),
        user_agent=str(params["user_agent"]),
    )
    cutoff = since.date().isoformat()
    return [
        Item(
            source=source,
            url=c["url"],
            title=_title(c),
            body=c["description"],
            external_id=c["name"] or None,
            published_at=c["published_at"] or None,
            raw=c.get("raw"),
            facts=_facts(c),
        )
        for c in crates
        # The registry cannot filter by date, so the window is applied here.
        # An undated record is kept: we cannot show it is old.
        if c["name"] and (not c["published_at"] or c["published_at"][:10] >= cutoff)
    ]


def _title(c: Mapping[str, Any]) -> str:
    version = c.get("version")
    return f"{c['name']} {version}".strip() if version else str(c["name"])


def _labels(c: Mapping[str, Any]) -> list[str]:
    """Keywords and categories as published on crates.io."""
    return [str(x) for x in list(c.get("keywords") or []) + list(c.get("categories") or []) if x]


def _facts(c: Mapping[str, Any]) -> dict[str, str]:
    pairs = (
        ("Version", c.get("version")),
        ("Repository", c.get("repository")),
        ("Downloads", f"{c['downloads']:,}" if c.get("downloads") else ""),
        ("Keywords", ", ".join(c.get("keywords") or [])),
    )
    return {k: str(v) for k, v in pairs if v}
