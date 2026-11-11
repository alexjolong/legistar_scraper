"""Small pages in the shape of the real ones, for the law-source tests.

Each mirrors the markup of the site it stands in for (class names, ids, link
shapes), cut down to a few elements. ``write_law_bronze`` files them where the
ingest would.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile

from legistutor import openstates, ordinances, revisor

CHAPTER = b"""<html><head><title>Ch. 383B MN Statutes</title></head><body>
<div id="header"><h1> 2026 Minnesota Statutes </h1></div>
<div id="xtend" class="statute"><div class="row"><div class="col-sm order-1">
<h2 class="chapter_title">CHAPTER 383B. HENNEPIN COUNTY</h2>
<div id="chapter_analysis"><table><tbody><tr class="fgh"><th colspan="2">ELECTIONS</th></tr>
<tr><td><a href="#stat.383B.158">383B.158</a></td><td>DESIGN-BUILD CONTRACTS.</td></tr></tbody></table></div>
<h1 class="fgh">ELECTIONS</h1>
<div class="sr" id="stat.383B.031"><b>383B.031</b> MS 2022 [Repealed, <a href="/laws/2024/0/112/#laws.2.81.0">2024 c 112 art 2 s 81</a>]</div>
<h1 class="fgh">DESIGN-BUILD CONTRACTS</h1>
<div class="section" id="stat.383B.158">
<h1 class="shn">383B.158 DESIGN-BUILD CONTRACTS.</h1>
<div class="subd" id="stat.383B.158.1"><a title="Link to Subdivision 1." class="permalink" href="#stat.383B.158.1">&#167;</a>
<h2 class="subd_no">Subdivision 1.<span class="headnote">Definitions.</span></h2>
<p>(a) In sections <a href="/statutes/2026/cite/383B.158">383B.158</a> to <a href="/statutes/2026/cite/383B.1584">383B.1584</a>, the definitions in this subdivision apply.</p>
<p>(b) "Design professional" means a person licensed under chapter 326.</p></div>
<div class="subd" id="stat.383B.158.2"><a class="permalink" href="#stat.383B.158.2">&#167;</a>
<h2 class="subd_no">Subd. 2.<span class="headnote">Authority.</span></h2>
<p>Notwithstanding section <a href="/statutes/cite/471.345">471.345</a>, the county board may solicit and award a design-build contract on the basis of a best value selection process.</p></div>
<div class="subd" id="stat.383B.158.3"><h2 class="subd_no">Subd. 3.</h2><p>[Repealed, 2007 c 70 s 4]</p></div>
</div>
<div class="history" id="stat.383B.158.history"><h2>History: </h2><p class="first"><a href="/laws/2002/0/393/#laws.0.57.0">2002 c 393 s 57</a>; <a href="/laws/2026/0/88/#laws.1.5.0">2026 c 88 art 1 s 5</a></p></div>
<h1 class="fgh">COUNTY BOARD</h1>
<div class="section" id="stat.383B.021"><h1 class="shn">383B.021 COMPENSATION.</h1>
<p class="in">No per diem payment shall be allowed county board members.</p>
<table><tr><td/><td><span class="leader">.</span></td></tr><tr><td/><td>County Auditor</td></tr></table></div>
<div class="history"><h2>History: </h2><p class="first"><a href="/laws/1982/0/577/#laws.0.14.0">1982 c 577 s 14</a></p></div>
</div></div></div></body></html>"""

TABLE2 = b"""<html><body><form id="form1" method="get" action=""><p><span>1 to 3 of 3 Records for </span>
<input type="text" name="search_term" value="ALL"></p></form>
<table><tr class="sort_options"><th>Text</th></tr>
<tr class="table2"><td><a href="/statutes/2025/cite/383B.158/subd/383B.158.2">2025</a></td><td><a href="/statutes/2026/cite/383B.158/subd/383B.158.2">2026</a></td>
<td><a href="?full=383B.158">383B.158</a></td><td>2</td><td>Amended</td><td>2026 Regular Session</td>
<td><a href="/laws/2026/0/88/laws.1.5.0">88</a></td><td>1</td><td>5</td></tr>
<tr class="table2"><td><a href="/statutes/2025/cite/383B.031">2025</a></td><td></td>
<td><a href="?full=383B.031">383B.031</a></td><td></td><td>Repealed</td><td>2026 Regular Session</td>
<td><a href="/laws/2026/0/88/laws.1.6.0">88</a></td><td>1</td><td>6</td></tr>
<tr class="table2"><td></td><td><a href="/statutes/2026/cite/999.01">2026</a></td>
<td><a href="?full=999.01">999.01</a></td><td></td><td>New</td><td>2026 Regular Session</td>
<td><a href="/laws/2026/0/90/laws.0.1.0">90</a></td><td></td><td>1</td></tr>
</table></body></html>"""

LAW_LIST = b"""<html><body><h2>Session Law</h2><table id="session_table"><thead><tr><th>Chapter</th><th>Bill</th>
<th>Presentment Date</th><th>Veto</th></tr></thead>
<tr><td><a href="https://www.revisor.mn.gov/laws/2026/0/Session+Law/Chapter/88/">Chapter&nbsp;88</a></td>
<td> <a href="/bills/94/2026/0/SF/4244/?body=senate">SF4244</a> </td><td>May 11, 2026</td><td></td></tr>
<tr><td><a href="https://www.revisor.mn.gov/laws/2026/0/Session+Law/Chapter/90/">Chapter&nbsp;90</a></td>
<td> <a href="/bills/94/2026/0/HF/1/?body=house">HF1</a> </td><td>May 12, 2026</td><td></td></tr>
</table></body></html>"""

LAW = b"""<html><body><div id="xtend" class="law"><h1 class="title">CHAPTER 88--S.F.No. 4244</h1>
<div id="btitle"><p class="btitle_prolog">An act</p><p class="btitle_summary">relating to counties; modifying design-build authority;</p>
<p class="btitle_action">amending Minnesota Statutes 2024, section 383B.158, subdivision 2.</p></div>
<div class="article" id="laws.1.0.0">
<div class="bill_section am_subd" id="laws.1.5.0"><h1 class="bill_sec_no">Sec. 5. </h1>
<h1 class="bill_sec_header">Minnesota Statutes 2024, section 383B.158, subdivision 2, is amended to read:</h1>
<div class="subd" id="laws.1.5.1"><h2 class="subd_no">Subd. 2.</h2><h2 class="hn">Authority.</h2>
<p>The county board may award a design-build contract for a project <span class="sr-only">deleted text begin </span><span style="text-decoration: line-through" class="del">over $5,000,000</span><span class="sr-only">deleted text end </span><span class="sr-only">new text begin </span><ins style="text-decoration: underline">of any size</ins><span class="sr-only">new text end </span>.</p></div>
<p><span class="sr-only">new text begin </span><ins style="text-decoration: underline">EFFECTIVE DATE.</ins><span class="sr-only">new text end </span>
<span class="sr-only">new text begin </span><ins style="text-decoration: underline">This section is effective August 1, 2026.</ins><span class="sr-only">new text end </span></p>
</div>
<div class="bill_section repealer" id="laws.1.6.0"><h1 class="bill_sec_no">Sec. 6. </h1>
<h2 class="hn"><ins>REPEALER.</ins></h2><div class="repeal_group"><p><ins>Minnesota Statutes 2024, section 383B.031, is repealed.</ins></p></div></div>
</div></div></body></html>"""

ORDINANCE_INDEX = b"""<html><body><a href="/government/about/ordinances">Ordinances</a>
<a href="/government/about/ordinances/solid-waste-ordinances">Solid waste ordinances</a>
<a href="/government/about">About</a></body></html>"""

ORDINANCE_CATEGORY = b"""<html><body><a href="/government/about/ordinances/solid-waste-ordinances">Solid waste ordinances</a>
<a href="/government/about/ordinances/solid-waste-ordinances/recycling-ordinance">Recycling ordinance</a></body></html>"""

ORDINANCE = b"""<html><head><meta name="description" content="Ordinance 13 concerns recycling."></head><body>
<section><h1 id="bannerHeadingID">Recycling ordinance</h1><p><div>Ordinance 13</div></p></section>
<section class="bg-gray-100"><div class="hc-in-page-alert prose"><div><p>The county is updating this ordinance.</p></div></div></section>
<section class="hc-rich-text mx-4"><div><p>Adopted by the County Board of Commissioners October 30, 1986</p>
<p>Amended on November 27, 2018</p><p><a href="/-/media/ordinance-13.pdf?rev=1">Ordinance 13 (PDF, 1MB)</a></p>
<p>This Ordinance is authorized pursuant to Minnesota Statutes, section 115A.551.</p></div></section>
<section class="hc-rich-text mx-4"><div><h2 id="s1">Section I: Definitions</h2><h3>A.</h3>
<p>"Organics" has the meaning given in Minnesota Statutes, section 383B.158.</p><ul><li>Food scraps</li><li>Yard waste</li></ul>
<h2 id="s2">Section II: Violations</h2><p>A violation is a misdemeanor.</p></div></section></body></html>"""

SESSIONS = {
    "legislative_sessions": [
        {"identifier": "2021-2022", "start_date": "2021-01-05", "end_date": "2022-05-23", "downloads": []},
        {
            "identifier": "2025-2026",
            "start_date": "2025-01-14",
            "end_date": "2026-05-20",
            "downloads": [{"data_type": "csv", "updated_at": "2026-11-02T23:18:16+00:00", "url": "https://data.openstates.org/csv/latest/MN_2025-2026.zip"}],
        },
    ]
}


def bills_archive() -> bytes:
    """A session archive in the layout Open States publishes: one CSV per table."""
    tables = {
        "bills": [
            ["id", "identifier", "title", "classification", "subject", "session_identifier", "jurisdiction", "organization_classification"],
            ["b1", "SF 4244", "Hennepin County design-build authority modified.", "['bill']", "['Counties', 'Contracts']", "2025-2026", "Minnesota", "upper"],
            ["b2", "HF 9", "Hennepin County design-build authority modified.", "['bill']", "[]", "2025-2026", "Minnesota", "lower"],
            ["b3", "HF 1", "Road funding provided.", "['bill']", "[]", "2025-2026", "Minnesota", "lower"],
        ],
        "bill_actions": [
            ["id", "bill_id", "organization_id", "description", "date", "classification", "order"],
            ["a2", "b1", "senate", "Third reading Passed", "2026-04-29", "['reading-3', 'passage']", "1"],
            ["a1", "b1", "senate", "Introduction and first reading", "2026-02-26", "['introduction']", "0"],
            ["a3", "b1", "senate", "Secretary of State Chapter 88 05/12/26", "2026-05-12", "[]", "2"],
            ["a4", "b3", "house", "Introduction and first reading", "2025-01-16T00:00:00+00:00", "['introduction']", "0"],
        ],
        "bill_related_bills": [
            ["id", "bill_id", "related_bill_id", "identifier", "legislative_session", "relation_type"],
            ["r1", "b1", "b2", "HF 9", "2025-2026", "companion"],
        ],
        "bill_sources": [["id", "note", "url", "bill_id"], ["s1", "", "https://www.revisor.mn.gov/bills/94/2026/0/SF/4244?body=Senate", "b1"]],
        "bill_sponsorships": [
            ["id", "name", "entity_type", "organization_id", "person_id", "bill_id", "primary", "classification"],
            ["p1", "Dibble", "person", "", "", "b1", "True", "primary"],
            ["p2", "Port", "person", "", "", "b1", "False", "cosponsor"],
        ],
        "organizations": [["id", "name", "classification"], ["senate", "Senate", "upper"], ["house", "House", "lower"]],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, rows in tables.items():
            text = io.StringIO()
            csv.writer(text).writerows(rows)
            archive.writestr(f"MN/2025-2026/MN_2025-2026_{name}.csv", text.getvalue())
    return buffer.getvalue()


API_BILL = {
    "session": "2025-2026",
    "identifier": "HF 1",
    "title": "Road funding provided; money appropriated.",
    "from_organization": {"classification": "lower"},
    "classification": ["bill"],
    "subject": ["Transportation"],
    "updated_at": "2026-11-05T00:00:00+00:00",
    "sponsorships": [{"name": "Koegel", "primary": True}],
    "sources": [{"url": "https://www.revisor.mn.gov/bills/94/2025/0/HF/1?body=House"}],
    "actions": [{"date": "2025-01-16", "order": 0, "organization": {"classification": "lower"}, "description": "Introduction and first reading", "classification": ["introduction"]}],
    "related_bills": [],
}


def write_law_bronze(data_dir, client: str = "test") -> None:
    """File the pages above where ``legistutor ingest-law`` would."""
    pages = revisor.store(data_dir)
    pages.write(revisor.chapter_key("383B"), CHAPTER, url="https://www.revisor.mn.gov/statutes/cite/383B/full")
    pages.write("table2/2026/1.html", TABLE2, complete=True)
    pages.write("laws/2026/0/index.html", LAW_LIST)
    pages.write(revisor.law_key(2026, "88"), LAW)
    site = "https://county.example/government/about/ordinances"
    ordinance_pages = ordinances.store(data_dir, client)
    ordinance_pages.write("index.html", ORDINANCE_INDEX, url=site)
    ordinance_pages.write("categories/solid-waste-ordinances.html", ORDINANCE_CATEGORY, url=f"{site}/solid-waste-ordinances")
    ordinance_pages.write(
        "pages/solid-waste-ordinances/recycling-ordinance.html", ORDINANCE, url=f"{site}/solid-waste-ordinances/recycling-ordinance"
    )
    bills = openstates.store(data_dir)
    bills.write("bulk/2025-2026.zip", bills_archive(), updated_at="2026-11-02T23:18:16+00:00")
    bills.write("bills/2025-2026/HF1.json", json.dumps(API_BILL).encode())
