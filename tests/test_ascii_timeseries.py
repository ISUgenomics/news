"""Tests for the ascii-timeseries seed.

Pure functions, so no harness. The test that matters is the first one: a
bucket with no records must still appear, because grouping by the dates you
have turns a collapse in activity into a smooth line.
"""

from __future__ import annotations

import pytest

from brief.lib import ascii_timeseries as ts


# ------------------------------------------------- the reason this exists ---


def test_an_empty_bucket_still_appears_in_the_range():
    """The whole point. Build the axis from the data and a quiet week does
    not exist; the chart closes the gap and lies about the trend."""
    axis = ts.bucket_range("2026-08-01", "2026-09-17", by="week")

    assert axis == [f"2026-W{n:02d}" for n in range(31, 39)]
    assert len(axis) == 8, "no week is skipped, however empty"


#: `render_bars` lays out ``{label:<label_width} {bar}  {total}``, so the bar
#: begins one space after the label field.
BAR_AT = 22 + 1


def _bar(line: str, width: int) -> str:
    return line[BAR_AT : BAR_AT + width]


def test_a_gap_renders_as_a_gap_not_a_join():
    counts = {"s": {"2026-W31": 5, "2026-W33": 5}}
    axis = ts.bucket_range("2026-08-01", "2026-08-17", by="week")

    # Not .split(): the bar CONTAINS a space, which is the thing under test.
    line = ts.render_bars(counts, axis).splitlines()[0]
    bar = _bar(line, len(axis))

    assert bar[axis.index("2026-W31")] != " ", "the first week has records"
    assert bar[axis.index("2026-W32")] == " ", "the empty week is blank, not closed over"
    assert bar[axis.index("2026-W33")] != " ", "and the third week has records again"


def test_zero_is_a_space_and_one_is_visible():
    """A count of 1 next to a peak of 1000 must not round to nothing —
    'rare' and 'never' are different answers."""
    assert ts.sparkline([0, 1, 1000])[0] == " "
    assert ts.sparkline([0, 1, 1000])[1] != " "


# -------------------------------------------------------------- bucketing ---


@pytest.mark.parametrize(
    "date,by,expected",
    [
        ("2026-09-17", "day", "2026-09-17"),
        ("2026-09-17", "week", "2026-W38"),
        ("2026-09-17", "month", "2026-09"),
        ("2026-09-17T19:27:34Z", "week", "2026-W38"),
    ],
)
def test_dates_bucket_as_documented(date, by, expected):
    assert ts.bucket_of(date, by=by) == expected


def test_weeks_are_iso_weeks_starting_monday():
    """Not 'seven days back from today', which makes two runs incomparable."""
    assert ts.bucket_of("2026-09-14", by="week") == "2026-W38"  # Monday
    assert ts.bucket_of("2026-09-20", by="week") == "2026-W38"  # Sunday
    assert ts.bucket_of("2026-09-21", by="week") == "2026-W39"  # next Monday


def test_a_month_range_crosses_the_year_boundary():
    assert ts.bucket_range("2025-11-15", "2026-02-02", by="month") == [
        "2025-11", "2025-12", "2026-01", "2026-02",
    ]


def test_a_range_inside_one_bucket_still_yields_that_bucket():
    assert ts.bucket_range("2026-09-15", "2026-09-16", by="week") == ["2026-W38"]


def test_a_reversed_range_is_ordered_rather_than_empty():
    assert ts.bucket_range("2026-09-17", "2026-08-01", by="week")[0] == "2026-W31"


@pytest.mark.parametrize("by", ["decade", "", "WEEK"])
def test_an_unknown_bucket_size_is_refused(by):
    with pytest.raises(ValueError):
        ts.bucket_of("2026-09-17", by=by)
    with pytest.raises(ValueError):
        ts.bucket_range("2026-01-01", "2026-02-01", by=by)


# ---------------------------------------------------------------- tallying ---


def test_unparsed_dates_are_counted_not_dropped():
    """A caller that does not know how many dates it failed to read cannot
    tell a quiet period from a broken parser."""
    records = [{"d": "2026-09-17"}, {"d": ""}, {"d": "not a date"}, {"d": None}]

    counts, unparsed = ts.tally(records, date_of=lambda r: r["d"], by="week")

    assert unparsed == 3
    assert counts == {"all": {"2026-W38": 1}}


def test_records_split_by_series():
    records = [
        {"d": "2026-09-17", "s": "a"},
        {"d": "2026-09-17", "s": "b"},
        {"d": "2026-09-16", "s": "a"},
    ]

    counts, _ = ts.tally(
        records, date_of=lambda r: r["d"], series_of=lambda r: r["s"], by="week"
    )

    assert counts == {"a": {"2026-W38": 2}, "b": {"2026-W38": 1}}


def test_an_empty_input_tallies_to_nothing_rather_than_raising():
    assert ts.tally([], date_of=lambda r: r) == ({}, 0)


# --------------------------------------------------------------- rendering ---


def test_series_share_one_scale_so_rows_are_comparable():
    """Scaled per row, a series peaking at 3 and one peaking at 300 would
    look identical, which is the opposite of what a chart is for."""
    counts = {"small": {"w": 3}, "large": {"w": 300}}

    out = ts.render_bars(counts, ["w"])
    small = next(line for line in out.splitlines() if line.startswith("small"))
    large = next(line for line in out.splitlines() if line.startswith("large"))

    assert _bar(small, 1) != _bar(large, 1), (
        f"both rows rendered as {_bar(small, 1)!r}; sparkline rescaled per "
        "row instead of using the shared peak"
    )
    assert _bar(large, 1) == "\u2588"


def test_the_footer_states_the_range_and_the_peak():
    out = ts.render_bars({"s": {"2026-W37": 4}}, ["2026-W37", "2026-W38"])
    assert "2026-W37 → 2026-W38" in out
    assert "peak 4" in out


def test_a_long_series_name_is_truncated_not_wrapped():
    out = ts.render_bars({"x" * 40: {"w": 1}}, ["w"], label_width=10)
    assert out.splitlines()[0].startswith("xxxxxxxxx…")


def test_no_data_says_so():
    assert ts.render_bars({}, ["w"]) == "(no data)"


def test_totals_count_every_bucket_not_just_the_visible_ones():
    counts = {"s": {"2026-W30": 100, "2026-W38": 1}}
    out = ts.render_bars(counts, ["2026-W38"])
    assert "101" in out, "the row total is the series, not the window"


@pytest.mark.parametrize("bad", [0, -1])
def test_a_nonsense_width_is_refused(bad):
    with pytest.raises(ValueError):
        ts.render_bars({"s": {"w": 1}}, ["w"], width=bad)


# -------------------------------------------------------------- boundaries ---


def test_the_module_imports_only_the_standard_library():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(ts.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                roots.add(node.module.split(".")[0])
    assert roots == {"__future__", "datetime", "collections", "typing"}


def test_the_seed_does_not_import_from_the_app():
    from pathlib import Path

    source = Path(ts.__file__).read_text(encoding="utf-8")
    assert "from brief" not in source and "import brief" not in source


def test_it_touches_no_clock():
    """A chart that depends on 'now' renders differently every run and
    cannot be regression-tested."""
    from pathlib import Path

    source = Path(ts.__file__).read_text(encoding="utf-8")
    assert "now(" not in source and "today(" not in source
