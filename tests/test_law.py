"""The law sources: parsing the Revisor's pages, the ordinance pages and the bill
archive; building their graphs; and joining them to the record."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from lawdata import (
    API_BILL,
    CHAPTER,
    LAW,
    LAW_LIST,
    ORDINANCE,
    ORDINANCE_CATEGORY,
    ORDINANCE_INDEX,
    SESSIONS,
    TABLE2,
    bills_archive,
    write_law_bronze,
)
from test_search import C, TEMPLATE, FakeEmbedder, _write_bronze
from test_silver import BOARD, _event, _item, _matter
from test_web import FakeChat

from legistutor import gold, law, openstates, ordinances, query, rag, revisor, silver, web
from legistutor import text as tx
from legistutor.bronze import BronzeStore
from legistutor.fetch import Disallowed, Fetcher, PageStore
from legistutor.search import Searcher

# --- citations in matter text -------------------------------------------------------------


def test_subdivision_citations_need_a_cited_section():
    text = (
        "under Minnesota Statutes, section 383B.158, subdivision 4, and Minn. Stat. § 373.01, subd. 1(d); also "
        "Minn. Stat. 13.461, subdivisions 16a, 2 and 3. The rate rose 12.5, subd. 9 percent."
    )
    assert [(s, d) for s, d, _ in tx.extract_subdivision_citations(text)] == [
        ("383B.158", "4"), ("373.01", "1"), ("13.461", "16a"), ("13.461", "2"), ("13.461", "3"),
    ]  # fmt: skip
    assert tx.extract_subdivision_citations("section 5.2, subdivision 1 of the agreement") == []


def test_session_law_citations():
    text = (
        "Laws of Minnesota 2023, Chapter 64, Article 8, Section 3 provides; Laws of Minnesota 2025, 1st Special Session, "
        "Chapter 13, Article 5, Sec 12 extends; Minnesota Session Laws 2024, Regular Session, Chapter 127; Laws 2006, chapter 257."
    )
    found = tx.extract_session_law_citations(text)
    assert [(year, special, chapter) for year, special, chapter, _ in found] == [(2023, 0, "64"), (2025, 1, "13"), (2024, 0, "127"), (2006, 0, "257")]
    assert found[0][3] == "Laws of Minnesota 2023, Chapter 64, Article 8, Section 3"
    assert tx.extract_session_law_citations("the laws of 2020 were many") == []


def test_ordinance_citations():
    text = "the Recycling Ordinance Number 13, Hennepin County Ordinance No. 26, Ordinance 9 - Section VII, and city ordinance 2023-14"
    assert tx.extract_ordinance_citations(text) == [("13", "Ordinance Number 13"), ("26", "Ordinance No. 26"), ("9", "Ordinance 9")]


# --- the Revisor's pages --------------------------------------------------------------------


def test_chapter_page():
    chapter = revisor.parse_chapter(CHAPTER)
    assert (chapter.number, chapter.title, chapter.edition) == ("383B", "HENNEPIN COUNTY", 2026)
    repealed, design, pay = chapter.sections
    assert (repealed.number, repealed.status, repealed.group) == ("383B.031", "repealed", "ELECTIONS")
    assert repealed.note == "MS 2022 [Repealed, 2024 c 112 art 2 s 81]" and repealed.history_laws == [(2024, 0, "112")]
    assert (design.headnote, design.group, design.status, design.text) == ("DESIGN-BUILD CONTRACTS", "DESIGN-BUILD CONTRACTS", "in force", "")
    assert [(d.number, d.headnote, d.status) for d in design.subdivisions] == [
        ("1", "Definitions", "in force"), ("2", "Authority", "in force"), ("3", None, "repealed"),
    ]  # fmt: skip
    assert design.subdivisions[0].text.startswith("(a) In sections 383B.158 to 383B.1584") and "\n(b)" in design.subdivisions[0].text
    assert design.subdivisions[0].references == ["383B.1584", "326"] and design.subdivisions[1].references == ["471.345"]
    assert design.history == "2002 c 393 s 57; 2026 c 88 art 1 s 5" and design.history_laws == [(2002, 0, "393"), (2026, 0, "88")]
    assert pay.text == "No per diem payment shall be allowed county board members.\nCounty Auditor" and pay.history == "1982 c 577 s 14"


def test_table_of_changes_and_law_list():
    total, changes = revisor.parse_table2(TABLE2)
    assert total == 3
    assert [(c.statute, c.subdivision, c.action, c.year, c.special, c.law_chapter, c.article, c.section, c.law_anchor) for c in changes] == [
        ("383B.158", "2", "Amended", 2026, 0, "88", "1", "5", "laws.1.5.0"),
        ("383B.031", None, "Repealed", 2026, 0, "88", "1", "6", "laws.1.6.0"),
        ("999.01", None, "New", 2026, 0, "90", None, "1", "laws.0.1.0"),
    ]
    laws = revisor.parse_session_laws(LAW_LIST)
    assert [(l.year, l.special, l.chapter, l.bill, l.presented) for l in laws] == [
        (2026, 0, "88", "SF4244", date(2026, 5, 11)), (2026, 0, "90", "HF1", date(2026, 5, 12)),
    ]  # fmt: skip


def test_session_law_text_keeps_struck_and_inserted_words():
    parsed = revisor.parse_law(LAW)
    assert parsed.title == "CHAPTER 88--S.F.No. 4244" and parsed.summary.startswith("relating to counties")
    amended = parsed.sections["laws.1.5.0"]
    assert (amended.kind, amended.label) == ("am_subd", "Sec. 5.")
    assert amended.heading == "Minnesota Statutes 2024, section 383B.158, subdivision 2, is amended to read:"
    assert "for a project [-over $5,000,000-]{+of any size+}." in amended.marked and "deleted text begin" not in amended.marked
    assert amended.effective == "This section is effective August 1, 2026."
    assert parsed.sections["laws.1.6.0"].kind == "repealer" and parsed.sections["laws.1.6.0"].effective is None


def test_ids_and_labels():
    assert revisor.statute_id("383B.158", "4") == "mnstat:383B.158#4" and revisor.session_law_id(2025, 13, 1) == "mnlaw:2025s1:13"
    assert revisor.label("session_law", {"year": 2025, "special": 1, "chapter": "13"}) == "Laws 2025, 1st Spec. Sess., ch. 13"
    assert sorted(["383B.16", "13D", "383B.158", "103B", "13", "383B.1581"], key=law.chapter_sort_key) == [
        "13", "13D", "103B", "383B.158", "383B.1581", "383B.16",
    ]  # fmt: skip


# --- fetching -----------------------------------------------------------------------------


def _site(routes: dict[str, bytes], seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        for fragment, body in routes.items():
            if fragment in str(request.url):
                return httpx.Response(200, content=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_fetcher_refuses_disallowed_paths_and_identifies_itself():
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: (seen.append(request), httpx.Response(200, content=b"ok"))[1])
    with revisor.fetcher(requests_per_second=1000, transport=transport) as http:
        with pytest.raises(Disallowed):
            http.get("https://www.revisor.mn.gov/bills/status_results.php?body=House")
        assert http.get("https://www.revisor.mn.gov/statutes/cite/383B/full").content == b"ok"
    assert len(seen) == 1 and seen[0].headers["user-agent"].startswith("legistutor/")


def test_revisor_ingest_fetches_once_and_keeps_editions(tmp_path):
    seen: list[str] = []
    routes = {"/statutes/cite/383B/full": CHAPTER, "/statutes/table2": TABLE2, "/laws/2026/0/Session+Law/Chapter/88/": LAW, "/laws/2026/0/": LAW_LIST}
    pages = revisor.store(tmp_path)
    with revisor.fetcher(requests_per_second=1000, transport=_site(routes, seen)) as http:
        report = revisor.ingest_chapters(pages, http, ["383B", "999"])
        assert (report["fetched"], report["missing"]) == (1, 1)
        assert pages.exists("statutes/current/383B.html") and pages.exists("statutes/2026/383B.html")  # filed by edition too
        assert revisor.ingest_chapters(pages, http, ["383B", "999"])["skipped"] == 2  # fresh, and the 404 is remembered
        assert revisor.ingest_table2(pages, http, [2026]) == {"years": 1, "fetched_pages": 1, "skipped_years": 0, "rows": 3}
        assert revisor.ingest_table2(pages, http, [2026])["skipped_years"] == 1  # a finished session is read once
        assert revisor.ingest_table2(pages, http, [2026], refresh_from=2026)["fetched_pages"] == 1  # the current one again
        assert revisor.ingest_session_laws(pages, http, [2026]) == {"lists": 1, "skipped": 0}
        assert law.laws_to_fetch(tmp_path) == [(2026, 0, "88")]  # chapter 90 changed nothing that was fetched
        assert revisor.ingest_laws(pages, http, law.laws_to_fetch(tmp_path))["fetched"] == 1
        assert revisor.ingest_laws(pages, http, law.laws_to_fetch(tmp_path))["skipped"] == 1
    assert sum("/statutes/cite/383B/full" in url for url in seen) == 1


def test_bills_ingest_takes_the_archive_once_and_keeps_the_key_to_the_api(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "v3.openstates.org":
            return httpx.Response(200, json=SESSIONS)
        return httpx.Response(200, content=bills_archive())

    transport = httpx.MockTransport(handler)
    with pytest.raises(openstates.MissingKey):
        openstates.ingest(tmp_path, None, transport=transport)
    report = openstates.ingest(tmp_path, "secret", transport=transport)
    assert (report["sessions"], report["archives_fetched"], report["api_requests"]) == (1, 1, 1)  # 2021-2022 is before since_year
    assert openstates.ingest(tmp_path, "secret", transport=transport)["archives_fetched"] == 0  # same archive date
    assert [request.headers.get("x-api-key") for request in seen] == ["secret", None, "secret"]


def test_bills_read_the_archive_and_prefer_the_api_copy(tmp_path):
    pages = openstates.store(tmp_path)
    pages.write("bulk/2025-2026.zip", bills_archive())
    pages.write("bills/2025-2026/HF1.json", json.dumps(API_BILL).encode())
    bills = {bill["id"]: bill for bill in openstates.read_bills(tmp_path)}
    assert set(bills) == {"mnbill:2025-2026:SF4244", "mnbill:2025-2026:HF9", "mnbill:2025-2026:HF1"}
    senate = bills["mnbill:2025-2026:SF4244"]
    assert (senate["chamber"], senate["sponsors"], senate["subjects"]) == ("Senate", "Dibble", "Counties; Contracts")
    assert (senate["first_action"], senate["last_action"], senate["passed"]) == (date(2026, 2, 26), date(2026, 5, 12), date(2026, 4, 29))
    assert senate["companions"] == [{"session": "2025-2026", "identifier": "HF 9"}] and senate["actions"][0]["chamber"] == "Senate"
    assert bills["mnbill:2025-2026:HF1"]["title"] == "Road funding provided; money appropriated."  # the API's newer title
    assert openstates.sessions_for(2026) == ["2025-2026"] and openstates.sessions_for(2025, 1) == ["2025s1"]


# --- ordinances ---------------------------------------------------------------------------


def test_ordinance_pages():
    site = "https://county.example/government/about/ordinances"
    assert ordinances.child_links(ORDINANCE_INDEX, site) == [f"{site}/solid-waste-ordinances"]
    assert ordinances.child_links(ORDINANCE_CATEGORY, f"{site}/solid-waste-ordinances") == [f"{site}/solid-waste-ordinances/recycling-ordinance"]
    parsed = ordinances.parse_ordinance(ORDINANCE, f"{site}/solid-waste-ordinances/recycling-ordinance")
    assert (parsed.title, parsed.number, parsed.adopted, parsed.amended) == ("Recycling ordinance", "13", date(1986, 10, 30), date(2018, 11, 27))
    assert parsed.pdf_url == "https://county.example/-/media/ordinance-13.pdf?rev=1" and parsed.notice == "The county is updating this ordinance."
    assert [heading for heading, _ in parsed.sections] == [None, "Section I: Definitions", "Section II: Violations"]
    assert parsed.sections[1][1].splitlines()[-2:] == ["Food scraps", "Yard waste"]


def test_ordinance_ingest_walks_index_category_page(tmp_path, monkeypatch):
    site = "https://county.example/government/about/ordinances"
    monkeypatch.setitem(ordinances.SITES, C, site)
    seen: list[str] = []
    routes = {"/recycling-ordinance": ORDINANCE, "/solid-waste-ordinances": ORDINANCE_CATEGORY, "/ordinances": ORDINANCE_INDEX}
    with Fetcher(requests_per_second=1000, transport=_site(routes, seen)) as http:
        assert ordinances.ingest(tmp_path, C, http) == {"categories": 1, "ordinances": 1, "fetched": 3, "skipped": 0, "failed": 0}
        assert ordinances.ingest(tmp_path, C, http)["skipped"] == 3
    assert ordinances.store(tmp_path, C).keys("pages") == ["pages/solid-waste-ordinances/recycling-ordinance.html"]


def test_page_store_keeps_metadata_beside_each_page(tmp_path):
    pages = PageStore(tmp_path, "somewhere")
    pages.write("a/b.html", b"<p>", url="https://example.org/b", status=200)
    assert pages.read("a/b.html") == b"<p>" and pages.meta("a/b.html")["url"] == "https://example.org/b"
    assert pages.keys("a") == ["a/b.html"] and pages.age_days("a/b.html") < 1 and pages.age_days("a/missing.html") is None


# --- the graphs, and their join to the record -------------------------------------------------


@pytest.fixture
def joined(tmp_path):
    """The record of the search tests, plus one item that cites law precisely, plus the law."""
    _write_bronze(tmp_path)
    store = BronzeStore(tmp_path, C)
    precise = _matter(16, "26-0006", TEMPLATE.format(
        "BE IT RESOLVED, that a design-build contract be awarded under Minnesota Statutes, section 383B.158, subdivision 2.",
        "Authorized by Laws of Minnesota 2026, Chapter 88, Article 1, Section 5, and consistent with Ordinance 13 and "
        "Ordinance 44. See also Minn. Stat. § 383B.031 and Minn. Stat. § 383B.999."), requester="Public Works - Roads")  # fmt: skip
    store.write("matters", 16, precise)
    store.write("events", 4, _event(4, BOARD, "Board", "2026-06-02", [_item(400, 1, matter=16, action="adopt", passed="Pass")]))
    silver.build(tmp_path, C)
    write_law_bronze(tmp_path, C)
    return tmp_path, law.build(tmp_path), ordinances.build(tmp_path, C)


def test_law_graph(joined):
    _, report, _ = joined
    assert report["vertices"] == {"statute": 4, "subdivision": 3, "session_law": 5, "bill": 3}
    assert report["edges"]["enacted_as"] == 2 and report["edges"]["companion"] == 1
    # one statewide change is outside the chapter fetched: in the table, not on an edge
    assert report["tables"]["statute_change"] == 3 and report["tables"]["law_section"] == 2
    assert report["edges_to_statutes_not_fetched"] == {"cross_references": 3}


def test_the_record_and_the_law_are_one_graph(joined):
    data_dir, _, _ = joined
    con = query.connect(data_dir, C)

    def rows(sql):
        return con.sql(sql).fetchall()

    # the record's stub gives way to the fetched section; a section nobody published stays a stub
    assert rows("select number, status, headnote from v_statute where number in ('383B.158', '383B.031', '383B.999') order by 1") == [
        ("383B.031", "repealed", None), ("383B.158", "in force", "DESIGN-BUILD CONTRACTS"), ("383B.999", None, None),
    ]  # fmt: skip
    assert rows("select count(*), count(distinct id) from v_statute")[0] in [(6, 6), (7, 7)]
    assert rows("select id, count(*) from vertices group by 1 having count(*) > 1") == []
    assert rows("select m.file_number, d.headnote from e_cites_subdivision c join v_matter m on m.id = c.src join v_subdivision d on d.id = c.dst") == [
        ("26-0006", "Authority")
    ]
    assert rows("select l.bill, l.presented, c.citation from e_cites_law c join v_session_law l on l.id = c.dst") == [
        ("SF4244", date(2026, 5, 11), "Laws of Minnesota 2026, Chapter 88, Article 1, Section 5")
    ]
    # Table 2 is the record of a change where it has one; the History line fills in older years
    assert rows("select src, year, action, basis from e_changed_by where statute = '383B.158' order by year") == [
        ("mnstat:383B.158", 2002, None, "history"), ("mnstat:383B.158#2", 2026, "Amended", "table2"),
    ]  # fmt: skip
    assert rows("select o.title, o.amended from e_cites_ordinance c join v_ordinance o on o.id = c.dst order by o.number") == [
        ("Recycling ordinance", date(2018, 11, 27)), (None, None),  # Ordinance 44 is cited but not published
    ]  # fmt: skip
    assert rows("select dst, states_authority from e_ordinance_cites order by 1") == [("mnstat:115A.551", True), ("mnstat:383B.158", False)]
    assert rows("select b.identifier from e_enacted_as e join v_bill b on b.id = e.src where e.dst = 'mnlaw:2026:88'") == [("SF 4244",)]


@pytest.fixture
def app(joined):
    data_dir, _, _ = joined
    embedder = FakeEmbedder()
    report = gold.build(data_dir, C, embedder)
    assert report["law"]["by_kind"] == {"statute": 1, "subdivision": 2, "ordinance": 3}  # repealed text is not indexed
    chat = FakeChat()
    application = web.App(Searcher(data_dir, C, embedder), lambda: chat, client=C, embed_model=embedder.model)
    application.fake_chat = chat
    return application


def test_search_finds_law_apart_from_the_record(app):
    found = app.search({"q": "may the county board award a design-build contract"})
    assert "26-0006" in [hit.file_number for hit in found["hits"]]
    top = found["law"][0]
    assert (top.id, top.label, top.title, top.cited_by) == ("mnstat:383B.158", "Minn. Stat. § 383B.158", "DESIGN-BUILD CONTRACTS", 2)
    assert top.passages[0].label == "Minn. Stat. § 383B.158, subd. 2" and top.passages[0].source == "mnstat:383B.158#2"
    assert app.search({"q": "26-0006"})["law"] == []  # a file number asks for an item
    organics = app.search({"q": "organics food scraps yard waste"})["law"][0]
    assert (organics.kind, organics.label, organics.cited_by) == ("ordinance", "Ordinance 13", 1)


def test_ask_adds_law_only_to_questions_about_the_law(app):
    events = list(app.ask({"q": "what authority does the county board have to award a design-build contract?", "top": "2"}))
    sources = events[0][1]
    assert sources["law"][0].id == "mnstat:383B.158" and len(sources["hits"]) == 2
    prompt = app.fake_chat.prompts[-1]
    assert '<source number="3" kind="law">' in prompt and "Minn. Stat. § 383B.158, subd. 2 (Authority), current text" in prompt
    assert events[-1][1]["unsupported"] == []  # "[1]." from the fake model
    app.fake_chat.pieces = ("The board may award it [3], see also [9].",)
    assert list(app.ask({"q": "what does the law say about design-build?", "top": "2"}))[-1][1]["unsupported"] == [
        "cites source [9], which does not exist"
    ]
    assert list(app.ask({"q": "what happened to the design-build contract", "top": "2"}))[0][1]["law"] == []
    assert rag.asks_about_law("Is that allowed by statute?") and not rag.asks_about_law("who authorized the paving agreement")


def test_changes_are_ranked_by_what_the_record_rests_on(app):
    data = app.changes({})
    assert (data["year"], data["total"]) == (2026, 2)
    assert data["years"] == [{"year": 2026, "counts": {"Amended": 1, "New": 0, "Repealed": 1, "Other": 0}, "total": 2}]
    first, second = data["changes"]
    assert (first["statute"], first["subdivision"], first["action"], first["law_label"], first["bill"]) == ("383B.158", "2", "Amended", "Laws 2026, ch. 88", "SF4244")
    assert (first["matters"], first["subdivision_matters"], first["ordinances"]) == (2, 1, 1)
    assert (first["effective"], first["has_text"], first["presented"]) == ("This section is effective August 1, 2026.", True, date(2026, 5, 11))
    assert (second["statute"], second["action"], second["matters"]) == ("383B.031", "Repealed", 1)
    assert [(c["label"], c["status"], c["file_number"]) for c in data["to_check"]] == [
        ("Minn. Stat. § 383B.031", "repealed", "26-0006"), ("Minn. Stat. § 383B.999", "not found", "26-0006"),
    ]  # fmt: skip
    text = app.law_text({"law": first["law"], "anchor": first["anchor"]})
    assert "[-over $5,000,000-]{+of any size+}" in text["marked"] and text["url"].endswith("/Chapter/88/#laws.1.5.0")
    with pytest.raises(web.NotFound):
        app.law_text({"law": "mnlaw:2026:90", "anchor": "laws.0.1.0"})


def test_law_nodes(app):
    section = app.node({"id": "mnstat:383B.158"})
    assert (section["headnote"], section["status"], section["edition"], section["total"]) == ("DESIGN-BUILD CONTRACTS", "in force", 2026, 2)
    assert [(d["number"], d["status"], d["matters"]) for d in section["subdivisions"]] == [("1", "in force", 0), ("2", "in force", 1), ("3", "repealed", 0)]
    assert [c["action"] for c in section["changes"]] == ["Amended"] and section["ordinances"][0]["label"] == "Ordinance 13: Recycling ordinance"
    assert app.node({"id": "mnstat:383B.158#2"})["focus"] == "mnstat:383B.158#2"
    chapter = app.node({"id": "mnstat:383B"})
    assert [(s["number"], s["matters"], s["changes"]) for s in chapter["sections"]] == [
        ("383B.021", 0, 0), ("383B.031", 1, 1), ("383B.158", 2, 1), ("383B.999", 1, 0),  # the last is cited but was never published
    ]  # fmt: skip
    assert app.node({"id": "mnstat:383B.999"})["fetched"] is False
    act = app.node({"id": "mnlaw:2026:88"})
    assert (act["bill_node"]["identifier"], act["presented"], act["total"]) == ("SF 4244", date(2026, 5, 11), 1)
    assert [c["statute"] for c in act["changes"]] == ["383B.158", "383B.031"] and act["citations"][0]["file_number"] == "26-0006"
    ordinance = app.node({"id": f"{C}:ordinance:13"})
    assert (ordinance["title"], ordinance["fetched"], ordinance["total"]) == ("Recycling ordinance", True, 1)
    assert [(s["label"], s["states_authority"], s["status"]) for s in ordinance["statutes"]] == [
        ("Minn. Stat. § 115A.551", True, None), ("Minn. Stat. § 383B.158", False, "in force"),
    ]  # fmt: skip
    assert app.node({"id": f"{C}:ordinance:44"})["fetched"] is False
    bill = app.node({"id": "mnbill:2025-2026:SF4244"})
    assert (bill["law"]["label"], bill["companions"][0]["identifier"], len(bill["actions"])) == ("Laws 2026, ch. 88", "HF 9", 3)
    groups = {group["name"]: [node["label"] for node in group["nodes"]] for group in app.matter({"file": "26-0006"})["neighbours"]}
    assert groups["Cites session law"] == ["Laws 2026, ch. 88"] and groups["Cites ordinance"] == ["Ordinance 13: Recycling ordinance", "Ordinance 44"]


def test_lists_of_ordinances_and_bills(app):
    assert [(o["number"], o["matters"], o["statutes"]) for o in app.ordinances()["ordinances"]] == [("13", 1, 2)]
    found = app.bills({"q": "hennepin design-build"})
    assert [(b["identifier"], b["law_label"]) for b in found["bills"]] == [("SF 4244", "Laws 2026, ch. 88"), ("HF 9", None)]
    assert [b["identifier"] for b in app.bills({"q": "", "enacted": "1"})["bills"]] == ["SF 4244", "HF 1"]
    meta = app.meta()["law"]
    assert (meta["chapters"], meta["sections"], meta["years"], meta["ordinances"], meta["bills"], meta["searchable"]) == (1, 3, [2026], 1, 3, True)


def test_without_any_law_the_page_says_so(tmp_path):
    _write_bronze(tmp_path)
    embedder = FakeEmbedder()
    assert "law" not in gold.build(tmp_path, C, embedder)
    app = web.App(Searcher(tmp_path, C, embedder), FakeChat, client=C, embed_model=embedder.model)
    assert app.meta()["law"] is None and app.changes({}) == {"available": False}
    assert app.search({"q": "paving agreement"})["law"] == [] and app.ordinances()["ordinances"] == []
    assert app.node({"id": "mnstat:383B.158"})["total"] == 1  # the plain list of citing items still works
