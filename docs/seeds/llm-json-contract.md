# Seed boundary: llm-json-contract

## Purpose
Turn a text-only completion callable into one that returns a schema-valid JSON object, retrying once with the validation error when the first reply fails.

## when_to_use (draft for FEATURE.toml)
You asked a text-only model (a coding CLI, a local Ollama box) for JSON and need a validated object back rather than a string that usually parses, with the raw replies kept for the week it does not.

## Inputs
- complete: Callable[[list[dict]], str] — any text-in/text-out completion; the seed never sees a provider object, only this callable
- messages: list[dict] — OpenAI-style role/content dicts already rendered by the caller (system prompt with schema inline, user turn with items); not mutated
- validate: Callable[[Any], str | None] — returns None when the object is acceptable, otherwise a human-readable error string that is appended to the retry prompt
- schema: dict — only for the jsonschema_validator(schema) helper, which builds a validate callable from a JSON Schema document
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
    complete: Callable[[list[dict]], str],
    messages: list[dict],
    validate: Callable[[Any], str | None],
    *,
    retries: int = 1,
    retry_template: str = DEFAULT_RETRY_TEMPLATE,
) -> ContractResult
```

## Test harness
none

## Approved module docstring (write this verbatim into the module)
```
llm_json_contract — get a schema-valid JSON object out of a text-only model.

Coding CLIs (`claude -p`, `codex exec`) and Ollama return prose, not structured
output. This module enforces the contract after the call: it takes any
``complete(messages) -> str`` callable, finds the first JSON object in the reply
(inside ```json fences, after "Here is the result:", or bare), hands it to an
injected ``validate(obj) -> str | None``, and on failure appends the reply and
the error as a corrective turn and asks once more. The caller gets the object
plus every raw reply, in order, so the evidence is kept when it matters.

Contract:
  - ``messages`` is never mutated; retries operate on a copy.
  - The first object that ``json.JSONDecoder.raw_decode`` accepts at a ``{``
    wins; arrays, scalars, and stray braces in prose are skipped.
  - ``validate`` returning None means accepted; any string means rejected, and
    that string is what the model sees on retry.
  - Exhausting ``retries`` raises ``JsonContractError`` carrying ``responses``
    and ``errors``; exceptions from ``complete`` propagate untouched.

Deliberately not here: provider selection or availability, prompt rendering,
domain checks on the object's content (citations, word counts), logging, and
persistence. Those belong to the caller, which knows the app.

Dependencies: stdlib only for the loop and extraction. ``jsonschema`` is
imported lazily by ``jsonschema_validator`` alone, because a JSON Schema file
is the usual contract and a hand-rolled checker is where fields go unchecked.
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
