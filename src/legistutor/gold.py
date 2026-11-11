"""Gold layer: the search index, as one Parquet table of text passages with embeddings.

``data/gold/legistar/<client>/chunks.parquet`` has one row per passage. It stands in
for a vector database: at this size (thousands of rows) DuckDB scans it in
milliseconds, and the same table is what a Delta Sync index in Databricks Vector
Search would be built on (``chunk_id`` as the key, ``embedding`` as the vector,
the remaining columns as filters).
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
    started = time.monotonic()
    rows = build_chunks(data_dir, client)
    if not rows:
        raise RuntimeError("No text to index. Run `legistar build` first.")
    path = chunks_path(data_dir, client)
    vectors = {} if full else _existing_vectors(path, embedder.model)
    reused = sum(1 for row in rows if row["text_hash"] in vectors)

    pending = list({row["text_hash"]: row["embed_text"] for row in rows if row["text_hash"] not in vectors}.items())
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        for (text_hash, _), vector in zip(batch, embedder.embed_documents([text for _, text in batch])):
            vectors[text_hash] = vector
        done = min(start + batch_size, len(pending))
        if done == len(pending) or (start // batch_size) % 10 == 9:
            log.info("embedded %d of %d passages", done, len(pending))

    dim = len(next(iter(vectors.values())))
    for row in rows:
        row["embedding"] = vectors[row["text_hash"]]
        row["embedding_model"] = embedder.model
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=_schema(dim)), temporary)
    os.replace(temporary, path)
    return {
        "path": str(path),
        "passages": len(rows),
        "matters": len({row["matter"] for row in rows}),
        "by_section": {s: sum(1 for r in rows if r["section"] == s) for s in (*BODY_SECTIONS, "title")},
        "embedded": len(pending),
        "reused": reused,
        "model": embedder.model,
        "dimensions": dim,
        "seconds": round(time.monotonic() - started, 1),
    }
