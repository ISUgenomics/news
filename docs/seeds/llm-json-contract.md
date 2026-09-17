# Seed boundary: llm-json-contract

## Purpose
Turn a completion callable into one that returns a schema-valid JSON object, by the strongest means the provider supports: constrain the decoding to the schema where that is possible, and otherwise extract, validate, and retry once with the validation error.

## when_to_use (draft for FEATURE.toml)
You asked a model (a coding CLI, a local Ollama box) for JSON and need a validated object back rather than a string that usually parses, with the raw replies kept for the week it does not. Providers differ in how much they can guarantee, and this covers both ends without the caller branching on which one it has.

## Inputs
- complete: Callable[[list[dict]], str] — any text-in/text-out completion; the seed never sees a provider object, only this callable
- messages: list[dict] — OpenAI-style role/content dicts already rendered by the caller (system prompt with schema inline, user turn with items); not mutated
- validate: Callable[[Any], str | None] — returns None when the object is acceptable, otherwise a human-readable error string that is appended to the retry prompt
- schema: dict | None = None — when given, passed to complete as the keyword `schema` on every attempt, for a provider that can constrain its decoding to a JSON Schema. Never merged into the prompt, never inspected, never used as the validator; a caller wanting both passes the same object twice, deliberately
- schema (to jsonschema_validator) — the same document, building a validate callable from a JSON Schema
- retries: int = 1 — how many corrective turns to append after the first failure; 0 disables the fallback
- retry_template: str — format string with {error} and {rule}, the user turn appended on retry; a default is provided
- text: str — for extract_json_object alone: any model reply, possibly wrapped in code fences or prose

## Outputs
- ContractResult — frozen dataclass: obj (the validated dict), responses (tuple[str, ...] of every raw reply in order, so the caller can store raw_response verbatim), errors (tuple[str, ...] of the failure text per failed attempt, for a parse-retry log line), attempts (int)
- JsonContractError(RuntimeError) — raised when every attempt fails; carries the same responses and errors tuples so raw evidence survives the failure
- extract_json_object(text) -> dict — the first decodable JSON object in the text; raises ValueError naming the reason (no object found / decoded value is not an object)
- Exceptions raised by complete() propagate unchanged; the seed does not classify provider failures

## Dependencies
- **json (stdlib)** — json.JSONDecoder.raw_decode tried at each '{' position finds the first well-formed object inside fences or prose without a regex or a brace-balancing parser
- **dataclasses (stdlib)** — ContractResult is a frozen value object
- **jsonschema (optional, imported lazily inside jsonschema_validator only)** — the app's contract is a JSON Schema document (prompts/schema.json) and hand-rolling required/enum/array checks is a known source of silent gaps; kept optional so complete_json and extract_json_object run and test with zero third-party packages, and an ImportError from the helper names the pip install

## Must NOT know about
- the LLMProvider Protocol or any provider class; it receives a bare callable
- any provider's spelling of the schema parameter (Ollama `format`, OpenAI `response_format`); the caller adapts with a one-line lambda, and teaching this module those names would make it a provider registry
- whether a given provider can be constrained at all; that is a capability question the caller answers before calling
- config.yaml / the profile YAML / the llm block (provider name, model, timeout, retry counts)
- the Item dataclass, item ids, or the citation rule (that is render.py's check)
- prompts/schema.json or prompts/system.md as paths; the schema arrives as a dict, the prompt as messages
- SQLite, the briefs table, raw_response/result_json columns
- logging configuration; it returns errors and attempts as data and never logs
- secret redaction; the caller redacts before storing responses
- how the retry turn is worded for a given app beyond the overridable template; no Iowa State, no buckets, no persona

## Public API
```python
DEFAULT_RETRY_TEMPLATE: str  # uses {error} and {rule}; rule defaults to 'Reply with only the corrected JSON object.'
class JsonContractError(RuntimeError):
    responses: tuple[str, ...]
    errors: tuple[str, ...]
@dataclass(frozen=True)
class ContractResult:
    obj: dict
    responses: tuple[str, ...]
    errors: tuple[str, ...]
    attempts: int
def extract_json_object(text: str) -> dict
def jsonschema_validator(schema: dict) -> Callable[[Any], str | None]
def complete_json(
    complete: Callable[..., str],
    messages: list[dict],
    validate: Callable[[Any], str | None],
    *,
    schema: dict | None = None,
    retries: int = 1,
    retry_template: str = DEFAULT_RETRY_TEMPLATE,
) -> ContractResult
```

## Test harness
none

## Approved module docstring (write this verbatim into the module)
```
llm_json_contract — get a schema-valid JSON object out of a model, reliably.

Asking a model to "reply with JSON" and hoping is the weak version of this. The
module is a ladder of four rungs, strongest first, and a caller takes the
highest rung its provider supports:

  1. CONSTRAIN. Pass ``schema`` and ``complete`` is called with it, so a
     provider that supports constrained decoding restricts its sampler to
     tokens that keep the reply conformant. The error does not happen.
  2. EXTRACT. Find the first JSON object in the reply -- inside ```json fences,
     after "Here is the result:", or bare. Coding CLIs (`claude -p`,
     `codex exec`) have no constrained mode and will wrap or preface it.
  3. VALIDATE AND RETRY. Hand the object to an injected
     ``validate(obj) -> str | None``; on failure, append the reply and the
     error as a corrective turn and ask once more.
  4. FAIL LOUDLY. Out of attempts, raise with every raw reply and every error,
     in order, so the caller can show its reader a stub rather than silence.

Rung 1 does not retire rungs 2-4, and that is the point most easily got wrong.
Measured against Ollama 0.33 with a 27B model and a real schema:

    enforced by the decoder      type, required, additionalProperties, enum,
                                 minItems -- told to emit an empty array where
                                 minItems was 1, it could not
    NOT enforced by the decoder  uniqueItems -- asked for [[1, 1]] it obliged,
                                 and only the validator caught it

So a constrained reply can still be invalid, and a grammar in any case
constrains shape and never truth: told to violate its schema, the model obeyed
the grammar and padded a declared section with junk to fill it. Constraint
moves the failure from "unparseable" to "well-formed and wrong". Validation is
the rung that catches the second kind, and it runs whether or not rung 1 did.

The corollary is worth stating because it is free: put everything the caller
already knows into the schema. A section name that comes from configuration
belongs in an ``enum``, not in a sentence asking the model to copy it
faithfully -- an unrepresentable mistake beats a corrected one.

Contract:
  - ``messages`` is never mutated; retries operate on a deep copy, and each
    attempt is handed its own copy, so a ``complete`` that edits the dicts it
    receives cannot reach the caller's list.
  - The first object that ``json.JSONDecoder.raw_decode`` accepts at a ``{``
    wins; arrays, scalars, and stray braces in prose are skipped.
  - ``validate`` returning None means accepted; any string means rejected, and
    that string is what the model sees on retry.
  - Exhausting ``retries`` raises ``JsonContractError`` carrying ``responses``
    and ``errors``; exceptions from ``complete`` propagate untouched.
  - ``schema`` is passed to ``complete`` as the keyword ``schema`` on every
    attempt or on none. It is never merged into the prompt, never inspected,
    and never used as the validator: a caller that wants both passes the same
    object twice, deliberately, because the two jobs can legitimately differ
    (a constrained call plus a validator that also checks what the grammar
    cannot express).

Deliberately not here: provider selection or availability, prompt rendering,
domain checks on the object's content (citations, word counts), logging,
persistence, and any provider's spelling of the schema parameter -- a
one-line lambda at the call site adapts ``format`` or ``response_format``, and
teaching this module those names would make it a provider registry.

Dependencies: stdlib only for the loop and extraction. ``jsonschema`` is
imported lazily by ``jsonschema_validator`` alone, because a JSON Schema file
is the usual contract and a hand-rolled checker is where fields go unchecked.

Notes on the two points the boundary review pinned:

  - The retry turn appends two messages, not one:
    ``{'role': 'assistant', 'content': <the failed reply>}`` then
    ``{'role': 'user', 'content': retry_template.format(error=..., rule=...)}``,
    so the model sees what it wrote as well as why it was rejected. Providers
    that flatten roles (coding CLIs) still receive both in order.
  - "First well-formed object wins" is literal: a stray ``{}`` or a small
    object in the preamble is returned in preference to the intended payload
    later in the reply. Callers that expect chatty preambles should ask the
    model for a bare object; the behaviour is pinned by test, not discovered.

``jsonschema`` is optional for the library and required by any caller using
``jsonschema_validator``; that caller pins it in its own dependencies.
```

## Prior art
verdict: seed

No feature or part in the library extracts a JSON object from model text, validates it, or retries with the error. A repo-wide grep of features/python src (excluding tests) for json.loads / JSONDecodeError / jsonschema / 'code fence' found only file and JSONL parsing. The two LLM provider features stop at text out and their READMEs say so. Seed it; the `complete` callable signature deliberately matches LLMProvider.complete from cli-llm-providers so the app passes provider.complete directly.

## Critic verdict: keep
One contract with a fallback branch; extraction is exposed separately but does not earn its own seed. Takes a bare callable that matches LLMProvider.complete, so the app passes provider.complete directly.

### Boundary fixes to apply at write time
- retry turn: public_api only shows retry_template({error},{rule}); the docstring says the failed reply is appended too — specify that retry appends [{'role':'assistant','content':reply},{'role':'user','content':template}] so the model sees what it wrote (roles are flattened by cli-llm-providers anyway)
- jsonschema stays optional and lazily imported; in this app it is required (prompts/schema.json), so pyproject pins it — note that in the seed docstring as 'optional for the library, required by any caller using jsonschema_validator'
- extract_json_object: 'first raw_decode-able object at a {' can pick a small object embedded in prose before the real one; document 'first well-formed object wins' and add a test with a stray {} in preamble so the behavior is pinned, not discovered

## Boundary change — 2026-09-17: the constrained rung

The approved boundary said "text-only model" and built the whole contract on
correcting a reply after the fact. That was true of the coding CLIs and was
wrong about Ollama, which accepts a JSON Schema as its `format` field and
restricts its sampler to tokens that keep the reply conformant.

Measured here before changing anything (Ollama 0.33, qwen3.8:27b-mlx, this
app's real schema, model explicitly instructed to violate it):

| | result |
|---|---|
| unconstrained | invented two top-level keys; would have cost a retry |
| `format` = the schema | type, `required`, `additionalProperties`, `minItems` all enforced by the decoder — told to emit an empty array where `minItems` was 1, it could not |
| + an `enum` of the section names | an invented section became unrepresentable |
| `uniqueItems` | **not** enforced; asked for `[[1, 1]]` it obliged, and only the validator caught it |

So the module becomes a ladder — constrain, extract, validate and retry, fail
loudly — and the caller takes the highest rung its provider supports. Two
things the measurement settled that an argument would not have:

- **Validation is not optional under constraint.** `uniqueItems` slips
  through, and shape is not truth in any case: told to violate its schema, the
  model obeyed the grammar and padded a declared section with junk to fill it.
  Constraint moves the failure from "unparseable" to "well-formed and wrong".
- **Rung 1 does not replace rungs 2–4**, it sits above them. The retry loop is
  unchanged and still carries the whole suite.

What did not change: the seed still takes a bare callable, still refuses to
know a provider's vocabulary, and still never touches the prompt.
