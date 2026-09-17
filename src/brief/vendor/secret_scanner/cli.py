"""Command-line front end. Thin on purpose — all of it is argument parsing.

    python -m secret_scanner ./dist ./docs
    python -m secret_scanner --json . > audit.json
    python -m secret_scanner --strict .        # skips count as failure

Exit codes are the contract:
    0  nothing needing a human
    1  real findings (or, with --strict, skips too)
    2  usage error
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .patterns import DEFAULT_PATTERNS
from .report import DEFAULT_MAX_LOCATIONS, render_report, to_jsonable
from .scanner import ScanConfig, scan_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secret-scanner",
        description="Report credential-shaped strings by location, never by value.",
    )
    parser.add_argument("paths", nargs="+", help="files or directories to scan")
    parser.add_argument(
        "--json", action="store_true",
        help="emit machine-readable JSON instead of a text report",
    )
    parser.add_argument(
        "--max-locations", type=int, default=DEFAULT_MAX_LOCATIONS,
        help=f"cap printed locations (default {DEFAULT_MAX_LOCATIONS})",
    )
    parser.add_argument(
        "--no-benign", action="store_true",
        help="omit the placeholder section from the text report",
    )
    parser.add_argument(
        "--pattern", action="append", metavar="NAME", default=None,
        help=(
            "restrict to a named pattern; repeatable. "
            f"available: {', '.join(sorted(DEFAULT_PATTERNS))}"
        ),
    )
    parser.add_argument(
        "--exclude", action="append", metavar="GLOB", default=[],
        help="skip paths matching this glob; repeatable",
    )
    parser.add_argument(
        "--prefix-chars", type=int, default=6,
        help="leading characters shown in a fingerprint (default 6, 0 for none)",
    )
    parser.add_argument(
        "--max-line-chars", type=int, default=100_000,
        help="skip and record lines longer than this (default 100000)",
    )
    parser.add_argument(
        "--whole-line-benign", action="store_true",
        help="look for placeholder markers anywhere on the line, not just nearby",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="treat skipped files/lines as failure too",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    patterns = DEFAULT_PATTERNS
    if args.pattern:
        unknown = [name for name in args.pattern if name not in DEFAULT_PATTERNS]
        if unknown:
            print(f"unknown pattern(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        patterns = {name: DEFAULT_PATTERNS[name] for name in args.pattern}

    config = ScanConfig(
        patterns=patterns,
        benign_context_chars=None if args.whole_line_benign else 40,
        prefix_chars=max(0, args.prefix_chars),
        max_line_chars=args.max_line_chars,
        exclude=tuple(args.exclude),
    )

    result = scan_paths(args.paths, config=config)

    if args.json:
        print(json.dumps(to_jsonable(result), indent=2))
    else:
        print(render_report(
            result,
            max_locations=args.max_locations,
            show_benign=not args.no_benign,
        ))

    if result.real:
        return 1
    if args.strict and result.skipped:
        return 1
    return 0
