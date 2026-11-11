# legistutor

Collects Hennepin County's legislative record (meetings, agenda items, resolutions,
actions, votes) from the public [Legistar Web API](https://webapi.legistar.com/Help)
so it can be searched and questioned with a retrieval-augmented LLM.

The data comes from Legistar, the legislative management system the county uses
(a Granicus product). This project is not affiliated with Granicus. Next planned source: 
Minnesota Statutes and session laws, see [docs/statutes-plan.md](docs/statutes-plan.md).

Status: proof of concept, working end to end on one machine: ingestion (bronze), the
graph (silver), the search index (gold), hybrid search, cited answers from a local
model, and a local web page with graph views.

```
Legistar API --ingest--> bronze (raw JSON) --build--> silver (graph as Parquet)
                                                        |-- DuckDB       interactive lookups
                                                        |-- GraphFrames  batch graph analytics
                                                        '--index--> gold (passages + embeddings)
                                                                      '-- search / ask / serve (web page)
refresh = ingest + build + index
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
uv run legistutor serve               # the web page, at http://127.0.0.1:8765/
uv run legistutor refresh             # later: fetch what changed, rebuild graph and index
uv run pytest
```

`index`, `search`, `ask`, `serve` and `refresh` need [Ollama](https://ollama.com)
running locally with the embedding model (`ollama pull qwen3-embedding:0.6b`).
`ask` and the page's answers also need a chat model; see
[Asking questions](#asking-questions).

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
  interface (`ChatProvider`). A Databricks model serving endpoint would be a second
  class there; nothing above it changes.

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

Opening an item splits the window: the page stays on the left and the details open on
the right. Drag the divider, or focus it and use the arrow keys, to resize; the width
is remembered. Below 900 pixels wide the details cover the page instead.

Each chart has a table or list form beside it, and tooltips appear on keyboard focus
as well as on hover. Deep links: `/?open=26-0333` opens an item, `/?q=road+repairs`
runs a search, `/?q=light+rail&topic=1` charts a topic, `/#coming-up` opens the queues.

[web.py](src/legistutor/web.py) is a JSON API (`/api/search`, `/api/ask`,
`/api/matter`, `/api/node`, `/api/queues`, `/api/topic`, `/api/meta`) on the standard library's HTTP
server; the page is [static/index.html](src/legistutor/static/index.html). The
`App` class holds the logic and does not know about HTTP, so it can move behind
another framework (a Databricks App, for instance) without changes.

## Keeping it current

```
uv run legistutor refresh
```

runs `ingest` for every stage (only what changed upstream is fetched), then `build`
and `index` (only new or edited passages are embedded). It needs Ollama running for
the embedding step, and exits non-zero if any fetch failed. To run it every morning
on Windows:

```
schtasks /Create /TN "legistutor refresh" /SC DAILY /ST 06:00 /TR "cmd /c cd /d C:\path\to\legistutor && uv run legistutor refresh >> data\logs\refresh.log 2>&1"
```

Restart `legistutor serve` after a refresh; it loads the index when it starts. If the
port is already being served, `serve` says so and stops; use `--port` for a second copy.

## GraphFrames (optional)

For graph algorithms (motif finding, PageRank, connected components) the same files
load into GraphFrames on a local Spark session:

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
