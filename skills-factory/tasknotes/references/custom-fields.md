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

## Discovery

- Custom field values are **not** included in `task_list` output — only standard
  modeled fields (title, status, priority, due, scheduled, projects,
  completedDate) are returned in listings.
- Custom field values are present in `task_get` output within the frontmatter.
  The frontmatter keys match the field's `key` as defined in the profile.
- There is no dedicated "list custom fields" tool. The set of available custom
  fields is defined in the profile's `data.json` and can be discovered by
  inspecting a task or by reading the plugin configuration file directly.
