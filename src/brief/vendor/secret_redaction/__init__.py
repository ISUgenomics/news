"""Scrub credential-shaped strings out of text before it leaves the process."""

from .redactor import (
    DEFAULT_MARKER,
    Redactor,
    SecretMatch,
    redact,
    scan,
)
from .rules import (
    DEFAULT_RULES,
    Rule,
    rule,
)

__all__ = [
    "DEFAULT_MARKER",
    "DEFAULT_RULES",
    "Redactor",
    "Rule",
    "SecretMatch",
    "redact",
    "rule",
    "scan",
]
