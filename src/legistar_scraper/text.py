"""Text parsing for Legistar records: RTF notes, legislation sections, citations."""

from __future__ import annotations

import re

from striprtf.striprtf import rtf_to_text as _strip_rtf

SECTION_ORDER = ("title", "resolution", "background", "recommendation", "other")

_ODD_SPACES = str.maketrans({" ": " ", " ": " ", " ": " ", "​": ""})


def clean(text: str | None) -> str:
    """Normalise line endings and the non-breaking spaces Legistar text is full of."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_ODD_SPACES)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def rtf_to_text(value: str | None) -> str:
    """Agenda and minutes notes arrive as RTF (occasionally as plain text)."""
    if not value:
        return ""
    if value.lstrip().startswith("{\\rtf"):
        value = _strip_rtf(value)
    return clean(value)


_DIRECTIVES = {
    "..title": "title",
    "..body": "resolution",
    "..recommendation": "recommendation",
    "..end": None,
}
_DIRECTIVE_INLINE = re.compile(r"[ \t]*(\.\.(?:title|body|recommendation|end))\b[ \t]*")
_RESOLUTION_OPENING = re.compile(r"WHEREAS\b|(?:NOW,? THEREFORE,? )?BE IT (?:FURTHER )?RESOLVED\b|RESOLVED\b")
_PLACEHOLDER_RECOMMENDATIONS = {"choose an item."}


def split_sections(plain: str | None) -> dict[str, str]:
    """Split a matter's plain text into its marked sections.

    The usual shape is::

        Item Description:
        ..title
        <title>
        ..end

        Resolution:
        ..body
        BE IT RESOLVED ...
        ..end

        Background:
        <staff background, often with "Current Request:" style sub-headings>
        ..recommendation
        Recommendation from County Administrator: Recommend Approval

    Variants exist (no ``..end``, a ``WHEREAS:`` preamble before ``..body``, an
    empty ``..body``/``..end`` pair with the resolution after it, directives glued
    to other text), so this is a small state machine rather than a template. Text
    outside any recognised section is returned as ``other``.
    """
    buckets: dict[str, list[str]] = {name: [] for name in SECTION_ORDER}
    current: str | None = None
    marked = False  # True while inside an explicit ..directive block
    for raw_line in _DIRECTIVE_INLINE.sub(r"\n\1\n", clean(plain)).split("\n"):
        line = raw_line.strip()
        key = line.lower()
        if key in _DIRECTIVES:
            current = _DIRECTIVES[key]
            marked = current is not None
            continue
        if key == "item description:":
            continue
        if key in ("resolution:", "whereas:"):
            if current is None:
                current = "resolution"
            continue
        if key == "background:":
            current, marked = "background", False
            continue
        if not marked:
            background = re.match(r"(?i)background:\s*(.+)", line)
            if background and current in (None, "resolution"):
                current, raw_line = "background", background.group(1)
            elif current is None and _RESOLUTION_OPENING.match(line):
                current = "resolution"
        buckets[current or "other"].append(raw_line.rstrip())
    sections = {name: clean("\n".join(lines)) for name, lines in buckets.items()}
    return {name: text for name, text in sections.items() if text}


def administrator_recommendation(recommendation_section: str | None) -> str | None:
    match = re.search(r"Recommendation from County Administrator:\s*(.+)", recommendation_section or "")
    value = match.group(1).strip() if match else None
    return None if not value or value.lower() in _PLACEHOLDER_RECOMMENDATIONS else value


# --- Minnesota Statutes citations -------------------------------------------------
# A chapter is 1-3 digits plus an optional capital letter (275, 13D, 383B); a
# section adds a dotted suffix (275.065, 383B.158). Four-digit numbers are years.
_NUM = r"(?<![\d.])\d{1,3}[A-Z]?(?:\.\d+)?(?![\dA-Za-z])"
_ANCHOR = r"(?:(?i:Minn(?:esota)?\.?\s+Stat(?:ute)?s?\.?)|M\.S\.A?\.?)"
_UNIT = r"(?i:§+|sections?|sec\.|chapters?|ch\.)"
_SEPARATOR = r"(?:\s*,\s*|\s+(?i:and|to|through)\s+|\s*&\s*)"
_CITATION = re.compile(
    rf"{_ANCHOR}(?:,?\s+(?i:Annotated))?(?:,?\s+\d{{4}})?[\s,]*(?:{_UNIT}\s*)?"
    rf"(?P<first>{_NUM})(?P<rest>(?:{_SEPARATOR}(?:{_UNIT}\s*)?{_NUM})*)"
)
_CONTINUATION = re.compile(rf"({_UNIT})?\s*({_NUM})")


def statute_chapter(number: str) -> str:
    return number.split(".", 1)[0]


def extract_statute_citations(text: str | None) -> list[tuple[str, str]]:
    """Return ``(number, citation_text)`` for each Minnesota Statutes chapter or
    section cited, in order of first appearance, e.g. ``("275.065", "Minnesota
    Statutes, section 275.065")``. Subdivisions are not captured."""
    found: dict[str, str] = {}
    for match in _CITATION.finditer(text or ""):
        citation = re.sub(r"\s+", " ", match.group(0))
        first = match.group("first")
        numbers = [first]
        for unit, number in _CONTINUATION.findall(match.group("rest")):
            # "section 383B.77 and 3 agreements": after a dotted section, a bare
            # integer is only a citation if it is explicitly called a chapter.
            if "." in first and number.isdigit() and not unit.lower().startswith("ch"):
                continue
            numbers.append(number)
        for number in numbers:
            found.setdefault(number, citation)
    return list(found.items())


# --- references to other matters by file number -------------------------------------
# 26-0333, 26-0333 R1, 25-0312 R1 S1, 23N-0018, 24-HCRRA-0003, 23-HHS-0041R1
_FILE_NUMBER = re.compile(r"(?<![\w-])\d{2}N?-(?:(?:HCHRA|HCRRA|HHS)-)?\d{4}(?:\s?[RS]\d)*(?![\w-])")


def normalize_file_number(file_number: str | None) -> str:
    return re.sub(r"\s+", "", file_number or "").upper()


def extract_file_numbers(text: str | None) -> list[str]:
    """Normalised file numbers mentioned in ``text``, in order of first appearance."""
    seen: dict[str, None] = {}
    for match in _FILE_NUMBER.finditer(text or ""):
        seen.setdefault(normalize_file_number(match.group(0)))
    return list(seen)


def base_file_number(file_number: str | None) -> str:
    """``26-0333 R1`` -> ``26-0333``: the file number without revision suffixes."""
    return re.sub(r"(?:[RS]\d)+$", "", normalize_file_number(file_number))


# --- chunking for the vector index ---------------------------------------------------
_SENTENCE_END = re.compile(r"(?<=[.;:?!])\s+")


def _units(text: str, max_chars: int) -> list[str]:
    """Paragraphs, with any paragraph longer than ``max_chars`` broken at sentence
    ends and, failing that, at word boundaries."""
    units: list[str] = []
    for paragraph in (p.strip() for p in re.split(r"\n+", text)):
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            units.append(paragraph)
            continue
        for sentence in _SENTENCE_END.split(paragraph):
            while len(sentence) > max_chars:
                cut = sentence.rfind(" ", 0, max_chars)
                cut = cut if cut > 0 else max_chars
                units.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if sentence:
                units.append(sentence)
    return units


def chunk_text(text: str | None, max_chars: int = 2400) -> list[str]:
    """Split ``text`` into passages of at most ``max_chars``, breaking between
    paragraphs and keeping the passages about the same size.

    Text up to 25% over the limit stays whole: most resolution and background
    sections fit in one passage, and a short orphaned tail retrieves badly.
    """
    text = clean(text)
    if not text:
        return []
    if len(text) <= max_chars * 1.25:
        return [text]
    units = _units(text, max_chars)
    total = sum(len(unit) for unit in units) + len(units) - 1
    target = total / -(-total // max_chars)  # even split over the fewest chunks
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if current and (size + 1 + len(unit) > max_chars or size >= target):
            chunks.append("\n".join(current))
            current, size = [], 0
        size += len(unit) + (1 if current else 0)
        current.append(unit)
    if current:
        chunks.append("\n".join(current))
    return chunks


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
