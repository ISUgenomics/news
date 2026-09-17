"""Feed sources: one feed fetch, plus a page fetch for entries too thin to use.

The 200-character threshold lives here, not in either seed. It is this
application's policy about what counts as a usable body, and the `feed-fetch`
and `page-main-text` seeds are deliberately ignorant of it — that split is what
lets either one graduate on its own.

A page that will not load is not a failed feed. The entry is kept with its
short summary and a debug note; losing a real item because its article page
404s would be worse than a thin entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brief.lib.feed_fetch import fetch_feed
from brief.lib.page_main_text import PageFetchError, page_main_text
from brief.models import Item

THIN_SUMMARY_CHARS = 200


def fetch(
    source: str,
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
) -> list[Item]:
    result = fetch_feed(
        str(params["url"]),
        etag=params.get("etag"),
        last_modified=params.get("last_modified"),
        timeout_s=float(params.get("timeout_s", 30.0)),
    )

    expand = bool(params.get("expand_thin_entries", True))
    threshold = int(params.get("thin_summary_chars", THIN_SUMMARY_CHARS))

    items: list[Item] = []
    for entry in result["entries"]:
        body = entry.get("summary") or ""
        if expand and len(body) < threshold and entry.get("url"):
            body = _expand(entry["url"], body)
        items.append(
            Item(
                source=source,
                url=entry["url"],
                title=entry.get("title") or entry["url"],
                body=body,
                external_id=entry.get("id"),
                published_at=entry.get("published_at"),
            )
        )
    return items


def _expand(url: str, fallback: str) -> str:
    try:
        return page_main_text(url)["text"]
    except PageFetchError:
        return fallback
