# Seed boundary: config-env-interpolate

Added by the design critic. `layered-config-overlay` merges layers and explicitly does not
interpolate; without this seed the app's profile loader reinvents it with a regex and a silent miss.

## Purpose
Substitute `${VAR}` references throughout a loaded config structure from an explicitly supplied
environment mapping, failing loudly and by key path when a variable is missing.

## when_to_use (draft for FEATURE.toml)
Your config file is checked into the repo and refers to secrets by name, and you need those
references resolved before anything reads the values — with a missing variable stopping startup
and naming where it was referenced, rather than a URL that silently contains the literal text
`${TOKEN}`.

## Inputs
- `data: Any` — a nested structure of mappings, sequences, and scalars, as a YAML or JSON load produces it
- `env: Mapping[str, str]` — the variables to substitute from. Required, never defaulted to `os.environ`: the caller decides what this config may see, and tests pass a dict
- `strict: bool = True` — `True` raises on an unresolved reference with no default; `False` leaves the original text in place and records it in the result

## Outputs
- the same structure, rebuilt with substitutions applied to every string, leaving non-string scalars untouched (ints stay ints)
- `MissingConfigVar` (subclass of `KeyError`) in strict mode, with `.variable`, `.path` (e.g. `feeds[1].url`), and a message naming both
- `interpolate_config` returns the structure; `interpolate_config_report` returns `(structure, unresolved: list[tuple[str, str]])` for the non-strict path

## Dependencies
- none; stdlib `re`, `typing` only

## Must NOT know about
- `os.environ` — the environment is an argument, always. Reading the process environment inside a library function is what makes it untestable and unsafe to reuse
- `config.yaml`, `sources.yaml`, the profile schema, or any key name from this app
- YAML or file loading — it takes an already-parsed structure
- secrets policy, redaction, or logging; it substitutes values and never prints one

## Public API
```python
class MissingConfigVar(KeyError):
    variable: str
    path: str
def interpolate_config(data: Any, env: Mapping[str, str], *, strict: bool = True) -> Any
def interpolate_config_report(data: Any, env: Mapping[str, str]) -> tuple[Any, list[tuple[str, str]]]  # (data, [(variable, path), ...])
def interpolate_str(text: str, env: Mapping[str, str], *, strict: bool = True, path: str = "") -> str  # the single-string case, exposed because callers have one-off strings
```

## Syntax to support
- `${VAR}` — substitute, or raise in strict mode
- `${VAR:-default}` — substitute, or use the literal default text when unset or empty
- `$$` — an escaped literal `$`, so a config can contain a real dollar sign
- A bare `$VAR` without braces is NOT substituted; requiring braces keeps `$5,000` and shell snippets in a config from being mangled. State this in the docstring as a deliberate choice.

## Test harness
none

## Approved module docstring (write this verbatim into the module)
```
Resolve ``${VAR}`` references in a loaded config structure.

Situation: the config file is checked into the repo, so it names secrets
rather than holding them — ``url: "http://host:5000/rss?token=${CD_TOKEN}"``.
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
bare ``$VAR`` is deliberately left alone — configs contain prices and shell
fragments, and requiring braces means those survive.

Deliberately not here: reading ``os.environ`` (the environment is an
argument, so a caller controls what this config may see and a test passes a
dict), loading or parsing files, merging config layers, and any knowledge of
which keys exist.
```

## Prior art
verdict: seed

`lib_search.py` was run for this situation during the critique ("config file refers to a secret by
name and must be resolved before use", "substitute environment variables into a loaded config").
`layered-config-overlay` is the top hit and is the adjacent feature: it merges a base config with
overlays and its README states it does not interpolate. Nothing else in the library touches
config values. Copy nothing; this is new.

## Boundary fixes to apply at write time
- The error must carry the key path, not just the variable name. That path is the whole reason this is a module and not a two-line regex, so pin it with a test on a nested list-inside-mapping case.
- `env` has no default. A caller writes `interpolate_config(data, os.environ)` at the edge of the app; the seed never reads the process environment.
- Never include a resolved value in the exception message or any string the module builds for a human. Names only.
- Test that `${VAR:-}` (empty default) resolves to the empty string rather than raising.
