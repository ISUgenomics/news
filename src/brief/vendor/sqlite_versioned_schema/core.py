"""Idempotent, versioned SQLite schema initialization.

Origin: a knowledge-graph app's ``kgx/db/schema.py``, which hardcoded its DDL
(``CREATE_SCHEMA``) and version (``SCHEMA_VERSION``) as module globals. Both
are caller-supplied parameters here; the mechanism is unchanged.

The mechanism: run ``IF NOT EXISTS`` DDL through ``executescript``, then
record ``version`` in a ``schema_version`` table on first creation only.
A database that already carries a version row keeps it — this is creation
plus version *reporting*, not migration. See README Gotchas.
"""

from __future__ import annotations

import sqlite3

# The one table the mechanism owns. In the origin this CREATE was the first
# statement of the app's monolithic CREATE_SCHEMA string; app DDL is now
# caller-supplied, so the mechanism carries its own copy. IF NOT EXISTS makes
# a duplicate CREATE in the caller's DDL harmless.
SCHEMA_VERSION_TABLE = """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    );
"""


def init_schema(conn: sqlite3.Connection, ddl: str, version: int) -> int:
    """Apply schema. Returns the current schema version.

    Safe to call on an existing DB — every statement should use IF NOT EXISTS
    (the origin's DDL did; ``executescript`` re-runs the whole script on every
    call).

    Behavior preserved from the origin, quirks included:

    - If a ``schema_version`` row already exists, its value is returned
      unchanged — even when ``version`` is newer and ``ddl`` just created new
      tables. No migration, no UPDATE, no mismatch error: the reported
      version can be stale relative to the schema actually present.
    - The stored version is read with ``LIMIT 1`` and no ``ORDER BY``. With
      more than one row the choice is left to SQLite (in practice the
      smallest value, because ``version`` is the INTEGER PRIMARY KEY and the
      table scan walks that b-tree — an implementation detail, not a
      contract).
    - ``commit()`` runs only on the first-creation path. ``executescript``
      itself implicitly commits any transaction pending on the connection
      before executing, a side effect callers inherit.
    """
    conn.executescript(SCHEMA_VERSION_TABLE + ddl)
    cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
        conn.commit()
        return version
    return row[0]
