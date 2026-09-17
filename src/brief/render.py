"""Turn a validated result into the page a reader receives.

App code. The model never writes Markdown — it returns structured data and
this renders it — so formatting cannot drift between weeks, profiles, or
providers. The `cited_digest_render` seed does the rendering and the citation
enforcement; what lives here is this brief's voice: its title, its week
subtitle, and its footer.

The footer is not decoration. It carries the provenance line that lets the VPR
office forward the page without vouching for it personally, the provider that
produced it, and the "n of m candidates" count that makes a thin week
distinguishable from a quiet one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from brief.lib.cited_digest_render import DroppedEntry, render_digest
from brief.models import Profile
from brief.select import Selection

PROVENANCE = (
    "Prepared automatically by the bioinformatics facility from public sources; "
    "every statement links to its source."
)


@dataclass(frozen=True, slots=True)
class RenderedBrief:
    markdown: str
    kept: int
    dropped: list[DroppedEntry]

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


def render(
    result: Mapping[str, Any],
    profile: Profile,
    selection: Selection,
    *,
    week_start: str,
    provider_name: str,
    item_urls: Mapping[int, str],
) -> RenderedBrief:
    """Render one week's result for one profile.

    `result` cites *positions* in the prompt, not database ids, so the two are
    translated here before rendering. An entry citing a position that was
    never sent is dropped by the seed and counted in the footer — the check
    that makes the page safe to forward.
    """
    positions = selection.citation_map()
    urls = {
        position: item_urls[item_id]
        for position, item_id in positions.items()
        if item_id in item_urls
    }

    rendered = render_digest(
        result,
        urls,
        title=profile.title,
        subtitle=f"week of {week_start}",
        bucket_order=[b.name for b in profile.buckets],
        footer="",
        link_label=lambda position: str(positions.get(position, position)),
    )

    footer = _footer(
        profile=profile,
        selection=selection,
        provider_name=provider_name,
        dropped=len(rendered.dropped),
    )
    markdown = f"{rendered.markdown.rstrip()}\n\n---\n\n{footer}\n"
    return RenderedBrief(
        markdown=markdown, kept=rendered.kept, dropped=list(rendered.dropped)
    )


def _footer(
    *,
    profile: Profile,
    selection: Selection,
    provider_name: str,
    dropped: int,
) -> str:
    """The provenance line, then one line of counts.

    Every number here is one the operator would otherwise have to read a log
    to learn, and one a reader benefits from seeing: how much was considered,
    how much fitted, what was thrown away for lacking a citation.
    """
    counts = [
        f"{selection.sent} of {selection.matched} matching items",
        f"{selection.considered} considered",
    ]
    if selection.left_out:
        counts.append(f"{len(selection.left_out)} did not fit the context window")
    if selection.truncated:
        counts.append(f"{len(selection.truncated)} truncated")
    if dropped:
        counts.append(f"{dropped} entries dropped for missing or unknown citations")

    return (
        f"*{PROVENANCE}*\n\n"
        f"*Profile `{profile.name}` · {provider_name} · " + " · ".join(counts) + ".*"
    )


def stub_markdown(
    profile: Profile,
    *,
    week_start: str,
    reason: str,
    candidates: int,
) -> str:
    """The page sent when a brief could not be generated.

    Silence is ambiguous: a reader cannot tell "nothing happened" from "the
    pipeline broke". This says which, names the candidate count so the
    operator knows whether the week had material, and repeats the fix.
    """
    return (
        f"# {profile.title} — week of {week_start}\n\n"
        f"**No brief was generated this week.**\n\n"
        f"{reason}\n\n"
        f"{candidates} candidate items were gathered, so the sources are "
        f"{'working' if candidates else 'worth checking'}.\n\n"
        f"---\n\n*{PROVENANCE}*\n"
    )
