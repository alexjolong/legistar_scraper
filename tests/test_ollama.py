"""Starting Ollama before it is needed, against a fake server: nothing is launched."""

from __future__ import annotations

import httpx
import pytest

from legistutor import ollama

HOST = "http://127.0.0.1:11434"


def _server(models, *, down_for=0):
    """A transport that refuses the first ``down_for`` requests, then lists ``models``."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= down_for:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"models": [{"name": name} for name in models]})

    return httpx.MockTransport(handler)


def _never(host):
    raise AssertionError("should not try to start Ollama")


def test_a_running_server_with_the_models_is_left_alone():
    ollama.ensure_running(HOST, ["qwen3-embedding:0.6b", "gemma4"], transport=_server(["qwen3-embedding:0.6b", "gemma4:latest"]), start=_never)


def test_a_stopped_local_server_is_started_and_waited_for(monkeypatch):
    monkeypatch.setattr(ollama.time, "sleep", lambda seconds: None)
    started = []
    ollama.ensure_running(HOST, ["m:1"], transport=_server(["m:1"], down_for=3), start=lambda host: started.append(host) or True)
    assert started == [HOST]


def test_failures_say_what_to_do(monkeypatch):
    monkeypatch.setattr(ollama.time, "sleep", lambda seconds: None)
    down = _server([], down_for=10**9)
    with pytest.raises(ollama.OllamaUnavailable, match="ollama pull gemma4:12b"):
        ollama.ensure_running(HOST, ["m:1", "gemma4:12b"], transport=_server(["m:1"]), start=_never)
    with pytest.raises(ollama.OllamaUnavailable, match="was not found"):
        ollama.ensure_running(HOST, transport=down, start=lambda host: False)
    with pytest.raises(ollama.OllamaUnavailable, match="did not answer"):
        ollama.ensure_running(HOST, transport=down, start=lambda host: True, wait_seconds=0.01)
    with pytest.raises(ollama.OllamaUnavailable, match="another machine"):
        ollama.ensure_running("http://gpu-box:11434", transport=down, start=_never)
