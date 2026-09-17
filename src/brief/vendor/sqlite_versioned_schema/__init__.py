"""Idempotent, versioned SQLite schema initialization."""

from .core import SCHEMA_VERSION_TABLE, init_schema

__all__ = ["SCHEMA_VERSION_TABLE", "init_schema"]
