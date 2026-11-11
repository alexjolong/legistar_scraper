"""Silver layer: a property graph as Parquet, built from the bronze JSON.

Layout under ``<data_dir>/silver/legistar/<client>/``::

    vertices/<type>.parquet   one table per vertex type, keyed by ``id``
    edges/<type>.parquet      one table per relationship, with ``src`` and ``dst``
    tables/<name>.parquet     detail that is not a vertex or an edge
    graph/vertices.parquet    every vertex as (id, type, label)
    graph/edges.parquet       every edge as (src, dst, relationship, date)

``id``/``src``/``dst`` are the column names GraphFrames expects, so
``GraphFrame(read(graph/vertices), read(graph/edges))`` works as is, and the typed
tables join back on ``id`` for attributes. Vertex ids are strings such as
``hennepinmn:matter:4798`` so that several tenants can share one graph; Minnesota
Statutes are tenant-independent (``mnstat:275.065``).

Law that a matter cites (a statute, a subdivision, a session law, a county ordinance)
gets a stub vertex here: its id and number, nothing more. The sources that own those
things (``silver/mnlaw``, ``silver/ordinances``) write the same ids with the text
and history, and the query layer shows their row in place of the stub.

The build is a full rebuild from bronze every time. At this size it takes seconds.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from . import revisor
from . import text as tx
from .bronze import BronzeStore

log = logging.getLogger(__name__)

# Actions that send a matter to another body.
REFERRAL_ACTIONS = frozenset(
    {"refer to committee", "refer as recommended", "return to committee", "send to board", "progress"}
)

_TYPES = {"s": pa.string(), "i": pa.int64(), "b": pa.bool_(), "d": pa.date32()}


def _schema(spec: str) -> pa.Schema:
    """``"id:s event_id:i date:d"`` -> a pyarrow schema."""
    return pa.schema([(name, _TYPES[kind]) for name, kind in (field.split(":") for field in spec.split())])


VERTEX_SCHEMAS = {
    "body": _schema("id:s body_id:i name:s body_type:s is_active:b"),
    "person": _schema("id:s person_id:i name:s is_active:b"),
    "department": _schema("id:s name:s"),
    "meeting": _schema(
        "id:s event_id:i body:s body_name:s date:d time:s location:s agenda_status:s minutes_status:s "
        "agenda_url:s minutes_url:s site_url:s comment:s"
    ),
    "matter": _schema(
        "id:s matter_id:i file_number:s title:s matter_type:s status:s body_name:s intro_date:d "
        "agenda_date:d passed_date:d department:s administrator_recommendation:s last_modified_utc:s site_url:s"
    ),
    "statute": _schema("id:s number:s kind:s chapter:s url:s"),
    "subdivision": _schema("id:s section:s subdivision:s chapter:s url:s"),
    "session_law": _schema("id:s year:i special:i chapter:s url:s"),
    "ordinance": _schema("id:s number:s"),
}

EDGE_SCHEMAS = {
    # Person -> Body
    "member_of": _schema("src:s dst:s title:s member_type:s start_date:d end_date:d"),
    # Meeting -> Body
    "held_by": _schema("src:s dst:s date:d"),
    # Matter -> Meeting: the matter was on that meeting's agenda (action is null until acted on)
    "on_agenda": _schema(
        "src:s dst:s event_item_id:i date:d body:s body_name:s agenda_number:s section:s action:s result:s "
        "consent:b status_after:s mover:s seconder:s action_text:s"
    ),
    # Matter -> Body: an action sent the matter to that body. A null action is a planned
    # referral: the item sits under a "Referred to ..." heading of a meeting not yet minuted.
    "referred_to": _schema("src:s dst:s date:d from_meeting:s event_item_id:i action:s basis:s"),
    # Matter -> Matter: earlier matter -> the later matter Legistar links to it
    "related_to": _schema("src:s dst:s kind:s"),
    # Matter -> Matter: the text of src mentions the file number of dst
    "references": _schema("src:s dst:s file_number:s"),
    # Matter -> Department
    "requested_by": _schema("src:s dst:s"),
    # Matter -> Statute (chapter or section)
    "cites": _schema("src:s dst:s citation:s"),
    # Matter -> Subdivision, where the citation is that precise
    "cites_subdivision": _schema("src:s dst:s citation:s"),
    # Matter -> Session law ("Laws of Minnesota 2023, chapter 64, article 8, section 3")
    "cites_law": _schema("src:s dst:s citation:s"),
    # Matter -> County ordinance, by number
    "cites_ordinance": _schema("src:s dst:s citation:s"),
    # Statute section -> Statute chapter
    "part_of": _schema("src:s dst:s"),
    # Person -> Matter
    "moved": _schema("src:s dst:s date:d meeting:s event_item_id:i"),
    "seconded": _schema("src:s dst:s date:d meeting:s event_item_id:i"),
    "voted": _schema("src:s dst:s date:d meeting:s event_item_id:i value:s via_consent_agenda:b"),
    # Person -> Meeting
    "attended": _schema("src:s dst:s date:d value:s"),
}

TABLE_SCHEMAS = {
    # every agenda line of every meeting, including procedural ones with no matter
    "agenda_item": _schema(
        "event_item_id:i meeting:s matter:s agenda_sequence:i minutes_sequence:i agenda_number:s title:s "
        "section:s action:s result:s consent:b mover:s seconder:s action_text:s minutes_note:s agenda_note:s"
    ),
    "matter_text": _schema("matter:s file_number:s section:s text:s text_version:s"),
    "attachment": _schema("matter:s attachment_id:i name:s url:s file_type:s"),
}

GRAPH_VERTICES = _schema("id:s type:s label:s")
GRAPH_EDGES = _schema("src:s dst:s relationship:s date:d")

# which column of each edge table dates the relationship in graph/edges
_EDGE_DATE = {"member_of": "start_date"}


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _blank_to_none(value: str | None) -> str | None:
    value = tx.clean(value)
    return value or None


def _agenda_order(item: dict[str, Any]) -> tuple[int, int]:
    """Printed agenda order; lines that exist only in the minutes sort after it."""
    agenda, minutes = item.get("EventItemAgendaSequence"), item.get("EventItemMinutesSequence")
    return (agenda if agenda is not None else 10**6 + (minutes or 0), item["EventItemId"])


class _Builder:
    def __init__(self, store: BronzeStore) -> None:
        self.client = store.client
        self.store = store
        self.vertices: dict[str, list[dict[str, Any]]] = {name: [] for name in VERTEX_SCHEMAS}
        self.edges: dict[str, list[dict[str, Any]]] = {name: [] for name in EDGE_SCHEMAS}
        self.tables: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLE_SCHEMAS}
        self.person_names: dict[int, str] = {}
        self.skipped_events = 0

    def vid(self, kind: str, key: Any) -> str:
        return f"{self.client}:{kind}:{key}"

    def person(self, person_id: int | None, name: str | None) -> str | None:
        """Vertex id for a person, registering them as someone who took part."""
        if not person_id:
            return None
        self.person_names.setdefault(person_id, tx.clean(name) or f"Person {person_id}")
        return self.vid("person", person_id)

    def reference(self, name: str) -> list[dict[str, Any]]:
        doc = self.store.read("reference", name)
        return doc["rows"] if doc else []

    # ---- build steps -----------------------------------------------------------

    def build(self) -> None:
        self.bodies = {b["BodyId"]: tx.clean(b["BodyName"]) for b in self.reference("bodies")}
        self.status_names = {s["MatterStatusId"]: s["MatterStatusName"] for s in self.reference("matter_statuses")}
        self._bodies_and_members()
        matters = list(self.store.iter_docs("matters"))
        self._matters(matters)
        events = []
        for event in self.store.iter_docs("events"):
            # "Hidden" agendas are the clerk's test meetings; they are not on the public calendar.
            if event.get("EventAgendaStatusName") == "Hidden":
                self.skipped_events += 1
            else:
                events.append(event)
        status_after = {
            h["MatterHistoryId"]: self.status_names.get(h["MatterHistoryMatterStatusId"])
            for m in matters
            for h in m["MatterHistories"]
        }
        self.open_meetings = {
            self.vid("meeting", e["EventId"]) for e in events if e.get("EventMinutesStatusName") != "Final"
        }
        self._meetings_and_agendas(events, status_after)
        self._referrals()
        self._votes({e["EventId"]: e for e in events})
        self._persons()

    def _bodies_and_members(self) -> None:
        for body in self.reference("bodies"):
            self.vertices["body"].append(
                {
                    "id": self.vid("body", body["BodyId"]),
                    "body_id": body["BodyId"],
                    "name": tx.clean(body["BodyName"]),
                    "body_type": body.get("BodyTypeName"),
                    "is_active": bool(body.get("BodyActiveFlag")),
                }
            )
        for record in self.reference("office_records"):
            # office records also exist for bodies the API does not list ("Do not use", clerk roles)
            if record["OfficeRecordBodyId"] not in self.bodies:
                continue
            self.edges["member_of"].append(
                {
                    "src": self.person(record["OfficeRecordPersonId"], record["OfficeRecordFullName"]),
                    "dst": self.vid("body", record["OfficeRecordBodyId"]),
                    "title": _blank_to_none(record.get("OfficeRecordTitle")),
                    "member_type": _blank_to_none(record.get("OfficeRecordMemberType")),
                    "start_date": _date(record.get("OfficeRecordStartDate")),
                    "end_date": _date(record.get("OfficeRecordEndDate")),
                }
            )

    def _matters(self, matters: list[dict[str, Any]]) -> None:
        by_file = {tx.normalize_file_number(m["MatterFile"]): m["MatterId"] for m in matters if m["MatterFile"]}
        file_of = {m["MatterId"]: tx.normalize_file_number(m["MatterFile"]) for m in matters}
        departments: dict[str, str] = {}
        statutes: set[str] = set()
        subdivisions: set[tuple[str, str]] = set()
        session_laws: set[tuple[int, int, str]] = set()
        ordinances: set[str] = set()

        for matter in matters:
            matter_vid = self.vid("matter", matter["MatterId"])
            file_number = _blank_to_none(matter["MatterFile"])
            latest = max(matter["MatterTexts"], key=lambda t: t["MatterTextId"], default=None)
            sections = tx.split_sections(latest["MatterTextPlain"]) if latest else {}
            department = _blank_to_none(matter.get("MatterRequester"))

            self.vertices["matter"].append(
                {
                    "id": matter_vid,
                    "matter_id": matter["MatterId"],
                    "file_number": file_number,
                    "title": _blank_to_none(matter.get("MatterTitle")) or sections.get("title"),
                    "matter_type": matter.get("MatterTypeName"),
                    "status": matter.get("MatterStatusName"),
                    "body_name": _blank_to_none(matter.get("MatterBodyName")),
                    "intro_date": _date(matter.get("MatterIntroDate")),
                    "agenda_date": _date(matter.get("MatterAgendaDate")),
                    "passed_date": _date(matter.get("MatterPassedDate")),
                    "department": department,
                    "administrator_recommendation": tx.administrator_recommendation(sections.get("recommendation")),
                    "last_modified_utc": matter.get("MatterLastModifiedUtc"),
                    "site_url": (
                        f"https://{self.client}.legistar.com/gateway.aspx?m=l&id=/matter.aspx?key={matter['MatterId']}"
                    ),
                }
            )
            for section, body in sections.items():
                self.tables["matter_text"].append(
                    {
                        "matter": matter_vid,
                        "file_number": file_number,
                        "section": section,
                        "text": body,
                        "text_version": latest.get("MatterTextVersion"),
                    }
                )
            for attachment in matter["MatterAttachments"]:
                file_name = attachment.get("MatterAttachmentFileName") or ""
                self.tables["attachment"].append(
                    {
                        "matter": matter_vid,
                        "attachment_id": attachment["MatterAttachmentId"],
                        "name": _blank_to_none(attachment.get("MatterAttachmentName")),
                        "url": attachment.get("MatterAttachmentHyperlink"),
                        "file_type": Path(file_name).suffix.lower().lstrip(".") or None,
                    }
                )

            if department:
                departments.setdefault(self.vid("department", tx.slugify(department)), department)
                self.edges["requested_by"].append(
                    {"src": matter_vid, "dst": self.vid("department", tx.slugify(department))}
                )

            body_text = "\n".join(sections.values())
            for number, citation in tx.extract_statute_citations(body_text):
                statutes.add(number)
                self.edges["cites"].append({"src": matter_vid, "dst": f"mnstat:{number}", "citation": citation})
            for section, subdivision, citation in tx.extract_subdivision_citations(body_text):
                subdivisions.add((section, subdivision))
                self.edges["cites_subdivision"].append(
                    {"src": matter_vid, "dst": revisor.statute_id(section, subdivision), "citation": citation}
                )
            for year, special, chapter, citation in tx.extract_session_law_citations(body_text):
                session_laws.add((year, special, chapter))
                self.edges["cites_law"].append(
                    {"src": matter_vid, "dst": revisor.session_law_id(year, chapter, special), "citation": citation}
                )
            for number, citation in tx.extract_ordinance_citations(body_text):
                ordinances.add(number)
                self.edges["cites_ordinance"].append(
                    {"src": matter_vid, "dst": self.vid("ordinance", number), "citation": citation}
                )

            own = tx.normalize_file_number(file_number)
            for mentioned in tx.extract_file_numbers(body_text):
                target = by_file.get(mentioned)
                if target and mentioned != own:
                    self.edges["references"].append(
                        {"src": matter_vid, "dst": self.vid("matter", target), "file_number": mentioned}
                    )

            # Legistar stores each link twice, once from each side. Flag 2 is the row on
            # the earlier matter pointing at the later one.
            for relation in matter["MatterRelations"]:
                other = relation["MatterRelationMatterId"]
                if relation.get("MatterRelationFlag") != 2 or other not in file_of:
                    continue
                same_base = bool(own) and tx.base_file_number(file_of[other]) == tx.base_file_number(own)
                self.edges["related_to"].append(
                    {
                        "src": matter_vid,
                        "dst": self.vid("matter", other),
                        "kind": "revision" if same_base else "related",
                    }
                )

        self.vertices["department"] = [{"id": vid, "name": name} for vid, name in sorted(departments.items())]
        # every cited section also gets its chapter, so citations to 383B.158 and 383B.77 meet at 383B
        for number in sorted(set(statutes) | {tx.statute_chapter(n) for n in statutes}):
            chapter = tx.statute_chapter(number)
            is_section = chapter != number
            self.vertices["statute"].append(
                {
                    "id": f"mnstat:{number}",
                    "number": number,
                    "kind": "section" if is_section else "chapter",
                    "chapter": chapter,
                    "url": revisor.statute_url(number),
                }
            )
            if is_section:
                self.edges["part_of"].append({"src": f"mnstat:{number}", "dst": f"mnstat:{chapter}"})
        for section, subdivision in sorted(subdivisions):
            self.vertices["subdivision"].append(
                {
                    "id": revisor.statute_id(section, subdivision),
                    "section": section,
                    "subdivision": subdivision,
                    "chapter": tx.statute_chapter(section),
                    "url": revisor.statute_url(section, subdivision),
                }
            )
            self.edges["part_of"].append({"src": revisor.statute_id(section, subdivision), "dst": f"mnstat:{section}"})
        for year, special, chapter in sorted(session_laws):
            self.vertices["session_law"].append(
                {
                    "id": revisor.session_law_id(year, chapter, special),
                    "year": year,
                    "special": special,
                    "chapter": chapter,
                    "url": revisor.session_law_url(year, chapter, special),
                }
            )
        self.vertices["ordinance"] = [
            {"id": self.vid("ordinance", number), "number": number} for number in sorted(ordinances, key=int)
        ]

    def _meetings_and_agendas(self, events: list[dict[str, Any]], status_after: dict[int, str | None]) -> None:
        for event in events:
            meeting_vid = self.vid("meeting", event["EventId"])
            body_vid = self.vid("body", event["EventBodyId"])
            body_name = tx.clean(event["EventBodyName"])
            meeting_date = _date(event["EventDate"])
            self.vertices["meeting"].append(
                {
                    "id": meeting_vid,
                    "event_id": event["EventId"],
                    "body": body_vid,
                    "body_name": body_name,
                    "date": meeting_date,
                    "time": _blank_to_none(event.get("EventTime")),
                    "location": _blank_to_none(event.get("EventLocation")),
                    "agenda_status": event.get("EventAgendaStatusName"),
                    "minutes_status": event.get("EventMinutesStatusName"),
                    "agenda_url": event.get("EventAgendaFile"),
                    "minutes_url": event.get("EventMinutesFile"),
                    "site_url": event.get("EventInSiteURL"),
                    "comment": _blank_to_none(event.get("EventComment")),
                }
            )
            self.edges["held_by"].append({"src": meeting_vid, "dst": body_vid, "date": meeting_date})

            section: str | None = None
            for item in sorted(event["EventItems"], key=_agenda_order):
                title = _blank_to_none(item.get("EventItemTitle"))
                matter_id = item.get("EventItemMatterId")
                matter_vid = self.vid("matter", matter_id) if matter_id else None
                if not matter_id and title and item.get("EventItemAgendaSequence") is not None:
                    section = title  # a heading line: "Consent", "Referred to Public Works Committee", ...
                mover = self.person(item.get("EventItemMoverId"), item.get("EventItemMover"))
                seconder = self.person(item.get("EventItemSeconderId"), item.get("EventItemSeconder"))
                row = {
                    "event_item_id": item["EventItemId"],
                    "meeting": meeting_vid,
                    "matter": matter_vid,
                    "agenda_sequence": item.get("EventItemAgendaSequence"),
                    "minutes_sequence": item.get("EventItemMinutesSequence"),
                    "agenda_number": _blank_to_none(item.get("EventItemAgendaNumber")),
                    "title": title,
                    "section": section if matter_id else None,
                    "action": item.get("EventItemActionName"),
                    "result": item.get("EventItemPassedFlagName"),
                    "consent": bool(item.get("EventItemConsent")),
                    "mover": mover,
                    "seconder": seconder,
                    "action_text": _blank_to_none(item.get("EventItemActionText")),
                    "minutes_note": tx.rtf_to_text(item.get("EventItemMinutesNote")) or None,
                    "agenda_note": tx.rtf_to_text(item.get("EventItemAgendaNote")) or None,
                }
                self.tables["agenda_item"].append(row)
                if not matter_vid:
                    continue
                self.edges["on_agenda"].append(
                    {
                        "src": matter_vid,
                        "dst": meeting_vid,
                        "event_item_id": row["event_item_id"],
                        "date": meeting_date,
                        "body": body_vid,
                        "body_name": body_name,
                        "agenda_number": row["agenda_number"],
                        "section": row["section"],
                        "action": row["action"],
                        "result": row["result"],
                        "consent": row["consent"],
                        "status_after": status_after.get(item["EventItemId"]),
                        "mover": mover,
                        "seconder": seconder,
                        "action_text": row["action_text"],
                    }
                )
                for role, person_vid in (("moved", mover), ("seconded", seconder)):
                    if person_vid:
                        self.edges[role].append(
                            {
                                "src": person_vid,
                                "dst": matter_vid,
                                "date": meeting_date,
                                "meeting": meeting_vid,
                                "event_item_id": row["event_item_id"],
                            }
                        )

    def _body_named_in(self, value: str | None) -> str | None:
        """Vertex id of the body whose full name appears in ``value`` (longest name wins)."""
        haystack = (value or "").lower()
        for body_id, name in sorted(self.bodies.items(), key=lambda kv: -len(kv[1])):
            if name.lower() in haystack:
                return self.vid("body", body_id)
        return None

    def _referrals(self) -> None:
        """Where did each referral send the matter? In order of trust: the body named
        in the action text, the agenda heading the item sat under ("Referred to Public
        Works Committee"), then the next body that actually took the matter up."""
        appearances: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.edges["on_agenda"]:
            appearances[edge["src"]].append(edge)
        for matter_vid, rows in appearances.items():
            rows.sort(key=lambda r: (r["date"] or date.min, r["event_item_id"]))
            for n, row in enumerate(rows):
                heading = row["section"] or ""
                under_referral_heading = heading.lower().startswith("referred to")
                # Before the minutes are final there is no action yet, but the agenda
                # already shows where the board intends to send the item.
                planned = row["action"] is None and under_referral_heading and row["dst"] in self.open_meetings
                if not planned and (row["action"] not in REFERRAL_ACTIONS or row["result"] == "Fail"):
                    continue
                target, basis = self._body_named_in(row["action_text"]), "action_text"
                if not target or target == row["body"]:
                    target = self._body_named_in(heading) if under_referral_heading else None
                    basis = "agenda_section"
                if not target or target == row["body"]:
                    target = next((later["body"] for later in rows[n + 1 :] if later["body"] != row["body"]), None)
                    basis = "next_action"
                if target:
                    self.edges["referred_to"].append(
                        {
                            "src": matter_vid,
                            "dst": target,
                            "date": row["date"],
                            "from_meeting": row["dst"],
                            "event_item_id": row["event_item_id"],
                            "action": row["action"],
                            "basis": basis,
                        }
                    )

    def _votes(self, events: dict[int, dict[str, Any]]) -> None:
        for doc in self.store.iter_docs("votes"):
            event = events.get(doc["EventId"])
            if not event:
                continue
            meeting_vid = self.vid("meeting", event["EventId"])
            meeting_date = _date(event["EventDate"])
            matter_of = {i["EventItemId"]: i.get("EventItemMatterId") for i in event["EventItems"]}
            for item in doc["Items"]:
                for call in item["RollCalls"]:
                    person_vid = self.person(call.get("RollCallPersonId"), call.get("RollCallPersonName"))
                    if person_vid:
                        self.edges["attended"].append(
                            {
                                "src": person_vid,
                                "dst": meeting_vid,
                                "date": meeting_date,
                                "value": call.get("RollCallValueName"),
                            }
                        )
                matter_id = matter_of.get(item["EventItemId"])
                if not matter_id:
                    continue  # votes on procedural items (approving the agenda) stay in bronze
                for vote in item["Votes"]:
                    person_vid = self.person(vote.get("VotePersonId"), vote.get("VotePersonName"))
                    if person_vid:
                        self.edges["voted"].append(
                            {
                                "src": person_vid,
                                "dst": self.vid("matter", matter_id),
                                "date": meeting_date,
                                "meeting": meeting_vid,
                                "event_item_id": item["EventItemId"],
                                "value": vote.get("VoteValueName"),
                                # consent items share the one vote taken on the consent agenda
                                "via_consent_agenda": vote.get("VoteEventItemId") != item["EventItemId"],
                            }
                        )

    def _persons(self) -> None:
        """Only people who hold office or took part in a meeting become vertices. The
        Legistar person list is mostly staff user accounts, which stay in bronze."""
        active = {p["PersonId"]: bool(p.get("PersonActiveFlag")) for p in self.reference("persons")}
        names = {p["PersonId"]: tx.clean(p.get("PersonFullName")) for p in self.reference("persons")}
        for person_id, seen_name in sorted(self.person_names.items()):
            self.vertices["person"].append(
                {
                    "id": self.vid("person", person_id),
                    "person_id": person_id,
                    "name": names.get(person_id) or seen_name,
                    "is_active": active.get(person_id),
                }
            )


def _label(kind: str, row: dict[str, Any]) -> str:
    if kind == "meeting":
        return f"{row['body_name']} {row['date']}"
    if kind == "matter":
        return f"{row['file_number'] or row['matter_id']}: {row['title'] or ''}".strip()
    if kind in ("statute", "subdivision", "session_law"):
        return revisor.label(kind, row)
    if kind == "ordinance":
        return f"Ordinance {row['number']}"
    return row["name"]


def _drop_dangling(builder: _Builder) -> dict[str, int]:
    """Edges must join to vertices at both ends; anything else is dropped and counted."""
    known = {row["id"] for rows in builder.vertices.values() for row in rows}
    dropped = {}
    for name, rows in builder.edges.items():
        kept = [r for r in rows if r["src"] in known and r["dst"] in known]
        if len(kept) != len(rows):
            dropped[name] = len(rows) - len(kept)
            builder.edges[name] = kept
    return dropped


def _write(path: Path, rows: Iterable[dict[str, Any]], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(list(rows), schema=schema), path)


def silver_root(data_dir: Path, client: str) -> Path:
    return Path(data_dir) / "silver" / "legistar" / client


def build(data_dir: Path, client: str) -> dict[str, Any]:
    """Rebuild the silver layer from bronze. Returns row counts per table."""
    builder = _Builder(BronzeStore(data_dir, client))
    builder.build()
    dropped = _drop_dangling(builder)
    root = silver_root(data_dir, client)

    report: dict[str, Any] = {"root": str(root), "vertices": {}, "edges": {}, "tables": {}}
    for group, schemas, data in (
        ("vertices", VERTEX_SCHEMAS, builder.vertices),
        ("edges", EDGE_SCHEMAS, builder.edges),
        ("tables", TABLE_SCHEMAS, builder.tables),
    ):
        for name, schema in schemas.items():
            _write(root / group / f"{name}.parquet", data[name], schema)
            report[group][name] = len(data[name])

    _write(
        root / "graph" / "vertices.parquet",
        ({"id": r["id"], "type": kind, "label": _label(kind, r)} for kind, rows in builder.vertices.items() for r in rows),
        GRAPH_VERTICES,
    )
    _write(
        root / "graph" / "edges.parquet",
        (
            {"src": r["src"], "dst": r["dst"], "relationship": name, "date": r.get(_EDGE_DATE.get(name, "date"))}
            for name, rows in builder.edges.items()
            for r in rows
        ),
        GRAPH_EDGES,
    )
    report["dangling_edges_dropped"] = dropped
    report["hidden_test_meetings_skipped"] = builder.skipped_events
    if dropped:
        log.warning("dropped edges with a missing endpoint: %s", dropped)
    return report
