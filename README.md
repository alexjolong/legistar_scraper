# legistar-scraper

Collects Hennepin County's legislative record (meetings, agenda items, resolutions,
actions, votes) from the public [Legistar Web API](https://webapi.legistar.com/Help)
so it can be searched and questioned with a retrieval-augmented LLM.

Status: proof of concept, working end to end on one machine: ingestion (bronze), the
graph (silver), the search index (gold), hybrid search, and cited answers from either
Claude or a local model.

```
Legistar API --ingest--> bronze (raw JSON) --build--> silver (graph as Parquet)
                                                        |-- DuckDB       interactive lookups
                                                        |-- GraphFrames  batch graph analytics
                                                        '--index--> gold (passages + embeddings)
                                                                      '-- search / ask
```

## Quick start

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you.

```
uv sync
uv run legistar ingest              # reference data, meetings, matters
uv run legistar ingest --stages votes
uv run legistar status
uv run legistar build               # bronze -> silver graph tables, a few seconds
uv run legistar sql "select o.date, o.body_name, o.action from e_on_agenda o join v_matter m on m.id = o.src where m.file_number = '26-0333' order by o.date"
uv run legistar index               # silver -> gold search index; needs Ollama (see below)
uv run legistar search "embedded social workers in police departments"
uv run legistar ask "Where does 26-0333 stand, and what was it about?"
uv run pytest
```

`index`, `search` and `ask` need [Ollama](https://ollama.com) running locally with the
embedding model: `ollama pull qwen3-embedding:0.6b`. `ask` also needs either an
Anthropic API key or a local chat model (`ollama pull gemma3:12b`); see
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

`legistar build` rebuilds everything under `data/silver/legistar/hennepinmn/` from
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

`legistar sql` and `legistar_scraper.query.connect()` expose every Parquet file as a
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

`legistar index` writes one table, `data/gold/legistar/hennepinmn/chunks.parquet`:
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
chosen because Databricks serves the same model. Set `LEGISTAR_EMBED_MODEL` to use
another; the query-side prefixes for Qwen3, BGE and GTE are in
[embed.py](src/legistar_scraper/embed.py). Ollama runs a quantised build, so after a
move to Databricks re-embed there rather than mixing vectors from both.

## Searching

```
uv run legistar search "road reconstruction" --since 2026-06-01 --body "Public Works"
```

`legistar_scraper.search.Searcher` combines three signals:

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
with `legistar sql` (the example under [Querying](#querying) does exactly this)
until the answering layer can run such queries itself.

## Asking questions

```
uv run legistar ask "What has the county done about embedded social workers in police departments?"
uv run legistar ask "Where does 26-0333 stand?" --provider ollama
uv run legistar ask "opioid settlement spending" --model claude-haiku-5-5 --since 2026-01-01
```

`ask` runs the same search, renders each matter as a numbered source, and has the
model answer from those sources only, citing them as `[1]`, `[2]`. The sources are
printed under the answer with links to the county's site. The prompt tells the
model what this record cannot show: discussion is not minuted, the staff background
is the department's case rather than the board's reasoning, and amounts are what a
resolution authorised, not what a contract finally said.

Two providers sit behind one interface ([llm.py](src/legistar_scraper/llm.py)):

| Provider | Default model | Needs |
|---|---|---|
| `anthropic` (default) | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| `ollama` | `gemma3:12b` | `ollama pull gemma3:12b`; about 8 GB of GPU memory |

Settings come from the environment, or from a `.env` file in the project directory
(git-ignored; copy [.env.example](.env.example)):

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | | for the `anthropic` provider |
| `LEGISTAR_LLM_PROVIDER` | `anthropic` | or `ollama` |
| `LEGISTAR_ANTHROPIC_MODEL` | `claude-sonnet-5-5` | e.g. `claude-haiku-5-5` |
| `LEGISTAR_LLM_EFFORT` | `medium` | Claude's effort level |
| `LEGISTAR_OLLAMA_MODEL` | `gemma3:12b` | |
| `LEGISTAR_OLLAMA_NUM_CTX` | `16384` | context window requested from Ollama, in tokens |
| `LEGISTAR_EMBED_MODEL` | `qwen3-embedding:0.6b` | |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | |

Notes:

- A question with eight sources is a prompt of roughly 5,000 to 9,000 tokens.
- With Sonnet, requests carry the API's server-side refusal fallback
  (`fallbacks="default"`, a beta feature): if a safety classifier declines a request,
  the API retries it on its fallback model within the same call. Haiku has no such
  option.
- With Ollama, sources that would overflow the model's window are dropped from the
  bottom of the ranking and the output says how many. On a 12 GB card the first
  answer takes close to a minute while the model loads; later ones about ten seconds.
- The local model is noticeably looser with identifiers than Claude (it has merged a
  file number with an agreement number, and misattributed an item to a committee).
  Check its answers against the printed sources.
- On Databricks, add a third class beside `AnthropicChat` and `OllamaChat` that calls
  the workspace's model serving endpoint; nothing above `llm.py` changes.

## GraphFrames (optional)

For graph algorithms (motif finding, PageRank, connected components) the same files
load into GraphFrames on a local Spark session:

```
uv sync --extra spark
uv run legistar graph-demo
```

```python
from pathlib import Path
from legistar_scraper import graph

spark = graph.spark_session()
g = graph.load_graph(spark, Path("data"), "hennepinmn")
g.find("(m)-[c]->(s)").filter("c.relationship = 'cites'").show()
```

Setup notes:

- Spark 4.1 needs Java 17 or 21. `graph.spark_session()` uses `LEGISTAR_JAVA_HOME`
  if set, otherwise a JDK 17 or 21 under `~/.jdks`, otherwise `JAVA_HOME`.
- The GraphFrames JARs are downloaded from Maven Central on first use, checked
  against their published SHA-1, and cached in `~/.cache/legistar_scraper/jars`.
- On Windows this runs without `winutils.exe` because Spark only reads here. The
  "Did not find winutils.exe" warning is expected. Writing from Spark to local
  disk would need it.
- `connectedComponents` needs `use_local_checkpoints=True` on a local session.
- Spark takes several seconds to start, so use DuckDB for interactive questions.
- On Databricks, skip `spark_session()`: GraphFrames is preinstalled on ML runtimes,
  and the two `graph/` tables load directly from ADLS.
