"""Rendering, kept strictly separate from scanning.

Two reasons for the split. The obvious one: a library caller wants structured
findings, a CLI caller wants text. The load-bearing one: every function here
takes a `ScanResult`, and a `ScanResult` cannot hold a secret — so there is no
rendering path that could print one, however the report is later reshaped.

No runtime dependencies.
"""

from __future__ import annotations

from dataclasses import asdict

from .models import Finding, ScanResult

DEFAULT_MAX_LOCATIONS = 40


def format_finding(finding: Finding, *, width: int = 16) -> str:
    """One report line. Location, pattern, redacted fingerprint, digest."""
    tag = finding.pattern.ljust(width)
    suffix = f"  #{finding.digest}"
    if finding.benign:
        suffix += f"  [benign: {finding.benign_marker}]"
    return f"{tag} {finding.location}  {finding.fingerprint}{suffix}"


def to_jsonable(result: ScanResult) -> dict:
    """A plain dict for `json.dumps`. Contains no secret text, by construction.

    Everything here comes out of the redaction-safe dataclasses, so piping this
    into a CI artifact or an issue tracker is safe in a way that piping raw
    grep output never is.
    """
    return {
        "files_scanned": result.files_scanned,
        "counts": result.counts(),
        "benign_counts": _counter(f.pattern for f in result.benign),
        "findings": [asdict(f) for f in result.findings],
        "flagged_files": [asdict(f) for f in result.flagged_files],
        "skipped": [asdict(s) for s in result.skipped],
    }


def _counter(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def render_report(
    result: ScanResult,
    *,
    max_locations: int = DEFAULT_MAX_LOCATIONS,
    show_benign: bool = True,
) -> str:
    """Render a human-triage report.

    Args:
        result: what a scan returned.
        max_locations: cap on printed location lines. The count line always
            reports the true total, so a truncated report never understates
            the problem — the origin implementation truncated silently.
        show_benign: list benign hits too, after the real ones. They are worth
            seeing: a placeholder filter that is wrong is invisible otherwise.
    """
    lines: list[str] = []
    real = result.real
    benign = result.benign

    lines.append(
        f"scanned {result.files_scanned} file(s): "
        f"{len(real)} finding(s), {len(benign)} benign, "
        f"{len(result.flagged_files)} flagged file(s), "
        f"{len(result.skipped)} skip(s)"
    )

    counts = result.counts()
    lines.append("")
    lines.append("--- findings by pattern ---")
    if counts:
        for name, count in counts.items():
            lines.append(f"  {name.ljust(16)} {count}")
    else:
        lines.append("  none")

    if real:
        lines.append("")
        lines.append("--- locations (redacted) ---")
        for finding in real[:max_locations]:
            lines.append(f"  {format_finding(finding)}")
        hidden = len(real) - max_locations
        if hidden > 0:
            lines.append(f"  ... and {hidden} more (raise max_locations to see them)")

    if result.flagged_files:
        lines.append("")
        lines.append("--- flagged by filename ---")
        for flagged in result.flagged_files:
            lines.append(f"  {flagged.path}  ({flagged.reason})")

    if show_benign and benign:
        lines.append("")
        lines.append(f"--- benign / placeholder ({len(benign)}) ---")
        for finding in benign[:max_locations]:
            lines.append(f"  {format_finding(finding)}")
        hidden = len(benign) - max_locations
        if hidden > 0:
            lines.append(f"  ... and {hidden} more")

    if result.skipped:
        lines.append("")
        lines.append(f"--- not scanned ({len(result.skipped)}) ---")
        for skip in result.skipped[:max_locations]:
            where = skip.path if skip.line is None else f"{skip.path}:{skip.line}"
            detail = f" — {skip.detail}" if skip.detail else ""
            lines.append(f"  {skip.reason.ljust(16)} {where}{detail}")
        hidden = len(result.skipped) - max_locations
        if hidden > 0:
            lines.append(f"  ... and {hidden} more")

    return "\n".join(lines)
