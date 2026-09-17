"""Result types.

The central invariant of this feature lives here: **no type in this module has
a field that can hold the matched text.** A `Finding` carries a location, a
pattern name, a truncated fingerprint and a salted digest — never the value.
The value exists only as a local variable inside `scanner.scan_text`, and is
discarded when that loop body ends.

That is why rendering is a separate module: if the only way to see a report is
to serialize these dataclasses, there is no path by which a secret reaches a
terminal, a log, or a CI artifact.

Plain dataclasses, no runtime dependencies.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# Reasons a file or line was not scanned. Skips are *recorded*, never silent —
# an unexamined file that reports "clean" is worse than no scan at all.
SKIP_BINARY = "binary"
SKIP_UNREADABLE = "unreadable"
SKIP_OVERSIZE_FILE = "oversize-file"
SKIP_OVERSIZE_LINE = "oversize-line"


@dataclass(frozen=True)
class Finding:
    """One credential-shaped match. Never holds the matched text."""

    path: str
    line: int  # 1-based
    column: int  # 1-based, character offset within the line
    pattern: str  # key from the pattern table, e.g. "anthropic"
    fingerprint: str  # e.g. "sk-ant... (len 51)" — locates it, cannot use it
    digest: str  # salted sha256 prefix; equal digests == equal secrets
    benign: bool = False  # matched the placeholder filter
    benign_marker: str = ""  # which placeholder token fired, if any

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


@dataclass(frozen=True)
class Skipped:
    """Something the scan did not look at, and why."""

    path: str
    reason: str  # one of the SKIP_* constants
    detail: str = ""
    line: int | None = None  # set for SKIP_OVERSIZE_LINE


@dataclass(frozen=True)
class FlaggedFile:
    """A file flagged by name alone — .env, id_rsa, anything.pem."""

    path: str
    reason: str


@dataclass
class ScanResult:
    """Everything one scan learned. Rendering lives in ``report.py``."""

    findings: list[Finding] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    flagged_files: list[FlaggedFile] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def real(self) -> list[Finding]:
        """Findings that did not look like placeholders. Triage these."""
        return [f for f in self.findings if not f.benign]

    @property
    def benign(self) -> list[Finding]:
        """Findings suppressed as placeholders. Kept, not discarded."""
        return [f for f in self.findings if f.benign]

    @property
    def clean(self) -> bool:
        """True when nothing needs a human. Skips count against clean."""
        return not self.real and not self.flagged_files and not self.skipped

    def counts(self, *, include_benign: bool = False) -> dict[str, int]:
        """Findings per pattern name, highest first."""
        source = self.findings if include_benign else self.real
        counter: Counter[str] = Counter(f.pattern for f in source)
        return dict(counter.most_common())

    def extend(self, other: ScanResult) -> None:
        """Merge another result into this one, in place."""
        self.findings.extend(other.findings)
        self.skipped.extend(other.skipped)
        self.flagged_files.extend(other.flagged_files)
        self.files_scanned += other.files_scanned
