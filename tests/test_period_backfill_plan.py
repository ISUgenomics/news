"""Tests for the backfill planner. Pure, so no harness and no fixtures.

Dates are written as real calendar dates rather than offsets, because the
month-boundary and year-boundary cases are where an off-by-one hides and an
offset would obscure which case is being checked.
"""

from __future__ import annotations

from datetime import date

import pytest

from brief.lib.period_backfill_plan import (
    CADENCES,
    BackfillPlan,
    period_bounds,
    period_start,
    plan_backfill,
)

# 2026-09-17 is a Thursday; its week began Monday 2026-09-14.
THURSDAY = date(2026, 9, 17)
THIS_MONDAY = date(2026, 9, 14)
LAST_MONDAY = date(2026, 9, 7)


# --- period_start ---------------------------------------------------------


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2026, 9, 14), date(2026, 9, 14)),  # Monday is its own start
        (date(2026, 9, 17), date(2026, 9, 14)),  # Thursday
        (date(2026, 9, 20), date(2026, 9, 14)),  # Sunday still belongs to that week
        (date(2026, 9, 21), date(2026, 9, 21)),  # next Monday starts the next week
        (date(2026, 1, 1), date(2025, 12, 29)),  # a week spanning the year boundary
    ],
)
def test_weekly_period_start_is_the_monday(day, expected):
    assert period_start(day, "weekly") == expected


def test_daily_period_start_is_the_day_itself():
    assert period_start(THURSDAY, "daily") == THURSDAY


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2026, 9, 17), date(2026, 9, 1)),
        (date(2026, 9, 1), date(2026, 9, 1)),
        (date(2026, 12, 31), date(2026, 12, 1)),
    ],
)
def test_monthly_period_start_is_the_first(day, expected):
    assert period_start(day, "monthly") == expected


def test_an_unknown_cadence_lists_the_real_ones():
    with pytest.raises(ValueError) as caught:
        period_start(THURSDAY, "fortnightly")
    assert "fortnightly" in str(caught.value)
    for cadence in CADENCES:
        assert cadence in str(caught.value)


# --- period_bounds --------------------------------------------------------


def test_bounds_are_half_open_so_midnight_is_unambiguous():
    start, end = period_bounds(THIS_MONDAY, "weekly")
    assert start == date(2026, 9, 14)
    assert end == date(2026, 9, 21), (
        "exclusive: the next Monday belongs to the next week"
    )


def test_bounds_snap_a_mid_period_day_first():
    assert period_bounds(THURSDAY, "weekly") == period_bounds(THIS_MONDAY, "weekly")


@pytest.mark.parametrize(
    "start,expected_end",
    [
        (date(2026, 1, 1), date(2026, 2, 1)),
        (date(2026, 2, 1), date(2026, 3, 1)),  # a short month
        (date(2026, 12, 1), date(2027, 1, 1)),  # across the year
    ],
)
def test_monthly_bounds_cross_month_and_year_correctly(start, expected_end):
    assert period_bounds(start, "monthly") == (start, expected_end)


def test_daily_bounds_are_one_day():
    assert period_bounds(THURSDAY, "daily") == (THURSDAY, date(2026, 9, 18))


# --- the plan -------------------------------------------------------------


def test_the_current_period_is_excluded_because_it_is_not_missing():
    """It is in progress and belongs to the live schedule, not to a backfill."""
    plan = plan_backfill(since=date(2026, 8, 31), today=THURSDAY)
    assert THIS_MONDAY not in plan.periods
    assert plan.until == LAST_MONDAY


def test_on_the_first_day_of_a_period_the_previous_one_is_the_last_complete():
    """Monday morning: last week is the most recent finished week."""
    plan = plan_backfill(since=date(2026, 8, 31), today=THIS_MONDAY)
    assert plan.until == LAST_MONDAY


def test_periods_come_back_oldest_first():
    """A run that dies halfway then leaves a contiguous finished prefix."""
    plan = plan_backfill(since=date(2026, 8, 10), today=THURSDAY)
    assert list(plan.periods) == sorted(plan.periods)
    assert plan.periods[0] == date(2026, 8, 10)
    assert plan.periods[-1] == LAST_MONDAY


def test_since_is_snapped_down_to_its_period():
    plan = plan_backfill(since=date(2026, 8, 12), today=THURSDAY)  # a Wednesday
    assert plan.since == date(2026, 8, 10)
    assert plan.periods[0] == date(2026, 8, 10)


def test_an_explicit_until_is_honoured_and_snapped():
    plan = plan_backfill(
        since=date(2026, 8, 3), until=date(2026, 8, 19), today=THURSDAY
    )
    assert plan.until == date(2026, 8, 17)
    assert plan.periods[-1] == date(2026, 8, 17)


def test_a_single_period_range_is_one_period():
    plan = plan_backfill(since=LAST_MONDAY, until=LAST_MONDAY, today=THURSDAY)
    assert plan.periods == (LAST_MONDAY,)


# --- what is already done -------------------------------------------------


def test_finished_periods_are_skipped_and_reported_separately():
    """'Nothing to do' and 'all already done' must be distinguishable."""
    plan = plan_backfill(
        since=date(2026, 8, 24),
        today=THURSDAY,
        done=[date(2026, 8, 31), date(2026, 9, 7)],
    )
    assert plan.periods == (date(2026, 8, 24),)
    assert plan.already_done == (date(2026, 8, 31), date(2026, 9, 7))


def test_everything_already_done_is_falsey_but_still_reports_what_it_found():
    plan = plan_backfill(
        since=date(2026, 8, 31), today=THURSDAY, done=[date(2026, 8, 31), LAST_MONDAY]
    )
    assert not plan
    assert plan.periods == ()
    assert len(plan.already_done) == 2, "distinguishable from an empty range"


def test_a_done_entry_may_be_any_day_in_its_period():
    """A caller holding a timestamp should not have to snap it first."""
    plan = plan_backfill(since=LAST_MONDAY, today=THURSDAY, done=[date(2026, 9, 9)])
    assert plan.periods == ()
    assert plan.already_done == (LAST_MONDAY,)


def test_done_entries_outside_the_range_are_ignored():
    plan = plan_backfill(since=LAST_MONDAY, today=THURSDAY, done=[date(2020, 1, 6)])
    assert plan.periods == (LAST_MONDAY,)
    assert plan.already_done == ()


# --- the guard ------------------------------------------------------------


def test_the_cap_keeps_the_most_recent_periods_and_says_how_many_it_dropped():
    """A tripped guard usually means a mistyped year; recent is the useful end."""
    plan = plan_backfill(since=date(2026, 1, 5), today=THURSDAY, max_periods=3)
    assert len(plan.periods) == 3
    assert plan.periods[-1] == LAST_MONDAY, "the recent end is kept"
    assert plan.capped > 0
    assert plan.capped + 3 == 36, "36 complete weeks between 5 January and 7 September"


def test_nothing_is_capped_when_the_range_fits():
    plan = plan_backfill(since=date(2026, 8, 31), today=THURSDAY, max_periods=52)
    assert plan.capped == 0


def test_the_cap_counts_only_work_not_periods_already_done():
    plan = plan_backfill(
        since=date(2026, 8, 17),
        today=THURSDAY,
        done=[date(2026, 8, 17), date(2026, 8, 24)],
        max_periods=2,
    )
    assert plan.capped == 0, "two remain after skipping two, which fits"
    assert len(plan.periods) == 2


@pytest.mark.parametrize("bad", [0, -1])
def test_a_nonsense_cap_is_refused(bad):
    with pytest.raises(ValueError, match="max_periods"):
        plan_backfill(since=date(2026, 8, 31), today=THURSDAY, max_periods=bad)


def test_an_inverted_range_is_refused_with_both_ends_named():
    with pytest.raises(ValueError) as caught:
        plan_backfill(since=date(2026, 9, 7), until=date(2026, 8, 3), today=THURSDAY)
    assert "2026-09-07" in str(caught.value)
    assert "2026-08-03" in str(caught.value)


def test_a_since_in_the_current_period_is_refused_rather_than_returning_nothing():
    """There is genuinely no complete period to fill, and silence would hide that."""
    with pytest.raises(ValueError):
        plan_backfill(since=THURSDAY, today=THURSDAY)


# --- determinism and shape ------------------------------------------------


def test_the_same_arguments_always_give_the_same_plan():
    """No clock read anywhere, so this holds whenever it is asked."""
    args = dict(since=date(2026, 8, 10), today=THURSDAY, done=[date(2026, 8, 24)])
    assert plan_backfill(**args) == plan_backfill(**args)


def test_the_module_reads_no_clock():
    """Determinism is the promise; a clock read anywhere would break it.

    Matched as calls, not substrings: `datetime.timedelta` contains the text
    `time.time`, so a naive substring check reports the module for using a
    timedelta. That false positive is exactly the kind that gets a check
    deleted rather than fixed.
    """
    import inspect
    import re

    from brief.lib import period_backfill_plan as mod

    source = inspect.getsource(mod)
    clock_calls = [
        r"\bdatetime\.date\.today\s*\(",
        r"\bdatetime\.datetime\.now\s*\(",
        r"\bdatetime\.datetime\.utcnow\s*\(",
        r"(?<!date)\btime\.time\s*\(",
        r"\btime\.monotonic\s*\(",
    ]
    for pattern in clock_calls:
        assert not re.search(pattern, source), f"{pattern} would break determinism"


def test_a_plan_is_truthy_when_there_is_work():
    assert plan_backfill(since=LAST_MONDAY, today=THURSDAY)
    assert not plan_backfill(since=LAST_MONDAY, today=THURSDAY, done=[LAST_MONDAY])


def test_a_plan_reports_its_own_length():
    plan = plan_backfill(since=date(2026, 8, 24), today=THURSDAY)
    assert len(plan) == len(plan.periods) == 3


def test_the_plan_carries_the_resolved_range_and_cadence():
    plan = plan_backfill(since=date(2026, 8, 12), cadence="weekly", today=THURSDAY)
    assert isinstance(plan, BackfillPlan)
    assert plan.cadence == "weekly"
    assert plan.since == date(2026, 8, 10)
    assert plan.until == LAST_MONDAY


def test_monthly_cadence_plans_whole_months():
    plan = plan_backfill(since=date(2026, 6, 15), cadence="monthly", today=THURSDAY)
    assert plan.periods == (date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1))
    assert date(2026, 9, 1) not in plan.periods, "September is still in progress"


def test_daily_cadence_plans_whole_days():
    plan = plan_backfill(since=date(2026, 9, 14), cadence="daily", today=THURSDAY)
    assert plan.periods == (date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16))
    assert THURSDAY not in plan.periods, "today is still in progress"
