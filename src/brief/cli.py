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
from datetime import datetime, timedelta, timezone
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

app = typer.Typer(add_completion=False, help="Weekly topic briefs from public sources.")

ROOT = Path.cwd()


def log(event: str, **fields: Any) -> None:
    """One JSON line per event, to stdout, captured by Docker or launchd."""
    payload = {"event": event, **fields}
    print(json.dumps(payload, default=str), flush=True)


def read_env(root: Path) -> dict[str, str]:
    """Environment for `${VAR}` resolution: the process, then a dotenv file.

    The file wins over the process so an operator can override a stale
    exported value without hunting for the shell that set it. Values are never
    logged.

    Read as ``utf-8-sig`` and tolerant of a leading ``export``, because both
    are what people actually put in these files, and a variable silently lost
    to a byte-order mark surfaces much later as a puzzling 401.
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
    if skipped:
        log("profile.skipped", count=len(skipped), files=skipped)
    return config, sources, profiles


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
    since = now - timedelta(days=int(ingest_cfg.get("lookback_days", 30)))
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
    conn = db.connect(root / str(config.get("db", "data/items.db")))

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
    conn, prof: Profile, *, week_start: str, now: datetime, root: Path
) -> None:
    rows = db.items_fetched_since(
        conn, [r.source_key() for r in prof.sources], since=now - timedelta(days=7)
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

    selection = select_mod.select(rows, prof, context_window_tokens=window)
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
    conn = db.connect(root / str(config.get("db", "data/items.db")))

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
                f"  set {exc.variable} in the environment or the dotenv file "
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
