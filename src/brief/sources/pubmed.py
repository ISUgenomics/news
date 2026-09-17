"""PubMed articles for a saved query, via E-utilities.

The design review corrected the spec here: PubMed's saved-search RSS ids are
minted server-side and cannot be built from a query string, so a query becomes
an esearch plus an esummary rather than a feed URL. A hand-minted PubMed RSS
URL is still usable — as an ordinary `rss` source.

The body is a formatted citation line, because a PubMed summary record has no
abstract and the journal, authors, and date are what make an entry judgeable.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brief.lib.pubmed_search import citation_line, search_pubmed
from brief.models import Item

#: E-utilities takes mindate/maxdate, so history is reachable.
SUPPORTS_HISTORY = True


def fetch(
    source: str,
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
) -> list[Item]:
    records = search_pubmed(
        str(params["query"]),
        email=str(params["email"]),
        mindate=since.date().strftime("%Y/%m/%d"),
        maxdate=now.date().strftime("%Y/%m/%d"),
        datetype=str(params.get("datetype", "edat")),
        max_results=int(params.get("max_results", 200)),
        api_key=params.get("api_key"),
    )
    return [
        Item(
            source=source,
            url=r["url"],
            title=r["title"],
            body=citation_line(r),
            external_id=r.get("external_id"),
            published_at=r.get("published_at"),
            raw=r.get("raw"),
        )
        for r in records
    ]
