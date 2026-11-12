"""The chat model that writes answers: a local model served by Ollama, or a Databricks
model serving endpoint.

Everything above this module only sees :class:`ChatProvider`, so another host is one
more class here and nothing else changes.
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


class DatabricksChat:
    """A chat model behind a Databricks model serving endpoint; ``model`` is the endpoint's name.

    Foundation model and external model endpoints all take the same chat request and
    stream the answer back as server-sent events, whichever model is behind them.
    """

    name = "databricks"

    def __init__(
        self,
        endpoint: str,
        *,
        context_tokens: int = 60000,
        max_tokens: int = 4000,
        temperature: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        from . import databricks

        self.model = endpoint
        self.max_tokens = max_tokens
        self.temperature = temperature  # None leaves the endpoint's default; some models refuse the setting
        self.context_chars = int(context_tokens * 3.5)
        self._databricks = databricks
        try:
            self._http = databricks.serving_client(timeout=httpx.Timeout(600.0, connect=10.0), transport=transport)
        except databricks.DatabricksError as exc:
            raise LLMError(str(exc)) from exc

    def stream(self, system: str, user: str) -> Iterator[str]:
        payload: dict = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        attempts = 4
        for attempt in range(1, attempts + 1):
            try:
                with self._http.stream("POST", f"{self.model}/invocations", json=payload) as response:
                    if response.status_code in self._databricks.RETRY_STATUSES and attempt < attempts:
                        response.read()
                        log.warning("%s is busy (%s); waiting to try again", self.model, response.status_code)
                        self._databricks.wait_before_retry(response, attempt)
                        continue
                    if response.status_code != 200:
                        response.read()
                        raise LLMError(self._databricks.problem(response, self.model))
                    yield from self._events(response)
                    return
            except httpx.TransportError as exc:
                raise LLMError(f"Cannot reach the Databricks workspace at {self._http.base_url.host}. ({exc})") from exc

    def _events(self, response: httpx.Response) -> Iterator[str]:
        finish = None
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            if event.get("error"):
                raise LLMError(f"{self.model}: {event['error']}")
            for choice in event.get("choices", []):
                finish = choice.get("finish_reason") or finish
                yield from _text_of((choice.get("delta") or {}).get("content"))
        if finish == "length":
            raise LLMError(
                f"The answer above stopped at the {self.max_tokens}-token limit and is incomplete. Raise LEGISTUTOR_MAX_TOKENS."
            )


def _text_of(content) -> Iterator[str]:
    """A delta's text. It is a string, or, from a model that reasons first, a list of
    parts of which only the text ones are shown."""
    if isinstance(content, str):
        if content:
            yield content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                yield part["text"]


def chat_model(settings: Settings, model: str | None = None) -> ChatProvider:
    if settings.provider == "databricks":
        return DatabricksChat(
            model or settings.chat_model,
            context_tokens=settings.context_tokens,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
        )
    return OllamaChat(
        model or settings.chat_model, settings.ollama_host, num_ctx=settings.ollama_num_ctx, think=settings.ollama_think
    )
