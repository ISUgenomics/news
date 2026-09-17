"""The rule table — what counts as credential-shaped.

The whole design decision of this feature lives in this file: **no entropy
heuristic**. A "this string looks random" check is the obvious approach and it
is wrong for the texts that actually get redacted — session logs, commit diffs,
crash dumps. Those are full of git SHAs, sha256 digests, UUIDs and base64
fixtures, all of which are high entropy and none of which are secrets. Mangling
them makes the log useless, and a redactor people turn off protects nothing.

So the table only contains two kinds of rule:

1. **High-confidence provider prefixes.** ``sk-ant-``, ``AKIA``, ``ghp_`` and
   friends. The prefix is the evidence; the entropy is irrelevant.
2. **Explicit assignments.** ``password = "..."`` — the *keyword* is the
   evidence. Prose that merely mentions a password has no ``=``.

Everything else is deliberately left alone. See README.md "Gotchas" for what
that means in practice.

Plain stdlib ``re``. No runtime dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "DEFAULT_RULES",
    "Rule",
    "rule",
]


@dataclass(frozen=True)
class Rule:
    """One detection rule.

    Attributes:
        name: stable identifier, reported on every match. Callers group,
            filter and count by this, so treat it as part of the API.
        regex: the compiled pattern.
        group: which capture group holds the *secret itself*. ``0`` (the whole
            match) is right for provider prefixes, where the entire token is
            sensitive. Assignment rules set this to a named group so that
            ``password = "hunter2hunter2"`` redacts to
            ``password = "[REDACTED]"`` and keeps the readable half.
    """

    name: str
    regex: re.Pattern[str]
    group: int | str = 0


def rule(name: str, pattern: str, *, group: int | str = 0, flags: int = 0) -> Rule:
    """Compile a custom rule.

    The supported way to extend the table without editing this module::

        from secret_redaction import DEFAULT_RULES, Redactor, rule

        internal = rule("acme_deploy_key", r"(?<![A-Za-z0-9])acme_dk_[a-f0-9]{40}")
        redactor = Redactor(extra_rules=[internal])

    Args:
        name: reported as ``SecretMatch.rule``.
        pattern: an ``re`` pattern string.
        group: index or name of the group holding the secret; ``0`` means the
            whole match is the secret.
        flags: ``re`` flags, e.g. ``re.IGNORECASE``.
    """
    return Rule(name=name, regex=re.compile(pattern, flags), group=group)


# --------------------------------------------------------------------------- #
# Shared fragments
# --------------------------------------------------------------------------- #

# Leading guard. Rejects a token embedded in a longer alphanumeric run, so
# "MAKIAVELLIAN" does not read as an AWS key, while still firing after any
# punctuation delimiter: `key=AKIA...`, `"AKIA..."`, `(AKIA...)`.
#
# Note it deliberately permits a preceding underscore — `AWS_AKIA...` is far
# more likely to be a real key in an env dump than an English word. This is the
# reason the guard is a lookbehind rather than \b: \b treats `_` as a word
# character and would refuse to match after it, which is exactly the false
# negative that hides `OPENAI_API_KEY=...` (see the assignment rules below).
_LEAD = r"(?<![A-Za-z0-9])"

# No *trailing* guard anywhere in this table. A trailing \b or a fixed {n}
# length silently fails whenever a token is followed by more alphanumerics —
# truncated in a log, concatenated into a URL, or simply longer than the vendor
# documented. Every token rule ends in an open-ended greedy run instead: worst
# case it over-redacts a few adjacent characters, which is the safe direction.

# PEM labels: "RSA PRIVATE KEY", "OPENSSH PRIVATE KEY", "PGP PRIVATE KEY BLOCK",
# or a bare "PRIVATE KEY".
_PEM_LABEL = r"(?:[A-Z][A-Z0-9]* )*PRIVATE KEY(?: BLOCK)?"

_PEM = (
    rf"-----BEGIN {_PEM_LABEL}-----"
    # First alternative: a complete block, header through footer. The lazy
    # quantifier must be anchored on the *END marker*, not on a bare "-----",
    # or it terminates on the header's own trailing dashes and leaves the
    # entire key body in the output.
    rf"(?:[\s\S]*?-----END {_PEM_LABEL}-----"
    # Second alternative: a truncated block with no footer (log tail, clipped
    # paste). Consume to the next blank line or end of text — PEM bodies never
    # contain a blank line, so this stops in the right place while still
    # removing the key material.
    rf"|[\s\S]*?(?=\n[ \t]*\n|\Z))"
)

# Keywords that make a `<name> = <value>` assignment credential-shaped.
#
# Ordered longest-first: `secret_access_key` must be tried before `secret_key`,
# or the alternation matches the shorter prefix and the trailing guard fails.
#
# Deliberately does NOT include bare `secret` or `token`. Those turn
# `token: cl100k_base_encoding` and every tokenizer config into a redaction.
# Add them yourself with `rule()` if your corpus warrants it.
_ASSIGN_KEYWORD = (
    r"(?:secret[_-]?access[_-]?key"
    r"|client[_-]?secret"
    r"|refresh[_-]?token"
    r"|private[_-]?key"
    r"|access[_-]?token"
    r"|secret[_-]?key"
    r"|auth[_-]?token"
    r"|passphrase"
    r"|api[_-]?key"
    r"|password"
    r"|passwd)"
)

# The keyword must be a whole identifier component. Underscore is allowed on
# both sides so `OPENAI_API_KEY` and `password_hash` both qualify.
_ASSIGN_HEAD = rf"{_LEAD}{_ASSIGN_KEYWORD}(?![A-Za-z0-9])"

# `= ` / `: ` / `=` / `:`. Horizontal whitespace only — crossing a newline
# would let a YAML key claim the next line's unrelated value.
#
# The leading `['"]?` is the *key's* closing quote, and it is what makes the
# JSON/YAML form work: `{"api_key": "..."}`. Without it the character after the
# keyword is `"` rather than the operator, the rule fails outright, and the
# single most common machine-generated shape of a leaked key — a serialized
# config or tool payload pasted into a log — passes through untouched.
_ASSIGN_OP = r"['\"]?[ \t]*[:=][ \t]*"

# Minimum value length. Short enough to catch a weak password, long enough that
# `password: yes` and `api_key: null` stay readable.
_MIN_VALUE = 12


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #

DEFAULT_RULES: tuple[Rule, ...] = (
    # --- provider prefixes -------------------------------------------------
    # Anthropic must precede the OpenAI rule: `sk-ant-...` satisfies both, and
    # ties at the same offset are broken by table order.
    rule("anthropic_api_key", _LEAD + r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    rule("openai_api_key", _LEAD + r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}"),
    rule(
        "github_token",
        _LEAD + r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})",
    ),
    rule("gitlab_token", _LEAD + r"glpat-[A-Za-z0-9_\-]{20,}"),
    # AWS access key ids are AKIA/ASIA + 16, but `{16,}` is open-ended on
    # purpose — see the note on trailing guards above.
    rule("aws_access_key_id", _LEAD + r"(?:AKIA|ASIA)[0-9A-Z]{16,}"),
    rule("google_api_key", _LEAD + r"AIza[0-9A-Za-z_\-]{35,}"),
    rule("slack_token", _LEAD + r"xox[abeprs]-[0-9A-Za-z\-]{10,}"),
    # Test-mode Stripe keys are included: they are shaped like live keys and
    # cost nothing to scrub. Drop this rule if your fixtures rely on them.
    rule("stripe_key", _LEAD + r"(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"),
    rule("huggingface_token", _LEAD + r"hf_[A-Za-z0-9]{34,}"),
    rule("npm_token", _LEAD + r"npm_[A-Za-z0-9]{36,}"),
    # PyPI tokens are `pypi-` + a long base64 macaroon. The length floor is
    # what keeps `pypi-server` and similar prose out.
    rule("pypi_token", _LEAD + r"pypi-[A-Za-z0-9_\-]{32,}"),
    # JWTs: header and payload both start `eyJ` (base64 of `{"`). Requiring
    # *two* such segments is what separates a token from stray base64.
    rule(
        "jwt",
        _LEAD + r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*",
    ),
    rule("pem_private_key", _PEM),

    # --- explicit assignments ----------------------------------------------
    # Quoted first: its value class accepts symbols that would end a bare
    # value, and the two cannot both match (the bare class excludes quotes).
    #
    # The value class is `[^'"`\n]`, not an alphanumeric+base64 set: a real
    # password contains symbols, and stopping at the first `&` is how
    # `password = "Tr0ub4dor&3xKcd9911"` survives redaction.
    #
    # The backtick is a quote character here because the bare rule's class
    # excludes it — without this alternative, `password=`hunter2hunter2`` is
    # matched by neither rule.
    rule(
        "assignment_quoted",
        _ASSIGN_HEAD + _ASSIGN_OP + rf"(?P<q>['\"`])(?P<secret>[^'\"`\n]{{{_MIN_VALUE},}})(?P=q)",
        group="secret",
        flags=re.IGNORECASE,
    ),
    rule(
        "assignment_bare",
        _ASSIGN_HEAD + _ASSIGN_OP + rf"(?P<secret>[^\s'\"`]{{{_MIN_VALUE},}})",
        group="secret",
        flags=re.IGNORECASE,
    ),
)
