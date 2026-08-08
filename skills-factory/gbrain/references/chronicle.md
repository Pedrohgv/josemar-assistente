# Life Chronicle — Full Reference

**Load with:** `skill_view("gbrain", file_path="references/chronicle.md")`

**This is a reference file — invisible to the skill system. Only loaded when the parent skill routes to it. Load when designing workflows that ingest, query, or build on chronicle data.**

## Concept: Notes and Atoms

Chronicle uses two distinct concepts that are easy to conflate:

| Concept | What it is | Where it lives |
|---|---|---|
| **Depth page** (note) | The full meeting/conversation/calendar-event note | `meetings/`, `conversations/`, `cal/`, `calendar/` (as `.md` files in the vault) |
| **Timeline atom** (event) | A single discrete fact extracted from a note (one decision, one commitment, one action item) | gbrain DB only (pages table + timeline_entries index), NOT as `.md` files |

**One depth page produces multiple atoms.** A meeting with 5 decisions and 3 action items produces 8 atoms (plus 1 "meeting" atom for the event itself). The depth page is the full context; the atoms are the queryable timeline entries.

The atom's `depth` field points back to the depth page slug, so you can always navigate from an atom to its full source.

## Eligible Page Types

Chronicle processes pages that are typed or path-prefixed as one of:

| Page type | Path prefix | Example |
|---|---|---|
| `meeting` | `meetings/` | `meetings/2026-07-24-elton-bora` |
| `conversation` | `conversations/` | `conversations/2026-07-20-podcast-interview` |
| `calendar-event` | `cal/` or `calendar/` | `cal/2026-08-01-team-offsite` |

**Diary pages (`life/diary/`) are excluded by design** — privacy guard. Diary interiority is never mined into events.

## Event Schema (Timeline Atom)

Each atom stored in the gbrain DB has this structure:

```yaml
type: event
event:
  when: 2026-07-09                  # ISO datetime or YYYY-MM-DD
  who:                               # array of entity slugs
    - people/madson-brommenschenkel
  what: "Pipeline de boletos as pilot project"  # one-clause summary
  where: Google Meet                  # optional, null if unknown
  kind: decision                     # one of the kind taxonomy below
  depth: meetings/2026-07-09-madson  # backreference to the source page
captured_via: life-chronicle:auto   # provenance marker
```

### Field reference

| Field | Type | Required | Description |
|---|---|---|---|
| `event.when` | string (ISO datetime or YYYY-MM-DD) | yes | When the event happened. Falls back to depth page's `date` frontmatter, then `effective_date`. |
| `event.who` | array of strings | yes | Entity slugs of participants. Falls back to depth page's `attendees` / `people` / `who` frontmatter. |
| `event.what` | string | yes | One-clause summary (max 120 chars in title, full text in compiled_truth). |
| `event.where` | string \| null | no | Location. null if unknown. |
| `event.kind` | string | yes | One of the taxonomy below. Normalized to lowercase. |
| `event.depth` | string | yes | Slug of the source depth page (backreference). |
| `captured_via` | string | yes | Provenance marker. `life-chronicle:auto` for auto-emission. |

## Kind Taxonomy

The LLM judge classifies each atom into one of these kinds. Anything not in this list is normalized to `event`.

| Kind | Meaning |
|---|---|
| `meeting` | The meeting/conversation/event itself |
| `call` | A call or video conference |
| `meal` | A meal (breakfast, lunch, dinner) |
| `solo` | A solo activity |
| `travel` | Travel event |
| `work` | General work activity |
| `commitment` | A promise to do something (action item) |
| `decision` | A decision made |
| `intro` | An introduction between people |
| `conflict` | A conflict or disagreement |
| `milestone` | A milestone reached |
| `event` | Fallback for anything not classified |

## Query Commands

| Command | Returns |
|---|---|
| `gbrain day <YYYY-MM-DD> [--week] [--narrative]` | Timeline atoms on a date (or ISO week), chronological |
| `gbrain since <YYYY-MM-DD> [--kind <K>]` | Atoms on/after a date, optionally filtered by kind |
| `gbrain last-seen <entity-slug>` | When an entity was last seen in any atom (who, depth, or both) |
| `gbrain on-this-day` | Prior-year atoms on this month-day |
| `gbrain orient [--days 7] [--entities a,b]` | Recent timeline (last 7 days) + per-entity ontology in one zero-LLM call |

### `gbrain day` output structure

```yaml
- date: 2026-07-09
  summary: "Pipeline de boletos as pilot project"
  detail: ""
  source: "life-chronicle:event:life/events/2026-07-09-96789ae1"
  page_id: 164                    # DB row id of the atom
  page_slug: "meetings/2026-07-09-madson"  # DB slug of the atom
  event_page_id: 577              # DB row id of the depth page
  event_slug: "meetings/2026-07-09-madson"  # backreference slug
  effective_date: "2026-07-08 21:00:00-03"
  kind: decision
```

Note: `page_slug` is the atom's DB slug (not a filesystem file). `event_slug` is the backreference to the depth page.

## Ontology (Entity Property Tracking)

Beyond timeline atoms, chronicle maintains a **bi-temporal ontology** — per-entity properties with sourcing, confidence, and time-travel. The `facts` table tracks properties about entities (people, companies, etc.) with:

- **dimension** — the property name (e.g., `role`, `employer`, `location`)
- **value** — the property value
- **source** — where this fact came from (which depth page, which event)
- **confidence** — how certain (0–1)
- **valid_from** / **valid_to** — temporal validity window

`gbrain orient` returns recent timeline atoms + the current ontology for the specified entities. `gbrain ontology <entity> --asof <date>` time-travels to what was true at a specific date.

## Backfill (Existing Pages)

To extract events from existing meeting/conversation/calendar-event pages:

```bash
# Enqueue extraction jobs (returns scanned/eligible/enqueued counts)
gbrain chronicle-backfill [--since YYYY-MM-DD] [--limit N] [--dry-run]

# On PGLite, run jobs inline (no background worker)
gbrain jobs submit chronicle_extract \
  --params '{"slug":"<meeting-slug>","sourceId":"default"}' \
  --follow
```

Auto-emission (on every new `put_page` of an eligible page) requires `auto_chronicle=true` (operator config). As of v0.42.73.2, `auto_chronicle` is a registered config key and no longer requires the `--force` flag that the v0.42.57.0 config-key registry bug forced.

## Where Events Are Stored

Events are stored **in the gbrain database only** — NOT as `.md` files in the vault filesystem. They are queryable via `gbrain day`, `gbrain get`, etc., but are not visible in Obsidian as files. This is by design (write-through doesn't fire for chronicle jobs running in the worker context).

If you need filesystem-visible notes for an event, you must create them manually via `gbrain put <slug> --content "<full page>"` for inline content, or `gbrain capture --file PATH --slug SLUG` for file-based content (the chronicle judge does not create filesystem files).

## Common Workflows

**"What happened this week?"** → `gbrain day <monday> --week` or `gbrain since <monday>`
**"When did I last talk to X?"** → `gbrain last-seen people/<slug>`
**"What decisions were made about Y?"** → `gbrain since <date> --kind decision` then filter results to the same day and the relevant depth pages (or `gbrain day <date>` then filter the returned atoms by `kind: decision` and the relevant depth page). Do not pass `--kind` to `gbrain day`: pinned gbrain 0.42.73.2 silently ignores that unsupported flag instead of rejecting it.
**"Session startup context"** → `gbrain orient --days 7 --entities people/x,people/y` (zero-LLM, fast)
**"Ingest a new meeting"** → Write to `meetings/<date>-<slug>` via `gbrain put <slug> --content "<full meeting note>"` for inline content, or `gbrain capture --file PATH --slug meetings/<date>-<slug>` for file-based meeting ingestion. Chronicle auto-extracts if `auto_chronicle=true`. Events appear in `gbrain day` for that date after processing.
