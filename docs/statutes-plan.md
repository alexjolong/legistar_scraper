# Plan: bringing statutes into legistutor

Written 2026-11-11 as a proposal. The status below was added the same day, after the
decisions came back; the proposal follows unchanged, so where the two differ the
status is right. How to use what was built is in the [README](../README.md#the-law).

## Status

Decisions: bills come from Open States (Plural Policy) for everything it offers, the
Revisor's site for the rest, the county's site for ordinances. No message is sent to
the Revisor's office or anyone else. Changes are read from the 2023 session on, with
History lines dating older ones. Readers are a few colleagues, so the change list is
a tab in the web page; no digest.

| Phase | State |
|---|---|
| 0. Settle unknowns | Done. Findings below. |
| 1. Statute text | Done: 104 chapters, sections and subdivisions as vertices, cross-references, subdivision and session-law citations from matter text, statute text in search and in answers, statute view in the page. |
| 2. Enacted-change watch | Done for what is published: Table 2 for 2023 to 2026, session laws with bill and presentment date, the amending text with struck and inserted words, a "what changed" list ranked by reliance, and a "citations to check" list. `refresh` re-reads the current year. |
| 3. Pending bills | Partly. Bills, actions, companions and "became law" are in the graph and searchable by title. Not done: which statutes a pending bill would amend, and matching bills to the county's platform. Both wait for the 2027 session to have bills to test on. |
| 4. Ordinances | Done: 28 pages, all HTML; linked to the matters and statutes they cite. |
| 5. Platform linkage | Not started. |

What phase 0 found:

- Table 2 filters with `search_term=`, not `full=` (`?search_term=383B&year=2026`
  returns the nine 2026 rows for chapter 383B). It was simpler to read whole sessions
  (40 pages for four years) and filter locally, which also keeps the statewide table.
- Open States carries no statute citations for a bill, only its short title, subjects,
  authors, actions, companion and links to the Revisor's text. One bulk archive per
  session makes the API almost unnecessary outside a session. The free tier's rate
  limit is not stated in the responses; the client spaces API calls seven seconds apart.
- Ordinances cite far more chapters than the record does: adding theirs took the
  watched set from 53 chapters to 104.
- The online statutes are now the 2026 edition, so this year's changes are already in
  the text that was fetched.

What the first run turned up in the record: four items cite a section that is
repealed, renumbered or does not exist, and 46 changes in the 2026 session touched
sections the record or an ordinance cites.

Next, in the order I would take them:

1. Your evaluation questions, extended with a few about the law, before more is built
   on the search.
2. When the 2027 session opens (January 12): fetch bill text from the Revisor for bills
   that name the county or fall under watched subjects, read the "amending Minnesota
   Statutes ..., section ..." clause of each title, and add `would_change` edges. That
   turns the Law tab's bill list into "bills that would change law the record rests on".
3. Platform linkage (phase 5), which needs those edges and the similarity index.
4. Text comparison between statute editions. The amending law's own text already shows
   each change, so this matters only for changes made by Revisor's instruction.

## Recommendation

1. **Do not start by crawling all of Minnesota Statutes.** Start from the statutes the
   county's own record already cites, which is a small and very concentrated set, and
   grow outward only where a use needs it.
2. **Build in this order**, because each step is useful alone and cheap relative to the
   next:
   1. statute text for the cited chapters, attached to the existing graph;
   2. a watch on *enacted* changes to those chapters (the state publishes a table of
      exactly this);
   3. a watch on *pending* bills that would touch them or that match the county's
      legislative platform;
   4. county ordinances.
3. **Keep general analysis of state legislation as a separate tool, later.** Build the
   statute source as its own package inside this repo, with no county assumptions in
   it, so it can be lifted out when that tool is worth starting. Reasons are in
   [One tool or two](#one-tool-or-two).

## What the record already tells us

Measured on the current graph (3,135 matters, May 2023 to November 2026):

| | |
|---|---|
| Matters that cite a Minnesota statute | 441 (14%); about one resolution in five |
| Distinct chapters cited | 53 |
| Share of citing matters covered by chapters 375 and 383B alone | 49% |
| Chapters needed to cover 90% | 20 |
| Chapters cited by only one matter | 20 |
| Matters citing a subdivision (not captured today) | 142 |
| Matters citing a session law, e.g. "Laws of Minnesota 2023, chapter 64, article 8, section 3" | 12 |
| Matters citing a county ordinance by number | 17 |
| Matters citing Minnesota Rules, or federal code | 9 each |

The top of the list is what you would expect: 375 (county boards), 383B (Hennepin
County), 469 (economic development), 462C and 462A (housing), 474A and 475 (bonds and
debt), 473 (metropolitan government), 103B (water), 13D (open meetings), 373.

Two findings shape the plan:

- **Session laws are a first-class source, not a footnote.** Some law that binds
  Hennepin County never enters the statutes: special and local laws stay in the session
  laws only. The record cites them directly.
- **The board states its own legislative intent every year.** There is a "State
  Legislative Priorities and Platform" matter for each year (`24-0451`, `25-0474`,
  `26-0384` for 2027), plus federal platforms and amendments to them. That is the
  anchor for "the county is lobbying for this change": platform text can be matched to
  bills.

## Sources, and what I verified about each

All checked on 2026-11-11 by reading the pages. Nothing was crawled.

| Source | What is there | Access |
|---|---|---|
| Statutes, by chapter: `revisor.mn.gov/statutes/cite/383B` | Table of sections with headnotes, grouped under subheadings. Repealed sections stay listed with the repealing law. `/full` gives the whole chapter's text on one page. | Plain HTML. `robots.txt` allows `/statutes/` and `/laws/`. |
| Statutes, by section: `/statutes/cite/383B.158` | Headnote, subdivisions with anchors, a `History:` line of every session law that created or amended it (`2002 c 393 s 57; 2007 c 70 s 1-3; ...`), each linked. | Same. |
| Versions: `/statutes/cite/383B.158/versions` | Each published edition of a section back to 2008, with why it was published (annual publish, legislative action, editorial update). Year-specific URLs exist: `/statutes/2025/cite/...`. | Same. |
| **Statutes changed, "Table 2": `/statutes/table2`** | One row per statute or subdivision touched by a session: action (New, Amended, Repealed, Revisor Instruction, Other) and the session law chapter, article and section, with links to the prior-year and current-year text. Every session from 1994. The 2026 regular session has 2,825 rows. | Paged HTML table, up to "ALL" rows per page. |
| Session laws: `/laws/current/`, `/laws/2026/0/Session+Law/Chapter/88/` | Chapters of the current session with bill number and presentment date (2026: chapters 40 to 131). Chapter text carries effective dates. | Plain HTML. |
| Bills: `/bills/94/2026/0/HF/4582/` | Short description, authors, actions with dates, committee referral, text versions, companion bill. A separate "long description" page. The bill index for a biennium is one large page. | Bill pages are allowed. The *search result* pages (`/bills/status_results.php`, `/search/doc_result.php`) are disallowed by `robots.txt`, so searching bills by statute citation through the site is off the table. |
| Open States API (`v3.openstates.org`) | Bills, actions, sponsors, votes for state legislatures. | Needs a registered API key. I did not confirm Minnesota coverage depth, rate limits or data terms. |
| County ordinances: `hennepincounty.gov/government/about/ordinances` | Five category pages (health; law enforcement; licenses, property and roads; solid waste; special taxes) linking to one page per ordinance. The page I opened (Ordinance 13, recycling) has the full text as HTML in sections and subsections, adoption and amendment dates, and a PDF copy. | Plain HTML. No PDF parsing needed if the other ordinances follow the same pattern. |

Things the Revisor's site does **not** offer: an API, bulk downloads, or stated terms of
use. Its "about" page says the online statutes are updated once a year, and that the
official versions are the printed edition and the authenticated PDFs.

Not yet verified, to settle before building:

- How Table 2 filters by chapter. `?year=2026&full=383B` returned no rows, although
  chapter 383B's own page shows a section repealed by a 2026 law. Either the filter
  wants sections or a range, or the table lags. The fallback is to fetch a whole
  session's table (six pages at 500 rows) and filter locally.
- Whether every ordinance page has HTML text, and how many ordinances there are.
- Open States terms and Minnesota coverage.

## Design

### A second source beside Legistar

The medallion layout already keys data by source. Statutes follow the same pattern:

```
data/bronze/mnrevisor/statutes/<edition>/<chapter>.html     raw pages as fetched
data/bronze/mnrevisor/table2/<session>.html
data/bronze/mnrevisor/laws/<year>/<chapter>.html
data/silver/mnrevisor/vertices|edges|tables/*.parquet       same shapes as today
data/bronze/hennepin_ordinances/<number>.html
```

Bronze keeps the raw HTML, so a parser fix never needs a re-crawl. The statute ids
already in the graph (`mnstat:375.18`, `mnstat:383B`) are tenant-independent by
design, so the new tables join to the existing `cites` edges with no migration. The
query layer unions the sources into one graph.

Crawling manners: one request per second, a descriptive User-Agent, conditional
requests, and no fetching of disallowed paths. The whole phase-one crawl is about 60
requests.

### Graph additions

| Vertex | Id | Carries |
|---|---|---|
| `statute` (exists as a stub) | `mnstat:383B.158` | Gains headnote, text, status (in force, repealed), edition, subheading |
| `subdivision` | `mnstat:383B.158#4` | Text of one subdivision; the unit for search passages and change tracking |
| `session_law` | `mnlaw:2026:88` | Year, chapter, bill, presentment date; article and section live on the edges |
| `bill` | `mnbill:94:HF4582` | Description, authors, status, last action, companion |
| `ordinance` | `hennepinmn:ordinance:13` | Title, adopted and amended dates, text by section |

| Edge | From, to | Source |
|---|---|---|
| `cites` (exists) | matter, statute | Extend the parser to keep the subdivision |
| `cites_law` | matter, session law | New pattern in the matter-text parser |
| `cites_ordinance` | matter, ordinance | New pattern |
| `changed_by` | statute or subdivision, session law | Table 2, with the action; also parsed from each `History:` line for the years before the watch began |
| `cross_references` | statute, statute | "section 375.18" inside statute text; this is what turns the statutes into a graph of their own |
| `enacted_as` | bill, session law | Session law listings |
| `would_change` | bill, statute | Parsed from bill titles and long descriptions |
| `authorized_by` | ordinance, statute | Each ordinance opens with its statutory authority |
| `advances` | bill or session law, platform matter | Suggested by similarity, confirmed by a person |

### How the three watches work

**Text as context (phase 1).** A statute node in the web page shows its text and the
matters that rely on it, and a matter shows the text of what it cites. Statute
passages go into the search index, tagged by source, so "what gives the county
authority to do X" can retrieve law as well as resolutions. The answer prompt gets the
cited subdivision's text alongside the resolution.

**Enacted change (phase 2).** After each session, Table 2 says exactly which sections
changed. Joined to the graph, that becomes a ranked list: sections changed this
session, ordered by how many county matters cite them, with the old and new text side
by side and the citing matters one click away. During a session, a weekly poll of the
current session laws page catches each new chapter within days of presentment; the
chapter text names the statutes it amends and its effective date. This is the "flag it
as soon as possible" path, and it is cheap because the state has already done the
bookkeeping.

**Pending change (phase 3).** A bill is worth surfacing when it would change a watched
section, or when its description is close in meaning to a plank of the county's
legislative platform or to a cluster of recent county matters. The second test is
where the embedding index earns its place. Output is a short list for a person to
judge, with status and last action, not an automatic alert.

What none of this does: decide what a change *means* for the county. The tool can say
"section 383B.1511 was repealed by 2026 chapter 88, and these matters rely on it." Whether that is a problem is a question for the County Attorney or
intergovernmental relations staff, and the page should say so.

## Phases

Estimates are working days for the two of us at the pace of the last two days. They
are guesses until phase 0 is done.

| Phase | Delivers | Est. |
|---|---|---|
| 0. Settle unknowns | Table 2 filter behaviour; ordinance page survey; Open States check; fixtures saved from real pages for parser tests | 0.5 |
| 1. Statute text | Crawler and parser for the ~60 cited or structural chapters; `statute` and `subdivision` vertices; `cross_references`; subdivision and session-law citations from matter text; statute passages in search; statute view in the web page | 2 |
| 2. Enacted-change watch | Table 2 for 2023 onward; `session_law` vertices and `changed_by` edges; text comparison between editions; a "What changed" page ranked by county reliance; in-session weekly poll added to `refresh` | 2 |
| 3. Pending bills | Bill ingest from the chosen source; `would_change` edges; platform matching; a "Bills to watch" page | 3 to 4 |
| 4. Ordinances | Crawl and parse the ordinance pages; link to matters and to authorising statutes | 1 |
| 5. Platform linkage | Split each year's platform into planks; link planks to bills and session laws with human confirmation; "what happened to what we asked for" view | 2 |

Phase 1 plus phase 2 is the smallest version that answers your two examples: a new
state requirement gets flagged, and the county's reliance on a changed law is visible.
Phase 3 is the first one with a real choice of source and real ongoing cost, since
bills move daily during session.

## One tool or two

You are weighing county-scoped statutes against analysing state legislation in
general. I would keep them apart in purpose and together in code, for now.

| | County tool (this plan) | General legislation analysis |
|---|---|---|
| Question | What law does the county rely on, and what is changing under it? | Where is state law inconsistent; which small changes have large effects? |
| Corpus | About 60 chapters, selected by citation | Every chapter, every edition back to 2008, every session law |
| Anchor | The county's matters give each statute a weight | No anchor; importance has to be inferred |
| Reader | County staff | Analysts, possibly outside the county |
| How you know it works | Your evaluation questions | Needs its own, much harder, evaluation: a flagged "inconsistency" is a legal claim |

The second tool needs everything the first builds (crawler, parser, subdivision
vertices, cross-references, edition comparison), then adds scale and a different kind
of analysis. So the practical path is: write the statute source as a package that
knows nothing about Hennepin County, use it here on 60 chapters, and when the general
tool becomes real, point the same package at the full table of chapters and move it to
its own repository. Splitting now would mean designing the second tool's interfaces
before the first has taught us what they should be.

One thing worth doing early for the second tool's sake: store every fetched edition in
bronze from the start. History cannot be re-crawled later if the site changes, and it
costs nothing now.

## Decisions I need from you

1. **Bill source for phase 3.** Register for an Open States API key (cleaner data,
   another party's terms), or read bill pages from the Revisor's site directly (no key,
   more parsing, and a few thousand polite requests per session)? This does not block
   phases 0 to 2.
2. **A courtesy note to the Revisor's office.** The crawl in phases 1 and 2 is small
   and within `robots.txt`. If this becomes a county-backed tool, asking the office
   whether they can provide data directly would be the right move. Do you want to do
   that now, in your own name, or wait?
3. **How far back** the change history should go. I would start at the 2023 session to
   match the Legistar record, and store the `History:` lines so older changes are at
   least dated.
4. **Who reads the change list.** If it is you and a few colleagues, a page in the web
   app is enough. If intergovernmental relations staff would use it during session,
   phase 3 moves up and needs a weekly digest.
