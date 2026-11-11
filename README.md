# legistar-scraper

Collects Hennepin County's legislative record (meetings, agenda items, resolutions,
actions, votes) from the public [Legistar Web API](https://webapi.legistar.com/Help)
so it can be searched and questioned with a retrieval-augmented LLM.

Status: proof of concept. Raw ingestion (bronze) and the graph tables (silver) work.
The vector index and question answering layers are not built yet.

```
Legistar API --ingest--> bronze (raw JSON) --build--> silver (graph as Parquet)
                                                        |-- DuckDB       interactive lookups
                                                        '-- GraphFrames  batch graph analytics
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
uv run pytest
```

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
  vector index will be built from.
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
