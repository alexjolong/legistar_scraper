"""Gold layer: the search index, as Parquet tables of text passages with embeddings.

``data/gold/legistar/<client>/chunks.parquet`` has one row per passage of the
record. ``law_chunks.parquet`` beside it has the passages of the law: every
subdivision of the statute chapters that were fetched, and every section of the
county's ordinances. They are separate tables because they answer different
questions (what the board did; what the law says) and are ranked separately.

Each stands in for a vector database: at this size (tens of thousands of rows)
DuckDB scans it in milliseconds, and the same table is what a Delta Sync index in
Databricks Vector Search would be built on (``chunk_id`` as the key, ``embedding``
as the vector, the remaining columns as filters).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from . import query
from .embed import Embedder
from .text import chunk_text

log = logging.getLogger(__name__)

# The sections worth retrieving on. "title" rows are only made for matters with
# no other text (mostly correspondence), so every matter is findable.
BODY_SECTIONS = ("resolution", "background")
SECTION_LABELS = {"resolution": "Resolution", "background": "Background"}
# Approvals of earlier minutes carry no text beyond "Approval of minutes of ..."
# and crowd out real items in topical searches. They stay in the graph.
UNINDEXED_TYPES = frozenset({"Minutes"})
_MINUTES_TITLE = re.compile(r"(?i)\bminutes\b")  # the few filed under another type


def gold_root(data_dir: Path, client: str) -> Path:
    return data_dir / "gold" / "legistar" / client


def chunks_path(data_dir: Path, client: str) -> Path:
    return gold_root(data_dir, client) / "chunks.parquet"


def law_chunks_path(data_dir: Path, client: str) -> Path:
    return gold_root(data_dir, client) / "law_chunks.parquet"


def _law_schema(dim: int) -> pa.Schema:
    return pa.schema(
        [
            ("chunk_id", pa.string()),
            ("source", pa.string()),  # vertex id: a statute section, a subdivision, or an ordinance
            ("kind", pa.string()),  # statute, subdivision, ordinance
            ("parent", pa.string()),  # the section a subdivision belongs to; otherwise the source itself
            ("label", pa.string()),  # "Minn. Stat. § 383B.158, subd. 2"
            ("heading", pa.string()),  # headnote, or the ordinance section's heading
            ("chapter", pa.string()),
            ("chunk_index", pa.int64()),
            ("text", pa.string()),
            ("embed_text", pa.string()),
            ("text_hash", pa.string()),
            ("embedding_model", pa.string()),
            ("embedding", pa.list_(pa.float32(), dim)),
        ]
    )


def _schema(dim: int) -> pa.Schema:
    return pa.schema(
        [
            ("chunk_id", pa.string()),
            ("matter", pa.string()),  # vertex id in the silver graph
            ("file_number", pa.string()),
            ("section", pa.string()),
            ("chunk_index", pa.int64()),
            ("text", pa.string()),  # the passage as shown to readers and the LLM
            ("embed_text", pa.string()),  # the passage with its context header: what is embedded and keyword-indexed
            ("text_hash", pa.string()),
            ("matter_type", pa.string()),
            ("status", pa.string()),
            ("department", pa.string()),
            ("intro_date", pa.date32()),
            ("last_action_date", pa.date32()),
            ("embedding_model", pa.string()),
            ("embedding", pa.list_(pa.float32(), dim)),
        ]
    )


def _header(matter: dict[str, Any]) -> str:
    """Context every passage is embedded with: a passage from the middle of a
    background section rarely names the item it belongs to."""
    facts = " | ".join(v for v in (matter["file_number"], matter["matter_type"], matter["department"]) if v)
    return "\n".join(v for v in (facts, matter["title"]) if v)


def build_chunks(data_dir: Path, client: str, *, max_chars: int = 2400) -> list[dict[str, Any]]:
    """Passages for every matter, without embeddings."""
    con = query.connect(data_dir, client)
    columns = ("matter", "file_number", "title", "matter_type", "status", "department", "intro_date", "last_action_date")
    matters = [
        dict(zip(columns, row))
        for row in con.sql(
            """select m.id, m.file_number, m.title, m.matter_type, m.status, m.department, m.intro_date,
                      coalesce((select max(o.date) from e_on_agenda o where o.src = m.id), m.intro_date)
               from v_matter m order by m.matter_id"""
        ).fetchall()
    ]
    sections: dict[str, dict[str, str]] = {}
    for matter, section, text in con.sql("select matter, section, text from t_matter_text").fetchall():
        sections.setdefault(matter, {})[section] = text

    rows: list[dict[str, Any]] = []
    for matter in matters:
        if matter["matter_type"] in UNINDEXED_TYPES:
            continue
        header = _header(matter)
        passages = [
            (section, index, passage, f"{header}\n{SECTION_LABELS[section]}:\n{passage}")
            for section in BODY_SECTIONS
            for index, passage in enumerate(chunk_text(sections.get(matter["matter"], {}).get(section), max_chars))
        ]
        if not passages and matter["title"] and not _MINUTES_TITLE.search(matter["title"]):
            passages = [("title", 0, matter["title"], header)]
        for section, index, passage, embed_text in passages:
            rows.append(
                {
                    **{k: matter[k] for k in columns if k != "title"},
                    "chunk_id": f"{matter['matter']}#{section}#{index}",
                    "section": section,
                    "chunk_index": index,
                    "text": passage,
                    "embed_text": embed_text,
                    "text_hash": hashlib.sha1(embed_text.encode("utf-8")).hexdigest(),
                }
            )
    return rows


def build_law_chunks(data_dir: Path, client: str, *, max_chars: int = 2400) -> list[dict[str, Any]]:
    """Passages of statutes and ordinances, without embeddings. Empty until
    ``legistutor ingest-law`` has fetched some."""
    con = query.connect(data_dir, client)
    views = {row[0] for row in con.sql("select table_name from information_schema.tables").fetchall()}
    columns = {
        row[0] for row in con.sql("select column_name from information_schema.columns where table_name = 'v_statute'").fetchall()
    }
    sources: list[tuple[str, str, str, str, str | None, str | None, str, str]] = []
    if "text" in columns:  # the statutes have been fetched, not only cited
        titles = dict(con.sql("select number, headnote from v_statute where kind = 'chapter'").fetchall())

        def context(chapter: str, number: str, headnote: str | None) -> str:
            title = f" ({titles[chapter].title()})" if titles.get(chapter) else ""
            return f"Minnesota Statutes, chapter {chapter}{title}\n§ {number} {headnote or ''}".rstrip()

        for vid, number, chapter, headnote, text in con.sql(
            """select id, number, chapter, headnote, text from v_statute
               where kind = 'section' and status = 'in force' and text is not null and text <> '' order by id"""
        ).fetchall():
            sources.append((vid, "statute", vid, f"Minn. Stat. § {number}", headnote, chapter, context(chapter, number, headnote), text))
        for vid, section, subdivision, chapter, headnote, section_headnote, text in con.sql(
            """select d.id, d.section, d.subdivision, d.chapter, d.headnote, s.headnote, d.text
               from v_subdivision d left join v_statute s on s.number = d.section and s.kind = 'section'
               where d.status = 'in force' and d.text is not null and d.text <> '' order by d.id"""
        ).fetchall():
            header = f"{context(chapter, section, section_headnote)}\nSubdivision {subdivision}. {headnote or ''}".rstrip()
            label = f"Minn. Stat. § {section}, subd. {subdivision}"
            sources.append((vid, "subdivision", f"mnstat:{section}", label, headnote or section_headnote, chapter, header, text))
    if "t_ordinance_text" in views:
        for vid, number, title, heading, text in con.sql(
            """select o.id, o.number, o.title, t.heading, t.text from t_ordinance_text t join v_ordinance o on o.id = t.ordinance
               order by o.id, t.position"""
        ).fetchall():
            label = f"Ordinance {number}: {title}" if number else title
            header = f"County ordinance | {label}" + (f"\n{heading}" if heading else "")
            sources.append((vid, "ordinance", vid, label, heading, None, header, text))

    rows: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for source, kind, parent, label, heading, chapter, header, text in sources:
        for passage in chunk_text(text, max_chars):
            index = counters.get(source, 0)
            counters[source] = index + 1
            embed_text = f"{header}\n{passage}"
            rows.append(
                {
                    "chunk_id": f"{source}#{index}" if kind != "subdivision" else f"{source}/{index}",
                    "source": source,
                    "kind": kind,
                    "parent": parent,
                    "label": label,
                    "heading": heading,
                    "chapter": chapter,
                    "chunk_index": index,
                    "text": passage,
                    "embed_text": embed_text,
                    "text_hash": hashlib.sha1(embed_text.encode("utf-8")).hexdigest(),
                }
            )
    return rows


def _existing_vectors(path: Path, model: str) -> dict[str, list[float]]:
    """Embeddings from the previous build, by text hash, if made with the same model."""
    if not path.exists():
        return {}
    table = pq.read_table(path, columns=["text_hash", "embedding_model", "embedding"])
    return {
        row["text_hash"]: row["embedding"]
        for row in table.to_pylist()
        if row["embedding_model"] == model and row["embedding"] is not None
    }


def build(data_dir: Path, client: str, embedder: Embedder, *, full: bool = False, batch_size: int = 32) -> dict[str, Any]:
    """Rebuild the index. Passages whose text is unchanged keep their embedding,
    so after the first run only new or edited matters reach the model."""
    rows = build_chunks(data_dir, client)
    if not rows:
        raise RuntimeError("No text to index. Run `legistutor build` first.")
    report = _embed_and_write(rows, chunks_path(data_dir, client), _schema, embedder, full=full, batch_size=batch_size)
    report["matters"] = len({row["matter"] for row in rows})
    report["by_section"] = {s: sum(1 for r in rows if r["section"] == s) for s in (*BODY_SECTIONS, "title")}
    law_rows = build_law_chunks(data_dir, client)
    if law_rows:
        law = _embed_and_write(law_rows, law_chunks_path(data_dir, client), _law_schema, embedder, full=full, batch_size=batch_size)
        law["by_kind"] = {k: sum(1 for r in law_rows if r["kind"] == k) for k in ("statute", "subdivision", "ordinance")}
        report["law"] = law
    return report


def _embed_and_write(
    rows: list[dict[str, Any]], path: Path, schema, embedder: Embedder, *, full: bool, batch_size: int
) -> dict[str, Any]:
    started = time.monotonic()
    vectors = {} if full else _existing_vectors(path, embedder.model)
    reused = sum(1 for row in rows if row["text_hash"] in vectors)

    pending = list({row["text_hash"]: row["embed_text"] for row in rows if row["text_hash"] not in vectors}.items())
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        for (text_hash, _), vector in zip(batch, embedder.embed_documents([text for _, text in batch])):
            vectors[text_hash] = vector
        done = min(start + batch_size, len(pending))
        if done == len(pending) or (start // batch_size) % 10 == 9:
            log.info("%s: embedded %d of %d passages", path.stem, done, len(pending))

    dim = len(next(iter(vectors.values())))
    for row in rows:
        row["embedding"] = vectors[row["text_hash"]]
        row["embedding_model"] = embedder.model
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=schema(dim)), temporary)
    os.replace(temporary, path)
    return {
        "path": str(path),
        "passages": len(rows),
        "embedded": len(pending),
        "reused": reused,
        "model": embedder.model,
        "dimensions": dim,
        "seconds": round(time.monotonic() - started, 1),
    }
