"""Turn retrieved matters into a prompt, and a prompt into a cited answer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .llm import ChatProvider
from .search import Hit

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

Lead with the answer, in plain language, and keep it as short as the question allows. A list is fine when several \
items answer the question."""


@dataclass
class Prompt:
    system: str
    user: str
    sources: list[Hit]  # in citation order: sources[0] is [1]
    dropped: int  # lower-ranked sources left out to fit the model's window


def render_source(hit: Hit) -> str:
    lines = [
        f"File number: {hit.file_number}" + (f" ({hit.matter_type})" if hit.matter_type else ""),
        f"Title: {hit.title}" if hit.title else None,
        f"Status: {hit.status}" if hit.status else None,
        f"Requesting department: {hit.department}" if hit.department else None,
        f"Introduced: {hit.intro_date}" if hit.intro_date else None,
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


def build_prompt(question: str, hits: list[Hit], provider: ChatProvider, *, client: str, today: date | None = None) -> Prompt:
    system = SYSTEM_PROMPT.format(jurisdiction=JURISDICTIONS.get(client, f"the {client} Legistar tenant"))
    closing = f"Today's date: {today or date.today()}\n\nQuestion: {question}"
    budget = provider.context_chars - len(system) - len(closing)
    kept: list[Hit] = []
    blocks: list[str] = []
    for hit in hits:  # best first; stop at the first source that does not fit
        block = f'<source number="{len(kept) + 1}">\n{render_source(hit)}\n</source>'
        if kept and len(block) > budget:
            break
        budget -= len(block)
        kept.append(hit)
        blocks.append(block)
    sources = "\n\n".join(blocks) if blocks else "(the search returned nothing)"
    return Prompt(system, f"<sources>\n{sources}\n</sources>\n\n{closing}", kept, len(hits) - len(kept))
