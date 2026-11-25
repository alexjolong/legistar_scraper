"""Filing matters under themes and storylines, over the tiny record the search tests use."""

from __future__ import annotations

import json

import pyarrow.parquet as pq
import pytest
from test_search import C, FakeEmbedder, _write_bronze
from test_web import FakeChat

from legistutor import cli, gold, llm, query, themes, web
from legistutor.search import Searcher

ROAD = "County Road 81 reconstruction"


class Filer:
    """Files by what the item says, as a model would, and counts what it was asked."""

    name, context_chars = "fake", 100_000

    def __init__(self, model="fake-filer", broken=()):
        self.model, self.broken, self.asked = model, broken, []

    def stream(self, system, user):
        self.asked.append(user)
        if any(word in user for word in self.broken):
            yield "I could not decide."
        elif "Acme Paving" in user:
            yield 'Here: {"gist": "Approve a paving  agreement for County Road 81.", "themes": ["roads-bridges"], "storyline": "%s"}' % ROAD
        elif "shelter" in user:
            # a theme by its name, one not on the list, and too many
            reply = {"gist": "Accept funding for shelter beds.", "storyline": "null",
                     "themes": ["homelessness", "Human services and public assistance", "paperwork", "equity", "public-health"]}  # fmt: skip
            yield json.dumps(reply)
        elif "Edina" in user:
            yield '{"gist": "Receive a letter about transit.", "themes": ["transit-rail"], "storyline": "County Road 81 Reconstruction (CR 81) project"}'
        else:
            yield '{"gist": "Extend library hours.", "themes": [], "storyline": null}'


def test_the_theme_list_and_the_reply_are_checked():
    listed = themes.read_themes()
    assert len(listed) == len({t.id for t in listed}) > 20 and all(t.area and t.name and t.description for t in listed)
    assert "affordable-housing: Affordable housing." in themes.system_prompt(listed)
    assert themes.fingerprint(listed) != themes.fingerprint(listed[:-1])  # a changed list is a reason to file again
    filed = themes.read_reply('{"gist": " Do  it. ", "themes": ["equity", "Equity and disparity reduction", 7], "storyline": " The   Plan "}', listed)
    assert filed == {"gist": "Do it.", "themes": ["equity"], "storyline": "The Plan"}
    for unreadable in ("no json", '{"themes": []}', '{"gist": "", "themes": []}', "[1, 2]"):
        assert themes.read_reply(unreadable, listed) is None


def test_two_spellings_of_a_storyline_are_one():
    key = themes.storyline_key
    assert key("Transit Oriented Communities (TOC) program") == key("Transit-Oriented Communities Program") == key("Transit oriented community")
    assert key("2025 Pedestrian Ramp Project Phase 4") == key("Pedestrian ramp projects") and key("2026 Budget") == "budget"
    assert key("METRO Blue Line Extension") == key("Blue Line Extension") and key("Hennepin Healthcare Systems, Inc.") == key("Hennepin Healthcare System")
    bodies = [" healthcare system inc governing board ", " regional railroad authority "]
    assert themes._is_a_body(key("Hennepin Healthcare System, Inc."), bodies) and not themes._is_a_body(key("Railroad crossings"), bodies)
    assert key("The Program") is None and key(None) is None and key("HERC") is None
    assert themes.title_agreements("Amd 1 to Agmt A2110804 and PR00012345") == ["A2110804", "PR00012345"]
    assert themes.title_agreements("Amds to A2110804, A2110805, A2110806") == []  # a bundle joins nothing


@pytest.fixture
def filed(tmp_path):
    _write_bronze(tmp_path)
    embedder = FakeEmbedder()
    gold.build(tmp_path, C, embedder)
    chat = Filer()
    report = themes.tag(tmp_path, C, chat)
    return tmp_path, embedder, chat, report


def test_every_matter_but_the_routine_is_filed_once(filed):
    data_dir, _, chat, report = filed
    assert (report["matters"], report["filed"], report["reused"], report["failed"]) == (5, 5, 0, 0)  # the minutes are not read
    assert report["without_a_theme"] == 1 and (report["storylines"], report["in_a_storyline"]) == (1, 3)
    rows = {row["file_number"]: row for row in pq.read_table(themes.tags_path(data_dir, C)).to_pylist()}
    assert rows["26-0002"]["themes"] == ["homelessness", "human-services", "equity"] and rows["26-0002"]["storyline"] is None
    assert rows["26-0001"]["gist"] == "Approve a paving agreement for County Road 81." and rows["26-0001"]["model"] == "fake-filer"
    assert "TITLE:" in chat.asked[0] and "RESOLUTION:" in chat.asked[0] and "Requested by: Public Works - Roads" in chat.asked[0]

    again = themes.tag(data_dir, C, chat)
    assert (again["filed"], again["reused"]) == (0, 5) and len(chat.asked) == 5
    other = Filer(model="another-model")
    assert themes.tag(data_dir, C, other, limit=2)["left_for_later"] == 3 and len(other.asked) == 2  # another model files afresh
    assert themes.tag(data_dir, C, other)["reused"] == 2
    assert themes.tag(data_dir, C, other, full=True)["filed"] == 5


def test_storylines_join_by_name_and_say_why(filed):
    data_dir, *_ = filed
    members = {row["file_number"]: row for row in pq.read_table(themes.storylines_path(data_dir, C)).to_pylist()}
    assert set(members) == {"26-0001", "26-0001 R1", "26-0005"}
    assert members["26-0001"]["basis"] == "name, revision" and members["26-0005"]["basis"] == "name"
    assert {row["name"] for row in members.values()} == {ROAD} and members["26-0005"]["themes"] == ["roads-bridges", "transit-rail"]
    # An item and its revision alone are one item, not a storyline.
    alone = [row for row in pq.read_table(themes.tags_path(data_dir, C)).to_pylist() if row["file_number"] != "26-0005"]
    assert themes.build_storylines(query.connect(data_dir, C), alone) == []


def test_a_mention_attaches_an_item_but_does_not_merge_two_named_stories():
    import duckdb

    con = duckdb.connect()
    con.execute("create table v_matter as select * from (values (1, 'm:1', '25-0001', 'Action plan 2025', date '2025-01-01'), "
                "(2, 'm:2', '26-0001', 'Action plan 2026', date '2026-01-01'), (3, 'm:3', '26-0002', 'Appoint members', date '2026-02-01'), "
                "(4, 'm:4', '26-0003', 'Appoint more members', date '2026-03-01'), (5, 'm:5', '26-0004', 'A letter', date '2026-04-01')) "
                "t(matter_id, id, file_number, title, intro_date)")  # fmt: skip
    con.execute("alter table v_matter add column agenda_date date")
    con.execute("create table e_related_to as select 'm:1' src, 'm:1' dst, 'other' kind limit 0")
    con.execute("create table e_references as select * from (values ('m:2', 'm:1'), ('m:3', 'm:2'), ('m:5', 'm:3')) t(src, dst)")
    con.execute("create table v_body as select 'Board of Hennepin County Commissioners' as name")
    tag = lambda n, story: {"matter": f"m:{n}", "storyline": story, "themes": ["governance"]}  # noqa: E731
    tags = [tag(1, "HUD Action Plan"), tag(2, "HUD Action Plans"), tag(3, "Advisory Board"), tag(4, "Advisory Board"), tag(5, None)]
    stories = {}
    for row in themes.build_storylines(con, tags):
        stories.setdefault(row["name"], {})[row["file_number"]] = row["basis"]
    assert stories == {
        "HUD Action Plan": {"25-0001": "name, reference", "26-0001": "name, reference"},
        "Advisory Board": {"26-0002": "name, reference", "26-0003": "name", "26-0004": "reference"},  # the letter comes along
    }


def test_a_reply_that_cannot_be_read_is_asked_again_then_left_for_next_time(tmp_path):
    _write_bronze(tmp_path)
    chat = Filer(broken=("shelter",))
    report = themes.tag(tmp_path, C, chat)
    assert (report["matters"], report["failed"]) == (4, 1) and len(chat.asked) == 6  # the shelter item twice
    assert themes.tag(tmp_path, C, Filer())["filed"] == 1

    class Down(Filer):
        def stream(self, system, user):
            raise llm.LLMError("no endpoint")
            yield

    themes.GIVE_UP_AFTER, kept = 3, themes.GIVE_UP_AFTER
    try:
        with pytest.raises(llm.LLMError, match="3 matters in a row"):
            themes.tag(tmp_path, C, Down(), full=True)
    finally:
        themes.GIVE_UP_AFTER = kept


def test_the_page_shows_themes_storylines_and_a_meeting_by_theme(filed):
    data_dir, embedder, *_ = filed
    app = web.App(Searcher(data_dir, C, embedder), FakeChat, client=C, embed_model=embedder.model)
    assert app.meta()["themes"] is True
    data = json.loads(web.to_json(app.explore()))
    assert data["months"] == ["2026-02", "2026-03"] and (data["filed"], data["without_a_theme"], data["storylines"]) == (5, 1, 1)
    roads = next(t for t in data["themes"] if t["id"] == "roads-bridges")
    assert (roads["matters"], roads["storylines"], roads["months"]) == (2, 1, {"2026-02": 1, "2026-03": 1})

    theme = json.loads(web.to_json(app.theme({"id": "roads-bridges"})))
    assert [(m["file_number"], m["days"]) for m in theme["matters"]] == [
        ("26-0005", ["2026-01-01"]),  # the letter comes with its storyline; never on an agenda, so the day it was introduced
        ("26-0001", ["2026-02-03", "2026-02-10"]),
        ("26-0001 R1", ["2026-03-17"]),
    ]
    assert theme["storylines"][0]["name"] == ROAD and theme["storylines"][0]["matters"] == 3
    shelter = app.theme({"id": "homelessness"})
    assert [m["file_number"] for m in shelter["matters"]] == ["26-0002"] and shelter["storylines"] == []
    with pytest.raises(web.NotFound):
        app.theme({"id": "paperwork"})

    story = app.storyline({"id": theme["storylines"][0]["id"]})
    assert [m["file_number"] for m in story["matters"]] == ["26-0005", "26-0001", "26-0001 R1"]
    assert (str(story["first"]), str(story["last"])) == ("2026-01-01", "2026-03-17") and [t["id"] for t in story["themes"]] == ["roads-bridges", "transit-rail"]
    with pytest.raises(web.NotFound):
        app.storyline({"id": "story:999"})

    tags = app.matter({"file": "26-0001"})["tags"]
    assert tags["gist"].startswith("Approve a paving") and tags["storyline"]["matters"] == 3 and tags["themes"][0]["name"] == "Roads and bridges"
    assert app.matter({"file": "26-0004"})["tags"] is None  # the minutes

    board = app.meeting({})
    assert board["meeting"]["body"] == "Board" and str(board["meeting"]["date"]) == "2026-03-17" and len(board["meetings"]) == 3
    assert [(g["name"], [i["file_number"] for i in g["items"]]) for g in board["groups"]] == [
        ("Roads and bridges", ["26-0001 R1"]),
        ("Routine or not filed", ["26-0003"]),
    ]
    first = app.meeting({"id": board["meetings"][-1]["id"]})
    assert [g["name"] for g in first["groups"]] == ["Homelessness", "Roads and bridges"] and first["groups"][0]["items"][0]["also"][0]["id"] == "human-services"


def test_the_page_without_any_filing(tmp_path):
    _write_bronze(tmp_path)
    embedder = FakeEmbedder()
    gold.build(tmp_path, C, embedder)
    app = web.App(Searcher(tmp_path, C, embedder), FakeChat, client=C, embed_model=embedder.model)
    assert app.meta()["themes"] is False and app.explore() == {"available": False} and app.matter({"file": "26-0001"})["tags"] is None
    assert app.meeting({})["groups"][0]["name"] == "Routine or not filed"
    with pytest.raises(web.NotFound):
        app.theme({"id": "roads-bridges"})


def test_the_set_paragraph_on_disparities_is_not_shown_to_the_model(tmp_path):
    report = "Emergency shelter beds this winter.\n\nHousing Disparity: households of color are cost burdened. This request helps.\n\nBeds open in December."
    _write_bronze(tmp_path, shelter_background=report)
    shelter = next(m for m in themes.taggable(query.connect(tmp_path, C)) if m["file_number"] == "26-0002")
    assert "Disparity" not in shelter["body"] and "this winter.\n\nBeds open in December." in shelter["body"]
    assert "RESOLUTION:\nBE IT RESOLVED, that funding for shelter services be accepted." in shelter["body"]


def test_the_command_reports_in_one_line():
    report = {"matters": 5, "filed": 2, "reused": 3, "failed": 1, "left_for_later": 4, "storylines": 1, "in_a_storyline": 3, "model": "m"}
    line = cli._themes_line(report)
    assert "5 matters filed (2 newly, 3 unchanged) by m" in line and "1 could not be filed" in line and "4 left for the next run" in line
    assert themes.worker_count("ollama") == 1 and themes.worker_count("databricks", env={}.get) == 4 and themes.worker_count("ollama", 3) == 3
