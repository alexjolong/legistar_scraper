"""Command line entry point: ``uv run legistutor <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

from . import bronze, config
from .client import LegistarClient


LAW_PARTS = ("statutes", "changes", "bills", "ordinances")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="legistutor", description="Collect, search and question a local government legislative record.")
    parser.add_argument("--client", default="hennepinmn", help="Legistar tenant name (default: hennepinmn)")
    parser.add_argument(
        "--data-dir", type=Path, default=config.data_dir(), help="root of the data lake (default: LEGISTUTOR_DATA_DIR, else ./data)"
    )
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
    ask.add_argument("--model", help="chat model (Ollama) or serving endpoint (Databricks) to answer with (default: LEGISTUTOR_CHAT_MODEL)")

    law = sub.add_parser("ingest-law", help="fetch statutes, session laws, bills and county ordinances")
    law.add_argument("--chapters", nargs="*", default=[], metavar="CHAPTER", help="statute chapters to watch besides the ones the record cites, e.g. 13 471")
    law.add_argument("--since-year", type=int, default=2023, help="first session to read changes and bills for (default: 2023)")
    law.add_argument("--full", action="store_true", help="fetch every watched chapter again, whatever its age")
    law.add_argument("--skip", nargs="+", choices=LAW_PARTS, default=[], help="parts to leave out")

    summarize = sub.add_parser("summarize-law", help="have the chat model write summaries of the most-cited statute sections ahead of time")
    summarize.add_argument("--top", type=int, default=25, help="how many sections, most cited first (default: 25)")
    summarize.add_argument("--again", action="store_true", help="write again even where a current summary exists")

    refresh = sub.add_parser("refresh", help="ingest what changed (record and law), then rebuild the graphs and the search index")
    refresh.add_argument("--rps", type=float, default=5.0, help="max requests per second to Legistar (default: 5)")
    refresh.add_argument("--workers", type=int, default=4, help="concurrent fetch threads (default: 4)")
    refresh.add_argument("--no-law", action="store_true", help="leave statutes, session laws, bills and ordinances as they are")
    refresh.add_argument("--no-themes", action="store_true", help="do not have the chat model file new matters under themes and storylines")

    themes = sub.add_parser("themes", help="have the chat model file each matter under themes and a storyline, with a one-line gist")
    themes.add_argument("--full", action="store_true", help="file every matter again, not only new or changed ones")
    themes.add_argument("--limit", type=int, help="file at most N matters this run; the rest wait for the next")
    themes.add_argument("--workers", type=int, help="requests to the model at once (default: 1 locally, 4 on Databricks, or LEGISTUTOR_TAG_WORKERS)")

    evaluate = sub.add_parser("eval", help="run the evaluation questions through search (and answers) and score them")
    evaluate.add_argument("--questions", type=Path, default=Path("eval/questions.csv"), help="the questions file (default: eval/questions.csv)")
    evaluate.add_argument("--top", type=int, default=8, help="how many matters to retrieve per question (default: 8)")
    evaluate.add_argument("--answers", action="store_true", help="also write an answer to each question and have a model check it against answer_facts")
    evaluate.add_argument("--judge", help="chat model or endpoint that checks the answers (default: the one that writes them)")
    evaluate.add_argument("--out", type=Path, default=Path("eval/results"), help="where each run is saved (default: eval/results)")
    evaluate.add_argument("--compare", type=Path, help="an earlier run's JSON file, to list what changed since")

    check = sub.add_parser("check", help="try the models, the data directory and the outside sites, and say what is missing")
    check.add_argument("--no-sites", action="store_true", help="skip the outside sites the pipeline reads")

    # A Databricks App is given its port, and must listen on every interface behind the platform's proxy.
    app_port = os.environ.get("DATABRICKS_APP_PORT")
    serve = sub.add_parser("serve", help="run the web page: ask, search, and explore the graph")
    serve.add_argument("--port", type=int, default=int(app_port or 8765), help="port to listen on (default: 8765, or DATABRICKS_APP_PORT)")
    serve.add_argument(
        "--host",
        default="0.0.0.0" if app_port else "127.0.0.1",  # noqa: S104
        help="address to listen on (default: 127.0.0.1, this machine only; 0.0.0.0 in a Databricks App)",
    )
    serve.add_argument("--no-browser", action="store_true", help="do not open the page in a browser")
    serve.add_argument(
        "--reload-seconds",
        type=float,
        default=float(os.environ.get("LEGISTUTOR_RELOAD_SECONDS") or 120),
        help="how often to look for refreshed data and load it (default: 120, or LEGISTUTOR_RELOAD_SECONDS; 0 never looks)",
    )
    return parser


def _searcher(args: argparse.Namespace):
    from . import search
    from .embed import embedder

    return search.Searcher(args.data_dir, args.client, embedder(config.settings()))


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
    from . import llm, rag

    hits = _retrieve(args)
    chat = llm.chat_model(config.settings(), args.model)
    prompt = rag.build_prompt(args.question, hits, chat, client=args.client)
    logging.getLogger(__name__).debug("prompt:\n%s\n\n%s", prompt.system, prompt.user)
    failure = None
    answer: list[str] = []
    try:
        for text in chat.stream(prompt.system, prompt.user):
            answer.append(text)
            print(text, end="", flush=True)
    except llm.LLMError as exc:
        failure = exc
    print("\n")
    unsupported = rag.unsupported_claims("".join(answer), prompt)
    if unsupported:
        print("Check against the sources; the answer contains identifiers that do not appear in them:")
        for problem in unsupported:
            print(f"  - {problem}")
        print()
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


def _utf8_stdout() -> None:
    """Titles in the record are not all ASCII, and a Windows console's default encoding is. A
    notebook's output stream is already UTF-8 and has no ``reconfigure``."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,  # a notebook has already set up logging; ours should still apply
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "ingest":
        return _ingest(args)
    if args.command == "status":
        store = bronze.BronzeStore(args.data_dir, args.client)
        print(json.dumps(bronze.status(store), indent=2))
        return 0
    if args.command == "build":
        print(json.dumps(_build(args), indent=2))
        return 0
    if args.command == "ingest-law":
        return _ingest_law(args)
    if args.command == "sql":
        from . import query

        _utf8_stdout()
        query.connect(args.data_dir, args.client).sql(args.query).show(max_rows=200, max_width=200)
        return 0
    if args.command == "graph-demo":
        from . import graph

        _utf8_stdout()
        graph.demo(args.data_dir, args.client)
        return 0
    if args.command == "check":
        from . import check

        _utf8_stdout()
        if config.settings().provider == "ollama":
            try:
                _check_model_host(args)
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
        return 1 if check.run(args.data_dir, args.client, sites=not args.no_sites) else 0
    if args.command in ("index", "search", "ask", "refresh", "serve", "summarize-law", "eval", "themes"):
        from .embed import EmbeddingError
        from .llm import LLMError

        _utf8_stdout()
        try:
            # Before anything slow: a refresh should not fetch for minutes and then find no model host.
            _check_model_host(args)
            if args.command == "index":
                print(json.dumps(_index(args, full=args.full), indent=2))
                return 0
            if args.command == "refresh":
                return _refresh(args)
            if args.command == "summarize-law":
                return _summarize_law(args)
            if args.command == "eval":
                return _eval(args)
            if args.command == "themes":
                print(_themes_line(_themes(args, full=args.full, limit=args.limit, workers=args.workers)))
                return 0
            if args.command == "serve":
                from . import web

                in_app = bool(os.environ.get("DATABRICKS_APP_PORT"))
                web.serve(args.data_dir, args.client, port=args.port, host=args.host, open_browser=not (args.no_browser or in_app), reload_seconds=args.reload_seconds)
                return 0
            return _search(args) if args.command == "search" else _ask(args)
        except (EmbeddingError, LLMError, FileNotFoundError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 2


def _check_model_host(args: argparse.Namespace) -> None:
    """Locally, start Ollama if it is not running and check it has the models this command uses."""
    from . import ollama

    settings = config.settings()
    if settings.provider != "ollama":
        return
    models = [] if args.command == "themes" else [settings.embed_model]
    if args.command == "themes" or (args.command == "refresh" and not args.no_themes):
        models.append(settings.chat_model)
    if args.command in ("ask", "summarize-law") or getattr(args, "answers", False):
        models.append(getattr(args, "model", None) or settings.chat_model)
        if getattr(args, "judge", None):
            models.append(args.judge)
    ollama.ensure_running(settings.ollama_host, models)


def _build(args: argparse.Namespace) -> dict:
    """Rebuild every silver graph there is bronze for."""
    from . import law, ordinances, silver

    report = {"record": silver.build(args.data_dir, args.client)}
    if law.fetched_chapters(args.data_dir):
        report["law"] = law.build(args.data_dir)
    if ordinances.store(args.data_dir, args.client).keys("pages"):
        report["ordinances"] = ordinances.build(args.data_dir, args.client)
    return report


def _watched_chapters(args: argparse.Namespace) -> list[str]:
    """Chapters the record cites, chapters fetched before, and any named on the command line."""
    from . import law, query

    chapters = set(args.chapters) | set(law.fetched_chapters(args.data_dir))
    try:
        con = query.connect(args.data_dir, args.client)
        chapters |= {row[0] for row in con.sql("select distinct chapter from v_statute where chapter is not null").fetchall()}
    except FileNotFoundError:
        print("No graph yet, so no cited chapters to add. Run `legistutor build` first.", file=sys.stderr)
    return sorted(chapters, key=law.chapter_sort_key)


def _ingest_law(args: argparse.Namespace) -> int:
    from datetime import date

    from . import law, openstates, ordinances, revisor
    from .fetch import Fetcher

    started = time.monotonic()
    years = list(range(args.since_year, date.today().year + 1))
    pages = revisor.store(args.data_dir)
    failed = False
    # Ordinances first: the statutes they cite join the chapters to watch.
    if "ordinances" not in args.skip and args.client in ordinances.SITES:
        with Fetcher() as http:
            report = ordinances.ingest(args.data_dir, args.client, http, full=args.full)
        failed |= bool(report["failed"])
        print(f"ordinances: {report}")
        ordinances.build(args.data_dir, args.client)
    with revisor.fetcher() as http:
        if "statutes" not in args.skip:
            chapters = _watched_chapters(args)
            report = revisor.ingest_chapters(pages, http, chapters, full=args.full)
            failed |= bool(report["failed"])
            print(f"statutes: {report}")
        if "changes" not in args.skip:
            print(f"changes: {revisor.ingest_table2(pages, http, years, refresh_from=years[-1])}")
            print(f"session laws: {revisor.ingest_session_laws(pages, http, years, refresh_from=years[-1])}")
            report = revisor.ingest_laws(pages, http, law.laws_to_fetch(args.data_dir))
            failed |= bool(report["failed"])
            print(f"session law texts: {report}")
        requests = http.request_count
    print(f"{requests} requests to the Revisor's site in {time.monotonic() - started:.0f}s -> {pages.root}")
    if "bills" not in args.skip:
        try:
            print(f"bills: {openstates.ingest(args.data_dir, config.bills_api_key(), since_year=args.since_year)}")
        except openstates.MissingKey as exc:
            print(f"bills: skipped. {exc}")
    return 1 if failed else 0


def _summarize_law(args: argparse.Namespace) -> int:
    """Summaries are otherwise written the first time someone asks for one in the page."""
    from . import web

    app = web.application(args.data_dir, args.client)
    if not app.has_law:
        print("No statutes have been fetched. Run `legistutor ingest-law` and `legistutor build` first.", file=sys.stderr)
        return 1
    written = kept = failed = 0
    for node, label, matters in app.most_cited_sections(args.top):
        events = list(app.law_ask({"id": node, "fresh": "1" if args.again else ""}))
        name, data = events[-1]
        if name == "error":
            failed += 1
            print(f"{label}: {data['message']}")
        elif data["kept"]:
            kept += 1
        else:
            written += 1
            flags = f"; check: {'; '.join(data['unsupported'])}" if data["unsupported"] else ""
            print(f"{label} (cited by {matters}): written in {data['seconds']} s{flags}")
    print(f"{written} written, {kept} already current, {failed} failed -> {web.notes_dir(args.data_dir, args.client)}")
    return 1 if failed else 0


def _eval(args: argparse.Namespace) -> int:
    from . import evaluate, llm, web

    try:
        questions = evaluate.read_questions(args.questions)
    except evaluate.QuestionsError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not questions:
        print(f"No questions in {args.questions} yet. docs/evaluation-set.md says how to write them.")
        return 0
    app = web.application(args.data_dir, args.client)
    for problem in evaluate.unknown_items(app, questions):
        print(f"warning: {problem}", file=sys.stderr)
    settings = config.settings()
    judge = llm.chat_model(settings, args.judge) if args.answers and args.judge else None
    results = evaluate.run(app, questions, top=args.top, answers=args.answers, judge=judge, progress=print)
    print()
    print("\n".join(evaluate.summary(results)))
    if args.compare:
        print()
        print("\n".join(evaluate.compare(results, args.compare)))
    facts = {
        "provider": settings.provider,
        "embed_model": settings.embed_model,
        "chat_model": settings.chat_model if args.answers else None,
        "judge_model": (args.judge or settings.chat_model) if args.answers else None,
        "top": args.top,
        "questions": len(questions),
    }
    print(f"\nSaved to {evaluate.save(results, args.out, facts)}")
    return 0


def _index(args: argparse.Namespace, *, full: bool = False) -> dict:
    from . import gold
    from .embed import embedder

    return gold.build(args.data_dir, args.client, embedder(config.settings()), full=full)


def _themes(args: argparse.Namespace, *, full: bool = False, limit: int | None = None, workers: int | None = None) -> dict:
    from . import llm, themes

    settings = config.settings()
    return themes.tag(
        args.data_dir, args.client, llm.chat_model(settings), full=full, limit=limit, workers=themes.worker_count(settings.provider, workers)
    )


def _themes_line(report: dict) -> str:
    line = (
        f"themes: {report['matters']} matters filed ({report['filed']} newly, {report['reused']} unchanged) by {report['model']}; "
        f"{report['storylines']} storylines over {report['in_a_storyline']} matters"
    )
    if report["failed"]:
        line += f"; {report['failed']} could not be filed and will be tried again next time"
    if report["left_for_later"]:
        line += f"; {report['left_for_later']} left for the next run"
    return line


def _refresh(args: argparse.Namespace) -> int:
    """Everything a scheduled run needs: fetch what changed, rebuild what depends on it."""
    from . import silver

    args.stages, args.full, args.limit, args.recent_days = ["all"], False, None, 120
    ingest_status = _ingest(args)  # a failed fetch leaves the older copy on disk, so the rebuild is still sound
    if not args.no_law:
        # The chapters to watch come from the record's citations, so its graph is built first.
        silver.build(args.data_dir, args.client)
        args.chapters, args.since_year, args.skip = [], 2023, []
        try:
            ingest_status |= _ingest_law(args)
        except Exception as exc:  # noqa: BLE001 - the record's refresh must not depend on other sites being up
            logging.getLogger(__name__).exception("law sources")
            print(f"law: not refreshed ({exc}); what was fetched before is kept", file=sys.stderr)
            ingest_status = 1
    for name, graph in _build(args).items():
        print(f"{name} graph: {sum(graph['vertices'].values())} vertices, {sum(graph['edges'].values())} edges")
    index = _index(args)
    print(f"index: {index['passages']} passages, {index['embedded']} newly embedded, {index['reused']} unchanged")
    if "law" in index:
        law = index["law"]
        print(f"law index: {law['passages']} passages, {law['embedded']} newly embedded, {law['reused']} unchanged")
    if not args.no_themes:
        from .llm import LLMError

        try:
            print(_themes_line(_themes(args)))
        except LLMError as exc:  # the record and its index are refreshed either way
            print(f"themes: stopped ({exc}); what was filed is kept, and the rest is tried on the next refresh", file=sys.stderr)
            ingest_status = 1
    return ingest_status


if __name__ == "__main__":
    sys.exit(main())
