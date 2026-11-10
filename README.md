# legistar-scraper

Collects Hennepin County's legislative record (meetings, agenda items, resolutions,
actions, votes) from the public [Legistar Web API](https://webapi.legistar.com/Help)
so it can be searched and questioned with a retrieval-augmented LLM.

Status: proof of concept. Raw ingestion (bronze) works; the graph tables, vector
index and question answering layers are not built yet.

## Quick start

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you.

```
uv sync
uv run legistar ingest              # reference data, meetings, matters
uv run legistar ingest --stages votes
uv run legistar status
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
