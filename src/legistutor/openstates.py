"""State bills from the Open States API (now run by Plural Policy), v3.

What it offers for Minnesota, checked 2026-11-11: every bill of a session with its
title, subjects, sponsors, actions, companion bill and links to the Revisor's text;
and one bulk CSV archive per session, which is how a whole session is loaded without
thousands of API calls. What it does not offer: statute text, session laws, or which
statutes a bill would change. Those come from the Revisor's site.

Bronze layout under ``<data_dir>/bronze/openstates/<state>/``::

    sessions.json            the state's sessions and their bulk downloads
    bulk/<session>.zip       the session's CSV archive, as downloaded
    bills/<session>/<n>.json bills changed since that archive, from the API

The API needs a key (``PLURAL_POLICY_API_KEY``). It is sent only to
``v3.openstates.org``; the bulk archives are public files and are fetched without it.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import logging
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .fetch import Fetcher, PageStore

log = logging.getLogger(__name__)

API = "https://v3.openstates.org"
INCLUDES = ("sponsorships", "actions", "related_bills", "sources", "versions")
CHAMBERS = {"lower": "House", "upper": "Senate"}
API_PAGE_SIZE = 20  # the most the API returns when child records are included


class MissingKey(RuntimeError):
    pass


def store(data_dir: Path, state: str = "mn") -> PageStore:
    return PageStore(data_dir, f"openstates/{state}")


def bill_id(state: str, session: str, identifier: str) -> str:
    """``mnbill:2025-2026:HF4582``"""
    return f"{state}bill:{session}:{identifier.replace(' ', '')}"


def sessions_for(year: int, special: int = 0) -> list[str]:
    """Open States session names a law of ``year`` can belong to: Minnesota's regular
    session spans a biennium that starts in an odd year."""
    if special:
        return [f"{year}s{special}"]
    start = year if year % 2 else year - 1
    return [f"{start}-{start + 1}"]


def _session_start_year(identifier: str) -> int:
    return int(identifier[:4]) if identifier[:4].isdigit() else 0


# --- ingest -------------------------------------------------------------------------------


def ingest(
    data_dir: Path,
    api_key: str | None,
    *,
    state: str = "mn",
    since_year: int = 2023,
    max_api_requests: int = 150,
    transport: Any = None,
) -> dict[str, Any]:
    """Fetch what is new: the session list, any bulk archive that was republished,
    and bills changed since the archive of each session still in progress."""
    if not api_key:
        raise MissingKey("Set PLURAL_POLICY_API_KEY (in the environment or in .env) to fetch bills.")
    pages = store(data_dir, state)
    report: dict[str, Any] = {"sessions": 0, "archives_fetched": 0, "bills_updated": 0, "api_requests": 0}
    # The API allows few requests a minute on the free tier; the archives are plain files.
    with (
        Fetcher(requests_per_second=1 / 7, headers={"X-API-KEY": api_key}, transport=transport) as api,
        Fetcher(requests_per_second=1.0, transport=transport) as files,
    ):
        response = api.get(f"{API}/jurisdictions/{state}", {"include": "legislative_sessions"})
        if response is None:
            raise RuntimeError(f"Open States has no jurisdiction {state!r}")
        pages.write("sessions.json", response.content, url=f"{API}/jurisdictions/{state}")
        today = date.today()
        for session in response.json().get("legislative_sessions", []):
            name = session["identifier"]
            if _session_start_year(name) < since_year:
                continue
            report["sessions"] += 1
            archive = next((d for d in session.get("downloads", []) if d.get("data_type") == "csv"), None)
            key = f"bulk/{name}.zip"
            if archive and pages.meta(key).get("updated_at") != archive["updated_at"]:
                downloaded = files.get(archive["url"])
                if downloaded is not None:
                    pages.write(key, downloaded.content, url=archive["url"], updated_at=archive["updated_at"])
                    report["archives_fetched"] += 1
                    log.info("bills: %s archive of %s (%.0f MB)", name, archive["updated_at"][:10], len(downloaded.content) / 1e6)
            # Bills move only while a session is open; give it two months after the end for signatures.
            start, end = session.get("start_date") or "", session.get("end_date") or ""
            in_progress = bool(start) and start <= today.isoformat() and (not end or end >= (today - timedelta(days=60)).isoformat())
            if not in_progress:
                continue
            since = (pages.meta(key).get("updated_at") or start)[:10]
            page = 1
            while api.request_count < max_api_requests:
                params = [
                    ("jurisdiction", state), ("session", name), ("updated_since", since), ("sort", "updated_asc"),
                    ("per_page", API_PAGE_SIZE), ("page", page), *(("include", item) for item in INCLUDES),
                ]  # fmt: skip
                listing = api.get(f"{API}/bills", params)
                if listing is None:
                    break
                body = listing.json()
                for bill in body.get("results", []):
                    number = bill["identifier"].replace(" ", "")
                    pages.write(f"bills/{name}/{number}.json", json.dumps(bill).encode(), updated_at=bill.get("updated_at"))
                    report["bills_updated"] += 1
                if page >= body.get("pagination", {}).get("max_page", 1):
                    break
                page += 1
            else:
                log.warning("bills: stopped at %d API requests; the rest will come on the next run", max_api_requests)
        report["api_requests"] = api.request_count
    return report


# --- reading ------------------------------------------------------------------------------


def _list(value: str | None) -> list[str]:
    """The archives write lists as Python literals: ``"['Energy', 'Taxes']"``."""
    if not value or not value.startswith("["):
        return []
    try:
        return [str(item) for item in ast.literal_eval(value)]
    except (ValueError, SyntaxError):
        return []


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _finish(state: str, bill: dict[str, Any]) -> dict[str, Any]:
    actions = sorted(bill["actions"], key=lambda a: (a["date"] or date.min, a["order"]))
    passages = [a["date"] for a in actions if "passage" in a["classification"] and a["date"]]
    number = bill["identifier"].replace(" ", "")
    return {
        "id": bill_id(state, bill["session"], bill["identifier"]),
        "session": bill["session"],
        "identifier": bill["identifier"],
        "title": bill["title"],
        "chamber": CHAMBERS.get(bill["chamber"], bill["chamber"]),
        "classification": ", ".join(bill["classification"]) or None,
        "subjects": "; ".join(bill["subjects"]) or None,
        "sponsors": ", ".join(bill["sponsors"]) or None,
        "first_action": actions[0]["date"] if actions else None,
        "last_action": actions[-1]["date"] if actions else None,
        "last_action_text": actions[-1]["description"] if actions else None,
        "passed": max(passages) if passages else None,
        "url": f"https://openstates.org/{state}/bills/{bill['session']}/{number}/",
        "source_url": bill["source_url"],
        "actions": [{"date": a["date"], "chamber": a["chamber"], "description": a["description"]} for a in actions],
        "companions": bill["companions"],
    }


def _from_archive(content: bytes) -> Iterator[dict[str, Any]]:
    tables: dict[str, list[dict[str, str]]] = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for member in archive.namelist():
            # longest name first: "..._bill_related_bills.csv" also ends in "_bills.csv"
            for table in ("bill_related_bills", "bill_sponsorships", "bill_actions", "bill_sources", "organizations", "bills"):
                if member.endswith(f"_{table}.csv"):
                    with archive.open(member) as handle:
                        tables[table] = list(csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline="")))
                    break
    chambers = {row["id"]: row.get("classification") for row in tables.get("organizations", [])}
    children: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for table in ("bill_actions", "bill_related_bills", "bill_sources", "bill_sponsorships"):
        for row in tables.get(table, []):
            children[row["bill_id"]][table].append(row)
    for row in tables.get("bills", []):
        mine = children[row["id"]]
        yield {
            "session": row["session_identifier"],
            "identifier": row["identifier"],
            "title": row["title"],
            "chamber": row.get("organization_classification"),
            "classification": _list(row.get("classification")),
            "subjects": _list(row.get("subject")),
            "sponsors": [s["name"] for s in mine["bill_sponsorships"] if s.get("primary") == "True"],
            "source_url": next((s["url"] for s in mine["bill_sources"]), None),
            "actions": [
                {
                    "date": _day(a["date"]),
                    "order": int(a.get("order") or 0),
                    "chamber": CHAMBERS.get(chambers.get(a["organization_id"]), None),
                    "description": a["description"],
                    "classification": _list(a.get("classification")),
                }
                for a in mine["bill_actions"]
            ],
            "companions": [
                {"session": r["legislative_session"], "identifier": r["identifier"]}
                for r in mine["bill_related_bills"]
                if r.get("relation_type") == "companion"
            ],
        }


def _from_api(bill: dict[str, Any]) -> dict[str, Any]:
    return {
        "session": bill["session"],
        "identifier": bill["identifier"],
        "title": bill["title"],
        "chamber": (bill.get("from_organization") or {}).get("classification"),
        "classification": bill.get("classification") or [],
        "subjects": bill.get("subject") or [],
        "sponsors": [s["name"] for s in bill.get("sponsorships", []) if s.get("primary")],
        "source_url": next((s["url"] for s in bill.get("sources", [])), None),
        "actions": [
            {
                "date": _day(a.get("date")),
                "order": a.get("order") or 0,
                "chamber": CHAMBERS.get((a.get("organization") or {}).get("classification")),
                "description": a.get("description"),
                "classification": a.get("classification") or [],
            }
            for a in bill.get("actions", [])
        ],
        "companions": [
            {"session": r["legislative_session"], "identifier": r["identifier"]}
            for r in bill.get("related_bills", [])
            if r.get("relation_type") == "companion"
        ],
    }


def read_bills(data_dir: Path, state: str = "mn") -> list[dict[str, Any]]:
    """Every bill on disk in one shape, the API's newer copy winning over the archive's."""
    pages = store(data_dir, state)
    bills: dict[str, dict[str, Any]] = {}
    for key in pages.keys("bulk", suffix=".zip"):
        for bill in _from_archive(pages.read(key)):
            finished = _finish(state, bill)
            bills[finished["id"]] = finished
    for key in pages.keys("bills", suffix=".json"):
        finished = _finish(state, _from_api(json.loads(pages.read(key))))
        bills[finished["id"]] = finished
    return list(bills.values())
