"""The Databricks model host and volume mirror, against fakes: no workspace, no SDK calls."""

from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from legistutor import check, config, databricks, embed, llm


def _sse(*events, done=True):
    lines = [f"data: {json.dumps(event)}" for event in events] + (["data: [DONE]"] if done else [])
    return httpx.Response(200, content="\n\n".join(lines).encode(), headers={"content-type": "text/event-stream"})


def _delta(content, finish=None):
    return {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(databricks.time, "sleep", lambda seconds: None)


def test_provider_follows_where_the_code_runs(monkeypatch):
    for name in ("LEGISTUTOR_PROVIDER", "DATABRICKS_RUNTIME_VERSION", "DATABRICKS_APP_NAME", "LEGISTUTOR_CHAT_MODEL", "LEGISTUTOR_EMBED_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert config.settings().provider == "ollama" and config.settings().embed_model == config.DEFAULT_EMBED_MODEL
    monkeypatch.setenv("DATABRICKS_APP_NAME", "legistutor")
    settings = config.settings()
    assert (settings.provider, settings.chat_model, settings.embed_model) == (
        "databricks", config.DATABRICKS_CHAT_ENDPOINT, config.DATABRICKS_EMBED_ENDPOINT,
    )  # fmt: skip
    assert settings.temperature is None
    monkeypatch.setenv("LEGISTUTOR_PROVIDER", "ollama")  # said outright wins
    assert config.settings().provider == "ollama"
    monkeypatch.setenv("LEGISTUTOR_PROVIDER", "elsewhere")
    with pytest.raises(ValueError, match="ollama, databricks"):
        config.settings()


def test_data_dir_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("LEGISTUTOR_DATA_DIR", raising=False)
    assert config.data_dir().as_posix() == "data"
    monkeypatch.setenv("LEGISTUTOR_DATA_DIR", "/Volumes/main/civic/legistutor")
    assert databricks.is_volume_path(config.data_dir()) and not databricks.is_volume_path("data")


def test_embedder_posts_to_the_endpoint_and_keeps_the_order_asked_for():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        rows = [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [3.0, 4.0]}]  # out of order
        return httpx.Response(200, json={"data": rows[-len(seen[-1][1]["input"]) :]})

    embedder = embed.DatabricksEmbedder("databricks-qwen3-embedding-0-6b", transport=httpx.MockTransport(handler))
    assert embedder.embed_documents(["first", "second"]) == [[0.6, 0.8], [0.0, 1.0]]
    assert seen[0] == ("/serving-endpoints/databricks-qwen3-embedding-0-6b/invocations", {"input": ["first", "second"]})
    embedder.embed_query("a question")
    assert seen[1][1]["input"][0].endswith("\nQuery: a question")  # the same prefix the local model gets


def test_embedder_waits_out_a_rate_limit_and_explains_refusals():
    calls = []

    def busy_then_fine(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"message": "slow down"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    assert embed.DatabricksEmbedder("e", transport=httpx.MockTransport(busy_then_fine)).embed_documents(["x"]) == [[1.0]]
    assert len(calls) == 3
    for status, words in ((404, "no serving endpoint named 'e'"), (403, "CAN QUERY"), (429, "rate limited"), (500, "error 500")):
        refusing = embed.DatabricksEmbedder("e", transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s, text="no")), attempts=2)
        with pytest.raises(embed.EmbeddingError, match=words):
            refusing.embed_documents(["x"])


def test_chat_streams_text_and_sends_no_temperature_unless_set():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _sse(
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            _delta("Adopted "),
            _delta([{"type": "reasoning", "summary": [{"text": "pondering"}]}, {"type": "text", "text": "[1]."}]),
            _delta(None, "stop"),
        )

    transport = httpx.MockTransport(handler)
    chat = llm.DatabricksChat("databricks-claude-sonnet-5-5", context_tokens=1000, max_tokens=500, transport=transport)
    assert "".join(chat.stream("system text", "user text")) == "Adopted [1]."
    assert seen[0] == {
        "messages": [{"role": "system", "content": "system text"}, {"role": "user", "content": "user text"}],
        "max_tokens": 500,
        "stream": True,
    }
    assert (chat.name, chat.model, chat.context_chars) == ("databricks", "databricks-claude-sonnet-5-5", 3500)
    list(llm.DatabricksChat("m", temperature=0.2, transport=transport).stream("s", "u"))
    assert seen[1]["temperature"] == 0.2


def test_chat_reports_a_cut_off_answer_a_refusal_and_retries_when_busy():
    cut = llm.DatabricksChat("m", max_tokens=5, transport=httpx.MockTransport(lambda r: _sse(_delta("partial"), _delta(None, "length"))))
    with pytest.raises(llm.LLMError, match="LEGISTUTOR_MAX_TOKENS"):
        list(cut.stream("s", "u"))
    missing = llm.DatabricksChat("nope", transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"message": "x"})))
    with pytest.raises(llm.LLMError, match="no serving endpoint named 'nope'"):
        list(missing.stream("s", "u"))
    calls = []

    def busy_once(request):
        calls.append(1)
        return httpx.Response(429, json={"message": "busy"}) if len(calls) == 1 else _sse(_delta("Fine."), _delta(None, "stop"))

    assert "".join(llm.DatabricksChat("m", transport=httpx.MockTransport(busy_once)).stream("s", "u")) == "Fine."


def test_requests_carry_the_workspace_credentials():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={})

    auth = databricks._Auth(SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer from-the-sdk"}))
    httpx.Client(auth=auth, transport=httpx.MockTransport(handler)).get("https://workspace.test/x")
    assert seen["authorization"] == "Bearer from-the-sdk"


class FakeFiles:
    """The part of the SDK's Files API the mirror uses, over a dict of path -> bytes."""

    def __init__(self, files):
        self.files, self.uploaded, self.downloads = dict(files), {}, []

    def list_directory_contents(self, directory):
        if not any(path.startswith(directory + "/") for path in self.files):
            raise FileNotFoundError(directory)
        names = {path[len(directory) + 1 :].split("/")[0] for path in self.files if path.startswith(directory + "/")}
        for name in sorted(names):
            path = f"{directory}/{name}"
            is_file = path in self.files
            yield SimpleNamespace(
                path=path, name=name, is_directory=not is_file, file_size=len(self.files[path]) if is_file else None,
                last_modified=1_700_000_000_000 if is_file else None,
            )  # fmt: skip

    def download(self, path):
        self.downloads.append(path)
        return SimpleNamespace(contents=io.BytesIO(self.files[path]))

    def create_directory(self, path):
        pass

    def upload(self, path, contents, overwrite=False):
        self.uploaded[path] = contents.read()


def test_mirror_copies_the_served_layers_down_once_and_summaries_back(tmp_path):
    root = "/Volumes/main/civic/legistutor"
    files = FakeFiles(
        {
            f"{root}/gold/legistar/c/chunks.parquet": b"vectors",
            f"{root}/silver/legistar/c/graph/vertices.parquet": b"graph",
            f"{root}/bronze/legistar/c/matters/1.json": b"raw",
        }
    )
    mirror = databricks.Mirror(files, root, tmp_path)
    assert mirror.pull(("silver", "gold", "not-built-yet"))["downloaded"] == 2
    assert (tmp_path / "gold/legistar/c/chunks.parquet").read_bytes() == b"vectors"
    assert not (tmp_path / "bronze").exists()  # the page does not read it
    assert mirror.pull() == {"downloaded": 0, "unchanged": 2, "bytes": 0}
    files.files[f"{root}/gold/legistar/c/chunks.parquet"] = b"new vectors"  # a refresh ran
    assert mirror.pull()["downloaded"] == 1

    note = tmp_path / "gold/legistar/c/law_summaries/mnstat_383B.json"
    note.parent.mkdir(parents=True)
    note.write_text("{}", encoding="utf-8")
    mirror.push(note)
    assert files.uploaded == {f"{root}/gold/legistar/c/law_summaries/mnstat_383B.json": b"{}"}
    files.upload = lambda *a, **k: (_ for _ in ()).throw(PermissionError("no WRITE VOLUME"))
    mirror.push(note)  # logged; the page carries on


def test_a_directory_on_disk_needs_no_mirror(tmp_path):
    assert databricks.mirror_if_needed(tmp_path) == (tmp_path, None)


def test_check_reports_each_dependency_and_counts_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISTUTOR_PROVIDER", "ollama")
    monkeypatch.setattr(check, "_embedding", lambda settings: "e: 4 dimensions")
    monkeypatch.setattr(check, "_chat", lambda settings: (_ for _ in ()).throw(llm.LLMError("no endpoint")))
    monkeypatch.setattr(check, "_keyword_search", lambda: "loaded")
    blocked = httpx.MockTransport(lambda r: httpx.Response(200) if "legistar" in r.url.host else httpx.Response(407))
    lines = []
    assert check.run(tmp_path, "c", transport=blocked, out=lines.append) == 4  # the chat model and three sites
    text = "\n".join(lines)
    assert "ok    data directory" in text and "writable" in text and os.listdir(tmp_path) == []
    assert "FAIL  chat model: no endpoint" in text and "ok    Legistar API" in text and "FAIL  Minnesota Revisor: HTTP 407" in text
    assert "ok    search index: none yet" in text
