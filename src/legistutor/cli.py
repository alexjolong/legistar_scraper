"""Command line entry point: ``uv run legistar <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

from . import bronze
from .client import LegistarClient


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="legistar", description="Ingest Legistar legislative data.")
    parser.add_argument("--client", default="hennepinmn", help="Legistar tenant name (default: hennepinmn)")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="root of the data lake (default: ./data)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="land raw API responses in the bronze layer")
    ingest.add_argument(
        "--stages",
        nargs="+",
        choices=[*bronze.STAGES, "all"],
        default=list(bronze.DEFAULT_STAGES),
        help="which stages to run (default: reference events matters; votes is opt-in)",
    )
    ingest.add_argument("--full", action="store_true", help="re-fetch everything, ignoring what is on disk")
    ingest.add_argument("--limit", type=int, help="fetch at most N entities per stage (for smoke tests)")
    ingest.add_argument("--rps", type=float, default=5.0, help="max requests per second (default: 5)")
    ingest.add_argument("--workers", type=int, default=4, help="concurrent fetch threads (default: 4)")
    ingest.add_argument(
        "--recent-days",
        type=int,
        default=120,
        help="always re-fetch meetings newer than this many days (default: 120)",
    )

    sub.add_parser("status", help="summarise what is in the bronze layer")
    sub.add_parser("build", help="rebuild the silver graph tables (Parquet) from bronze")
    sql = sub.add_parser("sql", help="run a SQL query against the silver layer with DuckDB")
    sql.add_argument("query", help="SQL text; views are v_<vertex>, e_<edge>, t_<table>, vertices, edges")
    sub.add_parser("graph-demo", help="load the graph into GraphFrames and run sample analytics (needs the spark extra)")

    index = sub.add_parser("index", help="rebuild the search index (text passages and embeddings) from silver")
    index.add_argument("--full", action="store_true", help="re-embed every passage instead of only new or changed ones")

    def retrieval_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("question", help="a question or topic, in plain language; file numbers are recognised")
        command.add_argument("--top", type=int, default=8, help="how many matters to retrieve (default: 8)")
        command.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD", help="only matters acted on since this date")
        command.add_argument("--department", help="only matters whose requesting department contains this text")
        command.add_argument("--body", help="only matters that appeared before a body whose name contains this text")
        command.add_argument("--type", dest="matter_type", help="only this matter type, e.g. Resolution")
        command.add_argument("--status", help="only this status, e.g. Referred")

    retrieval_options(sub.add_parser("search", help="find matters by meaning, keywords and file number"))
    ask = sub.add_parser("ask", help="answer a question from the record, with citations")
    retrieval_options(ask)
    ask.add_argument("--provider", choices=["anthropic", "ollama"], help="which LLM answers (default: LEGISTAR_LLM_PROVIDER, else anthropic)")
    ask.add_argument("--model", help="model name for that provider (default: claude-sonnet-5-5, or gemma3:12b for ollama)")
    return parser


def _searcher(args: argparse.Namespace):
    from . import config, search
    from .embed import OllamaEmbedder

    settings = config.settings()
    return search.Searcher(args.data_dir, args.client, OllamaEmbedder(settings.embed_model, settings.ollama_host))


def _retrieve(args: argparse.Namespace):
    from .search import Filters

    filters = Filters(args.since, args.department, args.body, args.matter_type, args.status)
    return _searcher(args).search(args.question, top=args.top, filters=filters)


def _source_line(number: int, hit) -> str:
    last = hit.history[-1] if hit.history else None
    where = f"; last: {last['date']} {last['body']}, {last['action'] or 'on agenda'}" if last else ""
    queued = f"; queued for {', '.join(hit.awaiting)}" if hit.awaiting else ""
    return (
        f"[{number}] {hit.file_number} ({hit.status or 'no status'}{where}{queued})\n"
        f"    {' '.join((hit.title or '').split())}\n    {hit.url}"
    )


def _search(args: argparse.Namespace) -> int:
    hits = _retrieve(args)
    if not hits:
        print("No matching matters.")
    for number, hit in enumerate(hits, start=1):
        print(_source_line(number, hit))
        facts = [hit.matter_type, hit.department, "matched by " + ", ".join(hit.matched_by) if hit.matched_by else None]
        print("    " + " | ".join(f for f in facts if f))
        if hit.passages and hit.passages[0].section != "title":
            excerpt = " ".join(hit.passages[0].text.split())
            print(f"    {hit.passages[0].section}: {excerpt[:240]}{'...' if len(excerpt) > 240 else ''}")
        print()
    return 0


def _ask(args: argparse.Namespace) -> int:
    from . import config, llm, rag

    hits = _retrieve(args)
    chat = llm.provider(config.settings(), args.provider, args.model)
    prompt = rag.build_prompt(args.question, hits, chat, client=args.client)
    logging.getLogger(__name__).debug("prompt:\n%s\n\n%s", prompt.system, prompt.user)
    failure = None
    try:
        for text in chat.stream(prompt.system, prompt.user):
            print(text, end="", flush=True)
    except llm.LLMError as exc:
        failure = exc
    print("\n")
    if prompt.sources:
        print("Sources")
        for number, hit in enumerate(prompt.sources, start=1):
            print(_source_line(number, hit))
    if prompt.dropped:
        print(f"\n{prompt.dropped} lower-ranked sources were left out to fit the model's context window.")
    if failure:
        print(f"\nerror ({chat.model}, {chat.name}): {failure}", file=sys.stderr)
        return 1
    print(f"\nAnswered by {chat.model} ({chat.name}).")
    return 0


def _ingest(args: argparse.Namespace) -> int:
    stages = bronze.STAGES if "all" in args.stages else [s for s in bronze.STAGES if s in args.stages]
    store = bronze.BronzeStore(args.data_dir, args.client)
    started = time.monotonic()
    results = []
    with LegistarClient(args.client, requests_per_second=args.rps) as api:
        for stage in stages:
            if stage == "reference":
                results.append(bronze.ingest_reference(api, store))
            elif stage == "events":
                results.append(
                    bronze.ingest_events(
                        api,
                        store,
                        full=args.full,
                        recent_days=args.recent_days,
                        limit=args.limit,
                        workers=args.workers,
                    )
                )
            elif stage == "matters":
                results.append(
                    bronze.ingest_matters(api, store, full=args.full, limit=args.limit, workers=args.workers)
                )
            elif stage == "votes":
                results.append(bronze.ingest_votes(api, store, full=args.full, limit=args.limit, workers=args.workers))
        requests = api.request_count
    for result in results:
        print(result)
    print(f"{requests} requests in {time.monotonic() - started:.0f}s -> {store.root}")
    return 1 if any(r.failed for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "ingest":
        return _ingest(args)
    if args.command == "status":
        store = bronze.BronzeStore(args.data_dir, args.client)
        print(json.dumps(bronze.status(store), indent=2))
        return 0
    if args.command == "build":
        from . import silver

        print(json.dumps(silver.build(args.data_dir, args.client), indent=2))
        return 0
    if args.command == "sql":
        from . import query

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        query.connect(args.data_dir, args.client).sql(args.query).show(max_rows=200, max_width=200)
        return 0
    if args.command == "graph-demo":
        from . import graph

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        graph.demo(args.data_dir, args.client)
        return 0
    if args.command in ("index", "search", "ask"):
        from .embed import EmbeddingError
        from .llm import LLMError

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        try:
            if args.command == "index":
                from . import config, gold
                from .embed import OllamaEmbedder

                settings = config.settings()
                embedder = OllamaEmbedder(settings.embed_model, settings.ollama_host)
                print(json.dumps(gold.build(args.data_dir, args.client, embedder, full=args.full), indent=2))
                return 0
            return _search(args) if args.command == "search" else _ask(args)
        except (EmbeddingError, LLMError, FileNotFoundError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
