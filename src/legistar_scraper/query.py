"""Query the silver layer with DuckDB.

Every Parquet file becomes a view: ``v_<type>`` for vertices, ``e_<type>`` for
edges, ``t_<name>`` for detail tables, plus ``vertices`` and ``edges`` for the
whole graph. Nothing is copied; DuckDB reads the Parquet files in place, which
makes this fast enough to sit behind an interactive question.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from .silver import silver_root

_PREFIXES = {"vertices": "v_", "edges": "e_", "tables": "t_", "graph": ""}


def connect(data_dir: Path, client: str) -> duckdb.DuckDBPyConnection:
    root = silver_root(data_dir, client)
    if not (root / "graph" / "vertices.parquet").exists():
        raise FileNotFoundError(f"No silver layer at {root}. Run `legistar build` first.")
    con = duckdb.connect()
    for group, prefix in _PREFIXES.items():
        for path in sorted((root / group).glob("*.parquet")):
            source = path.resolve().as_posix().replace("'", "''")
            con.execute(f'CREATE VIEW "{prefix}{path.stem}" AS SELECT * FROM read_parquet(\'{source}\')')
    return con
