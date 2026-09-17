"""Local LLM access via Ollama, with automatic model discovery."""

from .provider import (
    DEFAULT_BASE_URL,
    DEFAULT_CHAT_EXCLUDE,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_EMBED_MODELS,
    DEFAULT_MAX_PARAMS_B,
    DEFAULT_VISION_HINTS,
    EMBED_BATCH_SIZE,
    MAX_CONTEXT_WINDOW,
    OllamaProvider,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CHAT_EXCLUDE",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_EMBED_MODELS",
    "DEFAULT_MAX_PARAMS_B",
    "DEFAULT_VISION_HINTS",
    "EMBED_BATCH_SIZE",
    "MAX_CONTEXT_WINDOW",
    "OllamaProvider",
]
