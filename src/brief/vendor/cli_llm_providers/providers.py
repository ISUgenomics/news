"""Run LLM inference through already-authenticated coding CLIs.

No API keys anywhere. Each of these tools (`claude`, `codex`, `copilot`) holds
its own login, so shelling out to them gives an app model access using
credentials the user already has — nothing to store, nothing to leak, no
per-token billing to wire up.

The trade is real and worth stating: responses arrive **whole**, not streamed,
because these CLIs buffer to completion. ``chat_stream`` yields exactly once.
If token-by-token output matters, use a streaming HTTP provider instead.

Invocation quirks encoded here, each learned the hard way:

* prompts carrying RAG excerpts routinely exceed ``MAX_ARG_STRLEN`` as a single
  argv element, so they go over stdin wherever the CLI allows it
* ``codex`` requires the prompt *before* ``-i``, or it swallows it as an image
* ``claude`` needs images pre-authorized by resolved path, and on macOS
  ``/var/folders`` is a symlink, so the path must be resolved first
* ``copilot`` has no image support at all

No runtime dependencies.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator, Protocol

DEFAULT_TIMEOUT = 240


class LLMProvider(Protocol):
    """The narrow surface a caller needs. Deliberately small."""

    name: str

    def available(self) -> bool: ...
    def status(self) -> str: ...
    def context_window(self) -> int: ...
    def chat_stream(self, messages: list[dict]) -> Iterator[str]: ...
    def describe_image(self, png: bytes, prompt: str) -> str | None: ...
    def embed(self, texts: list[str]) -> list[list[float]] | None: ...


def messages_to_prompt(messages: list[dict]) -> str:
    """Flatten OpenAI-style messages into one prompt string.

    These CLIs take a single prompt, not a role-tagged conversation. System
    content is emitted bare; other roles are labelled so the model can still
    tell turns apart. The trailing ``Assistant:`` cues a completion.
    """
    parts: list[str] = []
    for message in messages:
        role = message.get("role", "user")
        if role == "system":
            parts.append(message["content"])
        else:
            parts.append(f"{role.capitalize()}: {message['content']}")
    parts.append("Assistant:")
    return "\n\n".join(parts)


class CLIProvider:
    """Base for CLI-backed providers. Subclasses define the invocation."""

    name = "cli"
    binary = ""
    context_tokens = 32_768

    def __init__(
        self,
        model: str | None = None,
        *,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.model = model or None
        self.timeout = timeout

    # ------------------------------------------------------------ shape ---

    def invocation(self, prompt: str) -> tuple[list[str], str | None]:
        """Return ``(argv, stdin)`` for a text prompt.

        Prompts carrying retrieval excerpts can exceed the OS limit on a
        single argv element, so pipe via stdin wherever the CLI supports it.
        """
        raise NotImplementedError

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def status(self) -> str:
        if not self.available():
            return (
                f"The {self.binary} CLI was not found on PATH. Install and "
                f"sign in to it, or choose another provider."
            )
        return f"{self.name} ready ({self.model or 'CLI default model'})."

    def context_window(self) -> int:
        return self.context_tokens

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Always None — no coding CLI exposes an embedding endpoint.

        Callers should treat None as "fall back to keyword retrieval" rather
        than as an error.
        """
        return None

    # ------------------------------------------------------- invocation ---

    def run(self, argv: list[str], stdin: str | None = None) -> str:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                input=stdin,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"{self.binary} timed out after {self.timeout}s"
            ) from None
        except OSError as exc:
            raise RuntimeError(f"could not run {self.binary}: {exc}") from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"{self.binary} exited {proc.returncode}: "
                f"{proc.stderr.strip()[:300]}"
            )
        return proc.stdout.strip()

    def chat_stream(self, messages: list[dict]) -> Iterator[str]:
        """Yield the whole response as a single chunk.

        Named ``chat_stream`` so it is drop-in compatible with a genuinely
        streaming provider; these CLIs simply have nothing to stream.
        """
        argv, stdin = self.invocation(messages_to_prompt(messages))
        yield self.run(argv, stdin=stdin)

    def complete(self, messages: list[dict]) -> str:
        """Convenience for callers that do not want an iterator."""
        return "".join(self.chat_stream(messages))

    def describe_image(self, png: bytes, prompt: str) -> str | None:
        """None means this provider cannot see images."""
        return None


class ClaudeCLIProvider(CLIProvider):
    name = "claude-cli"
    binary = "claude"
    context_tokens = 100_000

    def invocation(self, prompt: str) -> tuple[list[str], str | None]:
        # a bare -p reads the prompt from stdin
        argv = [self.binary, "-p", "--output-format", "text"]
        if self.model:
            argv += ["--model", self.model]
        return argv, prompt

    def describe_image(self, png: bytes, prompt: str) -> str | None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # resolve() matters on macOS: /var/folders is a symlink and the
            # CLI's permission rules match against resolved paths
            image = (Path(tmpdir) / "image.png").resolve()
            image.write_bytes(png)
            argv = [
                self.binary, "-p", "--output-format", "text",
                "--add-dir", str(image.parent),
                "--allowedTools", f"Read(//{str(image).lstrip('/')})",
            ]
            if self.model:
                argv += ["--model", self.model]
            stdin = f"Read the image file at {image}.\n\n{prompt}"
            return self.run(argv, stdin=stdin) or None


class CodexCLIProvider(CLIProvider):
    name = "codex-cli"
    binary = "codex"
    context_tokens = 100_000

    def invocation(self, prompt: str) -> tuple[list[str], str | None]:
        argv = [self.binary, "exec"]
        if self.model:
            argv += ["-m", self.model]
        argv.append("-")  # "-" reads the prompt from stdin
        return argv, prompt

    def describe_image(self, png: bytes, prompt: str) -> str | None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image = (Path(tmpdir) / "image.png").resolve()
            image.write_bytes(png)
            argv = [self.binary, "exec"]
            if self.model:
                argv += ["-m", self.model]
            # the prompt MUST precede -i or codex treats it as an image arg
            argv += [prompt, "-i", str(image)]
            return self.run(argv) or None


class CopilotCLIProvider(CLIProvider):
    """Copilot via the standalone `copilot` CLI, falling back to `gh copilot`.

    Text only: no image attachment support, so ``describe_image`` stays None.
    """

    name = "copilot-cli"
    binary = "copilot"
    context_tokens = 16_384
    fallback_binary = "gh"

    def available(self) -> bool:
        return (
            shutil.which(self.binary) is not None
            or shutil.which(self.fallback_binary) is not None
        )

    def status(self) -> str:
        if not self.available():
            return (
                f"Neither {self.binary} nor {self.fallback_binary} was found "
                f"on PATH. Install and sign in to one, or choose another "
                f"provider."
            )
        return f"{self.name} ready ({self.model or 'CLI default model'})."

    def invocation(self, prompt: str) -> tuple[list[str], str | None]:
        # no stdin-prompt support; the 16k context cap keeps argv well under
        # MAX_ARG_STRLEN
        if shutil.which(self.binary) is not None:
            argv = [self.binary, "-p", prompt]
        else:
            argv = [self.fallback_binary, "copilot", "-p", prompt]
        if self.model:
            argv += ["--model", self.model]
        return argv, None


PROVIDERS: dict[str, type[CLIProvider]] = {
    "claude-cli": ClaudeCLIProvider,
    "codex-cli": CodexCLIProvider,
    "copilot-cli": CopilotCLIProvider,
}


def get_provider(
    kind: str,
    model: str | None = None,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> CLIProvider:
    """Construct a provider by name.

    Raises:
        KeyError: if ``kind`` is not a known provider.
    """
    try:
        cls = PROVIDERS[kind]
    except KeyError:
        raise KeyError(
            f"unknown provider {kind!r}; expected one of "
            f"{sorted(PROVIDERS)}"
        ) from None
    return cls(model=model, timeout=timeout)


def available_providers(
    **kwargs,
) -> list[CLIProvider]:
    """Every provider whose CLI is actually installed, for a settings UI."""
    return [
        cls(**kwargs) for cls in PROVIDERS.values() if cls(**kwargs).available()
    ]
