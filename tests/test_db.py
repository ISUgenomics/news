"""Tests for the storage layer. Real SQLite files in tmp_path, never a mock."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from brief import db
from brief.models import Item

NOW = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "nested" / "items.db")
    yield c
    c.close()


def item(**kw):
    base = dict(
        source="feed_a", url="https://example.test/1", title="A title", body="body text"
    )
    base.update(kw)
    return Item(**base)


def test_connect_creates_parent_directory_and_schema(tmp_path):
    path = tmp_path / "deep" / "deeper" / "items.db"
    c = db.connect(path)
    assert path.exists()
    names = {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"items", "briefs", "schema_version"} <= names
    c.close()


def test_connect_is_idempotent_on_an_existing_database(tmp_path):
    path = tmp_path / "items.db"
    first = db.connect(path)
    db.upsert_items(first, [item()], now=NOW)
    first.close()
    second = db.connect(path)
    assert second.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    second.close()


def test_upsert_returns_rows_added_and_rerun_adds_none(conn):
    items = [item(url="https://example.test/1"), item(url="https://example.test/2")]
    assert db.upsert_items(conn, items, now=NOW) == 2
    assert db.upsert_items(conn, items, now=NOW) == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2


def test_whitespace_reflow_is_not_a_new_item(conn):
    db.upsert_items(conn, [item(body="hello   world\n\nagain")], now=NOW)
    added = db.upsert_items(conn, [item(body="hello world again")], now=NOW)
    assert added == 0, "a CMS re-wrapping a paragraph must not read as news"


def test_changed_body_is_a_new_item(conn):
    db.upsert_items(conn, [item(body="original")], now=NOW)
    assert db.upsert_items(conn, [item(body="genuinely different")], now=NOW) == 1


def test_same_content_from_two_sources_is_two_rows(conn):
    db.upsert_items(conn, [item(source="a"), item(source="b")], now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2


def test_fetched_at_is_the_supplied_time_not_the_clock(conn):
    db.upsert_items(conn, [item()], now=NOW)
    stored = conn.execute("SELECT fetched_at FROM items").fetchone()[0]
    assert stored == "2026-09-17T06:00:00Z"


def test_naive_datetime_is_treated_as_utc(conn):
    db.upsert_items(conn, [item()], now=datetime(2026, 9, 17, 6, 0))
    assert (
        conn.execute("SELECT fetched_at FROM items").fetchone()[0]
        == "2026-09-17T06:00:00Z"
    )


def test_raw_is_stored_as_sorted_json_and_none_stays_null(conn):
    db.upsert_items(
        conn,
        [
            item(url="https://example.test/r", raw={"b": 2, "a": 1}),
            item(url="https://example.test/n"),
        ],
        now=NOW,
    )
    rows = dict(conn.execute("SELECT url, raw_json FROM items").fetchall())
    assert json.loads(rows["https://example.test/r"]) == {"a": 1, "b": 2}
    assert rows["https://example.test/r"].index('"a"') < rows[
        "https://example.test/r"
    ].index('"b"')
    assert rows["https://example.test/n"] is None


def test_body_is_capped_in_bytes_without_splitting_a_character(conn):
    body = "é" * 9000
    db.upsert_items(conn, [item(body=body)], now=NOW, body_cap=100)
    stored = conn.execute("SELECT body FROM items").fetchone()[0]
    assert len(stored.encode("utf-8")) <= 100
    assert stored == "é" * 50


def test_cap_body_leaves_short_text_alone():
    assert db.cap_body("short") == "short"
    assert db.cap_body("") == ""


def test_items_fetched_since_filters_by_source_and_window(conn):
    old = NOW - timedelta(days=30)
    db.upsert_items(conn, [item(source="a", url="https://example.test/old")], now=old)
    db.upsert_items(conn, [item(source="a", url="https://example.test/new")], now=NOW)
    db.upsert_items(conn, [item(source="z", url="https://example.test/other")], now=NOW)

    rows = db.items_fetched_since(conn, ["a"], since=NOW - timedelta(days=7))
    assert [r["url"] for r in rows] == ["https://example.test/new"]


def test_items_fetched_since_with_no_sources_returns_empty(conn):
    db.upsert_items(conn, [item()], now=NOW)
    assert db.items_fetched_since(conn, [], since=NOW - timedelta(days=7)) == []


def test_items_fetched_since_orders_newest_first_by_published_then_fetched(conn):
    db.upsert_items(
        conn,
        [
            item(url="https://example.test/older", published_at="2026-09-10T00:00:00Z"),
            item(url="https://example.test/newer", published_at="2026-09-16T00:00:00Z"),
        ],
        now=NOW,
    )
    rows = db.items_fetched_since(conn, ["feed_a"], since=NOW - timedelta(days=7))
    assert [r["url"] for r in rows] == [
        "https://example.test/newer",
        "https://example.test/older",
    ]


def test_urls_for_ids_maps_only_requested_rows(conn):
    db.upsert_items(
        conn,
        [item(url="https://example.test/1"), item(url="https://example.test/2")],
        now=NOW,
    )
    assert db.urls_for_ids(conn, [1]) == {1: "https://example.test/1"}
    assert db.urls_for_ids(conn, []) == {}


def test_silent_sources_lists_never_seen_first_then_longest_silence():
    last_seen = {
        "quiet": "2026-08-01T00:00:00Z",
        "quieter": "2026-07-01T00:00:00Z",
        "fresh": "2026-09-16T00:00:00Z",
    }
    result = db.silent_sources(
        last_seen, ["fresh", "quiet", "quieter", "never"], now=NOW, threshold_days=14
    )
    assert [name for name, _ in result] == ["never", "quieter", "quiet"]
    assert result[0][1] is None
    assert result[1][1] > result[2][1]


def test_silent_sources_treats_a_future_timestamp_as_seen_now():
    last_seen = {"clock_skew": "2027-01-01T00:00:00Z"}
    assert (
        db.silent_sources(last_seen, ["clock_skew"], now=NOW, threshold_days=14) == []
    )


def test_silent_sources_treats_an_unparseable_timestamp_as_never_seen():
    assert db.silent_sources(
        {"broken": "not a date"}, ["broken"], now=NOW, threshold_days=14
    ) == [("broken", None)]


def test_record_brief_then_get_and_mark_sent(conn):
    db.record_brief(
        conn,
        profile="p",
        week_start="2026-09-14",
        generated_at=NOW,
        provider="claude-cli",
        model="m",
        prompt_hash="abc",
        input_item_ids=[3, 1, 2],
        raw_response="{}",
        result_json='{"buckets": []}',
        markdown="# brief",
    )
    row = db.get_brief(conn, "p", "2026-09-14")
    assert row["sent_at"] is None
    assert json.loads(row["input_item_ids"]) == [3, 1, 2], (
        "send order is provenance, not a set"
    )

    db.mark_sent(conn, "p", "2026-09-14", now=NOW)
    assert db.get_brief(conn, "p", "2026-09-14")["sent_at"] == "2026-09-17T06:00:00Z"


def test_regenerating_a_week_replaces_it_and_clears_sent(conn):
    for provider in ("claude-cli", "local"):
        db.record_brief(
            conn,
            profile="p",
            week_start="2026-09-14",
            generated_at=NOW,
            provider=provider,
            model="m",
            prompt_hash="abc",
            input_item_ids=[1],
            raw_response="{}",
            result_json="{}",
            markdown="# " + provider,
        )
    assert conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 1
    row = db.get_brief(conn, "p", "2026-09-14")
    assert row["provider"] == "local"
    assert row["sent_at"] is None


def test_get_brief_returns_none_when_absent(conn):
    assert db.get_brief(conn, "nobody", "2026-09-14") is None


def test_two_profiles_share_a_week_without_collision(conn):
    for profile in ("one", "two"):
        db.record_brief(
            conn,
            profile=profile,
            week_start="2026-09-14",
            generated_at=NOW,
            provider="claude-cli",
            model="m",
            prompt_hash="abc",
            input_item_ids=[1],
            raw_response="{}",
            result_json="{}",
            markdown="# x",
        )
    assert conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 2


def test_content_hash_depends_on_source_url_and_normalized_body():
    a = db.content_hash("s", "u", "a  b")
    assert a == db.content_hash("s", "u", "a b")
    assert a != db.content_hash("other", "u", "a b")
    assert a != db.content_hash("s", "other", "a b")
    assert a != db.content_hash("s", "u", "different")


# --- found by the adversarial review; each of these was a real defect --------


def test_two_profiles_querying_one_source_differently_do_not_see_each_other(conn):
    """A campus brief and a field brief both hit NSF. Neither may see the other.

    Before source_key existed, both fetches stored rows under `nsf`, so a maize
    award to another university landed in the Iowa State brief's candidate pool.
    """
    from brief.models import SourceRef

    campus = SourceRef("nsf", {"awardee": "A University"}).source_key()
    field = SourceRef("nsf", {"keyword": "maize genome"}).source_key()
    assert campus != field

    db.upsert_items(
        conn, [item(source="nsf", url="https://x/campus", title="Campus award")],
        now=NOW, source_key=campus,
    )
    db.upsert_items(
        conn, [item(source="nsf", url="https://x/field", title="Field award")],
        now=NOW, source_key=field,
    )

    campus_rows = db.items_fetched_since(conn, [campus], since=NOW - timedelta(days=7))
    assert [r["title"] for r in campus_rows] == ["Campus award"]
    assert all(r["source"] == "nsf" for r in campus_rows), "the readable name survives"


def test_a_source_with_no_parameters_is_stored_under_its_plain_name(conn):
    from brief.models import SourceRef

    assert SourceRef("inside_isu").source_key() == "inside_isu"
    db.upsert_items(conn, [item(source="inside_isu")], now=NOW, source_key="inside_isu")
    assert db.items_fetched_since(conn, ["inside_isu"], since=NOW - timedelta(days=7))


def test_the_source_key_is_stable_across_parameter_ordering():
    from brief.models import SourceRef

    one = SourceRef("nsf", {"awardee": "X", "keyword": "y"}).source_key()
    two = SourceRef("nsf", {"keyword": "y", "awardee": "X"}).source_key()
    assert one == two, "a reordered YAML mapping must not look like a new source"


def test_a_v1_database_gains_the_column_without_losing_its_rows(tmp_path):
    """The one explicit migration. A v1 file must not silently lack the column."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE items (
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT,
          url TEXT NOT NULL, title TEXT NOT NULL, body TEXT, published_at TEXT,
          fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL, raw_json TEXT,
          UNIQUE(source, content_hash));
        INSERT INTO items (source, url, title, body, fetched_at, content_hash)
        VALUES ('legacy', 'https://x/1', 'An older row', 'b', '2026-09-01T00:00:00Z', 'h1');
        """
    )
    old.commit()
    old.close()

    migrated = db.connect(path)
    row = migrated.execute("SELECT source, source_key, title FROM items").fetchone()
    assert row["title"] == "An older row", "the row survives"
    assert row["source_key"] == "legacy", "and becomes addressable by the new key"
    migrated.close()


def test_feed_validators_round_trip_so_a_conditional_fetch_can_fire(conn):
    """Without persistence the ETag can never be sent and every poll is a full download."""
    assert db.get_source_state(conn, "a_feed") == {"etag": None, "last_modified": None}

    db.set_source_state(
        conn, "a_feed", etag='W/"abc"', last_modified="Wed, 16 Sep 2026 00:00:00 GMT", now=NOW
    )
    state = db.get_source_state(conn, "a_feed")
    assert state["etag"] == 'W/"abc"'
    assert state["last_modified"] == "Wed, 16 Sep 2026 00:00:00 GMT"

    db.set_source_state(conn, "a_feed", etag=None, last_modified=None, now=NOW)
    assert db.get_source_state(conn, "a_feed")["etag"] is None, "a server that stopped sending one clears it"


def test_source_state_is_keyed_per_fetch_not_per_source_name(conn):
    db.set_source_state(conn, "nsf#aaaa", etag="one", last_modified=None, now=NOW)
    db.set_source_state(conn, "nsf#bbbb", etag="two", last_modified=None, now=NOW)
    assert db.get_source_state(conn, "nsf#aaaa")["etag"] == "one"
    assert db.get_source_state(conn, "nsf#bbbb")["etag"] == "two"
