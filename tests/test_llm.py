"""The model hosts, exercised against fake transports: no network, no GPU, no API key."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from legistar_scraper import config, llm
from legistar_scraper.embed import EmbeddingError, OllamaEmbedder, query_prefix


def test_query_prefix_by_model_family():
    assert query_prefix("qwen3-embedding:0.6b").startswith("Instruct: ") and query_prefix("qwen3-embedding:0.6b").endswith("\nQuery: ")
    assert query_prefix("databricks-qwen3-embedding-0-6b") == query_prefix("qwen3-embedding:0.6b")
    assert query_prefix("bge_large_en_v1_5") == "Represent this sentence for searching relevant passages: "
    assert query_prefix("gte_large_en_v1_5") == ""


def test_ollama_embedder_normalises_and_prefixes_queries_only():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"embeddings": [[3.0, 4.0] for _ in body["input"]]})

    embedder = OllamaEmbedder("qwen3-embedding:0.6b", transport=httpx.MockTransport(handler))
    assert embedder.embed_documents(["a passage"]) == [[0.6, 0.8]]
    assert embedder.embed_query("a question") == [0.6, 0.8]
    assert seen[0]["input"] == ["a passage"] and seen[0]["truncate"] is False
    assert seen[1]["input"][0].endswith("\nQuery: a question")


def test_ollama_embedder_errors_say_what_to_do():
    missing = OllamaEmbedder("nope", transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "not found"})))
    with pytest.raises(EmbeddingError, match="ollama pull nope"):
        missing.embed_documents(["x"])

    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(EmbeddingError, match="Is it running"):
        OllamaEmbedder("m", transport=httpx.MockTransport(refuse)).embed_documents(["x"])


def _ndjson(*events):
    return httpx.Response(200, content="\n".join(json.dumps(e) for e in events).encode())


def test_ollama_chat_streams_and_asks_for_its_context_window():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return _ndjson(
            {"message": {"content": "Adopted "}}, {"message": {"content": "[1]."}},
            {"message": {"content": ""}, "done": True, "prompt_eval_count": 900},
        )

    chat = llm.OllamaChat("gemma3:12b", num_ctx=8192, transport=httpx.MockTransport(handler))
    assert "".join(chat.stream("system text", "user text")) == "Adopted [1]."
    assert seen["options"]["num_ctx"] == 8192
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert chat.context_chars == int((8192 - 1500) * 3.5)


def test_ollama_chat_reports_a_full_window_and_a_missing_model():
    full = llm.OllamaChat("m", num_ctx=4096, transport=httpx.MockTransport(
        lambda r: _ndjson({"message": {"content": "partial"}}, {"done": True, "prompt_eval_count": 4096})))
    with pytest.raises(llm.LLMError, match="window"):
        list(full.stream("s", "u"))
    missing = llm.OllamaChat("nope", transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "x"})))
    with pytest.raises(llm.LLMError, match="ollama pull nope"):
        list(missing.stream("s", "u"))


class _FakeStream:
    def __init__(self, texts, stop_reason="end_turn"):
        self.text_stream = iter(texts)
        self._final = SimpleNamespace(stop_reason=stop_reason, stop_details=SimpleNamespace(category="cyber"), usage=None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, calls, label, stop_reason):
        self._calls, self._label, self._stop_reason = calls, label, stop_reason

    def stream(self, **kwargs):
        self._calls.append((self._label, kwargs))
        return _FakeStream(["An ", "answer."], self._stop_reason)


def _anthropic_chat(model, stop_reason="end_turn"):
    chat = llm.AnthropicChat(model, effort="medium")
    calls = []
    chat._client = SimpleNamespace(
        messages=_FakeMessages(calls, "stable", stop_reason),
        beta=SimpleNamespace(messages=_FakeMessages(calls, "beta", stop_reason)),
    )
    return chat, calls


def test_anthropic_request_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    chat, calls = _anthropic_chat("claude-sonnet-5-5")
    assert "".join(chat.stream("system text", "user text")) == "An answer."
    label, request = calls[0]
    assert label == "beta"  # Sonnet gets the server-side refusal fallback
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["system"] == "system text" and request["messages"] == [{"role": "user", "content": "user text"}]
    assert request["output_config"] == {"effort": "medium"}
    assert "thinking" not in request and "temperature" not in request

    chat, calls = _anthropic_chat("claude-haiku-5-5")
    list(chat.stream("s", "u"))
    label, request = calls[0]
    assert label == "stable" and "fallbacks" not in request and "betas" not in request


def test_anthropic_refusal_is_an_error_not_an_empty_answer(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    chat, _ = _anthropic_chat("claude-haiku-5-5", stop_reason="refusal")
    with pytest.raises(llm.LLMError, match="declined"):
        list(chat.stream("s", "u"))


def test_settings_and_dotenv(tmp_path, monkeypatch):
    for name in ("LEGISTAR_LLM_PROVIDER", "LEGISTAR_OLLAMA_MODEL", "OLLAMA_HOST", "EXAMPLE_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEGISTAR_OLLAMA_MODEL", "from-shell")
    env = tmp_path / ".env"
    env.write_text('# comment\nEXAMPLE_KEY="quoted value"\nLEGISTAR_OLLAMA_MODEL=from-file\nOLLAMA_HOST=0.0.0.0:11434\n', encoding="utf-8")
    config.load_dotenv(env)
    settings = config.settings()
    assert settings.llm_provider == "anthropic" and settings.anthropic_model == "claude-sonnet-5-5"
    assert settings.ollama_model == "from-shell"  # the shell wins over .env
    assert settings.ollama_host == "http://127.0.0.1:11434"
    import os

    assert os.environ["EXAMPLE_KEY"] == "quoted value"
    with pytest.raises(llm.LLMError, match="Unknown LLM provider"):
        llm.provider(settings, "other")
