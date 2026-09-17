"""Render a bucketed, citation-carrying digest to Markdown, keeping only entries that link to a real source.

An LLM was given a numbered list of items and asked for JSON: named buckets of entries, a watch
list, and on every entry an ``item_ids`` list naming the inputs it drew on. This module turns that
JSON into the page a reader sees, and it is where the citation rule is enforced by code rather than
by the prompt. An entry with no ``item_ids``, or with any id absent from the caller's id->url map,
is dropped and reported; it never reaches the page. Dropping the whole entry on one bad id is
deliberate: a claim that cites one real source and one invented one cannot be trusted either.

Contract:
- ``urls`` (id -> URL) is the whole set of known ids. Ids are compared as given; no coercion.
- Buckets come out in ``bucket_order`` first, then any others in model order; a bucket left empty
  after drops is omitted. Entry text is emitted verbatim except that newlines collapse to spaces.
- ``enforce_citations`` and ``render_markdown`` are usable alone; ``render_digest`` composes them
  and returns the page plus every drop with its reason, so the caller can count, log, or refuse
  to send when too much was cut.

Deliberately not done here: JSON extraction and schema validation (upstream), Markdown-to-HTML
and delivery (downstream), logging, and any knowledge of where ids or URLs come from.

No runtime dependencies: dataclasses and typing only. Markdown is emitted as text; a Markdown
library would add nothing but a way for formatting to drift.

Details of the contract above, decided when this module was written:

- Sections beyond the buckets are named by ``extra_sections``: a result-key -> heading mapping,
  ``{'watch_list': 'Watch list'}`` by default. An app whose schema calls the tail section
  ``open_questions`` passes its own mapping instead of forking this module. Extra sections are
  rendered after the buckets, in the order of that mapping, and a ``DroppedEntry`` from one of them
  carries the heading as its ``section``.
- ``render_digest`` derives the known-id set from ``urls.keys()``, so the page and the filter can
  never disagree about which ids are real.
- No coercion means ``'12'`` is not ``12``: a string id is not a known id, so the entry is dropped
  and reported as ``unknown_id``. That is a last line of defence, not the intended check — the
  caller's JSON schema must require integer ``item_ids`` so a string id fails validation upstream
  rather than quietly thinning the page here.
- A ``bool`` is never an id, for the same reason. Python holds ``True == 1``, so a model that
  answered ``"item_ids": [true]`` would otherwise be read as citing item 1 and would publish the
  label ``[True]`` beside a real URL. Bools are treated as unknown ids and dropped, whatever the
  caller's id set contains.
- A malformed structure raises rather than renders: ``TypeError`` for a wrong type (entries that
  are not mappings, ``item_ids`` that is not a list) and ``ValueError`` for a missing required key
  (a bucket with no ``name``, an entry with no ``text``). A missing ``buckets`` key, a missing
  extra-section key, and a missing ``item_ids`` are not malformed: the first two are empty, the
  last is an uncited entry.
- Entry text: a run of newlines, with any spaces or tabs around it, becomes one space, and the
  result is stripped at both ends; all other internal spacing is left verbatim. So a paragraph
  break inside an entry does not become a double space, and one entry is always one bullet.
- An entry whose text is empty or all whitespace still renders as a bullet, without the stray
  double space that ``- `` plus empty text would leave.
- ``markdown`` is ``''`` when no entry survived — no title, no footer, nothing to publish.
"""

from __future__ import annotations

import collections.abc as _abc
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Collection, Literal, Mapping, Sequence

__all__ = [
    "DEFAULT_EXTRA_SECTIONS",
    "DroppedEntry",
    "RenderResult",
    "enforce_citations",
    "render_markdown",
    "render_digest",
]

DEFAULT_EXTRA_SECTIONS: Mapping[str, str] = MappingProxyType(
    {"watch_list": "Watch list"}
)


@dataclass(frozen=True)
class DroppedEntry:
    """One entry that was cut, with the reason a reader never saw it."""

    section: str
    text: str
    item_ids: list
    reason: Literal["no_ids", "unknown_id"]
    unknown: tuple = ()


@dataclass(frozen=True)
class RenderResult:
    """The page, how many entries it carries, and everything that was cut."""

    markdown: str
    kept: int
    dropped: list


_NEWLINE_RUN = re.compile(r"[ \t]*(?:\r\n|\r|\n)[ \t\r\n]*")


def _require_mapping(value: Any, what: str) -> Mapping:
    if not isinstance(value, _abc.Mapping):
        raise TypeError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _as_list(value: Any, what: str) -> list:
    """A list of entries or buckets; a missing section is empty, a wrong type is a TypeError."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, _abc.Sequence):
        raise TypeError(f"{what} must be a list, got {type(value).__name__}")
    return list(value)


def _bucket_name(bucket: Any, position: int) -> str:
    _require_mapping(bucket, f"bucket {position}")
    if "name" not in bucket:
        raise ValueError(f"bucket {position} has no 'name'")
    name = bucket["name"]
    if not isinstance(name, str):
        raise TypeError(
            f"bucket {position} 'name' must be a str, got {type(name).__name__}"
        )
    return name


def _entry_parts(entry: Any, section: str, position: int) -> tuple[str, list]:
    where = f"entry {position} of {section!r}"
    _require_mapping(entry, where)
    if "text" not in entry:
        raise ValueError(f"{where} has no 'text'")
    text = entry["text"]
    if not isinstance(text, str):
        raise TypeError(f"{where} 'text' must be a str, got {type(text).__name__}")
    return text, _as_list(entry.get("item_ids"), f"{where} 'item_ids'")


def _is_known(value: Any, known: list) -> bool:
    """True if this id is one the caller declared. A bool never is: ``True == 1`` in Python,
    so accepting one would coerce a model's ``true`` into item 1 and link it."""
    if isinstance(value, bool):
        return False
    return value in known


def _first_seen(values: Sequence) -> list:
    seen: list = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _sections(
    result: Mapping[str, Any],
    extra_sections: Mapping[str, str],
    bucket_order: Sequence[str] | None = None,
) -> list[tuple[str, list]]:
    """(heading, entries) pairs: buckets in bucket_order then model order, then extra sections."""
    _require_mapping(result, "result")
    buckets = _as_list(result.get("buckets"), "'buckets'")
    named = [(_bucket_name(bucket, i), bucket) for i, bucket in enumerate(buckets)]
    order: list[int] = []
    for wanted in bucket_order or ():
        order.extend(
            i for i, (name, _) in enumerate(named) if name == wanted and i not in order
        )
    order.extend(i for i in range(len(named)) if i not in order)

    sections: list[tuple[str, list]] = []
    for i in order:
        name, bucket = named[i]
        sections.append(
            (name, _as_list(bucket.get("entries"), f"bucket {name!r} 'entries'"))
        )
    for key, heading in extra_sections.items():
        if key in result:
            sections.append((heading, _as_list(result[key], f"{key!r}")))
    return sections


def enforce_citations(
    result: Mapping[str, Any],
    known_ids: Collection[int],
    *,
    extra_sections: Mapping[str, str] = DEFAULT_EXTRA_SECTIONS,
) -> tuple[dict, list[DroppedEntry]]:
    """Copy of result with uncited or mis-cited entries removed and empty buckets dropped, plus the drops."""
    _require_mapping(result, "result")
    known = list(known_ids)
    clean = dict(result)
    dropped: list[DroppedEntry] = []

    buckets = _as_list(result.get("buckets"), "'buckets'")
    kept_buckets = []
    for position, bucket in enumerate(buckets):
        name = _bucket_name(bucket, position)
        entries = _as_list(bucket.get("entries"), f"bucket {name!r} 'entries'")
        kept, drops = _filter_entries(entries, name, known)
        dropped.extend(drops)
        if kept:
            kept_bucket = dict(bucket)
            kept_bucket["entries"] = kept
            kept_buckets.append(kept_bucket)
    if "buckets" in result:
        clean["buckets"] = kept_buckets

    for key, heading in extra_sections.items():
        if key not in result:
            continue
        kept, drops = _filter_entries(_as_list(result[key], f"{key!r}"), heading, known)
        dropped.extend(drops)
        clean[key] = kept

    return clean, dropped


def _filter_entries(
    entries: list, section: str, known: list
) -> tuple[list, list[DroppedEntry]]:
    kept: list = []
    dropped: list[DroppedEntry] = []
    for position, entry in enumerate(entries):
        text, item_ids = _entry_parts(entry, section, position)
        if not item_ids:
            dropped.append(
                DroppedEntry(
                    section=section, text=text, item_ids=item_ids, reason="no_ids"
                )
            )
            continue
        unknown = tuple(i for i in _first_seen(item_ids) if not _is_known(i, known))
        if unknown:
            dropped.append(
                DroppedEntry(
                    section=section,
                    text=text,
                    item_ids=item_ids,
                    reason="unknown_id",
                    unknown=unknown,
                )
            )
            continue
        kept.append(entry)
    return kept, dropped


def render_markdown(
    result: Mapping[str, Any],
    urls: Mapping[int, str],
    *,
    title: str,
    subtitle: str = "",
    bucket_order: Sequence[str] | None = None,
    extra_sections: Mapping[str, str] = DEFAULT_EXTRA_SECTIONS,
    footer: str = "",
    link_label: Callable[[int], str] = str,
) -> str:
    """Markdown for an already-clean result; raises KeyError on an id missing from urls."""
    body: list[str] = []
    for heading, entries in _sections(result, extra_sections, bucket_order):
        bullets = [
            _bullet(
                *_entry_parts(entry, heading, position),
                urls=urls,
                link_label=link_label,
            )
            for position, entry in enumerate(entries)
        ]
        if bullets:
            body.append("## " + heading)
            body.append("\n".join(bullets))
    if not body:
        return ""

    blocks = ["# " + title]
    if subtitle:
        blocks.append(subtitle)
    blocks.extend(body)
    if footer:
        blocks.append(footer)
    return "\n\n".join(blocks) + "\n"


def _bullet(
    text: str,
    item_ids: list,
    *,
    urls: Mapping[int, str],
    link_label: Callable[[int], str],
) -> str:
    """One entry as one bullet: text with newlines collapsed, then one link per distinct id."""
    line = _NEWLINE_RUN.sub(" ", text).strip()
    links = " ".join(f"[{link_label(i)}]({urls[i]})" for i in _first_seen(item_ids))
    return " ".join(part for part in ("-", line, links) if part)


def render_digest(
    result: Mapping[str, Any],
    urls: Mapping[int, str],
    *,
    title: str,
    subtitle: str = "",
    bucket_order: Sequence[str] | None = None,
    extra_sections: Mapping[str, str] = DEFAULT_EXTRA_SECTIONS,
    footer: str = "",
    link_label: Callable[[int], str] = str,
) -> RenderResult:
    """enforce_citations then render_markdown; the one call the app makes.

    The known-id set is ``urls.keys()``, so the filter and the page cannot disagree.
    """
    _require_mapping(urls, "urls")
    clean, dropped = enforce_citations(
        result, urls.keys(), extra_sections=extra_sections
    )
    markdown = render_markdown(
        clean,
        urls,
        title=title,
        subtitle=subtitle,
        bucket_order=bucket_order,
        extra_sections=extra_sections,
        footer=footer,
        link_label=link_label,
    )
    kept = sum(len(entries) for _, entries in _sections(clean, extra_sections))
    return RenderResult(markdown=markdown, kept=kept, dropped=dropped)
