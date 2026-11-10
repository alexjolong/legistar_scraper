"""Command line entry point: ``uv run legistar <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
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
    return parser


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
    return 2


if __name__ == "__main__":
    sys.exit(main())
