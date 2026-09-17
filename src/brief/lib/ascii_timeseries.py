"""ascii_timeseries — counts over time, as text, with the gaps left in.

Turning dated records into "how many per week" looks like a one-liner with
``Counter`` and is not, because of one thing: **a bucket with no records must
still appear**. Group by the dates you have and an empty week silently does
not exist — the chart closes the gap and a collapse in activity renders as a
smooth line. That is a chart that lies, which is worse than no chart, and it
is the reason this module exists rather than three lines at a call site.

So the bucket axis is built from the RANGE, not from the data, and every
series is rendered against the same axis.

What it does: bucket ISO dates by day, week or month; tally records into
``{series: {bucket: count}}``; render that as aligned bars or a sparkline.
Pure — no I/O, no clock, no colour, no terminal detection. The caller says
how wide.

Contract:
  - Weeks are ISO weeks, labelled ``2026-W38``, and start on Monday. Not
    "seven days back from today", which makes two runs incomparable.
  - A date that cannot be parsed is counted in ``unparsed`` and never guessed
    at. Silently dropping it would overstate a quiet period.
  - ``bucket_range`` is inclusive of both ends.
  - Bars scale to the largest count in the whole tally, so two series are
    comparable by eye. A caller wanting per-series scaling renders twice.
  - A count of zero renders as an empty cell, not a missing row.

Deliberately not here: what a record is, where dates come from, colour,
terminal width detection, cumulative or smoothed series, any notion of a
"source" or a "profile".

Dependencies: stdlib only.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable

__all__ = [
    "BUCKETS",
    "BLOCKS",
    "bucket_of",
    "bucket_range",
    "tally",
    "render_bars",
    "sparkline",
]

BUCKETS = ("day", "week", "month")

#: Eighth-blocks, for sparklines. Index 0 is reserved for "zero".
BLOCKS = " ▁▂▃▄▅▆▇█"


def _parse(value: Any) -> datetime.date | None:
    text = str(value or "")[:10]
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def bucket_of(value: Any, *, by: str = "week") -> str | None:
    """The bucket an ISO date falls in, or None if it cannot be parsed.

    Weeks are ISO weeks (Monday-start, ``2026-W38``) so that two runs a month
    apart bucket the same record identically. A rolling "last 7 days" window
    would not.
    """
    if by not in BUCKETS:
        raise ValueError(f"by must be one of {list(BUCKETS)}, got {by!r}")
    date = _parse(value)
    if date is None:
        return None
    if by == "day":
        return date.isoformat()
    if by == "month":
        return f"{date.year:04d}-{date.month:02d}"
    year, week, _weekday = date.isocalendar()
    return f"{year:04d}-W{week:02d}"


def bucket_range(start: Any, end: Any, *, by: str = "week") -> list[str]:
    """Every bucket from ``start`` to ``end`` inclusive, including empty ones.

    This is the whole point of the module. Build the axis from the data and a
    week in which nothing happened disappears, which turns a collapse into a
    smooth line.
    """
    if by not in BUCKETS:
        raise ValueError(f"by must be one of {list(BUCKETS)}, got {by!r}")
    first, last = _parse(start), _parse(end)
    if first is None or last is None:
        raise ValueError(f"start and end must be ISO dates, got {start!r} and {end!r}")
    if first > last:
        first, last = last, first

    step = {"day": datetime.timedelta(days=1), "week": datetime.timedelta(days=7)}.get(by)
    out: list[str] = []
    seen: set[str] = set()
    if step is not None:
        cursor = first
        while cursor <= last:
            key = bucket_of(cursor, by=by)
            if key and key not in seen:
                seen.add(key)
                out.append(key)
            cursor += step
        # A range shorter than one step still has a bucket.
        tail = bucket_of(last, by=by)
        if tail and tail not in seen:
            out.append(tail)
        return out

    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def tally(
    records: Iterable[Any],
    *,
    date_of: Callable[[Any], Any],
    series_of: Callable[[Any], str] | None = None,
    by: str = "week",
) -> tuple[dict[str, dict[str, int]], int]:
    """``({series: {bucket: count}}, unparsed)``.

    ``unparsed`` is returned rather than logged or dropped: a caller that
    does not know how many dates it failed to read cannot tell a quiet period
    from a broken parser.
    """
    counts: dict[str, dict[str, int]] = {}
    unparsed = 0
    for record in records:
        bucket = bucket_of(date_of(record), by=by)
        if bucket is None:
            unparsed += 1
            continue
        name = series_of(record) if series_of else "all"
        counts.setdefault(str(name), {}).setdefault(bucket, 0)
        counts[str(name)][bucket] += 1
    return counts, unparsed


def sparkline(counts: Sequence[int], *, peak: int | None = None) -> str:
    """One character per bucket. Zero is a space, so a gap reads as a gap.

    ``peak`` sets the scale. Passing it is what lets several series be
    compared by eye: left to scale itself, a row peaking at 3 and a row
    peaking at 300 both render as a full block, which is the opposite of
    what a chart is for.
    """
    values = [max(0, int(c)) for c in counts]
    peak = max(values, default=0) if peak is None else max(0, int(peak))
    if peak == 0:
        return " " * len(values)
    out = []
    for value in values:
        if value == 0:
            out.append(BLOCKS[0])
        else:
            # 1..8, so any non-zero count is visible.
            step = 1 + round((value / peak) * (len(BLOCKS) - 2))
            out.append(BLOCKS[min(step, len(BLOCKS) - 1)])
    return "".join(out)


def render_bars(
    counts: Mapping[str, Mapping[str, int]],
    buckets: Sequence[str],
    *,
    width: int = 40,
    label_width: int = 22,
) -> str:
    """Aligned bars, one row per series, one column block per bucket.

    Scaled to the largest count across every series, so two rows can be
    compared by eye rather than only within themselves.
    """
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")
    peak = max((n for row in counts.values() for n in row.values()), default=0)
    lines: list[str] = []
    for name in sorted(counts):
        row = counts[name]
        total = sum(row.values())
        series = [row.get(b, 0) for b in buckets]
        bar = sparkline(series, peak=peak)
        label = name if len(name) <= label_width else name[: label_width - 1] + "…"
        lines.append(f"{label:<{label_width}} {bar}  {total:>6,}")
    if not lines:
        return "(no data)"
    first, last = (buckets[0], buckets[-1]) if buckets else ("", "")
    footer = f"{'':<{label_width}} {first} → {last}   peak {peak:,}/bucket"
    return "\n".join(lines + ["", footer])
