"""Turn retrieved matters into a prompt, and a prompt into a cited answer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .llm import ChatProvider
from .search import Hit, LawHit
from .text import extract_file_numbers

JURISDICTIONS = {"hennepinmn": "Hennepin County, Minnesota"}
MAX_HISTORY_LINES = 12
PASSAGE_LABELS = {"resolution": "Resolution text", "background": "Staff background", "title": "Title"}

SYSTEM_PROMPT = """\
You answer questions about the legislative record of {jurisdiction}: the county board, its committees, and the \
related authorities whose meetings are kept in the same system. Your readers are county staff. Some are program \
administrators who want to know what has been decided or is pending in their area and do not know file numbers; \
others are commissioners' staff and clerks tracking specific items.

Each question arrives with numbered sources retrieved from the county's Legistar system. A source is one legislative \
item: its file number, type, current status, requesting department, the actions taken on it meeting by meeting, \
links to related items, and passages from its resolution text and from the staff background written by the \
requesting department.

Work from the sources alone. A search retrieved them, so expect some to be off-topic and leave those out. When the \
sources do not answer the question, or answer only part of it, say so plainly and mention the closest thing they do \
cover: the reader is better served by "the record I searched does not show this" than by a plausible guess.

Put the source number in square brackets, like [2], right after each statement it supports, and name items by file \
number so the reader can look them up. Give dates for actions.

Three things about this record shape what can honestly be said:
- It holds action minutes: motions, outcomes and votes. What was said in discussion is not recorded. The staff \
background is the department's case for the item, not the board's reasoning, so when asked why something was \
decided, attribute the rationale to the staff background.
- Where an item stands comes from its status and history. "Queued for" means it sits on a draft agenda under a \
referral heading and no action is recorded yet. A file number ending in R1 or S1 is a revision or substitute of the \
item without the suffix.
- Dollar amounts and contract terms are what the resolution authorized. The executed contracts are kept elsewhere \
and may differ.

Some sources may be marked as law instead: the current text of a Minnesota statute section or of a county \
ordinance. They say what the law provides today, not what the board did, and they carry no dates of board action. \
Cite them with their number in the same way, and name the section or ordinance. When a question turns on what a law \
requires or allows, stay close to its wording and add that the County Attorney's Office is the authority on what it \
means for the county; this record cannot settle that.

Lead with the answer, in plain language, and keep it as short as the question allows. A list is fine when several \
items answer the question.

Follow the county's writing style: short sentences and everyday words, at about an 8th-grade reading level; active \
voice; sentence case, with no words in all capitals except acronyms; the month spelled out in dates (May 12, 2025, \
not 5/12/25 or May 12th); numerals for counts and amounts ($5 million, 7%); "Hennepin County" or "the county", never \
"Hennepin" alone; "the board" in lowercase unless it is the full name. Spell out an acronym the first time you use it."""

LAW_SHARE = 0.25  # at most this much of the prompt goes to statute and ordinance text
# Law text is added to a question that is about the law, not to every question: for
# "what happened to 26-0333" it would only be noise for a small model to read past.
_ASKS_ABOUT_LAW = re.compile(
    r"(?i)\b(statut\w*|law|laws|legal\w*|lawful\w*|ordinances?|authority|subdivision|subd\.?|"
    r"minn\.?\s*stat\w*|legislat\w*)\b|§"
)


def asks_about_law(question: str) -> bool:
    return bool(_ASKS_ABOUT_LAW.search(question or ""))


@dataclass
class Prompt:
    system: str
    user: str
    sources: list[Hit]  # in citation order: sources[0] is [1]
    dropped: int  # lower-ranked sources left out to fit the model's window
    law: list[LawHit] = field(default_factory=list)  # numbered after the matters: law[0] is [len(sources) + 1]


_CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_CODE = re.compile(r"(?<![\w-])[A-Z]{1,5}[\d-]{5,}\d(?![\w-])")  # agreement and contract numbers: A2412472, PR00006625
_DOLLARS = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?!\.?\d|\s*(?:million|billion|thousand|[MBKk]\b))")


def _amount(text: str) -> str:
    digits = text.replace("$", "").replace(",", "").strip()
    return digits.rstrip("0").rstrip(".") if "." in digits else digits


def unsupported_claims(answer: str, prompt: Prompt) -> list[str]:
    """Identifiers in an answer that appear nowhere in the sources it was given.

    Small local models sometimes splice identifiers together or misquote them. File
    numbers, agreement numbers, dollar amounts and citation numbers are exact strings,
    so they can be checked mechanically. An empty list does not make an answer right;
    a non-empty one means part of it was not read off the sources.
    """
    sources = prompt.user
    problems: list[str] = []
    cited = {int(n) for group in _CITATION.findall(answer) for n in group.split(",")}
    for number in sorted(n for n in cited if not 1 <= n <= len(prompt.sources) + len(prompt.law)):
        problems.append(f"cites source [{number}], which does not exist")
    known_files = set(extract_file_numbers(sources))
    for number in extract_file_numbers(answer):
        if number not in known_files:
            problems.append(f"file number {number} is not in the sources")
    for code in dict.fromkeys(_CODE.findall(answer)):
        if code not in sources:
            problems.append(f"{code} is not in the sources")
    known_amounts = {_amount(m) for m in _DOLLARS.findall(sources)}
    for amount in dict.fromkeys(_DOLLARS.findall(answer)):
        if _amount(amount) not in known_amounts:
            problems.append(f"{amount.strip()} is not in the sources")
    return problems


def render_source(hit: Hit) -> str:
    lines = [
        f"File number: {hit.file_number}" + (f" ({hit.matter_type})" if hit.matter_type else ""),
        f"Title: {hit.title}" if hit.title else None,
        f"Status: {hit.status}" if hit.status else None,
        f"Requesting department: {hit.department}" if hit.department else None,
        # Legistar dates a revision's introduction when its record was created, which
        # can fall after the vote; a model reads that as the approval date.
        f"Introduced: {hit.intro_date}" if hit.intro_date and not any(h["date"] < hit.intro_date for h in hit.history) else None,
        f"County Administrator's recommendation: {hit.recommendation}" if hit.recommendation else None,
    ]
    if hit.history:
        skipped = len(hit.history) - MAX_HISTORY_LINES
        lines.append("History:" + (f" (the first {skipped} entries are omitted)" if skipped > 0 else ""))
        for entry in hit.history[-MAX_HISTORY_LINES:]:
            action = entry["action"] or "on the agenda, no action recorded yet"
            if entry["referred_to"]:
                action += f", to {entry['referred_to']}"
            result = f" ({entry['result']})" if entry["result"] else ""
            lines.append(f"- {entry['date']}, {entry['body']}: {action}{result}")
    if hit.awaiting:
        lines.append("Queued for: " + "; ".join(hit.awaiting))
    if hit.related:
        lines.append("Related items: " + "; ".join(f"{r['file_number']} ({r['kind']}, {r['direction']})" for r in hit.related))
    if hit.statutes:
        lines.append("Statutes cited: " + "; ".join(hit.statutes))
    if hit.url:
        lines.append(f"Link: {hit.url}")
    for passage in hit.passages:
        if passage.section != "title":
            lines.append(f"{PASSAGE_LABELS.get(passage.section, passage.section)} (passage):\n{passage.text}")
    return "\n".join(line for line in lines if line)


def render_law(hit: LawHit) -> str:
    lines = [
        f"Law: {hit.label}" + (f" ({hit.title})" if hit.title and hit.title not in hit.label else ""),
        f"Status: {hit.status}" if hit.status and hit.status != "in force" else None,
        f"Cited by {hit.cited_by} matters in the record" if hit.cited_by else None,
        f"Link: {hit.url}" if hit.url else None,
    ]
    for passage in hit.passages:
        heading = f" ({passage.heading})" if passage.heading and passage.heading != hit.title else ""
        lines.append(f"{passage.label}{heading}, current text (passage):\n{passage.text}")
    return "\n".join(line for line in lines if line)


def build_prompt(
    question: str,
    hits: list[Hit],
    provider: ChatProvider,
    *,
    client: str,
    today: date | None = None,
    law: list[LawHit] | None = None,
) -> Prompt:
    system = SYSTEM_PROMPT.format(jurisdiction=JURISDICTIONS.get(client, f"the {client} Legistar tenant"))
    closing = f"Today's date: {today or date.today()}\n\nQuestion: {question}"
    budget = provider.context_chars - len(system) - len(closing)
    law_kept: list[LawHit] = []
    law_budget = budget * LAW_SHARE
    for hit in law or []:  # best first, within the law's share of the prompt
        size = len(render_law(hit)) + 40
        if size > law_budget:
            break
        law_budget -= size
        budget -= size
        law_kept.append(hit)
    kept: list[Hit] = []
    blocks: list[str] = []
    for hit in hits:  # best first; stop at the first source that does not fit
        block = f'<source number="{len(kept) + 1}">\n{render_source(hit)}\n</source>'
        if kept and len(block) > budget:
            break
        budget -= len(block)
        kept.append(hit)
        blocks.append(block)
    for hit in law_kept:
        blocks.append(f'<source number="{len(blocks) + 1}" kind="law">\n{render_law(hit)}\n</source>')
    sources = "\n\n".join(blocks) if blocks else "(the search returned nothing)"
    return Prompt(system, f"<sources>\n{sources}\n</sources>\n\n{closing}", kept, len(hits) - len(kept), law_kept)
