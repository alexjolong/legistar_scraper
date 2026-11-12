"""Summaries of, and questions about, one piece of law, written by the chat model.

The model is given the text of one statute section, one chapter, or one ordinance,
and nothing else. What it can be given is bounded by its context window, so the text
is cut to fit in a way the reader is told about:

* everything, when it fits;
* for a question about something too long: the parts closest in meaning to the
  question (subdivisions of a section, sections of a chapter, headed parts of an
  ordinance), found with the search index;
* for a summary of a section or ordinance too long: the parts in order until the
  window is full;
* for a summary of a chapter too long: its table of sections, headings only.

A summary is kept on disk with the model that wrote it and a hash of what it read,
so it is written once and shown at once after that, until the text or the model
changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .law import chapter_sort_key

Rows = Callable[[str, list[Any]], list[tuple]]

SYSTEM_PROMPT = """\
You help staff of {jurisdiction} read the law. Your readers work in county programs and offices; most are not \
lawyers. You are given the current text of one piece of law, {what}, and either a request to summarise it or a \
question about it.

Work only from the text you are given. It is the whole of what you know about this law: do not add what such \
provisions usually say, what other statutes provide, or how a court might read it. If the text does not answer \
the question, say that plainly and point to the nearest thing it does say.

When summarising, say what the provision does, who it applies to, what it requires, allows or forbids, and any \
amounts, deadlines, dates or conditions, in plain words. Keep it to about 150 words, as a short paragraph or a \
short list. A repealed or expired part is worth one clause, no more.

When answering a question, lead with the answer and keep it short.

In both cases, name the place in the text that supports each point, the way a reader would look it up: \
"subd. 2", "section 383B.158", or the heading of the part. Keep numbers, dates and defined terms exactly as written.

{coverage}

You are describing what the text says, not advising anyone what they may do: other law can change how it applies, \
and the County Attorney's Office decides what it means for the county. The page already tells the reader this, so \
do not add a disclaimer of your own."""

WHAT = {
    "section": "a section of Minnesota Statutes",
    "chapter": "a chapter of Minnesota Statutes",
    "ordinance": "a county ordinance",
}
UNIT = {"section": "subdivision", "chapter": "section", "ordinance": "part"}


class NothingToRead(LookupError):
    pass


@dataclass
class Part:
    key: str  # what the search index knows it by: a vertex id, or an ordinance part's heading
    label: str  # "Subd. 2. Authority", "§ 383B.158 DESIGN-BUILD CONTRACTS", "Section I: Definitions"
    text: str

    def render(self) -> str:
        return f"{self.label}\n{self.text}" if self.label else self.text


@dataclass
class Scope:
    id: str
    kind: str  # section, chapter, ordinance
    label: str  # "Minn. Stat. § 383B.158 (DESIGN-BUILD CONTRACTS)"
    parts: list[Part]
    outline: str | None = None  # a chapter's table of sections
    subdivisions: set[str] = field(default_factory=set)

    @property
    def chars(self) -> int:
        return sum(len(part.render()) + 2 for part in self.parts)


@dataclass
class Selection:
    included: list[Part]
    omitted: list[Part]
    basis: str  # whole, closest, in order, outline
    text: str  # what the model reads
    coverage: str  # the same fact, said to the reader and to the model


def _subdivision_order(number: str) -> tuple[int, str]:
    digits = re.match(r"\d+", number)
    return (int(digits.group(0)) if digits else 0, number)


def load(rows: Rows, node: str) -> Scope:
    """The text of ``node``, in parts, read from the graph."""
    found = rows("select type from vertices where id = ?", [node])
    if not found:
        raise NothingToRead(f"No statute or ordinance {node!r}")
    kind = found[0][0]
    if kind == "subdivision":
        node, kind = node.split("#")[0], "statute"
    if kind == "ordinance":
        number, title = rows("select number, title from v_ordinance where id = ?", [node])[0]
        parts = [
            Part(heading or "", heading or "Opening", text)
            for heading, text in rows("select heading, text from t_ordinance_text where ordinance = ? order by position", [node])
            if text
        ]
        label = f"Ordinance {number}: {title}" if number else (title or node)
        if not parts:
            raise NothingToRead(f"The text of {label} has not been fetched.")
        return Scope(node, "ordinance", label, parts)
    if kind != "statute":
        raise NothingToRead("Summaries and questions are for statutes and ordinances.")
    number, statute_kind, chapter, headnote, status, note, text = rows(
        "select number, kind, chapter, headnote, status, note, text from v_statute where id = ?", [node]
    )[0]
    if statute_kind == "section":
        label = f"Minn. Stat. § {number}" + (f" ({headnote})" if headnote else "")
        parts = [Part(node, "", text)] if text else []
        subdivisions = rows("select id, subdivision, headnote, text from v_subdivision where section = ?", [number])
        for vid, subdivision, subd_headnote, subd_text in sorted(subdivisions, key=lambda r: _subdivision_order(r[1])):
            if subd_text:
                parts.append(Part(vid, f"Subd. {subdivision}." + (f" {subd_headnote}." if subd_headnote else ""), subd_text))
        if not parts:
            raise NothingToRead(f"There is no text to read for {label}" + (f": {note}" if note else "; it is not in the statutes as published."))
        return Scope(node, "section", label, parts, subdivisions={row[1] for row in subdivisions})
    sections = sorted(
        rows("select id, number, headnote, status, text from v_statute where chapter = ? and kind = 'section' and status is not null", [chapter]),
        key=lambda r: chapter_sort_key(r[1]),
    )
    by_section: dict[str, list[tuple]] = {}
    for section, subdivision, subd_headnote, subd_text in rows(
        "select section, subdivision, headnote, text from v_subdivision where chapter = ?", [chapter]
    ):
        by_section.setdefault(section, []).append((subdivision, subd_headnote, subd_text))
    parts, outline = [], []
    for vid, section, section_headnote, section_status, section_text in sections:
        outline.append(f"§ {section} {section_headnote or ''}".rstrip() + ("" if section_status == "in force" else f" [{section_status}]"))
        body = [section_text] if section_text else []
        for subdivision, subd_headnote, subd_text in sorted(by_section.get(section, []), key=lambda r: _subdivision_order(r[0])):
            if subd_text:
                body.append(f"Subd. {subdivision}." + (f" {subd_headnote}." if subd_headnote else "") + f"\n{subd_text}")
        if section_status == "in force" and body:
            parts.append(Part(vid, f"§ {section} {section_headnote or ''}".rstrip(), "\n".join(body)))
    label = f"Minn. Stat. ch. {number}" + (f" ({headnote})" if headnote else "")
    if not parts:
        raise NothingToRead(f"{label} has not been fetched.")
    return Scope(node, "chapter", label, parts, outline="\n".join(outline))


def _names(parts: list[Part], limit: int = 8) -> str:
    """Short names of some parts: "subd. 1, subd. 2a", "§ 375.18, § 375.19"."""
    names = [re.sub(r"^Subd\. (\w+)\..*$", r"subd. \1", p.label.split("\n")[0]) for p in parts]
    names = [re.sub(r"^(§ \S+).*$", r"\1", name) or "the opening text" for name in names]
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def select(scope: Scope, budget: int, scores: dict[str, float] | None = None, *, summary: bool) -> Selection:
    """Choose what fits in ``budget`` characters, and say what was chosen."""
    unit = UNIT[scope.kind]
    if scope.chars <= budget:
        text = "\n\n".join(part.render() for part in scope.parts)
        return Selection(scope.parts, [], "whole", text, f"All of {scope.label} was read.")
    too_long = f"{scope.label} is too long for the model to read at once"
    if scope.kind == "chapter" and summary:
        outline = scope.outline or ""
        if len(outline) > budget:
            outline = outline[:budget].rsplit("\n", 1)[0]
        coverage = f"{too_long}, so this was written from its table of sections: the headings only, not the text under them."
        return Selection([], scope.parts, "outline", outline, coverage)
    order = scope.parts if not scores else sorted(scope.parts, key=lambda p: -scores.get(p.key, 0.0))
    chosen: list[Part] = []
    remaining = budget
    for part in order:
        size = len(part.render()) + 2
        if size <= remaining:
            chosen.append(part)
            remaining -= size
        elif not chosen:  # one part alone overflows: read its beginning
            chosen.append(Part(part.key, part.label, part.text[: max(remaining - len(part.label) - 60, 0)] + "\n[cut off here for length]"))
            break
        elif not scores:
            break  # in order means in order: stop at the first part that does not fit
    kept = {part.key for part in chosen}
    included = [next(c for c in chosen if c.key == part.key) for part in scope.parts if part.key in kept]
    omitted = [part for part in scope.parts if part.key not in kept]
    how = "chosen as closest to the question" if scores else "taken in order from the start"
    coverage = (
        f"{too_long}. Read: {_names(included)} ({len(included)} of {len(scope.parts)} {unit}s, {how}). "
        f"Not read: {_names(omitted, 5)}."
    )
    text = "\n\n".join(part.render() for part in included)
    return Selection(included, omitted, "closest" if scores else "in order", text, coverage)


def build_prompt(scope: Scope, selection: Selection, question: str, *, jurisdiction: str) -> tuple[str, str]:
    if selection.basis == "whole":
        coverage = "You have the whole of it."
    else:
        coverage = (
            f"You do not have the whole of it. {selection.coverage} Say what you were not shown if the reader "
            "would otherwise take your answer to cover everything."
        )
    if scope.kind == "section":
        # Without this a model calls numbered clauses "subd. 1", or cites a subdivision that is not there.
        numbers = sorted(scope.subdivisions, key=_subdivision_order)
        coverage += (
            f" This section's subdivisions are numbered {', '.join(numbers)}; cite only those as subdivisions. "
            'Write a paragraph inside a subdivision with brackets, "subd. 5(a)": "subd. 5a" would name a different subdivision.'
            if numbers
            else " This section is not divided into subdivisions. Point to its paragraphs and clauses by the labels "
            "the text itself uses, such as (a) or clause (2), and never call one a subdivision."
        )
    system = SYSTEM_PROMPT.format(jurisdiction=jurisdiction, what=WHAT[scope.kind], coverage=coverage)
    task = f"Question: {question}" if question else f"Summarise this {scope.kind} for a county employee."
    return system, f'<law name="{scope.label}">\n{selection.text}\n</law>\n\n{task}'


def budget(context_chars: int, question: str, *, jurisdiction: str) -> int:
    """Characters of law text that fit beside the instructions and the question."""
    fixed = len(SYSTEM_PROMPT.format(jurisdiction=jurisdiction, what=WHAT["section"], coverage="")) + len(question)
    return max(context_chars - fixed - 900, 1000)


_SECTION_REFERENCE = re.compile(r"(?:§+|\b[Ss]ections?)\s*(\d{1,3}[A-Z]?\.\d+[A-Za-z]?)")
_SUBDIVISION_REFERENCE = re.compile(r"\b[Ss]ubd(?:ivision)?s?\.?\s*(\d+[a-z]?)\b")


def unsupported_references(answer: str, scope: Scope, selection: Selection) -> list[str]:
    """Section and subdivision numbers in an answer that are not in what the model read.
    A model that names "subd. 7" of a section with five subdivisions did not read that off the text."""
    problems: list[str] = []
    text = f"{scope.label}\n{selection.text}"
    for number in dict.fromkeys(_SECTION_REFERENCE.findall(answer)):
        if number not in text:
            problems.append(f"section {number} is not in the text it read")
    if scope.kind == "section":
        read = {m for part in selection.included for m in re.findall(r"^Subd\. (\w+)\.", part.label)}
        read |= set(_SUBDIVISION_REFERENCE.findall(selection.text))
        for number in dict.fromkeys(_SUBDIVISION_REFERENCE.findall(answer)):
            if number not in read:
                where = "was not among the parts it read" if number in scope.subdivisions else "does not exist in this section"
                problems.append(f"subdivision {number} {where}")
    return problems


# --- summaries kept on disk ---------------------------------------------------------------------


def _path(directory: Path, node: str) -> Path:
    return directory / (re.sub(r"[^A-Za-z0-9.]+", "_", node) + ".json")


def fingerprint(system: str, user: str) -> str:
    return hashlib.sha1(f"{system}\n{user}".encode()).hexdigest()


def read_summary(directory: Path | None, node: str) -> dict[str, Any] | None:
    if directory is None:
        return None
    try:
        return json.loads(_path(directory, node).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_summary(directory: Path | None, node: str, **facts: Any) -> Path | None:
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = _path(directory, node)
    temporary = path.with_name(path.name + ".tmp")
    record = {"id": node, "written_utc": datetime.now(UTC).isoformat(timespec="seconds"), **facts}
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
    return path
