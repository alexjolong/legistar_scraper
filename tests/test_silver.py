"""Build the silver graph from a tiny hand-made bronze layer and check its shape."""

from __future__ import annotations

from datetime import date

import pytest

from legistar_scraper import query, silver
from legistar_scraper.bronze import BronzeStore

C = "test"
BOARD, PUBLIC_WORKS = 1, 2


def _item(item_id, seq, *, matter=None, title=None, action=None, passed=None, text=None, mover=None, note=None):
    return {
        "EventItemId": item_id,
        "EventItemAgendaSequence": seq,
        "EventItemMinutesSequence": seq,
        "EventItemAgendaNumber": f"{seq}.",
        "EventItemTitle": title,
        "EventItemMatterId": matter,
        "EventItemActionName": action,
        "EventItemPassedFlagName": passed,
        "EventItemConsent": 0,
        "EventItemActionText": text,
        "EventItemMinutesNote": note,
        "EventItemAgendaNote": None,
        "EventItemMoverId": mover,
        "EventItemMover": "Ann Aye" if mover else None,
        "EventItemSeconderId": None,
        "EventItemSeconder": None,
    }


def _event(event_id, body_id, body, day, items, *, minutes="Final", agenda="Final"):
    return {
        "EventId": event_id,
        "EventBodyId": body_id,
        "EventBodyName": body,
        "EventDate": f"{day}T00:00:00",
        "EventTime": "1:30 PM",
        "EventAgendaStatusName": agenda,
        "EventMinutesStatusName": minutes,
        "EventInSiteURL": f"https://example.test/meeting/{event_id}",
        "EventItems": items,
    }


def _matter(matter_id, file_number, text, *, requester=None, relations=(), histories=()):
    return {
        "MatterId": matter_id,
        "MatterFile": file_number,
        "MatterTitle": f"Title of {file_number}",
        "MatterTypeName": "Resolution",
        "MatterStatusName": "Approved",
        "MatterBodyName": "Board",
        "MatterIntroDate": "2026-01-01T00:00:00",
        "MatterRequester": requester,
        "MatterLastModifiedUtc": "2026-02-01T00:00:00",
        "MatterTexts": [{"MatterTextId": 1, "MatterTextVersion": "1", "MatterTextPlain": text}],
        "MatterHistories": list(histories),
        "MatterAttachments": [
            {"MatterAttachmentId": 5, "MatterAttachmentName": "Map", "MatterAttachmentHyperlink": "https://x/a.pdf", "MatterAttachmentFileName": "a.PDF"}
        ],
        "MatterRelations": [{"MatterRelationMatterId": other, "MatterRelationFlag": flag} for other, flag in relations],
    }


@pytest.fixture
def report(tmp_path):
    store = BronzeStore(tmp_path, C)
    store.write("reference", "bodies", {"rows": [
        {"BodyId": BOARD, "BodyName": "Board", "BodyTypeName": "Primary Legislative Body", "BodyActiveFlag": 1},
        {"BodyId": PUBLIC_WORKS, "BodyName": "Public Works Committee", "BodyTypeName": "Committees", "BodyActiveFlag": 1},
    ]})
    store.write("reference", "matter_statuses", {"rows": [{"MatterStatusId": 68, "MatterStatusName": "Referred"}]})
    store.write("reference", "persons", {"rows": [
        {"PersonId": 7, "PersonFullName": "Ann Aye", "PersonActiveFlag": 1},
        {"PersonId": 99, "PersonFullName": "Staff Account", "PersonActiveFlag": 1},
    ]})
    store.write("reference", "office_records", {"rows": [
        {"OfficeRecordPersonId": 7, "OfficeRecordFullName": "Ann Aye", "OfficeRecordBodyId": BOARD, "OfficeRecordTitle": "Commissioner",
         "OfficeRecordMemberType": "Chair", "OfficeRecordStartDate": "2023-01-01T00:00:00", "OfficeRecordEndDate": None},
        {"OfficeRecordPersonId": 8, "OfficeRecordFullName": "Clerk", "OfficeRecordBodyId": 555, "OfficeRecordTitle": None,
         "OfficeRecordMemberType": "Member", "OfficeRecordStartDate": None, "OfficeRecordEndDate": None},
    ]})

    body = "Item Description:\n..title\nT\n..end\n\nResolution:\n..body\nBE IT RESOLVED, x.\n..end\n\nBackground:\n{}"
    store.write("matters", 10, _matter(
        10, "26-0001", body.format("Per Minn. Stat. § 383B.158. See Resolution 26-0002 R1."),
        requester="Public Works - Roads", relations=[(11, 2)],
        histories=[{"MatterHistoryId": 100, "MatterHistoryMatterStatusId": 68}],
    ))
    store.write("matters", 11, _matter(11, "26-0001 R1", body.format("Revised."), relations=[(10, 1)]))
    store.write("matters", 12, _matter(12, "26-0002 R1", body.format("Unrelated."), relations=[(404, 2)]))
    store.write("matters", 13, _matter(13, "26-0003", body.format("New item.")))

    store.write("events", 1, _event(1, BOARD, "Board", "2026-02-03", [
        _item(99, 1, title="Referred to Public Works Committee"),
        _item(100, 2, matter=10, action="refer to committee", passed="Pass", mover=7,
              text="Ann Aye moved to refer to committee the Resolution to the Public Works Committee."),
        _item(101, 3, title="Open forum", note=r"{\rtf1\ansi Two residents spoke.\par}"),
    ]))
    store.write("events", 2, _event(2, PUBLIC_WORKS, "Public Works Committee", "2026-02-10", [
        _item(200, 1, matter=10, action="progress", passed="Pass", text="moved to progress the Resolution."),
    ]))
    store.write("events", 3, _event(3, BOARD, "Board", "2026-02-17", [
        _item(300, 1, matter=10, action="adopt", passed="Pass", mover=7),
        _item(301, 2, title="Referred to Public Works Committee"),
        _item(302, 3, matter=13),  # no action yet: minutes are still draft
    ], minutes="Draft"))
    store.write("events", 4, _event(4, BOARD, "Board", "2026-03-01", [_item(400, 1, matter=12)], agenda="Hidden"))

    store.write("votes", 3, {"EventId": 3, "Items": [
        {"EventItemId": 300,
         "Votes": [{"VotePersonId": 7, "VotePersonName": "Ann Aye", "VoteValueName": "Aye", "VoteEventItemId": 999}],
         "RollCalls": [{"RollCallPersonId": 7, "RollCallPersonName": "Ann Aye", "RollCallValueName": "Present"}]},
    ]})
    return silver.build(tmp_path, C), query.connect(tmp_path, C)


def rows(con, sql):
    return con.sql(sql).fetchall()


def test_counts_and_hidden_meetings(report):
    summary, con = report
    assert summary["hidden_test_meetings_skipped"] == 1
    assert summary["dangling_edges_dropped"] == {}
    assert summary["vertices"]["meeting"] == 3
    assert summary["vertices"]["matter"] == 4
    # only people who hold office in a listed body or took part; not staff accounts or hidden-body clerks
    assert rows(con, "select name from v_person") == [("Ann Aye",)]


def test_matter_path_through_bodies(report):
    _, con = report
    path = rows(con, """select o.date, o.body_name, o.section, o.action, o.status_after
                        from e_on_agenda o where o.src = 'test:matter:10' order by o.date""")
    assert path == [
        (date(2026, 2, 3), "Board", "Referred to Public Works Committee", "refer to committee", "Referred"),
        (date(2026, 2, 10), "Public Works Committee", None, "progress", None),
        (date(2026, 2, 17), "Board", None, "adopt", None),
    ]


def test_referrals_resolve_by_text_next_action_and_planned_heading(report):
    _, con = report
    referrals = rows(con, "select src, dst, action, basis from e_referred_to order by date, src")
    assert referrals == [
        ("test:matter:10", "test:body:2", "refer to committee", "action_text"),
        ("test:matter:10", "test:body:1", "progress", "next_action"),
        ("test:matter:13", "test:body:2", None, "agenda_section"),  # planned: draft meeting, no action yet
    ]


def test_text_derived_edges(report):
    _, con = report
    assert rows(con, "select dst, citation from e_cites") == [("mnstat:383B.158", "Minn. Stat. § 383B.158")]
    assert rows(con, "select src, dst from e_part_of") == [("mnstat:383B.158", "mnstat:383B")]
    assert rows(con, "select number, kind from v_statute order by number") == [("383B", "chapter"), ("383B.158", "section")]
    assert rows(con, "select src, dst, file_number from e_references") == [("test:matter:10", "test:matter:12", "26-0002R1")]
    # relation to an unknown matter (404) is dropped; the 10 -> 11 link is a revision
    assert rows(con, "select src, dst, kind from e_related_to") == [("test:matter:10", "test:matter:11", "revision")]
    assert rows(con, "select d.name from e_requested_by r join v_department d on d.id = r.dst") == [("Public Works - Roads",)]


def test_people_edges(report):
    _, con = report
    assert rows(con, "select dst, title, member_type from e_member_of") == [("test:body:1", "Commissioner", "Chair")]
    assert rows(con, "select count(*) from e_moved")[0][0] == 2
    assert rows(con, "select dst, value, via_consent_agenda from e_voted") == [("test:matter:10", "Aye", True)]
    assert rows(con, "select dst, value from e_attended") == [("test:meeting:3", "Present")]


def test_detail_tables(report):
    _, con = report
    assert rows(con, "select minutes_note from t_agenda_item where event_item_id = 101") == [("Two residents spoke.",)]
    assert rows(con, "select count(*) from t_agenda_item")[0][0] == 7
    sections = rows(con, "select section from t_matter_text where matter = 'test:matter:10' order by section")
    assert [s for (s,) in sections] == ["background", "resolution", "title"]
    assert rows(con, "select file_type from t_attachment limit 1") == [("pdf",)]


def test_unified_graph_is_graphframes_shaped(report):
    _, con = report
    assert [c[0] for c in con.sql("select * from vertices limit 0").description] == ["id", "type", "label"]
    assert [c[0] for c in con.sql("select * from edges limit 0").description] == ["src", "dst", "relationship", "date"]
    assert rows(con, "select count(*) from vertices")[0][0] == rows(con, "select count(distinct id) from vertices")[0][0]
    dangling = rows(con, """select count(*) from edges e
                            where e.src not in (select id from vertices) or e.dst not in (select id from vertices)""")
    assert dangling == [(0,)]
    assert rows(con, "select label from vertices where id = 'test:matter:10'") == [("26-0001: Title of 26-0001",)]
    assert rows(con, "select label from vertices where id = 'mnstat:383B.158'") == [("Minn. Stat. § 383B.158",)]
