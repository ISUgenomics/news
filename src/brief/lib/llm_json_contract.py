"""llm_json_contract — get a schema-valid JSON object out of a model, reliably.

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
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "DEFAULT_RETRY_TEMPLATE",
    "DEFAULT_RETRY_RULE",
    "JsonContractError",
    "ContractResult",
    "extract_json_object",
    "jsonschema_validator",
    "complete_json",
]

DEFAULT_RETRY_RULE = "Reply with only the corrected JSON object."

DEFAULT_RETRY_TEMPLATE = "That reply was rejected: {error}\n{rule}"


class JsonContractError(RuntimeError):
    """Every attempt failed; carries the raw evidence.

    ``responses`` holds every raw reply in order and ``errors`` the failure
    text per attempt, so a caller can store the model's words verbatim even
    though nothing validated.
    """

    def __init__(
        self,
        message: str,
        responses: tuple[str, ...],
        errors: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.responses = tuple(responses)
        self.errors = tuple(errors)


@dataclass(frozen=True)
class ContractResult:
    """A validated object plus every raw reply and failure, in order."""

    obj: dict
    responses: tuple[str, ...]
    errors: tuple[str, ...]
    attempts: int


def extract_json_object(text: str) -> dict:
    """Return the first well-formed JSON object in ``text``.

    Scans every ``{`` with ``json.JSONDecoder.raw_decode`` and returns the
    first position that decodes, so fences, preambles and trailing chatter are
    all handled without a regex. A reply that is itself valid JSON but not an
    object (a top-level array or scalar) is an error rather than a hunt for a
    nested object: the model returned the wrong shape and saying so is more
    useful than unwrapping it.

    Raises ``ValueError`` naming the reason: no object found, or decoded value
    is not an object.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("no JSON object found: the reply is empty")

    try:
        whole = json.loads(stripped)
    except ValueError:
        pass
    else:
        if isinstance(whole, dict):
            return whole
        raise ValueError(
            f"decoded value is not an object (got {type(whole).__name__})"
        )

    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            candidate, _end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(candidate, dict):
            return candidate
        index = text.find("{", index + 1)

    raise ValueError("no JSON object found in the reply")


def jsonschema_validator(schema: dict) -> Callable[[Any], str | None]:
    """Build a ``validate`` callable from a JSON Schema document.

    ``jsonschema`` is imported here and nowhere else: the rest of the module
    is stdlib-only. A missing package raises ``ImportError`` naming the
    install.

    The schema itself is checked once, here, so a malformed schema raises
    ``jsonschema.exceptions.SchemaError`` at build time rather than being
    discovered as a mysterious pass or failure per object later.
    """
    try:
        import jsonschema
        import jsonschema.exceptions
        import jsonschema.validators
    except ImportError as exc:  # pragma: no cover - exercised in a subprocess
        raise ImportError(
            "jsonschema_validator requires the 'jsonschema' package "
            "(pip install jsonschema)"
        ) from exc

    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)

    def validate(obj: Any) -> str | None:
        error = jsonschema.exceptions.best_match(validator.iter_errors(obj))
        if error is None:
            return None
        path = "/".join(str(part) for part in error.absolute_path) or "(root)"
        return f"{path}: {error.message}"

    return validate


def complete_json(
    complete: Callable[..., str],
    messages: list[dict],
    validate: Callable[[Any], str | None],
    *,
    schema: dict | None = None,
    retries: int = 1,
    retry_template: str = DEFAULT_RETRY_TEMPLATE,
) -> ContractResult:
    """Call ``complete`` until it yields an object ``validate`` accepts.

    On each failure the conversation grows by two turns -- the assistant reply
    that failed, then a user turn rendered from ``retry_template`` with
    ``{error}`` and ``{rule}`` -- so the model sees both what it wrote and why
    it was rejected. ``messages`` itself is never mutated.

    Passing ``schema`` calls ``complete(conversation, schema=schema)`` on every
    attempt, including retries, for a provider that can constrain its decoding
    to a JSON Schema. It is the strongest rung of this contract and the cheapest
    one, because the failure never happens rather than being corrected. It does
    not replace ``validate`` -- see the module docstring for what a grammar does
    and does not enforce, measured.

    A provider spells the parameter its own way (Ollama ``format``, OpenAI
    ``response_format``). Adapt at the call site rather than teaching this
    module a provider's vocabulary::

        complete_json(
            lambda msgs, schema: provider.complete(msgs, response_format=schema),
            messages, validate, schema=my_schema,
        )

    Raises ``JsonContractError`` when the last allowed attempt still fails, and
    ``ValueError`` when ``retries`` is negative or ``retry_template`` is not a
    format string over ``{error}`` and ``{rule}`` alone. Both are checked
    before the first call, so a caller mistake never costs a model call and
    never strands the evidence tuples. Exceptions from ``complete`` and from
    ``validate`` propagate untouched.
    """
    if retries < 0:
        raise ValueError(f"retries must be >= 0, got {retries}")
    try:
        retry_template.format(error="", rule="")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            "retry_template must use only {error} and {rule}; literal braces "
            f"must be doubled ({{{{ and }}}}): {exc!r}"
        ) from exc

    conversation: list[dict] = copy.deepcopy(list(messages))
    responses: list[str] = []
    errors: list[str] = []

    for attempt in range(retries + 1):
        attempt_messages = copy.deepcopy(conversation)
        if schema is None:
            reply = complete(attempt_messages)
        else:
            reply = complete(attempt_messages, schema=schema)
        responses.append(reply)
        try:
            obj = extract_json_object(reply)
        except ValueError as exc:
            error: str | None = str(exc)
        else:
            error = validate(obj)
            if error is None:
                return ContractResult(
                    obj=obj,
                    responses=tuple(responses),
                    errors=tuple(errors),
                    attempts=attempt + 1,
                )
        errors.append(error)
        if attempt < retries:
            conversation = conversation + [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": retry_template.format(
                        error=error, rule=DEFAULT_RETRY_RULE
                    ),
                },
            ]

    raise JsonContractError(
        f"no valid JSON object after {len(responses)} attempt(s): {errors[-1]}",
        tuple(responses),
        tuple(errors),
    )
