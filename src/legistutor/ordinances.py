"""County ordinances, read from the county's own website.

The county publishes its ordinances as ordinary web pages: an index, a page per
category, and a page per ordinance whose text is HTML under ``h2`` headings. There
is no API. Pages are kept as fetched under ``bronze/ordinances/<client>/`` and the
graph tables are written to ``silver/ordinances/<client>/``:

* vertex ``ordinance`` (``hennepinmn:ordinance:13``; a page with no number is keyed
  by its address, ``hennepinmn:ordinance:wheelage-tax-resolution``);
* edge ``ordinance_cites`` (ordinance -> statute), marked when the citation is in the
  opening lines, where an ordinance states the authority it is adopted under;
* table ``ordinance_text``: one row per headed section.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from lxml import html as lxml_html

from . import revisor
from . import text as tx
from .fetch import Fetcher, PageStore
from .silver import GRAPH_EDGES, GRAPH_VERTICES, _schema, _write

log = logging.getLogger(__name__)

# Where each county lists its ordinances. robots.txt for hennepincounty.gov (read
# 2026-11-11) disallows only /choose-to-reuse/.
SITES = {"hennepinmn": "https://www.hennepincounty.gov/government/about/ordinances"}

VERTEX_SCHEMAS = {
    "ordinance": _schema(
        "id:s number:s title:s category:s url:s pdf_url:s adopted:d amended:d notice:s summary:s sections:i"
    ),
    # stubs for the statutes ordinances cite, in the shape the county graph uses
    "statute": _schema("id:s number:s kind:s chapter:s url:s"),
}
EDGE_SCHEMAS = {
    "ordinance_cites": _schema("src:s dst:s citation:s states_authority:b"),
    "part_of": _schema("src:s dst:s"),
}
TABLE_SCHEMAS = {"ordinance_text": _schema("ordinance:s position:i heading:s text:s")}


def store(data_dir: Path, client: str) -> PageStore:
    return PageStore(data_dir, f"ordinances/{client}")


def silver_root(data_dir: Path, client: str) -> Path:
    return Path(data_dir) / "silver" / "ordinances" / client


# --- parsing ------------------------------------------------------------------------------


def child_links(content: bytes, page_url: str) -> list[str]:
    """Links one level below ``page_url``: categories on the index, ordinances on a category."""
    doc = lxml_html.fromstring(content)
    base = urlsplit(page_url).path.rstrip("/") + "/"
    found: dict[str, None] = {}
    for href in doc.xpath("//a/@href"):
        path = urlsplit(urljoin(page_url, href)).path.rstrip("/")
        if path.startswith(base) and "/" not in path[len(base) :] and path[len(base) :]:
            found.setdefault(urljoin(page_url, path))
    return list(found)


@dataclass
class Ordinance:
    title: str | None
    number: str | None
    adopted: date | None
    amended: date | None  # the latest amendment the page lists
    pdf_url: str | None
    notice: str | None  # a banner on the page, e.g. that the ordinance is being revised
    summary: str | None  # the opening lines, before the first heading
    sections: list[tuple[str | None, str]] = field(default_factory=list)  # (heading, text)


_DATE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})"
)


def _dates(line: str) -> list[date]:
    found = []
    for month, day, year in _DATE.findall(line):
        try:
            found.append(datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date())
        except ValueError:
            pass
    return found


def _lines(element) -> list[tuple[str, str]]:
    """Headings and text blocks under ``element``, in order, as ``(tag, text)``."""
    out: list[tuple[str, str]] = []
    for node in element.iter("h2", "h3", "h4", "p", "li", "tr"):
        if node.tag in ("p", "li") and any(a.tag in ("li", "tr") for a in node.iterancestors()):
            continue  # already part of the enclosing list item or table row
        if node.tag == "tr":
            text = " | ".join(c for c in (" ".join(cell.text_content().split()) for cell in node) if c)
        else:
            text = " ".join(node.text_content().replace(" ", " ").split())
        if text:
            out.append((node.tag, text))
    return out


def parse_ordinance(content: bytes, page_url: str) -> Ordinance:
    doc = lxml_html.fromstring(content)
    title = " ".join("".join(doc.xpath('//h1[@id="bannerHeadingID"]//text()') or doc.xpath("//h1[1]//text()")).split()) or None
    banner = " ".join(" ".join(doc.xpath('//h1[@id="bannerHeadingID"]/ancestor::section[1]//text()')).split())
    described = " ".join(doc.xpath('//meta[@name="description"]/@content'))
    number = next(
        (m.group(1) for source in (banner, described) if (m := re.search(r"(?i)\bordinance\s+(?:no\.?\s*|number\s+)?(\d{1,2})\b", source))),
        None,
    )
    ordinance = Ordinance(title, number, None, None, None, None, None)
    for href in doc.xpath('//a[contains(translate(@href, "PDF", "pdf"), ".pdf")]/@href'):
        ordinance.pdf_url = urljoin(page_url, href)
        break
    notices = doc.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " hc-in-page-alert ")]')
    if notices:
        ordinance.notice = " ".join(notices[0].text_content().split()) or None

    heading: str | None = None
    body: list[str] = []
    opening: list[str] = []
    seen_heading = False

    def close() -> None:
        if body:
            ordinance.sections.append((heading, "\n".join(body)))

    for block in doc.xpath('//section[contains(concat(" ", normalize-space(@class), " "), " hc-rich-text ")]'):
        for tag, text in _lines(block):
            if tag == "h2":
                close()
                heading, body, seen_heading = text, [], True
            elif tag in ("h3", "h4"):
                body.append(text)
            else:
                body.append(text)
                if not seen_heading:
                    opening.append(text)
    close()
    # "Adopted by the ... Board of Commissioners / On June 6, 1995 / Amended June 26, 2018": the
    # date may sit on the line after the word, so the opening is read as one run of text.
    run = " ".join(opening)
    adopted = re.search(rf"(?i)\b(?:adopted|enacted)\b[^.;]{{0,90}}?({_DATE.pattern})", run)
    if adopted:
        ordinance.adopted = (_dates(adopted.group(1)) or [None])[0]
    amendments = [
        day for match in re.finditer(rf"(?i)\b(?:amended|revised)\b[^.;]{{0,30}}?({_DATE.pattern})", run) for day in _dates(match.group(1))
    ]
    ordinance.amended = max(amendments) if amendments else None
    ordinance.summary = "\n".join(opening) or None
    return ordinance


# --- ingest -------------------------------------------------------------------------------


def _slug(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


def ingest(data_dir: Path, client: str, http: Fetcher, *, max_age_days: float = 7, full: bool = False) -> dict[str, int]:
    """Index, category pages and every ordinance page. The set is small (about thirty
    pages), so it is read again each week to notice amendments."""
    if client not in SITES:
        raise KeyError(f"No ordinance site is configured for {client!r}")
    pages = store(data_dir, client)
    index_url = SITES[client]
    report = {"categories": 0, "ordinances": 0, "fetched": 0, "skipped": 0, "failed": 0}

    def page(key: str, url: str) -> bytes | None:
        age = pages.age_days(key)
        if not full and age is not None and age < max_age_days:
            report["skipped"] += 1
            return pages.read(key)
        try:
            content = pages.save(key, http, url)
        except Exception:  # noqa: BLE001 - keep the older copy
            log.exception("ordinances: failed on %s", url)
            report["failed"] += 1
            return pages.read(key)
        report["fetched"] += 1
        return content

    index = page("index.html", index_url)
    for category_url in child_links(index, index_url) if index else []:
        category = page(f"categories/{_slug(category_url)}.html", category_url)
        report["categories"] += 1
        for ordinance_url in child_links(category, category_url) if category else []:
            page(f"pages/{_slug(category_url)}/{_slug(ordinance_url)}.html", ordinance_url)
            report["ordinances"] += 1
    return report


# --- silver -------------------------------------------------------------------------------


def build(data_dir: Path, client: str) -> dict[str, Any]:
    pages = store(data_dir, client)
    vertices: dict[str, list[dict[str, Any]]] = {name: [] for name in VERTEX_SCHEMAS}
    edges: dict[str, list[dict[str, Any]]] = {name: [] for name in EDGE_SCHEMAS}
    texts: list[dict[str, Any]] = []
    statutes: set[str] = set()
    for key in pages.keys("pages"):
        url = pages.meta(key).get("url") or ""
        category = key.split("/")[1].removesuffix("-ordinances").replace("-", " ")
        ordinance = parse_ordinance(pages.read(key), url)
        ordinance_id = f"{client}:ordinance:{ordinance.number or Path(key).stem}"
        vertices["ordinance"].append(
            {
                "id": ordinance_id,
                "number": ordinance.number,
                "title": ordinance.title,
                "category": category,
                "url": url,
                "pdf_url": ordinance.pdf_url,
                "adopted": ordinance.adopted,
                "amended": ordinance.amended,
                "notice": ordinance.notice,
                "summary": ordinance.summary,
                "sections": len(ordinance.sections),
            }
        )
        cited: dict[str, tuple[str, bool]] = {}
        for position, (heading, body) in enumerate(ordinance.sections):
            texts.append({"ordinance": ordinance_id, "position": position, "heading": heading, "text": body})
            for number, citation in tx.extract_statute_citations(body):
                cited.setdefault(number, (citation, False))
        for number, citation in tx.extract_statute_citations(ordinance.summary):
            cited[number] = (citation, True)
        for number, (citation, authority) in cited.items():
            statutes.add(number)
            edges["ordinance_cites"].append(
                {"src": ordinance_id, "dst": revisor.statute_id(number), "citation": citation, "states_authority": authority}
            )
    for number in sorted(statutes | {tx.statute_chapter(n) for n in statutes}):
        chapter = tx.statute_chapter(number)
        vertices["statute"].append(
            {
                "id": revisor.statute_id(number),
                "number": number,
                "kind": "section" if chapter != number else "chapter",
                "chapter": chapter,
                "url": revisor.statute_url(number),
            }
        )
        if chapter != number:
            edges["part_of"].append({"src": revisor.statute_id(number), "dst": revisor.statute_id(chapter)})
    vertices["ordinance"].sort(key=lambda r: (int(r["number"]) if r["number"] else 10**6, r["title"] or ""))

    root = silver_root(data_dir, client)
    report: dict[str, Any] = {"root": str(root), "vertices": {}, "edges": {}, "tables": {}}
    for group, schemas, data in (
        ("vertices", VERTEX_SCHEMAS, vertices),
        ("edges", EDGE_SCHEMAS, edges),
        ("tables", TABLE_SCHEMAS, {"ordinance_text": texts}),
    ):
        for name, schema in schemas.items():
            _write(root / group / f"{name}.parquet", data[name], schema)
            report[group][name] = len(data[name])

    def label(kind: str, row: dict[str, Any]) -> str:
        if kind == "statute":
            return revisor.label(kind, row)
        return f"Ordinance {row['number']}: {row['title']}" if row["number"] else (row["title"] or row["id"])

    _write(
        root / "graph" / "vertices.parquet",
        ({"id": r["id"], "type": kind, "label": label(kind, r)} for kind, rows in vertices.items() for r in rows),
        GRAPH_VERTICES,
    )
    _write(
        root / "graph" / "edges.parquet",
        ({"src": r["src"], "dst": r["dst"], "relationship": name, "date": None} for name, rows in edges.items() for r in rows),
        GRAPH_EDGES,
    )
    return report
