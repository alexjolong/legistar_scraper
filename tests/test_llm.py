"""The model host, exercised against fake transports: no network, no GPU."""

from __future__ import annotations

import json
import httpx
import pytest

from legistutor import config, llm
from legistutor.embed import EmbeddingError, OllamaEmbedder, query_prefix


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


def test_thinking_is_only_sent_when_set_and_never_shown():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _ndjson({"message": {"content": "", "thinking": "pondering"}}, {"message": {"content": "Answer."}}, {"done": True})

    transport = httpx.MockTransport(handler)
    assert "".join(llm.OllamaChat("m", transport=transport).stream("s", "u")) == "Answer."
    list(llm.OllamaChat("m", think=False, transport=transport).stream("s", "u"))
    assert "think" not in seen[0] and seen[1]["think"] is False


def test_settings(monkeypatch):
    for name in ("LEGISTUTOR_CHAT_MODEL", "LEGISTUTOR_OLLAMA_THINK", "LEGISTUTOR_OLLAMA_NUM_CTX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    settings = config.settings()
    assert settings.chat_model == config.DEFAULT_CHAT_MODEL and settings.ollama_think is False
    assert settings.ollama_host == "http://127.0.0.1:11434" and settings.ollama_num_ctx == 16384
    monkeypatch.setenv("LEGISTUTOR_CHAT_MODEL", "other:7b")
    monkeypatch.setenv("LEGISTUTOR_OLLAMA_THINK", "true")
    chat = llm.chat_model(config.settings())
    assert (chat.model, chat.think) == ("other:7b", True)
    monkeypatch.setenv("LEGISTUTOR_OLLAMA_THINK", "")
    assert config.settings().ollama_think is None  # leave it to the model
    assert llm.chat_model(config.settings(), "named:1b").model == "named:1b"
