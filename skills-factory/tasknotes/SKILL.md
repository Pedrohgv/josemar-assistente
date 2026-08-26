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

## Planning states

Every task is in exactly one of three effective planning states:

| State | Frontmatter |
|---|---|
| Backlog | neither `scheduled` nor `planned_week` |
| Week-planned | `planned_week` only — the Monday (`YYYY-MM-DD`) of the target week |
| Day-scheduled | native `scheduled` only |

- `planned_week` is a first-class argument on `task_create` and `task_update`
  (`clear_planned_week` on update): a week-only target stored as the ISO
  Monday date. It is not a commitment to any single day.
- `scheduled` and `planned_week` are mutually exclusive. Setting either
  automatically clears the other; `clear_scheduled` with no new target
  removes both (Backlog).
- Never pass `planned_week` through `custom_fields` — the key is reserved
  and rejected there. Setting it also requires the profile to define a
  `planned_week` user field of type `date`.
- Direct Obsidian edits can leave both keys on one task; reads tolerate that,
  and the next MCP write keeps `scheduled` and drops stale `planned_week`.

Profile configuration, legacy `scheduled_week` migration, and effective-week
Base setup: see `references/custom-fields.md` and `docs/tasknotes-mcp.md`.

## Tools

- `task_create`: create one task. The slug is optional; when omitted, a
  timestamp-prefixed slug is auto-generated from the title
  (e.g. `2026-07-18-143000-buy-groceries`). When provided, the slug must be
  lowercase with no spaces or special characters (hyphens and underscores
  allowed). Slugs are immutable after creation. Supports `recurrence` for
  native TaskNotes recurrence rules (RFC 5545 RRULE string, e.g.
  `FREQ=WEEKLY;BYDAY=MO,WE,FR`). Accepts `custom_fields` keyed by field key
  from the profile's user field definitions; see
  `references/custom-fields.md` for types, validation, and how to define
  custom fields in the plugin configuration. Also accepts `planned_week`
  (Monday `YYYY-MM-DD`, mutually exclusive with `scheduled`).
- `task_get`: read one task by slug. Includes `planned_week` when set.
- `task_list`: list structured task metadata with a bounded result count.
  Supports optional filters: `status`, `priority`, `tag`, and `archived`
  (combine with AND logic). Results include `planned_week` when set.
- `task_update`: change status, priority, due date, scheduled date,
  projects, custom user fields, or the body. Use the explicit clear flags to remove
  optional fields. Pass `custom_fields` with `null` values to clear custom
  fields. Same dict shape as `task_create`; see `references/custom-fields.md`.
  The optional `body` field edits the task body: `body=None` (default) leaves
  the body unchanged, `body=""` clears it, and a string replaces the body
  content. Title edits are not supported. `planned_week=<Monday>` switches
  the task to week planning and clears `scheduled`; `clear_planned_week`
  removes only the week plan.
- `task_complete`: complete a task. Omit the completion date to use today in the
  configured timezone.
- `task_archive`: add the configured archive tag. This is idempotent.
- `task_delete`: remove a task. This is the only mutation that deletes a file
  from the vault rather than writing through the source-routed gbrain capture
  path. It first runs `gbrain delete <slug>` (soft-delete from the gbrain
  index) as a confirmation gate, then `git rm` removes the file from disk and
  stages the removal, and a Git commit records the deletion. The deleted file
  is recoverable via `git revert`; no data is permanently lost. If
  `gbrain delete` succeeds but `git rm` fails, the adapter returns
  `recovery_required` (the gbrain index is modified but the file remains on
  disk and the next sync re-imports it). There is no `--force` path; the
  gbrain soft-delete gate must succeed before the file is touched. See
  `docs/tasknotes-mcp.md` for the full deletion and recovery model.
- `task_add_tag`: add a custom tag to a task. Rejects the task-identification
  tag and the archive tag (use `task_archive` for archival). Idempotent.
- `task_remove_tag`: remove a custom tag from a task. Rejects the
  task-identification tag and the archive tag. Idempotent.

Dates must use `YYYY-MM-DD`; `planned_week` must additionally be a Monday.
Keep slugs lowercase and treat them as immutable.
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

The TaskNotes plugin is configured via `<vault>/.obsidian/plugins/tasknotes/data.json`.
The MCP server reads this profile dynamically on every operation and adapts to
the configured values. All settings can be edited in the Obsidian UI
(Settings → TaskNotes) or directly in the JSON file.

The only hard requirement is `taskIdentificationMethod: "tag"` — the adapter's
entire model depends on identifying tasks by tag. Everything else (statuses,
priorities, folders, filename format, archive behavior, custom user fields) is
config-adaptive and validated against the live profile. Views are user-owned
`.base` files in `TaskNotes/Views/`; create and customize them in Obsidian.

**Details:** `references/custom-fields.md` (custom user fields, reserved
`planned_week` date field) and `docs/tasknotes-mcp.md` (profile requirements,
effective-week Bases, migration runbook). Full reference: <https://tasknotes.dev/>

## Boundaries

Task task-file writes must go through these tools. Do not use native `gbrain
put` or `gbrain capture` to create or modify TaskNotes task files. Native gbrain
remains appropriate for non-task vault pages.

This interface does not currently support unarchive, search,
rename/move, title edits, bulk operations, raw frontmatter,
or inline-task conversion. (Delete is supported via `task_delete`;
body edits are supported via `task_update`'s `body` field.) Suggest
Obsidian for an unsupported task edit rather than approximating it with
another writer.

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
