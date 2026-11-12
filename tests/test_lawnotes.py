"""The model reading one statute or ordinance: what it is given, what the reader is told
it was given, and what is kept."""

from __future__ import annotations

import json

import httpx
import pytest
from test_law import app, joined  # noqa: F401 - fixtures
from test_search import C

from legistutor import lawnotes, llm, web
from legistutor.lawnotes import Part, Scope


def _scope(sizes: dict[str, int], kind: str = "section") -> Scope:
    parts = [Part(f"mnstat:1.01#{n}", f"Subd. {n}. Part {n}.", "x" * size) for n, size in sizes.items()]
    return Scope("mnstat:1.01", kind, "Minn. Stat. § 1.01 (TEST)", parts, outline="§ 1.01 TEST", subdivisions=set(sizes))


def test_everything_is_read_when_it_fits():
    scope = _scope({"1": 100, "2": 100})
    chosen = lawnotes.select(scope, 10_000, summary=True)
    assert (chosen.basis, len(chosen.included), chosen.omitted) == ("whole", 2, [])
    assert chosen.coverage == "All of Minn. Stat. § 1.01 (TEST) was read."
    system, user = lawnotes.build_prompt(scope, chosen, "", jurisdiction="Test County")
    assert "You have the whole of it." in system and "Test County" in system and "a section of Minnesota Statutes" in system
    assert "This section's subdivisions are numbered 1, 2; cite only those as subdivisions." in system and '"subd. 5(a)"' in system
    plain = Scope("mnstat:1.02", "section", "Minn. Stat. § 1.02", [Part("mnstat:1.02", "", "(1) one; (2) two.")])
    assert "never call one a subdivision" in lawnotes.build_prompt(plain, lawnotes.select(plain, 10_000, summary=True), "", jurisdiction="T")[0]
    assert user.startswith('<law name="Minn. Stat. § 1.01 (TEST)">\nSubd. 1. Part 1.\nxxx') and user.endswith("Summarise this section for a county employee.")
    assert lawnotes.build_prompt(scope, chosen, "Who pays?", jurisdiction="Test County")[1].endswith("Question: Who pays?")


def test_a_long_text_is_cut_to_the_parts_closest_to_the_question_and_says_so():
    scope = _scope({"1": 400, "2": 400, "3": 400, "4": 400})
    scores = {"mnstat:1.01#3": 0.9, "mnstat:1.01#1": 0.8, "mnstat:1.01#2": 0.1, "mnstat:1.01#4": 0.0}
    chosen = lawnotes.select(scope, 900, scores, summary=False)
    assert chosen.basis == "closest" and [p.label for p in chosen.included] == ["Subd. 1. Part 1.", "Subd. 3. Part 3."]  # statute order
    assert chosen.coverage == (
        "Minn. Stat. § 1.01 (TEST) is too long for the model to read at once. Read: subd. 1, subd. 3 "
        "(2 of 4 subdivisions, chosen as closest to the question). Not read: subd. 2, subd. 4."
    )
    system, _ = lawnotes.build_prompt(scope, chosen, "Who pays?", jurisdiction="Test County")
    assert "You do not have the whole of it." in system and "Not read: subd. 2, subd. 4." in system


def test_a_long_summary_reads_from_the_start_and_a_long_chapter_reads_its_headings():
    in_order = lawnotes.select(_scope({"1": 400, "2": 400, "3": 400}), 900, summary=True)
    assert in_order.basis == "in order" and [p.label for p in in_order.included] == ["Subd. 1. Part 1.", "Subd. 2. Part 2."]
    assert "taken in order from the start" in in_order.coverage
    one_huge = lawnotes.select(_scope({"1": 5000}), 900, summary=True)
    assert one_huge.included[0].text.endswith("[cut off here for length]") and len(one_huge.text) < 900
    chapter = lawnotes.select(_scope({"1": 5000, "2": 5000}, kind="chapter"), 900, summary=True)
    assert (chapter.basis, chapter.text, chapter.included) == ("outline", "§ 1.01 TEST", [])
    assert "the headings only, not the text under them" in chapter.coverage


def test_references_to_places_the_model_did_not_read_are_flagged():
    scope = _scope({"1": 50, "2": 50, "3": 50})
    part = lawnotes.select(scope, 150, {"mnstat:1.01#1": 1.0, "mnstat:1.01#2": 0.5}, summary=False)
    answer = "Under subd. 1 and section 1.01 the county pays; see subdivision 3, subd. 9 and section 471.345."
    assert lawnotes.unsupported_references(answer, scope, part) == [
        "section 471.345 is not in the text it read",
        "subdivision 3 was not among the parts it read",
        "subdivision 9 does not exist in this section",
    ]


def test_scopes_come_from_the_graph(app):  # noqa: F811
    section = lawnotes.load(app._rows, "mnstat:383B.158#2")  # a subdivision opens its section
    assert (section.id, section.kind, section.label) == ("mnstat:383B.158", "section", "Minn. Stat. § 383B.158 (DESIGN-BUILD CONTRACTS)")
    assert [p.label for p in section.parts] == ["Subd. 1. Definitions.", "Subd. 2. Authority.", "Subd. 3."]
    chapter = lawnotes.load(app._rows, "mnstat:383B")
    assert [p.label for p in chapter.parts] == ["§ 383B.021 COMPENSATION", "§ 383B.158 DESIGN-BUILD CONTRACTS"]
    assert chapter.outline.splitlines() == ["§ 383B.021 COMPENSATION", "§ 383B.031 [repealed]", "§ 383B.158 DESIGN-BUILD CONTRACTS"]
    assert "Subd. 2. Authority.\nNotwithstanding section 471.345" in chapter.parts[1].text
    ordinance = lawnotes.load(app._rows, f"{C}:ordinance:13")
    assert (ordinance.kind, [p.label for p in ordinance.parts]) == ("ordinance", ["Opening", "Section I: Definitions", "Section II: Violations"])
    for node, message in (("mnstat:383B.031", "Repealed, 2024 c 112"), ("mnstat:383B.999", "not in the statutes as published"), ("mnlaw:2026:88", "statutes and ordinances")):
        with pytest.raises(lawnotes.NothingToRead, match=message):
            lawnotes.load(app._rows, node)


def test_a_summary_is_written_once_then_kept_until_the_model_changes(app, tmp_path):  # noqa: F811
    app.notes_dir = tmp_path / "summaries"
    app.fake_chat.pieces = ("The county board may award design-build contracts ", "(subd. 2).")
    events = list(app.law_ask({"id": "mnstat:383B.158"}))
    assert [name for name, _ in events] == ["scope", "token", "token", "done"]
    assert events[0][1]["basis"] == "whole" and events[0][1]["coverage"] == "All of Minn. Stat. § 383B.158 (DESIGN-BUILD CONTRACTS) was read."
    assert events[-1][1]["kept"] is False and events[-1][1]["unsupported"] == []
    assert "Summarise this section for a county employee." in app.fake_chat.prompts[-1]

    again = list(app.law_ask({"id": "mnstat:383B.158"}))  # from disk: the model is not asked
    assert len(app.fake_chat.prompts) == 1 and again[-1][1]["kept"] is True
    assert again[1] == ("token", {"text": "The county board may award design-build contracts (subd. 2)."})
    shown = app.node({"id": "mnstat:383B.158"})["summary"]
    assert shown["text"].endswith("(subd. 2).") and shown["current"] is True and shown["model"] == "fake-chat"
    assert app.node({"id": "mnstat:383B.021"})["summary"] is None

    assert list(app.law_ask({"id": "mnstat:383B.158", "fresh": "1"}))[-1][1]["kept"] is False and len(app.fake_chat.prompts) == 2
    app.fake_chat.model = "another-model"  # a summary by a different model is shown as out of date, and rewritten on request
    assert app.node({"id": "mnstat:383B.158"})["summary"]["current"] is False
    assert list(app.law_ask({"id": "mnstat:383B.158"}))[-1][1]["kept"] is False


def test_questions_are_answered_from_the_text_and_never_kept(app, tmp_path):  # noqa: F811
    app.notes_dir = tmp_path / "summaries"
    app.fake_chat.pieces = ("Yes, under subd. 2 and subd. 7.",)
    events = list(app.law_ask({"id": "mnstat:383B.158", "q": "May the board award one?"}))
    assert events[-1][1]["unsupported"] == ["subdivision 7 does not exist in this section"]
    assert app.fake_chat.prompts[-1].endswith("Question: May the board award one?") and not (tmp_path / "summaries").exists()
    assert list(app.law_ask({"id": "mnstat:383B.031"}))[-1][0] == "error"
    assert list(app.law_ask({"id": "nope"}))[-1] == ("error", {"message": "No statute or ordinance 'nope'"})
    ordinance = list(app.law_ask({"id": f"{C}:ordinance:13", "q": "What is a violation?"}))
    assert ordinance[0][1]["kind"] == "ordinance" and "Section II: Violations\nA violation is a misdemeanor." in app.fake_chat.prompts[-1]


def test_a_long_section_is_ranked_against_the_question_with_the_search_index(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lawnotes, "budget", lambda *args, **kwargs: 260)  # room for one of the two long subdivisions, and the one-line repealed one
    events = list(app.law_ask({"id": "mnstat:383B.158", "q": "who is a design professional licensed under chapter 326"}))
    assert events[0][1]["basis"] == "closest" and "Read: subd. 1, subd. 3 (2 of 3 subdivisions, chosen as closest to the question). Not read: subd. 2." in events[0][1]["coverage"]
    summary = list(app.law_ask({"id": "mnstat:383B", "fresh": "1"}))  # a chapter that does not fit: headings only
    assert summary[0][1]["basis"] == "outline" and "§ 383B.031 [repealed]" in app.fake_chat.prompts[-1]


def test_the_stream_is_served_over_http(app):  # noqa: F811
    import threading

    server = web.make_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        stream = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/api/law_ask", params={"id": "mnstat:383B.158", "q": "who?"})
        names = [line.split(": ", 1)[1] for line in stream.text.splitlines() if line.startswith("event: ")]
        assert stream.headers["content-type"].startswith("text/event-stream") and names == ["scope", "token", "token", "done"]
    finally:
        server.shutdown()
        server.server_close()


def test_a_runner_that_dies_before_answering_is_asked_once_more():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": "model runner has unexpectedly stopped"})
        body = "\n".join(json.dumps(e) for e in ({"message": {"content": "Fine."}}, {"done": True, "prompt_eval_count": 10}))
        return httpx.Response(200, text=body)

    chat = llm.OllamaChat("m", "http://ollama.test", transport=httpx.MockTransport(handler))
    import time

    real_sleep, time.sleep = time.sleep, lambda seconds: None
    try:
        assert list(chat.stream("s", "u")) == ["Fine."] and len(calls) == 2
        calls.clear()
        always = llm.OllamaChat("m", "http://ollama.test", transport=httpx.MockTransport(lambda r: httpx.Response(500, json={"error": "runner stopped"})))
        with pytest.raises(llm.LLMError):
            list(always.stream("s", "u"))
    finally:
        time.sleep = real_sleep
