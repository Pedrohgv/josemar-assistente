---
name: tasknotes
description: Manage durable TaskNotes tasks through the bounded task lifecycle tools. TaskNotes supports native recurrence for repeating tasks.
categories:
  - tasks
  - productivity
  - planning
---

# TaskNotes

Use the TaskNotes MCP tools for durable, actionable items that need task state,
priority, dates, projects, completion, or archival.

**Authoritative reference:** <https://tasknotes.dev/>

## Task or reminder

- Use TaskNotes for work the user wants tracked until it is completed.
- Use Hermes cron for agent-triggered actions (run a report, sync state, send
  a digest) that are not task lifecycle operations.
- TaskNotes supports native recurrence (repeating tasks). Use `task_create`
  with recurrence rules for tasks that repeat on a schedule. Do not use Hermes
  cron for task recurrence.
- If the request is ambiguous and the distinction matters, ask whether the user
  wants a tracked task, a recurring task, or an agent-triggered action.

## Tools

- `task_create`: create one task. The slug is optional; when omitted, a
  timestamp-prefixed slug is auto-generated from the title
  (e.g. `2026-07-18-143000-buy-groceries`). When provided, the slug must be
  lowercase with no spaces or special characters (hyphens and underscores
  allowed). Slugs are immutable after creation. Supports `custom_fields` for
  plugin-configured custom user fields (text, list, date, number, boolean,
  link) and `recurrence` for native TaskNotes recurrence rules (RFC 5545 RRULE
  string, e.g. `FREQ=WEEKLY;BYDAY=MO,WE,FR`).
- `task_get`: read one task by slug.
- `task_list`: list structured task metadata with a bounded result count.
  Supports optional filters: `status`, `priority`, `tag`, and `archived`
  (combine with AND logic).
- `task_update`: change only status, priority, due date, scheduled date,
  projects, or custom user fields. Use the explicit clear flags to remove
  optional fields. Pass `custom_fields` with `None` values to clear custom
  fields.
- `task_complete`: complete a task. Omit the completion date to use today in the
  configured timezone.
- `task_archive`: add the configured archive tag. This is idempotent.
- `task_add_tag`: add a custom tag to a task. Rejects the task-identification
  tag and the archive tag (use `task_archive` for archival). Idempotent.
- `task_remove_tag`: remove a custom tag from a task. Rejects the
  task-identification tag and the archive tag. Idempotent.

Dates must use `YYYY-MM-DD`. Keep slugs lowercase and treat them as immutable.
Use the status and priority values accepted by the current TaskNotes profile;
if the profile rejects a value, report that constraint instead of guessing.

## Naming convention

Adapter-created tasks use the format `YYYY-MM-DD-HHmmss-slugified-title`
(e.g. `2026-07-18-143000-buy-groceries`). The timestamp prefix ensures
chronological ordering by filename. The slugified title provides human
readability. This matches gbrain's slug constraints (lowercase, hyphens, no
spaces) and is consistent with the TaskNotes plugin's `timestamp` filename
format (`2026-07-18-143000`), so both adapter-created and plugin-created tasks
sort chronologically in the file explorer.

When `task_create` is called without a slug, the adapter generates one
automatically. When a slug is provided explicitly, it must already be
gbrain-safe (lowercase, hyphens, no spaces).

## Plugin configuration

The TaskNotes plugin is configured by the user via the Obsidian UI. All
settings are stored in `<vault>/.obsidian/plugins/tasknotes/data.json`. The
MCP server reads this profile dynamically on every operation and adapts to
the configured values.

### Key configuration areas

| Setting area | What it controls | UI path |
|---|---|---|
| Task Properties → Status | Custom status labels and order | Settings → TaskNotes → Task Properties → Status |
| Task Properties → Priority | Custom priority levels and order | Settings → TaskNotes → Task Properties → Priority |
| Task Properties → Custom User Fields | Arbitrary fields (text, list, date, link) | Settings → TaskNotes → Task Properties → Custom User Fields |
| Task Defaults → Folder | Where task files are created (`tasksFolder`) | Settings → TaskNotes → Task Defaults |
| Task Defaults → Filename | Format of task filenames | Settings → TaskNotes → Task Defaults |
| Views | `.base` files in `TaskNotes/Views/` | Command palette or ribbon icon |

### What the adapter requires vs. adapts to

The only hard requirement is `taskIdentificationMethod: "tag"` — the adapter's
entire model depends on identifying tasks by tag. All other settings are
config-adaptive:

- `tasksFolder`: read dynamically; the adapter writes to whatever folder is
  configured.
- `archiveFolder`: when `moveArchivedTasks` is true, the adapter checks both
  the tasks folder and the archive folder for existing tasks.
- `storeTitleInFilename` and `taskFilenameFormat`: do not affect adapter-created
  tasks because gbrain writes files with explicit slugs and the frontmatter
  title always takes precedence.
- `customStatuses` and `customPriorities`: the adapter validates against the
  configured sets and rejects invalid values.

### Current limitations

The adapter does not yet support:

- Unarchive (removing the archive tag without a full unarchive workflow).
- Delete, rename/move, title or body edits, bulk operations, raw frontmatter,
  or inline-task conversion.

### Views (.base files)

Views are YAML files stored in `TaskNotes/Views/`. They control how tasks are
displayed. Key properties:

- `groupBy.property`: what to group columns by (`task.status`,
  `note.pipeline_stage`, etc.)
- `config.swimLane`: optional second grouping dimension
- `config.hideEmptyColumns`: hide columns with no tasks
- `config.pinnedColumns`: always-visible columns
- Filters: configured via Bases filter editor in Obsidian

Views are user state (not repo-owned). Create and customize them in Obsidian.

**Full reference:** <https://tasknotes.dev/>

## Boundaries

Task task-file writes must go through these tools. Do not use native `gbrain
put` or `gbrain capture` to create or modify TaskNotes task files. Native gbrain
remains appropriate for non-task vault pages.

This interface does not currently support unarchive, delete, search,
rename/move, title or body edits, recurrence, bulk operations, raw frontmatter,
or inline-task conversion. Suggest Obsidian for an unsupported task edit rather
than approximating it with another writer.

One author at a time per task file is required. Do not run parallel mutations
against the same task.

## Mutation outcomes

- `applied_and_committed`: complete.
- `not_applied`: an idempotent operation or empty update made no change.
- `applied_uncommitted`: the task change was applied but its Git commit failed;
  do not retry the mutation blindly.
- `db_updated_disk_failed` or `recovery_required`: do not retry. Tell the user
  that operator recovery is required. A recovery marker blocks later mutations
  when state is uncertain.