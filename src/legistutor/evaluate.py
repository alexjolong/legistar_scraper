"""``legistutor eval``: run the evaluation questions and measure the answers.

The questions are in ``eval/questions.csv``, written by people who know the record
(see ``docs/evaluation-set.md``). Each is put through the same search and the same
answering as the web page, and scored on what its row says a good result is:

* **found**: every item in ``must_find`` is among the results shown, and how high the
  first of them ranks;
* **lead**: for a question whose answer is in the law, the law's results came first,
  and for one about the record, the record's did;
* with ``--answers``, a model reads each answer against the row's ``answer_facts`` and
  ``must_not_say`` and says which facts are stated. This costs one answer and one
  judgment per question. The judge is a model too: read its tallies as a measurement
  with error, and read the answers behind any change that matters.

Each run is saved as JSON, so a later run can be compared with it (``--compare``).
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .llm import ChatProvider, LLMError
from .text import normalize_file_number

KINDS = (
    "lookup", "named-item", "topic", "revision", "path", "bounded", "amount", "vocabulary", "law", "law-and-record", "no-answer",
)  # fmt: skip
COLUMNS = ("id", "question", "asker", "kind", "must_find", "also_fine", "answer_facts", "must_not_say", "no_answer", "as_of", "how_known")
_STATUTE = re.compile(r"(?i)^(?:minn\.?\s*stat\.?\s*)?(?:§+\s*)?(\d+[A-Z]?\.\d+[A-Z]?)$")
_ORDINANCE = re.compile(r"(?i)^ordinance\s+(?:no\.?\s*)?(\w+)$")


class QuestionsError(ValueError):
    """The questions file needs fixing before it can be run; the message lists every problem."""


@dataclass
class Question:
    id: str
    question: str
    asker: str
    kind: str
    must_find: list[str]
    also_fine: list[str]
    answer_facts: list[str]
    must_not_say: str
    no_answer: bool


@dataclass
class Result:
    id: str
    kind: str
    question: str
    # search
    found: bool | None = None  # every must_find item was among the results; None where nothing must be found
    ranks: dict[str, int | None] = field(default_factory=dict)  # each must_find item's place, None if absent
    lead: str = ""  # which results the page would show first: "record" or "law"
    lead_expected: str | None = None
    returned: list[str] = field(default_factory=list)
    # answer (with --answers)
    answer: str | None = None
    facts: dict[str, bool] | None = None  # each answer_fact: does the answer state it
    said_forbidden: bool | None = None  # the answer says what must_not_say describes
    declined: bool | None = None  # the answer says the record does not hold this
    unsupported: list[str] = field(default_factory=list)  # identifiers in the answer that are not in its sources
    error: str | None = None

    @property
    def reciprocal_rank(self) -> float:
        places = [place for place in self.ranks.values() if place]
        return 1 / min(places) if places else 0.0


def _several(cell: str | None) -> list[str]:
    return [part.strip() for part in (cell or "").split(";") if part.strip()]


def read_questions(path: Path) -> list[Question]:
    """The rows of the questions file. Raises :class:`QuestionsError` naming every row that cannot be scored."""
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:  # Excel writes a byte-order mark
        reader = csv.DictReader(handle)
        missing = [column for column in COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise QuestionsError(f"{path} has no column named {', '.join(missing)}")
        rows = [row for row in reader if any((value or "").strip() for value in row.values())]
    questions, problems, seen = [], [], set()
    for line, row in enumerate(rows, start=2):
        cell = {column: (row.get(column) or "").strip() for column in COLUMNS}
        name = cell["id"] or f"line {line}"
        no_answer = cell["no_answer"].lower() in ("yes", "y", "true", "1") or cell["kind"] == "no-answer"
        if not cell["id"] or cell["id"] in seen:
            problems.append(f"{name}: id is {'used twice' if cell['id'] else 'empty'}")
        seen.add(cell["id"])
        if not cell["question"]:
            problems.append(f"{name}: question is empty")
        if cell["kind"] not in KINDS:
            problems.append(f"{name}: kind is {cell['kind']!r}; it must be one of {', '.join(KINDS)}")
        if not no_answer and not _several(cell["must_find"]):
            problems.append(f"{name}: must_find is empty (or set no_answer to yes)")
        if not no_answer and not _several(cell["answer_facts"]):
            problems.append(f"{name}: answer_facts is empty (or set no_answer to yes)")
        questions.append(
            Question(
                cell["id"], cell["question"], cell["asker"], cell["kind"], _several(cell["must_find"]),
                _several(cell["also_fine"]), _several(cell["answer_facts"]), cell["must_not_say"], no_answer,
            )  # fmt: skip
        )
    if problems:
        raise QuestionsError(f"{path} needs fixing:\n  " + "\n  ".join(problems))
    return questions


def target(item: str) -> tuple[str, str]:
    """What a must_find entry names: ``("statute", "163.051")``, ``("ordinance", "13")`` or ``("matter", "26-0333R1")``."""
    statute, ordinance = _STATUTE.match(item.strip()), _ORDINANCE.match(item.strip())
    if statute:
        return "statute", statute.group(1).upper()
    if ordinance:
        return "ordinance", ordinance.group(1).lower()
    return "matter", normalize_file_number(item)


def _place(item: str, hits: list[Any], law: list[Any]) -> int | None:
    kind, key = target(item)
    if kind == "matter":
        return next((n for n, hit in enumerate(hits, 1) if normalize_file_number(hit.file_number) == key), None)
    wanted = f"mnstat:{key}" if kind == "statute" else f":ordinance:{key}"
    return next((n for n, hit in enumerate(law, 1) if hit.id.upper() == wanted.upper() or hit.id.lower().endswith(wanted)), None)


def unknown_items(app: Any, questions: list[Question]) -> list[str]:
    """must_find entries that are not in the data at all: a typing slip, or a chapter not fetched."""
    problems = []
    for question in questions:
        for item in question.must_find:
            kind, key = target(item)
            if kind == "matter":
                known = key in app.searcher._by_number
            elif kind == "statute":
                known = app._rows("select 1 from vertices where upper(id) = ?", [f"MNSTAT:{key}"])
            else:
                known = app._rows("select 1 from vertices where type = 'ordinance' and lower(id) like ?", [f"%:ordinance:{key}"])
            if not known:
                problems.append(f"{question.id}: must_find names {item!r}, which is not in the data")
    return problems


def score_search(question: Question, found: dict[str, Any]) -> Result:
    """Score what the page's search returned for one question."""
    hits, law = found.get("hits", []), found.get("law", [])
    result = Result(question.id, question.kind, question.question, lead=found.get("lead", "record"))
    result.returned = [hit.file_number for hit in hits] + [hit.label for hit in law]
    if question.must_find:
        result.ranks = {item: _place(item, hits, law) for item in question.must_find}
        result.found = all(result.ranks.values())
        kinds = {target(item)[0] for item in question.must_find}
        result.lead_expected = "record" if kinds == {"matter"} else "law" if "matter" not in kinds else None
    return result


JUDGE_SYSTEM = (
    "You check an answer against a list of facts. You are strict and literal: a fact is stated only if the answer "
    "says it, with the same date, number, body or status; an answer that is vague where the fact is specific does "
    "not state it. Reply with one JSON object and nothing else."
)


def judge_prompt(question: Question, answer: str) -> str:
    facts = "\n".join(f"{n}. {fact}" for n, fact in enumerate(question.answer_facts, 1)) or "(none)"
    return (
        f"Question: {question.question}\n\nAnswer to check:\n<<<\n{answer}\n>>>\n\n"
        f"Facts a correct answer contains:\n{facts}\n\n"
        f"A wrong statement to look for: {question.must_not_say or '(none)'}\n\n"
        'Reply as JSON: {"facts": [true or false for each numbered fact, in order], '
        '"said_wrong_statement": true or false, '
        '"declined": true if the answer says the record or the sources do not contain the answer, else false}'
    )


def read_judgment(reply: str, facts: int) -> dict[str, Any] | None:
    """The judge's JSON, or None if it did not give one of the right shape."""
    start, end = reply.find("{"), reply.rfind("}")
    try:
        verdict = json.loads(reply[start : end + 1])
    except ValueError:
        return None
    stated = verdict.get("facts")
    if not isinstance(stated, list) or len(stated) != facts or not all(isinstance(value, bool) for value in stated):
        return None
    return {"facts": stated, "said_forbidden": bool(verdict.get("said_wrong_statement")), "declined": bool(verdict.get("declined"))}


def score_answer(question: Question, result: Result, events: list[tuple[str, Any]], judge: ChatProvider) -> None:
    """Fill in the answer's part of ``result`` from the page's ``ask`` events and the judge's reading."""
    result.answer = "".join(data["text"] for name, data in events if name == "token").strip()
    last_name, last = events[-1] if events else ("error", {"message": "no answer"})
    if last_name == "error":
        result.error = last["message"]
        return
    result.unsupported = last.get("unsupported", [])
    try:
        verdict = read_judgment("".join(judge.stream(JUDGE_SYSTEM, judge_prompt(question, result.answer))), len(question.answer_facts))
    except LLMError as exc:
        result.error = f"judge: {exc}"
        return
    if verdict is None:
        result.error = "judge: the reply could not be read"
        return
    result.facts = dict(zip(question.answer_facts, verdict["facts"]))
    result.said_forbidden = verdict["said_forbidden"] if question.must_not_say else None
    result.declined = verdict["declined"]


def run(
    app: Any,
    questions: list[Question],
    *,
    top: int = 8,
    answers: bool = False,
    judge: ChatProvider | None = None,
    progress: Callable[[str], None] = lambda line: None,
) -> list[Result]:
    """``app`` is the web page's :class:`~legistutor.web.App`, so this measures what people get."""
    results = []
    for number, question in enumerate(questions, 1):
        params = {"q": question.question, "top": str(top)}
        result = score_search(question, app.search(params))
        if answers:
            score_answer(question, result, list(app.ask(params)), judge or app.chat())
        results.append(result)
        progress(f"{number}/{len(questions)} {question.id}: {one_line(result)}")
    return results


def one_line(result: Result) -> str:
    parts = []
    if result.found is not None:
        missing = [item for item, place in result.ranks.items() if not place]
        parts.append("found" if result.found else f"MISSED {', '.join(missing)}")
    if result.lead_expected and result.lead != result.lead_expected:
        parts.append(f"led with the {result.lead}, not the {result.lead_expected}")
    if result.facts is not None:
        parts.append(f"{sum(result.facts.values())}/{len(result.facts)} facts" if result.facts else "no facts to state")
    if result.said_forbidden:
        parts.append("SAID what it must not")
    if result.kind == "no-answer" and result.declined is not None:
        parts.append("declined, as it should" if result.declined else "ANSWERED what the record cannot")
    if result.unsupported:
        parts.append(f"{len(result.unsupported)} unsupported identifiers")
    if result.error:
        parts.append(f"error: {result.error}")
    return "; ".join(parts) or "nothing to score"


def _share(part: int, whole: int) -> str:
    return f"{part}/{whole}" if whole else "-"


def summary(results: list[Result]) -> list[str]:
    """A table by kind of question, then the questions that need a look."""
    answered = any(result.answer is not None for result in results)
    header = f"{'kind':<16}{'n':>3}  {'found':>7}{'MRR':>7}{'lead ok':>9}" + (f"{'facts':>9}{'wrong':>7}{'unsupp.':>9}" if answered else "")
    lines = [header, "-" * len(header)]

    def row(name: str, group: list[Result]) -> str:
        searched = [r for r in group if r.found is not None]
        led = [r for r in group if r.lead_expected]
        line = (
            f"{name:<16}{len(group):>3}  {_share(sum(r.found for r in searched), len(searched)):>7}"
            f"{(sum(r.reciprocal_rank for r in searched) / len(searched) if searched else 0):>7.2f}"
            f"{_share(sum(r.lead == r.lead_expected for r in led), len(led)):>9}"
        )
        if answered:
            facts = [stated for r in group if r.facts for stated in r.facts.values()]
            line += (
                f"{_share(sum(facts), len(facts)):>9}{sum(bool(r.said_forbidden) for r in group):>7}"
                f"{sum(bool(r.unsupported) for r in group):>9}"
            )
        return line

    for kind in KINDS:
        group = [r for r in results if r.kind == kind]
        if group:
            lines.append(row(kind, group))
    lines += ["-" * len(header), row("all", results)]
    declining = [r for r in results if r.kind == "no-answer" and r.declined is not None]
    if declining:
        lines.append(f"no-answer questions declined: {_share(sum(r.declined for r in declining), len(declining))}")
    look = [r for r in results if "MISSED" in one_line(r) or "SAID" in one_line(r) or "ANSWERED" in one_line(r) or r.error]
    if look:
        lines += ["", "To look at:"] + [f"  {r.id} ({r.kind}): {one_line(r)}" for r in look]
    return lines


def save(results: list[Result], directory: Path, settings: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stamp}.json"
    record = {"run_utc": stamp, **settings, "results": [asdict(result) for result in results]}
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def compare(results: list[Result], earlier: Path) -> list[str]:
    """What changed for each question since an earlier run."""
    record = json.loads(Path(earlier).read_text(encoding="utf-8"))
    before = {row["id"]: row for row in record["results"]}
    told = ", ".join(f"{key} {record[key]}" for key in ("embed_model", "chat_model", "top") if key in record)
    lines = [f"Compared with {Path(earlier).name} ({told}):"]
    for result in results:
        old = before.get(result.id)
        if old is None:
            lines.append(f"  {result.id}: new question")
            continue
        changes = []
        if old.get("found") != result.found:
            changes.append("now found" if result.found else "no longer found")
        old_facts, new_facts = old.get("facts"), result.facts
        if old_facts is not None and new_facts is not None and sum(old_facts.values()) != sum(new_facts.values()):
            changes.append(f"facts {sum(old_facts.values())} -> {sum(new_facts.values())} of {len(new_facts)}")
        if old.get("lead") != result.lead:
            changes.append(f"leads with the {result.lead}, was the {old.get('lead')}")
        if changes:
            lines.append(f"  {result.id}: {'; '.join(changes)}")
    return lines if len(lines) > 1 else [*lines, "  no question changed"]
