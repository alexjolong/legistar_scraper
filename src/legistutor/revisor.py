"""Minnesota Statutes and session laws, read from the Revisor of Statutes' website.

Nothing here knows about any county. The Revisor publishes no API or bulk data, so
this reads four kinds of page, all allowed by the site's ``robots.txt``:

* ``/statutes/cite/<chapter>/full``: a whole chapter: every section with its
  subdivisions and ``History:`` line, and a stub for each repealed section;
* ``/statutes/table2?year=<year>``: "Statutes New, Amended or Repealed": one
  row per section or subdivision a session touched, with the session law that did it;
* ``/laws/<year>/<special>/``: the session laws of one session with bill and
  presentment date;
* ``/laws/<year>/<special>/Session+Law/Chapter/<n>/``: the text of one session law,
  whose sections show an amendment as struck and inserted text.

Pages are kept as fetched under ``<data_dir>/bronze/mnrevisor/``; the parsers below
are pure functions over those bytes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from lxml import html as lxml_html

from .fetch import Fetcher, PageStore

log = logging.getLogger(__name__)

SOURCE = "mnrevisor"
BASE = "https://www.revisor.mn.gov"
# From https://www.revisor.mn.gov/robots.txt (read 2026-11-11). Bill and document
# *search results* are off limits; statutes, laws and individual bill pages are not.
DISALLOW = ("/alpha/", "/beta/", "/search/doc_result.php", "/bills/status_results.php", "/rules/status/result")
TABLE2_PAGE_SIZE = 500
NUMBER = r"\d{1,3}[A-Z]?(?:\.\d+[A-Za-z]?)?"


def fetcher(**kwargs: Any) -> Fetcher:
    return Fetcher(disallow=DISALLOW, **kwargs)


def store(data_dir) -> PageStore:
    return PageStore(data_dir, SOURCE)


# --- ids ------------------------------------------------------------------------------


def statute_id(number: str, subdivision: str | None = None) -> str:
    """``mnstat:383B.158`` for a chapter or section, ``mnstat:383B.158#4`` for a subdivision."""
    return f"mnstat:{number}#{subdivision}" if subdivision else f"mnstat:{number}"


def session_law_id(year: int, chapter: int | str, special: int = 0) -> str:
    """``mnlaw:2026:88``; a special session's laws are ``mnlaw:2025s1:3``."""
    return f"mnlaw:{year}{f's{special}' if special else ''}:{chapter}"


def session_law_url(year: int, chapter: int | str, special: int = 0) -> str:
    return f"{BASE}/laws/{year}/{special}/Session+Law/Chapter/{chapter}/"


def statute_url(number: str, subdivision: str | None = None) -> str:
    return f"{BASE}/statutes/cite/{number}" + (f"#stat.{number}.{subdivision}" if subdivision else "")


def session_law_label(year: int, special: int, chapter: int | str) -> str:
    ordinal = {1: "1st", 2: "2nd", 3: "3rd"}.get(special, f"{special}th")
    return f"Laws {year}, {f'{ordinal} Spec. Sess., ' if special else ''}ch. {chapter}"


def label(kind: str, row: dict[str, Any]) -> str:
    """How a statute, subdivision or session law vertex is named to a reader."""
    if kind == "statute":
        return f"Minn. Stat. {'§ ' if row['kind'] == 'section' else 'ch. '}{row['number']}"
    if kind == "subdivision":
        return f"Minn. Stat. § {row['section']}, subd. {row['subdivision']}"
    return session_law_label(row["year"], row["special"], row["chapter"])


# --- chapters ---------------------------------------------------------------------------


@dataclass
class Subdivision:
    number: str  # "1", "2a"
    headnote: str | None
    text: str
    status: str  # "in force", "repealed", "renumbered", "expired"
    references: list[str] = field(default_factory=list)  # statute numbers named in the text


@dataclass
class Section:
    number: str  # "383B.158"
    headnote: str | None
    group: str | None  # the heading the chapter files it under, e.g. "DESIGN-BUILD CONTRACTS"
    status: str
    note: str | None  # for a repealed section: "MS 2022 [Repealed, 2024 c 112 art 2 s 81]"
    text: str  # text outside any subdivision; the whole section when it has none
    subdivisions: list[Subdivision] = field(default_factory=list)
    history: str | None = None  # "2002 c 393 s 57; 2007 c 70 s 1-3; ..."
    history_laws: list[tuple[int, int, str]] = field(default_factory=list)  # (year, special session, chapter)
    references: list[str] = field(default_factory=list)


@dataclass
class Chapter:
    number: str
    title: str | None
    edition: int | None  # the statutes edition the page shows, e.g. 2026
    sections: list[Section]


_STATUS_NOTE = re.compile(r"\[\s*(Repealed|Renumbered|Expired|Obsolete|Unconstitutional|Superseded|Omitted|Not coded)", re.I)
_STATUS = {"not coded": "omitted", "obsolete": "expired", "superseded": "repealed"}
_CITE_HREF = re.compile(rf"/statutes/(?:\d{{4}}/)?cite/({NUMBER})(?:[/#?]|$)")
_LAW_HREF = re.compile(r"/laws/(\d{4})/(\d+)/(?:Session\+Law/Chapter/)?(\d+)/?")
_CHAPTER_WORDS = re.compile(rf"\b[Cc]hapters?\s+((?:{NUMBER})(?:(?:,\s*(?:and\s+|or\s+)?|\s+(?:and|or|to)\s+)(?:{NUMBER}))*)")
_NUMBER = re.compile(NUMBER)


def _squash(value: str | None) -> str:
    return " ".join((value or "").replace(" ", " ").split())


def _status(text: str) -> str:
    match = _STATUS_NOTE.match(text.strip())
    if not match:
        return "in force"
    word = match.group(1).lower()
    return _STATUS.get(word, word)


def _has_class(name: str) -> str:
    return f'contains(concat(" ", normalize-space(@class), " "), " {name} ")'


def _block_text(element, skip: tuple[str, ...] = ()) -> str:
    """Readable text of a section or subdivision: one line per paragraph, table rows
    joined with " | ". Child blocks whose class is in ``skip`` are left out."""
    lines: list[str] = []
    for child in element:
        if not isinstance(child.tag, str):
            continue
        classes = (child.get("class") or "").split()
        if any(name in classes for name in skip) or child.tag in ("h1", "h2") or "permalink" in classes:
            continue
        if child.tag == "table":
            for row in child.iter("tr"):
                cells = [_squash(cell.text_content()) for cell in row if cell.tag in ("td", "th")]
                cells = [cell for cell in cells if cell.strip(". ")]
                if cells:
                    lines.append(" | ".join(cells))
        else:
            text = _squash(child.text_content())
            if text:
                lines.append(text)
    return "\n".join(lines)


def _references(element, own: str) -> list[str]:
    """Statute numbers the text points to: linked sections, and chapters named in words."""
    found: dict[str, None] = {}
    for href in element.xpath(".//a/@href"):
        match = _CITE_HREF.search(href)
        if match:
            found.setdefault(match.group(1))
    for match in _CHAPTER_WORDS.finditer(element.text_content()):
        for number in _NUMBER.findall(match.group(1)):
            if "." not in number:
                found.setdefault(number)
    found.pop(own, None)
    return list(found)


def _headnote(value: str) -> str | None:
    value = _squash(value)
    return value.rstrip(".") or None if not value.endswith("...") else value


def parse_chapter(content: bytes) -> Chapter:
    """A ``/statutes/cite/<chapter>/full`` page. A single-section page parses the same way."""
    doc = lxml_html.fromstring(content)
    edition = None
    for heading in doc.xpath('//h1[contains(., "Minnesota Statutes")]'):
        match = re.search(r"(\d{4})\s+Minnesota Statutes", heading.text_content())
        if match:
            edition = int(match.group(1))
            break
    title_text = _squash("".join(doc.xpath(f'//h2[{_has_class("chapter_title")}]//text()')))
    title_match = re.match(rf"CHAPTER\s+({NUMBER})\.?\s*(.*)", title_text)
    sections: list[Section] = []
    group: str | None = None
    wanted = (
        f'//h1[{_has_class("fgh")}] | //div[{_has_class("section")}] | //div[{_has_class("sr")}]'
        f' | //div[{_has_class("history")}]'
    )
    for element in doc.xpath(wanted):
        classes = (element.get("class") or "").split()
        if element.tag == "h1":
            group = _squash(element.text_content()) or None
        elif "sr" in classes:  # a repealed or renumbered section: number and a bracketed note
            number = (element.get("id") or "").removeprefix("stat.")
            note = _squash(element.text_content()).removeprefix(number).strip()
            section = Section(number, None, group, _status(note[note.find("[") :] if "[" in note else note), note or None, "")
            for href in element.xpath(".//a/@href"):
                law = _LAW_HREF.search(href)
                if law:
                    section.history_laws.append((int(law.group(1)), int(law.group(2)), law.group(3)))
            if number:
                sections.append(section)
        elif "history" in classes:
            if not sections:
                continue
            section = sections[-1]
            section.history = _squash(element.text_content()).removeprefix("History:").strip() or None
            for href in element.xpath(".//a/@href"):
                law = _LAW_HREF.search(href)
                if law and (key := (int(law.group(1)), int(law.group(2)), law.group(3))) not in section.history_laws:
                    section.history_laws.append(key)
        else:
            number = (element.get("id") or "").removeprefix("stat.")
            if not number:
                continue
            heading = _squash("".join(element.xpath(f'./h1[{_has_class("shn")}]//text()')))
            section = Section(
                number,
                _headnote(heading.removeprefix(number)),
                group,
                "in force",
                None,
                _block_text(element, skip=("subd", "history")),
            )
            section.references = _references(element, number)
            for subd in element.xpath(f'./div[{_has_class("subd")}]'):
                subd_number = (subd.get("id") or "").removeprefix(f"stat.{number}.")
                body = _block_text(subd)
                section.subdivisions.append(
                    Subdivision(
                        subd_number,
                        _headnote("".join(subd.xpath(f'./h2//span[{_has_class("headnote")}]//text()'))),
                        body,
                        _status(body),
                        _references(subd, number),
                    )
                )
            if not section.subdivisions and _STATUS_NOTE.match(section.text):
                section.status = _status(section.text)
            sections.append(section)
    return Chapter(
        number=title_match.group(1) if title_match else (sections[0].number.split(".")[0] if sections else ""),
        title=(title_match.group(2).strip() or None) if title_match else None,
        edition=edition,
        sections=sections,
    )


# --- Table 2: statutes new, amended or repealed -------------------------------------


@dataclass
class Change:
    statute: str  # "383B.041"
    subdivision: str | None
    action: str  # New, Amended, Repealed, Renumbered, Revisor Instruction, Other ...
    year: int
    special: int  # 0 for the regular session
    law_chapter: str
    article: str | None
    section: str | None
    law_anchor: str | None  # "laws.1.226.0": the place in the session law's page


def parse_table2(content: bytes) -> tuple[int | None, list[Change]]:
    """Rows of one Table 2 page, and the total number of records the query matched."""
    doc = lxml_html.fromstring(content)
    counted = re.search(r"(?:of\s+)?([\d,]+)\s+Records", _squash("".join(doc.xpath('//form[@id="form1"]//text()'))))
    changes: list[Change] = []
    for row in doc.xpath(f'//tr[{_has_class("table2")}]'):
        cells = row.findall("td")
        if len(cells) < 9:
            continue
        text = [_squash(cell.text_content()) for cell in cells]
        law = None
        for href in cells[6].xpath(".//a/@href"):
            law = re.search(r"/laws/(\d{4})/(\d+)/(\d+)/(laws\.[\d.]+)?", href)
        if not law or not text[2]:
            continue
        changes.append(
            Change(
                statute=text[2],
                subdivision=text[3] or None,
                action=text[4],
                year=int(law.group(1)),
                special=int(law.group(2)),
                law_chapter=law.group(3),
                article=text[7] or None,
                section=text[8] or None,
                law_anchor=law.group(4),
            )
        )
    return (int(counted.group(1).replace(",", "")) if counted else None), changes


# --- session laws -----------------------------------------------------------------------


@dataclass
class SessionLaw:
    year: int
    special: int
    chapter: str
    bill: str | None  # "SF4244"
    presented: date | None
    vetoed: str | None


def parse_session_laws(content: bytes) -> list[SessionLaw]:
    """The list of one session's laws."""
    doc = lxml_html.fromstring(content)
    laws: list[SessionLaw] = []
    for row in doc.xpath('//table[@id="session_table"]//tr[td]'):
        cells = row.findall("td")
        link = None
        for href in cells[0].xpath(".//a/@href"):
            link = _LAW_HREF.search(href)
        if not link or len(cells) < 3:
            continue
        presented = None
        try:
            presented = datetime.strptime(_squash(cells[2].text_content()), "%B %d, %Y").date()
        except ValueError:
            pass
        laws.append(
            SessionLaw(
                year=int(link.group(1)),
                special=int(link.group(2)),
                chapter=link.group(3),
                bill=re.sub(r"\s+", "", cells[1].text_content()) or None,
                presented=presented,
                vetoed=(_squash(cells[3].text_content()) or None) if len(cells) > 3 else None,
            )
        )
    return laws


@dataclass
class LawSection:
    anchor: str  # "laws.1.226.0"
    kind: str  # am_subd, am_cite, repealer, new, uncoded, session_laws ...
    label: str  # "Sec. 226."
    heading: str | None  # "Minnesota Statutes 2024, section 256D.05, subdivision 8, is amended to read:"
    marked: str  # the text; struck words as [-...-], inserted words as {+...+}
    effective: str | None  # the section's own "EFFECTIVE DATE." sentence, when it has one


@dataclass
class Law:
    title: str | None  # "CHAPTER 88--S.F.No. 4244"
    summary: str | None  # "relating to ...;"
    sections: dict[str, LawSection]


def _marked(element) -> str:
    """Text with deletions and insertions kept visible."""
    parts: list[str] = []

    def walk(node) -> None:
        classes = (node.get("class") or "").split()
        if "sr-only" in classes or "permalink" in classes:
            parts.append(node.tail or "")
            return
        style = (node.get("style") or "").replace(" ", "")
        opening, closing = "", ""
        if node.tag in ("del", "strike", "s") or "del" in classes or "line-through" in style:
            opening, closing = "[-", "-]"
        elif node.tag == "ins" or "ins" in classes:
            opening, closing = "{+", "+}"
        parts.append(opening + (node.text or ""))
        for child in node:
            if isinstance(child.tag, str):
                walk(child)
            else:
                parts.append(child.tail or "")
        parts.append(closing + (node.tail or ""))

    lines: list[str] = []
    for child in element:
        if not isinstance(child.tag, str) or "bill_sec_no" in (child.get("class") or ""):
            continue
        parts = []
        walk(child)
        if child.tail and parts:
            parts[-1] = parts[-1][: len(parts[-1]) - len(child.tail)]
        blocks = "".join(parts)
        # nested blocks (subdivisions, repeal groups) keep their own paragraph breaks
        if child.tag == "div":
            inner = _marked(child)
            if inner:
                lines.append(inner)
            continue
        text = _squash(blocks)
        if text:
            lines.append(text)
    merged = "\n".join(lines)
    # adjacent runs of the same kind read better as one
    merged = re.sub(r"\+\}(\s*)\{\+", r"\1", merged)
    return re.sub(r"-\](\s*)\[-", r"\1", merged)


def parse_law(content: bytes) -> Law:
    """A session law's page: its title and each section, keyed by anchor."""
    doc = lxml_html.fromstring(content)
    sections: dict[str, LawSection] = {}
    for element in doc.xpath(f'//div[{_has_class("bill_section")}]'):
        anchor = element.get("id") or ""
        if not anchor:
            continue
        kind = " ".join(name for name in (element.get("class") or "").split() if name != "bill_section") or "section"
        heading = _squash("".join(element.xpath(f'./h1[{_has_class("bill_sec_header")}]//text()'))) or None
        marked = _marked(element)
        if heading and marked.startswith(heading):
            marked = marked[len(heading) :].lstrip()
        effective = re.search(r"EFFECTIVE DATE(?:;[ A-Z]+)?[.:]?(?:\+\})?\s*(?:\{\+)?\s*([^\n]+)", marked)
        sections[anchor] = LawSection(
            anchor=anchor,
            kind=kind,
            label=_squash("".join(element.xpath(f'./h1[{_has_class("bill_sec_no")}]//text()'))),
            heading=heading,
            marked=marked,
            effective=re.sub(r"[{}+\[\]-]{2}", "", effective.group(1)).strip() if effective else None,
        )
    summary = _squash("".join(doc.xpath(f'//p[{_has_class("btitle_summary")}]//text()'))) or None
    title = _squash("".join(doc.xpath(f'//div[@id="xtend"]/h1[{_has_class("title")}]//text()'))) or None
    return Law(title=title, summary=summary, sections=sections)


# --- ingest -------------------------------------------------------------------------------


def chapter_key(chapter: str, edition: int | str = "current") -> str:
    return f"statutes/{edition}/{chapter}.html"


def ingest_chapters(
    pages: PageStore, http: Fetcher, chapters: list[str], *, max_age_days: float = 30, full: bool = False
) -> dict[str, int]:
    """Whole-chapter pages for ``chapters``. The statutes are republished once a year,
    with corrections in between, so a page is fetched again once it is a month old.
    Each page is also filed under its edition (``statutes/2026/383B.html``) so that
    earlier editions stay on disk when a new one replaces them."""
    report = {"chapters": len(chapters), "fetched": 0, "skipped": 0, "missing": 0, "failed": 0}
    for chapter in chapters:
        key = chapter_key(chapter)
        age = pages.age_days(key)
        missing_age = pages.age_days(key + ".missing")
        if not full and ((age is not None and age < max_age_days) or (missing_age is not None and missing_age < max_age_days)):
            report["skipped"] += 1
            continue
        try:
            content = pages.save(key, http, f"{BASE}/statutes/cite/{chapter}/full")
        except Exception:  # noqa: BLE001 - keep the older copy, try again next run
            log.exception("statutes: failed on chapter %s", chapter)
            report["failed"] += 1
            continue
        if content is None:
            log.warning("statutes: there is no chapter %s", chapter)
            report["missing"] += 1
            continue
        edition = parse_chapter(content).edition
        if edition:
            pages.write(chapter_key(chapter, edition), content, url=f"{BASE}/statutes/{edition}/cite/{chapter}/full")
        report["fetched"] += 1
        log.info("statutes: chapter %s (%s edition)", chapter, edition)
    return report


def ingest_table2(pages: PageStore, http: Fetcher, years: list[int], *, refresh_from: int | None = None) -> dict[str, int]:
    """Every row of Table 2 for each of ``years``. A finished session's table does not
    change, so it is fetched once; years from ``refresh_from`` on are fetched again."""
    report = {"years": len(years), "fetched_pages": 0, "skipped_years": 0, "rows": 0}
    for year in years:
        done = pages.meta(f"table2/{year}/1.html").get("complete")
        if done and (refresh_from is None or year < refresh_from):
            report["skipped_years"] += 1
            continue
        page, total, seen = 1, None, 0
        while True:
            key = f"table2/{year}/{page}.html"
            params = {"year": year, "full": "", "per_page": TABLE2_PAGE_SIZE, "page": page}
            response = http.get(f"{BASE}/statutes/table2", params)
            if response is None:
                break
            found, rows = parse_table2(response.content)
            total = found if found is not None else total
            pages.write(key, response.content, url=str(response.url), rows=len(rows), total=total)
            report["fetched_pages"] += 1
            seen += len(rows)
            if not rows or len(rows) < TABLE2_PAGE_SIZE or (total is not None and seen >= total):
                break
            page += 1
        report["rows"] += seen
        # mark the first page so the next run can tell the year was read to the end
        first = f"table2/{year}/1.html"
        if pages.exists(first):
            pages.write(first, pages.read(first), **{**pages.meta(first), "complete": total is None or seen >= total, "pages": page})
        log.info("table 2: %d: %d rows on %d pages", year, seen, page)
    return report


def sessions_in_table2(pages: PageStore) -> set[tuple[int, int]]:
    """``(year, special session)`` of every law Table 2 names: how special sessions are found."""
    return {(c.year, c.special) for key in pages.keys("table2") for c in parse_table2(pages.read(key))[1]}


def ingest_session_laws(pages: PageStore, http: Fetcher, years: list[int], *, refresh_from: int | None = None) -> dict[str, int]:
    """The list of laws for each year's regular session, and for each special session
    that changed a statute."""
    report = {"lists": 0, "skipped": 0}
    sessions = {(year, 0) for year in years} | {s for s in sessions_in_table2(pages) if s[0] in years}
    for year, special in sorted(sessions):
        key = f"laws/{year}/{special}/index.html"
        if pages.exists(key) and (refresh_from is None or year < refresh_from):
            report["skipped"] += 1
        elif pages.save(key, http, f"{BASE}/laws/{year}/{special}/"):
            report["lists"] += 1
    return report


def law_key(year: int, chapter: str | int, special: int = 0) -> str:
    return f"laws/{year}/{special}/chapter/{chapter}.html"


def ingest_laws(pages: PageStore, http: Fetcher, laws: list[tuple[int, int, str]]) -> dict[str, int]:
    """The text of the given session laws, as ``(year, special, chapter)``. An enacted
    law never changes, so each is fetched once."""
    report = {"laws": len(laws), "fetched": 0, "skipped": 0, "failed": 0}
    for year, special, chapter in laws:
        key = law_key(year, chapter, special)
        if pages.exists(key) or pages.exists(key + ".missing"):
            report["skipped"] += 1
            continue
        try:
            pages.save(key, http, session_law_url(year, chapter, special))
            report["fetched"] += 1
        except Exception:  # noqa: BLE001
            log.exception("laws: failed on %s chapter %s", year, chapter)
            report["failed"] += 1
    return report
