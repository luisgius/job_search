"""The local model: the one chain entry served from this machine.

Features that write free text per job (interview prep, company research,
application review, profile expansion) run only here. On a hosted endpoint
each would be a per-job cost nobody approved, so there is no fallback to one:
no local model configured is an error, never a detour to a paid model.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .llm import LLMClient, LLMError, model_entries

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


class LocalModelError(LLMError):
    """No local model is configured, or it could not answer."""


def local_model(config: Any) -> dict[str, Any]:
    """The first `scoring`/`tailoring` chain entry whose base_url is local."""
    for role in ("scoring", "tailoring"):
        for entry in model_entries(config, role):
            host = (urlsplit(entry.get("base_url") or "").hostname or "").lower()
            if host in LOCAL_HOSTS:
                return entry
    raise LocalModelError(
        "no local model configured: add an Ollama entry such as "
        '{model: "qwen3.8:27b", base_url: "http://localhost:11434/v1", timeout: 600} '
        "to scoring.fallback_models. This feature never uses a hosted model."
    )


def local_client(entry: dict[str, Any]) -> LLMClient:
    """An `LLMClient` for `entry`, speaking OpenAI-compatible HTTP to it."""
    return LLMClient(
        "ollama", provider="openrouter", base_url=entry["base_url"],
        timeout=entry.get("timeout") or 600, max_retries=0,
        reasoning_effort=entry.get("reasoning_effort"),
    )


def complete_locally(config: Any, *, system: str, prompt: str, max_tokens: int,
                     temperature: float = 0.3, client: Any = None) -> tuple[str, str]:
    """One local completion: `(text, model name)`. `client` is the test seam."""
    entry = local_model(config)
    text = (client or local_client(entry)).complete(
        model=entry["model"], system=system, prompt=prompt,
        max_tokens=max_tokens, temperature=temperature)
    text = str(text or "").strip()
    if not text:
        raise LocalModelError(f"{entry['model']} returned nothing")
    return text, entry["model"]
