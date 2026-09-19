"""Four commands, and the ordering that makes them safe to run from cron.

App glue: argument parsing, run order, per-profile isolation, and JSON-line
logging. No policy lives here that is not about sequencing.

Two rules shape all of it. **One profile failing must not stop the others** —
each is wrapped, logged, and the run continues, because a brief that arrives
for three of four readers beats a run that aborted on the first bad YAML file.
And **silence is never ambiguous**: a profile that cannot be synthesized gets
a stub email saying so, with the candidate count, rather than nothing.
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer

from brief import (
    db,
    llm,
    render as render_mod,
    select as select_mod,
    synthesize as synth_mod,
)
from brief.deliver import AlreadySent, deliver as deliver_one
from brief.lib.config_env_interpolate import MissingConfigVar
from brief.lib.macos_keychain_read import KeychainError, is_available, read_secrets
from brief.lib.period_backfill_plan import period_bounds, plan_backfill
from brief.lib.llm_json_contract import JsonContractError
from brief.models import Profile
from brief.profile import (
    ProfileError,
    fetch_plan,
    load_all_profiles,
    load_sources,
    load_yaml,
)
from brief.sources import fetch as fetch_source
from brief.sources import rederive, relabel, supports_history
from brief import tags as tags_mod
from brief.lib.distinctive_terms import distinctive_terms

app = typer.Typer(add_completion=False, help="Weekly topic briefs from public sources.")

ROOT = Path.cwd()


def log(event: str, **fields: Any) -> None:
    """One JSON line per event, to stdout, captured by Docker or launchd."""
    payload = {"event": event, **fields}
    print(json.dumps(payload, default=str), flush=True)


#: Secrets this app may need. Named after the variables that consume them, so
#: a keychain entry and a `${VAR}` reference in config are the same word.
SECRET_NAMES = (
    "CD_TOKEN",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
)

#: The generic-password service secrets live under. One entry per variable:
#:     security add-generic-password -s topic-brief -a CD_TOKEN -w
DEFAULT_KEYCHAIN_SERVICE = "topic-brief"


def read_env(root: Path, *, keychain_service: str | None = None) -> dict[str, str]:
    """Environment for `${VAR}` resolution, from three sources in this order.

    1. The process environment.
    2. A dotenv file beside the config, if there is one.
    3. The macOS keychain, if it is available.

    Later wins, so the keychain is the most authoritative. That ordering is
    deliberate: the keychain is the one place a secret is not sitting in a file,
    so once an operator has put it there it should not be silently overridden by
    a stale export or an old dotenv left on disk.

    A keychain that exists but cannot be read raises rather than falling back.
    Falling back would run the job with whatever stale value was lying around
    and call it success, which is the failure mode this whole path exists to
    remove. A keychain with no such entry is not an error — that is simply a
    secret configured somewhere else.

    Values are never logged.
    """
    import os

    env = dict(os.environ)

    for name in (".env", "env"):
        path = root / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            env[key] = value.strip().strip('"').strip("'")
        break

    service = keychain_service or DEFAULT_KEYCHAIN_SERVICE
    if is_available():
        env.update(read_secrets(service, SECRET_NAMES))

    return env


def load_world(root: Path) -> tuple[dict[str, Any], dict[str, Any], list[Profile]]:
    """Config, sources, and the profiles that loaded.

    A malformed **profile** is logged and skipped so the others still run: a
    brief that arrives for three of four readers beats a run that aborted on
    the first bad YAML file.

    A malformed `config.yaml` or `sources.yaml` still raises, and should. Those
    are shared state — carrying on with half a source registry would produce
    briefs that are quietly missing sources, which is worse than not running.
    """
    env = read_env(root)
    config = load_yaml(root / "config.yaml")
    sources = load_sources(root / "sources.yaml", env)
    skipped: list[str] = []

    def skip(file: Path, exc: Exception) -> None:
        skipped.append(file.name)
        log(
            "profile.error",
            file=str(file),
            error=str(exc),
            type=type(exc).__name__,
            note="this profile is skipped; the others still run",
        )

    profiles = load_all_profiles(
        root / "profiles", config=config, sources=sources, env=env, on_error=skip
    )
    vocab = tags_mod.load_vocabulary(root / _tags_path(config))
    kept: list[Profile] = []
    for prof in profiles:
        unknown = tags_mod.unknown_tags(prof.relevance.tags, vocab)
        if unknown:
            # A typo here would silently match nothing, which reads as a quiet
            # week. Treated like a malformed profile: named, skipped, others run.
            skipped.append(prof.name)
            log(
                "profile.error",
                file=prof.name,
                error=f"relevance.tags names {unknown} which tags.yaml does not define",
                type="UnknownTag",
                note="this profile is skipped; the others still run",
            )
            continue
        kept.append(prof)
    if skipped:
        log("profile.skipped", count=len(skipped), files=skipped)
    return config, sources, kept


def _tags_path(config: dict[str, Any]) -> str:
    return str(config.get("tags") or tags_mod.DEFAULT_PATH)


def pick(profiles: list[Profile], name: str | None, want_all: bool) -> list[Profile]:
    if want_all or name is None:
        return profiles
    chosen = [p for p in profiles if p.name == name]
    if not chosen:
        raise typer.BadParameter(
            f"no profile named {name!r}; found {[p.name for p in profiles]}"
        )
    return chosen


def monday_of(moment: datetime) -> str:
    return (moment.date() - timedelta(days=moment.date().weekday())).isoformat()


@app.command()
def ingest(
    root: Path = typer.Option(ROOT, help="Project root holding config.yaml"),
    source: str | None = typer.Option(None, help="Fetch only this source"),
    since: str | None = typer.Option(
        None, help="Reach back to this ISO date instead of the usual window"
    ),
) -> None:
    """Fetch every source any enabled profile references, and store what is new.

    Sources are deduplicated across profiles first: the same source with the
    same parameters is one fetch however many profiles want it. A source that
    errors is logged and the rest continue, because one dead site must not
    cost a week of everything else.
    """
    config, sources, profiles = load_world(root)
    now = datetime.now(timezone.utc)
    conn = db.connect(root / (str(config.get("db") or "data/items.db")))

    ingest_cfg = config.get("ingest") or {}
    plan = [
        (n, p, k) for n, p, k in fetch_plan(profiles) if source is None or n == source
    ]
    if since:
        try:
            since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise typer.BadParameter(f"--since must be an ISO date: {exc}")
        cannot = sorted(
            {n for n, _p, _k in plan if not supports_history(str(sources[n].get("kind")))}
        )
        if cannot:
            log(
                "ingest.history_unavailable",
                requested_since=since_dt.date().isoformat(),
                sources=cannot,
                note="these serve only their most recent entries and have no date filter",
            )
    else:
        since_dt = now - timedelta(days=int(ingest_cfg.get("lookback_days", 30)))
    since = since_dt
    body_cap = int(ingest_cfg.get("body_cap_bytes", db.DEFAULT_BODY_CAP))

    total_new = 0
    failures = 0
    for name, params, key in plan:
        # The upsert is inside the try on purpose: a single malformed item
        # must cost that source, not the rest of the run.
        try:
            items, state = fetch_source(
                name,
                sources[name],
                params,
                since=since,
                now=now,
                state=db.get_source_state(conn, key),
            )
            added = db.upsert_items(
                conn, items, now=now, source_key=key, body_cap=body_cap
            )
            if state:
                db.set_source_state(
                    conn,
                    key,
                    etag=state.get("etag"),
                    last_modified=state.get("last_modified"),
                    now=now,
                )
        except Exception as exc:
            failures += 1
            log(
                "source.error",
                source=name,
                source_key=key,
                error=str(exc),
                type=type(exc).__name__,
            )
            continue
        total_new += added
        log("source.ok", source=name, source_key=key, fetched=len(items), new=added)

    threshold = float(ingest_cfg.get("silent_source_days", 14))
    silent = db.silent_sources(
        db.last_seen_by_source(conn),
        [k for _n, _p, k in plan],
        now=now,
        threshold_days=threshold,
    )
    for name, days in silent:
        log("source.silent", source=name, days_silent=days, threshold_days=threshold)

    log(
        "ingest.done",
        sources=len(plan),
        new=total_new,
        failures=failures,
        silent=len(silent),
    )


@app.command()
def synthesize(
    profile: str | None = typer.Option(None, "--profile", help="Profile name"),
    all_profiles: bool = typer.Option(False, "--all", help="Every profile"),
    root: Path = typer.Option(ROOT),
    week: str | None = typer.Option(
        None, help="Monday, ISO date; defaults to this week"
    ),
    force: bool = typer.Option(
        False, "--force", help="Regenerate a week that has already been delivered"
    ),
) -> None:
    """Turn the week's candidates into a brief, once per profile.

    A week that has already been delivered is left alone. Overwriting it would
    replace the provenance of a page somebody has already read — the stored
    prompt hash, provider and raw response that make a claim checkable after
    the fact. `--force` is the deliberate way to regenerate one.
    """
    config, _sources, profiles = load_world(root)
    now = datetime.now(timezone.utc)
    week_start = week or monday_of(now)
    conn = db.connect(root / (str(config.get("db") or "data/items.db")))

    if week and week_start != monday_of(now):
        raise typer.BadParameter(
            f"--week {week_start} is not the current week. synthesize selects items "
            f"by when they were FETCHED, so asking it for a past week would brief "
            f"that week using THIS week's items. Use: brief backfill --since "
            f"{week_start} --until {week_start} --force , which selects by "
            f"publication date."
        )

    failures = 0
    for prof in pick(profiles, profile, all_profiles):
        existing = db.get_brief(conn, prof.name, week_start)
        if existing and existing["sent_at"] and not force:
            log(
                "synthesize.already_delivered",
                profile=prof.name,
                week=week_start,
                sent_at=existing["sent_at"],
                hint="pass --force to regenerate and discard its provenance",
            )
            continue
        try:
            _synthesize_one(conn, prof, week_start=week_start, now=now, root=root)
        except Exception as exc:
            failures += 1
            log(
                "synthesize.failed",
                profile=prof.name,
                error=str(exc),
                type=type(exc).__name__,
                traceback=traceback.format_exc(limit=3),
            )
    if failures:
        raise typer.Exit(code=1)


def _synthesize_one(
    conn,
    prof: Profile,
    *,
    week_start: str,
    now: datetime,
    root: Path,
    rows: list[dict[str, Any]] | None = None,
    replace: bool = False,
) -> None:
    """Synthesize one week. `rows` lets backfill supply a different window.

    Everything after the selection is identical, which is the point: a
    backfilled brief goes through the same prompt, the same JSON contract and
    the same citation check as a live one, so the two are comparable.
    """
    if rows is None:
        max_age = int((prof.config.get("select") or {}).get("max_item_age_days", 90))
        rows = db.items_fetched_since(
            conn,
            [r.source_key() for r in prof.sources],
            since=now - timedelta(days=7),
            published_after=(now - timedelta(days=max_age)).date().isoformat(),
        )

    try:
        provider = llm.require_provider(prof.llm)
        window = provider.context_window()
    except Exception as exc:
        # Every provider failure class lands here — missing binary, bad config,
        # a rate limit, an Ollama that died mid-probe. The reader gets a page
        # saying so; silence would be indistinguishable from a quiet week.
        _record_stub(
            conn,
            prof,
            week_start=week_start,
            now=now,
            reason=str(exc),
            candidates=len(rows),
        )
        log(
            "synthesize.provider_unavailable",
            profile=prof.name,
            reason=str(exc),
            type=type(exc).__name__,
        )
        return

    tags_by_item = db.tags_for(conn, [int(r["id"]) for r in rows])
    selection = select_mod.select(
        rows, prof, context_window_tokens=window, tags_by_item=tags_by_item
    )
    if not selection.rendered:
        _record_stub(
            conn,
            prof,
            week_start=week_start,
            now=now,
            reason="No items matched this profile's keywords this week.",
            candidates=len(rows),
            provider_name=str(getattr(provider, "name", "unknown")),
        )
        log("synthesize.no_candidates", profile=prof.name, considered=len(rows))
        return

    try:
        result = synth_mod.synthesize(prof, selection, provider)
    except JsonContractError as exc:
        _record_stub(
            conn,
            prof,
            week_start=week_start,
            now=now,
            reason="The model did not return a valid result after a retry.",
            candidates=len(rows),
            provider_name=str(getattr(provider, "name", "unknown")),
            raw_response="\n\n".join(exc.responses),
        )
        log("synthesize.contract_failed", profile=prof.name, errors=list(exc.errors))
        return

    urls = db.urls_for_ids(conn, [i for i, _ in selection.rendered])
    page = render_mod.render(
        result.result,
        prof,
        selection,
        week_start=week_start,
        provider_name=result.provider_name,
        item_urls=urls,
        item_tags=tags_by_item,
    )

    if page.kept == 0:
        # Not a brief. Every claim the model made cited an item that was not
        # in its input, so none could be verified. An empty page with a title
        # reads as "nothing happened"; what happened is that nothing could be
        # checked, and the reader needs to know which.
        _record_stub(
            conn,
            prof,
            week_start=week_start,
            now=now,
            reason=(
                f"The model returned {page.dropped_count} entries and every one of "
                f"them cited an item that was not in its input, so none could be "
                f"verified. Nothing is shown rather than an unsourced claim."
            ),
            candidates=len(rows),
            provider_name=result.provider_name,
            raw_response=result.raw_response,
        )
        log(
            "synthesize.all_entries_dropped",
            profile=prof.name,
            dropped=page.dropped_count,
        )
        return

    db.record_brief(
        conn,
        profile=prof.name,
        week_start=week_start,
        generated_at=now,
        provider=result.provider_name,
        model=result.model,
        prompt_hash=result.prompt_hash,
        input_item_ids=[i for i, _ in selection.rendered],
        raw_response=result.raw_response,
        result_json=json.dumps(result.result, sort_keys=True),
        markdown=page.markdown,
    )
    log(
        "synthesize.ok",
        profile=prof.name,
        provider=result.provider_name,
        considered=selection.considered,
        matched=selection.matched,
        sent=selection.sent,
        left_out=len(selection.left_out),
        attempts=result.attempts,
        entries_kept=page.kept,
        entries_dropped=page.dropped_count,
        prompt_hash=result.prompt_hash[:12],
    )


def _record_stub(
    conn,
    prof: Profile,
    *,
    week_start: str,
    now: datetime,
    reason: str,
    candidates: int,
    provider_name: str = "none",
    raw_response: str = "",
) -> None:
    """Store a page explaining why there is no brief this week.

    Refuses to overwrite a real brief already stored for this week. A later run
    that hits a dead provider must not turn last night's good, un-sent brief
    into an apology.
    """
    existing = db.get_brief(conn, prof.name, week_start)
    if existing and not json.loads(existing["result_json"] or "{}").get("stub"):
        log(
            "synthesize.stub_suppressed",
            profile=prof.name,
            week=week_start,
            reason="a real brief is already stored for this week; not downgrading it",
        )
        return
    markdown = render_mod.stub_markdown(
        prof, week_start=week_start, reason=reason, candidates=candidates
    )
    db.record_brief(
        conn,
        profile=prof.name,
        week_start=week_start,
        generated_at=now,
        provider=provider_name,
        model="none",
        prompt_hash="",
        input_item_ids=[],
        raw_response=raw_response,
        result_json=json.dumps({"stub": True, "reason": reason}),
        markdown=markdown,
    )


@app.command()
def backfill(
    since: str = typer.Option(..., help="Earliest week to fill, any ISO date in it"),
    until: str | None = typer.Option(None, help="Latest week; default is last week"),
    profile: str | None = typer.Option(None, "--profile"),
    all_profiles: bool = typer.Option(False, "--all"),
    root: Path = typer.Option(ROOT),
    max_weeks: int = typer.Option(52, help="Guard against a mistyped year"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan, generate nothing"),
    force: bool = typer.Option(
        False, "--force", help="Regenerate weeks that already have a brief"
    ),
) -> None:
    """Generate briefs for past weeks that do not have one.

    Backfill differs from the weekly run in one deliberate way: it selects items
    by **publication date**, not by when we fetched them. The live run asks
    "what is new to us"; a backfill asks "what happened that week", and those
    are different questions with different answers.

    A week that already has a brief is skipped, not regenerated. Backfill fills
    gaps, and quietly overwriting a brief you have already read would destroy
    its provenance. `--force` rebuilds them anyway, which is what you want after
    changing the prompt or the item format; a delivered week is still refused.

    Expect older weeks to be thin. The feeds serve only their most recent
    entries, so how far back this reaches is a property of the sources rather
    than of the range you ask for.
    """
    config, _sources, profiles = load_world(root)
    now = datetime.now(timezone.utc)
    conn = db.connect(root / (str(config.get("db") or "data/items.db")))

    try:
        since_date = date.fromisoformat(since)
        until_date = date.fromisoformat(until) if until else None
    except ValueError as exc:
        raise typer.BadParameter(f"dates must be ISO (YYYY-MM-DD): {exc}")

    failures = 0
    for prof in pick(profiles, profile, all_profiles):
        try:
            plan = plan_backfill(
                since=since_date,
                until=until_date,
                cadence="weekly",
                done=[] if force else [
                    date.fromisoformat(w) for w in db.brief_weeks(conn, prof.name)
                ],
                today=now.date(),
                max_periods=max_weeks,
            )
        except ValueError as exc:
            failures += 1
            log("backfill.bad_range", profile=prof.name, error=str(exc))
            continue

        log(
            "backfill.plan",
            profile=prof.name,
            weeks=[w.isoformat() for w in plan.periods],
            already_done=[w.isoformat() for w in plan.already_done],
            capped=plan.capped,
            since=plan.since.isoformat(),
            until=plan.until.isoformat(),
        )
        if dry_run or not plan:
            continue

        for week in plan.periods:
            start, end = period_bounds(week, "weekly")
            rows = db.items_published_between(
                conn,
                [r.source_key() for r in prof.sources],
                start=start.isoformat(),
                end=end.isoformat(),
            )
            if not rows:
                log("backfill.empty_week", profile=prof.name, week=week.isoformat())
                continue
            stored = db.get_brief(conn, prof.name, week.isoformat())
            if force and stored and stored["sent_at"]:
                log(
                    "backfill.already_delivered",
                    profile=prof.name,
                    week=week.isoformat(),
                    sent_at=stored["sent_at"],
                )
                continue
            try:
                _synthesize_one(
                    conn,
                    prof,
                    week_start=week.isoformat(),
                    now=now,
                    root=root,
                    rows=rows,
                    replace=force,
                )
            except Exception as exc:
                failures += 1
                log(
                    "backfill.failed",
                    profile=prof.name,
                    week=week.isoformat(),
                    error=str(exc),
                    type=type(exc).__name__,
                )
    if failures:
        raise typer.Exit(code=1)


@app.command()
def deliver(
    profile: str | None = typer.Option(None, "--profile"),
    all_profiles: bool = typer.Option(False, "--all"),
    root: Path = typer.Option(ROOT),
    week: str | None = typer.Option(None),
    resend: bool = typer.Option(
        False, "--resend", help="Send a week that was already sent"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Archive but do not send"),
) -> None:
    """Archive and email the stored brief for each profile, once."""
    config, _sources, profiles = load_world(root)
    now = datetime.now(timezone.utc)
    week_start = week or monday_of(now)
    conn = db.connect(root / (str(config.get("db") or "data/items.db")))

    failures = 0
    for prof in pick(profiles, profile, all_profiles):
        stored = db.get_brief(conn, prof.name, week_start)
        if stored is None:
            failures += 1
            pending = [
                r["week_start"]
                for r in conn.execute(
                    "SELECT week_start FROM briefs WHERE profile = ? AND sent_at IS NULL"
                    " ORDER BY week_start DESC LIMIT 5",
                    (prof.name,),
                )
            ]
            # Naming the weeks that do exist turns "nothing for this Monday"
            # into an actionable line. A synthesize/deliver pair that straddles
            # UTC midnight computes two different weeks, and this is what makes
            # that visible instead of looking like an empty week.
            log(
                "deliver.missing",
                profile=prof.name,
                week=week_start,
                unsent_weeks=pending,
            )
            continue
        item_count = len(json.loads(stored["input_item_ids"] or "[]"))
        try:
            sent = deliver_one(
                stored["markdown"],
                prof,
                week_start=week_start,
                item_count=item_count,
                briefs_root=root / "briefs",
                smtp=dict(prof.config.get("smtp") or {}),
                sent_at=stored["sent_at"],
                resend=resend,
                now=now,
                send=_archive_only if dry_run else None,
            )
        except AlreadySent as exc:
            log("deliver.skipped", profile=prof.name, week=week_start, reason=str(exc))
            continue
        except Exception as exc:
            failures += 1
            log(
                "deliver.failed",
                profile=prof.name,
                error=str(exc),
                type=type(exc).__name__,
            )
            continue

        # Recorded in the same block as the successful send. Outside the try,
        # a send that worked and a mark that failed would leave sent_at NULL
        # and the next run would mail the same brief again.
        if not dry_run:
            try:
                db.mark_sent(conn, prof.name, week_start, now=now)
            except Exception as exc:
                failures += 1
                log(
                    "deliver.unrecorded",
                    profile=prof.name,
                    week=week_start,
                    error=str(exc),
                    note="the brief WAS sent; mark it by hand or the next run resends",
                )
        log(
            "deliver.partial" if sent.refused else "deliver.ok",
            profile=prof.name,
            week=week_start,
            path=str(sent.path),
            recipients=len(sent.recipients),
            refused=sorted(sent.refused),
            dry_run=dry_run,
            resent=sent.resent,
        )
    if failures:
        raise typer.Exit(code=1)


def _archive_only(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """The `--dry-run` send: writes the archive, opens no socket."""
    return {}


@app.command()
def reindex(
    root: Path = typer.Option(ROOT),
    source: str | None = typer.Option(None, help="Only this source"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count what would change"),
) -> None:
    """Recompute derived fields from the raw records already stored.

    Offline. No network, no API calls, no new rows. `facts` and `published_at`
    are pure functions of `raw_json`, which every award row keeps, so adding a
    field or correcting a date mapping should not mean refetching a thousand
    records from an agency.

    Neither column feeds `content_hash`, so this cannot create a duplicate or
    change what dedup treats as the same item. It re-runs the seeds' own
    normalizers and the adapters' own facts mappings, so a reindex cannot drift
    from an ingest: they are the same code.

    Sources with no raw record — feeds — are left alone rather than guessed at.
    """
    config, _sources, _profiles = load_world(root)
    conn = db.connect(root / (str(config.get("db") or "data/items.db")))

    updates: list[tuple[int, dict[str, str] | None, str | None]] = []
    skipped = 0
    for item_id, src, raw in db.iter_raw_records(conn, [source] if source else None):
        try:
            facts, published_at = rederive(src, raw)
        except Exception as exc:
            skipped += 1
            log("reindex.error", item=item_id, source=src, error=str(exc))
            continue
        if facts is None and published_at is None:
            continue
        updates.append((item_id, facts, published_at))

    if dry_run:
        log("reindex.plan", candidates=len(updates), unreadable=skipped)
        return

    changed = db.update_derived(conn, updates)
    log("reindex.done", examined=len(updates), changed=changed, unreadable=skipped)
    _retag(conn, root, config, source)


def _retag(conn: Any, root: Path, config: dict[str, Any], source: str | None) -> None:
    """Rebuild every item's tags from its raw record and text.

    Same offline guarantee as facts: not in `content_hash`, no new rows. An
    absent tags.yaml is "tagging off" and leaves the table untouched, so a
    project that has not adopted tags pays nothing here.
    """
    vocab = tags_mod.load_vocabulary(root / _tags_path(config))
    if not vocab:
        log("reindex.tags_skipped", reason="no tags.yaml")
        return
    examined = tagged = changed = unreadable = 0
    new_terms: dict[str, int] = {}
    for item_id, src, title, body, raw in db.iter_taggable(conn, [source] if source else None):
        examined += 1
        try:
            labels = relabel(src, raw) if raw is not None else []
        except Exception as exc:
            unreadable += 1
            log("reindex.tags_error", item=item_id, source=src, error=str(exc))
            continue
        pairs, new = tags_mod.derive_tags(labels, f"{title}\n\n{body}", vocab)
        for term in new:
            new_terms[term] = new_terms.get(term, 0) + 1
        if pairs:
            tagged += 1
        if db.replace_tags(conn, item_id, pairs):
            changed += 1
    conn.commit()
    log(
        "reindex.tags",
        examined=examined,
        tagged=tagged,
        changed=changed,
        unreadable=unreadable,
        new_terms=len(new_terms),
    )


@app.command()
def tags(
    root: Path = typer.Option(ROOT),
    counts: bool = typer.Option(False, "--counts", help="Items per tag"),
    by_source: bool = typer.Option(False, "--by-source", help="With --counts: split per source"),
    leaves: bool = typer.Option(False, "--leaves", help="With --counts: hide tags earned only as a parent"),
    new: bool = typer.Option(False, "--new", help="Labels the sources send that tags.yaml does not know"),
    suggest: bool = typer.Option(False, "--suggest", help="Words the UNTAGGED items are made of — candidates for match phrases"),
    item: int | None = typer.Option(None, "--item", help="Tags on one item id"),
    source: str | None = typer.Option(None, help="Only this source"),
    top: int = typer.Option(40, help="Rows to show"),
    min_count: int = typer.Option(10, help="With --suggest: a word must appear in this many untagged items"),
    min_ratio: float = typer.Option(1.5, help="With --suggest: how much commoner among untagged than tagged items"),
) -> None:
    """Explore the tags: what the corpus carries, and what it could carry.

    `--counts` reads the stored table. `--new` re-derives offline and reports
    the labels the resolver could not place, most frequent first — that list
    is how the registry grows. `--suggest` reads the untagged items' own
    words against the tagged ones — that list is how `match` phrases grow.
    Both from evidence rather than memory. Nothing here writes.
    """
    config, _sources, _profiles = load_world(root)
    conn = db.connect(root / (str(config.get("db") or "data/items.db")))
    if item is not None:
        for tag in db.tags_for(conn, [item]).get(item, []):
            typer.echo(tag)
        return
    if counts:
        rows = db.tag_counts(conn, by_source=by_source, leaves=leaves)
        for row in rows[:top]:
            typer.echo("  ".join(str(x) for x in row))
        if not rows:
            typer.echo("no tags stored — run `brief reindex` after writing tags.yaml")
        return
    if new:
        vocab = tags_mod.load_vocabulary(root / _tags_path(config))
        seen: dict[str, int] = {}
        for _id, src, title, body, raw in db.iter_taggable(conn, [source] if source else None):
            try:
                labels = relabel(src, raw) if raw is not None else []
            except Exception:
                continue
            _pairs, unplaced = tags_mod.derive_tags(labels, f"{title}\n\n{body}", vocab) if vocab else (
                [],
                [tags_mod.kebab_case(x) for x in labels],
            )
            for term in unplaced:
                seen[term] = seen.get(term, 0) + 1
        for term, n in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:top]:
            typer.echo(f"{n:>6}  {term}")
        if not seen:
            typer.echo("nothing unplaced — every label the sources send is in tags.yaml")
        return
    if suggest:
        untagged = db.untagged_ids(conn)
        fg: list[str] = []
        bg: list[str] = []
        for item_id, _src, title, body, _raw in db.iter_taggable(conn, [source] if source else None):
            (fg if item_id in untagged else bg).append(f"{title}\n\n{body}")
        terms = distinctive_terms(fg, bg, min_count=min_count, min_ratio=min_ratio, top=top)
        for t in terms:
            typer.echo(f"{t['foreground']:>6}  {t['term']}  (x{t['ratio']})")
        if not fg:
            typer.echo("every item is tagged — nothing to suggest")
        elif not terms:
            typer.echo(f"{len(fg)} untagged items share no word above the floors; lower --min-count or --min-ratio")
        else:
            typer.echo(f"\n{len(fg)} untagged of {len(fg) + len(bg)}. Add the useful ones as `match` phrases in tags.yaml, then `brief reindex`.")
        return
    typer.echo("one of --counts, --new, --suggest, or --item is required")
    raise typer.Exit(code=2)


@app.command()
def trends(
    root: Path = typer.Option(ROOT),
    profile: str = typer.Option(None, help="only this profile's sources, and its keyword filter"),
    by: str = typer.Option("week", help="day | week | month"),
    periods: int = typer.Option(12, help="how many buckets back from the most recent"),
    dates: str = typer.Option(
        "published", help="published = when it happened; fetched = when we learned of it"
    ),
    source: str = typer.Option(None, help="only this source"),
) -> None:
    """How much arrived per day, week or month, per source.

    Counts what is already stored; fetches nothing. `published` dates answer
    "how much is the field producing", and work from the first run because
    the sources carry history. `fetched` answers "how much are we learning",
    and only becomes meaningful once ingest has run on more than one day.
    """
    from brief.lib.ascii_timeseries import BUCKETS, bucket_of, bucket_range, render_bars, tally

    if by not in BUCKETS:
        raise typer.BadParameter(f"by must be one of {list(BUCKETS)}")
    if dates not in ("published", "fetched"):
        raise typer.BadParameter("dates must be 'published' or 'fetched'")

    config, _sources, profiles = load_world(root)
    conn = db.connect(root / str(config.get("db", "data/items.db")))

    wanted_keys: set[str] | None = None
    prof = None
    if profile:
        prof = next((p for p in profiles if p.name == profile), None)
        if prof is None:
            raise typer.BadParameter(f"no profile named {profile!r}")
        wanted_keys = {ref.source_key() for ref in prof.sources}

    column = "published_at" if dates == "published" else "fetched_at"
    rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT source, source_key, title, coalesce(body,'') AS body, {column} AS when_ts "
            f"FROM items WHERE {column} IS NOT NULL"
        )
    ]
    if wanted_keys is not None:
        rows = [r for r in rows if r["source_key"] in wanted_keys]
    if source:
        rows = [r for r in rows if r["source"] == source]

    # The profile's own keyword filter, so "AI news per week" means the same
    # thing here as it does in the brief.
    if prof is not None and prof.relevance.any_of:
        from brief.lib.keyword_relevance import filter_ranked

        rows = [
            row
            for row, _hits in filter_ranked(
                rows,
                lambda r: f"{r['title']} {r['body']}",
                prof.relevance.any_of,
                prof.relevance.none_of,
            )
        ]

    if not rows:
        log("trends.empty", profile=profile, source=source, dates=dates)
        typer.echo("no stored items match that selection")
        return

    counts, unparsed = tally(
        rows, date_of=lambda r: r["when_ts"], series_of=lambda r: r["source"], by=by
    )
    stamps = sorted(str(r["when_ts"])[:10] for r in rows)
    axis = bucket_range(stamps[0], stamps[-1], by=by)[-periods:]
    # Drop series with nothing in the visible window rather than printing
    # empty rows for sources whose history predates it.
    visible = {
        name: row for name, row in counts.items() if any(row.get(b) for b in axis)
    }

    typer.echo(f"items per {by}, by {dates} date"
               + (f", profile {profile}" if profile else "")
               + f"  ({len(rows):,} items)")
    typer.echo("")
    typer.echo(render_bars(visible, axis))

    # The bucket containing today is nearly always incomplete, and an
    # incomplete bucket rendered as a full one reads as a dip — or, when a
    # short-window source lands entirely inside it, as a spike. Say so
    # rather than letting the last bar be misread.
    today = datetime.now(timezone.utc).date().isoformat()
    if axis and bucket_of(today, by=by) == axis[-1]:
        typer.echo(
            f"\nthe last bucket ({axis[-1]}) is still in progress — "
            "not comparable with the ones before it"
        )
    if unparsed:
        typer.echo(f"\n{unparsed:,} item(s) had an unreadable {column} and are not counted")
    hidden = len(counts) - len(visible)
    if hidden:
        typer.echo(f"{hidden} source(s) had nothing in this window")


@app.command()
def doctor(root: Path = typer.Option(ROOT)) -> None:
    """Report whether each profile's LLM provider is usable, and why not.

    Run first from cron. A provider that is missing, not signed in, or has no
    model prints the sentence that names the fix.
    """
    try:
        _config, sources, profiles = load_world(root)
    except (ProfileError, MissingConfigVar, OSError) as exc:
        typer.secho(f"configuration error: {exc}", fg="red")
        if isinstance(exc, MissingConfigVar):
            typer.secho(
                f"  store it in the keychain (preferred):\n"
                f"    security add-generic-password -s {DEFAULT_KEYCHAIN_SERVICE} "
                f"-a {exc.variable} -w\n"
                f"  or set {exc.variable} in the environment or a dotenv file "
                f"(see env.example)",
                fg="yellow",
            )
        raise typer.Exit(code=1)

    typer.echo(f"{len(profiles)} profile(s), {len(sources)} source(s) declared\n")
    bad = 0
    for prof in profiles:
        state = llm.describe(prof.llm)
        mark = "ok  " if state["available"] else "FAIL"
        if not state["available"]:
            bad += 1
        window = (
            f"{state['context_window']:,} tokens" if state["context_window"] else "—"
        )
        typer.echo(f"  [{mark}] {prof.name:20} {state['provider']:14} {window}")
        typer.echo(f"         {state['status']}")
        unknown = [r.name for r in prof.sources if r.name not in sources]
        if unknown:
            typer.echo(f"         unknown sources: {unknown}")
            bad += 1
    if bad:
        raise typer.Exit(code=1)


def main() -> None:
    try:
        app()
    except (ProfileError, MissingConfigVar) as exc:
        print(json.dumps({"event": "config.error", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
