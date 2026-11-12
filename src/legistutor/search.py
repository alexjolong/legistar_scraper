"""Hybrid retrieval over the gold index, with each result filled in from the graph.

Three signals find passages:

* **file numbers** in the question (``26-0333``) are looked up directly;
* **meaning**: cosine similarity between the question and passage embeddings;
* **keywords**: BM25 over the same passages, which catches agreement numbers,
  vendor names and other exact strings that embeddings blur.

The meaning and keyword rankings are merged with reciprocal rank fusion, passages
are grouped by matter, and each matter comes back as a :class:`Hit` carrying its
status, its path through the bodies, and its links, read from the silver graph.

On Databricks, steps two and three become one hybrid query against a Vector
Search index; the graph lookups run unchanged on Spark SQL.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from . import gold, query
from .embed import Embedder
from .text import base_file_number, extract_file_numbers, normalize_file_number

log = logging.getLogger(__name__)

RRF_K = 60  # the usual constant: damps the gap between adjacent ranks
CANDIDATES = 100  # passages taken from each ranking before fusion
SECTION_ORDER = {"title": 0, "resolution": 1, "background": 2}
_FILE_NUMBER_ONLY = re.compile(r"\b\d{2}N?-[A-Z0-9-]+(?:\s?[RS]\d)*")


@dataclass
class Passage:
    chunk_id: str
    section: str
    chunk_index: int
    text: str


@dataclass
class Hit:
    matter: str
    file_number: str
    title: str | None
    matter_type: str | None
    status: str | None
    department: str | None
    intro_date: date | None
    recommendation: str | None
    url: str | None
    score: float = 0.0
    matched_by: list[str] = field(default_factory=list)  # "file number", "meaning", "keywords"
    passages: list[Passage] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)  # date, body, action, result, referred_to
    awaiting: list[str] = field(default_factory=list)  # bodies it is queued for, not yet acted on
    related: list[dict[str, str]] = field(default_factory=list)  # file_number, kind, direction
    statutes: list[str] = field(default_factory=list)


@dataclass
class LawPassage:
    source: str  # vertex id of the section, subdivision or ordinance the text is from
    label: str  # "Minn. Stat. § 383B.158, subd. 2"
    heading: str | None
    text: str


@dataclass
class LawHit:
    """A statute section or a county ordinance found by the search, with the passages that matched."""

    id: str  # the section's or the ordinance's vertex id
    kind: str  # "statute" or "ordinance"
    label: str
    title: str | None  # headnote, or the ordinance's title
    url: str | None
    status: str | None
    cited_by: int = 0  # matters in the record that cite it
    score: float = 0.0
    matched_by: list[str] = field(default_factory=list)
    passages: list[LawPassage] = field(default_factory=list)


@dataclass
class Filters:
    since: date | None = None  # last action on or after
    department: str | None = None  # substring of the requesting department
    body: str | None = None  # substring of a body the matter appeared before
    matter_type: str | None = None
    status: str | None = None

    def sql(self) -> tuple[str, list[Any]]:
        clauses, params = ["true"], []
        if self.since:
            clauses.append("last_action_date >= ?")
            params.append(self.since)
        if self.department:
            clauses.append("department ilike ?")
            params.append(f"%{self.department}%")
        if self.body:
            clauses.append("matter in (select src from e_on_agenda where body_name ilike ?)")
            params.append(f"%{self.body}%")
        if self.matter_type:
            clauses.append("matter_type ilike ?")
            params.append(self.matter_type)
        if self.status:
            clauses.append("status ilike ?")
            params.append(self.status)
        return " and ".join(clauses), params


class Searcher:
    def __init__(self, data_dir: Path, client: str, embedder: Embedder) -> None:
        path = gold.chunks_path(data_dir, client)
        if not path.exists():
            raise FileNotFoundError(f"No search index at {path}. Run `legistutor index` first.")
        self.embedder = embedder
        self.con = query.connect(data_dir, client)
        source = path.resolve().as_posix().replace("'", "''")
        # Parquet gives the vectors back as variable-length lists; the similarity
        # functions want fixed-size arrays.
        self.dim = self.con.sql(f"select len(embedding) from read_parquet('{source}') limit 1").fetchall()[0][0]
        self.con.execute(
            f"""create table chunk as
                select * replace (embedding::FLOAT[{self.dim}] as embedding) from read_parquet('{source}')"""
        )
        models = [row[0] for row in self.con.sql("select distinct embedding_model from chunk").fetchall()]
        if models != [embedder.model]:
            raise RuntimeError(
                f"The index was built with {models}, not {embedder.model!r}; their vectors are not comparable. "
                "Run `legistutor index` with the model you want to search with."
            )
        self.keywords = self._build_keyword_index("chunk")
        self.law = self._load_law(gold.law_chunks_path(data_dir, client))
        self._last_query: tuple[str, list[float]] | None = None
        # File numbers as typed ("26-0333", "26-0333R1") to matter ids, and each
        # base number to the original item plus its revisions.
        self._by_number: dict[str, str] = {}
        self._by_base: dict[str, list[str]] = {}
        for matter, file_number in self.con.sql("select id, file_number from v_matter order by matter_id").fetchall():
            self._by_number[normalize_file_number(file_number)] = matter
            self._by_base.setdefault(base_file_number(file_number), []).append(matter)

    def _build_keyword_index(self, table: str) -> bool:
        try:
            self.con.execute("install fts")
            self.con.execute("load fts")
            # Keep digits: the default tokenizer drops them, and agreement and
            # file numbers are exactly what keyword search is here for.
            self.con.execute(f"pragma create_fts_index('{table}', 'chunk_id', 'embed_text', ignore='[^a-z0-9]+')")
            return True
        except Exception as exc:  # the extension is downloaded on first use
            log.warning("Keyword search is unavailable (DuckDB fts extension: %s); using embeddings only.", exc)
            return False

    def _load_law(self, path: Path) -> bool:
        """The statute and ordinance passages, when they have been indexed with this model."""
        if not path.exists():
            return False
        source = path.resolve().as_posix().replace("'", "''")
        models = [row[0] for row in self.con.sql(f"select distinct embedding_model from read_parquet('{source}')").fetchall()]
        if models != [self.embedder.model]:
            log.warning("The law index was built with %s; run `legistutor index` to search statutes and ordinances.", models)
            return False
        self.con.execute(
            f"""create table law_chunk as
                select * replace (embedding::FLOAT[{self.dim}] as embedding) from read_parquet('{source}')"""
        )
        self.law_keywords = self._build_keyword_index("law_chunk")
        return True

    def _vector(self, question: str) -> list[float]:
        """The question's embedding; the record search and the law search share one."""
        if self._last_query is None or self._last_query[0] != question:
            self._last_query = (question, self.embedder.embed_query(question))
        return self._last_query[1]

    def search_law(self, question: str, *, top: int = 4, per_source: int = 2) -> list[LawHit]:
        """Statute sections and ordinances whose text matches the question, by meaning
        and by keywords, best first. Subdivisions are grouped under their section."""
        # A bare file number asks for an item, not for law.
        if not self.law or not re.search(r"[A-Za-z]{3,}", _FILE_NUMBER_ONLY.sub(" ", question)):
            return []
        vector = self._vector(question)
        similarity = self.con.execute(
            f"""select chunk_id, parent, array_cosine_similarity(embedding, ?::FLOAT[{self.dim}]) as score
                from law_chunk order by score desc limit {CANDIDATES}""",
            [vector],
        ).fetchall()
        parent_of = {chunk_id: parent for chunk_id, parent, _ in similarity}
        rankings = {"meaning": [chunk_id for chunk_id, _, _ in similarity]}
        if self.law_keywords:
            rows = self.con.execute(
                f"""select chunk_id, parent from (
                        select chunk_id, parent, fts_main_law_chunk.match_bm25(chunk_id, ?) as score from law_chunk)
                    where score is not null order by score desc limit {CANDIDATES}""",
                [question],
            ).fetchall()
            parent_of.update(rows)
            rankings["keywords"] = [chunk_id for chunk_id, _ in rows]
        fused: dict[str, float] = {}
        signals: dict[str, set[str]] = {}
        for signal, ranking in rankings.items():
            for rank, chunk_id in enumerate(ranking, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
                signals.setdefault(parent_of[chunk_id], set()).add(signal)
        by_parent: dict[str, list[str]] = {}
        for chunk_id in sorted(fused, key=fused.get, reverse=True):
            by_parent.setdefault(parent_of[chunk_id], []).append(chunk_id)
        parents = list(by_parent)[:top]
        hits = {hit.id: hit for hit in self.law_cards(parents)}
        chosen = [chunk_id for parent in parents for chunk_id in by_parent[parent][:per_source]]
        passages = {
            row[0]: (row[1], LawPassage(*row[2:]))
            for row in self.con.execute(
                "select chunk_id, parent, source, label, heading, text from law_chunk where chunk_id in (select unnest(?))", [chosen]
            ).fetchall()
        }
        for chunk_id in chosen:
            parent, passage = passages[chunk_id]
            if parent in hits:
                hits[parent].passages.append(passage)
        for parent, hit in hits.items():
            hit.score = fused[by_parent[parent][0]]
            hit.matched_by = sorted(signals.get(parent, ()))
        return [hits[parent] for parent in parents if parent in hits]

    def best_similarity(self, question: str) -> tuple[float, float]:
        """The closest passage to the question in the record, and in the law. The two
        numbers come from one question in one vector space, so unlike scores from
        different questions they can be compared: the larger says where the better match is."""
        vector = self._vector(question)

        def best(table: str) -> float:
            return self.con.execute(
                f"select max(array_cosine_similarity(embedding, ?::FLOAT[{self.dim}])) from {table}", [vector]
            ).fetchall()[0][0] or 0.0

        return best("chunk"), (best("law_chunk") if self.law else 0.0)

    def law_cards(self, ids: list[str]) -> list[LawHit]:
        """Statute sections and ordinances by vertex id, with how many matters cite each."""
        if not ids:
            return []
        hits: dict[str, LawHit] = {}
        for vid, label, title, url, status in self.con.execute(
            """select s.id, v.label, s.headnote, s.url, s.status from v_statute s join vertices v on v.id = s.id
               where s.id in (select unnest(?))""",
            [ids],
        ).fetchall():
            hits[vid] = LawHit(vid, "statute", label, title, url, status)
        if any(vid not in hits for vid in ids) and self._has_view("v_ordinance"):
            for vid, number, title, url in self.con.execute(
                "select id, number, title, url from v_ordinance where id in (select unnest(?))", [ids]
            ).fetchall():
                hits[vid] = LawHit(vid, "ordinance", f"Ordinance {number}" if number else (title or vid), title, url, "in force")
        for vid, count in self.con.execute(
            """select dst, count(distinct src) from (
                   select dst, src from e_cites union all select dst, src from e_cites_ordinance)
               where dst in (select unnest(?)) group by 1""",
            [ids],
        ).fetchall():
            if vid in hits:
                hits[vid].cited_by = count
        return [hits[vid] for vid in ids if vid in hits]

    def _has_view(self, name: str) -> bool:
        return bool(self.con.execute("select 1 from information_schema.tables where table_name = ?", [name]).fetchall())

    def search(self, question: str, *, top: int = 8, per_matter: int = 2, filters: Filters | None = None) -> list[Hit]:
        where, params = (filters or Filters()).sql()
        vector = self._vector(question)
        similarity = self.con.execute(
            f"""select chunk_id, matter, array_cosine_similarity(embedding, ?::FLOAT[{self.dim}]) as score
                from chunk where {where} order by score desc""",
            [vector, *params],
        ).fetchall()
        matter_of = {chunk_id: matter for chunk_id, matter, _ in similarity}
        rankings = {"meaning": [chunk_id for chunk_id, _, _ in similarity[:CANDIDATES]]}
        if self.keywords:
            rankings["keywords"] = [
                row[0]
                for row in self.con.execute(
                    f"""select chunk_id from (
                            select chunk_id, fts_main_chunk.match_bm25(chunk_id, ?) as score from chunk where {where})
                        where score is not null order by score desc limit {CANDIDATES}""",
                    [question, *params],
                ).fetchall()
            ]

        fused: dict[str, float] = {}
        signals: dict[str, set[str]] = {}
        for signal, ranking in rankings.items():
            for rank, chunk_id in enumerate(ranking, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
                signals.setdefault(matter_of[chunk_id], set()).add(signal)

        by_matter: dict[str, list[str]] = {}
        for chunk_id in sorted(fused, key=fused.get, reverse=True):
            by_matter.setdefault(matter_of[chunk_id], []).append(chunk_id)

        # Matters named by file number lead, whatever the filters say: the reader asked for them.
        exact = self._named_matters(question)
        ordered = exact + [matter for matter in by_matter if matter not in exact]
        ordered = ordered[: max(top, len(exact))]
        chosen: dict[str, list[str]] = {}
        for matter in ordered:
            chunk_ids = by_matter.get(matter, [])[:per_matter]
            if not chunk_ids and matter in exact:  # named but not otherwise matched: its closest passages
                chunk_ids = [c for c, m, _ in self._similarity_within(matter, vector)][:per_matter]
            chosen[matter] = chunk_ids

        hits = self.cards(ordered)
        passages = self._passages([chunk_id for chunk_ids in chosen.values() for chunk_id in chunk_ids])
        for hit in hits:
            hit.passages = sorted(
                (passages[c] for c in chosen[hit.matter]), key=lambda p: (SECTION_ORDER.get(p.section, 9), p.chunk_index)
            )
            hit.score = max((fused.get(c, 0.0) for c in chosen[hit.matter]), default=0.0)
            hit.matched_by = (["file number"] if hit.matter in exact else []) + sorted(signals.get(hit.matter, ()))
        return hits

    def _named_matters(self, question: str) -> list[str]:
        named: list[str] = []
        for number in extract_file_numbers(question):
            if number in self._by_base:  # no revision suffix: the item and all its revisions
                matches = self._by_base[number]
            else:
                matches = [self._by_number[number]] if number in self._by_number else []
            named.extend(m for m in matches if m not in named)
        return named

    def _similarity_within(self, matter: str, vector: list[float]) -> list[tuple[str, str, float]]:
        return self.con.execute(
            f"""select chunk_id, matter, array_cosine_similarity(embedding, ?::FLOAT[{self.dim}]) as score
                from chunk where matter = ? order by score desc""",
            [vector, matter],
        ).fetchall()

    def _passages(self, chunk_ids: list[str]) -> dict[str, Passage]:
        rows = self.con.execute(
            "select chunk_id, section, chunk_index, text from chunk where chunk_id in (select unnest(?))", [chunk_ids]
        ).fetchall()
        return {row[0]: Passage(*row) for row in rows}

    def cards(self, matters: list[str]) -> list[Hit]:
        """The graph's view of each matter, in the order given."""
        if not matters:
            return []
        con, ids = self.con, [matters]
        hits = {
            row[0]: Hit(*row)
            for row in con.execute(
                """select id, file_number, title, matter_type, status, department, intro_date,
                          administrator_recommendation, site_url
                   from v_matter where id in (select unnest(?))""",
                ids,
            ).fetchall()
        }
        for matter, day, body, action, result, referred_to in con.execute(
            """select o.src, o.date, o.body_name, o.action, o.result, b.name
               from e_on_agenda o
               left join e_referred_to r on r.event_item_id = o.event_item_id and r.src = o.src and r.action is not null
               left join v_body b on b.id = r.dst
               where o.src in (select unnest(?)) order by o.date, o.event_item_id""",
            ids,
        ).fetchall():
            hits[matter].history.append(
                {"date": day, "body": body, "action": action, "result": result, "referred_to": referred_to}
            )
        for matter, body in con.execute(
            """select r.src, b.name from e_referred_to r join v_body b on b.id = r.dst
               where r.action is null and r.src in (select unnest(?)) order by r.date""",
            ids,
        ).fetchall():
            hits[matter].awaiting.append(body)
        for matter, file_number, kind, direction in con.execute(
            """select r.src, m.file_number, r.kind, 'later' from e_related_to r join v_matter m on m.id = r.dst
               where r.src in (select unnest(?))
               union all
               select r.dst, m.file_number, r.kind, 'earlier' from e_related_to r join v_matter m on m.id = r.src
               where r.dst in (select unnest(?))
               order by 2""",
            [matters, matters],
        ).fetchall():
            hits[matter].related.append({"file_number": file_number, "kind": kind, "direction": direction})
        for matter, label in con.execute(
            """select c.src, v.label from e_cites c join vertices v on v.id = c.dst
               where c.src in (select unnest(?)) order by v.label""",
            ids,
        ).fetchall():
            hits[matter].statutes.append(label)
        return [hits[matter] for matter in matters if matter in hits]
