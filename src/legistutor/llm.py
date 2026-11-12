"""The chat model that writes answers: a local model served by Ollama.

Everything above this module only sees :class:`ChatProvider`, so another host (a
Databricks model serving endpoint, say) is one more class here and nothing else
changes.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """A failure worth showing to the person at the keyboard as-is."""


class ChatProvider(Protocol):
    name: str
    model: str
    context_chars: int  # rough size of prompt the model can take, in characters

    def stream(self, system: str, user: str) -> Iterator[str]:
        """Yield the answer as it is generated."""
        ...


class OllamaChat:
    name = "ollama"

    def __init__(
        self,
        model: str,
        host: str = "http://127.0.0.1:11434",
        *,
        num_ctx: int = 16384,
        think: bool | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.num_ctx = num_ctx
        self.think = think  # None leaves a reasoning model at its own default
        # Leave room for the answer, and assume a dense ~3.5 characters per token.
        self.context_chars = int((num_ctx - 1500) * 3.5)
        self._host = host
        self._http = httpx.Client(base_url=host, timeout=httpx.Timeout(600.0, connect=10.0), transport=transport)

    def stream(self, system: str, user: str) -> Iterator[str]:
        """Text as it is generated. If Ollama's model runner dies before producing anything
        (it does, now and then, when memory is tight while another model is loading), Ollama
        starts a new one on the next request, so the request is made once more."""
        for attempt in (1, 2):
            produced = False
            try:
                for text in self._stream_once(system, user):
                    produced = True
                    yield text
                return
            except LLMError as exc:
                if attempt == 2 or produced or "runner" not in str(exc):
                    raise
                log.warning("Ollama's model runner stopped; trying once more (%s)", exc)
                time.sleep(2)

    def _stream_once(self, system: str, user: str) -> Iterator[str]:
        payload = {
            "model": self.model,
            "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # Ollama's default window is small and it drops what does not fit
            # without saying so; ask for the window explicitly.
            "options": {"num_ctx": self.num_ctx, "temperature": 0.2},
        }
        if self.think is not None:
            payload["think"] = self.think
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
                    # A reasoning model's thinking arrives in a separate field and is not shown.
                    text = event.get("message", {}).get("content")
                    if text:
                        yield text
                    if event.get("done"):
                        prompt_tokens = event.get("prompt_eval_count") or 0
                        log.debug("ollama prompt tokens: %s of %s", prompt_tokens, self.num_ctx)
                        if prompt_tokens >= self.num_ctx - 16:
                            raise LLMError(
                                f"The prompt filled the model's {self.num_ctx}-token window, so part of it was "
                                "dropped and the answer above may be missing sources. Use a smaller --top or raise "
                                "LEGISTUTOR_OLLAMA_NUM_CTX."
                            )
        except httpx.TransportError as exc:
            raise LLMError(f"Cannot reach Ollama at {self._host}. Is it running? ({exc})") from exc


def chat_model(settings: Settings, model: str | None = None) -> ChatProvider:
    return OllamaChat(
        model or settings.chat_model, settings.ollama_host, num_ctx=settings.ollama_num_ctx, think=settings.ollama_think
    )
