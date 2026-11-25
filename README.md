# legistutor

Collects Hennepin County's legislative record (meetings, agenda items, resolutions,
actions, votes) from the public [Legistar Web API](https://webapi.legistar.com/Help)
so it can be searched and questioned with a retrieval-augmented LLM.

The data comes from Legistar, the legislative management system the county uses
(a Granicus product). This project is not affiliated with Granicus.

It also collects the law the record rests on: the chapters of Minnesota Statutes that
the county's items and ordinances cite, what each legislative session changed in them,
the session laws and bills behind those changes, and the county's own ordinances. See
[The law](#the-law).

Status: proof of concept, working end to end on one machine: ingestion (bronze), the
graph (silver), the search index (gold), hybrid search, cited answers from a local
model, and a local web page with graph views.

```
Legistar API ----ingest-----> bronze (raw JSON) --.
Revisor of Statutes --.                            |
Open States (bills) --+-ingest-law-> bronze -------+--build--> silver (one graph, as Parquet)
county ordinance pages'    (pages and archives as fetched)        |-- DuckDB       interactive lookups
                                                                  |-- GraphFrames  batch graph analytics
                                                                  '--index--> gold (passages + embeddings)
                                                                                '-- search / ask / serve (web page)
                                                                  '--themes-> gold (gist, themes, storylines)
refresh = ingest + ingest-law + build + index + themes
```

## Quick start

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you.

```
uv sync
uv run legistutor ingest              # reference data, meetings, matters
uv run legistutor ingest --stages votes
uv run legistutor status
uv run legistutor build               # bronze -> silver graph tables, a few seconds
uv run legistutor sql "select o.date, o.body_name, o.action from e_on_agenda o join v_matter m on m.id = o.src where m.file_number = '26-0333' order by o.date"
uv run legistutor index               # silver -> gold search index; needs Ollama (see below)
uv run legistutor search "embedded social workers in police departments"
uv run legistutor ask "Where does 26-0333 stand, and what was it about?"
uv run legistutor ingest-law          # statutes, session laws, bills, ordinances; then build and index again
uv run legistutor themes              # the chat model files each matter under themes and storylines
uv run legistutor serve               # the web page, at http://127.0.0.1:8765/
uv run legistutor refresh             # later: fetch what changed, rebuild graph and index
uv run pytest
```

`index`, `search`, `ask`, `serve` and `refresh` need [Ollama](https://ollama.com)
installed locally with the embedding model (`ollama pull qwen3-embedding:0.6b`).
If Ollama is not running (after a restart, say) these commands start it, and they
stop before doing any work if it cannot be started or a model is missing.
`ask` and the page's answers also need a chat model; see
[Asking questions](#asking-questions). `uv run legistutor check` tries every
dependency (models, data directory, outside sites) and says what is missing.

In a Databricks workspace the models are serving endpoints instead; see
[Running on Databricks](#running-on-databricks).

The first `ingest` is a full backfill: about 16,000 requests, roughly an hour at the
default 5 requests per second. Later runs fetch only what changed and take a minute
or two. Useful flags: `--limit N` for a smoke test, `--full` to ignore what is on
disk, `--rps` and `--workers` to change the pace, `--client` for another Legistar
tenant.

## Where the data comes from

Everything comes from the API. The public site (`hennepinmn.legistar.com`) is not
scraped: it shows the same records, and its "Accessible Minutes" page is HTML
converted from the PDF with broken word spacing.

| Public site | API endpoint |
|---|---|
| Calendar | `/events` |
| Meeting details, "Meeting Items" | `/events/{EventId}/eventitems` |
| Vote and attendance tallies | `/eventitems/{EventItemId}/votes`, `/rollcalls` |
| Legislation detail | `/matters/{MatterId}` |
| Legislation text | `/matters/{MatterId}/versions`, then `/texts/{TextId}` |
| Legislation history | `/matters/{MatterId}/histories` |
| Attachments (PDF links) | `/matters/{MatterId}/attachments` |
| Revision links (`26-0333` to `26-0333 R1`) | `/matters/{MatterId}/relations` |
| Committees, commissioners, memberships | `/bodies`, `/persons`, `/officerecords` |

Things worth knowing about this tenant:

- **Site IDs are not API IDs.** `MeetingDetail.aspx?ID=1446426` is API `EventId` 1473,
  and the GUIDs differ too. Each event's `EventInSiteURL` field links back to the site.
- **Minutes are action minutes.** They record the motion, mover, seconder and vote.
  Discussion is not written down anywhere; it exists only in the meeting video.
  The closest thing to a rationale is the staff "Background" section of each
  resolution's text.
- **Resolution text has marked sections**: `..title`, `..body` (the "BE IT RESOLVED"
  clauses), `Background:`, and `..recommendation`.
- **A matter history row and an event item are the same record** (same ID and GUID),
  seen from the matter side and the meeting side.
- **Free-text notes arrive as RTF** (`EventItemMinutesNote`, `EventItemAgendaNote`).
- **Consent agenda votes** are recorded once, on the "Approve the Consent Agenda"
  item. The votes endpoint returns that shared vote for each consent item.
- Collections return at most 1000 rows per request; page with `$top` and `$skip`.

## The law

`legistutor ingest-law` adds three sources beside Legistar. Each lands in its own
bronze folder as fetched, and builds its own silver graph; the query layer joins them
to the record by id (a matter's citation of `mnstat:383B.158` meets the statute's own
row). Nothing in the state-law code knows about a county, so it can be pointed at
other chapters or lifted out.

| Source | What is taken | How |
|---|---|---|
| [Revisor of Statutes](https://www.revisor.mn.gov/statutes/) | Whole chapters of Minnesota Statutes (sections, subdivisions, History lines); the table of statutes each session created, amended or repealed; each session's list of laws; the text of the session laws that changed a watched chapter | Web pages, one request per second. The site has no API. `robots.txt` allows these pages and disallows bill *search results*, which are never requested |
| [Open States](https://docs.openstates.org/api-v3/) (run by Plural Policy) | Every bill of the sessions since 2023: title, subjects, authors, actions, companion | One bulk archive per session, plus the API for bills changed since. Needs `PLURAL_POLICY_API_KEY` in the environment or in `.env` |
| [County ordinance pages](https://www.hennepincounty.gov/government/about/ordinances) | The 28 ordinances and tax resolutions the county publishes, as text | Web pages; all are HTML, none needed PDF parsing |

**Which chapters.** Not all of Minnesota Statutes: the chapters that an item in the
record or a county ordinance cites (104 today), plus any you name with
`--chapters 13 471`. A chapter stays watched once fetched. Changes are read from the
2023 session on (`--since-year`); older changes are still dated by each section's
History line.

**What it is for.**

- *What changed under the record.* After each session the state publishes exactly which
  sections it touched. Joined to the graph, that is a list ranked by reliance: how many
  matters and ordinances cite each changed section, with the session law's own text
  showing struck and inserted words, and its effective date where the section states one.
- *Citations to check.* Items that cite a section which is repealed, renumbered, or not
  in the statutes at all.
- *Law as context.* A statute or ordinance opens with its text, the items citing it
  (down to the subdivision where the citation is that precise), and its cross-references.
  Statute and ordinance text is searchable, and is given to the model for questions
  about the law.
- *Bills.* Searchable by words in the title, with their actions and, for those that
  passed, the session law they became.

**What it does not do.** It does not say what a change means for the county; that is
legal judgment, and the page says so. It does not yet say which statutes a *pending*
bill would amend: Open States does not carry that, so it will come from the bill text
on the Revisor's site once a session is under way (the 2027 session opens in January).
The copy of the statutes here is for finding and linking; the Revisor's publication is
the official text.

```
uv run legistutor ingest-law                  # everything; a few minutes the first time
uv run legistutor ingest-law --chapters 13    # also watch chapter 13
uv run legistutor ingest-law --skip bills     # parts: statutes changes bills ordinances
uv run legistutor summarize-law --top 130     # optional: model summaries of the most-cited sections, ahead of time
uv run legistutor sql "select c.statute, c.subdivision, c.action, count(distinct m.src) matters from e_changed_by c join e_cites m on m.dst = 'mnstat:' || c.statute where c.year = 2026 and c.basis = 'table2' group by all order by matters desc limit 10"
```

Costs, measured: the first run made about 330 requests to the Revisor's site in six
minutes and 34 to the county's; bronze for the law is about 250 MB. Indexing the law
text (28,600 passages) took nine minutes once; later runs embed only what changed.

## Bronze layout

```
data/bronze/legistar/hennepinmn/
  reference/<name>.json     full snapshot of a lookup collection
  events/<EventId>.json     event, with its agenda items nested under EventItems
  matters/<MatterId>.json   matter, with MatterTexts, MatterHistories,
                            MatterAttachments and MatterRelations nested
  votes/<EventId>.json      votes and roll calls for that meeting's items
```

Payloads are stored as the API returns them, plus a `_meta` block (tenant, fetch
time). `data/` is not committed; it can always be rebuilt from the API.

Incremental runs use the files themselves as state:

- A matter is re-fetched when its `MatterRowVersion` differs from the copy on disk.
- A meeting is re-fetched when its `EventRowVersion` differs, and always if it is
  within the last 120 days, upcoming, or its minutes are not yet `Final`. Agenda
  items can change without the event row changing, and minutes take a few weeks to
  be finalised.
- Votes are re-fetched when any agenda item of the meeting changes.
- Records that disappear upstream are counted as `orphaned` in the run summary and
  left on disk.

Attachments are recorded as metadata and links only. The PDFs are not downloaded.

Law sources (see [fetch.py](src/legistutor/fetch.py) for the page store):

```
data/bronze/mnrevisor/
  statutes/current/<chapter>.html     the chapter as last fetched (re-fetched monthly)
  statutes/<edition>/<chapter>.html   the same page filed by edition, so earlier editions are kept
  table2/<year>/<page>.html           statutes new, amended or repealed, 500 rows a page
  laws/<year>/<special>/index.html    the session's laws: chapter, bill, presentment date
  laws/<year>/<special>/chapter/<n>.html   text of a session law
data/bronze/openstates/mn/
  sessions.json, bulk/<session>.zip, bills/<session>/<bill>.json
data/bronze/ordinances/<client>/
  index.html, categories/<slug>.html, pages/<category>/<slug>.html
```

Every page has a `.meta.json` beside it (URL, time fetched).

## Silver layout: the graph

`legistutor build` rebuilds everything under `data/silver/legistar/hennepinmn/` from
bronze. It is a property graph stored as plain Parquet, in the shape GraphFrames
expects (`id` on vertices, `src` and `dst` on edges).

```
vertices/<type>.parquet   one table per vertex type, with its attributes
edges/<type>.parquet      one table per relationship, with its attributes
tables/<name>.parquet     detail that is neither: agenda_item, matter_text, attachment
graph/vertices.parquet    every vertex as (id, type, label)
graph/edges.parquet       every edge as (src, dst, relationship, date)
```

Vertex ids are strings like `hennepinmn:matter:4798`, so another Legistar tenant can
share the graph. Statutes are tenant-independent: `mnstat:275.065`.

| Vertex | What it is |
|---|---|
| `body` | The board, a committee, or an authority |
| `person` | Someone who holds office on a listed body or took part in a meeting |
| `meeting` | One meeting of one body |
| `matter` | A resolution, correspondence item, minutes approval, and so on |
| `department` | The requesting department, by its raw Legistar name |
| `statute` | A Minnesota Statutes chapter or section cited by some matter |

| Edge | From, to | Source |
|---|---|---|
| `member_of` | person, body | office records, with title and dates |
| `held_by` | meeting, body | |
| `on_agenda` | matter, meeting | agenda item: section heading, action, result, mover, seconder |
| `referred_to` | matter, body | where a referral sent the matter (see below) |
| `related_to` | matter, later matter | Legistar's own links; `kind` is `revision` or `related` |
| `references` | matter, matter | the text mentions the other's file number |
| `requested_by` | matter, department | |
| `cites` | matter, statute | parsed from the text |
| `part_of` | statute section, chapter | |
| `moved`, `seconded`, `voted` | person, matter | |
| `attended` | person, meeting | roll call |

How the derived parts work:

- **Text sections.** `tables/matter_text` holds each matter's latest text split into
  `title`, `resolution`, `background` and `recommendation`. These are the rows the
  search index is built from.
- **Referral targets** are resolved in order of trust: the body named in the action
  text, then the agenda heading the item sat under ("Referred to Public Works
  Committee"), then the next body that took the matter up. The `basis` column says
  which. A row with a null `action` is a planned referral: the item sits under a
  referral heading on a meeting whose minutes are not final yet.
- **Statute citations** are found by pattern ("Minnesota Statutes, section 275.065",
  "Minn. Stat. § 383B.158", "M.S. 373", chapter lists and ranges). Subdivisions and
  session laws are not captured.
- **Excluded on purpose:** meetings whose agenda status is `Hidden` (the clerk's test
  meetings), staff user accounts, and staff contact names and emails on matters.
  All of it remains in bronze.

### The law in the graph

`data/silver/mnlaw/` and `data/silver/ordinances/<client>/` have the same layout. The
views below are what `legistutor sql` and the page see: the three graphs as one.

| Vertex | Id | Carries |
|---|---|---|
| `statute` | `mnstat:383B`, `mnstat:383B.158` | headnote, status (in force, repealed, renumbered), text, History line, edition |
| `subdivision` | `mnstat:383B.158#4` | headnote, status, text |
| `session_law` | `mnlaw:2026:88`, `mnlaw:2025s1:13` | bill, presentment date, title |
| `bill` | `mnbill:2025-2026:HF4582` | title, subjects, authors, first and last action |
| `ordinance` | `hennepinmn:ordinance:13` | title, category, adopted and amended dates, notice |

| Edge | From, to | Notes |
|---|---|---|
| `cites` | matter, statute | as before |
| `cites_subdivision` | matter, subdivision | when the citation names one |
| `cites_law` | matter, session law | "Laws of Minnesota 2023, chapter 64, article 8, section 3" |
| `cites_ordinance` | matter, ordinance | by number |
| `changed_by` | statute or subdivision, session law | `basis` is `table2` (with the action, article and section) or `history` (older years, from the History line) |
| `cross_references` | statute or subdivision, statute | sections and chapters named in the text |
| `enacted_as` | bill, session law | |
| `companion` | bill, bill | |
| `ordinance_cites` | ordinance, statute | `states_authority` when cited in the opening lines |
| `part_of` | subdivision, section, chapter | |

Tables: `t_statute_change` (every row of the state's table, including chapters not
fetched), `t_law_section` (the session-law sections behind the changes, with struck
words as `[-...-]` and inserted words as `{+...+}`), `t_bill_action`, `t_ordinance_text`.

The record writes a stub vertex for each thing it cites (id and number only), so it is
a complete graph alone. When the owning source has the same id, its row is shown in
place of the stub. A stub that stays a stub is information: `v_statute` rows with a null
`status` are cited in the record but are not in Minnesota Statutes as published.

## Querying

`legistutor sql` and `legistutor.query.connect()` expose every Parquet file as a
DuckDB view: `v_<vertex>`, `e_<edge>`, `t_<table>`, plus `vertices` and `edges`.
DuckDB reads the files in place, so lookups take milliseconds.

```sql
-- what is queued for each committee on a board agenda that has not been minuted yet
select b.name, m.file_number, m.title
from e_referred_to r
join v_body b on b.id = r.dst
join v_matter m on m.id = r.src
where r.action is null
order by b.name, m.file_number;
```

## Gold layout: the search index

`legistutor index` writes one table, `data/gold/legistar/hennepinmn/chunks.parquet`:
a row per text passage, with its embedding. There is no vector database. At this
size (about 5,300 passages) DuckDB compares a question against every passage in a
few milliseconds, and the table is already in the shape a Delta Sync index in
Databricks Vector Search is built on: `chunk_id` is the key, `embedding` the vector,
and `matter_type`, `status`, `department`, `intro_date` and `last_action_date` the
filter columns.

Once the law has been fetched, `index` also writes `law_chunks.parquet` beside it: one
row per passage of statute text (a subdivision, or a section that has none) and of
ordinance text (a headed section), about 28,600 passages. It is a separate table
because it is ranked separately: a search returns the record's matters and, apart from
them, the law whose text matches.

- **What is indexed.** Each matter's `resolution` and `background` sections. A
  section up to 3,000 characters stays whole (most do); longer ones are split
  between paragraphs into passages of at most 2,400. A matter with neither section
  (mostly correspondence) gets one passage holding its title.
- **What is not.** Approvals of earlier minutes (their only text is "Approval of
  minutes of ..."), and the free-text notes on agenda items, which are almost all
  procedural boilerplate. Both remain in silver.
- **Context header.** Each passage is embedded together with its file number, type,
  department and title (`embed_text`), because a paragraph from the middle of a
  background section rarely names the item it belongs to. `text` is the passage
  alone.
- **Incremental.** A passage whose `embed_text` is unchanged keeps its vector, so
  after the first build (about two and a half minutes on an RTX 3060) only new or
  edited matters reach the model. `--full` re-embeds everything. Changing the
  embedding model re-embeds everything too, and search refuses to run a question
  through a different model than the index was built with.

The embedding model is `qwen3-embedding:0.6b` through Ollama (1,024 dimensions),
chosen because Databricks serves the same model. Set `LEGISTUTOR_EMBED_MODEL` to use
another; the query-side prefixes for Qwen3, BGE and GTE are in
[embed.py](src/legistutor/embed.py). Ollama runs a quantised build, so after a
move to Databricks re-embed there rather than mixing vectors from both.

## Searching

```
uv run legistutor search "road reconstruction" --since 2026-06-01 --body "Public Works"
```

`legistutor.search.Searcher` combines three signals:

1. **File numbers** in the question are looked up directly and always come first.
   `26-0333` brings the item and its revisions; `26-0333 R1` only the revision.
2. **Meaning**: cosine similarity between the question and every passage.
3. **Keywords**: BM25 full-text search (DuckDB's `fts` extension, tokenised to keep
   digits), which is what finds `PR00006625`, a vendor name, or a road number.

The meaning and keyword rankings are merged with reciprocal rank fusion, passages
are grouped by matter, and each matter is returned with what the graph knows about
it: status, the action taken at each meeting and where a referral sent it, the
bodies it is queued for, earlier and later related items, and statutes cited.

Filters: `--since` (last action on or after a date), `--department`, `--body` (a body
the matter appeared before), `--type`, `--status`. `--top` sets how many matters
come back (default 8).

Known limit: search finds the *most relevant* matters, not *all* matters meeting a
condition. "List everything queued for Public Works" is a graph question; ask it
with `legistutor sql` (the example under [Querying](#querying) does exactly this)
until the answering layer can run such queries itself.

## Asking questions

```
uv run legistutor ask "What has the county done about embedded social workers in police departments?"
uv run legistutor ask "opioid settlement spending" --since 2026-01-01
uv run legistutor ask "Where does 26-0333 stand?" --model qwen3.5:9b
```

`ask` runs the same search, renders each matter as a numbered source, and has a
local model answer from those sources only, citing them as `[1]`, `[2]`. The sources
are printed under the answer with links to the county's site. The prompt tells the
model what this record cannot show: discussion is not minuted, the staff background
is the department's case rather than the board's reasoning, and amounts are what a
resolution authorised, not what a contract finally said.

After the answer, `rag.unsupported_claims` checks it mechanically: every file number,
agreement number, dollar amount and citation number in the answer must appear in the
sources the model was given. Anything that does not is listed as a warning. This
catches a small model splicing identifiers together; it does not prove an answer
right.

The model runs in [Ollama](https://ollama.com) (`ollama pull gemma4:12b`). Settings
are environment variables:

| Variable | Default | |
|---|---|---|
| `LEGISTUTOR_CHAT_MODEL` | `gemma4:12b` | any Ollama chat model; `--model` overrides per question |
| `LEGISTUTOR_OLLAMA_THINK` | `false` | `true` lets a reasoning model think before answering (slower); empty leaves the model's default |
| `LEGISTUTOR_OLLAMA_NUM_CTX` | `16384` | context window requested from Ollama, in tokens |
| `LEGISTUTOR_EMBED_MODEL` | `qwen3-embedding:0.6b` | |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | |

Notes:

- A question with eight sources is a prompt of roughly 5,000 to 9,000 tokens. Sources
  that would overflow the model's window are dropped from the bottom of the ranking
  and the output says how many.
- The first answer after a pause waits for the model to load into GPU memory (up to
  a minute on a 12 GB card); after that an answer takes roughly 6 to 12 seconds.
- [llm.py](src/legistutor/llm.py) keeps the model behind a three-member
  interface (`ChatProvider`). `OllamaChat` and `DatabricksChat` (a model serving
  endpoint) are the two classes there; nothing above them differs.

### Choosing the chat model

The embedding model and the chat model are separate jobs. `qwen3-embedding`, `bge`
and `gte` are embedding models: they turn text into vectors for search and cannot
write an answer. The chat model reads the retrieved sources and writes one.

Chat models that fit a 12 GB card were compared on 2026-11-10 with six questions over
identical retrieved sources (a topical summary, a point question about an item and
its revision, a committee queue, a question the record cannot answer, an agreement
lookup, and a "why"). All ran entirely on the GPU with a 16k-token window. Times
exclude model loading.

| Model | Answer time | What went wrong |
|---|---|---|
| `gemma3:12b` | 7 to 34 s | Spliced file and agreement numbers into 8 invented identifiers; missed the revision; listed an item under the wrong committee |
| `gemma4:12b`, reasoning off (**default**) | 6 to 28 s, usually under 12 | Left one of three queued items out of a list |
| `gemma4:12b`, reasoning on | 14 to 45 s | Nothing found; answers are terser |
| `qwen3.5:9b`, reasoning off | 1 to 8 s | Reported the superseded original item as "Approved" and called it the primary record |
| `qwen3.5:9b`, reasoning on | 14 to 127 s | Nothing found, but too slow to use interactively |

Six questions is a smoke test, not a benchmark. Larger models (`gemma4:26b`,
`qwen3.5:27b`, `gpt-oss:20b`) need 14 GB or more and would spill onto the CPU here.
Rerun the comparison when the evaluation question set exists.

Changing the *embedding* model is a different experiment: it means re-running
`legistutor index --full` and judging retrieval, which needs that question set to
measure. Two facts to carry into it: `bge_large_en_v1_5` reads at most 512 tokens,
and 27% of the current passages are longer than that, so it would need smaller
passages; `gte_large_en_v1_5` reads 8,192 tokens but is not in the Ollama library, so
running it locally means adding a PyTorch-based embedder.
## The web page

```
uv run legistutor serve            # opens http://127.0.0.1:8765/
```

One page, served from this machine to this machine only (it binds to 127.0.0.1), with
no external scripts or fonts.

- **Explore**, the page's first tab once `legistutor themes` has run (see
  [Themes and storylines](#themes-and-storylines)):
  - *What the board has been working on*: one row per theme, one column per month,
    shaded by how many of the theme's items were before a body that month. Select a
    theme to open it, or a cell to open it at that month.
  - *A theme*: its storylines as rows on a timeline over the whole record (a dot per
    item, a line from the first to the last, so a discussion that kept coming back is a
    long line with many dots), then its items with their gists, which can be narrowed
    to those in a storyline, those that came up on their own, or one month.
  - *A storyline* (opens beside the page): its items in date order, and for each one
    why it was put there.
  - *By meeting*: any meeting's agenda sorted by theme, with a link to the meeting in
    Legistar. This is the way in for someone who starts from the calendar.
  - A matter's own view shows its gist, its themes and its storyline.
  - `?theme=affordable-housing` and `?storyline=...` in the address open one directly.
- **Ask and search.** A question box with the same filters as the CLI. *Ask* streams
  an answer with numbered citations that jump to their source; *Search only* returns
  the ranked matters at once without waiting for the model. Identifiers in an
  answer that are not in its sources are listed in a warning under it.
- **Topic over time.** Counts the matters that contain every word of the query, as
  stacked columns per quarter coloured by requesting department (the five largest;
  the rest fold into "Other"), with the list beneath. Select a column to narrow the
  list to that quarter. This view matches wording, with stemming, and not meaning:
  a count needs a cut-off, and similarity ranking has none. About 45% of matters
  have no requesting department recorded; they are shown as their own grey segment.
- **Path and links**, for any matter:
  - *Path through the bodies*: lanes are bodies, columns are meeting dates, and each
    dot is an agenda appearance labelled with its action. An item and its revisions
    are drawn together (`26-0333` is referred and revised; `26-0333 R1` is adopted).
    A hollow dot has no action recorded yet, and a dashed leg leads to a committee
    the item is queued for.
  - *Connections*: the matter's neighbours in the graph, grouped by what the edge
    means: requested by, heard by, earlier and later related items, items it
    mentions or is mentioned by, statutes cited. Select an item to move to it;
    select a department, body or statute to list every matter attached to it.
    This is how to walk the graph: item, statute, other items citing it, and on.
  - The full resolution and background text, and attachment links.
- **Coming up.** What is waiting on each body, as a ranked bar chart with the lists
  beneath. An item is listed when a draft board agenda places it under a "Referred
  to ..." heading, or when a recorded action referred or returned it to the body and
  it has not been on an agenda since; the list says which. Items sent somewhere more
  than 180 days ago and never heard are shown apart as probable leftovers. Recent
  meetings still without final minutes follow. This answers "what is queued for
  Public Works" exactly, which search and the model cannot.

- **Law.** Appears once `ingest-law` has run.
  - *What each session changed under the record*: sessions as stacked columns (amended,
    new, repealed), and for the selected session the changed sections, most relied on
    first. *Show the change* opens the session law's section with struck and inserted
    text marked.
  - *Citations to check*, *County ordinances*, and a *State bills* search.
  - Any statute, subdivision, session law, ordinance or bill opens in the details pane
    with its text and its links; from there the graph can be walked as from an item.
- **Related law.** A search also lists the statute sections and ordinances whose text
  matches, apart from the record's results. The two lists stay separate (they are
  separate indexes, and a merged list would bury board items under six times as much
  statute text), but whichever holds the closer passage is shown first: the question is
  embedded once, so its best similarity to the record and to the law can be compared.
  *Ask* gives the best three law results to the model as numbered sources when the law
  leads, or when the question names the law (statute, law, ordinance, authority,
  legislature), and not otherwise. The margin for "leads" (`LAW_LEAD_MARGIN` in
  [web.py](src/legistutor/web.py)) was set from fifteen trial questions and should be
  set again from the evaluation set.
- **Summary and questions**, on any statute section, chapter or ordinance. The chat
  model writes a summary from that text alone, and answers questions about it, naming
  the subdivision or part behind each point. A summary is written once and kept in
  `data/gold/legistar/<client>/law_summaries/` with the model that wrote it; it is
  marked out of date when the text or the model changes. `legistutor summarize-law
  --top 130` writes them ahead of time for the most-cited sections, about eight
  seconds each.
  - What the model reads is bounded by its context window, and the page always says
    what was read. Everything, when it fits: all but 13 of the 6,952 sections do, and
    25 of the 104 chapters and 25 of the 28 ordinances. For a question about something
    longer, the parts closest to the question are chosen with the search index. For a
    summary of a longer section or ordinance, the parts are read in order until the
    window is full. For a summary of a longer chapter, only its table of sections is
    read, and the summary says so.
  - Section and subdivision numbers in the model's text that are not in what it read
    are listed in a warning.
  - Subdivisions are listed closed, as headnotes with a line of preview, and open on a
    click; the one a link pointed to is open.

Opening an item splits the window: the page stays on the left and the details open on
the right. Drag the divider, or focus it and use the arrow keys, to resize; the width
is remembered. Below 900 pixels wide the details cover the page instead.

Each chart has a table or list form beside it, and tooltips appear on keyboard focus
as well as on hover. Deep links: `/?open=26-0333` opens an item, `/?q=road+repairs`
runs a search, `/?q=light+rail&topic=1` charts a topic, `/#coming-up` opens the queues,
`/#law` opens the law tab, `/?node=mnstat:383B.158` opens a statute (or an ordinance,
session law or bill by its id).

[web.py](src/legistutor/web.py) is a JSON API (`/api/search`, `/api/ask`,
`/api/matter`, `/api/node`, `/api/queues`, `/api/topic`, `/api/changes`, `/api/law_text`, `/api/law_ask`,
`/api/ordinances`, `/api/bills`, `/api/meta`) on the standard library's HTTP
server; the page is [static/index.html](src/legistutor/static/index.html). The
`App` class holds the logic and does not know about HTTP, so it can move behind
another framework without changes. The same server runs as a Databricks App; see
[Running on Databricks](#running-on-databricks).

## Keeping it current

```
uv run legistutor refresh
```

runs `ingest` for every stage (only what changed upstream is fetched), `ingest-law`,
then `build` and `index` (only new or edited passages are embedded), then `themes`
(only new or edited matters are read). It needs Ollama running with the embedding model
and the chat model, and exits non-zero if any fetch failed. `--no-law` leaves the law
sources alone; `--no-themes` skips the filing, and with it the need for the chat model.

What `ingest-law` re-reads on a routine run: a statute chapter once it is a month old
(the statutes are republished yearly, with corrections in between); the current
year's table of changes and list of laws, every run; ordinance pages weekly; one API
request for the bill sessions, and more only while a session is open. A finished
session's table and an enacted law's text are fetched once. A typical day is under
twenty requests. To run it every morning on Windows:

```
schtasks /Create /TN "legistutor refresh" /SC DAILY /ST 06:00 /TR "cmd /c cd /d C:\path\to\legistutor && uv run legistutor refresh >> data\logs\refresh.log 2>&1"
```

A running `legistutor serve` picks up a refresh by itself: every two minutes it looks
at the silver and gold files, and once they have changed and then stayed the same for
two looks (so a refresh still under way is waited out) it loads them, answering from
the old copy until the new one is ready. `--reload-seconds` or
`LEGISTUTOR_RELOAD_SECONDS` changes the interval; `0` turns it off. If the port is
already being served, `serve` says so and stops; use `--port` for a second copy.

## Themes and storylines

```
uv run legistutor themes              # file what is new or changed; the first run reads everything
uv run legistutor themes --limit 200  # a first run in pieces
```

The record has no subject field, so the chat model reads each matter once
([themes.py](src/legistutor/themes.py)) and returns three things:

- a **gist**: what the item does, as a headline of at most 20 words ("Accept $1.5 million
  from the state to house families with school-age children"). Titles are clerk's
  shorthand; matters are short (the median is about 2,300 characters), so a longer
  summary would add little.
- one to three **themes** from the fixed list in
  [themes.csv](src/legistutor/themes.csv): 26 subjects in nine areas. A fixed list
  keeps the themes the same from one refresh to the next. Left to name themes freely,
  the local model produced 160 for 200 matters.
- a **storyline** name, when the item is one step in a named project, program, facility
  or contract ("Blue Line Extension").

Storylines are then joined without a model. Matters are in one storyline when the model
gave them the same name (compared without years, phases, acronyms in brackets, plurals,
"METRO", "Inc." and filler words), when their titles carry the same agreement number
(`A2311867`, `PR00006316`), or when the record links them (a revision, a relation, a
mention of the other's file number). Each member records which of those put it there.
A mention can bring an item into a storyline but never merges two storylines the model
named differently. Without that rule, chains of mentions made a 37-item "storyline" out
of brownfield grants, HUD action plans and board appointments. A name that is one of the
county's own boards ("Housing and Redevelopment Authority") joins nothing, since it
would sweep up everything that board does. A storyline needs two items; an item and its
own revisions count as one. A storyline is shown under every theme that at least 30% of
its filed items carry.

Rebuilding storylines needs no model: `uv run legistutor themes` reads nothing again when
nothing changed, and joins the storylines afresh.

Minutes, claims registers and "Hennepin Highlights" are not read; they can still be in a
storyline through a link in the record.

The result is two tables beside the search index, `matter_tags.parquet` (one row per
matter) and `storylines.parquet` (one row per matter in a storyline). A matter is read
again only when its text, the theme list, the instructions or the model changes, so
**editing `themes.csv` files everything again** on the next run. Locally that is about
an hour and a half for the whole record with `gemma4:12b` (2,207 matters); on Databricks it is one
request per matter to the chat endpoint, four at a time (`--workers`, or
`LEGISTUTOR_TAG_WORKERS`). The run saves every 100 matters and can be stopped and
started again. `refresh` files new matters as its last step; `--no-themes` skips it.

The filing is a model's reading, and the page says so wherever it shows one. Expect a
small local model to over-apply broad themes; the storyline joins by name are the least
certain, which is why each says how it was made.

## Measuring it

```
uv run legistutor eval                 # search only: seconds
uv run legistutor eval --answers       # also writes and checks an answer per question
uv run legistutor eval --compare eval/results/<earlier run>.json
```

runs the questions in [eval/questions.csv](eval/questions.csv) (how to write them:
[docs/evaluation-set.md](docs/evaluation-set.md)) through the same search and
answering as the page ([evaluate.py](src/legistutor/evaluate.py)). For each kind of
question it reports:

- **found**: every `must_find` item was among the results shown, and **MRR**, how high
  the first of them ranked (1.00 is always first);
- **lead ok**: a question about the law led with the law's results, one about the
  record with the record's;
- with `--answers`: **facts**, how many `answer_facts` the answers state; **wrong**,
  answers that say what `must_not_say` describes; **unsupp.**, answers containing an
  identifier that is not in their sources; and how many `no-answer` questions were
  declined.

Facts are read by a model (`--judge` names another one; by default the one that wrote
the answer), so those columns are a measurement with error. The file is checked first:
a row that cannot be scored is named and nothing runs, and a `must_find` entry that is
not in the data at all is warned about. Each run is saved under `eval/results/` with
the models it used, and `--compare` lists the questions whose result changed since an
earlier run. To compare embedding models, index with each (`LEGISTUTOR_EMBED_MODEL`,
then `legistutor index --full`) and run `eval` after each.

## Running on Databricks

The same code runs in a Databricks workspace with three substitutions. `check` (step 3)
is there to find what differs in yours.

| Locally | On Databricks |
|---|---|
| Ollama for embeddings and chat | model serving endpoints ([embed.py](src/legistutor/embed.py), [llm.py](src/legistutor/llm.py)) |
| `./data` | a directory in a Unity Catalog volume |
| `legistutor serve` on 127.0.0.1 | a Databricks App running [app.py](app.py) |
| `legistutor refresh` by hand or `schtasks` | a scheduled job running [notebooks/legistutor_job.py](notebooks/legistutor_job.py) |

DuckDB over Parquet stays as it is, and so does the search (vectors in Parquet, not
Vector Search).

The provider is `databricks` whenever the code runs on a cluster or in an App
(`DATABRICKS_RUNTIME_VERSION` or `DATABRICKS_APP_NAME` is set), and `ollama` anywhere
else; `LEGISTUTOR_PROVIDER` overrides. Credentials come from the Databricks SDK: the
notebook's identity on a cluster, the app's service principal in an App.

| Variable | Default on Databricks | |
|---|---|---|
| `LEGISTUTOR_DATA_DIR` | `./data` | set to the volume directory, e.g. `/Volumes/<catalog>/<schema>/<volume>/data` |
| `LEGISTUTOR_EMBED_MODEL` | `databricks-qwen3-embedding-0-6b` | name of the embedding endpoint |
| `LEGISTUTOR_CHAT_MODEL` | `databricks-claude-sonnet-5-5` | name of the chat endpoint |
| `LEGISTUTOR_CONTEXT_TOKENS` | `60000` | how much prompt to send; long statute chapters are read up to this |
| `LEGISTUTOR_MAX_TOKENS` | `4000` | longest answer |
| `LEGISTUTOR_TEMPERATURE` | unset | sent only if set; some models refuse it |

Setting up:

1. Create a volume (Catalog > your schema > Create > Volume) and decide on a
   directory in it for the data.
2. Clone this repository as a Git folder (Workspace > Create > Git folder).
3. Open [notebooks/legistutor_job.py](notebooks/legistutor_job.py), set `data_dir`
   and the two endpoint names, and run it with `command` = `check`. Fix whatever it
   reports as `FAIL` before going on: a wrong endpoint name, no permission on the
   volume, a site the workspace cannot reach.
4. Run it with `command` = `refresh`. The first run is the full backfill (about an
   hour of fetching, then embedding every passage). The index built on your own
   machine is not carried over: vectors from Ollama's build of the model are not
   mixed with the endpoint's.
5. Schedule the notebook as a job (Schedule, in the notebook) with `command` =
   `refresh`. For state bills, keep the Open States key as a Unity Catalog secret and
   give its three-part name (`catalog.schema.key`) in `plural_policy_api_key_path`.
   The first `refresh` after themes were added sends every matter to the chat endpoint
   once (about 2,400 requests of roughly 1,500 tokens each); `command` = `themes` does
   only that step, and `refresh --no-themes` leaves it out.
6. Create the app (New > App > Custom), pointing it at the Git folder. Edit
   [app.yaml](app.yaml) first so `LEGISTUTOR_DATA_DIR` and the endpoint names match
   the job's. Under the app's resources add the two serving endpoints (Can query)
   and the volume (Can read and write), which grants them to the app's service
   principal. Deploy.

How the app differs from the local page:

- A cluster sees the volume as files under `/Volumes`; an App does not. At start the
  app downloads the `silver` and `gold` directories (about 180 MB today) to its own
  disk through the Files API ([databricks.py](src/legistutor/databricks.py)) and
  reads those.
- Every two minutes it lists the volume, downloads what the job has rewritten, and
  loads it once the files have stopped changing. No restart is needed after a refresh.
- Summaries of statutes written in the app are uploaded back to the volume, so each is
  written once. Summaries written by the local model are written again by the endpoint's.
- It listens on `0.0.0.0` at `DATABRICKS_APP_PORT`; Databricks signs people in before
  a request reaches it. Everyone who can use the app sees the same public record.

## GraphFrames (optional)

For graph algorithms (motif finding, PageRank, connected components) the same files
load into GraphFrames on a local Spark session. `load_graph` reads the record's own
graph (`silver/legistar/<client>/graph`), in which cited law appears as stub vertices;
the law graphs under `silver/mnlaw` and `silver/ordinances` have the same two files
and can be unioned in when an analysis needs them.

```
uv sync --extra spark
uv run legistutor graph-demo
```

```python
from pathlib import Path
from legistutor import graph

spark = graph.spark_session()
g = graph.load_graph(spark, Path("data"), "hennepinmn")
g.find("(m)-[c]->(s)").filter("c.relationship = 'cites'").show()
```

Setup notes:

- Spark 4.1 needs Java 17 or 21. `graph.spark_session()` uses `LEGISTUTOR_JAVA_HOME`
  if set, otherwise a JDK 17 or 21 under `~/.jdks`, otherwise `JAVA_HOME`.
- The GraphFrames JARs are downloaded from Maven Central on first use, checked
  against their published SHA-1, and cached in `~/.cache/legistutor/jars`.
- On Windows this runs without `winutils.exe` because Spark only reads here. The
  "Did not find winutils.exe" warning is expected. Writing from Spark to local
  disk would need it.
- `connectedComponents` needs `use_local_checkpoints=True` on a local session.
- Spark takes several seconds to start, so use DuckDB for interactive questions.
- On Databricks, skip `spark_session()`: GraphFrames is preinstalled on ML runtimes,
  and the two `graph/` tables load directly from ADLS.
