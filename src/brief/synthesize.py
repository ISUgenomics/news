"""One provider call per profile per week, and the contract around it.

App code. It renders the prompt template from the profile, calls whatever
provider the config names through the `LLMProvider` Protocol, and enforces the
JSON contract in code. It imports no vendor SDK — that rule is what makes the
provider a config line rather than an edit.

The contract is enforced after the call, not by the API, because the CLI
providers are prompt-in text-out and have no server-side schema. Extract the
first JSON object from whatever came back, validate it against
`prompts/schema.json`, and on failure retry once with the error and the failed
reply attached. Two failures is a stub email, not a guess.

What is stored is deliberately more than the answer: `prompt_hash`,
`input_item_ids` in send order, `provider`, and `raw_response`. That is what
lets the same week be regenerated on a different provider and diffed entry by
entry, which no amount of re-reading the prose would give you.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brief.lib.llm_json_contract import (
    JsonContractError,
    complete_json,
    jsonschema_validator,
)
from brief.models import Profile
from brief.select import Selection

PROMPTS = Path(__file__).parent / "prompts"

RETRY_RULE = (
    "Return one JSON object matching the schema, and nothing else: "
    "no prose, no code fence, no trailing commentary."
)


@dataclass(frozen=True, slots=True)
class Synthesis:
    result: dict[str, Any]
    raw_response: str
    prompt_hash: str
    system_prompt: str
    attempts: int
    provider_name: str
    model: str


def load_schema(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or PROMPTS / "schema.json").read_text(encoding="utf-8"))


def render_system_prompt(
    profile: Profile,
    *,
    schema: dict[str, Any],
    template_path: Path | None = None,
) -> str:
    """Fill the template from the profile. No f-strings on user text.

    The template uses `{name}` placeholders and the body contains JSON braces,
    so substitution is explicit and ordered rather than `str.format`, which
    would choke on the schema's braces.
    """
    template = (template_path or PROMPTS / "system.md").read_text(encoding="utf-8")

    buckets = "\n".join(
        f"{index}. **{bucket.name}** — {bucket.ask}"
        if bucket.ask
        else f"{index}. **{bucket.name}**"
        for index, bucket in enumerate(profile.buckets, start=1)
    )
    extra = ""
    if profile.extra_rules:
        extra = "## Additional rules for this brief\n\n" + "\n".join(
            f"- {rule}" for rule in profile.extra_rules
        )

    replacements = {
        "{persona}": profile.persona,
        "{audience}": profile.audience,
        "{buckets}": buckets,
        "{extra_rules}": extra,
        "{schema}": json.dumps(schema, indent=2),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def prompt_hash(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def synthesize(
    profile: Profile,
    selection: Selection,
    provider: Any,
    *,
    schema: dict[str, Any] | None = None,
    template_path: Path | None = None,
) -> Synthesis:
    """Call the provider once (twice on a contract failure) and validate.

    Raises `JsonContractError` when the model cannot produce a valid object.
    The caller turns that into a stub email; it does not fall back to another
    provider, because a silent provider swap changes the character of the
    brief without telling its reader.
    """
    schema = schema or load_schema()
    system_prompt = render_system_prompt(
        profile, schema=schema, template_path=template_path
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": selection.numbered_list()},
    ]

    outcome = complete_json(
        provider.complete,
        messages,
        jsonschema_validator(schema),
        retries=1,
        retry_template="That reply was rejected: {error}\n{rule}".replace(
            "{rule}", RETRY_RULE
        ),
    )

    result = _drop_unknown_positions(outcome.obj, sent=selection.sent)

    return Synthesis(
        result=result,
        raw_response=outcome.responses[-1] if outcome.responses else "",
        prompt_hash=prompt_hash(system_prompt),
        system_prompt=system_prompt,
        attempts=outcome.attempts,
        provider_name=str(getattr(provider, "name", "unknown")),
        model=str(
            getattr(provider, "model", None) or getattr(provider, "name", "unknown")
        ),
    )


def _drop_unknown_positions(result: dict[str, Any], *, sent: int) -> dict[str, Any]:
    """Strip `merged` pairs that name positions never sent.

    Entry citations are enforced by the renderer, which has the url map. This
    handles `merged` only, which the renderer does not read but the operator
    does.
    """
    merged = result.get("merged")
    if not isinstance(merged, list):
        return result
    cleaned = [
        group
        for group in merged
        if isinstance(group, list)
        and all(isinstance(p, int) and 1 <= p <= sent for p in group)
    ]
    if cleaned == merged:
        return result
    out = dict(result)
    out["merged"] = cleaned
    return out


__all__ = [
    "JsonContractError",
    "Synthesis",
    "load_schema",
    "prompt_hash",
    "render_system_prompt",
    "synthesize",
]
