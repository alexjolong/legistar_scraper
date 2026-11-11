"""Chat models behind one small interface: Claude through the Anthropic API, or a
local model through Ollama. Everything above this module only sees
:class:`ChatProvider`, so a Databricks model serving endpoint can be added as a
third class without touching retrieval or prompting.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# Claude models for which the API can re-run a declined request on another model.
_SERVER_FALLBACK_MODELS = frozenset(
    {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1"}
)
_SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """A failure worth showing to the person at the keyboard as-is."""


class ChatProvider(Protocol):
    name: str
    model: str
    context_chars: int  # rough size of prompt the model can take, in characters

    def stream(self, system: str, user: str) -> Iterator[str]:
        """Yield the answer as it is generated."""
        ...


class AnthropicChat:
    name = "anthropic"
    context_chars = 1_500_000  # a 1M-token window; retrieval never gets near it

    def __init__(self, model: str, effort: str = "medium") -> None:
        import anthropic

        self.model = model
        self.effort = effort
        self._anthropic = anthropic
        # Credentials come from the environment: ANTHROPIC_API_KEY, or a profile
        # saved by `ant auth login`.
        self._client = anthropic.Anthropic()

    def stream(self, system: str, user: str) -> Iterator[str]:
        anthropic = self._anthropic
        request = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": self.effort},
        }
        if self.model in _SERVER_FALLBACK_MODELS:
            # If a safety classifier declines the request, let the API retry it
            # on its default fallback model inside the same call.
            manager = self._client.beta.messages.stream(**request, betas=[_SERVER_FALLBACK_BETA], fallbacks="default")
        else:
            manager = self._client.messages.stream(**request)
        try:
            with manager as stream:
                yield from stream.text_stream
                final = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMError("The Anthropic API rejected the API key. Check ANTHROPIC_API_KEY (shell or .env).") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"The API key is not allowed to use {self.model}: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"The Anthropic API does not know the model {self.model!r}.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("The Anthropic API rate limit was hit. Wait a moment and ask again.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc
        except TypeError as exc:
            if "authentication" not in str(exc):
                raise
            # The SDK's way of saying it found no credentials anywhere.
            raise LLMError(
                "No Anthropic API key found. Put ANTHROPIC_API_KEY=... in a .env file here (or set it in the shell), "
                "or answer with the local model: --provider ollama."
            ) from exc
        if final.stop_reason == "refusal":
            category = getattr(final.stop_details, "category", None)
            raise LLMError(f"The model declined to answer (category: {category or 'unspecified'}).")
        if final.stop_reason == "max_tokens":
            raise LLMError("The answer was cut off at the output limit.")
        log.debug("anthropic usage: %s", final.usage)


class OllamaChat:
    name = "ollama"

    def __init__(
        self,
        model: str,
        host: str = "http://127.0.0.1:11434",
        *,
        num_ctx: int = 16384,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.num_ctx = num_ctx
        # Leave room for the answer, and assume a dense ~3.5 characters per token.
        self.context_chars = int((num_ctx - 1500) * 3.5)
        self._host = host
        self._http = httpx.Client(base_url=host, timeout=httpx.Timeout(600.0, connect=10.0), transport=transport)

    def stream(self, system: str, user: str) -> Iterator[str]:
        payload = {
            "model": self.model,
            "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # Ollama's default window is small and it drops what does not fit
            # without saying so; ask for the window explicitly.
            "options": {"num_ctx": self.num_ctx, "temperature": 0.2},
        }
        try:
            with self._http.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code == 404:
                    raise LLMError(f"Ollama does not have the model {self.model!r}. Run: ollama pull {self.model}")
                if response.status_code != 200:
                    raise LLMError(f"Ollama error {response.status_code}: {response.read().decode(errors='replace')[:300]}")
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise LLMError(f"Ollama error: {event['error']}")
                    text = event.get("message", {}).get("content")
                    if text:
                        yield text
                    if event.get("done"):
                        prompt_tokens = event.get("prompt_eval_count") or 0
                        log.debug("ollama prompt tokens: %s of %s", prompt_tokens, self.num_ctx)
                        if prompt_tokens >= self.num_ctx - 16:
                            raise LLMError(
                                f"The prompt filled the local model's {self.num_ctx}-token window, so part of it was "
                                "dropped and the answer above may be missing sources. Use a smaller --top or raise "
                                "LEGISTAR_OLLAMA_NUM_CTX."
                            )
        except httpx.TransportError as exc:
            raise LLMError(f"Cannot reach Ollama at {self._host}. Is it running? ({exc})") from exc


def provider(settings: Settings, name: str | None = None, model: str | None = None) -> ChatProvider:
    name = (name or settings.llm_provider).lower()
    if name == "anthropic":
        return AnthropicChat(model or settings.anthropic_model, effort=settings.effort)
    if name == "ollama":
        return OllamaChat(model or settings.ollama_model, settings.ollama_host, num_ctx=settings.ollama_num_ctx)
    raise LLMError(f"Unknown LLM provider {name!r}. Use 'anthropic' or 'ollama'.")
