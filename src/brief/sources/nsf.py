"""NSF awards. The profile supplies the filter: an awardee, or a keyword.

Pass `awardee_state` alongside `awardee` for an institution search. NSF's
`awardeeName` is not an exact filter — the seed enforces the institution
locally either way, but the state code is what stops the query fetching several
hundred unrelated awards first.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brief.lib.nsf_award_search import search_nsf_awards
from brief.models import Item


def fetch(
    source: str,
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
) -> list[Item]:
    records = search_nsf_awards(
        awardee=params.get("awardee"),
        awardee_state=params.get("awardee_state"),
        keyword=params.get("keyword"),
        pi_name=params.get("pi_name"),
        date_start=since.date(),
        date_end=now.date(),
    )
    return [
        Item(
            source=source,
            url=r["url"],
            title=r["title"],
            body=r.get("body") or "",
            external_id=r.get("external_id"),
            published_at=r.get("published_at"),
            raw=r.get("raw"),
        )
        for r in records
    ]
