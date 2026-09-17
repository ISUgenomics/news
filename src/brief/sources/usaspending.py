"""USAspending awards. The only federal source that covers USDA/NIFA.

Its data lags awards by weeks, so a profile relying on it for timeliness will
be disappointed; it is here for coverage, not speed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brief.lib.usaspending_award_search import search_awards
from brief.models import Item

#: time_period is a real filter, so history is reachable — and this is the only
#: source covering USDA/NIFA, which is where a deep fetch earns its keep.
SUPPORTS_HISTORY = True


def fetch(
    source: str,
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
) -> list[Item]:
    records = search_awards(
        start_date=since.date().isoformat(),
        end_date=now.date().isoformat(),
        recipient=params.get("recipient"),
        keywords=list(params["keywords"]) if params.get("keywords") else None,
        award_types=tuple(params.get("award_types", ("grants", "contracts"))),
        date_type=str(params.get("date_type", "action_date")),
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
            facts=_facts(r),
        )
        for r in records
    ]


def _facts(r: Mapping[str, Any]) -> dict[str, str]:
    """No PI: USAspending names the recipient institution, not a person."""
    pairs = (
        ("Amount", r.get("amount")),
        ("Sponsor", r.get("awarding_agency")),
        ("Sub-agency", r.get("awarding_sub_agency")),
        ("Recipient", r.get("recipient")),
        ("Period start", r.get("start_date")),
    )
    return {k: str(v) for k, v in pairs if v not in (None, "")}
