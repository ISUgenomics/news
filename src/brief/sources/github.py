"""GitHub repositories matching a search query.

Software shows up on a code host before it reaches a package registry, and
long before a paper. The query is GitHub's own syntax, set per profile —
`created:>` for what is new, `pushed:>` for what is alive.

Forks and archived repositories are dropped here, not in the seed: a digest
about new work does not want either, but that is this app's judgement and
the seed leaves it to the caller. Set `include_forks`/`include_archived` to
keep them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from brief.lib.github_repo_search import search_repositories
from brief.models import Item

#: The query itself carries the date qualifier, so any past window is
#: reachable — the adapter substitutes `since` into it.
SUPPORTS_HISTORY = True

#: Replaced with the window start, so one profile query serves every run.
SINCE_TOKEN = "{since}"


def fetch(
    source: str,
    params: Mapping[str, Any],
    *,
    since: datetime,
    now: datetime,
) -> list[Item]:
    query = str(params["query"]).replace(SINCE_TOKEN, since.date().isoformat())
    repos = search_repositories(
        query,
        sort=params.get("sort") or None,
        per_page=int(params.get("per_page", 100)),
        max_pages=int(params.get("max_pages", 3)),
        token=params.get("token") or None,
        user_agent=str(params["user_agent"]),
    )
    include_forks = bool(params.get("include_forks", False))
    include_archived = bool(params.get("include_archived", False))
    return [
        Item(
            source=source,
            url=r["url"],
            title=_title(r),
            body=r["description"],
            external_id=r["full_name"] or None,
            published_at=r["published_at"] or None,
            raw=r.get("raw"),
            facts=_facts(r),
        )
        for r in repos
        if r["full_name"]
        and r["url"]
        and (include_forks or not r["fork"])
        and (include_archived or not r["archived"])
    ]


def _title(r: Mapping[str, Any]) -> str:
    language = r.get("language")
    return f"{r['full_name']} ({language})" if language else str(r["full_name"])


def _labels(r: Mapping[str, Any]) -> list[str]:
    """Repository topics as the owner set them, plus the primary language."""
    return [str(t) for t in (r.get("topics") or []) if t] + (
        [str(r["language"])] if r.get("language") else []
    )


def _facts(r: Mapping[str, Any]) -> dict[str, str]:
    pairs = (
        ("Stars", f"{r['stars']:,}" if r.get("stars") else ""),
        ("Language", r.get("language")),
        ("Topics", ", ".join(r.get("topics") or [])),
        ("License", r.get("license")),
        ("Last pushed", (r.get("pushed_at") or "")[:10]),
    )
    return {k: str(v) for k, v in pairs if v}
