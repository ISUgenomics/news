"""The scan itself: text, one file, or a whole tree.

Scanning never renders and never prints. It returns a `ScanResult` whose types
cannot hold the matched text (see `models.py`), so the value is structurally
incapable of escaping. `report.py` turns a result into something readable.

No runtime dependencies.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

from .models import (
    SKIP_BINARY,
    SKIP_OVERSIZE_FILE,
    SKIP_OVERSIZE_LINE,
    SKIP_UNREADABLE,
    FlaggedFile,
    Finding,
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

DEFAULT_PREFIX_CHARS = 6
DEFAULT_MAX_LINE_CHARS = 100_000
DEFAULT_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class ScanConfig:
    """Every knob, in one place, so callers thread one object instead of ten.

    Attributes:
        patterns: name -> compiled regex. Replace wholesale to narrow a scan;
            each match becomes a finding named by its key.
        benign: placeholder classifier, or None to classify nothing as benign.
            Alternatives should be named groups — the group name is reported
            in place of the matched text.
        benign_context_chars: how far either side of a match to look for a
            placeholder marker. ``None`` means the whole line, which is what a
            line-oriented pre-commit hook does; on long lines (JSONL, minified
            JS) that suppresses nearly everything, hence the bounded default.
        prefix_chars: how many leading characters of a match appear in the
            fingerprint. For provider-prefixed keys these are the provider tag
            and carry no entropy; lower it to 0 if you are paranoid.
        max_line_chars: lines longer than this are skipped and *recorded*.
        max_file_bytes: files larger than this are skipped and recorded.
            ``None`` disables the check.
        skip_dirs: directory names never descended into during a tree walk.
        exclude: fnmatch globs tested against both the full path and the
            basename of every candidate file.
        follow_symlinks: off by default — a symlinked tree is someone else's
            data and an easy way to walk out of the directory you meant.
        flag_sensitive_names: report .env / id_rsa / *.pem by name alone.
        correlation_salt: mixed into every digest. Leave ``None`` and one
            random salt is generated per top-level scan, so equal digests mean
            "same secret, same report" and nothing more. Set it explicitly for
            reproducible output (tests, fixtures) and understand that a fixed
            salt makes low-entropy values dictionary-attackable.
    """

    # default_factory, not a plain default: Python 3.11's dataclasses reject any
    # default whose *class* declares no __hash__, and mappingproxy is exactly
    # that on 3.11. A bare `= DEFAULT_PATTERNS` raises ValueError at import time
    # on 3.11 while working on 3.10 and on 3.12+, which is the worst shape of
    # portability bug — invisible on the interpreter you happen to develop on.
    patterns: Mapping[str, re.Pattern[str]] = field(
        default_factory=lambda: DEFAULT_PATTERNS
    )
    benign: re.Pattern[str] | None = DEFAULT_BENIGN
    benign_context_chars: int | None = 40
    prefix_chars: int = DEFAULT_PREFIX_CHARS
    max_line_chars: int = DEFAULT_MAX_LINE_CHARS
    max_file_bytes: int | None = None
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
    exclude: Sequence[str] = ()
    follow_symlinks: bool = False
    flag_sensitive_names: bool = True
    correlation_salt: bytes | None = None

    def resolved(self) -> ScanConfig:
        """Return a copy guaranteed to have a concrete correlation salt."""
        if self.correlation_salt is not None:
            return self
        return replace(self, correlation_salt=secrets.token_bytes(16))


# ---------------------------------------------------------------------------
# Redaction primitives
# ---------------------------------------------------------------------------

def fingerprint(value: str, *, prefix_chars: int = DEFAULT_PREFIX_CHARS) -> str:
    """Enough to locate a secret in a file, not enough to use it.

    ``"sk-ant... (len 51)"``. The length is included because it is the single
    most useful triage signal after the provider tag — it separates a real
    64-character token from a 17-character placeholder at a glance.

    Note the *trailing* characters are deliberately absent. Prefix plus length
    plus suffix narrows a value considerably more than prefix plus length.
    """
    stripped = value.strip()
    return f"{stripped[:prefix_chars]}... (len {len(stripped)})"


def digest(value: str, *, salt: bytes, length: int = 8) -> str:
    """A salted, truncated hash used only to say "this is the same secret".

    Salting is not decoration. An unsalted 8-hex-character digest of a
    low-entropy value — a password field holding ``hunter2hunter2hunter2`` —
    falls to a dictionary in seconds. With a per-scan random salt the digest
    correlates occurrences *within one report* and is worthless outside it.

    (Note the phrasing: writing that example as an assignment would make this
    docstring match the ``generic-assign`` pattern. Scanners scan themselves.)
    """
    return hashlib.sha256(salt + value.strip().encode("utf-8", "replace")).hexdigest()[
        :length
    ]


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def _classify_benign(
    line: str,
    start: int,
    end: int,
    config: ScanConfig,
) -> tuple[bool, str]:
    """Decide whether a match at ``line[start:end]`` looks like a placeholder.

    Returns (is_benign, marker_label). The label is a regex group name, never
    matched text — placeholder markers frequently sit *inside* the fake value.
    """
    if config.benign is None:
        return False, ""
    if config.benign_context_chars is None:
        context = line
    else:
        pad = config.benign_context_chars
        context = line[max(0, start - pad):end + pad]
    match = config.benign.search(context)
    if match is None:
        return False, ""
    return True, match.lastgroup or "benign"


def scan_text(
    text: str,
    *,
    path: str = "<text>",
    config: ScanConfig | None = None,
) -> ScanResult:
    """Scan an in-memory string. Useful before writing a generated artifact.

    ``path`` is a label only; nothing is opened.
    """
    config = (config or ScanConfig()).resolved()
    assert config.correlation_salt is not None  # resolved() guarantees it
    result = ScanResult(files_scanned=1)
    _scan_lines(text.splitlines(), path, config, result)
    return result


def _scan_lines(
    lines: Iterable[str],
    path: str,
    config: ScanConfig,
    result: ScanResult,
) -> None:
    """Core loop. ``hit`` below is the only place the secret exists."""
    salt = config.correlation_salt or b""
    for number, line in enumerate(lines, 1):
        if len(line) > config.max_line_chars:
            # Recorded, not swallowed. A silently skipped line is a blind spot
            # that reads as "clean".
            result.skipped.append(Skipped(
                path=path,
                reason=SKIP_OVERSIZE_LINE,
                detail=f"{len(line)} chars > max_line_chars={config.max_line_chars}",
                line=number,
            ))
            continue
        for name, pattern in config.patterns.items():
            for match in pattern.finditer(line):
                hit = match.group(0)
                benign, marker = _classify_benign(
                    line, match.start(), match.end(), config
                )
                result.findings.append(Finding(
                    path=path,
                    line=number,
                    column=match.start() + 1,
                    pattern=name,
                    fingerprint=fingerprint(hit, prefix_chars=config.prefix_chars),
                    digest=digest(hit, salt=salt),
                    benign=benign,
                    benign_marker=marker,
                ))
                del hit  # the value does not outlive this iteration


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def looks_binary(sample: bytes) -> bool:
    """git's heuristic, roughly: a NUL byte, or a lot of control bytes."""
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    text_bytes = bytes(range(0x20, 0x7F)) + b"\n\r\t\f\b\x1b"
    control = sum(1 for byte in sample if byte < 0x80 and byte not in text_bytes)
    return control / len(sample) > 0.30


def sensitive_name_reason(path: Path) -> str:
    """Why this filename is a finding on its own, or "" if it is not."""
    if path.name in SENSITIVE_BASENAMES:
        return f"sensitive filename: {path.name}"
    for suffix in SENSITIVE_SUFFIXES:
        if path.name.endswith(suffix):
            return f"sensitive extension: {suffix}"
    return ""


def scan_file(
    path: str | os.PathLike[str],
    *,
    config: ScanConfig | None = None,
    display_path: str | None = None,
) -> ScanResult:
    """Scan one file. Never raises for an unreadable or binary file.

    Anything that stops the scan becomes a `Skipped` entry, because the caller
    needs to know the difference between "looked and found nothing" and "never
    looked".
    """
    config = (config or ScanConfig()).resolved()
    target = Path(path)
    label = display_path if display_path is not None else str(target)
    result = ScanResult()

    if config.flag_sensitive_names:
        reason = sensitive_name_reason(target)
        if reason:
            result.flagged_files.append(FlaggedFile(path=label, reason=reason))

    try:
        size = target.stat().st_size
        if config.max_file_bytes is not None and size > config.max_file_bytes:
            result.skipped.append(Skipped(
                path=label,
                reason=SKIP_OVERSIZE_FILE,
                detail=f"{size} bytes > max_file_bytes={config.max_file_bytes}",
            ))
            return result
        with target.open("rb") as handle:
            sample = handle.read(DEFAULT_SNIFF_BYTES)
        if looks_binary(sample):
            result.skipped.append(Skipped(
                path=label, reason=SKIP_BINARY, detail="binary content sniffed",
            ))
            return result
        with target.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            result.files_scanned = 1
            _scan_lines((line.rstrip("\r\n") for line in handle), label, config, result)
    except (OSError, ValueError) as exc:
        # OSError covers permissions, dangling symlinks, vanished files and
        # device nodes; ValueError covers a handle closed underneath us.
        result.skipped.append(Skipped(
            path=label, reason=SKIP_UNREADABLE, detail=type(exc).__name__,
        ))
    return result


def _excluded(path: Path, root: Path, config: ScanConfig) -> bool:
    if not config.exclude:
        return False
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.as_posix()
    for glob in config.exclude:
        if fnmatch.fnmatch(relative, glob) or fnmatch.fnmatch(path.name, glob):
            return True
    return False


def iter_files(
    root: str | os.PathLike[str],
    *,
    config: ScanConfig | None = None,
) -> Iterator[Path]:
    """Yield scannable files under ``root`` in a stable, sorted order.

    Sorted because a scan report that reorders itself between runs cannot be
    diffed, and diffing two reports is how you check that a cleanup worked.
    """
    config = config or ScanConfig()
    base = Path(root)
    if base.is_file():
        yield base
        return
    for dirpath, dirnames, filenames in os.walk(
        base, followlinks=config.follow_symlinks
    ):
        dirnames[:] = sorted(d for d in dirnames if d not in config.skip_dirs)
        here = Path(dirpath)
        for name in sorted(filenames):
            candidate = here / name
            if not config.follow_symlinks and candidate.is_symlink():
                continue
            if _excluded(candidate, base, config):
                continue
            yield candidate


def scan_path(
    root: str | os.PathLike[str],
    *,
    config: ScanConfig | None = None,
    relative: bool = False,
) -> ScanResult:
    """Scan a file or a directory tree.

    Args:
        root: file or directory.
        config: see `ScanConfig`.
        relative: report paths relative to ``root`` rather than as given.
            Absolute paths in a shared report leak usernames and layout.
    """
    config = (config or ScanConfig()).resolved()
    base = Path(root)
    combined = ScanResult()
    if not base.exists():
        combined.skipped.append(Skipped(
            path=str(base), reason=SKIP_UNREADABLE, detail="does not exist",
        ))
        return combined
    # relative_to(itself) is ".", so a single-file root would otherwise report
    # every finding at ".:12:5". The filename is the useful label there.
    anchor = base.parent if base.is_file() else base
    for candidate in iter_files(base, config=config):
        label: str | None = None
        if relative:
            try:
                label = candidate.relative_to(anchor).as_posix()
            except ValueError:
                label = candidate.as_posix()
        combined.extend(scan_file(candidate, config=config, display_path=label))
    return combined


def scan_paths(
    roots: Iterable[str | os.PathLike[str]],
    *,
    config: ScanConfig | None = None,
) -> ScanResult:
    """Scan several roots under one shared correlation salt.

    The shared salt is the reason this exists rather than a list comprehension:
    it is what lets the report say "the same key appears in these four files".
    """
    config = (config or ScanConfig()).resolved()
    combined = ScanResult()
    for root in roots:
        combined.extend(scan_path(root, config=config))
    return combined
