"""Deep-merge a chain of overlay files onto a base config mapping."""

from .core import (
    OverlayError,
    apply_overlays,
    deep_merge,
    load_overlay_file,
    load_overlays,
    merge_all,
    overlay_paths,
)

__all__ = [
    "OverlayError",
    "apply_overlays",
    "deep_merge",
    "load_overlay_file",
    "load_overlays",
    "merge_all",
    "overlay_paths",
]
