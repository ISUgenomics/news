# Seed boundary: ascii-timeseries

## Purpose
Turn dated records into "how many per day/week/month, per series", and render that as text — with empty buckets still present, which is the one thing the obvious implementation gets wrong.

## when_to_use (draft for FEATURE.toml)
You have dated records and want to see the shape of them over time without a plotting dependency or a browser. Reach for it especially when a gap matters: a `Counter` over the dates you have cannot show a week in which nothing happened.

## Inputs
- records + `date_of` / `series_of` callables — the module never knows what a record is
- by: "day" | "week" | "month"
- start / end for `bucket_range`, width and label_width for rendering

## Outputs
- `bucket_of(value, by) -> str | None`
- `bucket_range(start, end, by) -> list[str]` — inclusive, contiguous, gaps included
- `tally(...) -> ({series: {bucket: count}}, unparsed)`
- `sparkline(counts, peak=None) -> str`
- `render_bars(counts, buckets, ...) -> str`

## Must NOT know about
- records, sources, profiles, the database, the app's types
- the clock — nothing calls `now()` or `today()`, so a chart is reproducible and testable
- colour, terminal width detection, or whether a terminal is attached

## Deliberate decisions

**The axis comes from the range, not the data.** This is the module's reason
to exist. Group by the dates present and an empty week does not appear; the
chart closes the gap and a collapse in activity renders as a smooth line.

**Weeks are ISO weeks, Monday-start.** Not "seven days back from today",
which buckets the same record differently depending on when you run it and
makes two runs incomparable.

**All series share one scale.** Left to scale itself, a row peaking at 3 and
a row peaking at 300 both render as a full block — the opposite of what a
chart is for. `sparkline` therefore takes an optional `peak`.

**A count of 1 is never rounded away.** Non-zero maps to at least one eighth
of a block, because "rare" and "never" are different answers.

**Unparsed dates are returned as a count, not dropped or logged.** A caller
that does not know how many dates it failed to read cannot tell a quiet
period from a broken parser.

## Dependencies
- **stdlib only**

## Public API
```python
BUCKETS: tuple[str, ...]
BLOCKS: str

def bucket_of(value: Any, *, by: str = "week") -> str | None
def bucket_range(start: Any, end: Any, *, by: str = "week") -> list[str]
def tally(records, *, date_of, series_of=None, by="week") -> tuple[dict, int]
def sparkline(counts: Sequence[int], *, peak: int | None = None) -> str
def render_bars(counts, buckets, *, width=40, label_width=22) -> str
```

## Prior art
verdict: seed

Nothing in codeLibrary renders a series as text. The alternative in this
repo would be a plotting dependency for a CLI that deliberately has no web
UI, which CLAUDE.md 8 would require justifying — and would still not fix the
empty-bucket problem, which is in the bucketing rather than the drawing.

## Test harness
None needed: pure functions, no I/O, no clock.
