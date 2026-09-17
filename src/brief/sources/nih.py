"""NIH RePORTER projects. An institution filter, or a text search."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brief.lib.nih_reporter_search import fetch_projects
from brief.models import Item

#: RePORTER takes a date window in its criteria, so history is reachable.
SUPPORTS_HISTORY = True


def fetch(
    source: str,
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
) -> list[Item]:
    records = fetch_projects(
        org_names=list(params["org_names"]) if params.get("org_names") else None,
        advanced_text_search=params.get("advanced_text_search"),
        start_from=since.date().isoformat(),
        start_to=now.date().isoformat(),
        date_field=str(params.get("date_field", "project_start_date")),
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
    names = r.get("pi_names") or []
    pairs = (
        ("PI", ", ".join(names) if isinstance(names, list) else names),
        ("Amount", r.get("amount")),
        ("Sponsor", r.get("agency") or "NIH"),
        ("Awardee", r.get("org_name")),
        ("Fiscal year", r.get("fiscal_year")),
    )
    return {k: str(v) for k, v in pairs if v not in (None, "", [])}
