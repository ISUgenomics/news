"""Work out which periods a scheduled job still owes.

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
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass

#: The cadences a period can have. Weekly periods start on Monday, matching
#: ISO 8601, so "week of" means the same thing here as in an ISO week number.
CADENCES: tuple[str, ...] = ("daily", "weekly", "monthly")

DEFAULT_CADENCE = "weekly"
DEFAULT_MAX_PERIODS = 52


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """What still needs doing, what was already done, and what was dropped."""

    periods: tuple[datetime.date, ...]
    already_done: tuple[datetime.date, ...]
    capped: int
    since: datetime.date
    until: datetime.date
    cadence: str

    def __bool__(self) -> bool:
        """Truthy when there is work. `if plan:` reads the way it should."""
        return bool(self.periods)

    def __len__(self) -> int:
        return len(self.periods)


def period_start(day: datetime.date, cadence: str = DEFAULT_CADENCE) -> datetime.date:
    """The canonical first day of the period containing ``day``."""
    _check_cadence(cadence)
    if cadence == "daily":
        return day
    if cadence == "weekly":
        return day - datetime.timedelta(days=day.weekday())
    return day.replace(day=1)


def period_bounds(
    start: datetime.date, cadence: str = DEFAULT_CADENCE
) -> tuple[datetime.date, datetime.date]:
    """``[start, end)`` for the period beginning at ``start``.

    The end is exclusive so a caller can write ``start <= x < end`` without
    worrying whether a timestamp at midnight belongs to this period or the
    next. ``start`` is snapped first, so any day in the period works.
    """
    _check_cadence(cadence)
    start = period_start(start, cadence)
    return start, _next_period(start, cadence)


def plan_backfill(
    *,
    since: datetime.date,
    until: datetime.date | None = None,
    cadence: str = DEFAULT_CADENCE,
    done: Iterable[datetime.date] = (),
    today: datetime.date,
    max_periods: int = DEFAULT_MAX_PERIODS,
) -> BackfillPlan:
    """The periods between ``since`` and ``until`` that are not already done."""
    _check_cadence(cadence)
    if max_periods < 1:
        raise ValueError(f"max_periods must be at least 1, got {max_periods}")

    first = period_start(since, cadence)
    last = (
        _last_complete_period(today, cadence)
        if until is None
        else period_start(until, cadence)
    )
    if first > last:
        raise ValueError(
            f"since ({first}) is after until ({last}); there is no range to fill"
        )

    finished = {period_start(day, cadence) for day in done}

    wanted: list[datetime.date] = []
    skipped: list[datetime.date] = []
    cursor = first
    while cursor <= last:
        (skipped if cursor in finished else wanted).append(cursor)
        cursor = _next_period(cursor, cadence)

    capped = max(0, len(wanted) - max_periods)
    if capped:
        wanted = wanted[capped:]  # keep the most recent; see the module docstring

    return BackfillPlan(
        periods=tuple(wanted),
        already_done=tuple(skipped),
        capped=capped,
        since=first,
        until=last,
        cadence=cadence,
    )


def _last_complete_period(today: datetime.date, cadence: str) -> datetime.date:
    """The period before the one ``today`` falls in.

    Always the previous period, even when ``today`` is the first day of one: a
    period is complete only once it is over, and on Monday morning last week is
    the most recent finished week.
    """
    current = period_start(today, cadence)
    return _previous_period(current, cadence)


def _next_period(start: datetime.date, cadence: str) -> datetime.date:
    if cadence == "daily":
        return start + datetime.timedelta(days=1)
    if cadence == "weekly":
        return start + datetime.timedelta(days=7)
    year, month = (
        (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    )
    return datetime.date(year, month, 1)


def _previous_period(start: datetime.date, cadence: str) -> datetime.date:
    if cadence == "daily":
        return start - datetime.timedelta(days=1)
    if cadence == "weekly":
        return start - datetime.timedelta(days=7)
    year, month = (
        (start.year - 1, 12) if start.month == 1 else (start.year, start.month - 1)
    )
    return datetime.date(year, month, 1)


def _check_cadence(cadence: str) -> None:
    if cadence not in CADENCES:
        raise ValueError(
            f"unknown cadence {cadence!r}; expected one of {', '.join(CADENCES)}"
        )
