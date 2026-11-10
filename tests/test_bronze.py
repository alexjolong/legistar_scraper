"""Offline tests: the Legistar API is replaced by an in-memory fake."""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from legistar_scraper import bronze
from legistar_scraper.client import LegistarClient

OLD = "2023-06-01T00:00:00"


def make_event(event_id: int, *, version: str = "v1", event_date: str = OLD, minutes: str = "Final") -> dict:
    return {
        "EventId": event_id,
        "EventRowVersion": version,
        "EventDate": event_date,
        "EventMinutesStatusName": minutes,
    }


def make_item(item_id: int, *, passed: int | None = 1, roll_call: int = 0, version: str = "i1") -> dict:
    return {
        "EventItemId": item_id,
        "EventItemRowVersion": version,
        "EventItemPassedFlag": passed,
        "EventItemRollCallFlag": roll_call,
    }


class FakeLegistar:
    """Serves just enough of the API surface for the bronze stages."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.items: dict[int, list[dict]] = {}
        self.matters: list[dict] = []
        self.calls: list[str] = []
        self.fail_once: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/test/")
        self.calls.append(path)
        if path in self.fail_once:
            self.fail_once.discard(path)
            return httpx.Response(503)
        params = request.url.params
        parts = path.split("/")
        if path in ("events", "matters"):
            rows = self.events if path == "events" else self.matters
            skip, top = int(params["$skip"]), int(params["$top"])
            return httpx.Response(200, json=rows[skip : skip + top])
        if parts[0] == "events" and parts[2] == "eventitems":
            return httpx.Response(200, json=self.items.get(int(parts[1]), []))
        if parts[0] == "matters" and parts[2] == "versions":
            return httpx.Response(200, json=[{"Key": "900", "Value": "1"}])
        if parts[0] == "matters" and parts[2] == "texts":
            return httpx.Response(200, json={"MatterTextPlain": f"text of {parts[1]}"})
        if parts[0] == "matters":
            return httpx.Response(200, json=[{"kind": parts[2]}])
        if parts[0] == "eventitems":
            return httpx.Response(200, json=[{"kind": parts[2], "item": int(parts[1])}])
        return httpx.Response(404)

    def count(self, fragment: str) -> int:
        return sum(fragment in c for c in self.calls)


@pytest.fixture
def fake() -> FakeLegistar:
    return FakeLegistar()


@pytest.fixture
def api(fake: FakeLegistar, monkeypatch: pytest.MonkeyPatch) -> LegistarClient:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return LegistarClient("test", requests_per_second=1e6, transport=httpx.MockTransport(fake.handler))


@pytest.fixture
def store(tmp_path) -> bronze.BronzeStore:
    return bronze.BronzeStore(tmp_path, "test")


def test_get_all_pages_until_short_page(fake, api):
    fake.events = [make_event(i) for i in range(5)]
    rows = list(api.get_all("events", order_by="EventId", page_size=2))
    assert [r["EventId"] for r in rows] == [0, 1, 2, 3, 4]
    assert fake.count("events") == 3


def test_get_retries_transient_errors_and_returns_none_on_404(fake, api):
    fake.fail_once.add("events/1/eventitems")
    assert api.get("events/1/eventitems") == []
    assert fake.count("events/1/eventitems") == 2
    assert api.get("nope") is None


def test_events_are_fetched_once_then_skipped_until_they_change(fake, api, store):
    fake.events = [make_event(1), make_event(2)]
    fake.items = {1: [make_item(10)], 2: []}

    first = bronze.ingest_events(api, store)
    assert (first.fetched, first.skipped) == (2, 0)
    doc = store.read("events", 1)
    assert doc["EventItems"] == [make_item(10)]
    assert doc["_meta"]["client"] == "test"

    second = bronze.ingest_events(api, store)
    assert (second.fetched, second.skipped) == (0, 2)

    fake.events[1] = make_event(2, version="v2")
    third = bronze.ingest_events(api, store)
    assert (third.fetched, third.skipped) == (1, 1)
    assert store.read("events", 2)["EventRowVersion"] == "v2"


def test_unsettled_events_are_always_refetched(fake, api, store):
    recent = (date.today() - timedelta(days=5)).isoformat() + "T00:00:00"
    fake.events = [
        make_event(1),  # old and final: settled
        make_event(2, event_date=recent),  # recent
        make_event(3, minutes="Draft"),  # old, but minutes never finalised
    ]
    bronze.ingest_events(api, store)
    again = bronze.ingest_events(api, store)
    assert (again.fetched, again.skipped) == (2, 1)


def test_matters_nest_their_child_collections(fake, api, store):
    fake.matters = [{"MatterId": 7, "MatterRowVersion": "m1"}]
    result = bronze.ingest_matters(api, store)
    assert result.fetched == 1
    doc = store.read("matters", 7)
    assert doc["MatterTexts"] == [{"MatterTextPlain": "text of 7"}]
    assert doc["MatterHistories"] == [{"kind": "histories"}]
    assert doc["MatterAttachments"] == [{"kind": "attachments"}]
    assert doc["MatterRelations"] == [{"kind": "relations"}]
    assert bronze.ingest_matters(api, store).skipped == 1


def test_orphans_are_counted_not_deleted(fake, api, store):
    fake.matters = [{"MatterId": 7, "MatterRowVersion": "m1"}, {"MatterId": 8, "MatterRowVersion": "m1"}]
    bronze.ingest_matters(api, store)
    fake.matters = fake.matters[:1]
    result = bronze.ingest_matters(api, store)
    assert result.orphaned == 1
    assert store.read("matters", 8) is not None


def test_votes_only_requested_for_items_with_an_outcome(fake, api, store):
    fake.events = [make_event(1)]
    fake.items = {1: [make_item(10), make_item(11, passed=None), make_item(12, passed=None, roll_call=1)]}
    bronze.ingest_events(api, store)

    result = bronze.ingest_votes(api, store)
    assert result.fetched == 1
    items = {i["EventItemId"]: i for i in store.read("votes", 1)["Items"]}
    assert set(items) == {10, 12}
    assert items[10]["Votes"] and not items[10]["RollCalls"]
    assert items[12]["RollCalls"] and not items[12]["Votes"]

    assert bronze.ingest_votes(api, store).skipped == 1

    # an agenda item changing upstream invalidates that meeting's votes
    fake.items[1][0] = make_item(10, version="i2")
    bronze.ingest_events(api, store, full=True)
    assert bronze.ingest_votes(api, store).fetched == 1


def test_a_failing_entity_does_not_sink_the_stage(fake, api, store, monkeypatch):
    fake.events = [make_event(1), make_event(2)]
    real_get = api.get

    def flaky_get(path, params=None):
        if path == "events/1/eventitems":
            raise RuntimeError("boom")
        return real_get(path, params)

    monkeypatch.setattr(api, "get", flaky_get)
    result = bronze.ingest_events(api, store)
    assert (result.fetched, result.failed) == (1, 1)
    assert store.read("events", 1) is None
    assert json.dumps(store.read("events", 2))  # the good one landed
