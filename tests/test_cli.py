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
from harness.stub_http import stub_http  # noqa: F401  (pytest fixture)
from harness.stub_smtp import stub_smtp  # noqa: F401  (pytest fixture)

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


# --- the commands themselves, driven through the real typer app -------------
#
# Until these existed the two headline claims in cli.py's docstring — the
# commands are idempotent, and one profile failing does not stop the others —
# were asserted in prose and nowhere else.


import yaml
from typer.testing import CliRunner

from brief.deliver import UnsafeArchiveName
from brief.render import neutralize_links

runner = CliRunner()

GOOD = {
    "name": "good",
    "title": "Good Brief",
    "audience": "a reader",
    "persona": "an analyst",
    "sources": ["feed_a"],
    "relevance": {"any_of": ["award"], "max_items": 10},
    "buckets": [{"name": "Funding", "ask": "awards"}],
    "delivery": {"from": "a@example.test", "to": ["b@example.test"]},
}


def project(tmp_path, feed_url, *, extra_profiles=None, smtp=None):
    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"db": "data/items.db", "smtp": smtp or {"host": "localhost", "port": 2525}}),
        encoding="utf-8",
    )
    (tmp_path / "sources.yaml").write_text(
        yaml.safe_dump({"feed_a": {"kind": "rss", "url": feed_url}}), encoding="utf-8"
    )
    (tmp_path / "profiles" / "good.yaml").write_text(yaml.safe_dump(GOOD), encoding="utf-8")
    for name, text in (extra_profiles or {}).items():
        (tmp_path / "profiles" / name).write_text(text, encoding="utf-8")
    return tmp_path


FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>An award was announced</title><link>https://example.test/a</link>
<guid>a</guid><pubDate>Tue, 15 Sep 2026 12:00:00 GMT</pubDate>
<description>%s</description></item>
</channel></rss>""" % (b"long enough body " * 30)


def events(result):
    out = []
    for line in result.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def test_ingest_is_idempotent_through_the_real_command(tmp_path, stub_http):
    stub_http.route("/feed", FEED)
    root = project(tmp_path, stub_http.url + "/feed")

    first = runner.invoke(cli.app, ["ingest", "--root", str(root)])
    assert first.exit_code == 0, first.output
    assert [e for e in events(first) if e["event"] == "ingest.done"][0]["new"] == 1

    second = runner.invoke(cli.app, ["ingest", "--root", str(root)])
    assert [e for e in events(second) if e["event"] == "ingest.done"][0]["new"] == 0, (
        "a rerun must never double-count"
    )


def test_one_malformed_profile_does_not_stop_the_others(tmp_path, stub_http):
    """spec.md's failure table: skipped with a logged error, the others run."""
    stub_http.route("/feed", FEED)
    broken = yaml.safe_dump({**GOOD, "name": "broken", "relevance": {"any_of": []}})
    root = project(tmp_path, stub_http.url + "/feed", extra_profiles={"aaa-broken.yaml": broken})

    result = runner.invoke(cli.app, ["ingest", "--root", str(root)])
    seen = events(result)

    assert result.exit_code == 0, result.output
    assert any(e["event"] == "profile.error" for e in seen), "the bad file must be reported"
    assert any(e["event"] == "source.ok" for e in seen), "the good profile must still run"
    assert (root / "data" / "items.db").exists(), "and the run must reach the database"


def test_doctor_reports_the_good_profile_despite_a_broken_sibling(tmp_path, stub_http):
    broken = yaml.safe_dump({**GOOD, "name": "broken", "relevance": {"any_of": []}})
    root = project(tmp_path, stub_http.url + "/feed", extra_profiles={"aaa-broken.yaml": broken})

    result = runner.invoke(cli.app, ["doctor", "--root", str(root)])
    assert "good" in result.output
    assert "profile.error" in result.output


def test_a_broken_sources_file_still_stops_the_run(tmp_path, stub_http):
    """Shared state is different: half a source registry is worse than no run."""
    root = project(tmp_path, stub_http.url + "/feed")
    (root / "sources.yaml").write_text("feed_a:\n  url: no-kind-here\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["doctor", "--root", str(root)])
    assert result.exit_code != 0
    assert "kind" in result.output


def test_deliver_sends_once_then_refuses_the_same_week(tmp_path, stub_http, stub_smtp):
    """The idempotency claim, driven through the real command and a real socket."""
    stub_http.route("/feed", FEED)
    root = project(
        tmp_path,
        stub_http.url + "/feed",
        smtp={"host": stub_smtp.host, "port": stub_smtp.port},
    )
    runner.invoke(cli.app, ["ingest", "--root", str(root)])

    conn = db.connect(root / "data" / "items.db")
    week = cli.monday_of(datetime.now(timezone.utc))
    db.record_brief(
        conn, profile="good", week_start=week, generated_at=NOW, provider="stub",
        model="m", prompt_hash="h", input_item_ids=[1], raw_response="{}",
        result_json="{}", markdown="# a brief",
    )
    conn.close()

    first = runner.invoke(cli.app, ["deliver", "--root", str(root)])
    assert any(e["event"] == "deliver.ok" for e in events(first)), first.output
    assert len(stub_smtp.envelopes) == 1
    assert stub_smtp.last().rcpt_tos == ["b@example.test"]

    second = runner.invoke(cli.app, ["deliver", "--root", str(root)])
    assert any(e["event"] == "deliver.skipped" for e in events(second)), second.output
    assert len(stub_smtp.envelopes) == 1, "a second run must not reach the server at all"

    third = runner.invoke(cli.app, ["deliver", "--root", str(root), "--resend"])
    assert len(stub_smtp.envelopes) == 2, "--resend is the deliberate way through"


def test_a_dry_run_archives_without_sending_or_marking_sent(tmp_path, stub_http, stub_smtp):
    stub_http.route("/feed", FEED)
    root = project(
        tmp_path,
        stub_http.url + "/feed",
        smtp={"host": stub_smtp.host, "port": stub_smtp.port},
    )
    runner.invoke(cli.app, ["ingest", "--root", str(root)])
    conn = db.connect(root / "data" / "items.db")
    week = cli.monday_of(datetime.now(timezone.utc))
    db.record_brief(
        conn, profile="good", week_start=week, generated_at=NOW, provider="stub",
        model="m", prompt_hash="h", input_item_ids=[1], raw_response="{}",
        result_json="{}", markdown="# a brief",
    )

    runner.invoke(cli.app, ["deliver", "--root", str(root), "--dry-run"])
    assert stub_smtp.envelopes == [], "a dry run opens no socket"
    assert (root / "briefs" / "good" / f"{week}.md").exists(), "but still archives"
    assert db.get_brief(conn, "good", week)["sent_at"] is None, "and does not mark it sent"
    conn.close()


def test_an_unknown_profile_name_is_rejected_rather_than_silently_doing_nothing(
    tmp_path, stub_http
):
    root = project(tmp_path, stub_http.url + "/feed")
    result = runner.invoke(cli.app, ["synthesize", "--profile", "nope", "--root", str(root)])
    assert result.exit_code != 0
    assert "nope" in result.output


# --- smaller gaps the review named -----------------------------------------


def test_model_authored_text_cannot_carry_a_link(tmp_path):
    """Every link on the page must be one we resolved from a citation."""
    assert neutralize_links("see [here](https://evil.test)") == r"see \[here\](https://evil.test)"
    assert neutralize_links("<https://evil.test>") == "https://evil.test"
    assert neutralize_links("<mailto:a@evil.test>") == "mailto:a@evil.test"
    assert neutralize_links("the award was <$1M and 3 < 5") == "the award was <$1M and 3 < 5"


def test_an_archive_path_cannot_escape_the_briefs_directory(tmp_path):
    from brief.deliver import archive_path

    assert archive_path(tmp_path, "isu-ai", "2026-09-14").parent.name == "isu-ai"
    for profile_name, week in [("../../etc", "2026-09-14"), ("ok", "../../x"), ("", "w")]:
        with pytest.raises(UnsafeArchiveName):
            archive_path(tmp_path, profile_name, week)


def test_merged_groups_citing_positions_never_sent_are_dropped():
    from brief.synthesize import _drop_unknown_positions

    result = {"buckets": [], "merged": [[1, 2], [1, 99], [3, 4]]}
    cleaned = _drop_unknown_positions(result, sent=4)
    assert cleaned["merged"] == [[1, 2], [3, 4]]
    assert _drop_unknown_positions({"buckets": []}, sent=4) == {"buckets": []}


def test_a_broken_item_is_reported_not_counted_as_already_seen(tmp_path):
    """INSERT OR IGNORE swallowed NOT NULL too, so a bad adapter looked healthy."""
    conn = db.connect(tmp_path / "x.db")
    assert db.upsert_items(conn, [Item(source="s", url="https://x/1", title="t")], now=NOW) == 1
    with pytest.raises(Exception):
        db.upsert_items(conn, [Item(source="s", url=None, title="t")], now=NOW)
    conn.close()
