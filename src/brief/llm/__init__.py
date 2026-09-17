"""Choose an LLM provider from config, and report on the ones available.

App code: it knows the shape of the `llm` block in `config.yaml`. Everything
below it is vendored. `synthesize.py` imports only the `LLMProvider` Protocol
and calls `complete`, never a vendor SDK, so switching provider is a config
line and never an edit.

Four kinds:

- `claude-cli`, `codex-cli` — shell out to a coding CLI the operator is
  already signed in to. No API key, no billing integration, nothing to leak.
- `local` — an Ollama server, which picks a model from what is installed
  rather than demanding one by name.
- `anthropic-api` — the metered path, for a headless host that cannot hold a
  CLI login. Not implemented yet; asking for it raises with that sentence
  rather than silently falling back to a different provider, because a silent
  provider swap changes the character of a brief without telling its reader.

Failure is a state, not an exception: a provider that is missing, not signed
in, or has no model answers `available() == False` and a `status()` sentence
that names the fix. `brief doctor` prints exactly that.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, Iterator, Protocol, runtime_checkable

from brief.vendor.cli_llm_providers import PROVIDERS as CLI_PROVIDERS
from brief.vendor.cli_llm_providers import get_provider as get_cli_provider
from brief.vendor.ollama_local_llm import OllamaProvider

CLI_KINDS = ("claude-cli", "codex-cli", "copilot-cli")
KINDS = (*CLI_KINDS, "local", "anthropic-api")
DEFAULT_KIND = "claude-cli"


@runtime_checkable
class LLMProvider(Protocol):
    """The narrow surface synthesis needs. Deliberately small.

    Vendored from `cli-llm-providers`; restated here so app modules can type
    against it without importing a concrete provider class.
    """

    name: str

    def available(self) -> bool: ...
    def status(self) -> str: ...
    def context_window(self) -> int: ...
    def complete(self, messages: list[dict]) -> str: ...
    def chat_stream(self, messages: list[dict]) -> Iterator[str]: ...
    def embed(self, texts: list[str]) -> list[list[float]] | None: ...


CONSTRAINT_KEYWORD = "response_format"


def supports_constrained_json(provider: Any) -> bool:
    """Can this provider restrict its own decoding to a JSON Schema?

    Answered by asking the callable, not by naming a class, so a provider that
    gains the capability later is used without editing this module. Ollama has
    it; `claude -p` and `codex exec` are prompt-in text-out and do not.

    A declared `response_format` parameter counts; `**kwargs` does not. A
    generic wrapper accepts every keyword and silently discards the ones it
    does not understand, which would advertise a guarantee nothing enforces --
    and an unconstrained call that believes it is constrained is exactly the
    failure this whole path exists to remove.
    """
    try:
        parameters = inspect.signature(provider.complete).parameters
    except (AttributeError, TypeError, ValueError):
        return False
    parameter = parameters.get(CONSTRAINT_KEYWORD)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def provider_model(provider: Any) -> str:
    """The model name to record against a stored brief.

    Providers spell it two ways and neither is the Protocol's business: the
    coding CLIs carry a `model` attribute that is None when the CLI picks its
    own default, and Ollama resolves one through `chat_model()`, by discovery
    when config did not name it. Both are asked, in that order.

    Returns "unknown" when neither answers, and deliberately NOT the provider
    name. `briefs.provider` already records that, and a model column reading
    "ollama" is worse than one reading "unknown": it looks like an answer, so
    nobody goes looking for the real one. The column exists so a brief can be
    traced to the model that wrote it — a 3B and a 27B produce very different
    pages from the same prompt hash.
    """
    named = getattr(provider, "model", None)
    if isinstance(named, str) and named.strip():
        return named.strip()

    resolve = getattr(provider, "chat_model", None)
    if callable(resolve):
        try:
            resolved = resolve()
        except Exception:
            resolved = None
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()

    return "unknown"


def _sampler_options(cfg: Mapping[str, Any]) -> dict[str, Any] | None:
    """Ollama sampler settings, defaulting to a reproducible answer.

    Ollama's own defaults are temperature 0.8 and a fresh random seed, so the
    same prompt over the same items returns different text every run. That makes
    the provenance this app stores — prompt_hash, input_item_ids,
    ITEM_RENDER_VERSION — only structurally comparable: you can diff two briefs
    entry by entry but you cannot tell a real change from resampling.

    Pinning temperature and seed turns that into genuine reproducibility, which
    is what a weekly report wants. `seed: null` in config opts back out for
    anyone who would rather have variety.
    """
    llm_cfg = dict(cfg.get("options") or {})
    if "seed" not in llm_cfg and "seed" in cfg:
        if cfg["seed"] is None:
            return llm_cfg or None
        llm_cfg["seed"] = cfg["seed"]
    llm_cfg.setdefault("temperature", cfg.get("temperature", 0))
    llm_cfg.setdefault("seed", 42)
    llm_cfg.setdefault("top_k", 1)
    return llm_cfg


class ProviderNotAvailable(RuntimeError):
    """A configured provider cannot be used. The message names the fix."""

    def __init__(self, kind: str, status: str) -> None:
        self.kind = kind
        self.status = status
        super().__init__(f"{kind}: {status}")


def get_provider(llm: Mapping[str, Any] | None = None) -> LLMProvider:
    """Build the provider named by an `llm` config block.

    The block is `{provider, model?, base_url?, timeout_s?}`. An unknown
    provider name raises rather than defaulting, so a typo in a profile is
    caught at load instead of producing a brief from an unexpected model.
    """
    cfg = dict(llm or {})
    kind = str(cfg.get("provider") or DEFAULT_KIND)
    model = cfg.get("model")
    timeout = int(cfg.get("timeout_s") or 600)

    if kind in CLI_PROVIDERS:
        return get_cli_provider(kind, model, timeout=timeout)

    if kind == "local":
        return OllamaProvider(
            cfg.get("base_url"),
            chat_model=model,
            request_timeout=float(timeout),
            context_window=cfg.get("context_window"),
            options=_sampler_options(cfg),
            keep_alive=cfg.get("keep_alive"),
        )

    if kind == "anthropic-api":
        raise ValueError(
            "provider 'anthropic-api' is not implemented; it is a ~40-line module on the "
            "anthropic SDK, planned for when a host cannot hold a CLI login. "
            f"Use one of {', '.join(CLI_KINDS)} or 'local' meanwhile."
        )

    raise ValueError(
        f"unknown llm provider {kind!r}; expected one of {', '.join(KINDS)}"
    )


def require_provider(llm: Mapping[str, Any] | None = None) -> LLMProvider:
    """Build a provider and refuse to return an unusable one.

    Synthesis calls this. The raised message is `status()`, which names the
    fix ("run `ollama pull qwen3:8b`", "claude is not on PATH"), so the stub
    email a reader receives says something actionable.
    """
    provider = get_provider(llm)
    if not provider.available():
        raise ProviderNotAvailable(
            str(getattr(provider, "name", "provider")), provider.status()
        )
    return provider


def describe(llm: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One provider's state, for `brief doctor`. Never raises."""
    cfg = dict(llm or {})
    kind = str(cfg.get("provider") or DEFAULT_KIND)
    try:
        provider = get_provider(cfg)
    except ValueError as exc:
        return {
            "provider": kind,
            "name": kind,
            "available": False,
            "status": str(exc),
            "context_window": None,
        }
    try:
        available = provider.available()
        status = provider.status()
        window = provider.context_window() if available else None
    except Exception as exc:  # a probe must not take down the report
        return {
            "provider": kind,
            "name": str(getattr(provider, "name", kind)),
            "available": False,
            "status": f"probe failed: {exc}",
            "context_window": None,
        }
    return {
        "provider": kind,
        "name": str(getattr(provider, "name", kind)),
        "available": available,
        "status": status,
        "context_window": window,
    }
