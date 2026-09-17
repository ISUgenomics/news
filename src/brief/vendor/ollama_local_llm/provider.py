"""Local LLM access via a user-installed Ollama server.

Never ships or downloads model weights — it talks to whatever the user already
pulled. The interesting part is **automatic model selection**: an app that
demands a specific model name fails on every machine but the developer's, so
this inspects what is installed and picks something reasonable.

Selection rules, each with a reason:

* **chat** — the largest model at or under a parameter cap, because an
  interactive app needs first tokens in seconds and a 70B model on consumer
  hardware takes minutes. Vision, coder, and embedding specialists are excluded;
  they make poor general chat models.
* **embedding** — the first match from a preference list of known embedding
  models. Absent one, embeddings are simply unavailable and the caller falls
  back to keyword retrieval.
* **vision** — the *smallest* capable model, because describing an image is a
  one-shot request where latency dominates quality.

Every discovery call degrades to None rather than raising, so a missing server
is a state the UI can report rather than a crash.

No runtime dependencies — urllib only.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Iterator

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_CONTEXT_WINDOW = 8192
MAX_CONTEXT_WINDOW = 32_768

# an interactive app needs answers in seconds; above this, first tokens on
# consumer hardware take minutes
DEFAULT_MAX_PARAMS_B = 15.0

DEFAULT_EMBED_MODELS = ("nomic-embed-text", "mxbai-embed-large", "all-minilm")
DEFAULT_VISION_HINTS = ("vl", "vision", "llava", "minicpm-v", "moondream")
# specialists that make poor default *chat* models
DEFAULT_CHAT_EXCLUDE = ("vl", "vision", "llava", "minicpm-v", "coder", "embed")

EMBED_BATCH_SIZE = 64

_NETWORK_ERRORS = (urllib.error.URLError, OSError, ValueError)


class OllamaProvider:
    """Talks to a user-installed Ollama server."""

    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        chat_model: str | None = None,
        embed_model: str | None = None,
        vision_model: str | None = None,
        max_params_b: float = DEFAULT_MAX_PARAMS_B,
        embed_preferences: tuple[str, ...] = DEFAULT_EMBED_MODELS,
        vision_hints: tuple[str, ...] = DEFAULT_VISION_HINTS,
        chat_exclude: tuple[str, ...] = DEFAULT_CHAT_EXCLUDE,
        context_window: int | None = None,
        connect_timeout: float = 3.0,
        request_timeout: float = 300.0,
        options: dict | None = None,
        keep_alive: str | float | None = None,
    ):
        """Explicit model names skip discovery for that role entirely.

        ``options`` is passed straight through to Ollama's ``options`` object on
        every chat call. It is the only way to reach the sampler, and the
        defaults matter more than they look: Ollama uses temperature 0.8 and a
        fresh random seed, so two identical requests return different text.
        A caller that needs a reproducible answer passes
        ``{"temperature": 0, "seed": 42, "top_k": 1}``; this module does not
        choose that for you, because an interactive assistant wants the
        opposite of a nightly report.

        ``keep_alive`` is how long the server holds the model in memory after a
        request — "30m", or seconds as a number, or 0 to unload at once. The
        default is five minutes, which is short for a batch that does other work
        between calls: a 27B model costs tens of seconds to reload and the
        caller sees it as a mysteriously slow request.
        """
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._chat_model = chat_model or None
        self._embed_model = embed_model or None
        self._vision_model = vision_model or None
        self.max_params_b = max_params_b
        self.embed_preferences = embed_preferences
        self.vision_hints = vision_hints
        self.chat_exclude = chat_exclude
        self._context_window = context_window
        self.options = dict(options) if options else None
        self.keep_alive = keep_alive
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout

    # -------------------------------------------------------- plumbing ---

    def _get(self, path: str, timeout: float | None = None) -> dict:
        request = urllib.request.Request(self.base_url + path)
        with urllib.request.urlopen(
            request, timeout=timeout or self.connect_timeout
        ) as response:
            return json.loads(response.read())

    def _post(self, path: str, payload: dict, timeout: float | None = None):
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(
            request, timeout=timeout or self.request_timeout
        )

    # ------------------------------------------------------- discovery ---

    def installed_models(self) -> list[dict]:
        """Raw model records from the server. Empty list if unreachable."""
        try:
            return self._get("/api/tags").get("models", [])
        except _NETWORK_ERRORS:
            return []

    def installed_model_names(self) -> list[str]:
        """Model names, for a settings dropdown."""
        return [m.get("name", "") for m in self.installed_models()]

    @staticmethod
    def _params_b(model: dict) -> float:
        """Parameter count in billions, 0.0 when the server does not say."""
        raw = (model.get("details") or {}).get("parameter_size", "")
        try:
            return float(str(raw).rstrip("Bb"))
        except ValueError:
            return 0.0

    def chat_model(self) -> str | None:
        """Best general chat model installed, or None."""
        if self._chat_model:
            return self._chat_model
        candidates = [
            m for m in self.installed_models()
            if not any(x in m.get("name", "").lower() for x in self.chat_exclude)
        ]
        if not candidates:
            return None
        # largest model that still answers interactively; if none report a
        # size, fall back to the smallest known.
        # Ties break on name so the choice is stable — the server lists models
        # in modification order, which changes every time one is pulled.
        capped = [
            m for m in candidates
            if 0 < self._params_b(m) <= self.max_params_b
        ]
        if capped:
            pick = min(capped, key=lambda m: (-self._params_b(m), self._name(m)))
        else:
            pick = min(
                candidates,
                key=lambda m: (self._params_b(m) or 1e9, self._name(m)),
            )
        self._chat_model = pick.get("name")
        return self._chat_model

    @staticmethod
    def _name(model: dict) -> str:
        return model.get("name", "")

    def embed_model(self) -> str | None:
        """First installed model matching the embedding preference list."""
        if self._embed_model:
            return self._embed_model
        names = self.installed_model_names()
        for preference in self.embed_preferences:
            for name in names:
                if name.startswith(preference):
                    self._embed_model = name
                    return name
        return None

    def vision_model(self) -> str | None:
        """Smallest installed vision model — latency dominates here."""
        if self._vision_model:
            return self._vision_model
        candidates = [
            m for m in self.installed_models()
            if any(x in m.get("name", "").lower() for x in self.vision_hints)
        ]
        if not candidates:
            return None
        pick = min(
            candidates,
            key=lambda m: (self._params_b(m) or 1e9, self._name(m)),
        )
        self._vision_model = pick.get("name")
        return self._vision_model

    # ---------------------------------------------------------- status ---

    def available(self) -> bool:
        return self.chat_model() is not None

    def status(self) -> str:
        """A sentence to show the user, naming the next action if any."""
        try:
            self._get("/api/tags")
        except _NETWORK_ERRORS:
            return (
                "Ollama is not running. Install it from ollama.com, then run: "
                "ollama pull qwen3:8b"
            )
        model = self.chat_model()
        if not model:
            return (
                "Ollama is running but no chat model is installed. "
                "Run: ollama pull qwen3:8b"
            )
        if self.embed_model():
            return f"Local model ready: {model}."
        return (
            f"Local model ready: {model}. (No embedding model; using keyword "
            f"search. For better retrieval run: ollama pull nomic-embed-text)"
        )

    def context_window(self) -> int:
        """The chat model's context length, capped and with a safe default."""
        if self._context_window:
            return self._context_window
        model = self.chat_model()
        if model:
            try:
                with self._post("/api/show", {"model": model}, timeout=5.0) as r:
                    info = json.loads(r.read())
                for key, value in (info.get("model_info") or {}).items():
                    if key.endswith(".context_length"):
                        return min(int(value), MAX_CONTEXT_WINDOW)
            except Exception:
                # any failure here is non-fatal; the default is workable
                pass
        return DEFAULT_CONTEXT_WINDOW

    # ------------------------------------------------------- inference ---

    def chat_stream(
        self,
        messages: list[dict],
        *,
        response_format: dict | str | None = None,
    ) -> Iterator[str]:
        """Yield response tokens as they arrive.

        ``response_format`` is Ollama's ``format`` field: the string ``"json"``
        for "valid JSON, any shape", or a JSON Schema object for constrained
        decoding, where the sampler is restricted to tokens that keep the reply
        conformant. A schema makes malformed JSON and undeclared keys
        impossible rather than unlikely, so the caller stops paying for a retry
        loop it cannot win.

        It does not make the content correct. Measured against a 27B model told
        to violate its schema: constrained, it obeyed the shape and padded a
        section with junk to fill it. Constraining relocates the error from
        "unparseable" to "well-formed and wrong", so keep validating the object
        you get back.

        Not every server or model supports a schema here; one that does not
        answers with an HTTP error rather than ignoring the field, which is the
        behaviour you want — a silent downgrade to unconstrained decoding would
        look identical to success.

        Raises:
            RuntimeError: if no chat model is available.
        """
        model = self.chat_model()
        if not model:
            raise RuntimeError(self.status())
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            # reasoning models otherwise emit minutes of hidden thought before
            # the first useful token; see the streaming-think-filter feature
            # if you would rather keep it and strip it client-side
            "think": False,
        }
        if response_format is not None:
            payload["format"] = response_format
        self._apply_call_options(payload)
        try:
            response = self._post("/api/chat", payload)
        except urllib.error.HTTPError:
            # older servers and non-reasoning models reject the flag
            del payload["think"]
            response = self._post("/api/chat", payload)
        with response:
            for line in response:
                if not line.strip():
                    continue
                data = json.loads(line)
                token = (data.get("message") or {}).get("content", "")
                if token:
                    yield token
                if data.get("done"):
                    break

    def _apply_call_options(self, payload: dict) -> None:
        """Add ``options`` and ``keep_alive`` to a chat payload, when set.

        Both are omitted entirely when unset rather than sent as null, because
        older servers reject unknown nulls and the whole point of this module is
        that a missing capability degrades rather than raises.
        """
        if self.options:
            payload["options"] = dict(self.options)
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive

    def complete(
        self,
        messages: list[dict],
        *,
        response_format: dict | str | None = None,
    ) -> str:
        """Convenience for callers that do not want an iterator.

        ``response_format`` is forwarded to ``chat_stream``; see it for what a
        JSON Schema here does and does not buy you.
        """
        return "".join(
            self.chat_stream(messages, response_format=response_format)
        )

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed ``texts``, or None if embeddings are unavailable.

        Batched: one request for a whole long document blows the timeout and
        silently degrades retrieval to keyword mode.

        Returns None — never a partial list — on any failure, so the caller
        never mixes embedded and un-embedded content. A partially embedded
        corpus is worse than none: the un-embedded parts score zero in cosine
        ranking and vanish from retrieval without any error.
        """
        model = self.embed_model()
        if not model or not texts:
            return None if not model else []
        out: list[list[float]] = []
        try:
            for start in range(0, len(texts), EMBED_BATCH_SIZE):
                batch = texts[start:start + EMBED_BATCH_SIZE]
                with self._post(
                    "/api/embed", {"model": model, "input": batch},
                    timeout=120.0,
                ) as response:
                    embeddings = json.loads(response.read()).get("embeddings")
                if not embeddings or len(embeddings) != len(batch):
                    return None
                out.extend(embeddings)
        except _NETWORK_ERRORS:
            return None
        return out

    def describe_image(self, png: bytes, prompt: str) -> str | None:
        """Describe an image, or None if no vision model is installed."""
        model = self.vision_model()
        if not model:
            return None
        with self._post("/api/chat", {
            "model": model,
            "messages": [{
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(png).decode()],
            }],
            "stream": False,
        }) as response:
            data = json.loads(response.read())
        return ((data.get("message") or {}).get("content") or "").strip() or None
