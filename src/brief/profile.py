"""Load and validate profiles, and the sources they reference.

App code. It is the module that knows every field name in `profiles/*.yaml`
and `sources.yaml`, which is precisely the knowledge a seed must not hold.

Three steps, in this order, and the order matters:

1. **Read** the YAML files. Nothing else happens during parsing.
2. **Overlay** `config.yaml` with the profile's own `llm`, `delivery`, and
   `select` blocks, via the vendored deep merge. A profile states only what
   differs; a hand-rolled merge would drop nested keys, which is the defect
   that feature exists to prevent.
3. **Interpolate** `${VAR}` references against an env mapping supplied by the
   caller. Last, so a profile can override a value that itself contains a
   reference, and so a missing variable fails at load with the key path rather
   than as a puzzling 401 hours later.

Validation is deliberately loud and specific: a malformed profile names the
file, the field, and what was expected. A brief that silently loses its
recipients is worse than one that refuses to start.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from brief.lib.config_env_interpolate import interpolate_config
from brief.models import DEFAULT_SUBJECT, Bucket, Delivery, Profile, Relevance, SourceRef
from brief.vendor.layered_config_overlay import deep_merge

# Blocks a profile may override on top of config.yaml.
OVERLAYABLE = ("llm", "delivery", "select", "smtp", "ingest")


class ProfileError(ValueError):
    """A profile or sources file is malformed. The message names where."""

    def __init__(self, path: str | Path, field: str, problem: str) -> None:
        self.path = str(path)
        self.field = field
        self.problem = problem
        super().__init__(f"{path}: {field}: {problem}")


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Parse one YAML file into a mapping, or raise naming the file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(p, "<file>", f"cannot be read: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(p, "<yaml>", f"is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ProfileError(p, "<root>", f"must be a mapping, got {type(data).__name__}")
    return dict(data)


def load_sources(path: str | Path, env: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Load `sources.yaml`: name -> {kind, ...params}, with `${VAR}` resolved."""
    raw = load_yaml(path)
    resolved = interpolate_config(raw, env)
    sources: dict[str, dict[str, Any]] = {}
    for name, body in resolved.items():
        if not isinstance(body, Mapping):
            raise ProfileError(path, name, "must be a mapping with a `kind`")
        if "kind" not in body:
            raise ProfileError(path, name, "is missing `kind` (the adapter to use)")
        sources[name] = dict(body)
    return sources


def load_profile(
    path: str | Path,
    *,
    config: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    env: Mapping[str, str],
) -> Profile:
    """Load one profile, overlay it on the global config, and validate it."""
    p = Path(path)
    raw = load_yaml(p)

    merged = dict(config)
    for block in OVERLAYABLE:
        if block in raw:
            merged[block] = deep_merge(merged.get(block, {}), raw[block])
    raw = interpolate_config(raw, env)
    merged = interpolate_config(merged, env)

    name = _require_str(raw, "name", p)
    title = _require_str(raw, "title", p)

    return Profile(
        name=name,
        title=title,
        audience=_require_str(raw, "audience", p),
        persona=_require_str(raw, "persona", p),
        sources=_parse_sources(raw, p, sources),
        relevance=_parse_relevance(raw, p),
        buckets=_parse_buckets(raw, p),
        delivery=_parse_delivery(merged, p),
        llm=dict(merged.get("llm") or {}),
        extra_rules=tuple(raw.get("extra_rules") or ()),
        cadence=str(raw.get("cadence", "weekly")),
        config=merged,
    )


def load_all_profiles(
    directory: str | Path,
    *,
    config: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    env: Mapping[str, str],
    on_error: Callable[[Path, Exception], None] | None = None,
) -> list[Profile]:
    """Every `*.yaml` and `*.yml` under `directory`, sorted by filename.

    A profile that fails validation raises, unless the caller supplies
    ``on_error``, in which case that file is reported and skipped and the rest
    load. The decision belongs to the caller: one malformed file must not cost
    every other profile its brief, but a tool that silently drops profiles
    would be worse, so skipping is opt-in and always reported.

    The duplicate-name check runs over the profiles that survived.
    """
    d = Path(directory)
    loaded: list[tuple[Path, Profile]] = []
    for file in sorted([*d.glob("*.yaml"), *d.glob("*.yml")]):
        try:
            loaded.append(
                (file, load_profile(file, config=config, sources=sources, env=env))
            )
        except Exception as exc:
            if on_error is None:
                raise
            on_error(file, exc)

    seen: dict[str, Path] = {}
    for file, profile in loaded:
        if profile.name in seen:
            raise ProfileError(
                file,
                "name",
                f"duplicates {profile.name!r} already declared in {seen[profile.name].name}; "
                f"two profiles with one name would overwrite each other's stored brief",
            )
        seen[profile.name] = file
    return [profile for _file, profile in loaded]


def fetch_plan(profiles: list[Profile]) -> list[tuple[str, dict[str, Any], str]]:
    """The deduplicated set of fetches every profile between them requires.

    Two profiles naming the same source with the same parameters is one fetch;
    with different parameters, two. This is what keeps source cost flat as
    profiles multiply.

    Each entry is ``(name, params, source_key)``. The key is what the fetched
    rows are stored under, so the separation established here survives into
    storage instead of collapsing back to the source name.
    """
    seen: dict[tuple[str, str], tuple[str, dict[str, Any], str]] = {}
    for profile in profiles:
        for ref in profile.sources:
            seen.setdefault(ref.key(), (ref.name, dict(ref.params), ref.source_key()))
    return [seen[k] for k in sorted(seen)]


def _parse_sources(
    raw: Mapping[str, Any], path: Path, known: Mapping[str, Mapping[str, Any]]
) -> tuple[SourceRef, ...]:
    entries = raw.get("sources")
    if not entries:
        raise ProfileError(path, "sources", "must list at least one source")
    if not isinstance(entries, list):
        raise ProfileError(path, "sources", "must be a list")

    refs: list[SourceRef] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            name, params = entry, {}
        elif isinstance(entry, Mapping) and len(entry) == 1:
            name, params = next(iter(entry.items()))
            if params is None:
                params = {}
            if not isinstance(params, Mapping):
                raise ProfileError(
                    path, f"sources[{index}].{name}", "overrides must be a mapping"
                )
            params = dict(params)
        else:
            raise ProfileError(
                path,
                f"sources[{index}]",
                "must be a source name, or a single-key mapping of name to overrides",
            )
        if name not in known:
            raise ProfileError(
                path,
                f"sources[{index}]",
                f"unknown source {name!r}; sources.yaml declares {sorted(known)}",
            )
        refs.append(SourceRef(name=name, params=params))
    return tuple(refs)


def _parse_relevance(raw: Mapping[str, Any], path: Path) -> Relevance:
    block = raw.get("relevance") or {}
    if not isinstance(block, Mapping):
        raise ProfileError(path, "relevance", "must be a mapping")
    any_of = _terms(block.get("any_of"), path, "relevance.any_of")
    if not any_of:
        raise ProfileError(
            path,
            "relevance.any_of",
            "must list at least one term; an empty filter would send every item to the model",
        )
    max_items = block.get("max_items", 80)
    if not isinstance(max_items, int) or max_items < 1:
        raise ProfileError(path, "relevance.max_items", "must be a positive integer")
    return Relevance(
        any_of=any_of,
        none_of=_terms(block.get("none_of"), path, "relevance.none_of"),
        max_items=max_items,
    )


def _terms(value: Any, path: Path, field: str) -> tuple[str, ...]:
    """A keyword list, refusing the two shapes that silently break the filter.

    A bare string is the trap: ``any_of: genome`` is natural YAML and iterating
    it yields one keyword per character, which matches nearly everything and
    turns the filter off without any error. A blank entry does the same, since
    an empty pattern matches every text.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raise ProfileError(
            path,
            field,
            f"must be a list, not the bare string {value!r} — a string would be read "
            f"as one keyword per character and match almost everything",
        )
    if not isinstance(value, (list, tuple)):
        raise ProfileError(path, field, f"must be a list, got {type(value).__name__}")
    terms = []
    for index, term in enumerate(value):
        text = str(term).strip()
        if not text:
            raise ProfileError(
                path, f"{field}[{index}]", "is blank; an empty term matches every item"
            )
        terms.append(text)
    return tuple(terms)


def _parse_buckets(raw: Mapping[str, Any], path: Path) -> tuple[Bucket, ...]:
    entries = raw.get("buckets")
    if not entries or not isinstance(entries, list):
        raise ProfileError(path, "buckets", "must list at least one section")
    buckets: list[Bucket] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or "name" not in entry:
            raise ProfileError(
                path, f"buckets[{index}]", "must be a mapping with `name` and `ask`"
            )
        name = str(entry["name"])
        if name in seen:
            raise ProfileError(
                path, f"buckets[{index}]", f"duplicate section name {name!r}"
            )
        seen.add(name)
        buckets.append(Bucket(name=name, ask=str(entry.get("ask", ""))))
    return tuple(buckets)


def _parse_delivery(merged: Mapping[str, Any], path: Path) -> Delivery:
    block = merged.get("delivery") or {}
    if not isinstance(block, Mapping):
        raise ProfileError(path, "delivery", "must be a mapping")
    sender = block.get("from")
    if not sender:
        raise ProfileError(path, "delivery.from", "is required")
    to = block.get("to") or []
    if isinstance(to, str):
        to = [to]
    if not isinstance(to, list) or not to:
        raise ProfileError(path, "delivery.to", "must list at least one recipient")
    return Delivery(
        sender=str(sender),
        to=tuple(str(x) for x in to),
        subject=str(block.get("subject") or DEFAULT_SUBJECT),
    )


def _require_str(raw: Mapping[str, Any], field: str, path: Path) -> str:
    value = raw.get(field)
    if not value or not isinstance(value, str):
        raise ProfileError(path, field, "is required and must be a non-empty string")
    return value
