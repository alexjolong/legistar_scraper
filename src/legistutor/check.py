"""``legistutor check``: is everything this installation depends on in place?

Meant to be the first thing run in a new home (a Databricks cluster or App, a new
laptop). Each line is one dependency, tried for real with the smallest request that
proves it: the embedding model, the chat model, the data directory, the outside sites
the pipeline reads, and DuckDB's keyword search extension.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx

from . import config, gold
from .client import BASE_URL as LEGISTAR
from .client import USER_AGENT

SITES = {
    "Legistar API": LEGISTAR + "/{client}/bodies?$top=1",
    "Minnesota Revisor": "https://www.revisor.mn.gov/robots.txt",
    "Open States API": "https://v3.openstates.org/",
    "DuckDB extensions": "https://extensions.duckdb.org/",
}


def _embedding(settings: config.Settings) -> str:
    from .embed import embedder

    vector = embedder(settings).embed_query("county board resolution")
    return f"{settings.embed_model}: {len(vector)} dimensions"


def _chat(settings: config.Settings) -> str:
    from .llm import chat_model

    reply = "".join(chat_model(settings).stream("You are being tested.", "Reply with the single word: ready")).strip()
    if not reply:
        raise RuntimeError(f"{settings.chat_model} returned no text")
    return f"{settings.chat_model}: replied {reply[:40]!r}"


def _data_dir(data_dir: Path) -> str:
    """Written to the way the pipeline writes: a temporary file renamed into place."""
    from . import databricks

    if databricks.is_volume_path(data_dir) and not data_dir.exists():
        # A Databricks App: the volume is reached through the Files API, and only read at start.
        entries = [entry.name for entry in databricks._volume_files().list_directory_contents(data_dir.as_posix())]
        return f"{data_dir.as_posix()} (through the Files API): contains {', '.join(sorted(entries)) or 'nothing yet'}"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / f".check-{uuid.uuid4().hex}"
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.write_text("ok", encoding="utf-8")
        os.replace(temporary, target)
        if target.read_text(encoding="utf-8") != "ok":
            raise RuntimeError("a file read back differently from how it was written")
    finally:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
    return f"{data_dir.resolve().as_posix()}: writable"


def _index(data_dir: Path, client: str, settings: config.Settings) -> str:
    import pyarrow.parquet as pq

    path = gold.chunks_path(data_dir, client)
    if not path.exists():
        return f"none yet at {path.as_posix()} (run `legistutor refresh`)"
    models = set(pq.read_table(path, columns=["embedding_model"]).column("embedding_model").to_pylist())
    if models != {settings.embed_model}:
        raise RuntimeError(f"built with {sorted(models)}, not {settings.embed_model!r}; run `legistutor index`")
    return f"{pq.read_metadata(path).num_rows} passages, embedded with {settings.embed_model}"


def _keyword_search() -> str:
    import duckdb

    con = duckdb.connect()
    con.execute("install fts")
    con.execute("load fts")
    return f"DuckDB {duckdb.__version__} loaded the fts extension"


def _site(url: str, transport: httpx.BaseTransport | None) -> str:
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}, transport=transport) as http:
        response = http.get(url)
    # Any answer from the site itself will do; a proxy's refusal will not.
    if response.status_code in (407, 502, 503, 504):
        raise RuntimeError(f"HTTP {response.status_code}")
    return f"reachable (HTTP {response.status_code})"


def run(data_dir: Path, client: str, *, sites: bool = True, transport: httpx.BaseTransport | None = None, out: Callable[[str], None] = print) -> int:
    """Try everything, print a line for each, and return how many failed."""
    settings = config.settings()
    data_dir = Path(data_dir)
    out(f"provider: {settings.provider}")
    checks: list[tuple[str, Callable[[], str]]] = [
        ("embedding model", lambda: _embedding(settings)),
        ("chat model", lambda: _chat(settings)),
        ("data directory", lambda: _data_dir(data_dir)),
        ("search index", lambda: _index(data_dir, client, settings)),
        ("keyword search", _keyword_search),
    ]
    if sites:
        checks += [(name, lambda url=url: _site(url.format(client=client), transport)) for name, url in SITES.items()]
    failed = 0
    for name, check in checks:
        started = time.monotonic()
        try:
            out(f"  ok    {name}: {check()} [{time.monotonic() - started:.1f}s]")
        except Exception as exc:  # noqa: BLE001 - every kind of failure is the thing being reported
            failed += 1
            out(f"  FAIL  {name}: {' '.join(str(exc).split()) or type(exc).__name__}")
    out("Everything is in place." if not failed else f"{failed} of {len(checks)} checks failed.")
    return failed
