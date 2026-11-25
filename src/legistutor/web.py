"""A small local web page over the search index and the graph.

``legistutor serve`` starts it on 127.0.0.1 only. The page (``static/index.html``)
is a single file with no external assets; this module is its JSON API:

* ``/api/meta``     counts, filter choices, model names
* ``/api/search``   hybrid search, results filled in from the graph
* ``/api/ask``      the same search, then a cited answer streamed as server-sent events
* ``/api/matter``   one matter: its path through the bodies, its neighbours in the graph, its text
* ``/api/node``     a department, body, statute, session law, ordinance or bill, and what is connected to it
* ``/api/queues``   what is coming to each body, and recent and upcoming meetings
* ``/api/topic``    matters containing every word of a query, by quarter and department
* ``/api/explore``  every theme the chat model filed matters under, by month; ``/api/theme`` one of them,
  with its storylines; ``/api/storyline`` one storyline; ``/api/meeting`` one meeting's items by theme
* ``/api/changes``  statutes the record relies on that a session changed, and citations that need a look
* ``/api/law_text`` one section of a session law, with struck and inserted text marked
* ``/api/law_ask``  a summary of one statute section, chapter or ordinance, or an answer to a
  question about it, written by the chat model from that text alone and streamed like ``/api/ask``
* ``/api/ordinances``, ``/api/bills``   the county's ordinances; state bills by words in their title

The standard library's HTTP server is enough for one person on one machine, and
keeps the install small. :class:`App` holds all the logic and knows nothing about
HTTP, so it can sit behind another framework later.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import config, gold, lawnotes, llm, rag
from . import themes as theme_filing
from .embed import EmbeddingError
from .embed import embedder as make_embedder
from .law import chapter_sort_key
from .search import Filters, Hit, Searcher
from .text import normalize_file_number

log = logging.getLogger(__name__)

GROUP_LIMIT = 8  # neighbours drawn per group around a matter
NODE_MATTERS = 40  # matters listed for a department, statute or body
STALE_DAYS = 180  # sent to a body longer ago than this and never heard: a leftover, not a backlog
TOPIC_DEPARTMENTS = 5  # departments given their own colour in the topic chart; the rest fold into "Other"
LAW_SOURCES = 3  # statute sections or ordinances added to a question about the law
# The law's results are shown first when its best passage is closer to the question than
# the record's best by more than this. On fifteen trial questions the gap was -0.27 to -0.04
# for questions about what the board did and +0.09 to +0.23 for questions about what the law
# says, so the margin sits between; it should be set again from the evaluation questions.
LAW_LEAD_MARGIN = 0.03
CHANGE_ACTIONS = ("Amended", "New", "Repealed")  # Table 2 actions shown by name; the rest are "Other"
BILL_LIMIT = 200
MEETING_CHOICES = 120  # meetings offered in the page's list, latest first
_LAST_ACTION = "coalesce((select max(o.date) from e_on_agenda o where o.src = m.id), m.intro_date)"


class NotFound(LookupError):
    pass


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Cannot serialise {type(value).__name__}")


def to_json(value: Any) -> str:
    return json.dumps(value, default=_jsonable, ensure_ascii=False)


def _filters(params: dict[str, str]) -> Filters:
    since = params.get("since")
    return Filters(
        since=date.fromisoformat(since) if since else None,
        department=params.get("department") or None,
        body=params.get("body") or None,
        matter_type=params.get("type") or None,
        status=params.get("status") or None,
    )


def _top(params: dict[str, str]) -> int:
    return max(1, min(int(params.get("top") or 8), 25))


class App:
    def __init__(
        self,
        searcher: Searcher,
        chat: Callable[[], llm.ChatProvider],
        *,
        client: str,
        embed_model: str,
        notes_dir: Path | None = None,
        summary_written: Callable[[Path], None] | None = None,
        load: Callable[[], Searcher] | None = None,
    ) -> None:
        self.chat = chat
        self.client = client
        self.embed_model = embed_model
        self.notes_dir = notes_dir  # where the model's summaries of the law are kept; None keeps none
        self.summary_written = summary_written  # told of each summary file written, to copy it somewhere lasting
        self.load = load  # builds a searcher over what is on disk now; None means the data never changes
        # One DuckDB connection is shared by the request threads.
        self.lock = threading.Lock()
        self._adopt(searcher)

    def _adopt(self, searcher: Searcher) -> None:
        self.searcher = searcher
        self.loaded_utc = datetime.now(UTC).isoformat(timespec="seconds")
        views = {row[0] for row in searcher.con.sql("select table_name from information_schema.tables").fetchall()}
        columns = {
            row[0]
            for row in searcher.con.sql("select column_name from information_schema.columns where table_name = 'v_statute'").fetchall()
        }
        # What has been fetched beyond the record itself decides which views exist.
        self.has_law = "text" in columns and "e_changed_by" in views
        self.has_ordinances = "t_ordinance_text" in views
        self.has_bills = "v_bill" in views
        # What the chat model has filed under themes and storylines, if `legistutor themes` has run.
        self.has_themes = theme_filing.attach(searcher.con, searcher.data_dir, self.client)
        self.themes = {theme.id: theme for theme in theme_filing.read_themes()}

    def reload(self) -> None:
        """Read the data again, after a refresh. Questions are answered from the old copy
        until the new one is ready, and the old one is kept if the new one cannot be read."""
        if self.load is None:
            return
        searcher = self.load()  # the slow part, outside the lock
        with self.lock:
            old = self.searcher
            self._adopt(searcher)
        old.con.close()

    def close(self) -> None:
        """Close the DuckDB connection, once nothing else will ask the page for anything."""
        with self.lock:
            self.searcher.con.close()

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        with self.lock:
            return self.searcher.con.execute(sql, params or []).fetchall()

    # --- lookups -------------------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        def column(sql: str) -> list[str]:
            return [row[0] for row in self._rows(sql)]

        matters, meetings, passages, latest = self._rows(
            """select (select count(*) from v_matter), (select count(*) from v_meeting),
                      (select count(*) from chunk), (select max(date) from v_meeting)"""
        )[0]
        return {
            "client": self.client,
            "jurisdiction": rag.JURISDICTIONS.get(self.client, self.client),
            "matters": matters,
            "meetings": meetings,
            "passages": passages,
            "latest_meeting": latest,
            "loaded_utc": self.loaded_utc,
            "chat_model": self.chat().model,
            "embed_model": self.embed_model,
            "bodies": column("select name from v_body where id in (select body from e_on_agenda) order by name"),
            "departments": column("select name from v_department order by name"),
            "matter_types": column("select distinct matter_type from chunk where matter_type is not null order by 1"),
            "statuses": column("select distinct status from chunk where status is not null order by 1"),
            "law": self._law_meta(),
            "themes": self.has_themes,
        }

    def _law_meta(self) -> dict[str, Any] | None:
        if not self.has_law:
            return None
        chapters, sections, edition = self._rows(
            """select count(*) filter (where kind = 'chapter' and status is not null),
                      count(*) filter (where kind = 'section' and status is not null), max(edition) from v_statute"""
        )[0]
        years = [row[0] for row in self._rows("select distinct year from e_changed_by where basis = 'table2' order by 1")]
        return {
            "chapters": chapters,
            "sections": sections,
            "edition": edition,
            "years": years,
            "ordinances": self._rows("select count(*) from v_ordinance where title is not null")[0][0] if self.has_ordinances else 0,
            "bills": self._rows("select count(*) from v_bill")[0][0] if self.has_bills else 0,
            "bill_sessions": [r[0] for r in self._rows("select distinct session from v_bill order by 1 desc")] if self.has_bills else [],
            "searchable": self.searcher.law,
        }

    def search(self, params: dict[str, str]) -> dict[str, Any]:
        question = (params.get("q") or "").strip()
        if not question:
            return {"hits": []}
        with self.lock:
            hits = self.searcher.search(question, top=_top(params), filters=_filters(params))
            law = self.searcher.search_law(question, top=LAW_SOURCES + 1)
            record_best, law_best = self.searcher.best_similarity(question) if law else (0.0, 0.0)
        lead = "law" if law and law_best - record_best > LAW_LEAD_MARGIN else "record"
        return {"hits": hits, "law": law, "lead": lead, "closest": {"record": round(record_best, 3), "law": round(law_best, 3)}}

    def ask(self, params: dict[str, str]) -> Iterator[tuple[str, Any]]:
        """Events for one question: ``sources``, then ``token`` repeatedly, then
        ``done`` (or ``error``)."""
        question = (params.get("q") or "").strip()
        started = time.monotonic()
        try:
            found = self.search(params)
            hits = found["hits"]
            # Law text goes to the model when the question names the law, or matches the law better than the record.
            wants_law = rag.asks_about_law(question) or found.get("lead") == "law" or params.get("law") == "1"
            law = found.get("law", [])[:LAW_SOURCES] if wants_law else []
            chat = self.chat()
            prompt = rag.build_prompt(question, hits, chat, client=self.client, law=law)
            yield "sources", {"hits": prompt.sources, "law": prompt.law, "dropped": prompt.dropped}
            answer: list[str] = []
            for text in chat.stream(prompt.system, prompt.user):
                answer.append(text)
                yield "token", {"text": text}
            yield "done", {
                "model": chat.model,
                "seconds": round(time.monotonic() - started, 1),
                "unsupported": rag.unsupported_claims("".join(answer), prompt),
            }
        except (llm.LLMError, EmbeddingError) as exc:
            yield "error", {"message": str(exc)}

    def matter(self, params: dict[str, str]) -> dict[str, Any]:
        matter = params.get("id") or self.searcher._by_number.get(normalize_file_number(params.get("file")))
        with self.lock:
            cards = self.searcher.cards([matter]) if matter else []
        if not cards:
            raise NotFound(f"No matter {params.get('file') or params.get('id')} is in the record. Check the file number and try again.")
        card = cards[0]
        # The item and its revisions travel together: 26-0333 is referred, 26-0333 R1 is adopted.
        relatives = [
            self.searcher._by_number[normalize_file_number(r["file_number"])] for r in card.related if r["kind"] == "revision"
        ]
        with self.lock:
            family = [card, *self.searcher.cards(relatives)]
        family.sort(key=lambda hit: (len(hit.file_number), hit.file_number))
        sections = dict(
            self._rows(
                "select section, text from t_matter_text where matter = ? and section in ('resolution', 'background')", [matter]
            )
        )
        attachments = self._rows("select name, url from t_attachment where matter = ? order by attachment_id", [matter])
        return {
            "card": card,
            "journey": [{"file_number": h.file_number, "status": h.status, "history": h.history, "awaiting": h.awaiting} for h in family],
            "neighbours": self._neighbours(matter),
            "sections": sections,
            "attachments": [{"name": name, "url": url} for name, url in attachments],
            "tags": self._tags_of(matter),
        }

    def _neighbours(self, matter: str) -> list[dict[str, Any]]:
        """The matter's one-hop neighbourhood, grouped by what the edge means."""
        matter_node = "m.id, 'matter', m.file_number, m.title"
        groups = [
            ("Requested by", "in", "select d.id, 'department', d.name, null from e_requested_by r join v_department d on d.id = r.dst where r.src = ?"),
            ("Earlier matters", "in", f"select {matter_node} || ' (' || r.kind || ')' from e_related_to r join v_matter m on m.id = r.src where r.dst = ? order by m.file_number"),
            ("Mentions", "in", f"select {matter_node} from e_references r join v_matter m on m.id = r.dst where r.src = ? order by m.file_number"),
            ("Heard by", "out", """select o.body, 'body', o.body_name, count(*) || case when count(*) = 1 then ' appearance' else ' appearances' end
                                   from e_on_agenda o where o.src = ? group by 1, 3 order by min(o.date)"""),
            ("Later matters", "out", f"select {matter_node} || ' (' || r.kind || ')' from e_related_to r join v_matter m on m.id = r.dst where r.src = ? order by m.file_number"),
            ("Mentioned by", "out", f"select {matter_node} from e_references r join v_matter m on m.id = r.src where r.dst = ? order by m.file_number"),
            ("Cites", "out", "select s.id, 'statute', v.label, null from e_cites c join v_statute s on s.id = c.dst join vertices v on v.id = s.id where c.src = ? order by s.number"),
            ("Cites session law", "out", "select v.id, 'session_law', v.label, c.citation from e_cites_law c join vertices v on v.id = c.dst where c.src = ? order by v.id"),
            ("Cites ordinance", "out", "select v.id, 'ordinance', v.label, null from e_cites_ordinance c join vertices v on v.id = c.dst where c.src = ? order by v.id"),
        ]
        result = []
        for name, side, sql in groups:
            rows = self._rows(sql, [matter])
            if rows:
                nodes = [{"id": r[0], "type": r[1], "label": r[2], "detail": r[3]} for r in rows[:GROUP_LIMIT]]
                result.append({"name": name, "side": side, "nodes": nodes, "more": max(0, len(rows) - GROUP_LIMIT)})
        return result

    def node(self, params: dict[str, str]) -> dict[str, Any]:
        """A department, statute or body, with the matters attached to it, newest first."""
        node = params.get("id") or ""
        found = self._rows("select type, label from vertices where id = ?", [node])
        if not found:
            raise NotFound(f"Nothing in the record has the ID {node}. Check the link and try again.")
        kind, label = found[0]
        if kind == "subdivision":  # shown inside its section
            return {**self.node({"id": node.split("#")[0]}), "focus": node}
        connected = {
            "department": ("requested by this department", "select src from e_requested_by where dst = ?", 1),
            # A chapter also answers for its sections.
            "statute": ("citing this statute", "select src from e_cites where dst = ? or dst in (select src from e_part_of where dst = ?)", 2),
            "body": ("that came before this board or committee", "select src from e_on_agenda where body = ?", 1),
            "session_law": ("citing this session law", "select src from e_cites_law where dst = ?", 1),
            "ordinance": ("citing this ordinance", "select src from e_cites_ordinance where dst = ?", 1),
            "bill": ("", "select null where ? is null", 1),
        }
        if kind not in connected:
            raise NotFound(f"{label} is a {kind}; open it as a matter")
        relation, subquery, uses = connected[kind]
        rows = self._rows(
            f"""select m.id, m.file_number, m.title, m.status, {_LAST_ACTION} as last_action
                from v_matter m where m.id in ({subquery}) order by last_action desc nulls last, m.file_number desc""",
            [node] * uses,
        )
        url = self._rows("select url from v_statute where id = ?", [node]) if kind == "statute" else []
        result = {
            "id": node,
            "type": kind,
            "label": label,
            "relation": relation,
            "url": url[0][0] if url else None,
            "total": len(rows),
            "matters": [
                {"id": r[0], "file_number": r[1], "title": r[2], "status": r[3], "last_action": r[4]} for r in rows[:NODE_MATTERS]
            ],
        }
        detail = {
            "statute": self._statute_detail if self.has_law else None,
            "session_law": self._session_law_detail if self.has_law else None,
            "ordinance": self._ordinance_detail if self.has_ordinances else None,
            "bill": self._bill_detail if self.has_bills else None,
        }.get(kind)
        if detail:
            result.update(detail(node))
            if kind in ("statute", "ordinance"):
                result["summary"] = self._kept_summary(node)
        return result

    # --- the model reading one piece of law ---------------------------------------------

    def _reading(self, node: str, question: str):
        """What the model would be given to summarise ``node`` or to answer ``question`` about it."""
        try:
            scope = lawnotes.load(self._rows, node)
        except IndexError:
            raise lawnotes.NothingToRead(f"No statute or ordinance {node!r}") from None
        chat = self.chat()
        jurisdiction = rag.JURISDICTIONS.get(self.client, self.client)
        room = lawnotes.budget(chat.context_chars, question, jurisdiction=jurisdiction)
        scores = self._closeness(scope, question) if question and scope.chars > room and self.searcher.law else None
        selection = lawnotes.select(scope, room, scores, summary=not question)
        system, user = lawnotes.build_prompt(scope, selection, question, jurisdiction=jurisdiction)
        return scope, selection, chat, system, user

    def _closeness(self, scope: lawnotes.Scope, question: str) -> dict[str, float]:
        """How close each part of a long text is to the question, from the search index."""
        by_kind = {
            "section": ("source", "parent = ?", scope.id),
            "chapter": ("parent", "chapter = ?", scope.id.removeprefix("mnstat:")),
            "ordinance": ("coalesce(heading, '')", "source = ?", scope.id),
        }
        key, where, value = by_kind[scope.kind]
        with self.lock:
            vector = self.searcher._vector(question)
            rows = self.searcher.con.execute(
                f"""select {key}, max(array_cosine_similarity(embedding, ?::FLOAT[{self.searcher.dim}]))
                    from law_chunk where {where} group by 1""",
                [vector, value],
            ).fetchall()
        return dict(rows)

    def _kept_summary(self, node: str) -> dict[str, Any] | None:
        """The summary on disk, and whether it was written by the current model from the current text."""
        kept = lawnotes.read_summary(self.notes_dir, node)
        if not kept:
            return None
        try:
            _, _, chat, system, user = self._reading(node, "")
        except lawnotes.NothingToRead:
            return None
        current = kept.get("model") == chat.model and kept.get("fingerprint") == lawnotes.fingerprint(system, user)
        return {**{k: kept.get(k) for k in ("text", "model", "written_utc", "coverage", "basis", "unsupported")}, "current": current}

    def law_ask(self, params: dict[str, str]) -> Iterator[tuple[str, Any]]:
        """Events for a summary (no ``q``) or an answer: ``scope`` saying what was read, then
        ``token`` repeatedly, then ``done`` (or ``error``). A summary already on disk from the
        same model and text is sent back as it is, unless ``fresh=1``."""
        node, question = params.get("id") or "", (params.get("q") or "").strip()
        started = time.monotonic()
        try:
            scope, selection, chat, system, user = self._reading(node, question)
            mark = lawnotes.fingerprint(system, user)
            yield "scope", {"id": scope.id, "label": scope.label, "kind": scope.kind, "basis": selection.basis, "coverage": selection.coverage}
            kept = None if question or params.get("fresh") == "1" else lawnotes.read_summary(self.notes_dir, scope.id)
            if kept and kept.get("model") == chat.model and kept.get("fingerprint") == mark:
                yield "token", {"text": kept["text"]}
                yield "done", {"model": chat.model, "kept": True, "written_utc": kept["written_utc"], "unsupported": kept.get("unsupported", [])}
                return
            answer: list[str] = []
            for text in chat.stream(system, user):
                answer.append(text)
                yield "token", {"text": text}
            full = "".join(answer).strip()
            unsupported = lawnotes.unsupported_references(full, scope, selection)
            if full and not question:
                written = lawnotes.write_summary(
                    self.notes_dir, scope.id, model=chat.model, fingerprint=mark, text=full,
                    coverage=selection.coverage, basis=selection.basis, unsupported=unsupported,
                )  # fmt: skip
                if written and self.summary_written:
                    self.summary_written(written)
            yield "done", {"model": chat.model, "kept": False, "seconds": round(time.monotonic() - started, 1), "unsupported": unsupported}
        except lawnotes.NothingToRead as exc:
            yield "error", {"message": str(exc)}
        except (llm.LLMError, EmbeddingError) as exc:
            yield "error", {"message": str(exc)}

    def most_cited_sections(self, limit: int) -> list[tuple[str, str, int]]:
        """Statute sections in force, by how many matters cite them: the ones worth summarising ahead of time."""
        return self._rows(
            """select s.id, v.label, count(distinct c.src) as matters
               from e_cites c join v_statute s on s.id = c.dst join vertices v on v.id = s.id
               where s.kind = 'section' and s.status = 'in force'
               group by all order by matters desc, s.id limit ?""",
            [limit],
        )

    # --- the law: statutes, session laws, ordinances, bills -------------------------

    def _changes_of(self, where: str, values: list[Any]) -> list[dict[str, Any]]:
        """Table 2 rows for some statutes, newest first, with the session law that made each."""
        rows = self._rows(
            f"""select c.statute, c.subdivision, c.action, c.year, l.id, v.label, c.article, c.section, c.law_anchor,
                       l.presented, t.effective, t.anchor is not null, l.bill
                from e_changed_by c join v_session_law l on l.id = c.dst join vertices v on v.id = l.id
                left join t_law_section t on t.session_law = c.dst and t.anchor = c.law_anchor
                where c.basis = 'table2' and {where}
                order by c.year desc, l.presented desc nulls last, c.statute, c.subdivision""",
            values,
        )
        return [
            {
                "statute": r[0], "subdivision": r[1], "action": r[2], "year": r[3], "law": r[4], "law_label": r[5],
                "article": r[6], "section": r[7], "anchor": r[8], "presented": r[9], "effective": r[10],
                "has_text": r[11], "bill": r[12],
            }
            for r in rows
        ]  # fmt: skip

    def _statute_detail(self, node: str) -> dict[str, Any]:
        number, kind, chapter, headnote, heading, status, note, text, history, edition = self._rows(
            "select number, kind, chapter, headnote, heading, status, note, text, history, edition from v_statute where id = ?",
            [node],
        )[0]
        detail: dict[str, Any] = {
            "number": number, "kind": kind, "chapter": chapter, "headnote": headnote, "heading": heading,
            "status": status, "note": note, "text": text, "history": history, "edition": edition,
            "fetched": status is not None,
        }  # fmt: skip
        if kind == "chapter":
            sections = self._rows(
                """select s.id, s.number, s.headnote, s.status,
                          (select count(distinct src) from e_cites c where c.dst = s.id),
                          (select count(*) from e_changed_by c where c.basis = 'table2' and c.statute = s.number)
                   from v_statute s where s.chapter = ? and s.kind = 'section'""",
                [chapter],
            )
            detail["sections"] = [
                {"id": r[0], "number": r[1], "headnote": r[2], "status": r[3], "matters": r[4], "changes": r[5]}
                for r in sorted(sections, key=lambda r: chapter_sort_key(r[1]))
            ]
            return detail
        subdivisions = self._rows(
            """select d.id, d.subdivision, d.headnote, d.status, d.text,
                      (select count(distinct src) from e_cites_subdivision c where c.dst = d.id)
               from v_subdivision d where d.section = ?""",
            [number],
        )

        def order(row: tuple) -> tuple[int, str]:
            digits = "".join(ch for ch in row[1] if ch.isdigit())
            return (int(digits) if digits else 0, row[1])

        detail["subdivisions"] = [
            {"id": r[0], "number": r[1], "headnote": r[2], "status": r[3], "text": r[4], "matters": r[5]}
            for r in sorted(subdivisions, key=order)
        ]
        detail["changes"] = self._changes_of("c.statute = ?", [number])
        detail["ordinances"] = (
            [
                {"id": r[0], "label": r[1], "states_authority": r[2]}
                for r in self._rows(
                    """select v.id, v.label, c.states_authority from e_ordinance_cites c join vertices v on v.id = c.src
                       where c.dst = ? order by v.label""",
                    [node],
                )
            ]
            if self.has_ordinances
            else []
        )
        refer = "select distinct {other}, v.label from e_cross_references x join vertices v on v.id = {other} where {this} = ? or {this} like ? order by 2 limit 60"
        detail["refers_to"] = [
            {"id": r[0], "label": r[1]} for r in self._rows(refer.format(other="x.dst", this="x.src"), [node, node + "#%"])
        ]
        referred = self._rows(
            """select distinct split_part(x.src, '#', 1) as section, v.label
               from e_cross_references x join vertices v on v.id = split_part(x.src, '#', 1)
               where x.dst = ? and split_part(x.src, '#', 1) <> ? order by 2 limit 60""",
            [node, node],
        )
        detail["referred_by"] = [{"id": r[0], "label": r[1]} for r in referred]
        return detail

    def _session_law_detail(self, node: str) -> dict[str, Any]:
        year, special, chapter, url, bill, presented, vetoed, title, summary = self._rows(
            "select year, special, chapter, url, bill, presented, vetoed, title, summary from v_session_law where id = ?", [node]
        )[0]
        enacted = self._rows("select b.id, b.identifier, b.title from e_enacted_as e join v_bill b on b.id = e.src where e.dst = ?", [node]) if self.has_bills else []
        cited = self._rows("select m.file_number, c.citation from e_cites_law c join v_matter m on m.id = c.src where c.dst = ? order by 1", [node])
        return {
            "url": url, "year": year, "special": special, "chapter": chapter, "bill": bill, "presented": presented,
            "vetoed": vetoed, "title": title, "summary": summary,
            "bill_node": {"id": enacted[0][0], "identifier": enacted[0][1], "title": enacted[0][2]} if enacted else None,
            "citations": [{"file_number": r[0], "citation": r[1]} for r in cited],
            "changes": self._by_reliance(self._changes_of("c.dst = ?", [node])),
        }  # fmt: skip

    def _ordinance_detail(self, node: str) -> dict[str, Any]:
        number, title, category, url, pdf_url, adopted, amended, notice, summary = self._rows(
            "select number, title, category, url, pdf_url, adopted, amended, notice, summary from v_ordinance where id = ?", [node]
        )[0]
        sections = self._rows("select heading, text from t_ordinance_text where ordinance = ? order by position", [node])
        statutes = self._rows(
            """select s.id, v.label, s.headnote, s.status, c.states_authority,
                      (select count(*) from e_changed_by x where x.basis = 'table2' and x.statute = s.number)
               from e_ordinance_cites c join v_statute s on s.id = c.dst join vertices v on v.id = s.id
               where c.src = ? order by c.states_authority desc, s.number""",
            [node],
        )
        return {
            "url": url, "number": number, "title": title, "category": category, "pdf_url": pdf_url, "adopted": adopted,
            "amended": amended, "notice": notice, "fetched": title is not None,
            "sections": [{"heading": r[0], "text": r[1]} for r in sections],
            "statutes": [
                {"id": r[0], "label": r[1], "headnote": r[2], "status": r[3], "states_authority": r[4], "changes": r[5]}
                for r in statutes
            ],
        }  # fmt: skip

    def _bill_detail(self, node: str) -> dict[str, Any]:
        row = self._rows(
            """select session, identifier, title, chamber, classification, subjects, sponsors, first_action, last_action,
                      last_action_text, passed, url, source_url from v_bill where id = ?""",
            [node],
        )[0]
        keys = ("session", "identifier", "title", "chamber", "classification", "subjects", "sponsors", "first_action",
                "last_action", "last_action_text", "passed", "openstates_url", "url")  # fmt: skip
        actions = self._rows("select date, chamber, description from t_bill_action where bill = ? order by sequence", [node])
        companions = self._rows("select b.id, b.identifier from e_companion c join v_bill b on b.id = c.dst where c.src = ?", [node])
        law = self._rows("select v.id, v.label from e_enacted_as e join vertices v on v.id = e.dst where e.src = ?", [node])
        return {
            **dict(zip(keys, row)),
            "actions": [{"date": r[0], "chamber": r[1], "description": r[2]} for r in actions],
            "companions": [{"id": r[0], "identifier": r[1]} for r in companions],
            "law": {"id": law[0][0], "label": law[0][1]} if law else None,
        }

    def _with_reliance(self, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add to each change what the record rests on it: matters citing the section, matters
        citing that very subdivision, ordinances citing the section, and the headnote."""
        numbers = sorted({change["statute"] for change in changes})
        facts = {
            r[0]: r[1:]
            for r in self._rows(
                f"""select s.number, s.headnote, s.status,
                          (select count(distinct src) from e_cites c where c.dst = s.id),
                          {"(select count(distinct src) from e_ordinance_cites c where c.dst = s.id)" if self.has_ordinances else "0"}
                   from v_statute s where s.number in (select unnest(?))""",
                [numbers],
            )
        }
        precise = dict(
            self._rows(
                "select dst, count(distinct src) from e_cites_subdivision where split_part(dst, '#', 1) in (select 'mnstat:' || unnest(?)) group by 1",
                [numbers],
            )
        )
        for change in changes:
            headnote, status, matters, ordinances = facts.get(change["statute"], (None, None, 0, 0))
            change.update(
                headnote=headnote, status=status, matters=matters, ordinances=ordinances,
                subdivision_matters=precise.get(f"mnstat:{change['statute']}#{change['subdivision']}", 0) if change["subdivision"] else 0,
            )  # fmt: skip
        return changes

    def _by_reliance(self, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Most relied on first; ties in statute order."""
        return sorted(
            self._with_reliance(changes),
            key=lambda c: (-c["matters"], -c["subdivision_matters"], -c["ordinances"], chapter_sort_key(c["statute"]), c["subdivision"] or ""),
        )

    def changes(self, params: dict[str, str]) -> dict[str, Any]:
        """What a session did to the statutes the record relies on.

        The state publishes which sections each session created, amended or repealed.
        Joined to the graph, that is a list ranked by reliance: how many matters and
        ordinances cite each changed section. It says that the law under an item
        moved. What the change means is for the County Attorney to say.
        """
        if not self.has_law:
            return {"available": False}
        years = [row[0] for row in self._rows("select distinct year from e_changed_by where basis = 'table2' order by 1")]
        year = int(params["year"]) if params.get("year") else (years[-1] if years else None)
        scope = params.get("scope") or "cited"
        cited = "select dst from e_cites" + (" union select dst from e_ordinance_cites" if self.has_ordinances else "")
        relied = f"('mnstat:' || c.statute) in ({cited})"
        changes = self._by_reliance(self._changes_of(f"c.year = ? and ({relied if scope == 'cited' else 'true'})", [year]))
        by_year: dict[int, dict[str, int]] = {y: dict.fromkeys((*CHANGE_ACTIONS, "Other"), 0) for y in years}
        for y, action, count in self._rows(
            f"select c.year, c.action, count(*) from e_changed_by c where c.basis = 'table2' and {relied} group by 1, 2"
        ):
            by_year[y][action if action in CHANGE_ACTIONS else "Other"] += count
        return {
            "available": True,
            "year": year,
            "scope": scope,
            "years": [{"year": y, "counts": counts, "total": sum(counts.values())} for y, counts in by_year.items()],
            "actions": [*CHANGE_ACTIONS, "Other"],
            "total": len(changes),
            "changes": changes[:400],
            "to_check": self._citations_to_check(),
        }

    def _citations_to_check(self) -> list[dict[str, Any]]:
        """Statutes a matter cites that are repealed, renumbered, or not in the statutes at all."""
        rows = self._rows(
            """select s.id, v.label, coalesce(s.status, 'not found'), s.note, m.id, m.file_number, m.title, c.citation,
                      coalesce((select max(o.date) from e_on_agenda o where o.src = m.id), m.intro_date) as last_action
               from e_cites c join v_statute s on s.id = c.dst join vertices v on v.id = s.id join v_matter m on m.id = c.src
               where s.kind = 'section' and (s.status <> 'in force' or s.status is null)
                 and s.chapter in (select number from v_statute where kind = 'chapter' and status is not null)
               order by last_action desc nulls last, m.file_number desc, s.number"""
        )
        return [
            {"statute": r[0], "label": r[1], "status": r[2], "note": r[3], "matter": r[4], "file_number": r[5], "title": r[6],
             "citation": r[7], "last_action": r[8]}
            for r in rows
        ]  # fmt: skip

    def law_text(self, params: dict[str, str]) -> dict[str, Any]:
        rows = self._rows(
            """select v.label, t.label, t.kind, t.heading, t.marked, t.effective, l.url
               from t_law_section t join v_session_law l on l.id = t.session_law join vertices v on v.id = l.id
               where t.session_law = ? and t.anchor = ?""",
            [params.get("law"), params.get("anchor")],
        ) if self.has_law else []
        if not rows:
            raise NotFound("That section of the session law has not been fetched")
        law, label, kind, heading, marked, effective, url = rows[0]
        return {"law": law, "label": label, "kind": kind, "heading": heading, "marked": marked, "effective": effective,
                "url": f"{url}#{params.get('anchor')}"}  # fmt: skip

    def ordinances(self) -> dict[str, Any]:
        if not self.has_ordinances:
            return {"available": False, "ordinances": []}
        rows = self._rows(
            """select o.id, o.number, o.title, o.category, o.adopted, o.amended, o.notice, o.url,
                      (select count(distinct src) from e_cites_ordinance c where c.dst = o.id),
                      (select count(*) from e_ordinance_cites c where c.src = o.id),
                      (select count(*) from e_ordinance_cites c join v_statute s on s.id = c.dst where c.src = o.id and s.status <> 'in force')
               from v_ordinance o where o.title is not null
               order by try_cast(o.number as integer) nulls last, o.title"""
        )
        keys = ("id", "number", "title", "category", "adopted", "amended", "notice", "url", "matters", "statutes", "statutes_not_in_force")
        return {"available": True, "ordinances": [dict(zip(keys, row)) for row in rows]}

    def bills(self, params: dict[str, str]) -> dict[str, Any]:
        """State bills whose title contains every word asked for, latest action first."""
        if not self.has_bills:
            return {"available": False, "bills": []}
        words = (params.get("q") or "").split()
        clauses, values = ["true"], []
        for word in words:
            clauses.append("(b.title ilike ? or b.subjects ilike ?)")
            values += [f"%{word}%", f"%{word}%"]
        if params.get("session"):
            clauses.append("b.session = ?")
            values.append(params["session"])
        if params.get("enacted") == "1":
            clauses.append("b.id in (select src from e_enacted_as)")
        rows = self._rows(
            f"""select b.id, b.session, b.identifier, b.title, b.sponsors, b.last_action, b.last_action_text,
                       (select v.label from e_enacted_as e join vertices v on v.id = e.dst where e.src = b.id limit 1),
                       (select e.dst from e_enacted_as e where e.src = b.id limit 1)
                from v_bill b where {" and ".join(clauses)}
                order by b.last_action desc nulls last, b.identifier limit {BILL_LIMIT + 1}""",
            values,
        )
        keys = ("id", "session", "identifier", "title", "sponsors", "last_action", "last_action_text", "law_label", "law")
        return {
            "available": True,
            "query": " ".join(words),
            "more": len(rows) > BILL_LIMIT,
            "bills": [dict(zip(keys, row)) for row in rows[:BILL_LIMIT]],
        }

    def queues(self) -> dict[str, Any]:
        """What the record says is coming to each body.

        Two ways an item is "coming up" for a body: a draft board agenda lists it
        under a "Referred to ..." heading (no action recorded yet), or a recorded
        action referred or returned it there and it has not appeared on any agenda
        since. An item sent somewhere more than ``STALE_DAYS`` ago and never heard
        is reported separately: in this record those are leftovers, not a backlog.
        """
        latest = self._rows("select max(date) from v_meeting")[0][0]
        queued: dict[str, list[dict[str, Any]]] = {}
        stale: list[dict[str, Any]] = []
        for body, matter, file_number, title, department, day, action in self._rows(
            """with last as (select src, max(date) as d from e_on_agenda group by 1)
               select b.name, m.id, m.file_number, m.title, m.department, r.date, r.action
               from e_referred_to r
               join last l on l.src = r.src and l.d = r.date  -- nothing has happened to it since
               join v_body b on b.id = r.dst join v_matter m on m.id = r.src
               order by b.name, r.date, m.file_number"""
        ):
            item = {
                "id": matter,
                "file_number": file_number,
                "title": title,
                "department": department,
                "date": day,
                "reason": f"{action} (recorded)" if action else "on a draft agenda, no action yet",
            }
            if action and latest and (latest - day).days > STALE_DAYS:
                stale.append({**item, "body": body})
            else:
                queued.setdefault(body, []).append(item)
        meetings = self._rows(
            """select mt.body_name, mt.date, mt.minutes_status, mt.site_url,
                      (select count(*) from e_on_agenda o where o.dst = mt.id) as items
               from v_meeting mt
               where mt.minutes_status <> 'Final' and mt.date >= (select max(date) from v_meeting) - interval 60 day
               order by mt.date desc, mt.body_name"""
        )
        return {
            "queues": sorted(
                ({"body": body, "items": items} for body, items in queued.items()), key=lambda q: (-len(q["items"]), q["body"])
            ),
            "stale": stale,
            "stale_days": STALE_DAYS,
            "meetings": [
                {"body": r[0], "date": r[1], "minutes_status": r[2], "url": r[3], "items": r[4]} for r in meetings
            ],
        }

    # --- themes and storylines ---------------------------------------------------------

    def _theme_names(self, ids: list[str] | None) -> list[dict[str, str]]:
        return [{"id": i, "name": self.themes[i].name} for i in ids or [] if i in self.themes]

    def _tags_of(self, matter: str) -> dict[str, Any] | None:
        """How one matter was filed: its gist, its themes, and the storyline it is part of."""
        if not self.has_themes:
            return None
        filed = self._rows("select gist, themes, model from matter_tag where matter = ?", [matter])
        story = self._rows(
            "select storyline, name, (select count(*) from storyline_member x where x.storyline = s.storyline) from storyline_member s where matter = ?",
            [matter],
        )
        if not filed and not story:
            return None
        return {
            "gist": filed[0][0] if filed else None,
            "themes": self._theme_names(filed[0][1]) if filed else [],
            "model": filed[0][2] if filed else None,
            "storyline": {"id": story[0][0], "name": story[0][1], "matters": story[0][2]} if story else None,
        }

    def explore(self) -> dict[str, Any]:
        """Every theme, with the number of matters before a body in each month of the record."""
        if not self.has_themes:
            return {"available": False}
        first, last = self._rows("select min(date), max(date) from v_meeting")[0]
        months, (year, month) = [], (first.year, first.month)
        while (year, month) <= (last.year, last.month):
            months.append(f"{year}-{month:02d}")
            year, month = (year, month + 1) if month < 12 else (year + 1, 1)
        by_month: dict[str, dict[str, int]] = {}
        for theme, label, count in self._rows(
            """select t.theme, strftime(d.day, '%Y-%m'), count(distinct d.matter)
               from (select matter, unnest(themes) as theme from matter_tag) t join matter_day d using (matter) group by 1, 2"""
        ):
            by_month.setdefault(theme, {})[label] = count
        totals = dict(self._rows("select theme, count(*) from (select unnest(themes) as theme from matter_tag) group by 1"))
        storylines = dict(
            self._rows(
                """select theme, count(distinct storyline) from (select storyline, unnest(themes) as theme from storyline_member) group by 1"""
            )
        )
        filed, without, model, latest = self._rows(
            "select count(*), count(*) filter (where len(themes) = 0), max(model), max(tagged_utc) from matter_tag"
        )[0]
        return {
            "available": True,
            "months": months,
            "record_starts": first,
            "record_ends": last,
            "filed": filed,
            "without_a_theme": without,
            "model": model,
            "filed_utc": latest,
            "storylines": self._rows("select count(distinct storyline) from storyline_member")[0][0],
            "themes": [
                {
                    "id": theme.id,
                    "area": theme.area,
                    "name": theme.name,
                    "description": theme.description,
                    "matters": totals.get(theme.id, 0),
                    "storylines": storylines.get(theme.id, 0),
                    "months": by_month.get(theme.id, {}),
                }
                for theme in self.themes.values()
            ],
        }

    def _filed_matters(self, where: str, values: list[Any]) -> list[dict[str, Any]]:
        """Matters with how they were filed and the days they were before a body, oldest first."""
        rows = self._rows(
            f"""select m.id, m.file_number, m.title, m.status, m.department, t.gist, t.themes, s.storyline, s.basis, d.days
                from v_matter m left join matter_tag t on t.matter = m.id left join storyline_member s on s.matter = m.id
                left join (select matter, list(day order by day) as days from matter_day group by 1) d on d.matter = m.id
                where {where}
                order by d.days[1] nulls last, m.file_number""",
            values,
        )
        return [
            {
                "id": r[0], "file_number": r[1], "title": r[2], "status": r[3], "department": r[4], "gist": r[5],
                "themes": r[6] or [], "storyline": r[7], "basis": r[8], "days": r[9] or [],
            }
            for r in rows
        ]  # fmt: skip

    def theme(self, params: dict[str, str]) -> dict[str, Any]:
        """One theme: its storylines, and every matter filed under it or belonging to one of them."""
        theme = self.themes.get(params.get("id") or "") if self.has_themes else None
        if theme is None:
            raise NotFound(f"There is no theme called {params.get('id')}. Pick one from the Explore page.")
        matters = self._filed_matters(
            """m.id in (select matter from matter_tag where list_contains(themes, ?)
                        union select matter from storyline_member where list_contains(themes, ?))""",
            [theme.id, theme.id],
        )
        names = dict(self._rows("select distinct storyline, name from storyline_member where list_contains(themes, ?)", [theme.id]))
        storylines: dict[str, dict[str, Any]] = {}
        for matter in matters:
            # A storyline filed mostly elsewhere is not this theme's, though one of its items is.
            if matter["storyline"] not in names:
                matter["storyline"] = None
                continue
            story = storylines.setdefault(
                matter["storyline"], {"id": matter["storyline"], "name": names[matter["storyline"]], "matters": 0, "first": None, "last": None}
            )
            story["matters"] += 1
            if matter["days"]:
                story["first"] = min(filter(None, [story["first"], matter["days"][0]]))
                story["last"] = max(filter(None, [story["last"], matter["days"][-1]]))
        return {
            "id": theme.id,
            "name": theme.name,
            "area": theme.area,
            "description": theme.description,
            "record_ends": self._rows("select max(date) from v_meeting")[0][0],
            "storylines": sorted(storylines.values(), key=lambda s: (-s["matters"], str(s["first"]))),
            "matters": matters,
        }

    def storyline(self, params: dict[str, str]) -> dict[str, Any]:
        """One storyline: its matters in date order, and what put each of them in it."""
        chosen = params.get("id") or ""
        found = self._rows("select name, themes from storyline_member where storyline = ? limit 1", [chosen]) if self.has_themes else []
        if not found:
            raise NotFound(f"There is no storyline {chosen}. Pick one from a theme on the Explore page.")
        matters = self._filed_matters("s.storyline = ?", [chosen])
        days = [day for matter in matters for day in matter["days"]]
        return {
            "id": chosen,
            "name": found[0][0],
            "themes": self._theme_names(found[0][1]),
            "first": min(days, default=None),
            "last": max(days, default=None),
            "record_ends": self._rows("select max(date) from v_meeting")[0][0],
            "matters": matters,
        }

    def meeting(self, params: dict[str, str]) -> dict[str, Any]:
        """One meeting's items under their themes, and the meetings to choose from, latest first."""
        filed = "count(distinct o.src) filter (where o.src in (select matter from matter_tag))" if self.has_themes else "0"
        meetings = self._rows(
            f"""select mt.id, mt.date, mt.body_name, mt.site_url, count(distinct o.src), {filed}
                from v_meeting mt join e_on_agenda o on o.dst = mt.id group by all order by mt.date desc, mt.body_name limit {MEETING_CHOICES}"""
        )
        if not meetings:
            return {"meetings": [], "meeting": None, "groups": []}
        # Unless one is asked for, the latest meeting with something filed: a meeting of minutes alone says little.
        chosen = params.get("id") or next((m[0] for m in meetings if m[5]), meetings[0][0])
        facts = self._rows("select id, date, body_name, site_url from v_meeting where id = ?", [chosen])
        if not facts:
            raise NotFound(f"There is no meeting {chosen} in the record. Pick one from the list.")
        tagged = "left join matter_tag t on t.matter = m.id left join storyline_member s on s.matter = m.id" if self.has_themes else ""
        columns = "t.gist, t.themes, s.storyline, s.name" if self.has_themes else "null, null, null, null"
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in self._rows(
            f"""select m.id, m.file_number, m.title, m.matter_type, o.action, o.result, {columns}
                from e_on_agenda o join v_matter m on m.id = o.src {tagged}
                where o.dst = ? order by o.agenda_number, m.file_number""",
            [chosen],
        ):
            themes = [t for t in row[7] or [] if t in self.themes]
            item = {
                "id": row[0], "file_number": row[1], "title": row[2], "matter_type": row[3], "action": row[4], "result": row[5],
                "gist": row[6], "also": self._theme_names(themes[1:]), "storyline": {"id": row[8], "name": row[9]} if row[8] else None,
            }  # fmt: skip
            groups.setdefault(themes[0] if themes else "", []).append(item)
        order = {theme: n for n, theme in enumerate(self.themes)} | {"": len(self.themes)}
        return {
            "meetings": [{"id": r[0], "date": r[1], "body": r[2], "url": r[3], "items": r[4]} for r in meetings],
            "meeting": {"id": facts[0][0], "date": facts[0][1], "body": facts[0][2], "url": facts[0][3]},
            "groups": [
                {"theme": theme or None, "name": self.themes[theme].name if theme else "Routine or not filed", "items": items}
                for theme, items in sorted(groups.items(), key=lambda pair: order[pair[0]])
            ],
        }

    def topic(self, params: dict[str, str]) -> dict[str, Any]:
        """Matters about a topic, counted by the quarter they were introduced and by
        requesting department.

        "About" is deliberately literal here: every word of the query must appear in
        one passage of the matter (after stemming, so "roads" finds "road"). Ranking
        by meaning is right for finding the best few matters, but it has no natural
        cut-off, and a count needs one.
        """
        question = (params.get("q") or "").strip()
        if not question or not self.searcher.keywords:
            return {"query": question, "total": 0, "quarters": [], "departments": [], "matters": []}
        where, values = _filters(params).sql()
        gist, tagged = ("t.gist", "left join matter_tag t on t.matter = m.id") if self.has_themes else ("null", "")
        rows = self._rows(
            f"""select m.id, m.file_number, m.title, m.status, coalesce(m.department, 'No department'),
                       coalesce(m.intro_date, c.last_action) as day, {gist}
                from (select matter, max(last_action_date) as last_action
                      from (select matter, last_action_date, fts_main_chunk.match_bm25(chunk_id, ?, conjunctive := 1) as score
                            from chunk where {where})
                      where score is not null group by 1) c
                join v_matter m on m.id = c.matter {tagged}
                order by day desc nulls last, m.file_number desc""",
            [question, *values],
        )
        by_department: dict[str, int] = {}
        for row in rows:
            by_department[row[4]] = by_department.get(row[4], 0) + 1
        # Many matters have no requesting department recorded; that is an absence,
        # not a department, so it never takes one of the named slots.
        ranked = sorted((d for d in by_department if d != "No department"), key=lambda d: (-by_department[d], d))
        named = ranked[:TOPIC_DEPARTMENTS]
        departments = (
            named
            + (["Other departments"] if len(ranked) > len(named) else [])
            + (["No department"] if "No department" in by_department else [])
        )
        first, last = self._rows("select min(date), max(date) from v_meeting")[0]
        quarters: dict[str, dict[str, Any]] = {}
        if rows and first:
            year, quarter = first.year, (first.month - 1) // 3 + 1
            while (year, quarter) <= (last.year, (last.month - 1) // 3 + 1):  # every quarter, including empty ones
                label = f"{year} Q{quarter}"
                quarters[label] = {"quarter": label, "counts": dict.fromkeys(departments, 0), "total": 0}
                year, quarter = (year, quarter + 1) if quarter < 4 else (year + 1, 1)
        matters = []
        for matter, file_number, title, status, department, day, matter_gist in rows:
            label = f"{day.year} Q{(day.month - 1) // 3 + 1}" if day else None
            group = department if department in named or department == "No department" else "Other departments"
            if label in quarters:
                quarters[label]["counts"][group] += 1
                quarters[label]["total"] += 1
            matters.append(
                {"id": matter, "file_number": file_number, "title": title, "gist": matter_gist, "status": status,
                 "department": department, "date": day, "quarter": label}
            )
        return {
            "query": question,
            "total": len(rows),
            "record_starts": first,
            "record_ends": last,
            "departments": departments,
            "quarters": list(quarters.values()),
            "matters": matters,
        }


def _handler(app: App) -> type[BaseHTTPRequestHandler]:
    routes: dict[str, Callable[[dict[str, str]], Any]] = {
        "/api/meta": lambda params: app.meta(),
        "/api/search": app.search,
        "/api/matter": app.matter,
        "/api/node": app.node,
        "/api/queues": lambda params: app.queues(),
        "/api/topic": app.topic,
        "/api/changes": app.changes,
        "/api/law_text": app.law_text,
        "/api/ordinances": lambda params: app.ordinances(),
        "/api/bills": app.bills,
        "/api/explore": lambda params: app.explore(),
        "/api/theme": app.theme,
        "/api/storyline": app.storyline,
        "/api/meeting": app.meeting,
    }
    page = resources.files("legistutor").joinpath("static", "index.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
            log.debug("%s %s", self.address_string(), format % args)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: Any) -> None:
            self._send(status, "application/json; charset=utf-8", to_json(value).encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802 - the base class's name
            url = urlparse(self.path)
            params = {key: values[-1] for key, values in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    self._send(200, "text/html; charset=utf-8", page.read_bytes())
                elif url.path == "/api/ask":
                    self._stream(app.ask(params))
                elif url.path == "/api/law_ask":
                    self._stream(app.law_ask(params))
                elif url.path in routes:
                    self._json(200, routes[url.path](params))
                else:
                    self._json(404, {"error": "Not found"})
            except NotFound as exc:
                self._json(404, {"error": str(exc)})
            except (EmbeddingError, ValueError) as exc:
                self._json(400 if isinstance(exc, ValueError) else 503, {"error": str(exc)})
            except (BrokenPipeError, ConnectionError):
                pass  # the browser went away mid-answer

        def _stream(self, events: Iterator[tuple[str, Any]]) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            for name, data in events:
                self.wfile.write(f"event: {name}\ndata: {to_json(data)}\n\n".encode("utf-8"))
                self.wfile.flush()

    return Handler


def make_server(app: App, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Listens on this machine only unless told otherwise. A Databricks App must listen on
    every interface; there the platform's proxy is what faces people, and it signs them in."""
    try:
        server = _Server((host, port), _handler(app))
    except OSError as exc:
        raise RuntimeError(f"Port {port} is in use. Is the page already running? Stop it, or pick another with --port.") from exc
    server.daemon_threads = True
    return server


class _Server(ThreadingHTTPServer):
    # The default lets a second copy bind a port that is already being served on
    # Windows; the two then answer requests at random. Fail instead.
    allow_reuse_address = os.name != "nt"


def notes_dir(data_dir: Path, client: str) -> Path:
    return gold.gold_root(Path(data_dir), client) / "law_summaries"


def application(data_dir: Path, client: str) -> App:
    settings = config.settings()
    mirror = None
    if settings.provider == "databricks":
        from . import databricks

        # In a Databricks App the volume is not on disk: read a local copy, and send new summaries back.
        data_dir, mirror = databricks.mirror_if_needed(Path(data_dir))
    embedder = make_embedder(settings)
    app = App(
        Searcher(data_dir, client, embedder),
        lambda: llm.chat_model(config.settings()),
        client=client,
        embed_model=settings.embed_model,
        notes_dir=notes_dir(data_dir, client),
        summary_written=mirror.push if mirror else None,
        load=lambda: Searcher(data_dir, client, embedder),
    )
    app.mirror = mirror
    return app


def data_signature(data_dir: Path) -> frozenset[tuple[str, int, int]]:
    """Every table the page reads, with its size and time of writing: differs after a refresh."""
    data_dir = Path(data_dir)
    return frozenset(
        (path.relative_to(data_dir).as_posix(), path.stat().st_size, path.stat().st_mtime_ns)
        for layer in ("silver", "gold")
        for path in (data_dir / layer).rglob("*.parquet")
    )


class Watcher:
    """Reloads the page's data when a refresh has rewritten it, so nobody has to restart the page.

    A refresh writes its tables over some minutes. The data is read again only once two looks
    in a row see the same files, so a refresh under way is waited out, not read halfway.
    """

    def __init__(self, app: App, pull: Callable[[], Any] | None = None) -> None:
        self.app = app
        self.pull = pull  # in a Databricks App: fetch what changed in the volume first
        self.loaded = data_signature(app.searcher.data_dir)
        self.seen = self.loaded

    def look(self) -> bool:
        """One look at the files. True if the data was reloaded."""
        if self.pull:
            self.pull()
        now = data_signature(self.app.searcher.data_dir)
        settled, self.seen = now == self.seen, now
        if now == self.loaded or not settled:
            return False
        self.app.reload()
        self.loaded = now
        log.info("The data changed on disk; reloaded it (%s tables)", len(now))
        return True

    def run(self, seconds: float, stop: threading.Event) -> None:
        while not stop.wait(seconds):
            try:
                self.look()
            except Exception:  # noqa: BLE001 - the page keeps serving what it has, and looks again later
                log.exception("Could not reload the data; still serving the copy loaded at %s", self.app.loaded_utc)


def watch(app: App, seconds: float) -> Callable[[], None]:
    """Start looking for new data every ``seconds``. Call the returned function to stop.

    Stopping waits for the thread to end. A thread still running at exit keeps the app, and
    with it DuckDB's objects, alive, and DuckDB's bindings (nanobind) then report them as leaked.
    """
    stop = threading.Event()
    mirror = getattr(app, "mirror", None)
    watcher = Watcher(app, mirror.pull if mirror else None)
    thread = threading.Thread(target=watcher.run, args=(seconds, stop), name="legistutor-reload", daemon=True)
    thread.start()

    def end() -> None:
        stop.set()
        thread.join(timeout=30)  # a reload under way finishes first

    return end


def serve(
    data_dir: Path, client: str, *, port: int = 8765, host: str = "127.0.0.1", open_browser: bool = True, reload_seconds: float = 120.0
) -> None:
    app = application(data_dir, client)
    server = make_server(app, port, host)
    stop_watching = watch(app, reload_seconds) if reload_seconds > 0 else None
    address = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{server.server_address[1]}/"
    print(f"Serving {gold.chunks_path(app.searcher.data_dir, client)} at {address}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if stop_watching:
            stop_watching()
        server.server_close()
        app.close()
