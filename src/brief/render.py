"""Turn a validated result into the page a reader receives.

App code. The model never writes Markdown — it returns structured data and
this renders it — so formatting cannot drift between weeks, profiles, or
providers. The `cited_digest_render` seed does the rendering and the citation
enforcement; what lives here is this brief's voice: its title, its week
subtitle, and its footer.

The footer is not decoration. It carries the provenance line that lets a reader
forward the page without vouching for it personally, the provider that produced
it, and the "n of m candidates" count that makes a thin week distinguishable
from a quiet one. That line is configuration, not code: which organization
prepares a brief is a fact about a deployment, not about this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from brief.lib.cited_digest_render import DroppedEntry, render_digest
from brief.models import Profile
from brief.select import Selection

DEFAULT_PROVENANCE = (
    "Prepared automatically from public sources; every statement links to its source."
)


_LINK_CHARS = str.maketrans({"[": r"\[", "]": r"\]"})
# <https://…> and <mailto:…> are Markdown autolinks. Backslash-escaping the
# angle bracket does not stop them, so the brackets are removed and the URL is
# left as plain text. Targeted at the autolink form on purpose: a bare "<" in
# prose ("<$1M") is ordinary and must survive untouched.
_AUTOLINK = re.compile(r"<((?:https?|ftp|ftps|mailto|tel|data|javascript|file):[^>\s]*)>", re.I)


def neutralize_links(text: str) -> str:
    """Stop model-authored text from carrying links of its own.

    The page's contract with the reader is that every link goes to a cited
    source. The citation links are appended by us from the url map; a link
    inside the model's own sentence has not been through that check and could
    point anywhere, which is precisely the trust the brief is asking for.

    So ``[`` and ``]`` are escaped, which kills inline links and images, and
    the angle brackets around an autolink are removed. A bare URL stays visible
    as text, which is honest — the reader can see it and judge it — but it is
    not clickable and not presented as a source.
    """
    return _AUTOLINK.sub(r"\1", text).translate(_LINK_CHARS)


def provenance_for(profile: Profile) -> str:
    """The line that lets a reader forward the page without vouching for it.

    Config, not code. Rule 2: no string in `src/brief/` may name an
    institution, and the organization preparing a brief is exactly that. Set
    `delivery.provenance` in `config.yaml`, or per profile to override it.
    """
    delivery = profile.config.get("delivery") or {}
    return str(delivery.get("provenance") or DEFAULT_PROVENANCE)


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
    item_tags: Mapping[int, Sequence[str]] | None = None,
) -> RenderedBrief:
    """Render one week's result for one profile.

    `result` cites *positions* in the prompt, not database ids, so the two are
    translated here before rendering. An entry citing a position that was
    never sent is dropped by the seed and counted in the footer — the check
    that makes the page safe to forward.

    A bucket the model invents is dropped the same way. The profile decides
    what sections a brief has; a model that returns a section nobody asked for
    is returning something the reader did not agree to receive, and silently
    rendering it would let the shape of the page drift week to week.
    """
    positions = selection.citation_map()
    urls = {
        position: item_urls[item_id]
        for position, item_id in positions.items()
        if item_id in item_urls
    }

    declared = {b.name for b in profile.buckets}
    result = _neutralize_result(dict(result))
    invented = [
        b for b in (result.get("buckets") or []) if str(b.get("name")) not in declared
    ]
    if invented:
        result["buckets"] = [
            b for b in result["buckets"] if str(b.get("name")) in declared
        ]

    rendered = render_digest(
        result,
        urls,
        title=profile.title,
        subtitle=f"week of {week_start}",
        bucket_order=[b.name for b in profile.buckets],
        footer="",
        link_label=lambda position: _label(position, positions, item_tags),
    )

    footer = _footer(
        profile=profile,
        selection=selection,
        provider_name=provider_name,
        dropped=len(rendered.dropped),
        invented=len(invented),
    )
    markdown = f"{rendered.markdown.rstrip()}\n\n---\n\n{footer}\n"
    return RenderedBrief(
        markdown=markdown, kept=rendered.kept, dropped=list(rendered.dropped)
    )


#: How many tags a citation shows. Three names a topic; a longer list is a
#: table row, and the link text is the one place the page has no room.
TAGS_PER_CITATION = 3


def _label(
    position: int,
    positions: Mapping[int, int],
    item_tags: Mapping[int, Sequence[str]] | None,
) -> str:
    """The citation's link text: the prompt position, plus the item's first
    tags when it has any — ``[12 · genomics, crispr](url)``."""
    item_id = positions.get(position, position)
    tags = list((item_tags or {}).get(item_id, ()))[:TAGS_PER_CITATION]
    return f"{item_id} · {', '.join(tags)}" if tags else str(item_id)


def _neutralize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Apply `neutralize_links` to every piece of model-authored text."""
    out = dict(result)
    if isinstance(out.get("buckets"), list):
        out["buckets"] = [
            {**b, "entries": [_neutralize_entry(e) for e in (b.get("entries") or [])]}
            if isinstance(b, dict)
            else b
            for b in out["buckets"]
        ]
    for key, value in list(out.items()):
        if key != "buckets" and isinstance(value, list):
            out[key] = [
                _neutralize_entry(e) if isinstance(e, dict) and "text" in e else e
                for e in value
            ]
    return out


def _neutralize_entry(entry: Any) -> Any:
    if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
        return entry
    return {**entry, "text": neutralize_links(entry["text"])}


def _footer(
    *,
    profile: Profile,
    selection: Selection,
    provider_name: str,
    dropped: int,
    invented: int = 0,
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
    if invented:
        counts.append(f"{invented} sections dropped as not declared by this profile")

    return (
        f"*{provenance_for(profile)}*\n\n"
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
        f"---\n\n*{provenance_for(profile)}*\n"
    )
