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


_SUBDIVISION = re.compile(
    r"(?P<section>(?<![\d.])\d{1,3}[A-Z]?\.\d+)\s*,?\s*(?P<unit>(?i:subdivisions?|subds?\.?))\s*"
    r"(?P<first>\d+[a-z]?)(?![\dA-Za-z])(?P<rest>(?:\s*(?:,|,?\s*(?i:and)|&)\s*\d+[a-z]?(?![\dA-Za-z]))*)"
)


def extract_subdivision_citations(text: str | None) -> list[tuple[str, str, str]]:
    """``(section, subdivision, citation_text)`` for each subdivision cited, e.g.
    ``("383B.158", "4", "383B.158, subdivision 4")``. Only sections that
    :func:`extract_statute_citations` also finds are kept, so a decimal number that
    merely looks like a section is not mistaken for one."""
    sections = {number for number, _ in extract_statute_citations(text)}
    found: dict[tuple[str, str], str] = {}
    for match in _SUBDIVISION.finditer(text or ""):
        section = match.group("section")
        if section not in sections:
            continue
        numbers = [match.group("first")]
        if match.group("unit").lower().rstrip(".").endswith("s"):  # "subdivisions 1, 2 and 3"
            numbers += re.findall(r"\d+[a-z]?", match.group("rest"))
        for number in numbers:
            found.setdefault((section, number), re.sub(r"\s+", " ", match.group(0)))
    return [(section, number, citation) for (section, number), citation in found.items()]


# "Laws of Minnesota 2023, Chapter 64, Article 8, Section 3", "Minnesota Session Laws 2024,
# Regular Session, Chapter 127", "Laws of Minnesota 2025, 1st Special Session, Chapter 13"
_SPECIAL_SESSIONS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
_SESSION_LAW = re.compile(
    r"(?i)\b(?:session\s+)?laws\s+(?:of\s+minnesota,?\s*)?(?:for\s+|of\s+)?(?P<year>(?:19|20)\d{2}),?\s*"
    r"(?:(?:regular|(?P<special>first|1st|second|2nd|third|3rd)\s+special)\s+session,?\s*)?"
    r"chapter\s+(?P<chapter>\d+)"
    r"(?:\s*\([^)]{0,20}\))?(?:,?\s*chapter\s+\d+)?"
    r"(?:,?\s*art(?:icle|\.)\s*\d+)?(?:,?\s*(?:sections?|sec\.?)\s*\d+(?:\s*(?:-|and|,)\s*\d+)*)?"
)


def extract_session_law_citations(text: str | None) -> list[tuple[int, int, str, str]]:
    """``(year, special_session, chapter, citation_text)`` for each session law cited;
    ``special_session`` is 0 for a regular session."""
    found: dict[tuple[int, int, str], str] = {}
    for match in _SESSION_LAW.finditer(text or ""):
        special = _SPECIAL_SESSIONS.get((match.group("special") or "").lower(), 0)
        key = (int(match.group("year")), special, match.group("chapter"))
        citation = re.sub(r"\s+", " ", match.group(0)).strip(" ,")
        if len(citation) > len(found.get(key, "")):  # keep the fullest wording: article and section
            found[key] = citation
    return [(year, special, chapter, citation) for (year, special, chapter), citation in found.items()]


# "Ordinance 13", "Ordinance No. 26", "Recycling Ordinance Number 13". County ordinances
# are numbered 1 to 99; a year ("Ordinance 2023-14") belongs to some other body.
_ORDINANCE = re.compile(r"(?i)\bordinance\s+(?:no\.?\s*|number\s+|#\s*)?(?P<number>\d{1,2})(?![\d-])")


def extract_ordinance_citations(text: str | None) -> list[tuple[str, str]]:
    """``(number, citation_text)`` for each county ordinance cited by number."""
    found: dict[str, str] = {}
    for match in _ORDINANCE.finditer(text or ""):
        found.setdefault(match.group("number"), re.sub(r"\s+", " ", match.group(0)))
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
