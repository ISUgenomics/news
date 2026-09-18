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
import re
from datetime import datetime, timedelta, timezone

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


@pytest.fixture(autouse=True)
def no_keychain_by_default(monkeypatch):
    """Keep the developer's real keychain out of every test in this file.

    `read_env` consults the macOS keychain, so without this a secret stored on
    the machine running the suite silently overrides a test's own fixture — two
    dotenv tests failed exactly that way once a real CD_TOKEN existed. A test
    that means to exercise the keychain turns it back on itself.
    """
    monkeypatch.setattr(cli, "is_available", lambda **kw: False)


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


# --- secrets from the macOS keychain ---------------------------------------


def test_the_keychain_is_consulted_and_wins_over_a_stale_dotenv(tmp_path, monkeypatch):
    """The keychain is the one place the secret is not sitting in a file."""
    (tmp_path / "env").write_text("CD_TOKEN=stale-from-a-file\n", encoding="utf-8")
    monkeypatch.setenv("CD_TOKEN", "staler-from-the-shell")
    monkeypatch.setattr(cli, "is_available", lambda **kw: True)
    monkeypatch.setattr(cli, "read_secrets", lambda svc, names, **kw: {"CD_TOKEN": "from-keychain"})

    assert cli.read_env(tmp_path)["CD_TOKEN"] == "from-keychain"


def test_no_keychain_entry_leaves_the_other_sources_alone(tmp_path, monkeypatch):
    (tmp_path / "env").write_text("CD_TOKEN=from-a-file\n", encoding="utf-8")
    monkeypatch.setattr(cli, "is_available", lambda **kw: True)
    monkeypatch.setattr(cli, "read_secrets", lambda svc, names, **kw: {})

    assert cli.read_env(tmp_path)["CD_TOKEN"] == "from-a-file"


def test_a_machine_without_the_keychain_tool_still_works(tmp_path, monkeypatch):
    (tmp_path / "env").write_text("CD_TOKEN=from-a-file\n", encoding="utf-8")
    monkeypatch.setattr(cli, "is_available", lambda **kw: False)

    def explode(*a, **kw):
        raise AssertionError("must not be called when the tool is absent")

    monkeypatch.setattr(cli, "read_secrets", explode)
    assert cli.read_env(tmp_path)["CD_TOKEN"] == "from-a-file"


def test_an_unreadable_keychain_stops_the_run_rather_than_using_a_stale_value(
    tmp_path, monkeypatch
):
    """Falling back would run the job without its real credential and call it success."""
    (tmp_path / "env").write_text("CD_TOKEN=stale\n", encoding="utf-8")
    monkeypatch.setattr(cli, "is_available", lambda **kw: True)

    def locked(svc, names, **kw):
        raise cli.KeychainError("could not read topic-brief/CD_TOKEN (exit 36): locked")

    monkeypatch.setattr(cli, "read_secrets", locked)
    with pytest.raises(cli.KeychainError):
        cli.read_env(tmp_path)


def test_the_secret_names_match_the_variables_config_refers_to():
    """A keychain account and a ${VAR} reference must be the same word."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    def referenced_in(path):
        # Values only. The header comments use ${VAR} as documentation, and a
        # scan that counted those would demand a keychain entry named VAR.
        body = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        return set(re.findall(r"\$\{(\w+)", body))

    referenced = referenced_in(root / "sources.yaml") | referenced_in(root / "config.yaml")
    assert referenced, "the fixture would be vacuous if config referenced nothing"
    assert referenced <= set(cli.SECRET_NAMES), (
        f"config refers to {referenced - set(cli.SECRET_NAMES)}, which the keychain "
        f"lookup would never fetch"
    )


# --- backfill ---------------------------------------------------------------


def _backfill_project(tmp_path, stub_http):
    root = project(tmp_path, stub_http.url + "/feed")
    conn = db.connect(root / "data" / "items.db")
    # Published across three past weeks, all fetched today — the shape a real
    # first ingest produces, and the reason backfill cannot use fetched_at.
    db.upsert_items(
        conn,
        [
            Item(source="feed_a", url=f"https://example.test/{d}", title=f"An award on {d}",
                 body="body", published_at=f"{d}T00:00:00Z")
            for d in ("2026-08-05", "2026-08-12", "2026-08-19")
        ],
        now=NOW,
        source_key="feed_a",
    )
    conn.close()
    return root


def test_backfill_plans_the_weeks_that_have_no_brief(tmp_path, stub_http):
    root = _backfill_project(tmp_path, stub_http)
    result = runner.invoke(
        cli.app,
        ["backfill", "--since", "2026-08-03", "--until", "2026-08-24", "--root", str(root), "--dry-run"],
    )
    plan = [e for e in events(result) if e["event"] == "backfill.plan"][0]
    assert plan["weeks"] == ["2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"]
    assert plan["already_done"] == []


def test_backfill_skips_a_week_that_already_has_a_brief(tmp_path, stub_http):
    """Backfill fills gaps; overwriting a brief you have read destroys provenance."""
    root = _backfill_project(tmp_path, stub_http)
    conn = db.connect(root / "data" / "items.db")
    db.record_brief(
        conn, profile="good", week_start="2026-08-10", generated_at=NOW, provider="x",
        model="m", prompt_hash="h", input_item_ids=[], raw_response="{}",
        result_json="{}", markdown="# already here",
    )
    conn.close()

    result = runner.invoke(
        cli.app,
        ["backfill", "--since", "2026-08-03", "--until", "2026-08-17", "--root", str(root), "--dry-run"],
    )
    plan = [e for e in events(result) if e["event"] == "backfill.plan"][0]
    assert "2026-08-10" not in plan["weeks"]
    assert plan["already_done"] == ["2026-08-10"], "skipped AND reported"


def test_backfill_never_touches_the_current_week(tmp_path, stub_http):
    """It is in progress, not missing, and belongs to the live schedule."""
    root = _backfill_project(tmp_path, stub_http)
    this_week = cli.monday_of(datetime.now(timezone.utc))
    result = runner.invoke(
        cli.app, ["backfill", "--since", "2026-08-03", "--root", str(root), "--dry-run"]
    )
    plan = [e for e in events(result) if e["event"] == "backfill.plan"][0]
    assert this_week not in plan["weeks"]


def test_backfill_reports_a_week_with_nothing_published(tmp_path, stub_http):
    """Silence would be indistinguishable from a week that failed."""
    root = _backfill_project(tmp_path, stub_http)
    result = runner.invoke(
        cli.app,
        ["backfill", "--since", "2026-07-06", "--until", "2026-07-13", "--root", str(root)],
    )
    empty = [e for e in events(result) if e["event"] == "backfill.empty_week"]
    assert {e["week"] for e in empty} == {"2026-07-06", "2026-07-13"}


def test_backfill_refuses_an_inverted_range_naming_both_ends(tmp_path, stub_http):
    root = _backfill_project(tmp_path, stub_http)
    result = runner.invoke(
        cli.app,
        ["backfill", "--since", "2026-08-24", "--until", "2026-08-03", "--root", str(root)],
    )
    bad = [e for e in events(result) if e["event"] == "backfill.bad_range"]
    assert bad and "2026-08-24" in bad[0]["error"] and "2026-08-03" in bad[0]["error"]
    assert result.exit_code != 0


def test_backfill_guards_against_a_mistyped_year(tmp_path, stub_http):
    root = _backfill_project(tmp_path, stub_http)
    result = runner.invoke(
        cli.app,
        ["backfill", "--since", "2019-01-01", "--max-weeks", "3", "--root", str(root), "--dry-run"],
    )
    plan = [e for e in events(result) if e["event"] == "backfill.plan"][0]
    assert len(plan["weeks"]) == 3
    assert plan["capped"] > 300, "and it says how many it dropped rather than shortening quietly"


def test_backfill_rejects_a_non_iso_date(tmp_path, stub_http):
    root = _backfill_project(tmp_path, stub_http)
    result = runner.invoke(
        cli.app, ["backfill", "--since", "last august", "--root", str(root)]
    )
    assert result.exit_code != 0
    assert "ISO" in result.output


def test_a_dry_run_generates_nothing(tmp_path, stub_http):
    root = _backfill_project(tmp_path, stub_http)
    runner.invoke(
        cli.app,
        ["backfill", "--since", "2026-08-03", "--until", "2026-08-24", "--root", str(root), "--dry-run"],
    )
    conn = db.connect(root / "data" / "items.db")
    assert db.brief_weeks(conn, "good") == []
    conn.close()


def test_the_weekly_run_applies_the_publication_age_bound(tmp_path, monkeypatch):
    """Through _synthesize_one, not just the query it calls.

    Testing db.items_fetched_since directly passes even if the CLI never passes
    published_after, which is how this nearly shipped unguarded.
    """
    conn = db.connect(tmp_path / "x.db")
    db.upsert_items(
        conn,
        [
            Item(source="feed_a", url="https://example.test/recent", title="A recent award",
                 body="b", published_at=(NOW - timedelta(days=3)).isoformat()),
            Item(source="feed_a", url="https://example.test/ancient", title="An ancient award",
                 body="b", published_at="2024-03-01T00:00:00Z"),
        ],
        now=NOW,
        source_key="feed_a",
    )

    seen = {}

    class Recording:
        name = "recording"

        def available(self):
            return True

        def status(self):
            return "ready"

        def context_window(self):
            return 100_000

        def complete(self, messages):
            seen["prompt"] = " ".join(m["content"] for m in messages)
            return json.dumps(
                {"buckets": [{"name": "Funding",
                              "entries": [{"text": "x", "item_ids": [1]}]}]}
            )

    monkeypatch.setattr(cli.llm, "require_provider", lambda cfg: Recording())
    profile = make_profile(config={"smtp": {}, "select": {"max_item_age_days": 90}})
    cli._synthesize_one(conn, profile, week_start=WEEK, now=NOW, root=None)

    assert "A recent award" in seen["prompt"]
    assert "An ancient award" not in seen["prompt"], (
        "a deep historical ingest must not flood the weekly brief"
    )
    conn.close()


def test_synthesize_refuses_a_past_week_and_points_at_backfill(tmp_path, stub_http):
    """It selects by FETCH date, so a past week would be briefed with this week's items.

    Caught in use: `synthesize --week 2026-08-10` silently rebuilt that week from
    the current window, considering 177 items none of which were published then.
    """
    root = project(tmp_path, stub_http.url + "/feed")
    result = runner.invoke(
        cli.app, ["synthesize", "--week", "2020-01-06", "--root", str(root)]
    )
    assert result.exit_code != 0
    assert "backfill" in result.output, "the message must name the tool that is correct"


def test_synthesize_still_accepts_the_current_week(tmp_path, stub_http, monkeypatch):
    root = project(tmp_path, stub_http.url + "/feed")
    this_week = cli.monday_of(datetime.now(timezone.utc))
    monkeypatch.setattr(
        cli.llm, "require_provider",
        lambda cfg: (_ for _ in ()).throw(cli.llm.ProviderNotAvailable("x", "no provider")),
    )
    result = runner.invoke(
        cli.app, ["synthesize", "--week", this_week, "--root", str(root)]
    )
    assert "is not the current week" not in result.output


def test_backfill_force_regenerates_a_week_that_already_has_a_brief(tmp_path, stub_http):
    """Needed after a prompt or item-format change; the default still skips."""
    root = _backfill_project(tmp_path, stub_http)
    conn = db.connect(root / "data" / "items.db")
    db.record_brief(
        conn, profile="good", week_start="2026-08-10", generated_at=NOW, provider="x",
        model="m", prompt_hash="h", input_item_ids=[], raw_response="{}",
        result_json='{"buckets": []}', markdown="# old",
    )
    conn.close()

    without = runner.invoke(cli.app, ["backfill", "--since", "2026-08-10",
                                      "--until", "2026-08-10", "--root", str(root), "--dry-run"])
    assert [e for e in events(without) if e["event"] == "backfill.plan"][0]["weeks"] == []

    with_force = runner.invoke(cli.app, ["backfill", "--since", "2026-08-10",
                                         "--until", "2026-08-10", "--root", str(root),
                                         "--force", "--dry-run"])
    assert [e for e in events(with_force) if e["event"] == "backfill.plan"][0]["weeks"] == ["2026-08-10"]


def test_backfill_force_still_refuses_a_delivered_week(tmp_path, stub_http):
    """Rebuilding a page someone has read would destroy its provenance."""
    root = _backfill_project(tmp_path, stub_http)
    conn = db.connect(root / "data" / "items.db")
    db.record_brief(
        conn, profile="good", week_start="2026-08-10", generated_at=NOW, provider="x",
        model="m", prompt_hash="h", input_item_ids=[], raw_response="{}",
        result_json='{"buckets": []}', markdown="# delivered",
    )
    db.mark_sent(conn, "good", "2026-08-10", now=NOW)
    conn.close()

    result = runner.invoke(cli.app, ["backfill", "--since", "2026-08-10", "--until",
                                     "2026-08-10", "--root", str(root), "--force"])
    assert any(e["event"] == "backfill.already_delivered" for e in events(result))
    conn = db.connect(root / "data" / "items.db")
    assert db.get_brief(conn, "good", "2026-08-10")["markdown"] == "# delivered"
    conn.close()


# --- reindex ----------------------------------------------------------------


def _award_project(tmp_path, stub_http):
    root = project(tmp_path, stub_http.url + "/feed")
    conn = db.connect(root / "data" / "items.db")
    db.upsert_items(
        conn,
        [
            Item(
                source="nsf", url="https://x/1", title="An award", body="abstract",
                published_at="2026-09-01",
                raw={"id": "1", "title": "An award", "piFirstName": "Ada", "piLastName": "Lovelace",
                     "fundsObligatedAmt": "500000", "awardeeName": "A University",
                     "date": "09/01/2026", "abstractText": "abstract"},
            )
        ],
        now=NOW,
        source_key="nsf#aaaa",
    )
    conn.close()
    return root


def test_reindex_rebuilds_derived_fields_without_a_network_call(tmp_path, stub_http):
    """The whole point: facts are a pure function of raw_json, already stored."""
    root = _award_project(tmp_path, stub_http)
    conn = db.connect(root / "data" / "items.db")
    conn.execute("UPDATE items SET facts_json = NULL")
    conn.commit()
    conn.close()

    before = len(stub_http.requests)
    result = runner.invoke(cli.app, ["reindex", "--root", str(root)])
    assert [e for e in events(result) if e["event"] == "reindex.done"][0]["changed"] == 1
    assert len(stub_http.requests) == before, "reindex must not touch the network"

    conn = db.connect(root / "data" / "items.db")
    facts = json.loads(conn.execute("SELECT facts_json FROM items").fetchone()["facts_json"])
    assert facts["PI"] == "Ada Lovelace"
    assert facts["Amount"] == "500000"
    conn.close()


def test_reindex_creates_no_rows_and_changes_no_hashes(tmp_path, stub_http):
    """Neither derived column feeds content_hash, so dedup is untouched."""
    root = _award_project(tmp_path, stub_http)
    conn = db.connect(root / "data" / "items.db")
    before = conn.execute("SELECT COUNT(*), GROUP_CONCAT(content_hash) FROM items").fetchone()
    conn.close()

    runner.invoke(cli.app, ["reindex", "--root", str(root)])

    conn = db.connect(root / "data" / "items.db")
    after = conn.execute("SELECT COUNT(*), GROUP_CONCAT(content_hash) FROM items").fetchone()
    assert tuple(after) == tuple(before)
    conn.close()


def test_reindex_is_idempotent(tmp_path, stub_http):
    root = _award_project(tmp_path, stub_http)
    runner.invoke(cli.app, ["reindex", "--root", str(root)])
    second = runner.invoke(cli.app, ["reindex", "--root", str(root)])
    assert [e for e in events(second) if e["event"] == "reindex.done"][0]["changed"] == 0, (
        "nothing changed, so nothing should be rewritten"
    )


def test_reindex_leaves_feed_rows_alone(tmp_path, stub_http):
    """A feed entry has no raw record; guessing would be worse than skipping."""
    root = _award_project(tmp_path, stub_http)
    conn = db.connect(root / "data" / "items.db")
    db.upsert_items(
        conn, [Item(source="feed_a", url="https://x/feed", title="A story", body="b")],
        now=NOW, source_key="feed_a",
    )
    conn.close()

    result = runner.invoke(cli.app, ["reindex", "--root", str(root)])
    assert [e for e in events(result) if e["event"] == "reindex.done"][0]["examined"] == 1


def test_a_dry_run_counts_without_writing(tmp_path, stub_http):
    root = _award_project(tmp_path, stub_http)
    conn = db.connect(root / "data" / "items.db")
    conn.execute("UPDATE items SET facts_json = NULL")
    conn.commit()
    conn.close()

    result = runner.invoke(cli.app, ["reindex", "--root", str(root), "--dry-run"])
    assert [e for e in events(result) if e["event"] == "reindex.plan"][0]["candidates"] == 1

    conn = db.connect(root / "data" / "items.db")
    assert conn.execute("SELECT facts_json FROM items").fetchone()["facts_json"] is None
    conn.close()


def test_an_unreadable_raw_record_is_reported_not_fatal(tmp_path, stub_http):
    root = _award_project(tmp_path, stub_http)
    conn = db.connect(root / "data" / "items.db")
    conn.execute("UPDATE items SET raw_json = 'not json'")
    conn.commit()
    conn.close()
    result = runner.invoke(cli.app, ["reindex", "--root", str(root)])
    assert result.exit_code == 0, "one bad row must not stop the rebuild"


# --- tags: reindex derives them, the tags command reads them ---------------


def _tag_root(tmp_path, vocab: str = "registry:\n  genomics:\n    match: [genome]\n  rust: {}\n"):
    root = tmp_path / "root"
    (root / "profiles").mkdir(parents=True)
    (root / "config.yaml").write_text("db: data/items.db\nsmtp: {host: localhost, port: 2525}\n")
    (root / "sources.yaml").write_text("feed_a:\n  kind: rss\n  url: https://example.test/feed\n")
    (root / "tags.yaml").write_text(vocab)
    conn = db.connect(root / "data" / "items.db")
    db.upsert_items(
        conn,
        [
            Item(source="feed_a", url="https://example.test/g", title="A maize genome", body="assembled"),
            Item(source="feed_a", url="https://example.test/p", title="Parking", body="lot 4"),
            Item(
                source="github_repos", url="https://github.com/o/r", title="o/r", body="a rust tool",
                raw={"full_name": "o/r", "html_url": "https://github.com/o/r", "topics": ["rust", "phylogenetics"],
                     "language": "Rust", "stargazers_count": 1, "pushed_at": "2026-09-01T00:00:00Z",
                     "created_at": "2026-01-01T00:00:00Z", "owner": {"login": "o"}},
            ),
        ],
        now=NOW,
    )
    conn.close()
    return root


def test_reindex_derives_tags_and_is_idempotent(tmp_path):
    root = _tag_root(tmp_path)
    first = runner.invoke(cli.app, ["reindex", "--root", str(root)])
    assert first.exit_code == 0, first.output
    events = [json.loads(line) for line in first.output.splitlines() if line.startswith("{")]
    tags_event = next(e for e in events if e["event"] == "reindex.tags")
    assert tags_event["tagged"] == 2 and tags_event["changed"] == 2 and tags_event["new_terms"] == 1
    second = runner.invoke(cli.app, ["reindex", "--root", str(root)])
    again = next(json.loads(l) for l in second.output.splitlines() if '"reindex.tags"' in l)
    assert again["changed"] == 0
    conn = db.connect(root / "data" / "items.db")
    by_url = {r[0]: r[1] for r in conn.execute("SELECT url, id FROM items")}
    assert db.tags_for(conn, by_url.values()) == {
        by_url["https://example.test/g"]: ["genomics"],
        by_url["https://github.com/o/r"]: ["rust"],
    }
    origins = dict(conn.execute("SELECT tag, origin FROM item_tags").fetchall())
    assert origins == {"genomics": "phrase", "rust": "source"}


def test_tags_command_counts_new_and_item(tmp_path):
    root = _tag_root(tmp_path)
    runner.invoke(cli.app, ["reindex", "--root", str(root)])
    counts = runner.invoke(cli.app, ["tags", "--root", str(root), "--counts"])
    assert counts.exit_code == 0 and "genomics  1" in counts.output and "rust  1" in counts.output
    new = runner.invoke(cli.app, ["tags", "--root", str(root), "--new"])
    assert new.exit_code == 0 and "phylogenetics" in new.output and "rust" not in new.output
    conn = db.connect(root / "data" / "items.db")
    gid = conn.execute("SELECT id FROM items WHERE url='https://example.test/g'").fetchone()[0]
    one = runner.invoke(cli.app, ["tags", "--root", str(root), "--item", str(gid)])
    assert one.output.strip() == "genomics"
    nothing = runner.invoke(cli.app, ["tags", "--root", str(root)])
    assert nothing.exit_code == 2


def test_reindex_without_a_vocabulary_leaves_tags_alone(tmp_path):
    root = _tag_root(tmp_path)
    (root / "tags.yaml").unlink()
    result = runner.invoke(cli.app, ["reindex", "--root", str(root)])
    assert result.exit_code == 0 and '"reindex.tags_skipped"' in result.output
    conn = db.connect(root / "data" / "items.db")
    assert conn.execute("SELECT COUNT(*) FROM item_tags").fetchone()[0] == 0


def test_a_profile_naming_an_unknown_tag_is_skipped_not_run(tmp_path):
    root = _tag_root(tmp_path)
    (root / "profiles" / "ok.yaml").write_text(
        "name: ok\ntitle: OK\ncadence: weekly\naudience: a\npersona: b\nsources: [feed_a]\n"
        "relevance:\n  any_of: [genome]\n  tags: [genomics]\nbuckets:\n  - {name: Funding, ask: awards}\n"
        "delivery: {from: a@example.test, to: [b@example.test]}\n"
    )
    (root / "profiles" / "typo.yaml").write_text(
        "name: typo\ntitle: Typo\ncadence: weekly\naudience: a\npersona: b\nsources: [feed_a]\n"
        "relevance:\n  any_of: [genome]\n  tags: [genomcs]\nbuckets:\n  - {name: Funding, ask: awards}\n"
        "delivery: {from: a@example.test, to: [b@example.test]}\n"
    )
    _config, _sources, profiles = cli.load_world(root)
    assert [p.name for p in profiles] == ["ok"]
