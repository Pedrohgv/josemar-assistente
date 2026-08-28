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

- Use TaskNotes for work the user wants tracked until it is completed. Use
  Hermes cron for agent-triggered actions (run a report, sync state, send a
  digest) that are not task lifecycle operations.
- Native recurrence (repeating tasks) belongs to `task_create` recurrence
  rules, never Hermes cron.
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

## Daily Note links (opt-in)

When the operator enables `TASKNOTES_DAILY_LINKS_ENABLED` (default off),
scheduling mutations own the Daily Note projection: `task_create`,
`task_update`, and `task_delete` automatically maintain exactly one
`- [[slug|title]]` line in the matching date's Daily Note `## Tasks`
section — reschedules move it; clearing scheduling or deleting the task
removes it. Never edit a projected link manually: the adapter owns it, and
direct Obsidian task edits are not re-projected. Backlog and week-planned
tasks are never projected; completion and archive keep the link. Flag off
(default) means unchanged behavior.

**Details:** `references/daily-notes.md` and `docs/tasknotes-mcp.md`.

## Tools

- `task_create`: create one task. The slug is optional; when omitted, a
  timestamp-prefixed slug is auto-generated from the title
  (e.g. `2026-07-18-143000-buy-groceries`); when provided, it must be
  lowercase with no spaces or special characters and is immutable. Supports
  `recurrence` for native TaskNotes recurrence rules (RFC 5545 RRULE string,
  e.g. `FREQ=WEEKLY;BYDAY=MO,WE,FR`), `custom_fields` keyed by the profile's
  user field definitions (see `references/custom-fields.md`), and
  `planned_week` (Monday `YYYY-MM-DD`, mutually exclusive with `scheduled`).
- `task_get`: read one task by slug. Includes `planned_week` when set.
- `task_list`: list structured task metadata with a bounded result count.
  Supports optional filters: `status`, `priority`, `tag`, and `archived`
  (combine with AND logic). Results include `planned_week` when set.
- `task_update`: change status, priority, due date, scheduled date,
  projects, custom user fields, or the body. Use the explicit clear flags to
  remove optional fields; pass `custom_fields` with `null` values to clear
  custom fields (same dict shape as `task_create`). The optional `body`
  field: `body=None` (default) leaves the body unchanged, `body=""` clears
  it, a string replaces it. Title edits are not supported.
  `planned_week=<Monday>` switches to week planning and clears `scheduled`;
  `clear_planned_week` removes only the week plan.
- `task_complete`: complete a task. Omit the completion date to use today in the
  configured timezone.
- `task_archive`: add the configured archive tag. This is idempotent.
- `task_delete`: remove a task. The only mutation that deletes a vault file:
  `gbrain delete <slug>` runs first as a soft-delete confirmation gate (no
  `--force` path), then `git rm` + a Git commit remove the file. Deletions
  are recoverable via `git revert`. If `gbrain delete` succeeds but `git rm`
  fails, the adapter returns `recovery_required`. See `docs/tasknotes-mcp.md`
  for the full deletion and recovery model.
- `task_add_tag`: add a custom tag to a task. Rejects the task-identification
  tag and the archive tag (use `task_archive` for archival). Idempotent.
- `task_remove_tag`: remove a custom tag from a task. Rejects the
  task-identification tag and the archive tag. Idempotent.

Dates must use `YYYY-MM-DD`; `planned_week` must additionally be a Monday.
Keep slugs lowercase and treat them as immutable.
Use the status and priority values accepted by the current TaskNotes profile;
if the profile rejects a value, report that constraint instead of guessing.

## Naming convention

Adapter-created tasks use `YYYY-MM-DD-HHmmss-slugified-title`
(e.g. `2026-07-18-143000-buy-groceries`): the timestamp prefix keeps
chronological ordering and the slugified title matches gbrain's slug rules.
This matches the plugin's recommended `timestamp` filename format
(`2026-07-18-143000`), so adapter-created and plugin-created tasks sort
together in the file explorer. Explicit slugs must already be gbrain-safe
(lowercase, hyphens, no spaces); the adapter does not slugify them.

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

This interface does not currently support unarchive, search, rename/move,
title edits, bulk operations, raw frontmatter, or inline-task conversion
(delete is supported via `task_delete`; body edits via `task_update`'s
`body` field). Suggest Obsidian for an unsupported task edit rather than
approximating it with another writer.

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
