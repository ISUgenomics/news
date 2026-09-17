# Seed boundary: silent-source-check

## Purpose
Given last-seen timestamps per source and a threshold in days, return the sources that have been silent longer than the threshold.

## when_to_use (draft for FEATURE.toml)
A scheduled job has been finishing cleanly for weeks while one of its inputs quietly stopped producing, and you need a way to notice that a source went dark instead of mistaking it for a slow week.

## Inputs
- last_seen: Mapping[str, datetime | str | None] — the expected sources by name (the keys) and when each last yielded, as an aware/naive datetime or an ISO 8601 string; None means the source has never been seen
- threshold_days: float — how long a source may go without yielding before it counts as silent; must be >= 0
- now: datetime | None (keyword) — the reference instant, injected so tests are deterministic; defaults to datetime.now(timezone.utc)

## Outputs
- list[SilentSource] — one frozen dataclass per silent source: name, last_seen (aware UTC datetime or None), silent_for (timedelta or None when never seen); sorted never-seen first, then longest silence first, then by name; empty list when nothing is silent
- ValueError on a negative threshold, an unparseable timestamp string, or a timestamp in the future relative to now (a clock skew symptom the caller should see, not have silently swallowed)
- TypeError when a timestamp value is neither datetime, str, nor None

## Dependencies
- none

## Must NOT know about
- config.yaml, sources.yaml, or any profile object — the caller passes the mapping, the seed never reads YAML or decides which sources are 'enabled'
- the Item dataclass or the items table — the caller computes MAX(fetched_at) GROUP BY source; the seed never touches SQLite or knows the column names
- logging configuration — it returns a value; the caller logs or emails
- SMTP, the operator address, or any alert rendering — the spec's 'alert email' is deliver.py's job
- the 14-day figure from the spec — threshold_days is a parameter, never a default baked in
- hardcoded paths, the DB path, or the environment

## Public API
```python
@dataclass(frozen=True)
class SilentSource:
    name: str
    last_seen: datetime | None   # aware UTC, or None when never seen
    silent_for: timedelta | None # None when never seen
def silent_sources(
    last_seen: Mapping[str, datetime | str | None],
    threshold_days: float,
    *,
    now: datetime | None = None,
) -> list[SilentSource]:
    """Sources whose last_seen is more than threshold_days before now, never-seen first, then longest silence first, then by name."""
```

## Test harness
none

## Approved module docstring (write this verbatim into the module)
```
Report which sources have gone silent for longer than a threshold.

A scheduled poller that keeps exiting 0 hides a dead feed: a source that
stopped yielding looks exactly like a quiet fortnight. This module answers
one question — "which of these sources have not yielded anything in more
than N days?" — from values the caller already has, so the check is a pure
function that any ingest loop can call after a run.

Contract:
    silent_sources({"nsf": "2026-09-01T06:00:00+00:00", "inside_isu": None},
                   threshold_days=14, now=<aware datetime>)
    -> [SilentSource("inside_isu", last_seen=None, silent_for=None),
        SilentSource("nsf", last_seen=<dt>, silent_for=<timedelta>)]

The mapping's keys are the expected sources; a value of None means never
seen and is always reported first. Timestamps may be datetimes or ISO 8601
strings (a trailing "Z" is accepted); naive values are taken as UTC. A source
is silent when now - last_seen is strictly greater than the threshold.
A negative threshold, an unparseable string, or a timestamp after ``now``
raises ValueError rather than being hidden — a future timestamp is a clock
problem the operator needs to hear about. The result is sorted never-seen
first, then longest silence first, then by name, so output is stable across
runs and diffable in a log.

Deliberately not done here: reading the database (the caller computes
MAX(fetched_at) per source), deciding which sources are enabled, formatting
or sending the alert, and logging. Those belong to the app; this module
returns a list and nothing else.

No dependencies: stdlib datetime and dataclasses suffice, and ISO parsing is
done with fromisoformat plus a "Z" shim so Python 3.10 works without dateutil.
```

## Prior art
verdict: seed

Five phrasings, all hits are single-keyword noise ('source', 'last', 'feed', 'missing'). The full feature list (45 Python features) contains nothing about liveness, staleness, heartbeats, or timestamp-threshold checks; the closest neighbours are PDF and LLM-provider features. No whole feature and no part matches, so this is a fresh seed. It should be stdlib-only like secret-redaction and sqlite-versioned-schema, which is the library's house style for small verified features.

## Critic verdict: merge
So small the module is a function: the job is `now - t > threshold` over a mapping; ISO parsing and sort order are decoration. Fold into brief/db.py (last_seen_by_source(): MAX(fetched_at) GROUP BY source) plus a ~12-line silent_sources() in the ingest command, with the never-seen-first ordering tests kept. Keep as a seed only if the owner wants it in the library for its own sake; it will band strong but adds a file, a test module and a seeds.toml row for one list comprehension.

### Boundary fixes to apply at write time
- if kept: drop the ValueError on a future timestamp — fetched_at is written by us, so a future value means an NTP/DST step, and aborting the alert pass for it hides the silent sources it was asked about; treat future as 'seen now'
- if kept: threshold_days float is fine; add allowed-silence-per-source override (Mapping[str, float]) since weekly APIs vs daily feeds have different normal gaps — otherwise the 14-day figure ends up hardcoded in the caller anyway
