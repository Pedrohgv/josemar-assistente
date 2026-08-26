# Custom User Fields

Custom user fields are arbitrary fields defined in the TaskNotes plugin profile
that can be set on tasks via `task_create` and `task_update`. Before a custom
field can be used in a task, it must be defined in the plugin's `data.json`.

## Defining custom fields in the profile

Custom fields are defined as entries in the `userFields` array within
`<vault>/.obsidian/plugins/tasknotes/data.json`.

Each entry is an object with:

| Property  | Type          | Required | Description |
|-----------|---------------|----------|-------------|
| `id`      | string        | yes      | Unique field identifier (arbitrary, used internally by TaskNotes) |
| `key`     | string        | yes      | The frontmatter key written to task files — this is what you pass in `custom_fields` |
| `type`    | string        | yes      | One of: `text`, `list`, `date`, `number`, `boolean`, `link`, `enum` |
| `label`   | string        | no       | Human-readable display label (defaults to `""`) |
| `options` | list of strings | required when `type` is `"enum"` | Allowed values for the enum field | |

**Constraints:**
- `id` must be unique across all user fields
- `key` must be unique across all user fields
- `key` must not collide with reserved frontmatter keys (`type`, `tags`, `slug`, `ingested_via`, `ingested_at`, `source_kind`)
- `key` must not collide with modeled field mapping values (the property names used for status, priority, due, scheduled, projects, completedDate)
- `planned_week` is a valid — and for week planning, required — `userFields`
  entry of type `date` (see "Week planning" below). Separately, the MCP's
  generic `custom_fields` argument reserves that key and always rejects it:
  callers set and clear week planning only through the first-class
  `planned_week`/`clear_planned_week` arguments

Example `data.json` snippet:

```json
{
  "userFields": [
    {"id": "pipeline_stage", "key": "pipeline_stage", "type": "text", "label": "Pipeline Stage"},
    {"id": "effort_hours", "key": "effort_hours", "type": "number", "label": "Effort hours"},
    {"id": "blocked", "key": "blocked", "type": "boolean", "label": "Blocked"},
    {"id": "review_date", "key": "review_date", "type": "date", "label": "Review date"},
    {"id": "extra_tags", "key": "extra_tags", "type": "list", "label": "Extra tags"},
    {"id": "related_note", "key": "related_note", "type": "link", "label": "Related note"},
    {"id": "team", "key": "team", "type": "enum", "label": "Team",
     "options": ["engineering", "design", "product", "marketing"]}
  ]
}
```

### Adding fields

Custom fields are defined in two ways:

1. **Obsidian UI (recommended)** — open Obsidian, go to Settings → TaskNotes →
   Task Properties → Custom User Fields, and add fields via the plugin UI.
   This is the normal path and keeps the profile consistent with the plugin's
   own validation.

2. **Directly edit `data.json`** — edit
   `<vault>/.obsidian/plugins/tasknotes/data.json` and add entries to the
   `userFields` array. This is an explicit operator action on an Obsidian
   plugin configuration file (NOT a task file). Use a JSON-aware editor
   (not a text editor that may corrupt JSON), back up the file first, and
   validate the JSON after editing. **Do not use `gbrain put` or
   `gbrain capture` to edit this file** — native gbrain is a vault-page
   authoring tool, not a safe arbitrary JSON-file editor, and using it here
   can corrupt the plugin profile. After editing, the MCP server picks up the
   new fields on the next operation (the profile is re-read on every locked
   operation).

## Using custom fields in tasks

Once defined in the profile, custom fields are passed as a `custom_fields` dict
keyed by the field's `key` (NOT the `id`):

**On create:**
```json
{
  "title": "Review Q3 report",
  "custom_fields": {
    "pipeline_stage": "review",
    "effort_hours": 3.5,
    "blocked": false,
    "review_date": "2026-08-15"
  }
}
```

**On update:**
Set a field value, or pass `null` to clear it:
```json
{
  "slug": "2026-07-26-143000-review-q3-report",
  "custom_fields": {
    "pipeline_stage": "done",
    "blocked": null
  }
}
```

## Per-type validation

The MCP server validates each custom field value against its declared type.
Unknown keys (not in the profile's `userFields`) are rejected.

| Type      | Accepted values                                    | Bounds |
|-----------|---------------------------------------------------|--------|
| `text`    | string                                             | max 500 chars |
| `list`    | list of strings                                    | max 50 items, each max 200 chars |
| `date`    | `YYYY-MM-DD` string, valid calendar date           | — |
| `number`  | `int` or `float` (booleans are rejected)           | — |
| `boolean` | `True` or `False` (bool only, not ints)            | — |
| `link`    | string                                             | max 500 chars |
| `enum`    | string matching one of the field's `options`       | must be in the defined options list |

## Week planning: the reserved `planned_week` field

`planned_week` (issue #128) is a first-class week-planning argument on
`task_create` and `task_update` — not a normal custom field. It stores the
ISO `YYYY-MM-DD` date of the target week's Monday and expresses week-only
intent ("sometime that week"), never a commitment to a specific day.

### Reservation and profile prerequisite

- `planned_week` is **rejected inside `custom_fields`**, even with a `null`
  value; clearing uses the dedicated `clear_planned_week` update flag. The
  reservation is enforced before any side effect, so the transition and
  invariant logic cannot be bypassed.
- Setting `planned_week` requires the profile to define a matching user
  field of type `date`. A missing definition or an incompatible type fails
  explicitly before any Git or gbrain mutation. Normalizing already-stored
  values during unrelated rewrites does NOT require the definition.

Safe profile configuration (add via the Obsidian UI when possible; see
"Adding fields" above for the guarded direct-edit procedure):

```json
{
  "userFields": [
    {"id": "planned_week", "key": "planned_week", "type": "date",
     "label": "Planned week"}
  ]
}
```

### Transition semantics

Every task is in exactly one of three effective planning states: Backlog
(neither key), week-planned (`planned_week` only), or day-scheduled (native
`scheduled` only). `scheduled` and `planned_week` are mutually exclusive on
MCP writes:

| Call | Resulting state |
|---|---|
| `task_create` with neither target | Backlog |
| create/update with `planned_week="<Monday>"` | Week-planned; on update, `scheduled` is removed |
| create/update with `scheduled="<date>"` | Day-scheduled; on update, `planned_week` is removed |
| update with `clear_scheduled=true` and no new target | Backlog (both keys removed) |
| update with `clear_planned_week=true` and no new target | `planned_week` removed; a manually set `scheduled` remains authoritative |

Contradictory or redundant combinations (supplying both targets, or pairing
a set with a clear of the same planning transition) are rejected before any
side effect. Values are never silently rounded: a non-Monday `planned_week`
is rejected.

### Manual edits and normalization

Direct Obsidian edits can leave both keys on one task. That inconsistency is
accepted for this interface: reads (`task_get`/`task_list`) never mutate
state to repair it, and the next MCP rewrite of that task (update, complete,
archive, tag changes — any non-delete mutation) keeps native `scheduled`
(scheduled wins) and drops the stale `planned_week`, so no MCP write ever
persists the inconsistent pair.

### Read visibility exception

Unlike ordinary custom user fields, `planned_week` is promoted into
structured `task_list` results and `task_get` output, so automated callers
can distinguish Backlog, week-planned, and day-scheduled tasks without raw
frontmatter access.

### Legacy `scheduled_week` migration

An earlier design discussion used a `scheduled_week` metadata key. It is not
retained as a cache or alias. Operators migrate deliberately — the full
sequence is in `docs/tasknotes-mcp.md` → "Legacy `scheduled_week`
migration":

1. Define the `planned_week` date user field (snippet above).
2. Re-express genuine week-only intent as Monday dates via
   `task_update(planned_week=...)`.
3. While the legacy field is still configured as a user field, clear stale
   `scheduled_week` values through the bounded MCP path:
   `task_update(custom_fields={"scheduled_week": null})`.
4. Update private Base views to effective-week grouping, then retire the
   legacy field from the TaskNotes configuration. Clearing must precede
   retirement: after the field leaves the profile, the generic
   `custom_fields` argument rejects the unknown key.

**Rollback:** code rollback restores the prior MCP schema; vault-side
rollback is to stop using/remove `planned_week` values and restore the prior
private Base. No schema/database migration and no TaskNotes upgrade is
involved in either direction. Private `.base` files and vault task state are
operator-owned: this repository's change never commits or manipulates them.

## Discovery

- Custom field values are **not** included in `task_list` output — only standard
  modeled fields (title, status, priority, due, scheduled, projects,
  completedDate) are returned in listings. The sole exception is the semantic
  `planned_week` key, which is always promoted into `task_list` and `task_get`
  output (see "Week planning" above).
- Ordinary custom field values are not included in structured `task_get` output;
  the sole exception is the semantic `planned_week` key described above.
- There is no dedicated "list custom fields" tool. The set of available custom
  fields is defined in the profile's `data.json` and can be discovered by
  inspecting a task or by reading the plugin configuration file directly.
