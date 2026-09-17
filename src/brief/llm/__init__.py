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
