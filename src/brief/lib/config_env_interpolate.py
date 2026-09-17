"""Resolve ``${VAR}`` references in a loaded config structure.

Situation: the config file is checked into the repo, so it names secrets
rather than holding them -- ``url: "http://host:5000/rss?token=${CD_TOKEN}"``.
Something has to turn those names into values after the file is parsed and
before anything reads it, and the failure mode that matters is the quiet one:
an unset variable leaving the literal text ``${CD_TOKEN}`` in a URL, which
then fails hours later as a puzzling 401.

Contract: ``interpolate_config(data, env)`` walks mappings, sequences, and
strings, rebuilding the structure with substitutions applied. Non-string
scalars are returned untouched. In strict mode (the default) an unresolved
reference raises ``MissingConfigVar`` carrying both the variable name and the
key path where it appeared, e.g. ``feeds[1].url``. Non-strict callers use
``interpolate_config_report`` and get the unresolved list back instead.

Syntax: ``${VAR}``, ``${VAR:-default}``, and ``$$`` for a literal dollar. A
bare ``$VAR`` is deliberately left alone -- configs contain prices and shell
fragments, and requiring braces means those survive.

Deliberately not here: reading ``os.environ`` (the environment is an
argument, so a caller controls what this config may see and a test passes a
dict), loading or parsing files, merging config layers, and any knowledge of
which keys exist.

Specified at write time, where the paragraphs above leave a choice:

- A variable name is ``[A-Za-z_][A-Za-z0-9_]*``. Anything else between ``${``
  and ``}`` is not a reference and is left in place verbatim: ``${}``,
  ``${ VAR }``, ``${2FA}`` and an unterminated ``${VAR`` all survive the walk
  unchanged and raise nothing. Strictness is about a *reference* that cannot
  be resolved, not about punctuation.
- ``${VAR:-default}`` uses the default when the name is absent from ``env``
  *or* maps to the empty string, matching the shell's ``:-``. The default is
  literal text and stops at the first ``}``; it is not itself interpolated, so
  there is no nesting and no recursion to bound. Literal means literal all the
  way: ``${T:-$$5}`` yields ``$$5``, because ``$$`` is unescaped only in
  ordinary text, never inside a default.
- ``${VAR}`` with no default resolves to whatever ``env`` holds, including the
  empty string. Present-and-empty is a value, not a miss; only a name that is
  absent from ``env`` raises.
- Mapping keys are not interpolated, only values. The key path in an error
  names a value, and a substituted key would be a value's address changing
  under the caller. A non-string key is rendered with ``str()`` for the path
  only (``{3: ...}`` reports ``3``); the key itself is kept as it was.
- A resolved value is inserted verbatim. Text inside it that reads as a regular
  expression replacement template -- ``\\1``, ``\\g<0>``, a trailing backslash --
  is not expanded and never raises, so a password full of backslashes survives
  intact.
- Mappings are rebuilt as ``dict`` in their original order; lists as ``list``
  and tuples as ``tuple``. ``str`` and ``bytes`` are scalars, never sequences
  to walk into; ``bytes`` are returned untouched.
- Error text carries the variable name and the key path and nothing else. No
  resolved value, and no surrounding config text, is ever put in a message --
  the whole point of the module is that those values are secrets.
- ``interpolate_config_report`` records one entry per unresolved *occurrence*,
  in document order, left to right within a string. Two misses of the same
  name are two entries; the caller decides whether to deduplicate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "MissingConfigVar",
    "interpolate_config",
    "interpolate_config_report",
    "interpolate_str",
]


# ``$$`` first, so an escaped dollar can never open a reference. A name is
# ASCII ``[A-Za-z_][A-Za-z0-9_]*``; a default runs to the first ``}``.
_REFERENCE = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class MissingConfigVar(KeyError):
    """An unresolved ``${VAR}`` reference, naming the variable and where it sat.

    Subclasses ``KeyError`` so a caller already guarding config loading with
    ``except KeyError`` keeps working. ``variable`` is the name that was not in
    ``env``; ``path`` is the key path of the value that referenced it
    (``feeds[1].url``), or ``""`` when the string was the whole structure.
    Neither the resolved value of any other reference nor the surrounding text
    appears in the message.
    """

    variable: str
    path: str

    def __init__(self, variable: str, path: str = "") -> None:
        self.variable = variable
        self.path = path
        where = f" (referenced at {path})" if path else ""
        super().__init__(f"config variable {variable} is not set{where}")

    def __str__(self) -> str:
        # KeyError renders repr(args[0]); a config error should read as a sentence.
        return str(self.args[0])


def interpolate_str(
    text: str, env: Mapping[str, str], *, strict: bool = True, path: str = ""
) -> str:
    """Substitute ``${VAR}`` references in one string.

    ``path`` is only used to build the error, so a caller with a one-off string
    can pass the name of the setting it came from. With ``strict`` false an
    unresolved reference is left in the text verbatim; use
    ``interpolate_config_report`` when the misses need collecting rather than
    ignoring. Substituted values are not re-scanned, so a value that itself
    contains ``${...}`` is inserted literally.
    """
    return _substitute(text, env, path, None if strict else [])


def interpolate_config(
    data: Any, env: Mapping[str, str], *, strict: bool = True
) -> Any:
    """Rebuild ``data`` with every string value interpolated.

    Mappings and sequences are rebuilt, not mutated: the caller's structure is
    left as it was. Non-string scalars -- ints, floats, bools, ``None``,
    ``bytes`` -- pass through untouched, and mapping keys are not interpolated.
    Strict mode raises ``MissingConfigVar`` at the first unresolved reference in
    document order; non-strict leaves it in place and says nothing.
    """
    return _walk(data, env, "", None if strict else [])


def interpolate_config_report(
    data: Any, env: Mapping[str, str]
) -> tuple[Any, list[tuple[str, str]]]:
    """Non-strict walk: the rebuilt structure plus ``[(variable, path), ...]``.

    Never raises. Unresolved references stay in the text and are reported, one
    entry per occurrence, in document order and left to right within a string;
    a reference that used its ``:-`` default is resolved, not reported.
    """
    unresolved: list[tuple[str, str]] = []
    return _walk(data, env, "", unresolved), unresolved


def _walk(
    data: Any, env: Mapping[str, str], path: str, sink: list[tuple[str, str]] | None
) -> Any:
    """Rebuild ``data``, interpolating strings and threading the key path down."""
    if isinstance(data, str):
        return _substitute(data, env, path, sink)
    if isinstance(data, Mapping):
        return {
            key: _walk(value, env, _child(path, str(key)), sink)
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        items = [
            _walk(value, env, f"{path}[{index}]", sink)
            for index, value in enumerate(data)
        ]
        return tuple(items) if isinstance(data, tuple) else items
    return data


def _child(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _substitute(
    text: str, env: Mapping[str, str], path: str, sink: list[tuple[str, str]] | None
) -> str:
    """One string. ``sink`` is ``None`` for strict, else the list misses land in."""

    def resolve(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        name, default = match.group(1), match.group(2)
        value = env.get(name)
        if default is not None:
            return value if value else default
        if value is None:
            if sink is None:
                raise MissingConfigVar(name, path)
            sink.append((name, path))
            return match.group(0)
        return value

    return _REFERENCE.sub(resolve, text)
