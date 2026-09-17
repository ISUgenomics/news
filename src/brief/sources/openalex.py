"""OpenAlex works for an institution, in a date window.

Where `pubmed` asks one database for an affiliation string, this asks OpenAlex
for a resolved institution id across every discipline. Measured over one
30-day window at one university: PubMed 99 works, OpenAlex 338.

The institution id is profile data and is required. The seed refuses a bare
name on purpose — see its docstring — so a profile pins an id that a human
looked up once.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brief.lib.openalex_works_search import search_openalex_works
from brief.models import Item

#: OpenAlex filters on publication_date, so any past window is reachable.
SUPPORTS_HISTORY = True


def fetch(
    source: str,
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
) -> list[Item]:
    works = search_openalex_works(
        str(params["institution"]),
        from_date=since.date().isoformat(),
        to_date=now.date().isoformat(),
        search=params.get("search") or None,
        mailto=str(params["mailto"]),
        per_page=int(params.get("per_page", 200)),
        max_pages=int(params.get("max_pages", 10)),
    )
    return [
        Item(
            source=source,
            url=w["url"],
            title=w["title"],
            body=w["abstract"],
            external_id=w["external_id"] or None,
            published_at=w["published_at"] or None,
            raw=w.get("raw"),
            facts=_facts(w),
        )
        for w in works
        if w["title"] and w["url"]
    ]


def _facts(w: Mapping[str, Any]) -> dict[str, str]:
    """Quoted verbatim, in display order. The brief cites these, so they are
    never reformatted here."""
    authors = w.get("authors") or []
    pairs = (
        ("Authors", ", ".join(authors)),
        ("Journal", w.get("journal")),
        ("DOI", w.get("doi")),
        ("PMID", w.get("pmid")),
    )
    return {k: str(v) for k, v in pairs if v}
