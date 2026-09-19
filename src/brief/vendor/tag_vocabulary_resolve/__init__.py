"""Match free-text labels against a controlled vocabulary, so near-misses resolve to the term you already have."""

from .core import edit_distance, fuzzy_find, kebab_case, resolve_tags

__all__ = ["resolve_tags", "fuzzy_find", "edit_distance", "kebab_case"]
