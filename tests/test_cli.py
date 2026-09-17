"""Tests for the run-ordering rules in the CLI.

These are the rules that only show up when something goes wrong, which is
exactly why they need tests: a pipeline that behaves correctly on the happy
path and destroys provenance on the bad one looks fine until the week it
matters.

Every case here was found by the adversarial review, and each was a real
defect before the fix.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from brief import cli, db
from brief.models import Bucket, Delivery, Item, Profile, Relevance, SourceRef

NOW = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
WEEK = "2026-09-14"


def make_profile(**over) -> Profile:
    base = dict(
        name="test",
        title="Test Brief",
        audience="a reader",
        persona="an analyst",
        sources=(SourceRef("feed_a"),),
        relevance=Relevance(any_of=("award",)),
        buckets=(Bucket("Funding", "awards"),),
        delivery=Delivery(sender="a@example.test", to=("b@example.test",)),
        llm={"provider": "stub"},
        config={"smtp": {}},
    )
    base.update(over)
    return Profile(**base)


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "items.db")
    db.upsert_items(
        c,
        [
            Item(
                source="feed_a",
                url="https://example.test/1",
                title="An award",
                body="x",
            )
        ],
        now=NOW,
        source_key="feed_a",
    )
    yield c
    c.close()


def store(conn, *, stub: bool, sent: bool, markdown="# real brief"):
    db.record_brief(
        conn,
        profile="test",
        week_start=WEEK,
        generated_at=NOW,
        provider="claude-cli",
        model="m",
        prompt_hash="abc",
        input_item_ids=[1],
        raw_response="{}",
        result_json=json.dumps({"stub": True} if stub else {"buckets": []}),
        markdown=markdown,
    )
    if sent:
        db.mark_sent(conn, "test", WEEK, now=NOW)


# --- the week key ---------------------------------------------------------


def test_monday_of_is_the_monday_of_that_week():
    assert cli.monday_of(datetime(2026, 9, 17, tzinfo=timezone.utc)) == "2026-09-14"
    assert cli.monday_of(datetime(2026, 9, 14, tzinfo=timezone.utc)) == "2026-09-14"
    assert (
        cli.monday_of(datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc))
        == "2026-09-14"
    )
    assert (
        cli.monday_of(datetime(2026, 9, 21, 0, 1, tzinfo=timezone.utc)) == "2026-09-21"
    )


# --- the stub must not destroy a real brief -------------------------------


def test_a_stub_does_not_overwrite_a_real_brief_already_stored(conn):
    """A dead provider tonight must not turn last night's good brief into an apology."""
    store(conn, stub=False, sent=False, markdown="# the good brief")
    cli._record_stub(
        conn,
        make_profile(),
        week_start=WEEK,
        now=NOW,
        reason="provider died",
        candidates=3,
    )
    assert db.get_brief(conn, "test", WEEK)["markdown"] == "# the good brief"


def test_a_stub_does_not_overwrite_a_brief_that_was_already_delivered(conn):
    """Overwriting would destroy the provenance of a page someone has read."""
    store(conn, stub=False, sent=True, markdown="# the delivered brief")
    cli._record_stub(
        conn,
        make_profile(),
        week_start=WEEK,
        now=NOW,
        reason="provider died",
        candidates=3,
    )
    row = db.get_brief(conn, "test", WEEK)
    assert row["markdown"] == "# the delivered brief"
    assert row["sent_at"] is not None, "and it stays marked as sent"


def test_a_stub_may_replace_an_earlier_stub(conn):
    """A better explanation for the same empty week is an improvement."""
    store(conn, stub=True, sent=False, markdown="# old stub")
    cli._record_stub(
        conn,
        make_profile(),
        week_start=WEEK,
        now=NOW,
        reason="a clearer reason",
        candidates=3,
    )
    assert "a clearer reason" in db.get_brief(conn, "test", WEEK)["markdown"]


def test_a_stub_is_written_when_there_is_nothing_stored_yet(conn):
    cli._record_stub(
        conn,
        make_profile(),
        week_start=WEEK,
        now=NOW,
        reason="no provider",
        candidates=7,
    )
    row = db.get_brief(conn, "test", WEEK)
    assert json.loads(row["result_json"])["stub"] is True
    assert "7 candidate items" in row["markdown"]
    assert "no provider" in row["markdown"]


# --- a provider that raises is a stub, not silence ------------------------


class Exploding:
    name = "exploding"

    def available(self):
        return True

    def status(self):
        return "fine, apparently"

    def context_window(self):
        raise RuntimeError("rate limited")

    def complete(self, messages):
        raise AssertionError("must not be reached")


def test_a_provider_that_raises_mid_probe_still_produces_a_page(conn, monkeypatch):
    """Silence is indistinguishable from a quiet week; a page says which."""
    monkeypatch.setattr(cli.llm, "require_provider", lambda cfg: Exploding())
    cli._synthesize_one(conn, make_profile(), week_start=WEEK, now=NOW, root=None)
    row = db.get_brief(conn, "test", WEEK)
    assert row is not None, "a reader must not simply hear nothing"
    assert json.loads(row["result_json"])["stub"] is True
    assert "rate limited" in row["markdown"]


def test_a_provider_that_is_unavailable_produces_a_page_naming_the_fix(
    conn, monkeypatch
):
    def refuse(cfg):
        raise cli.llm.ProviderNotAvailable("claude-cli", "claude is not on PATH")

    monkeypatch.setattr(cli.llm, "require_provider", refuse)
    cli._synthesize_one(conn, make_profile(), week_start=WEEK, now=NOW, root=None)
    assert "not on PATH" in db.get_brief(conn, "test", WEEK)["markdown"]


# --- a brief with nothing verifiable is a stub, not an empty page ---------


class Fabricating:
    """Returns well-formed JSON whose every citation is invented."""

    name = "fabricating"

    def available(self):
        return True

    def status(self):
        return "ready"

    def context_window(self):
        return 100_000

    def complete(self, messages):
        return json.dumps(
            {
                "buckets": [
                    {
                        "name": "Funding",
                        "entries": [{"text": "A made-up award.", "item_ids": [999]}],
                    }
                ]
            }
        )


def test_a_brief_whose_every_entry_fails_the_citation_check_becomes_a_stub(
    conn, monkeypatch
):
    """An empty page reads as 'nothing happened'. What happened is worse."""
    monkeypatch.setattr(cli.llm, "require_provider", lambda cfg: Fabricating())
    cli._synthesize_one(conn, make_profile(), week_start=WEEK, now=NOW, root=None)

    row = db.get_brief(conn, "test", WEEK)
    assert json.loads(row["result_json"])["stub"] is True
    assert "A made-up award" not in row["markdown"], (
        "the fabrication never reaches a reader"
    )
    assert (
        "could not be verified" in row["markdown"] or "cited an item" in row["markdown"]
    )


# --- read_env ------------------------------------------------------------


def test_read_env_handles_export_quotes_comments_and_a_byte_order_mark(tmp_path):
    (tmp_path / "env").write_text(
        '﻿export CD_TOKEN="abc123"\n'
        "# a comment\n"
        "\n"
        "SMTP_USER=plain\n"
        "QUOTED='single'\n"
        "WITH_EQUALS=a=b=c\n"
        "  SPACED  =  padded  \n",
        encoding="utf-8",
    )
    env = cli.read_env(tmp_path)
    assert env["CD_TOKEN"] == "abc123", (
        "a byte-order mark must not eat the first variable"
    )
    assert env["SMTP_USER"] == "plain"
    assert env["QUOTED"] == "single"
    assert env["WITH_EQUALS"] == "a=b=c", "only the first = separates key from value"
    assert env["SPACED"] == "padded"


def test_read_env_falls_back_to_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FROM_PROCESS", "yes")
    assert cli.read_env(tmp_path)["FROM_PROCESS"] == "yes"


def test_a_dotenv_value_overrides_the_process(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_TOKEN", "stale")
    (tmp_path / "env").write_text("CD_TOKEN=fresh\n", encoding="utf-8")
    assert cli.read_env(tmp_path)["CD_TOKEN"] == "fresh"
