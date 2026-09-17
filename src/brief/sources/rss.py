"""Feed sources: one feed fetch, plus a page fetch for entries too thin to use.

The 200-character threshold lives here, not in either seed. It is this
application's policy about what counts as a usable body, and the `feed-fetch`
and `page-main-text` seeds are deliberately ignorant of it — that split is what
lets either one graduate on its own.

Two decisions worth stating, because both were wrong in the first version:

**Cache validators round-trip.** The seed accepts an ETag and a Last-Modified
and returns whatever the server sent back. If the caller does not persist them,
the conditional request can never fire and every poll downloads the whole feed.
They are read from and written to the database by the caller, keyed by the same
fetch identity as the rows.

**A failed article page does not change the stored item.** If a thin entry's
page will not load, the entry is kept with its short summary — losing a real
item because its page 404s would be worse. But it must be kept *identically*
each time: a body that alternates between the summary and the article text
hashes differently, so one flaky page would store the same entry twice and send
both to the model as if they were separate news. So expansion either succeeds or
leaves the entry exactly as the feed gave it.
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
    state: Mapping[str, str | None] | None = None,
) -> tuple[list[Item], dict[str, str | None]]:
    """Fetch one feed. Returns the items and the validators to store.

    Returning the validators rather than writing them keeps this adapter free
    of the database, per CLAUDE.md rule 5. The caller persists them.
    """
    state = state or {}
    result = fetch_feed(
        str(params["url"]),
        etag=state.get("etag"),
        last_modified=state.get("last_modified"),
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
    return items, {
        "etag": result.get("etag"),
        "last_modified": result.get("last_modified"),
    }


def _expand(url: str, fallback: str) -> str:
    """The article text, or the feed's own summary unchanged.

    Unchanged matters: returning a partial or an empty string on failure would
    give the same entry a different content hash on the next run and store it
    twice.
    """
    try:
        text = page_main_text(url)["text"]
    except PageFetchError:
        return fallback
    return text or fallback
