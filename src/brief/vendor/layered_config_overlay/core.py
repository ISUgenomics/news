"""Deep-merge a chain of overlay files onto a base config mapping.

Origin: a knowledge-graph app's ``kgx/semantic_registry_overlay.py``, which
read two hardcoded config keys (``semantic_registry_overlay`` and its plural),
unwrapped one hardcoded envelope key (``registry_patch``), and hardcoded
``yaml.safe_load`` as the parser. All four are parameters here; the merge rule
and the load policy are unchanged.

The mechanism: read overlay paths out of a config mapping (one scalar key,
then one list key), load each file, and deep-merge them left to right onto a
base mapping. Later overlays win.

Load policy is deliberately forgiving and preserved verbatim from the origin:
a missing file, an unparseable file, and a file whose top level is not a
mapping all produce ``{}``. Pass ``strict=True`` to raise instead. See README
Gotchas.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "OverlayError",
    "apply_overlays",
    "deep_merge",
    "load_overlay_file",
    "load_overlays",
    "merge_all",
    "overlay_paths",
]


class OverlayError(Exception):
    """Raised by the loaders when ``strict=True`` and a file cannot be used."""


def _parse(text: str) -> Any:
    """Parse YAML if PyYAML is installed, else JSON.

    YAML is a superset of JSON, so the YAML path also accepts JSON overlays —
    which is how the origin loaded its generated ``.json`` patches through
    ``yaml.safe_load``. Without PyYAML the feature still works for JSON
    overlays, which is why it declares no hard dependency.
    """
    try:
        import yaml
    except ImportError:
        return json.loads(text)
    return yaml.safe_load(text)


def deep_merge(base: Any, overlay: Any) -> Any:
    """Recursively merge ``overlay`` onto ``base``. Overlay wins.

    Two mappings merge key by key; keys present only in ``base`` survive.
    Anything else — a scalar, a list, a type mismatch — replaces the base
    value outright. Note in particular that **lists replace rather than
    concatenate**: an overlay list drops every element the base list
    contributed. Preserved from the origin; see README Gotchas.
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    return overlay


def merge_all(mappings: Iterable[Any]) -> dict[str, Any]:
    """Left-to-right ``deep_merge`` over a sequence. Later mappings win."""
    merged: dict[str, Any] = {}
    for mapping in mappings:
        merged = deep_merge(merged, mapping)
    return merged


def load_overlay_file(
    path: str | Path,
    *,
    envelope_key: str | None = None,
    base_dir: str | Path | None = None,
    loader: Callable[[str], Any] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Load one overlay file into a mapping.

    ``envelope_key``: if the parsed document carries this key and its value is
    a mapping, that inner mapping is the overlay. The origin used this to
    accept both a bare patch and one wrapped in ``{"registry_patch": {...}}``.

    ``base_dir``: directory that relative paths resolve against. The origin had
    no such parameter and resolved relative paths against the *current working
    directory*, which is what ``None`` still does — a trap for any caller that
    does not pre-resolve. See README Gotchas.

    Returns ``{}`` for a missing file, an unparseable one, or one whose top
    level is not a mapping, unless ``strict`` is set.
    """
    path = Path(path)
    if not path.is_absolute():
        path = (Path(base_dir) / path).resolve() if base_dir else path.resolve()

    if not path.exists():
        if strict:
            raise OverlayError(f"overlay file does not exist: {path}")
        return {}

    parse = loader or _parse
    try:
        data = parse(path.read_text(encoding="utf-8"))
    except Exception as exc:
        if strict:
            raise OverlayError(f"overlay file could not be parsed: {path}") from exc
        return {}

    if not isinstance(data, dict):
        if strict:
            raise OverlayError(
                f"overlay top level is {type(data).__name__}, not a mapping: {path}"
            )
        return {}

    if envelope_key is not None:
        inner = data.get(envelope_key)
        if isinstance(inner, dict):
            return dict(inner)
    return data


def overlay_paths(
    config: dict[str, Any] | None,
    *,
    key: str,
    list_key: str | None = None,
) -> list[str]:
    """Pull overlay paths out of a config mapping: ``key`` first, then ``list_key``.

    Blank and whitespace-only entries are dropped. Preserved from the origin,
    ordering included: the scalar key is always applied before the list.
    """
    config = config or {}
    paths: list[str] = []

    raw = str(config.get(key, "") or "").strip()
    if raw:
        paths.append(raw)

    if list_key is not None:
        for item in list(config.get(list_key, []) or []):
            text = str(item or "").strip()
            if text:
                paths.append(text)
    return paths


def load_overlays(
    config: dict[str, Any] | None,
    *,
    key: str,
    list_key: str | None = None,
    envelope_key: str | None = None,
    base_dir: str | Path | None = None,
    loader: Callable[[str], Any] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Load and merge every overlay named in ``config``. Later overlays win."""
    return merge_all(
        load_overlay_file(
            path,
            envelope_key=envelope_key,
            base_dir=base_dir,
            loader=loader,
            strict=strict,
        )
        for path in overlay_paths(config, key=key, list_key=list_key)
    )


def apply_overlays(
    base: dict[str, Any],
    config: dict[str, Any] | None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Merge the overlays named in ``config`` onto ``base``.

    The one-call form of ``deep_merge(base, load_overlays(config, ...))``,
    which is how the origin's two public functions were always used together.
    """
    return deep_merge(base, load_overlays(config, **kwargs))
