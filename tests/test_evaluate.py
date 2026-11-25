"""Scoring the evaluation questions, over the tiny record the web tests use."""

from __future__ import annotations

import json

import pytest
from test_web import FakeChat, app  # noqa: F401 - fixture

from legistutor import cli, evaluate, llm

HEADER = ",".join(evaluate.COLUMNS)


def _questions(tmp_path, *rows):
    path = tmp_path / "questions.csv"
    path.write_text("﻿" + "\n".join([HEADER, *rows, ",,,,,,,,,,"]) + "\n", encoding="utf-8")  # as Excel saves it
    return path


ROAD = 'q1,road contract with Acme Paving,tracker,named-item,26-0001; 26-0001 R1,,adopted 2026-03-17; referred first to Public Works,that 26-0001 itself was adopted,,2026-11-16,'
MISSING = "q2,library hours,topical,topic,26-0002,26-0003,funding accepted,,,2026-11-16,"
NOTHING = "q3,why did the commissioner object?,topical,no-answer,,,,,yes,2026-11-16,"


def test_reading_the_file_and_naming_every_problem(tmp_path):
    questions = evaluate.read_questions(_questions(tmp_path, ROAD, NOTHING))
    assert [q.id for q in questions] == ["q1", "q3"]  # the blank line is not a question
    assert questions[0].must_find == ["26-0001", "26-0001 R1"] and questions[0].answer_facts[0] == "adopted 2026-03-17"
    assert questions[1].no_answer and questions[1].must_find == []
    bad = _questions(tmp_path, "q1,,topical,topics,,,,,,,", "q1,a question,topical,topic,26-0001,,,,,,")
    with pytest.raises(evaluate.QuestionsError) as raised:
        evaluate.read_questions(bad)
    message = str(raised.value)
    for words in ("q1: question is empty", "kind is 'topics'", "q1: must_find is empty", "q1: id is used twice", "q1: answer_facts is empty"):
        assert words in message
    (tmp_path / "short.csv").write_text("id,question\nq1,x\n", encoding="utf-8")
    with pytest.raises(evaluate.QuestionsError, match="no column named asker"):
        evaluate.read_questions(tmp_path / "short.csv")


def test_what_a_must_find_entry_names():
    assert evaluate.target("26-0333 r1") == ("matter", "26-0333R1")
    assert evaluate.target("163.051") == ("statute", "163.051") and evaluate.target("Minn. Stat. § 383b.158") == ("statute", "383B.158")
    assert evaluate.target("Ordinance 13") == ("ordinance", "13")


def test_search_is_scored_on_what_must_be_found(app, tmp_path):  # noqa: F811
    questions = evaluate.read_questions(_questions(tmp_path, ROAD, MISSING, NOTHING))
    lines = []
    road, missing, nothing = evaluate.run(app, questions, top=1, progress=lines.append)
    assert road.found is False and road.ranks["26-0001"] == 1 and road.ranks["26-0001 R1"] is None  # top=1 has room for one
    assert road.reciprocal_rank == 1.0 and road.lead_expected == "record" and road.lead == "record"
    assert missing.found is False and "MISSED 26-0002" in lines[1]
    assert nothing.found is None and nothing.answer is None
    wider = evaluate.run(app, questions, top=4)
    assert wider[0].found is True and wider[0].ranks == {"26-0001": 1, "26-0001 R1": 2}
    assert "To look at:" not in evaluate.summary(wider)
    text = "\n".join(evaluate.summary([road, missing, nothing]))
    assert "named-item" in text and "q2 (topic): MISSED 26-0002" in text and "q1 (named-item): MISSED 26-0001 R1" in text
    assert evaluate.unknown_items(app, questions) == []
    questions[1].must_find = ["27-9999", "999.99", "Ordinance 77"]
    assert len(evaluate.unknown_items(app, questions)) == 3


class Judge:
    name, model, context_chars = "fake", "fake-judge", 100_000

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def stream(self, system, user):
        self.prompts.append(user)
        if isinstance(self.reply, Exception):
            raise self.reply
        yield self.reply


def test_answers_are_read_by_a_judge(app, tmp_path):  # noqa: F811
    questions = evaluate.read_questions(_questions(tmp_path, ROAD, NOTHING))
    judge = Judge('Here you go: {"facts": [true, false], "said_wrong_statement": true, "declined": false}')
    road, nothing = evaluate.run(app, questions, top=4, answers=True, judge=judge)
    assert road.answer == "Adopted [1]." and road.facts == {"adopted 2026-03-17": True, "referred first to Public Works": False}
    assert road.said_forbidden is True and "1/2 facts" in evaluate.one_line(road) and "SAID" in evaluate.one_line(road)
    assert "1. adopted 2026-03-17\n2. referred first" in judge.prompts[0] and "that 26-0001 itself was adopted" in judge.prompts[0]
    assert nothing.error == "judge: the reply could not be read"  # two facts told of, none asked about

    declining = Judge('{"facts": [], "said_wrong_statement": false, "declined": true}')
    nothing = evaluate.run(app, questions[1:], answers=True, judge=declining)[0]
    assert nothing.declined is True and nothing.said_forbidden is None and "declined, as it should" in evaluate.one_line(nothing)
    text = "\n".join(evaluate.summary([road, nothing]))
    assert "no-answer questions declined: 1/1" in text and "q1 (named-item)" in text

    failed = evaluate.run(app, questions[:1], answers=True, judge=Judge(llm.LLMError("no endpoint")))[0]
    assert failed.error == "judge: no endpoint" and failed.found is True
    assert evaluate.read_judgment("not json", 2) is None and evaluate.read_judgment('{"facts": [true]}', 2) is None


def test_a_run_is_saved_and_compared_with_an_earlier_one(app, tmp_path):  # noqa: F811
    questions = evaluate.read_questions(_questions(tmp_path, ROAD, MISSING))
    narrow = evaluate.run(app, questions, top=1)
    saved = evaluate.save(narrow, tmp_path / "results", {"embed_model": "fake-embedder", "top": 1})
    record = json.loads(saved.read_text(encoding="utf-8"))
    assert record["top"] == 1 and record["results"][0]["ranks"] == {"26-0001": 1, "26-0001 R1": None}
    wide = evaluate.run(app, questions, top=4)
    lines = evaluate.compare(wide, saved)
    assert "embed_model fake-embedder, top 1" in lines[0] and any("q1: now found" in line for line in lines)
    assert evaluate.compare(narrow, saved)[-1] == "  no question changed"


def test_the_command_with_no_questions_and_with_a_broken_file(tmp_path, capsys):
    empty = tmp_path / "questions.csv"
    empty.write_text(HEADER + "\n", encoding="utf-8")
    assert cli._eval(cli._build_parser().parse_args(["eval", "--questions", str(empty)])) == 0
    assert "No questions" in capsys.readouterr().out
    empty.write_text(HEADER + "\nq1,x,topical,nonsense,,,,,,,\n", encoding="utf-8")
    assert cli._eval(cli._build_parser().parse_args(["eval", "--questions", str(empty)])) == 2
    assert "kind is 'nonsense'" in capsys.readouterr().err
