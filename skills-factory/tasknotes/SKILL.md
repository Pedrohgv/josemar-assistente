---
name: tasknotes
description: Manage durable TaskNotes tasks through the bounded task lifecycle tools. Use cron separately for time-triggered reminders.
categories:
  - tasks
  - productivity
  - planning
---

# TaskNotes

Use the TaskNotes MCP tools for durable, actionable items that need task state,
priority, dates, projects, completion, or archival.

## Task or reminder

- Use TaskNotes for work the user wants tracked until it is completed.
- Use Hermes cron/reminder tooling for a notification that must fire at a
  particular time.
- If the user asks for both tracking and a timed notification, create the task
  and reminder separately. Do not assume one replaces the other.
- If the request is ambiguous and the distinction matters, ask whether the user
  wants a tracked task, a timed reminder, or both.

## Tools

- `task_create`: create one task. The slug is optional; when omitted, a
  timestamp-prefixed slug is auto-generated from the title
  (e.g. `2026-07-18-143000-buy-groceries`). When provided, the slug must be
  lowercase with no spaces or special characters (hyphens and underscores
  allowed). Slugs are immutable after creation.
- `task_get`: read one task by slug.
- `task_list`: list structured task metadata with a bounded result count.
- `task_update`: change only status, priority, due date, scheduled date, or
  projects. Use the explicit clear flags to remove optional fields.
- `task_complete`: complete a task. Omit the completion date to use today in the
  configured timezone.
- `task_archive`: add the configured archive tag. This is idempotent.

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

## Boundaries

Task task-file writes must go through these tools. Do not use native `gbrain
put` or `gbrain capture` to create or modify TaskNotes task files. Native gbrain
remains appropriate for non-task vault pages.

This interface does not support unarchive, delete, search, rename/move, title or
body edits, arbitrary tag replacement/removal, recurrence changes, bulk
operations, raw frontmatter, or inline-task conversion. Suggest Obsidian for an
unsupported task edit rather than approximating it with another writer.

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
