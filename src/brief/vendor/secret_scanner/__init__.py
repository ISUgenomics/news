"""Scan files or trees for credential-shaped strings, reporting location only.

The matched value is never stored in a result type and never rendered. What you
get is file:line:column, a pattern name, a truncated fingerprint, and a salted
digest for correlating repeats.
"""

from .models import (
    SKIP_BINARY,
    SKIP_OVERSIZE_FILE,
    SKIP_OVERSIZE_LINE,
    SKIP_UNREADABLE,
    Finding,
    FlaggedFile,
    ScanResult,
    Skipped,
)
from .patterns import (
    DEFAULT_BENIGN,
    DEFAULT_PATTERNS,
    DEFAULT_SKIP_DIRS,
    SENSITIVE_BASENAMES,
    SENSITIVE_SUFFIXES,
)
from .report import DEFAULT_MAX_LOCATIONS, format_finding, render_report, to_jsonable
from .scanner import (
    ScanConfig,
    digest,
    fingerprint,
    iter_files,
    looks_binary,
    scan_file,
    scan_path,
    scan_paths,
    scan_text,
    sensitive_name_reason,
)

__all__ = [
    "DEFAULT_BENIGN",
    "DEFAULT_MAX_LOCATIONS",
    "DEFAULT_PATTERNS",
    "DEFAULT_SKIP_DIRS",
    "Finding",
    "FlaggedFile",
    "SENSITIVE_BASENAMES",
    "SENSITIVE_SUFFIXES",
    "SKIP_BINARY",
    "SKIP_OVERSIZE_FILE",
    "SKIP_OVERSIZE_LINE",
    "SKIP_UNREADABLE",
    "ScanConfig",
    "ScanResult",
    "Skipped",
    "digest",
    "fingerprint",
    "format_finding",
    "iter_files",
    "looks_binary",
    "render_report",
    "scan_file",
    "scan_path",
    "scan_paths",
    "scan_text",
    "sensitive_name_reason",
    "to_jsonable",
]
