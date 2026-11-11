"""The web page's API, over the same tiny record the search tests use."""

from __future__ import annotations

import json
import threading

import httpx
import pytest
from test_search import C, FakeEmbedder, _write_bronze
from test_silver import BOARD, _event, _item

from legistutor import gold, llm, rag, silver, web
from legistutor.bronze import BronzeStore
from legistutor.search import Searcher


class FakeChat:
    name, model, context_chars = "fake", "fake-chat", 100_000

    def __init__(self, pieces=("Adopted ", "[1]."), fail=None):
        self.pieces, self.fail, self.prompts = pieces, fail, []

    def stream(self, system, user):
        self.prompts.append(user)
        yield from self.pieces
        if self.fail:
            raise llm.LLMError(self.fail)


@pytest.fixture
def app(tmp_path):
    _write_bronze(tmp_path)
    embedder = FakeEmbedder()
    gold.build(tmp_path, C, embedder)
    chat = FakeChat()
    application = web.App(Searcher(tmp_path, C, embedder), lambda: chat, client=C, embed_model=embedder.model)
    application.fake_chat = chat
    return application


def test_meta_lists_filter_choices(app):
    meta = app.meta()
    assert (meta["matters"], meta["meetings"], meta["passages"]) == (6, 3, 9)
    assert meta["chat_model"] == "fake-chat" and meta["embed_model"] == "fake-embedder"
    assert meta["bodies"] == ["Board", "Public Works Committee"]
    assert "Public Works - Roads" in meta["departments"] and "Resolution" in meta["matter_types"]


def test_search_is_json_and_takes_filters(app):
    data = json.loads(web.to_json(app.search({"q": "26-0001", "top": "2"})))
    assert [hit["file_number"] for hit in data["hits"]] == ["26-0001", "26-0001 R1"]
    assert data["hits"][0]["history"][0]["date"] == "2026-02-03"
    filtered = app.search({"q": "agreement shelter library", "department": "public works"})
    assert {hit.file_number for hit in filtered["hits"]} == {"26-0001", "26-0001 R1"}
    assert app.search({"q": "  "}) == {"hits": []}


def test_ask_streams_sources_then_tokens_then_a_checked_answer(app):
    events = list(app.ask({"q": "what happened with 26-0001?", "top": "2"}))
    assert [name for name, _ in events] == ["sources", "token", "token", "done"]
    assert [hit.file_number for hit in events[0][1]["hits"]] == ["26-0001", "26-0001 R1"]
    assert events[-1][1]["model"] == "fake-chat" and events[-1][1]["unsupported"] == []
    assert "Question: what happened with 26-0001?" in app.fake_chat.prompts[0]

    app.fake_chat.pieces = ("Agreement PR99999999 for $5,000,000 was adopted under 27-0999 [7].",)
    done = list(app.ask({"q": "26-0001", "top": "2"}))[-1][1]
    assert done["unsupported"] == [
        "cites source [7], which does not exist",
        "file number 27-0999 is not in the sources",
        "PR99999999 is not in the sources",
        "$5,000,000 is not in the sources",
    ]


def test_ask_reports_model_failures_as_an_event(app):
    app.fake_chat.fail = "Ollama fell over"
    events = list(app.ask({"q": "26-0001"}))
    assert events[-1] == ("error", {"message": "Ollama fell over"})


def test_matter_view_has_the_family_path_and_typed_neighbours(app):
    data = app.matter({"file": "26-0001"})
    assert data["card"].file_number == "26-0001"
    assert [(member["file_number"], [h["action"] for h in member["history"]]) for member in data["journey"]] == [
        ("26-0001", ["refer to committee", "progress"]),
        ("26-0001 R1", ["adopt"]),
    ]
    groups = {group["name"]: group for group in data["neighbours"]}
    assert set(groups) == {"Requested by", "Heard by", "Later items", "Cites"}
    assert groups["Requested by"]["nodes"][0]["label"] == "Public Works - Roads"
    assert [node["label"] for node in groups["Heard by"]["nodes"]] == ["Board", "Public Works Committee"]
    assert groups["Later items"]["nodes"][0]["label"] == "26-0001 R1"
    assert groups["Cites"]["nodes"][0] == {"id": "mnstat:383B.158", "type": "statute", "label": "Minn. Stat. § 383B.158", "detail": None}
    assert data["sections"]["resolution"].startswith("BE IT RESOLVED") and data["attachments"][0]["name"] == "Map"
    assert app.matter({"id": "test:matter:13"})["journey"][0]["awaiting"] == ["Public Works Committee"]
    with pytest.raises(web.NotFound):
        app.matter({"file": "99-9999"})


def test_node_view_lists_attached_matters_newest_first(app):
    department = app.node({"id": "test:department:public-works-roads"})
    assert (department["type"], department["total"]) == ("department", 2)
    assert [m["file_number"] for m in department["matters"]] == ["26-0001 R1", "26-0001"]
    chapter = app.node({"id": "mnstat:383B"})  # a chapter answers for its sections
    assert [m["file_number"] for m in chapter["matters"]] == ["26-0001"] and chapter["url"]
    body = app.node({"id": "test:body:2"})
    assert {m["file_number"] for m in body["matters"]} == {"26-0001"}
    with pytest.raises(web.NotFound):
        app.node({"id": "test:matter:10"})
    with pytest.raises(web.NotFound):
        app.node({"id": "nope"})


def test_queues(app):
    data = app.queues()
    assert [(queue["body"], [item["file_number"] for item in queue["items"]]) for queue in data["queues"]] == [
        ("Public Works Committee", ["26-0003"])
    ]
    assert [(meeting["body"], meeting["minutes_status"], meeting["items"]) for meeting in data["meetings"]] == [("Board", "Draft", 2)]
    assert data["queues"][0]["items"][0]["reason"] == "on a draft agenda, no action yet" and data["stale"] == []


def test_queues_include_what_a_recorded_action_sent_and_set_leftovers_apart(tmp_path):
    _write_bronze(tmp_path)
    store = BronzeStore(tmp_path, C)
    to_public_works = "moved to {} the Resolution to the Public Works Committee."
    store.write("events", 5, _event(5, BOARD, "Board", "2026-03-03", [  # after its adoption, the shelter item is sent to committee
        _item(500, 1, matter=12, action="refer to committee", passed="Pass", text=to_public_works.format("refer to committee")),
    ]))
    store.write("events", 6, _event(6, BOARD, "Board", "2025-06-03", [  # returned long ago and never heard: a leftover
        _item(600, 1, matter=15, action="return to committee", passed="Pass", text=to_public_works.format("return to committee")),
    ]))
    silver.build(tmp_path, C)
    embedder = FakeEmbedder()
    gold.build(tmp_path, C, embedder)
    data = web.App(Searcher(tmp_path, C, embedder), FakeChat, client=C, embed_model=embedder.model).queues()
    assert [(q["body"], [(i["file_number"], i["reason"]) for i in q["items"]]) for q in data["queues"]] == [
        ("Public Works Committee", [
            ("26-0002", "refer to committee (recorded)"),
            ("26-0003", "on a draft agenda, no action yet"),
        ])
    ]
    assert [(i["file_number"], i["body"], i["reason"]) for i in data["stale"]] == [
        ("26-0005", "Public Works Committee", "return to committee (recorded)")
    ]


def test_topic_counts_matters_containing_every_word_by_quarter_and_department(app):
    data = app.topic({"q": "agreement paving"})
    assert data["total"] == 2 and data["departments"] == ["Public Works - Roads"]
    assert data["quarters"] == [{"quarter": "2026 Q1", "counts": {"Public Works - Roads": 2}, "total": 2}]
    assert [(m["file_number"], m["quarter"]) for m in data["matters"]] == [("26-0001 R1", "2026 Q1"), ("26-0001", "2026 Q1")]
    assert app.topic({"q": "agreement shelter"})["total"] == 0  # both words, in one passage
    letter = app.topic({"q": "Edina transit"})
    assert letter["departments"] == ["No department"] and letter["quarters"][0]["counts"] == {"No department": 1}
    assert app.topic({"q": "agreement paving", "department": "human"})["total"] == 0
    assert app.topic({"q": ""})["total"] == 0


def test_unsupported_claims_accepts_what_the_sources_say(app):
    hits = app.search({"q": "26-0001", "top": "2"})["hits"]
    prompt = rag.build_prompt("q", hits, FakeChat(), client=C)
    fine = "26-0001 R1 approved Agreement PR00012345 with Acme Paving [1, 2]. It cost about $2 million."
    assert rag.unsupported_claims(fine, prompt) == []
    assert rag.unsupported_claims("See A26-0001 [1].", prompt) == ["A26-0001 is not in the sources"]


def test_http_server_end_to_end(app):
    server = web.make_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = httpx.get(base + "/")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"] and "<title>" in page.text
        assert httpx.get(base + "/api/meta").json()["client"] == C
        assert httpx.get(base + "/api/queues").json()["queues"][0]["body"] == "Public Works Committee"
        missing = httpx.get(base + "/api/matter", params={"file": "99-9999"})
        assert missing.status_code == 404 and "99-9999" in missing.json()["error"]
        assert httpx.get(base + "/api/search", params={"q": "x", "since": "not-a-date"}).status_code == 400
        assert httpx.get(base + "/nope").status_code == 404
        stream = httpx.get(base + "/api/ask", params={"q": "26-0001", "top": 1})
        assert stream.headers["content-type"].startswith("text/event-stream")
        names = [line.split(": ", 1)[1] for line in stream.text.splitlines() if line.startswith("event: ")]
        assert names == ["sources", "token", "token", "done"]
    finally:
        server.shutdown()
        server.server_close()
