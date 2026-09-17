"""The pattern table — the part you are expected to edit.

Two tables, and the second matters as much as the first:

``DEFAULT_PATTERNS``
    High-confidence, provider-prefixed credential shapes. Deliberately **no
    generic entropy check**: entropy scoring drowns real findings in base64
    noise from test fixtures, minified assets, and content hashes.

``DEFAULT_BENIGN``
    Values that merely *look* like secrets — placeholders, docs, fixtures.
    Findings whose surrounding context matches this are classified benign and
    counted separately rather than dropped, so they neither hide a real key nor
    bury one. A scanner that cries wolf gets switched off, which is strictly
    worse than a narrower one that stays on.

Adding a provider is one line. Both tables are plain module constants; pass
your own to any scan function instead of editing these in place.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Mapping

# --------------------------------------------------------------------------
# Credential shapes. Ordered most specific first, purely for report readability
# — every pattern is tried against every line, so order does not affect which
# findings are produced.
# --------------------------------------------------------------------------
# Wrapped in a MappingProxyType: a module-level table that any caller could
# mutate is a footgun, and it also lets `ScanConfig` use it as a default.
DEFAULT_PATTERNS: Mapping[str, re.Pattern[str]] = MappingProxyType({
    "anthropic": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai-proj": re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
    "openai": re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    "github-pat": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"
    ),
    "gitlab-pat": re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16,}"),
    "google-api": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "slack": re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}"),
    "stripe-live": re.compile(r"\b(?:sk|pk|rk)_live_[A-Za-z0-9]{20,}"),
    "huggingface": re.compile(r"\bhf_[A-Za-z0-9]{34,}"),
    "npm-token": re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    "pypi-token": re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{20,}"),
    "private-key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY"
    ),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\."),
    # The only shape-based (non-provider) rule. It fires on assignment syntax,
    # not on entropy, which keeps the false-positive rate survivable. It also
    # overlaps the provider patterns on purpose — see README "Gotchas".
    "generic-assign": re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|"
        r"password|passwd|client[_-]?secret)\b\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9+/=_\-]{16,}"
    ),
})

# --------------------------------------------------------------------------
# Placeholder markers. Union of the two tables in the origin project, which had
# drifted apart (see the manifest findings). Every token here is a deliberate
# trade of recall for a report a human will actually read.
# --------------------------------------------------------------------------
# Every alternative is a *named group* so a report can say which marker fired
# without ever echoing the matched text. That matters: a placeholder token can
# legitimately sit inside the credential-shaped string itself (the whole point
# of `sk-ant-api03-EXAMPLE-KEY`), so printing the marker text would print part
# of the value. `match.lastgroup` gives a fixed label instead.
DEFAULT_BENIGN: re.Pattern[str] = re.compile(
    r"(?i)(?:"
    r"(?P<fake>FAKE)"
    r"|(?P<example>EXAMPLE)"
    r"|(?P<placeholder>PLACEHOLDER)"
    r"|(?P<redacted>REDACTED)"
    r"|(?P<sample>SAMPLE)"
    r"|(?P<dummy>DUMMY)"
    r"|(?P<replace_me>REPLACE[_-]?ME)"
    r"|(?P<change_me>CHANGE[_-]?ME)"
    r"|(?P<your_prefix>YOUR[_-])"
    r"|(?P<not_a_real>not-a-real)"
    r"|(?P<test_key>TEST[_-]?KEY)"
    r"|(?P<abc123>abc123)"
    r"|(?P<repeated_x>XXXX+)"
    # An angle-bracket token such as <your-key-here>. Bounded, unlike the
    # origin's `<[^>]+>`, which classified any line containing an HTML or XML
    # tag as benign.
    r"|(?P<angle_token><[A-Za-z0-9_.\- ]{1,40}>)"
    r")"
)

# Directories that are never worth scanning and are large enough to dominate a
# run. Not a security boundary — pass your own set if yours differ.
#
# Pruning is *silent*: a pruned directory produces no `Skipped` entry and does
# not count against `ScanResult.clean`. Recording one per pruned directory would
# make `clean` False for every repository with a `.git`, which would make the
# skip channel useless. That asymmetry is the reason this set is deliberately
# short: everything here is vendored or regenerable content you did not write.
#
# Build outputs (dist/, build/, target/, .next/) are pointedly NOT here. Bundlers
# inline environment variables, and "about to share a generated artifact" is the
# headline use case for this feature — pruning the artifact by default would
# answer the exact question the caller asked with a silent no.
DEFAULT_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".gradle", ".terraform",
})

# Files whose *name* is the finding — content is irrelevant. Reported
# separately from line findings because there is no line to point at.
SENSITIVE_BASENAMES: frozenset[str] = frozenset({
    ".env", ".env.local", ".env.production", ".env.staging",
    "secrets.json", "credentials.json", "service-account.json",
    ".npmrc", ".pypirc", ".netrc",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})

SENSITIVE_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".jks")
