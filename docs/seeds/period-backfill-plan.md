# Seed boundary: period-backfill-plan

Approved 2026-09-17.

## Purpose
Work out which periods a scheduled job still owes, oldest first.

## when_to_use (draft for FEATURE.toml)
You run something on a schedule and need to fill in the periods it missed — the weeks before
it existed, or the runs that failed — and you want the list of periods to work through,
oldest first, with the ones already done left out.

## Inputs
- `since: date` — start of the range. Snapped down to its period start, so a Wednesday means that week.
- `until: date | None = None` — end of the range, inclusive of its period. `None` means the last **complete** period before `today`.
- `cadence: str = "weekly"` — one of `daily`, `weekly`, `monthly`. Anything else raises.
- `done: Iterable[date] = ()` — period starts the caller already has. Snapped the same way, so a caller may pass any day in a finished period.
- `today: date` — required, never read from the clock.
- `max_periods: int = 52` — guard against a mistyped year.

## Outputs
- `BackfillPlan(periods, already_done, capped, since, until, cadence)`; `periods` is oldest first.
- `ValueError` for an unknown cadence, `max_periods` below 1, or `since` after `until`.

## Decisions, and why

- **Oldest first.** A run that dies halfway then leaves a contiguous run of finished periods
  and an obvious resume point. Newest-first leaves a hole in the middle.
- **The current period is excluded.** It is not missing, it is in progress, and it belongs to
  the live schedule. `until=None` therefore resolves to the last *complete* period.
- **`already_done` is reported, not just filtered.** "Nothing to do" and "everything was
  already done" are different answers and a caller should be able to say which.
- **The cap keeps the most recent periods**, not the oldest. If the cap fires, the caller
  probably mistyped a year; the recent end is the useful end, and `capped` says how many were
  dropped so it is never silent.
- **Dates, not datetimes.** A period is a calendar thing. Taking `date` keeps every timezone
  decision at the caller's edge, where it belongs, and keeps this module free of `tzinfo`.

## Must NOT know about
- items, briefs, profiles, the database, or anything this application stores
- what a period *contains*, or how to do the work
- persistence, retry, concurrency, or how long a period takes
- the clock: `today` is a parameter and there is no clock read anywhere in the module

## Public API
```python
CADENCES: tuple[str, ...]  # ("daily", "weekly", "monthly")

@dataclass(frozen=True)
class BackfillPlan:
    periods: tuple[date, ...]        # oldest first; what still needs doing
    already_done: tuple[date, ...]   # in range, skipped
    capped: int                      # dropped by max_periods, oldest end
    since: date
    until: date
    cadence: str
    def __bool__(self) -> bool: ...  # truthy when there is work

def period_start(day: date, cadence: str = "weekly") -> date
def period_bounds(start: date, cadence: str = "weekly") -> tuple[date, date]  # [start, end)
def plan_backfill(*, since: date, until: date | None = None, cadence: str = "weekly",
                  done: Iterable[date] = (), today: date, max_periods: int = 52) -> BackfillPlan
```

## Test harness
none — the module is pure.

## Approved module docstring (verbatim into the module)
```
Work out which periods a scheduled job still owes.

Situation: something runs weekly and has a history with holes in it — the
weeks before it existed, a fortnight the machine was asleep, a run that
failed. You want the list of periods still to do, and you want it to be
the same list whether you ask now or in an hour.

Contract: plan_backfill(since, until, cadence, done, today) returns period
starts, OLDEST FIRST. Oldest first is the whole ordering decision: a run
that dies halfway leaves a contiguous run of finished periods and an
obvious resume point, where newest-first leaves a hole in the middle.

``until`` defaults to the last COMPLETE period before ``today``. The
current period is deliberately excluded — it is not missing, it is in
progress, and it belongs to the live schedule rather than to a backfill.

``done`` is whatever the caller already has. Periods in it are dropped and
reported separately, so "nothing to do" and "everything was already done"
are distinguishable answers rather than the same empty list.

``max_periods`` is a guard, not a preference. A mistyped year turns into
hundreds of periods, and for a caller that spends a model call per period
that is real money. It keeps the most RECENT periods, because a caller who
trips the guard has usually reached too far back, and it reports how many
it dropped rather than silently shortening.

``today`` is a parameter and there is no clock read anywhere in this
module, so the same arguments always produce the same plan. Inputs are
dates rather than datetimes: a period is a calendar thing, and keeping
timezone decisions at the caller's edge keeps tzinfo out of here entirely.

Deliberately not here: doing the work, knowing what a period contains,
persistence, concurrency, retry. This module answers "which periods", and
nothing else.
```

## Prior art
verdict: seed

Searched three phrasings: "work out which past periods still need processing and which are
already done", "generate the list of weeks between two dates for a recurring report",
"backfill missing runs of a scheduled job". Every hit was lexical noise — PDF features,
`layered-config-overlay`, `secret-scanner` — matching on words like "layer", "between" and
"missing". Nothing in the library plans periods or schedules. Nothing copied.
