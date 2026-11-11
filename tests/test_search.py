"""Index a tiny hand-made record with a fake embedding model, then search it and
build prompts from the results."""

from __future__ import annotations

import hashlib
import re
from datetime import date

import pyarrow.parquet as pq
import pytest
from test_silver import BOARD, PUBLIC_WORKS, _event, _item, _matter

from legistutor import gold, rag, silver
from legistutor.bronze import BronzeStore
from legistutor.embed import normalize
from legistutor.search import Filters, Searcher

C = "test"
TEMPLATE = "Item Description:\n..title\nT\n..end\n\nResolution:\n..body\n{}\n..end\n\nBackground:\n{}"


class FakeEmbedder:
    """Bag of words hashed into a few dimensions: texts sharing words are close."""

    def __init__(self, model: str = "fake-embedder") -> None:
        self.model = model
        self.embedded: list[str] = []

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * 64
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1.0
        return normalize(vector)

    def embed_documents(self, texts):
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)


def _write_bronze(tmp_path, *, shelter_background="Emergency shelter beds for people experiencing homelessness this winter."):
    store = BronzeStore(tmp_path, C)
    store.write("reference", "bodies", {"rows": [
        {"BodyId": BOARD, "BodyName": "Board", "BodyTypeName": "Primary Legislative Body", "BodyActiveFlag": 1},
        {"BodyId": PUBLIC_WORKS, "BodyName": "Public Works Committee", "BodyTypeName": "Committees", "BodyActiveFlag": 1},
    ]})
    store.write("reference", "matter_statuses", {"rows": []})
    store.write("reference", "persons", {"rows": []})
    store.write("reference", "office_records", {"rows": []})

    road = _matter(10, "26-0001", TEMPLATE.format(
        "BE IT RESOLVED, that Agreement PR00012345 with Acme Paving be approved.",
        "Reconstruction of County Road 81 under Minn. Stat. § 383B.158. The bridge deck is failing."),
        requester="Public Works - Roads", relations=[(11, 2)])
    revision = _matter(11, "26-0001 R1", TEMPLATE.format(
        "BE IT RESOLVED, that Agreement PR00012345 with Acme Paving be approved as revised.", "Revised cost estimate."),
        requester="Public Works - Roads", relations=[(10, 1)])
    shelter = _matter(12, "26-0002", TEMPLATE.format(
        "BE IT RESOLVED, that funding for shelter services be accepted.", shelter_background), requester="Human Services")
    library = _matter(13, "26-0003", TEMPLATE.format("BE IT RESOLVED, that library hours be extended.", "Sunday hours."))
    minutes = _matter(14, "26-0004", "")
    minutes.update(MatterTypeName="Minutes", MatterTitle="Approval of minutes of the road committee", MatterTexts=[])
    letter = _matter(15, "26-0005", "")
    letter.update(MatterTypeName="Correspondence", MatterTitle="Letter from the City of Edina regarding transit", MatterTexts=[])
    for matter in (road, revision, shelter, library, minutes, letter):
        store.write("matters", matter["MatterId"], matter)

    store.write("events", 1, _event(1, BOARD, "Board", "2026-02-03", [
        _item(100, 1, matter=10, action="refer to committee", passed="Pass",
              text="moved to refer to committee the Resolution to the Public Works Committee."),
        _item(101, 2, matter=12, action="adopt", passed="Pass"),
    ]))
    store.write("events", 2, _event(2, PUBLIC_WORKS, "Public Works Committee", "2026-02-10", [
        _item(200, 1, matter=10, action="progress", passed="Pass", text="moved to progress the Resolution."),
    ]))
    store.write("events", 3, _event(3, BOARD, "Board", "2026-03-17", [
        _item(300, 1, matter=11, action="adopt", passed="Pass"),
        _item(301, 2, title="Referred to Public Works Committee"),
        _item(302, 3, matter=13),  # draft meeting: queued, no action yet
    ], minutes="Draft"))
    silver.build(tmp_path, C)


@pytest.fixture
def indexed(tmp_path):
    _write_bronze(tmp_path)
    embedder = FakeEmbedder()
    report = gold.build(tmp_path, C, embedder)
    return tmp_path, embedder, report


def numbers(hits):
    return [hit.file_number for hit in hits]


def test_index_rows_and_skipped_types(indexed):
    tmp_path, _, report = indexed
    rows = pq.read_table(gold.chunks_path(tmp_path, C)).to_pylist()
    assert report["passages"] == len(rows) == 9  # 4 matters x (resolution + background) + 1 title-only letter
    assert report["by_section"] == {"resolution": 4, "background": 4, "title": 1}
    assert "26-0004" not in {row["file_number"] for row in rows}  # approvals of minutes are not indexed
    road = next(row for row in rows if row["chunk_id"] == "test:matter:10#background#0")
    assert road["embed_text"].startswith("26-0001 | Resolution | Public Works - Roads\nTitle of 26-0001\nBackground:\n")
    assert road["text"].startswith("Reconstruction of County Road 81")
    assert (road["department"], road["last_action_date"], road["embedding_model"]) == ("Public Works - Roads", date(2026, 2, 10), "fake-embedder")
    assert len(road["embedding"]) == 64


def test_rebuild_only_embeds_changed_text(indexed):
    tmp_path, _, first = indexed
    assert (first["embedded"], first["reused"]) == (9, 0)
    again = FakeEmbedder()
    report = gold.build(tmp_path, C, again)
    assert (report["embedded"], report["reused"], again.embedded) == (0, 9, [])

    _write_bronze(tmp_path, shelter_background="Rewritten: transitional housing vouchers.")
    report = gold.build(tmp_path, C, again)
    assert (report["embedded"], report["reused"]) == (1, 8)
    assert "transitional housing vouchers" in again.embedded[0]

    other_model = FakeEmbedder("another-model")  # vectors from a different model are never reused
    assert gold.build(tmp_path, C, other_model)["embedded"] == 9


def test_file_number_leads_and_brings_its_revision(indexed):
    tmp_path, embedder, _ = indexed
    hits = Searcher(tmp_path, C, embedder).search("what happened with 26-0001?", top=3)
    assert numbers(hits)[:2] == ["26-0001", "26-0001 R1"]
    assert hits[0].matched_by[0] == "file number"
    assert hits[0].passages  # a named matter still shows its text
    only_revision = Searcher(tmp_path, C, embedder).search("status of 26-0001 R1", top=1)
    assert numbers(only_revision) == ["26-0001 R1"]


def test_keywords_and_meaning(indexed):
    tmp_path, embedder, _ = indexed
    searcher = Searcher(tmp_path, C, embedder)
    assert searcher.keywords
    by_agreement = searcher.search("PR00012345", top=2)
    assert set(numbers(by_agreement)) == {"26-0001", "26-0001 R1"}
    assert "keywords" in by_agreement[0].matched_by
    assert numbers(searcher.search("winter shelter beds for people experiencing homelessness", top=1)) == ["26-0002"]
    assert numbers(searcher.search("letter from Edina about transit", top=1)) == ["26-0005"]


def test_filters(indexed):
    tmp_path, embedder, _ = indexed
    searcher = Searcher(tmp_path, C, embedder)
    everything = "agreement shelter library transit resolution"
    assert set(numbers(searcher.search(everything, filters=Filters(department="public works")))) == {"26-0001", "26-0001 R1"}
    assert set(numbers(searcher.search(everything, filters=Filters(body="Public Works")))) == {"26-0001"}
    assert set(numbers(searcher.search(everything, filters=Filters(since=date(2026, 3, 1))))) == {"26-0001 R1", "26-0003"}
    assert set(numbers(searcher.search(everything, filters=Filters(matter_type="correspondence")))) == {"26-0005"}


def test_cards_come_from_the_graph(indexed):
    tmp_path, embedder, _ = indexed
    searcher = Searcher(tmp_path, C, embedder)
    road, library = searcher.cards(["test:matter:10", "test:matter:13"])
    assert [(h["date"], h["body"], h["action"], h["referred_to"]) for h in road.history] == [
        (date(2026, 2, 3), "Board", "refer to committee", "Public Works Committee"),
        (date(2026, 2, 10), "Public Works Committee", "progress", None),  # the revision went to the Board instead
    ]
    assert road.related == [{"file_number": "26-0001 R1", "kind": "revision", "direction": "later"}]
    assert road.statutes == ["Minn. Stat. § 383B.158"]
    assert road.url and road.department == "Public Works - Roads"
    assert library.awaiting == ["Public Works Committee"]
    assert library.history[0]["action"] is None


def test_index_and_query_models_must_match(indexed):
    tmp_path, _, _ = indexed
    with pytest.raises(RuntimeError, match="not comparable"):
        Searcher(tmp_path, C, FakeEmbedder("another-model"))


class _Provider:
    name, model = "fake", "fake-model"

    def __init__(self, context_chars):
        self.context_chars = context_chars


def test_prompt_numbers_sources_and_states_what_the_graph_knows(indexed):
    tmp_path, embedder, _ = indexed
    hits = Searcher(tmp_path, C, embedder).search("26-0001 and 26-0003", top=3)
    prompt = rag.build_prompt("Where does the road project stand?", hits, _Provider(100_000), client=C, today=date(2026, 3, 20))
    assert numbers(prompt.sources) == ["26-0001", "26-0001 R1", "26-0003"] and prompt.dropped == 0
    assert prompt.user.index('<source number="1">') < prompt.user.index("File number: 26-0001 (Resolution)")
    assert "- 2026-02-03, Board: refer to committee, to Public Works Committee (Pass)" in prompt.user
    assert "Related items: 26-0001 R1 (revision, later)" in prompt.user
    assert "Queued for: Public Works Committee" in prompt.user
    assert "on the agenda, no action recorded yet" in prompt.user
    assert "Resolution text (passage):\nBE IT RESOLVED, that Agreement PR00012345" in prompt.user
    assert prompt.user.endswith("Today's date: 2026-03-20\n\nQuestion: Where does the road project stand?")


def test_prompt_drops_lowest_ranked_sources_that_do_not_fit(indexed):
    tmp_path, embedder, _ = indexed
    hits = Searcher(tmp_path, C, embedder).search("26-0001 and 26-0003", top=3)
    system_size = len(rag.build_prompt("q", [], _Provider(10**6), client=C).system)
    one = len(rag.render_source(hits[0])) + 200
    prompt = rag.build_prompt("q", hits, _Provider(system_size + one), client=C)
    assert numbers(prompt.sources) == ["26-0001"] and prompt.dropped == 2
    assert '<source number="2">' not in prompt.user
