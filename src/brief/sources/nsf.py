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
from brief.sources._format import money

#: dateStart/dateEnd filter on the award's effective date, so this reaches back
#: as far as the range asks. Measured: 190 awards over three years.
SUPPORTS_HISTORY = True


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
            facts=_facts(r),
        )
        for r in records
    ]


def _labels(r: Mapping[str, Any]) -> list[str]:
    """The program line, one label per comma-separated program element."""
    program = str(r.get("program") or "")
    return [part.strip() for part in program.split(",") if part.strip()]


def _facts(r: Mapping[str, Any]) -> dict[str, str]:
    """The named details a brief quotes verbatim: PI, amount, sponsor.

    The seed already extracts these; without this mapping they never reach the
    model, which is then asked for a PI it has not been shown.
    """
    pairs = (
        ("PI", r.get("pi_name")),
        ("Amount", money(r.get("amount"))),
        ("Sponsor", r.get("agency") or "NSF"),
        ("Program", r.get("program")),
        ("Awardee", r.get("awardee")),
    )
    return {k: str(v) for k, v in pairs if v not in (None, "")}
