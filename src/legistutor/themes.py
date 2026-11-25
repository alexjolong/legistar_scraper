"""Themes, storylines and one-line gists: what the record is about, and what keeps coming back.

The record has no subject field. A chat model reads each matter once and files it:

* a **gist**: what the item does, in a headline's worth of words (titles are clerk's shorthand);
* one to three **themes** from the fixed list in ``themes.csv`` (edit that file to change the list);
* a **storyline** name, when the item is one step in a named project, program or contract.

``gold/legistar/<client>/matter_tags.parquet`` keeps one row per matter. A matter is read
again only when its text, the theme list, the instructions or the model changes.

Storylines are then joined without a model, in ``storylines.parquet``: matters belong to one
storyline when the model gave them the same name, when their titles carry the same agreement
number, or when the record itself links them (a revision, a relation, a mention of the other's
file number). Each member row says which of those put it there, so an inferred link can be
told from a recorded one. A mention never merges two groups the model named differently (a
report that cites last year's action plan is not one story with it). A name that is one of the
county's own boards ("Housing and Redevelopment Authority") joins nothing. A storyline needs
two items; an item and its own revisions are one.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from . import gold, llm, query
from .text import base_file_number

log = logging.getLogger(__name__)

MAX_THEMES = 3
TEXT_CHARS = 8000  # of a matter's background and resolution given to the model; nine in ten are under 5,000
CHECKPOINT = 100  # matters between saves, so a long first run can be stopped and picked up
GIVE_UP_AFTER = 8  # failures in a row: the model host is down, not the matter odd
# Approvals of minutes, claims registers and the like say nothing about a subject. They can still
# join a storyline through a recorded link.
ROUTINE = re.compile(r"(?i)\b(minutes|claims register|highlights)\b")
# Four in ten staff reports close with a set paragraph on which disparity the item touches ("Housing
# Disparity: ...", "This request supports ... the disparity reduction Health domain"). Shown that, a
# model files nearly everything under equity, so those paragraphs of the background are left out.
# An item that is about disparities says so in its title and resolution, which are kept whole.
BOILERPLATE = re.compile(r"(?i)disparit")
AGREEMENT = re.compile(r"\b(A\d{6,7}|PR\d{8})\b")
AGREEMENTS_IN_TITLE = 2  # a title naming more is a bundle of unrelated contracts
STORYLINE_THEME_SHARE = 0.3  # a storyline is shown under a theme that this share of its tagged items carry

TAG_SCHEMA = pa.schema(
    [
        ("matter", pa.string()),
        ("file_number", pa.string()),
        ("gist", pa.string()),
        ("themes", pa.list_(pa.string())),
        ("storyline", pa.string()),  # as the model named it; null when the item stands alone
        ("text_hash", pa.string()),
        ("fingerprint", pa.string()),  # of the instructions and the theme list
        ("model", pa.string()),
        ("tagged_utc", pa.string()),
    ]
)
STORYLINE_SCHEMA = pa.schema(
    [
        ("storyline", pa.string()),
        ("name", pa.string()),
        ("themes", pa.list_(pa.string())),
        ("matter", pa.string()),
        ("file_number", pa.string()),
        ("basis", pa.string()),  # why this matter is in it: "name", "agreement", "revision", "reference"
    ]
)


@dataclass(frozen=True)
class Theme:
    id: str
    area: str
    name: str
    description: str


def read_themes(path: Path | None = None) -> list[Theme]:
    source = path or resources.files("legistutor").joinpath("themes.csv")
    with source.open(encoding="utf-8-sig", newline="") as handle:
        themes = [Theme(*(row[k].strip() for k in ("id", "area", "name", "description"))) for row in csv.DictReader(handle) if row.get("id")]
    if len({t.id for t in themes}) != len(themes):
        raise ValueError(f"{source}: a theme id is used twice")
    return themes


def tags_path(data_dir: Path, client: str) -> Path:
    return gold.gold_root(Path(data_dir), client) / "matter_tags.parquet"


def storylines_path(data_dir: Path, client: str) -> Path:
    return gold.gold_root(Path(data_dir), client) / "storylines.parquet"


# --- what the model is asked ------------------------------------------------------------

_INSTRUCTIONS = """\
You file items from a county board's legislative record so that staff can follow subjects over time.
Read the item and reply with one JSON object and nothing else:
{"gist": "...", "themes": ["..."], "storyline": "..." or null}

gist: what the item does, as a headline: at most 20 words, starting with a verb in the imperative. \
"Accept $1.5 million from the state to house families with school-age children." Never begin \
"This resolution" or "The county". No agreement numbers, and no abbreviations a resident would not know.

themes: one to three ids from the list below, the best fit first. Choose by the subject the item \
serves, not by its paperwork: a grant agreement for shelter beds is homelessness, not budget-finance. \
Most items need one theme or two; give a third only when the item is as much about it as about the first. \
Two themes are over-used, so hold them to this: budget-finance is for the county's own budget, levy, \
bonds, taxes and audits, never merely because an item accepts, grants or spends money, as nearly all do; \
equity is for items whose main purpose is reducing disparities or protecting civil rights, never merely \
because the staff report closes with its usual sentence on which disparity the item touches. \
Use the other county operations themes only when the item is about running the county itself. If nothing fits, give [].

storyline: the specific named project, program, facility or contract that this item is one step in, \
written the way it would be named every time it comes up: the official name, spelled out, without \
years, phases, amendment numbers or agreement numbers ("Blue Line Extension", "Homework Starts with \
Home program", "County Road 152 reconstruction"). null when the item names nothing that specific.

Themes:
"""


def system_prompt(themes: list[Theme]) -> str:
    return _INSTRUCTIONS + "\n".join(f"{t.id}: {t.name}. {t.description}." for t in themes)


def fingerprint(themes: list[Theme]) -> str:
    return hashlib.sha1(system_prompt(themes).encode("utf-8")).hexdigest()[:12]


def user_prompt(matter: dict[str, Any]) -> str:
    facts = f"File {matter['file_number']} ({matter['matter_type'] or 'no type'})"
    if matter["intro_date"]:
        facts += f", {matter['intro_date']}"
    lines = [facts, f"Requested by: {matter['department'] or 'not recorded'}", f"TITLE: {matter['title'] or ''}"]
    return "\n".join(lines) + "\n\n" + matter["body"]


_NOTHING = {"", "null", "none", "n/a", "na"}


def read_reply(reply: str, themes: list[Theme]) -> dict[str, Any] | None:
    """The model's filing, checked: only themes on the list, at most three. None if it cannot be read."""
    found = re.search(r"\{.*\}", reply, re.DOTALL)
    try:
        data = json.loads(found.group(0)) if found else None
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("gist"), str) or not data["gist"].strip():
        return None
    by_id = {t.id: t.id for t in themes} | {t.name.lower(): t.id for t in themes}
    chosen: list[str] = []
    for item in data.get("themes") if isinstance(data.get("themes"), list) else []:
        theme = by_id.get(item.strip().lower()) if isinstance(item, str) else None
        if theme and theme not in chosen:
            chosen.append(theme)
    storyline = data.get("storyline")
    storyline = " ".join(storyline.split())[:120] if isinstance(storyline, str) else None
    if storyline is not None and storyline.lower() in _NOTHING:
        storyline = None
    return {"gist": " ".join(data["gist"].split())[:300], "themes": chosen[:MAX_THEMES], "storyline": storyline}


# --- tagging ----------------------------------------------------------------------------


def taggable(con) -> list[dict[str, Any]]:
    """Every matter worth filing, with the text the model is given."""
    sections: dict[str, dict[str, str]] = {}
    for matter, section, text in con.sql("select matter, section, text from t_matter_text").fetchall():
        sections.setdefault(matter, {})[section] = text
    columns = ("matter", "file_number", "title", "matter_type", "department", "intro_date")
    rows = []
    for row in con.sql(
        "select id, file_number, title, matter_type, department, coalesce(intro_date, agenda_date) from v_matter order by matter_id"
    ).fetchall():
        matter = dict(zip(columns, row))
        if ROUTINE.search(matter["matter_type"] or "") or ROUTINE.search(matter["title"] or ""):
            continue
        parts = dict(sections.get(matter["matter"], {}))
        if parts.get("background"):
            kept = [p for p in re.split(r"\n\s*\n", parts["background"]) if not BOILERPLATE.search(p)]
            parts["background"] = "\n\n".join(kept)
        body = "\n\n".join(f"{gold.SECTION_LABELS[s].upper()}:\n{parts[s]}" for s in ("background", "resolution") if parts.get(s))
        if not body and not matter["title"]:
            continue
        matter["body"] = body[:TEXT_CHARS]
        matter["text_hash"] = hashlib.sha1(user_prompt(matter).encode("utf-8")).hexdigest()
        rows.append(matter)
    return rows


def _write(rows: list[dict[str, Any]], path: Path, schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), temporary)
    for wait in (0.2, 0.5, 1, 2, 4, 0):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:  # Windows: the page is reading the file this moment
            if not wait:
                raise
            time.sleep(wait)


def tag_one(chat: llm.ChatProvider, system: str, matter: dict[str, Any], themes: list[Theme]) -> dict[str, Any] | None:
    """One matter's filing; a reply that cannot be read is asked for once more."""
    for _ in range(2):
        filed = read_reply("".join(chat.stream(system, user_prompt(matter))), themes)
        if filed:
            return filed
    return None


def tag(
    data_dir: Path,
    client: str,
    chat: llm.ChatProvider,
    *,
    full: bool = False,
    limit: int | None = None,
    workers: int = 1,
    themes: list[Theme] | None = None,
) -> dict[str, Any]:
    """File every matter not yet filed with these instructions by this model, then join the storylines."""
    started = time.monotonic()
    themes = themes or read_themes()
    system, mark = system_prompt(themes), fingerprint(themes)
    con = query.connect(data_dir, client)
    matters = taggable(con)
    path = tags_path(data_dir, client)
    kept: dict[str, dict[str, Any]] = {}
    if path.exists() and not full:
        current = {m["matter"]: m["text_hash"] for m in matters}
        for row in pq.read_table(path).to_pylist():
            if current.get(row["matter"]) == row["text_hash"] and row["fingerprint"] == mark and row["model"] == chat.model:
                kept[row["matter"]] = row
    pending = [m for m in matters if m["matter"] not in kept]
    left_for_later = max(0, len(pending) - limit) if limit is not None else 0
    pending = pending[:limit] if limit is not None else pending
    reused, failed, streak = len(kept), 0, 0

    def one(matter: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        try:
            return matter, tag_one(chat, system, matter, themes), None
        except llm.LLMError as exc:
            return matter, None, str(exc)

    def save() -> None:
        order = {m["matter"]: n for n, m in enumerate(matters)}
        _write(sorted(kept.values(), key=lambda row: order[row["matter"]]), path, TAG_SCHEMA)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for done, (matter, filed, error) in enumerate(pool.map(one, pending), start=1):
            if filed is None:
                failed, streak = failed + 1, streak + 1
                log.warning("%s was not filed: %s", matter["file_number"], error or "the model's reply could not be read")
                if streak >= GIVE_UP_AFTER:
                    pool.shutdown(cancel_futures=True)
                    save()
                    raise llm.LLMError(f"{streak} matters in a row could not be filed; the last error was: {error or 'unreadable reply'}")
                continue
            streak = 0
            kept[matter["matter"]] = {
                "matter": matter["matter"],
                "file_number": matter["file_number"],
                **filed,
                "text_hash": matter["text_hash"],
                "fingerprint": mark,
                "model": chat.model,
                "tagged_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            if done % CHECKPOINT == 0:
                save()
                log.info("themes: filed %d of %d matters", done, len(pending))
    save()
    members = build_storylines(con, list(kept.values()))
    _write(members, storylines_path(data_dir, client), STORYLINE_SCHEMA)
    return {
        "path": str(path),
        "matters": len(kept),
        "filed": len(pending) - failed,
        "reused": reused,
        "failed": failed,
        "left_for_later": left_for_later,
        "without_a_theme": sum(1 for row in kept.values() if not row["themes"]),
        "storylines": len({row["storyline"] for row in members}),
        "in_a_storyline": len(members),
        "model": chat.model,
        "seconds": round(time.monotonic() - started, 1),
    }


# --- storylines -------------------------------------------------------------------------

_NAME_NOISE = re.compile(r"\([^)]*\)|\b(?:19|20)\d{2}\b|\b(?:phase|amendment|amd)\s+\w+\b|[^a-z0-9 ]+")
_NAME_FILLER = frozenset("the a an of for and to in at on program project initiative hennepin county metro inc llc".split())


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")) else word


def storyline_key(name: str | None) -> str | None:
    """What two spellings of one storyline have in common: "Transit Oriented Communities (TOC) program"
    and "Transit-Oriented Communities Program" are the same, as are "METRO Blue Line Extension" and
    "Blue Line Extension". None if nothing distinctive is left."""
    text = _NAME_NOISE.sub(" ", (name or "").lower().replace("-", " ").replace("&", " and "))
    words = [_singular(word) for word in text.split()]
    words = [word for word in words if word not in _NAME_FILLER]
    if not words or (len(words) == 1 and len(words[0]) < 5):
        return None
    return " ".join(words)


def _body_keys(con) -> list[str]:
    """The county's own boards, by key: a storyline named after one ("Housing and Redevelopment
    Authority") is an agency, not a story, and would sweep up everything it does."""
    columns = [c for c in ("name", "type") if c in con.sql("select * from v_body limit 0").columns]
    keys = (storyline_key(name) for row in con.sql(f"select {', '.join(columns)} from v_body").fetchall() for name in row if name)
    return [f" {key} " for key in keys if key]


def _is_a_body(key: str, bodies: list[str]) -> bool:
    return any(f" {key} " in body for body in bodies)


def title_agreements(title: str | None) -> list[str]:
    found = list(dict.fromkeys(AGREEMENT.findall(title or "")))
    return found if len(found) <= AGREEMENTS_IN_TITLE else []


def build_storylines(con, tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per matter in a storyline. See the module's description for what joins them."""
    matters = {
        row[0]: {"matter": row[0], "file_number": row[1], "title": row[2], "date": row[3], "order": n}
        for n, row in enumerate(
            con.sql("select id, file_number, title, coalesce(intro_date, agenda_date) from v_matter order by matter_id").fetchall()
        )
    }
    tagged = {row["matter"]: row for row in tags if row["matter"] in matters}
    parent: dict[str, str] = {}
    basis: dict[str, set[str]] = {}

    def find(node: str) -> str:
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    keys: dict[str, set[str]] = {}  # the storyline names a group carries, by its root

    def join(a: str, b: str, why: str) -> None:
        if a == b or a not in matters or b not in matters:
            return
        ra, rb = find(a), find(b)
        if why == "reference" and keys.get(ra) and keys.get(rb) and not keys[ra] & keys[rb]:
            return  # a mention does not merge two stories the model named differently
        if ra != rb:
            parent[ra] = rb
            keys[rb] = keys.get(rb, set()) | keys.pop(ra, set())
        basis.setdefault(a, set()).add(why)
        basis.setdefault(b, set()).add(why)

    def join_all(groups: dict[str, list[str]], why: str) -> None:
        for members in groups.values():
            for other in members[1:]:
                join(members[0], other, why)

    bodies = _body_keys(con)

    def named(storyline: str | None) -> str | None:
        key = storyline_key(storyline)
        return None if key is None or _is_a_body(key, bodies) else key

    # What the record and the titles say for certain comes first; mentions only add to it.
    references: list[tuple[str, str]] = []
    for src, dst, kind in con.sql("select src, dst, kind from e_related_to").fetchall():
        if kind == "revision":
            join(src, dst, "revision")
        else:
            references.append((src, dst))
    references += con.sql("select src, dst from e_references").fetchall()
    by_name: dict[str, list[str]] = {}
    for matter, row in tagged.items():
        key = named(row["storyline"])
        if key:
            by_name.setdefault(key, []).append(matter)
            keys.setdefault(find(matter), set()).add(key)
    join_all(by_name, "name")
    by_agreement: dict[str, list[str]] = {}
    for matter, facts in matters.items():
        if matter in tagged:  # routine items are joined only by what the record itself links
            for number in title_agreements(facts["title"]):
                by_agreement.setdefault(number, []).append(matter)
    join_all(by_agreement, "agreement")
    for src, dst in references:
        join(src, dst, "reference")

    components: dict[str, list[str]] = {}
    for matter in parent:
        components.setdefault(find(matter), []).append(matter)
    rows: list[dict[str, Any]] = []
    for members in components.values():
        # An item and its revisions are one item, not a storyline.
        if len({base_file_number(matters[m]["file_number"]) for m in members}) < 2:
            continue
        members.sort(key=lambda m: (str(matters[m]["date"] or "9999"), matters[m]["order"]))
        names = Counter(tagged[m]["storyline"] for m in members if m in tagged and named(tagged[m]["storyline"]))
        if names:
            name = names.most_common(1)[0][0]
        else:
            shared = Counter(n for m in members for n in title_agreements(matters[m]["title"])).most_common(1)
            first_title = " ".join((matters[members[0]]["title"] or matters[members[0]]["file_number"]).split())
            name = f"Agreement {shared[0][0]}" if shared and shared[0][1] > 1 else first_title[:90]
        theme_counts = Counter(theme for m in members if m in tagged for theme in tagged[m]["themes"])
        filed = sum(1 for m in members if m in tagged and tagged[m]["themes"])
        ranked = [theme for theme, _ in theme_counts.most_common()]
        themes = [t for t in ranked if theme_counts[t] >= STORYLINE_THEME_SHARE * filed] or ranked[:1]
        storyline = "story:" + min(members, key=lambda m: matters[m]["order"]).rsplit(":", 1)[-1]
        for member in members:
            rows.append(
                {
                    "storyline": storyline,
                    "name": name,
                    "themes": themes,
                    "matter": member,
                    "file_number": matters[member]["file_number"],
                    "basis": ", ".join(sorted(basis.get(member, ()))),
                }
            )
    return rows


# --- for the page -----------------------------------------------------------------------


def attach(con, data_dir: Path, client: str) -> bool:
    """Load the filing into a connection as ``matter_tag``, ``storyline_member`` and
    ``matter_day`` (the days each matter was before a body). False if nothing is filed yet."""
    tags, storylines = tags_path(data_dir, client), storylines_path(data_dir, client)
    if not tags.exists():
        return False

    def quoted(path: Path) -> str:
        return "'" + path.resolve().as_posix().replace("'", "''") + "'"

    con.execute(f"create or replace table matter_tag as select * from read_parquet({quoted(tags)})")
    if storylines.exists():
        con.execute(f"create or replace table storyline_member as select * from read_parquet({quoted(storylines)})")
    else:
        con.execute(
            "create or replace table storyline_member (storyline varchar, name varchar, themes varchar[], matter varchar, file_number varchar, basis varchar)"
        )
    con.execute(
        """create or replace table matter_day as
           select distinct src as matter, date as day from e_on_agenda where date is not null
           union
           select id, coalesce(intro_date, agenda_date) from v_matter
           where id not in (select src from e_on_agenda where date is not null) and coalesce(intro_date, agenda_date) is not null"""
    )
    return True


def worker_count(provider: str, asked: int | None = None, env: Callable[[str], str | None] = os.environ.get) -> int:
    """A local model answers one request at a time; a serving endpoint takes several."""
    if asked:
        return asked
    return int(env("LEGISTUTOR_TAG_WORKERS") or (4 if provider == "databricks" else 1))
