"""Query the silver layer with DuckDB.

Every Parquet file becomes a view: ``v_<type>`` for vertices, ``e_<type>`` for
edges, ``t_<name>`` for detail tables, plus ``vertices`` and ``edges`` for the
whole graph. Nothing is copied; DuckDB reads the Parquet files in place, which
makes this fast enough to sit behind an interactive question.

There is one silver graph per source: the county's record (``silver/legistar/<client>``),
state law (``silver/mnlaw``) and the county's ordinances (``silver/ordinances/<client>``).
The views join them into one graph:

* tables with the same name are stacked, columns matched by name;
* where two sources describe the same vertex id, the source that owns it wins. The
  record writes a bare stub for each statute a matter cites; once the statutes have
  been fetched, ``v_statute`` shows the full row instead;
* a vertex that only the record knows keeps its stub, with the owner's columns null.
  For a statute that means "cited, but not in the statutes as fetched": a chapter not
  yet fetched, or a citation that is wrong.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from .silver import silver_root

_PREFIXES = {"vertices": "v_", "edges": "e_", "tables": "t_", "graph": ""}


def roots(data_dir: Path, client: str) -> list[Path]:
    """Silver roots that exist, owners of shared vertices first."""
    data_dir = Path(data_dir)
    candidates = [data_dir / "silver" / "mnlaw", data_dir / "silver" / "ordinances" / client, silver_root(data_dir, client)]
    return [root for root in candidates if (root / "graph" / "vertices.parquet").exists()]


def _quoted(path: Path) -> str:
    return "'" + path.resolve().as_posix().replace("'", "''") + "'"


def _view_sql(group: str, name: str, files: list[Path]) -> str:
    if len(files) == 1:
        return f"SELECT * FROM read_parquet({_quoted(files[0])})"
    stacked = " UNION ALL BY NAME ".join(
        f"SELECT *, {rank} AS _source_rank FROM read_parquet({_quoted(path)})" for rank, path in enumerate(files)
    )
    if group == "vertices" or name == "vertices":
        return (
            f"SELECT * EXCLUDE (_source_rank) FROM ({stacked}) "
            "QUALIFY row_number() OVER (PARTITION BY id ORDER BY _source_rank) = 1"
        )
    if group == "edges" or name == "edges":
        return f"SELECT DISTINCT * EXCLUDE (_source_rank) FROM ({stacked})"
    return f"SELECT * EXCLUDE (_source_rank) FROM ({stacked})"


def connect(data_dir: Path, client: str) -> duckdb.DuckDBPyConnection:
    if not (silver_root(data_dir, client) / "graph" / "vertices.parquet").exists():
        raise FileNotFoundError(f"No silver layer at {silver_root(data_dir, client)}. Run `legistutor build` first.")
    con = duckdb.connect()
    for group, prefix in _PREFIXES.items():
        files: dict[str, list[Path]] = {}
        for root in roots(data_dir, client):
            for path in sorted((root / group).glob("*.parquet")):
                files.setdefault(path.stem, []).append(path)
        for name, paths in sorted(files.items()):
            con.execute(f'CREATE VIEW "{prefix}{name}" AS {_view_sql(group, name, paths)}')
    return con
