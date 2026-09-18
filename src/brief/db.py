"""The whole state of the system: one SQLite file.

App code, not a seed — it knows the column names, the hash rule, and the body
cap, all of which are this application's decisions. Adapters never touch it;
they return ``Item``s and this module decides what a row is.

Two tables. ``items`` is append-only and deduplicated on
``(source_key, content_hash)``, so a page that changed gets a new row and an
unchanged one costs nothing. The key is the *fetch*, not the source name: one
NSF source queried for two different awardees is two keys, so two profiles
never see each other's rows. ``briefs`` records one synthesis per profile per
week, with enough provenance (``input_item_ids``, ``prompt_hash``,
``provider``, ``raw_response``) to regenerate the same week on a different
provider and diff the structured results.

Back it up by copying the file.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from brief.models import Item
from brief.vendor.sqlite_versioned_schema import init_schema

SCHEMA_VERSION = 4

DDL = """
CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY,
  source        TEXT NOT NULL,
  source_key    TEXT NOT NULL DEFAULT '',
  external_id   TEXT,
  url           TEXT NOT NULL,
  title         TEXT NOT NULL,
  body          TEXT,
  published_at  TEXT,
  fetched_at    TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  raw_json      TEXT,
  facts_json    TEXT,
  UNIQUE(source_key, content_hash)
);

CREATE INDEX IF NOT EXISTS items_fetched_at ON items(fetched_at);
CREATE INDEX IF NOT EXISTS items_source ON items(source);
CREATE INDEX IF NOT EXISTS items_source_key ON items(source_key);

CREATE TABLE IF NOT EXISTS source_state (
  source_key    TEXT PRIMARY KEY,
  etag          TEXT,
  last_modified TEXT,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS briefs (
  id             INTEGER PRIMARY KEY,
  profile        TEXT NOT NULL,
  week_start     TEXT NOT NULL,
  generated_at   TEXT NOT NULL,
  provider       TEXT NOT NULL,
  model          TEXT NOT NULL,
  prompt_hash    TEXT NOT NULL,
  input_item_ids TEXT NOT NULL,
  raw_response   TEXT NOT NULL,
  result_json    TEXT NOT NULL,
  markdown       TEXT NOT NULL,
  sent_at        TEXT,
  UNIQUE(profile, week_start)
);

CREATE TABLE IF NOT EXISTS item_tags (
  item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  tag      TEXT    NOT NULL,
  origin   TEXT    NOT NULL,
  PRIMARY KEY (item_id, tag)
);

CREATE INDEX IF NOT EXISTS item_tags_tag ON item_tags(tag);
"""

DEFAULT_BODY_CAP = 8192

_WHITESPACE = re.compile(r"\s+")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database, creating its parent directory and schema if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Order matters: the DDL creates an index on `source_key`, which fails on a
    # v1 table that does not have the column yet. Add it first, then apply.
    _add_source_key_column(conn)
    _add_facts_column(conn)
    init_schema(conn, DDL, SCHEMA_VERSION)
    return conn


def _add_facts_column(conn: sqlite3.Connection) -> None:
    """Add `facts_json` to a database created before schema version 3.

    Nullable and unindexed, so nothing else changes: rows stored before it
    existed simply have no facts and render without that line. The column is
    not part of `content_hash`, so re-fetching a page to populate it does not
    create a second row.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if not columns or "facts_json" in columns:
        return
    conn.execute("ALTER TABLE items ADD COLUMN facts_json TEXT")
    conn.commit()


def _add_source_key_column(conn: sqlite3.Connection) -> None:
    """Add `source_key` to a database created before schema version 2.

    The vendored schema helper creates and reports a version; it deliberately
    does not migrate. This is the one explicit exception, five lines rather
    than a framework, because a v1 database that silently lacks the column
    would merge two profiles' award searches back into one bucket — the exact
    contamination the column exists to prevent.

    Existing rows get `source_key = source`, which is correct for every source
    that carries no parameters and is the closest honest answer for the rest.

    A no-op on a fresh database, where there is no `items` table yet and the
    DDL will create the column itself.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if not columns or "source_key" in columns:
        return
    conn.execute("ALTER TABLE items ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE items SET source_key = source WHERE source_key = ''")
    conn.commit()


def normalize_body(body: str) -> str:
    """Collapse whitespace so cosmetic reflow does not read as a changed page.

    This is the hash input, not the stored text. A CMS that re-wraps a
    paragraph must not produce a new row, or every brief fills with items
    that only look new.
    """
    return _WHITESPACE.sub(" ", body or "").strip()


def content_hash(source: str, url: str, body: str) -> str:
    """sha256 of source, url, and the normalized body. The dedup identity."""
    blob = "\x00".join((source, url, normalize_body(body)))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cap_body(body: str, cap: int = DEFAULT_BODY_CAP) -> str:
    """Cap stored body length in bytes, cutting on a character boundary."""
    if not body:
        return ""
    encoded = body.encode("utf-8")
    if len(encoded) <= cap:
        return body
    return encoded[:cap].decode("utf-8", errors="ignore")


def upsert_items(
    conn: sqlite3.Connection,
    items: Iterable[Item],
    *,
    now: datetime,
    source_key: str | None = None,
    body_cap: int = DEFAULT_BODY_CAP,
) -> int:
    """Insert items that are new. Returns how many rows were actually added.

    ``now`` is a parameter so a test can pin ``fetched_at`` and so ingest
    stamps one consistent time across a run. ``INSERT OR IGNORE`` on
    ``(source_key, content_hash)`` does the dedup.

    ``source_key`` identifies the *fetch*, not just the source: one NSF source
    queried for two different awardees is two keys, so two profiles cannot see
    each other's awards. It defaults to the item's source name, which is right
    whenever the source carries no per-profile parameters.

    The conflict clause names the unique index rather than using
    ``INSERT OR IGNORE``, which would swallow every constraint violation alike:
    an item with a null url is a broken adapter, and reporting it as "already
    seen" is how a source quietly stops working while the log says it is fine.
    """
    fetched_at = _iso(now)
    added = 0
    for item in items:
        body = cap_body(item.body, body_cap)
        row = (
            item.source,
            source_key or item.source,
            item.external_id,
            item.url,
            item.title,
            body,
            item.published_at,
            fetched_at,
            content_hash(source_key or item.source, item.url, body),
            json.dumps(item.raw, sort_keys=True, default=str)
            if item.raw is not None
            else None,
            # Insertion order preserved: it is the display order.
            json.dumps(item.facts, default=str) if item.facts else None,
        )
        cur = conn.execute(
            "INSERT INTO items"
            " (source, source_key, external_id, url, title, body, published_at,"
            "  fetched_at, content_hash, raw_json, facts_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_key, content_hash) DO NOTHING",
            row,
        )
        added += cur.rowcount if cur.rowcount > 0 else 0
    conn.commit()
    return added


def items_fetched_since(
    conn: sqlite3.Connection,
    source_keys: Sequence[str],
    *,
    since: datetime,
    published_after: str | None = None,
) -> list[dict[str, Any]]:
    """Candidate rows for one profile: its sources, fetched since a cutoff.

    ``fetched_at``, not ``published_at``, is deliberate: the reader wants what
    is new to us. An old award first indexed this week is news to them.

    Selection is on ``source_key``, not ``source``: a profile sees rows from
    the fetches it actually asked for, never another profile's parameters
    against the same endpoint.

    ``published_after`` bounds how old an item may be and exists because of a
    real accident. A deep historical ingest fetches three years of awards in one
    afternoon, so every one of them is "new to us" and the next weekly brief
    fills with awards from 2024. The window keeps the original intent — an award
    we only just learned about is still news — while excluding history we went
    looking for. An item with no publication date is kept: we cannot show it is
    old, and dropping it would silently lose every undated feed entry.
    """
    if not source_keys:
        return []
    placeholders = ",".join("?" for _ in source_keys)
    age_clause = ""
    params: list[Any] = [*source_keys, _iso(since)]
    if published_after:
        age_clause = " AND (published_at IS NULL OR published_at >= ?)"
        params.append(published_after)
    cur = conn.execute(
        f"SELECT id, source, source_key, external_id, url, title, body,"
        f" published_at, fetched_at, facts_json"
        f" FROM items WHERE source_key IN ({placeholders}) AND fetched_at >= ?"
        f"{age_clause}"
        f" ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC",
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def get_source_state(conn: sqlite3.Connection, source_key: str) -> dict[str, str | None]:
    """The cache validators from the last successful fetch of this key.

    A feed that has not changed should cost one conditional request and no
    body. That only works if the ETag and Last-Modified the server sent last
    time are given back to it, which means they have to live somewhere between
    runs — here, keyed by the same fetch identity everything else uses.
    """
    cur = conn.execute(
        "SELECT etag, last_modified FROM source_state WHERE source_key = ?", (source_key,)
    )
    row = cur.fetchone()
    return {"etag": None, "last_modified": None} if row is None else dict(row)


def set_source_state(
    conn: sqlite3.Connection,
    source_key: str,
    *,
    etag: str | None,
    last_modified: str | None,
    now: datetime,
) -> None:
    """Record this fetch's validators. Absent values clear the stored ones."""
    conn.execute(
        "INSERT INTO source_state (source_key, etag, last_modified, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(source_key) DO UPDATE SET"
        "  etag=excluded.etag, last_modified=excluded.last_modified,"
        "  updated_at=excluded.updated_at",
        (source_key, etag, last_modified, _iso(now)),
    )
    conn.commit()


def items_published_between(
    conn: sqlite3.Connection,
    source_keys: Sequence[str],
    *,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Rows a source *published* in ``[start, end)``. For backfill only.

    The live weekly run selects on ``fetched_at`` — "new to us" — because an old
    award first indexed this week is news to its reader. A backfill needs the
    opposite question: what happened during that week, whenever we happened to
    learn it. Every row in this database was fetched on the same day the
    pipeline first ran, so a ``fetched_at`` backfill would pile every item into
    that one week and leave every earlier week empty.

    Rows with no ``published_at`` are excluded rather than guessed at. A backfill
    is a claim about a particular week, and an item whose date we do not know
    cannot support one.
    """
    if not source_keys:
        return []
    placeholders = ",".join("?" for _ in source_keys)
    cur = conn.execute(
        f"SELECT id, source, source_key, external_id, url, title, body,"
        f" published_at, fetched_at, facts_json"
        f" FROM items WHERE source_key IN ({placeholders})"
        f"   AND published_at IS NOT NULL"
        f"   AND published_at >= ? AND published_at < ?"
        f" ORDER BY published_at DESC, id DESC",
        (*source_keys, start, end),
    )
    return [dict(r) for r in cur.fetchall()]


def iter_raw_records(
    conn: sqlite3.Connection, sources: Sequence[str] | None = None
) -> Iterable[tuple[int, str, dict[str, Any]]]:
    """Every row that kept its raw record, as (id, source, parsed raw)."""
    where, params = "raw_json IS NOT NULL", []
    if sources:
        where += f" AND source IN ({','.join('?' for _ in sources)})"
        params = list(sources)
    for row in conn.execute(
        f"SELECT id, source, raw_json FROM items WHERE {where} ORDER BY id", params
    ):
        try:
            yield row["id"], row["source"], json.loads(row["raw_json"])
        except ValueError:
            continue


def update_derived(
    conn: sqlite3.Connection,
    updates: Iterable[tuple[int, dict[str, str] | None, str | None]],
) -> int:
    """Rewrite facts and published_at for rows whose derived values changed.

    Neither column feeds ``content_hash``, so this cannot create a duplicate row
    and cannot change what dedup considers the same item. It exists because a
    derived field is a pure function of ``raw_json``, which is already stored —
    adding one should not mean refetching a thousand records from an API.
    """
    changed = 0
    for item_id, facts, published_at in updates:
        cur = conn.execute(
            "UPDATE items SET facts_json = ?, published_at = COALESCE(?, published_at)"
            " WHERE id = ?"
            "   AND (IFNULL(facts_json,'') IS NOT ? OR IFNULL(published_at,'') IS NOT ?)",
            (
                json.dumps(facts, default=str) if facts else None,
                published_at,
                item_id,
                json.dumps(facts, default=str) if facts else "",
                published_at if published_at is not None else "",
            ),
        )
        changed += cur.rowcount if cur.rowcount > 0 else 0
    conn.commit()
    return changed


def replace_tags(
    conn: sqlite3.Connection, item_id: int, tags: Iterable[tuple[str, str]]
) -> bool:
    """Set an item's tags to exactly ``tags``. True if anything changed.

    Derived, like ``facts_json``: not in ``content_hash``, so re-tagging can
    never create a duplicate row, and ``brief reindex`` rebuilds every item's
    tags from ``raw_json`` offline. Does not commit; the caller commits once
    per pass.
    """
    wanted = sorted({(str(t), str(o)) for t, o in tags})
    have = sorted(
        (row[0], row[1])
        for row in conn.execute(
            "SELECT tag, origin FROM item_tags WHERE item_id = ?", (item_id,)
        )
    )
    if wanted == have:
        return False
    conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO item_tags (item_id, tag, origin) VALUES (?, ?, ?)",
        [(item_id, t, o) for t, o in wanted],
    )
    return True


def tags_for(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, list[str]]:
    """``{item_id: [tag, ...]}`` for the ids given; ids with no tags are absent."""
    out: dict[int, list[str]] = {}
    wanted = [int(i) for i in ids]
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT item_id, tag FROM item_tags WHERE item_id IN ({marks}) ORDER BY item_id, tag",
            chunk,
        ):
            out.setdefault(int(row[0]), []).append(str(row[1]))
    return out


def tag_counts(conn: sqlite3.Connection, *, by_source: bool = False) -> list[tuple]:
    """``(tag, n)`` rows, or ``(tag, source, n)`` with ``by_source``, most common first."""
    if by_source:
        return [
            (str(r[0]), str(r[1]), int(r[2]))
            for r in conn.execute(
                "SELECT t.tag, i.source, COUNT(*) FROM item_tags t JOIN items i ON i.id = t.item_id"
                " GROUP BY t.tag, i.source ORDER BY COUNT(*) DESC, t.tag, i.source"
            )
        ]
    return [
        (str(r[0]), int(r[1]))
        for r in conn.execute(
            "SELECT tag, COUNT(*) FROM item_tags GROUP BY tag ORDER BY COUNT(*) DESC, tag"
        )
    ]


def iter_taggable(
    conn: sqlite3.Connection, sources: Sequence[str] | None = None
) -> Iterable[tuple[int, str, str, str, dict | None]]:
    """Every item as ``(id, source, title, body, raw)``; ``raw`` is None when
    the source stored no record (feeds), in which case only phrases can tag it."""
    sql = "SELECT id, source, title, body, raw_json FROM items"
    params: tuple = ()
    if sources:
        sql += f" WHERE source IN ({','.join('?' * len(sources))})"
        params = tuple(sources)
    for row in conn.execute(sql + " ORDER BY id", params):
        raw = None
        if row[4]:
            try:
                parsed = json.loads(row[4])
                raw = parsed if isinstance(parsed, dict) else None
            except ValueError:
                raw = None
        yield int(row[0]), str(row[1]), str(row[2] or ""), str(row[3] or ""), raw


def brief_weeks(
    conn: sqlite3.Connection, profile: str, *, include_stubs: bool = False
) -> list[str]:
    """Weeks this profile has a real brief for, as ISO dates.

    A stub is excluded by default, because a stub is the record of a week that
    could NOT be briefed — no provider, no matching items — and treating it as
    done would permanently freeze that failure. This matters after a deep
    historical ingest: a week stubbed when the database held three items should
    be reconsidered once it holds thirty.

    A stub is cheap to reconsider. An empty week short-circuits before the model
    is called, so retrying one that is genuinely empty costs a query.
    """
    cur = conn.execute(
        "SELECT week_start, result_json FROM briefs WHERE profile = ? ORDER BY week_start",
        (profile,),
    )
    weeks = []
    for row in cur.fetchall():
        if not include_stubs:
            try:
                if json.loads(row["result_json"] or "{}").get("stub"):
                    continue
            except ValueError:
                pass
        weeks.append(row["week_start"])
    return weeks


def last_seen_by_source(conn: sqlite3.Connection) -> dict[str, str]:
    """Most recent ``fetched_at`` per source key, for the silence check.

    Keyed the same way ingest plans its fetches, so a source that works for one
    profile's parameters and fails for another's is reported as one working and
    one silent, rather than as uniformly healthy.
    """
    cur = conn.execute(
        "SELECT source_key, MAX(fetched_at) AS last FROM items GROUP BY source_key"
    )
    return {r["source_key"]: r["last"] for r in cur.fetchall()}


def silent_sources(
    last_seen: dict[str, str],
    configured: Iterable[str],
    *,
    now: datetime,
    threshold_days: float,
) -> list[tuple[str, float | None]]:
    """Sources that have produced nothing for longer than the threshold.

    Never-seen sources come first, with ``None`` days, because a source that
    has never worked is a different problem from one that went quiet.

    A timestamp in the future never reports as silent, by construction rather
    than by a guard: it cannot be older than the cutoff. A clock step is
    therefore ignored here instead of raising, which is the behavior we want —
    aborting would hide the silent sources this was asked about. An
    unparseable timestamp is reported as never-seen, because a source whose
    last mark cannot be read is a source we cannot vouch for.
    """
    cutoff = now - timedelta(days=threshold_days)
    never: list[tuple[str, float | None]] = []
    stale: list[tuple[str, float | None]] = []
    for source in configured:
        raw = last_seen.get(source)
        if raw is None:
            never.append((source, None))
            continue
        try:
            seen = _parse(raw)
        except ValueError:
            never.append((source, None))
            continue
        if seen < cutoff:
            stale.append((source, (now - seen).total_seconds() / 86400.0))
    never.sort(key=lambda p: p[0])
    stale.sort(key=lambda p: (-(p[1] or 0.0), p[0]))
    return never + stale


def record_brief(
    conn: sqlite3.Connection,
    *,
    profile: str,
    week_start: str,
    generated_at: datetime,
    provider: str,
    model: str,
    prompt_hash: str,
    input_item_ids: Sequence[int],
    raw_response: str,
    result_json: str,
    markdown: str,
) -> None:
    """Store or replace one week's brief for a profile, unsent."""
    conn.execute(
        "INSERT INTO briefs"
        " (profile, week_start, generated_at, provider, model, prompt_hash,"
        "  input_item_ids, raw_response, result_json, markdown, sent_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)"
        " ON CONFLICT(profile, week_start) DO UPDATE SET"
        "  generated_at=excluded.generated_at, provider=excluded.provider,"
        "  model=excluded.model, prompt_hash=excluded.prompt_hash,"
        "  input_item_ids=excluded.input_item_ids, raw_response=excluded.raw_response,"
        "  result_json=excluded.result_json, markdown=excluded.markdown",
        (
            profile,
            week_start,
            _iso(generated_at),
            provider,
            model,
            prompt_hash,
            json.dumps(list(input_item_ids)),
            raw_response,
            result_json,
            markdown,
        ),
    )
    conn.commit()


def get_brief(
    conn: sqlite3.Connection, profile: str, week_start: str
) -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT * FROM briefs WHERE profile = ? AND week_start = ?",
        (profile, week_start),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def mark_sent(
    conn: sqlite3.Connection, profile: str, week_start: str, *, now: datetime
) -> None:
    conn.execute(
        "UPDATE briefs SET sent_at = ? WHERE profile = ? AND week_start = ?",
        (_iso(now), profile, week_start),
    )
    conn.commit()


def urls_for_ids(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, str]:
    """id -> url, for turning the model's citations into links."""
    ids = list(ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(f"SELECT id, url FROM items WHERE id IN ({placeholders})", ids)
    return {r["id"]: r["url"] for r in cur.fetchall()}


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _parse(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
