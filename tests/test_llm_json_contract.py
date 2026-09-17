"""Tests for the llm_json_contract seed.

Stubs, never mocks: every `complete` here is a real callable with real
behaviour, the fenced reply is real bytes on disk, and the lazy-import check
runs a real subprocess with `jsonschema` made unimportable.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from brief.lib.llm_json_contract import (
    DEFAULT_RETRY_TEMPLATE,
    ContractResult,
    JsonContractError,
    complete_json,
    extract_json_object,
    jsonschema_validator,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]

SCHEMA = {
    "type": "object",
    "required": ["headline", "entries"],
    "properties": {
        "headline": {"type": "string"},
        "entries": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
        },
    },
}


class ScriptedCompletion:
    """A text-in/text-out completion that replays canned replies in order."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def __call__(self, messages: list[dict]) -> str:
        self.calls.append(copy.deepcopy(messages))
        if not self._replies:
            raise AssertionError("complete() called more times than scripted")
        return self._replies.pop(0)


def accept_everything(obj):
    return None


def base_messages() -> list[dict]:
    return [
        {"role": "system", "content": "Return JSON matching the schema."},
        {"role": "user", "content": "Items: 1, 2"},
    ]


# --------------------------------------------------------------------------
# extract_json_object
# --------------------------------------------------------------------------


def test_extract_bare_object():
    assert extract_json_object('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_extract_from_json_code_fence():
    text = 'Sure.\n\n```json\n{"a": 1}\n```\n'
    assert extract_json_object(text) == {"a": 1}


def test_extract_after_prose_preamble():
    text = 'Here is the result: {"a": 1}\nHope that helps.'
    assert extract_json_object(text) == {"a": 1}


def test_extract_reads_a_real_fenced_reply_with_unicode():
    text = (FIXTURES / "llm_json_contract_fenced_reply.txt").read_text(encoding="utf-8")
    obj = extract_json_object(text)
    assert obj["headline"] == "Université de Montréal — naïve coöperation"
    assert obj["entries"][0]["summary"] == "café 📈 growth"
    assert obj["entries"][0]["item_ids"] == [1, 2]


def test_extract_preserves_unicode_escapes_as_characters():
    # Real \uXXXX escapes, including a surrogate pair for an astral-plane
    # character: models emit both forms and they must decode identically.
    obj = extract_json_object(r'{"t": "caf\u00e9 \ud83d\udcc8"}')
    assert obj["t"] == "café 📈"
    assert extract_json_object('{"t": "café 📈"}') == obj


def test_extract_scans_by_character_not_byte_past_a_multibyte_preamble():
    # The preamble is multi-byte; a byte-indexed scan would land mid-object.
    text = "Résumé — naïve 📈 préambule: " + '{"a": "élan"}'
    assert extract_json_object(text) == {"a": "élan"}


def test_first_well_formed_object_wins_even_when_it_is_a_stray_preamble_object():
    # Pinned behaviour, not an accident: a stray {} before the payload wins.
    text = 'Thinking {} about it...\n```json\n{"real": true}\n```'
    assert extract_json_object(text) == {}


def test_extract_skips_unbalanced_braces_in_prose():
    text = 'I considered { the options } first, then: {"a": 1}'
    assert extract_json_object(text) == {"a": 1}


def test_extract_skips_an_array_embedded_in_prose():
    text = 'Ids: [1, 2, 3]. Object: {"a": 1}'
    assert extract_json_object(text) == {"a": 1}


def test_extract_raises_when_no_object_is_present():
    with pytest.raises(ValueError) as exc:
        extract_json_object("I could not produce JSON, sorry.")
    assert "no" in str(exc.value).lower() and "object" in str(exc.value).lower()


def test_extract_raises_on_empty_text():
    with pytest.raises(ValueError) as exc:
        extract_json_object("")
    assert "empty" in str(exc.value)


def test_extract_raises_when_the_whole_reply_decodes_to_a_non_object():
    with pytest.raises(ValueError) as exc:
        extract_json_object('[{"a": 1}]')
    assert "not an object" in str(exc.value)


def test_extract_names_a_scalar_reply_as_not_an_object():
    with pytest.raises(ValueError) as exc:
        extract_json_object("42")
    assert "not an object" in str(exc.value)


# --------------------------------------------------------------------------
# complete_json — happy path
# --------------------------------------------------------------------------


def test_first_reply_valid_returns_result_with_one_attempt():
    complete = ScriptedCompletion('{"a": 1}')
    result = complete_json(complete, base_messages(), accept_everything)
    assert isinstance(result, ContractResult)
    assert result.obj == {"a": 1}
    assert result.attempts == 1
    assert result.responses == ('{"a": 1}',)
    assert result.errors == ()
    assert len(complete.calls) == 1


def test_validate_receives_the_extracted_object_not_the_text():
    seen = []

    def validate(obj):
        seen.append(obj)
        return None

    complete_json(
        ScriptedCompletion('```json\n{"a": 1}\n```'), base_messages(), validate
    )
    assert seen == [{"a": 1}]


def test_messages_are_not_mutated():
    messages = base_messages()
    snapshot = copy.deepcopy(messages)
    complete = ScriptedCompletion("not json", '{"a": 1}')
    complete_json(complete, messages, accept_everything)
    assert messages == snapshot
    assert len(messages) == 2


# --------------------------------------------------------------------------
# complete_json — retry behaviour
# --------------------------------------------------------------------------


def test_unparseable_first_reply_retries_and_succeeds():
    complete = ScriptedCompletion("Sorry, no JSON today.", '{"a": 1}')
    result = complete_json(complete, base_messages(), accept_everything)
    assert result.obj == {"a": 1}
    assert result.attempts == 2
    assert result.responses == ("Sorry, no JSON today.", '{"a": 1}')
    assert len(result.errors) == 1
    assert "object" in result.errors[0].lower()


def test_rejected_object_retries_with_the_validator_error():
    complete = ScriptedCompletion('{"a": 1}', '{"a": 2}')

    def validate(obj):
        return None if obj.get("a") == 2 else "field 'a' must be 2"

    result = complete_json(complete, base_messages(), validate)
    assert result.obj == {"a": 2}
    assert result.attempts == 2
    assert result.errors == ("field 'a' must be 2",)


def test_retry_appends_the_failed_reply_then_the_corrective_user_turn():
    complete = ScriptedCompletion('{"a": 1}', '{"a": 2}')

    def validate(obj):
        return None if obj.get("a") == 2 else "field 'a' must be 2"

    complete_json(complete, base_messages(), validate)
    second = complete.calls[1]
    assert len(second) == 4
    assert second[:2] == base_messages()
    assert second[2] == {"role": "assistant", "content": '{"a": 1}'}
    assert second[3]["role"] == "user"
    assert "field 'a' must be 2" in second[3]["content"]
    assert "Reply with only the corrected JSON object." in second[3]["content"]


def test_custom_retry_template_is_used_verbatim():
    complete = ScriptedCompletion("nope", '{"a": 1}')
    complete_json(
        complete,
        base_messages(),
        accept_everything,
        retry_template="BAD: {error} || DO: {rule}",
    )
    assert complete.calls[1][3]["content"].startswith("BAD: ")
    assert (
        " || DO: Reply with only the corrected JSON object."
        in complete.calls[1][3]["content"]
    )


def test_default_retry_template_carries_both_placeholders():
    assert "{error}" in DEFAULT_RETRY_TEMPLATE
    assert "{rule}" in DEFAULT_RETRY_TEMPLATE


def test_two_retries_make_three_attempts():
    complete = ScriptedCompletion("nope", "still nope", '{"a": 1}')
    result = complete_json(complete, base_messages(), accept_everything, retries=2)
    assert result.attempts == 3
    assert len(result.responses) == 3
    assert len(result.errors) == 2
    assert len(complete.calls[2]) == 6


# --------------------------------------------------------------------------
# complete_json — failure paths
# --------------------------------------------------------------------------


def test_retries_zero_asks_once_and_raises():
    complete = ScriptedCompletion("no json here")
    with pytest.raises(JsonContractError) as exc:
        complete_json(complete, base_messages(), accept_everything, retries=0)
    assert len(complete.calls) == 1
    assert exc.value.responses == ("no json here",)
    assert len(exc.value.errors) == 1


def test_exhausting_retries_raises_with_every_response_and_error():
    complete = ScriptedCompletion('{"a": 1}', '{"a": 1}')

    def validate(obj):
        return "always wrong"

    with pytest.raises(JsonContractError) as exc:
        complete_json(complete, base_messages(), validate)
    assert exc.value.responses == ('{"a": 1}', '{"a": 1}')
    assert exc.value.errors == ("always wrong", "always wrong")
    assert isinstance(exc.value, RuntimeError)


def test_complete_exceptions_propagate_unchanged():
    class ProviderDown(Exception):
        pass

    def complete(messages):
        raise ProviderDown("socket closed")

    with pytest.raises(ProviderDown, match="socket closed"):
        complete_json(complete, base_messages(), accept_everything)


def test_complete_exception_on_the_retry_turn_also_propagates():
    state = {"n": 0}

    class ProviderDown(Exception):
        pass

    def complete(messages):
        state["n"] += 1
        if state["n"] == 1:
            return "no json"
        raise ProviderDown("timed out")

    with pytest.raises(ProviderDown):
        complete_json(complete, base_messages(), accept_everything)


def test_negative_retries_is_rejected():
    with pytest.raises(ValueError):
        complete_json(
            ScriptedCompletion('{"a": 1}'),
            base_messages(),
            accept_everything,
            retries=-1,
        )


def test_contract_result_is_a_frozen_dataclass():
    # The boundary declares @dataclass(frozen=True); callers hold this value
    # object alongside the raw replies they are about to store.
    result = complete_json(
        ScriptedCompletion('{"a": 1}'), base_messages(), accept_everything
    )
    assert dataclasses.is_dataclass(result)
    assert dataclasses.fields(ContractResult)  # a dataclass, not a bare class
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.attempts = 99
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.obj = {}


def test_a_complete_that_edits_what_it_receives_cannot_reach_the_callers_messages():
    """Real mutating stub: the seed's copy must be deep, not shallow."""
    messages = base_messages()
    snapshot = copy.deepcopy(messages)
    state = {"n": 0}

    def vandal(received: list[dict]) -> str:
        state["n"] += 1
        received[0]["content"] = "CLOBBERED"
        received.append({"role": "user", "content": "injected"})
        return "not json" if state["n"] == 1 else '{"a": 1}'

    result = complete_json(vandal, messages, accept_everything)
    assert result.obj == {"a": 1}
    assert messages == snapshot, "caller's message dicts were mutated"


def test_each_attempt_is_handed_its_own_copy():
    """A stub that empties the list it is handed must not shrink the next turn."""
    seen: list[list[dict]] = []
    state = {"n": 0}

    def vandal(received: list[dict]) -> str:
        state["n"] += 1
        seen.append(copy.deepcopy(received))
        received.clear()
        return "not json" if state["n"] == 1 else '{"a": 1}'

    result = complete_json(vandal, base_messages(), accept_everything)
    assert result.attempts == 2
    # 2 base turns, then those 2 plus the failed reply and the corrective turn.
    assert [len(call) for call in seen] == [2, 4]
    assert seen[1][:2] == base_messages()


def test_empty_messages_are_passed_through_unchanged():
    complete = ScriptedCompletion('{"a": 1}')
    result = complete_json(complete, [], accept_everything)
    assert result.obj == {"a": 1}
    assert complete.calls == [[]]


def test_a_retry_template_with_literal_braces_is_rejected_before_any_call():
    complete = ScriptedCompletion('{"a": 1}')
    with pytest.raises(ValueError) as exc:
        complete_json(
            complete,
            base_messages(),
            accept_everything,
            retry_template='Rejected: {error}. Return {"entries": []}. {rule}',
        )
    assert "retry_template" in str(exc.value)
    assert complete.calls == [], "a bad template must not cost a model call"


def test_a_retry_template_with_positional_braces_is_rejected():
    with pytest.raises(ValueError):
        complete_json(
            ScriptedCompletion('{"a": 1}'),
            base_messages(),
            accept_everything,
            retry_template="Rejected: {} {error} {rule}",
        )


def test_doubled_braces_in_a_retry_template_survive_to_the_model():
    complete = ScriptedCompletion("nope", '{"a": 1}')
    complete_json(
        complete,
        base_messages(),
        accept_everything,
        retry_template='{error}. Return {{"entries": []}}. {rule}',
    )
    assert 'Return {"entries": []}.' in complete.calls[1][3]["content"]


def test_validate_exceptions_propagate_unchanged():
    class ValidatorBug(Exception):
        pass

    def validate(obj):
        raise ValidatorBug("boom")

    complete = ScriptedCompletion('{"a": 1}', '{"a": 1}')
    with pytest.raises(ValidatorBug, match="boom"):
        complete_json(complete, base_messages(), validate)
    assert len(complete.calls) == 1, "no retry after a validator bug"


# --------------------------------------------------------------------------
# jsonschema_validator
# --------------------------------------------------------------------------


def test_jsonschema_validator_accepts_a_valid_object():
    validate = jsonschema_validator(SCHEMA)
    assert validate({"headline": "hi", "entries": [{"summary": "s"}]}) is None


def test_jsonschema_validator_reports_a_missing_field():
    validate = jsonschema_validator(SCHEMA)
    error = validate({"entries": [{"summary": "s"}]})
    assert isinstance(error, str)
    assert "headline" in error


def test_jsonschema_validator_reports_a_wrong_type_with_a_path():
    validate = jsonschema_validator(SCHEMA)
    error = validate({"headline": 7, "entries": [{"summary": "s"}]})
    assert isinstance(error, str)
    assert "headline" in error


def test_jsonschema_validator_rejects_a_non_object_value():
    validate = jsonschema_validator(SCHEMA)
    assert isinstance(validate([1, 2, 3]), str)


def test_jsonschema_validator_rejects_a_malformed_schema_at_build_time():
    import jsonschema.exceptions

    with pytest.raises(jsonschema.exceptions.SchemaError):
        jsonschema_validator({"type": "obhect"})


def test_jsonschema_validator_drives_a_real_retry_loop():
    bad = json.dumps({"headline": "hi"})
    good = json.dumps({"headline": "hi", "entries": [{"summary": "s"}]})
    complete = ScriptedCompletion(bad, good)
    result = complete_json(complete, base_messages(), jsonschema_validator(SCHEMA))
    assert result.attempts == 2
    assert "entries" in result.errors[0]
    assert result.obj["entries"][0]["summary"] == "s"


def test_jsonschema_is_imported_lazily_and_the_rest_works_without_it():
    """Real subprocess with `jsonschema` unimportable: the module still loads."""
    script = """
import sys

class Block:
    def find_module(self, name, path=None):
        return self.find_spec(name, path)
    def find_spec(self, name, path=None, target=None):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ImportError("jsonschema is blocked for this test")
        return None

sys.meta_path.insert(0, Block())
for mod in list(sys.modules):
    if mod == "jsonschema" or mod.startswith("jsonschema."):
        del sys.modules[mod]

from brief.lib import llm_json_contract as m

assert "jsonschema" not in sys.modules, "import was not lazy"
assert m.extract_json_object('{"a": 1}') == {"a": 1}
res = m.complete_json(lambda msgs: '{"a": 1}', [{"role": "user", "content": "x"}], lambda o: None)
assert res.obj == {"a": 1}
try:
    m.jsonschema_validator({"type": "object"})
except ImportError as exc:
    assert "jsonschema" in str(exc)
else:
    raise AssertionError("jsonschema_validator should raise ImportError when the package is missing")
print("ok")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok" in proc.stdout


# --- rung 1: constrained decoding --------------------------------------------


def test_a_one_argument_complete_still_works_when_no_schema_is_given():
    """Every existing caller passes `complete(messages)`. Adding the rung must
    not require them to grow a keyword they have no use for."""
    calls = []

    def complete(messages):            # deliberately no **kwargs
        calls.append(messages)
        return '{"headline": "h", "entries": [{"summary": "s"}]}'

    result = complete_json(complete, [{"role": "user", "content": "x"}],
                           jsonschema_validator(SCHEMA))

    assert result.attempts == 1
    assert len(calls) == 1


def test_the_schema_reaches_the_provider_on_the_first_attempt():
    """The whole value of the rung is that the provider gets it. A schema that
    is accepted and quietly not forwarded is the failure this pins."""
    seen = []

    def complete(messages, *, schema=None):
        seen.append(schema)
        return '{"headline": "h", "entries": [{"summary": "s"}]}'

    complete_json(complete, [{"role": "user", "content": "x"}],
                  jsonschema_validator(SCHEMA), schema=SCHEMA)

    assert seen == [SCHEMA]


def test_the_retry_stays_constrained():
    """Measured: a constrained reply can still fail validation, because the
    decoder does not enforce every keyword (uniqueItems, for one). The retry
    that follows must not silently drop to unconstrained decoding -- that would
    make attempt two strictly weaker than attempt one."""
    seen = []
    replies = iter([
        '{"headline": "h", "entries": []}',     # minItems 1 -> rejected
        '{"headline": "h", "entries": [{"summary": "s"}]}',
    ])

    def complete(messages, *, schema=None):
        seen.append(schema)
        return next(replies)

    result = complete_json(complete, [{"role": "user", "content": "x"}],
                           jsonschema_validator(SCHEMA), schema=SCHEMA,
                           retries=1)

    assert result.attempts == 2
    assert seen == [SCHEMA, SCHEMA], "the retry must carry the schema too"


def test_the_schema_is_not_injected_into_the_prompt():
    """Constraining is a transport concern. A module that also edited the
    messages would silently double-count with a caller that renders the schema
    into its own system prompt, as this repo's synthesizer does."""
    messages = [{"role": "user", "content": "x"}]
    seen = []

    def complete(msgs, *, schema=None):
        seen.append(copy.deepcopy(msgs))
        return '{"headline": "h", "entries": [{"summary": "s"}]}'

    complete_json(complete, messages, jsonschema_validator(SCHEMA),
                  schema=SCHEMA)

    assert seen == [messages]
    assert messages == [{"role": "user", "content": "x"}]
