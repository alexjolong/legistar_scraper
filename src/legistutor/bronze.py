"""Bronze layer: land raw Legistar API responses on disk, one JSON file per entity.

Layout under ``<data_dir>/bronze/legistar/<client>/``::

    reference/<name>.json     full snapshot of a lookup collection
    events/<EventId>.json     event + its agenda items (``EventItems``)
    matters/<MatterId>.json   matter + ``MatterTexts``, ``MatterHistories``,
                              ``MatterAttachments``, ``MatterRelations``
    votes/<EventId>.json      votes and roll calls for that meeting's items

Payloads are stored exactly as the API returns them (child collections are
nested under the parent, mirroring the API's own shape) plus a ``_meta`` block.
The files themselves are the incremental state: a run re-fetches only what is
missing or whose upstream row version has changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .client import LegistarClient

log = logging.getLogger(__name__)

# name on disk -> (API path, primary key used for stable paging)
REFERENCE_COLLECTIONS: dict[str, tuple[str, str | None]] = {
    "bodies": ("bodies", "BodyId"),
    "body_types": ("bodytypes", "BodyTypeId"),
    "persons": ("persons", "PersonId"),
    "office_records": ("officerecords", "OfficeRecordId"),
    "matter_types": ("mattertypes", "MatterTypeId"),
    "matter_statuses": ("matterstatuses", "MatterStatusId"),
    "actions": ("actions", "ActionId"),
    "vote_types": ("votetypes", "VoteTypeId"),
    "code_sections": ("codesections", None),
    "indexes": ("indexes", None),
}

STAGES = ("reference", "events", "matters", "votes")
DEFAULT_STAGES = ("reference", "events", "matters")
NOTE_PARAMS = {"AgendaNote": 1, "MinutesNote": 1}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class BronzeStore:
    """File-per-entity JSON store with atomic writes."""

    def __init__(self, data_dir: Path, client: str) -> None:
        self.root = Path(data_dir) / "bronze" / "legistar" / client
        self.client = client

    def path(self, kind: str, key: int | str) -> Path:
        return self.root / kind / f"{key}.json"

    def read(self, kind: str, key: int | str) -> dict[str, Any] | None:
        try:
            with self.path(kind, key).open(encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def write(self, kind: str, key: int | str, doc: dict[str, Any], **meta: Any) -> None:
        doc = {**doc, "_meta": {"client": self.client, "fetched_at_utc": utc_now(), **meta}}
        path = self.path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, path)

    def keys(self, kind: str) -> set[str]:
        return {p.stem for p in (self.root / kind).glob("*.json")}

    def iter_docs(self, kind: str) -> Iterable[dict[str, Any]]:
        for p in sorted((self.root / kind).glob("*.json")):
            with p.open(encoding="utf-8") as f:
                yield json.load(f)


@dataclass
class StageResult:
    stage: str
    upstream: int = 0
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    orphaned: int = 0  # on disk but no longer returned by the API

    def __str__(self) -> str:
        parts = f"upstream={self.upstream} fetched={self.fetched} skipped={self.skipped} failed={self.failed}"
        return f"{self.stage}: {parts}" + (f" orphaned={self.orphaned}" if self.orphaned else "")


def _run_pool(
    stage: str,
    todo: list[Any],
    work: Callable[[Any], None],
    result: StageResult,
    workers: int,
) -> None:
    """Run ``work`` over ``todo`` on a thread pool; one failure never sinks the stage."""
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, item): item for item in todo}
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                future.result()
                result.fetched += 1
            except Exception:  # noqa: BLE001 - log and keep going; the next run retries it
                result.failed += 1
                log.exception("%s: failed on %r", stage, _describe(futures[future]))
            if done % 100 == 0 or done == len(todo):
                log.info("%s: %d/%d", stage, done, len(todo))


def _describe(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("EventId") or item.get("MatterId") or item
    return item


def ingest_reference(api: LegistarClient, store: BronzeStore) -> StageResult:
    result = StageResult("reference", upstream=len(REFERENCE_COLLECTIONS))
    for name, (path, key) in REFERENCE_COLLECTIONS.items():
        try:
            rows = list(api.get_all(path, order_by=key))
        except Exception:  # noqa: BLE001
            result.failed += 1
            log.exception("reference: failed on %s", name)
            continue
        store.write("reference", name, {"rows": rows}, row_count=len(rows))
        result.fetched += 1
        log.info("reference: %s (%d rows)", name, len(rows))
    return result


def _event_is_unsettled(event: dict[str, Any], cutoff: date) -> bool:
    """Agenda items can change without the event row changing, so anything recent,
    upcoming, or without final minutes is re-fetched on every run."""
    if event.get("EventMinutesStatusName") != "Final":
        return True
    return date.fromisoformat(event["EventDate"][:10]) >= cutoff


def ingest_events(
    api: LegistarClient,
    store: BronzeStore,
    *,
    full: bool = False,
    recent_days: int = 120,
    limit: int | None = None,
    workers: int = 4,
) -> StageResult:
    events = list(api.get_all("events", order_by="EventId"))
    result = StageResult("events", upstream=len(events))
    result.orphaned = len(store.keys("events") - {str(e["EventId"]) for e in events})
    cutoff = date.today() - timedelta(days=recent_days)

    def needs_fetch(event: dict[str, Any]) -> bool:
        if full or _event_is_unsettled(event, cutoff):
            return True
        on_disk = store.read("events", event["EventId"])
        return on_disk is None or on_disk.get("EventRowVersion") != event["EventRowVersion"]

    todo = [e for e in events if needs_fetch(e)][:limit]
    result.skipped = len(events) - len(todo)

    def fetch(event: dict[str, Any]) -> None:
        items = api.get(f"events/{event['EventId']}/eventitems", {**NOTE_PARAMS, "Attachments": 1})
        store.write("events", event["EventId"], {**event, "EventItems": items or []})

    _run_pool("events", todo, fetch, result, workers)
    return result


def ingest_matters(
    api: LegistarClient,
    store: BronzeStore,
    *,
    full: bool = False,
    limit: int | None = None,
    workers: int = 4,
) -> StageResult:
    matters = list(api.get_all("matters", order_by="MatterId"))
    result = StageResult("matters", upstream=len(matters))
    result.orphaned = len(store.keys("matters") - {str(m["MatterId"]) for m in matters})

    def needs_fetch(matter: dict[str, Any]) -> bool:
        if full:
            return True
        on_disk = store.read("matters", matter["MatterId"])
        return on_disk is None or on_disk.get("MatterRowVersion") != matter["MatterRowVersion"]

    todo = [m for m in matters if needs_fetch(m)][:limit]
    result.skipped = len(matters) - len(todo)

    def fetch(matter: dict[str, Any]) -> None:
        base = f"matters/{matter['MatterId']}"
        texts = []
        for version in api.get(f"{base}/versions") or []:
            text = api.get(f"{base}/texts/{version['Key']}")
            if text:
                texts.append(text)
        doc = {
            **matter,
            "MatterTexts": texts,
            "MatterHistories": api.get(f"{base}/histories", NOTE_PARAMS) or [],
            "MatterAttachments": api.get(f"{base}/attachments") or [],
            "MatterRelations": api.get(f"{base}/relations") or [],
        }
        store.write("matters", matter["MatterId"], doc)

    _run_pool("matters", todo, fetch, result, workers)
    return result


def _vote_signature(event: dict[str, Any]) -> str:
    """Changes whenever any agenda item of the meeting changes upstream."""
    versions = sorted(f"{i['EventItemId']}:{i['EventItemRowVersion']}" for i in event["EventItems"])
    return hashlib.sha1("|".join(versions).encode()).hexdigest()


def ingest_votes(
    api: LegistarClient,
    store: BronzeStore,
    *,
    full: bool = False,
    limit: int | None = None,
    workers: int = 4,
) -> StageResult:
    """Votes and roll calls, per meeting. Reads the event files already in bronze,
    so run the ``events`` stage first. Only items that record an outcome or a roll
    call are requested; that is roughly half of all agenda items."""
    result = StageResult("votes")
    todo = []
    for event in store.iter_docs("events"):
        result.upstream += 1
        on_disk = store.read("votes", event["EventId"])
        if full or on_disk is None or on_disk["_meta"].get("signature") != _vote_signature(event):
            todo.append(event)
    todo = todo[:limit]
    result.skipped = result.upstream - len(todo)

    def fetch(event: dict[str, Any]) -> None:
        items = []
        for item in event["EventItems"]:
            has_vote = item.get("EventItemPassedFlag") is not None
            has_roll_call = bool(item.get("EventItemRollCallFlag"))
            if not (has_vote or has_roll_call):
                continue
            item_id = item["EventItemId"]
            items.append(
                {
                    "EventItemId": item_id,
                    "Votes": (api.get(f"eventitems/{item_id}/votes") or []) if has_vote else [],
                    "RollCalls": (api.get(f"eventitems/{item_id}/rollcalls") or []) if has_roll_call else [],
                }
            )
        store.write(
            "votes",
            event["EventId"],
            {"EventId": event["EventId"], "Items": items},
            signature=_vote_signature(event),
        )

    _run_pool("votes", todo, fetch, result, workers)
    return result


def status(store: BronzeStore) -> dict[str, Any]:
    """Summarise what is on disk."""
    summary: dict[str, Any] = {"root": str(store.root)}
    for kind in ("reference", "events", "matters", "votes"):
        summary[kind] = len(store.keys(kind))
    if summary["events"]:
        events = list(store.iter_docs("events"))
        summary["event_items"] = sum(len(e["EventItems"]) for e in events)
        summary["event_date_range"] = [
            min(e["EventDate"][:10] for e in events),
            max(e["EventDate"][:10] for e in events),
        ]
        summary["events_with_final_minutes"] = sum(e.get("EventMinutesStatusName") == "Final" for e in events)
    if summary["matters"]:
        matters = list(store.iter_docs("matters"))
        summary["matters_with_text"] = sum(bool(m["MatterTexts"]) for m in matters)
        summary["matter_histories"] = sum(len(m["MatterHistories"]) for m in matters)
        summary["matter_attachments"] = sum(len(m["MatterAttachments"]) for m in matters)
        summary["matter_relations"] = sum(len(m["MatterRelations"]) for m in matters)
    return summary
