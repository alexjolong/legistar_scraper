"""Silver layer for state law: statutes, session laws and bills as graph tables.

Built from the pages in ``bronze/mnrevisor`` and the bill data in
``bronze/openstates``, into ``<data_dir>/silver/mnlaw/`` with the same layout as the
county graph (``vertices/``, ``edges/``, ``tables/``, ``graph/``). Nothing here
depends on a county: the ids (``mnstat:383B.158``, ``mnstat:383B.158#4``,
``mnlaw:2026:88``, ``mnbill:2025-2026:HF4582``) are the ones a county graph uses
when its matters cite the law, which is how the two join.

==================  ===================================  ================================
vertex              id                                   from
==================  ===================================  ================================
``statute``         ``mnstat:383B`` / ``mnstat:383B.158``  chapter pages
``subdivision``     ``mnstat:383B.158#4``                chapter pages
``session_law``     ``mnlaw:2026:88``                    law lists, Table 2, History lines
``bill``            ``mnbill:2025-2026:HF4582``          Open States
==================  ===================================  ================================

Edges: ``part_of`` (subdivision -> section -> chapter), ``changed_by`` (statute or
subdivision -> session law, from Table 2 and from History lines), ``cross_references``
(statute or subdivision -> statute), ``enacted_as`` (bill -> session law),
``companion`` (bill -> bill).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from . import revisor
from .silver import GRAPH_EDGES, GRAPH_VERTICES, _schema, _write

log = logging.getLogger(__name__)

VERTEX_SCHEMAS = {
    # the first five columns are what a county graph writes for a statute it only knows by citation
    "statute": _schema(
        "id:s number:s kind:s chapter:s url:s headnote:s heading:s status:s note:s text:s history:s edition:i "
        "subdivisions:i"
    ),
    "subdivision": _schema("id:s section:s subdivision:s chapter:s url:s headnote:s status:s text:s"),
    "session_law": _schema("id:s year:i special:i chapter:s url:s bill:s presented:d vetoed:s title:s summary:s"),
    "bill": _schema(
        "id:s session:s identifier:s title:s chamber:s classification:s subjects:s sponsors:s first_action:d "
        "last_action:d last_action_text:s passed:d url:s source_url:s"
    ),
}
EDGE_SCHEMAS = {
    "part_of": _schema("src:s dst:s"),
    # action is null when the change is known only from a History line
    "changed_by": _schema(
        "src:s dst:s date:d year:i action:s statute:s subdivision:s article:s section:s law_anchor:s basis:s"
    ),
    "cross_references": _schema("src:s dst:s"),
    "enacted_as": _schema("src:s dst:s date:d"),
    "companion": _schema("src:s dst:s"),
}
TABLE_SCHEMAS = {
    # every Table 2 row, including statutes outside the chapters that were fetched
    "statute_change": _schema(
        "statute:s subdivision:s chapter:s action:s year:i special:i session_law:s article:s section:s law_anchor:s"
    ),
    # the sections of session laws that changed a fetched statute, with struck and inserted text marked
    "law_section": _schema("session_law:s anchor:s label:s kind:s heading:s marked:s effective:s"),
    "bill_action": _schema("bill:s date:d chamber:s description:s sequence:i"),
}


def silver_root(data_dir: Path) -> Path:
    return Path(data_dir) / "silver" / "mnlaw"


def chapter_sort_key(number: str) -> tuple[int, str, float]:
    """Statute order: 13 < 13D < 103B < 383B, and 383B.158 < 383B.1581 < 383B.16."""
    chapter, _, section = number.partition(".")
    match = re.match(r"(\d+)([A-Z]?)", chapter)
    digits = re.match(r"\d+", section)
    # sections sort as decimals: .158 < .1581 < .16
    return (int(match.group(1)) if match else 0, match.group(2) if match else "", float(f"0.{digits.group(0)}") if digits else 0.0)


def _label(kind: str, row: dict[str, Any]) -> str:
    if kind == "bill":
        return f"{row['identifier']} ({row['session']})"
    return revisor.label(kind, row)


class _Builder:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.pages = revisor.store(data_dir)
        self.vertices: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in VERTEX_SCHEMAS}
        self.edges: dict[str, list[dict[str, Any]]] = {name: [] for name in EDGE_SCHEMAS}
        self.tables: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLE_SCHEMAS}

    def law(self, year: int, special: int, chapter: str, **facts: Any) -> str:
        """Vertex id of a session law, creating it (or adding facts to it) as needed."""
        law_id = revisor.session_law_id(year, chapter, special)
        row = self.vertices["session_law"].setdefault(
            law_id,
            {"id": law_id, "year": year, "special": special, "chapter": str(chapter), "url": revisor.session_law_url(year, chapter, special)},
        )
        row.update({k: v for k, v in facts.items() if v is not None})
        return law_id

    # ---- statutes --------------------------------------------------------------------

    def statutes(self) -> None:
        for key in self.pages.keys("statutes/current"):
            chapter = revisor.parse_chapter(self.pages.read(key))
            if not chapter.number:
                log.warning("statutes: could not read %s", key)
                continue
            chapter_id = revisor.statute_id(chapter.number)
            self.vertices["statute"][chapter_id] = {
                "id": chapter_id,
                "number": chapter.number,
                "kind": "chapter",
                "chapter": chapter.number,
                "url": revisor.statute_url(chapter.number),
                "headnote": chapter.title,
                "status": "in force" if any(s.status == "in force" for s in chapter.sections) else "repealed",
                "edition": chapter.edition,
            }
            for section in chapter.sections:
                section_id = revisor.statute_id(section.number)
                self.vertices["statute"][section_id] = {
                    "id": section_id,
                    "number": section.number,
                    "kind": "section",
                    "chapter": chapter.number,
                    "url": revisor.statute_url(section.number),
                    "headnote": section.headnote,
                    "heading": section.group,
                    "status": section.status,
                    "note": section.note,
                    "text": section.text or None,
                    "history": section.history,
                    "edition": chapter.edition,
                    "subdivisions": len(section.subdivisions),
                }
                self.edges["part_of"].append({"src": section_id, "dst": chapter_id})
                for year, special, law_chapter in section.history_laws:
                    self.edges["changed_by"].append(
                        {
                            "src": section_id,
                            "dst": self.law(year, special, law_chapter),
                            "year": year,
                            "statute": section.number,
                            "basis": "history",
                        }
                    )
                for target in section.references if not section.subdivisions else ():
                    self.edges["cross_references"].append({"src": section_id, "dst": revisor.statute_id(target)})
                for subdivision in section.subdivisions:
                    subdivision_id = revisor.statute_id(section.number, subdivision.number)
                    self.vertices["subdivision"][subdivision_id] = {
                        "id": subdivision_id,
                        "section": section.number,
                        "subdivision": subdivision.number,
                        "chapter": chapter.number,
                        "url": revisor.statute_url(section.number, subdivision.number),
                        "headnote": subdivision.headnote,
                        "status": subdivision.status,
                        "text": subdivision.text or None,
                    }
                    self.edges["part_of"].append({"src": subdivision_id, "dst": section_id})
                    for target in subdivision.references:
                        self.edges["cross_references"].append({"src": subdivision_id, "dst": revisor.statute_id(target)})

    # ---- session laws and the changes they made ----------------------------------

    def session_laws(self) -> None:
        for key in self.pages.keys("laws"):
            if not key.endswith("/index.html"):
                continue
            for law in revisor.parse_session_laws(self.pages.read(key)):
                self.law(law.year, law.special, law.chapter, bill=law.bill, presented=law.presented, vetoed=law.vetoed)

    def changes(self) -> None:
        seen: set[tuple] = set()
        wanted: dict[str, set[str]] = {}  # session law -> anchors of sections that changed a fetched statute
        for key in self.pages.keys("table2"):
            for change in revisor.parse_table2(self.pages.read(key))[1]:
                identity = (change.statute, change.subdivision, change.action, change.year, change.special, change.law_chapter, change.article, change.section)
                if identity in seen:
                    continue
                seen.add(identity)
                law_id = self.law(change.year, change.special, change.law_chapter)
                self.tables["statute_change"].append(
                    {
                        "statute": change.statute,
                        "subdivision": change.subdivision,
                        "chapter": change.statute.split(".")[0],
                        "action": change.action,
                        "year": change.year,
                        "special": change.special,
                        "session_law": law_id,
                        "article": change.article,
                        "section": change.section,
                        "law_anchor": change.law_anchor,
                    }
                )
                section_id = revisor.statute_id(change.statute)
                subdivision_id = revisor.statute_id(change.statute, change.subdivision) if change.subdivision else None
                source = subdivision_id if subdivision_id in self.vertices["subdivision"] else section_id
                if source not in self.vertices["statute"] and source not in self.vertices["subdivision"]:
                    continue  # a statute in a chapter that has not been fetched: kept in the table only
                self.edges["changed_by"].append(
                    {
                        "src": source,
                        "dst": law_id,
                        "date": self.vertices["session_law"][law_id].get("presented"),
                        "year": change.year,
                        "action": change.action,
                        "statute": change.statute,
                        "subdivision": change.subdivision,
                        "article": change.article,
                        "section": change.section,
                        "law_anchor": change.law_anchor,
                        "basis": "table2",
                    }
                )
                if change.law_anchor:
                    wanted.setdefault(law_id, set()).add(change.law_anchor)
        # Table 2 is the better record where both exist: drop History-line edges it repeats.
        from_table = {(e["src"].split("#")[0], e["dst"]) for e in self.edges["changed_by"] if e["basis"] == "table2"}
        self.edges["changed_by"] = [
            e for e in self.edges["changed_by"] if e["basis"] == "table2" or (e["src"], e["dst"]) not in from_table
        ]
        for edge in self.edges["changed_by"]:
            if edge.get("date") is None:
                edge["date"] = self.vertices["session_law"][edge["dst"]].get("presented")
        self._law_sections(wanted)

    def _law_sections(self, wanted: dict[str, set[str]]) -> None:
        for law_id, anchors in sorted(wanted.items()):
            row = self.vertices["session_law"][law_id]
            content = self.pages.read(revisor.law_key(row["year"], row["chapter"], row["special"]))
            if content is None:
                continue
            law = revisor.parse_law(content)
            row["title"], row["summary"] = law.title, law.summary
            for anchor in sorted(anchors):
                section = law.sections.get(anchor)
                if section:
                    self.tables["law_section"].append(
                        {
                            "session_law": law_id,
                            "anchor": anchor,
                            "label": section.label,
                            "kind": section.kind,
                            "heading": section.heading,
                            "marked": section.marked,
                            "effective": section.effective,
                        }
                    )

    def laws_to_fetch(self) -> list[tuple[int, int, str]]:
        """Session laws from Table 2 that changed a fetched statute: their text is worth having."""
        laws = {
            (row["year"], row["special"], row["chapter"])
            for edge in self.edges["changed_by"]
            if edge["basis"] == "table2"
            for row in [self.vertices["session_law"][edge["dst"]]]
        }
        return sorted(laws)

    # ---- bills -------------------------------------------------------------------------

    def bills(self) -> None:
        from . import openstates

        by_number: dict[tuple[str, str], str] = {}
        bills = openstates.read_bills(self.data_dir)
        for bill in bills:
            self.vertices["bill"][bill["id"]] = {k: bill.get(k) for k in VERTEX_SCHEMAS["bill"].names}
            by_number[(bill["session"], bill["identifier"].replace(" ", ""))] = bill["id"]
            for sequence, action in enumerate(bill["actions"]):
                self.tables["bill_action"].append({"bill": bill["id"], "sequence": sequence, **action})
        for bill in bills:
            for other in bill["companions"]:
                target = by_number.get((other["session"], other["identifier"].replace(" ", "")))
                if target:
                    self.edges["companion"].append({"src": bill["id"], "dst": target})
        for law in self.vertices["session_law"].values():
            if not law.get("bill"):
                continue
            for session in openstates.sessions_for(law["year"], law["special"]):
                bill_id = by_number.get((session, law["bill"]))
                if bill_id:
                    self.edges["enacted_as"].append({"src": bill_id, "dst": law["id"], "date": law.get("presented")})
                    break

    def build(self, *, bills: bool = True) -> None:
        self.statutes()
        self.session_laws()
        self.changes()
        if bills:
            self.bills()


def builder(data_dir: Path, *, bills: bool = True) -> _Builder:
    made = _Builder(data_dir)
    made.build(bills=bills)
    return made


def laws_to_fetch(data_dir: Path) -> list[tuple[int, int, str]]:
    return builder(data_dir, bills=False).laws_to_fetch()


def fetched_chapters(data_dir: Path) -> list[str]:
    """Chapters already in bronze. Once fetched, a chapter stays on the watch list."""
    return [Path(key).stem for key in revisor.store(data_dir).keys("statutes/current")]


def build(data_dir: Path) -> dict[str, Any]:
    """Rebuild ``silver/mnlaw`` from bronze. Returns row counts per table."""
    made = builder(data_dir)
    known = {vertex_id for rows in made.vertices.values() for vertex_id in rows}
    dropped: dict[str, int] = {}
    for name, rows in made.edges.items():
        unique = list({tuple(sorted((k, str(v)) for k, v in row.items())): row for row in rows}.values())
        kept = [row for row in unique if row["src"] in known and row["dst"] in known]
        if len(kept) != len(unique):
            dropped[name] = len(unique) - len(kept)
        made.edges[name] = kept
    root = silver_root(data_dir)
    report: dict[str, Any] = {"root": str(root), "vertices": {}, "edges": {}, "tables": {}}
    ordered = {
        "statute": lambda r: chapter_sort_key(r["number"]),
        "subdivision": lambda r: (chapter_sort_key(r["section"]), r["subdivision"]),
        "session_law": lambda r: (r["year"], r["special"], int(r["chapter"]) if r["chapter"].isdigit() else 0),
        "bill": lambda r: (r["session"], r["identifier"]),
    }
    vertices = {name: sorted(rows.values(), key=ordered[name]) for name, rows in made.vertices.items()}
    for group, schemas, data in (
        ("vertices", VERTEX_SCHEMAS, vertices),
        ("edges", EDGE_SCHEMAS, made.edges),
        ("tables", TABLE_SCHEMAS, made.tables),
    ):
        for name, schema in schemas.items():
            _write(root / group / f"{name}.parquet", data[name], schema)
            report[group][name] = len(data[name])
    _write(
        root / "graph" / "vertices.parquet",
        ({"id": r["id"], "type": kind, "label": _label(kind, r)} for kind, rows in vertices.items() for r in rows),
        GRAPH_VERTICES,
    )
    _write(
        root / "graph" / "edges.parquet",
        (
            {"src": r["src"], "dst": r["dst"], "relationship": name, "date": r.get("date")}
            for name, rows in made.edges.items()
            for r in rows
        ),
        GRAPH_EDGES,
    )
    # cross-references to chapters that were not fetched are expected; say how many
    report["edges_to_statutes_not_fetched"] = dropped
    return report


def status(data_dir: Path) -> dict[str, Any]:
    """What is on disk in bronze for the law sources."""
    pages = revisor.store(data_dir)
    chapters = [Path(key).stem for key in pages.keys("statutes/current")]
    return {
        "root": str(pages.root),
        "chapters": len(chapters),
        "editions": sorted({key.split("/")[1] for key in pages.keys("statutes")} - {"current"}),
        "table2_years": sorted({key.split("/")[1] for key in pages.keys("table2")}),
        "session_law_lists": len([key for key in pages.keys("laws") if key.endswith("/index.html")]),
        "session_law_texts": len([key for key in pages.keys("laws") if "/chapter/" in key]),
        "as_of": date.today().isoformat(),
    }
