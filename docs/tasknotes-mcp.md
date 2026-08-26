# TaskNotes MCP Operations

Josemar ships a bounded stdio MCP adapter for durable TaskNotes tasks. The
authoritative TaskNotes documentation is [tasknotes.dev](https://tasknotes.dev/).

The adapter uses short-lived native gbrain CLI commands. It does not run
`gbrain serve`, open PGLite directly, or write task files directly. Gbrain is
the sole task writer. The MCP exposes only:

- `task_create`
- `task_get`
- `task_list`
- `task_update`
- `task_complete`
- `task_archive`
- `task_delete`
- `task_add_tag`
- `task_remove_tag`

There is no unarchive, search, rename/move, bulk, raw
Markdown/frontmatter, or title edit API. `task_update` accepts an optional
`body` field: `body=None` (default) leaves the body unchanged, `body=""`
clears the body, and a string replaces the body content. Title edits remain
unsupported.

`task_create` accepts an optional `recurrence` field (RFC 5545 RRULE string,
e.g. `FREQ=WEEKLY;BYDAY=MO,WE,FR`) for native TaskNotes repeating tasks. The
profile must declare a `recurrence` field mapping; otherwise `task_create`
rejects the call before any Git or gbrain mutation.

`task_create` and `task_update` also accept the semantic `planned_week`
argument for week-only planning (issue #128); see "Week planning" below.

## Access non-negotiables (issue #110)

The public `gbrain` command (safe by default) is the single
agent-facing gateway for general vault access; see
`docs/gbrain-operations.md` → "Issue #110: Safe gbrain Adapter". The TaskNotes
MCP does NOT use the public wrapper: it is already the lock owner and stays
implemented on short-lived internal native gbrain commands (the reference
bounded pattern).

- **No root execution.** Every adapter and operator command runs as the Hermes
  runtime user, never as root.
- **No concurrent PGLite opens.** The adapter never opens PGLite directly, and
  no other path may hold the gbrain database open concurrently.
- **Cooperative flock.** `/opt/data/.locks/tasknotes.lock` is the global
  serialization point: every TaskNotes transaction takes it; the
  `gbrain-refresh` and `gbrain-embedding-refresh` crons take it nonblockingly
  and skip when busy; backfills take it exclusively; the public `gbrain`
  command acquires it through the same lock runner.
- **No nested wrapper usage.** TaskNotes is implemented on short-lived internal
  native gbrain commands and must NEVER route through the public `gbrain`
  wrapper's lock path inside its transaction path (it retains its
  transaction-level global lock). Conversely, the public wrapper must never be
  used for TaskNotes task-file mutations and must never call into TaskNotes.
  Task-file mutations go through the `task_*` MCP tools only.
- **Pause ALL THREE owned jobs for maintenance windows.** For recovery,
  reindex/rebuild, migrations, vault swaps, and unadapted/third-party
  diagnostics, the operator pauses `gbrain-refresh`,
  `gbrain-embedding-refresh`, AND `vault-recovery-export` — a lock-held
  export would repopulate state inside the window (procedure in
  `docs/gbrain-operations.md` → "Cron Pause/Resume for Maintenance
  Windows"). Routine adapted access does not require pausing.

## External prerequisites

Repo deployment does not apply these changes automatically. Complete all of
them before enabling task mutations in production.

### 1. Verify the existing gbrain Git repository

Native gbrain sync already requires `$GBRAIN_BRAIN_REPO` to be a Git repository
with a valid `HEAD`. A Josemar vault that has completed `josemar-gbrain reindex`
or a periodic refresh already satisfies this requirement; the TaskNotes adapter
reuses that history and does not introduce a second repository. Do not
reinitialize an existing vault. This repository is local-only gbrain
infrastructure: Josemar does not publish it, pull from it, or use it as a remote
backup channel.

Verify the current repository as the Hermes runtime user:

```bash
docker compose exec hermes su -s /bin/sh hermes -c \
  'git -C "$GBRAIN_BRAIN_REPO" rev-parse --is-inside-work-tree &&
   git -C "$GBRAIN_BRAIN_REPO" rev-parse --verify HEAD'
```

Only a new greenfield vault that has never completed native gbrain activation
needs Git initialization and an initial commit. That setup belongs to gbrain
activation, not TaskNotes activation.

### 2. Exclude `.git/` from Syncthing

Configure the vault folder's Syncthing ignore patterns so `.git/` is never
synchronized to another device. Verify the exclusion on every device that
shares the vault. Continue using the existing rotating vault backups for user
content; the local transaction history is not a cross-device data format.

### 3. Compatible TaskNotes profile

The adapter currently supports exactly TaskNotes `4.11.1` and fails closed if
the profile differs. Required settings are:

- task identification method: `tag`, with a non-empty task tag;
- an existing lowercase relative tasks folder with no symlink components;
- exactly one completed custom status and valid default status;
- valid custom priorities and default priority;
- unique, non-conflicting mappings for title, status, priority, due, scheduled,
  projects, and completed date;
- a valid archive tag different from the task-identification tag.

Week planning additionally requires a `userFields` entry with key
`planned_week` and type `date` — but only when a caller actually sets
`planned_week`; see "Week planning" below for the exact prerequisite and
failure behavior.

The adapter is config-adaptive for other plugin settings:

- `storeTitleInFilename` and `taskFilenameFormat` do not affect adapter-created
  tasks because gbrain writes files with explicit slugs and the frontmatter
  title always takes precedence over filename-based extraction.
- `moveArchivedTasks` is supported. When true, the adapter reads `archiveFolder`
  and checks both the tasks folder and the archive folder for `task_get`,
  `task_update`, and read-back verification. The plugin moves archived files
  asynchronously; the adapter handles whichever location the file is in.

The adapter reads `.obsidian/plugins/tasknotes/manifest.json` and `data.json`
dynamically. Unsupported or changed settings produce a tool error before Git or
gbrain mutation side effects.

### 4. Gbrain source routing

Run operator activation first, then confirm exactly one gbrain source resolves
to `$GBRAIN_BRAIN_REPO`:

```bash
docker compose exec hermes su -s /bin/sh hermes -c 'gbrain sources list --json'
```

The adapter passes that source ID explicitly to every gbrain read, capture,
delete/tag mutation, and sync that it invokes.

## Runtime behavior

Gbrain uses Git `HEAD` and its stored `last_commit` as the native incremental
sync boundary. Native gbrain write-through updates the database and Markdown
file but does not create a Git commit. The TaskNotes adapter fills that local
commit gap: before a mutation, it takes `/opt/data/.locks/tasknotes.lock`,
commits pending vault edits, and runs incremental source-scoped gbrain sync. It
then performs one source-routed write via
`gbrain capture --stdin --slug <slug> --source <source-id> --json`, verifies
both gbrain and the on-disk task, and commits only the target task file.

TaskNotes invokes the private native launcher
(`/opt/josemar/libexec/gbrain-native`) under the transaction-level lock. The
launcher enforces `GBRAIN_SKIP_STARTUP_HOOKS=1` on every invocation, so
gbrain's startup upgrade notice is never emitted and cannot corrupt the
stdin-routed write even if a caller merges stderr into stdin (`2>&1`). This is
defense in depth, not generic stderr filtering: the source-routed write path
above remains the only sanctioned task-file write, and task mutations go
through the `task_*` MCP tools only.

The periodic `gbrain-refresh` cron uses the same lock nonblockingly. If a task
operation holds the lock, refresh logs a skip and exits successfully rather
than queueing behind it. `GBRAIN_REFRESH_TIMEOUT` bounds the refresh child
process (default `240` seconds). The daily `gbrain-embedding-refresh` cron
takes the same lock nonblockingly via `refresh-embeddings`. This lock is the
global serialization point for cooperative gbrain access (issue #110):
TaskNotes transactions, both refresh crons, and backfills all cooperate on it.

Hermes registers the server from `config/hermes-config.yaml` with parallel tool
calls disabled. One author at a time per task file remains an operating rule;
do not mutate the same task concurrently from Obsidian and chat.

### Native gbrain durability hardening

Pinned gbrain also offers an optional `gbrain sources harden` topology for
GitHub-backed sources, but it is not part of Josemar's supported deployment.
Josemar's vault repository has no remote consumer, refresh deliberately uses
`--no-pull`, and the adapter creates local commits with hooks disabled. It never
pulls or pushes. Any future remote-backed vault would require a separate design
and validation effort rather than enabling native hardening implicitly.

## Task naming convention

The adapter and the TaskNotes plugin use different filename generation
strategies, but both produce gbrain-safe filenames that sort chronologically:

- **Adapter-created tasks** (`task_create` without an explicit slug):
  `YYYY-MM-DD-HHmmss-slugified-title` (e.g.
  `2026-07-18-143000-buy-groceries.md`). The timestamp prefix ensures
  chronological ordering. The slugified title (lowercased, hyphens, no spaces
  or special characters) provides human readability and is safe for the
  source-routed `gbrain capture --stdin --slug` write path, which does not
  slugify the slug argument.

- **Plugin-created tasks** (with `taskFilenameFormat: "timestamp"`):
  `YYYY-MM-DD-HHmmss` (e.g. `2026-07-18-143000.md`). The plugin's `timestamp`
  format is recommended so both adapter-created and plugin-created tasks sort
  chronologically in the Obsidian file explorer.

When `task_create` is called with an explicit slug, the slug must already be
gbrain-safe (lowercase, hyphens, no spaces). The adapter does not slugify an
explicitly provided slug.

The title is always stored in frontmatter and is visible in the Obsidian UI
regardless of the filename.

### Known gbrain slug limitation

Gbrain's slug argument (used by both `put` and `capture --slug`) is not
slugified — it is only lowercased and unsafe characters are rejected.
Gbrain's `sync` command, however, does slugify file paths (lowercases, replaces
spaces with hyphens, strips special characters). This means a file created by
the plugin with spaces in its filename would be indexed under a different slug
than the same content written via the adapter's source-routed
`gbrain capture --stdin --slug` path. See
<https://github.com/garrytan/gbrain/issues/3034>.

The adapter avoids this mismatch by always generating gbrain-safe slugs
(lowercase, hyphens, no spaces) for adapter-created tasks. The recommended
plugin ``taskFilenameFormat: "timestamp"`` also produces gbrain-safe filenames.

## Task relationships (subtasks)

There is no dedicated ``subtasks`` field in the TaskNotes 4.11.1 schema, and
the adapter does not expose a separate subtask management API. However, the
**``projects`` field serves as the idiomatic parent-child linking mechanism.**

Set ``projects: ["[[parent-slug]]"]`` on a child task to establish a backlink
to the parent. Obsidian's graph view resolves these wikilinks, and gbrain's
backlink extraction surfaces the relationship. The adapter's ``task_create``
and ``task_update`` tools both accept ``projects`` as a list of wikilink
strings.

**Conventions for subtask workflows:**

- **Parent task as a project container.** Create one parent task (e.g.
  ``2026-07-20-120000-plan-q3-launch``) that represents the project or
  deliverable. It holds the scope, description, and deadline in its body.
- **Child tasks link with ``[[slug]]``.** On each child task, set
  ``projects: ["[[2026-07-20-120000-plan-q3-launch]]"]``. This creates a
  backlink from child to parent.
- **No cascading semantics.** Completing or archiving a parent does not
  automatically affect children; each task remains independently mutable.
- **Multiple parents are valid.** A task can reference several parent
  projects via ``projects: ["[[parent-a]]", "[[parent-b]]"]``.

For discovery, use ``task_list`` with tag or status filters to find all tasks
linked to a parent project, or inspect the ``projects`` field in individual
``task_get`` responses.

## Week planning (issue #128)

The MCP models three unambiguous planning states. A task is in exactly one:

| State | Frontmatter |
|---|---|
| Backlog | neither `scheduled` nor `planned_week` |
| Week-planned | `planned_week` only |
| Day-scheduled | native `scheduled` only |

`planned_week` is a first-class argument on `task_create` and `task_update`
(`clear_planned_week` on update), stored under the raw `planned_week`
frontmatter key as the ISO `YYYY-MM-DD` date of the target week's Monday. It
expresses week-only intent ("sometime that week") without fabricating an
exact day.

### API semantics

- **Validation.** `planned_week` must be a valid `YYYY-MM-DD` calendar date
  AND a Monday (the ISO week start). Non-Monday dates are rejected; nothing
  is silently rounded.
- **Mutual exclusivity.** Supplying both `scheduled` and `planned_week` on
  create or update is rejected before any side effect.
- **Create.** At most one target: `scheduled=<date>` (day-scheduled),
  `planned_week=<Monday>` (week-planned), neither (Backlog).
- **Update transitions.**
  - `scheduled=<date>` → sets `scheduled`, removes `planned_week`.
  - `planned_week=<Monday>` → sets `planned_week`, removes `scheduled`.
  - `clear_scheduled=true` with no new target → removes both (Backlog).
  - `clear_planned_week=true` with no new target → removes only
    `planned_week`; on a manually inconsistent task an existing `scheduled`
    remains authoritative.
  - Contradictory or redundant combinations that express both a set and a
    clear for the same planning transition are rejected before any side
    effect. Callers set the desired target state instead of composing both
    halves.
- **Normalization.** The invariant is centralized in the core rewrite layer:
  whenever any non-delete MCP mutation rewrites an existing task carrying
  both keys (only possible via direct/manual Obsidian edits), native
  `scheduled` wins and the stale `planned_week` is dropped. This keeps
  `task_complete`, archive/tag updates, and unrelated `task_update` calls
  from perpetuating an inconsistent pair. Reads never mutate state merely to
  repair it — the manual-edit inconsistency is an accepted trade-off of this
  design.
- **Reserved key.** `planned_week` cannot be written through generic
  `custom_fields` (rejected even with a `null` value), so the transition and
  invariant logic cannot be bypassed.
- **Profile prerequisite.** Setting `planned_week` requires the profile to
  define a `userFields` entry with key `planned_week` and type `date`. A
  missing definition or incompatible type fails explicitly before any Git or
  gbrain side effect. Normalizing already-stored values does not require the
  definition.
- **Read visibility.** Unlike other custom user fields, `planned_week` is
  promoted into structured `task_list` results and `task_get` output so
  automated callers can distinguish Backlog, week-planned, and day-scheduled
  tasks without raw-frontmatter access. No new list filter was added.
- **No upgrade, no workflows.** TaskNotes remains pinned to `4.11.1`. This
  feature introduces no plugin upgrade and no TaskNotes Workflows or other
  Obsidian-side synchronization dependency.

### Effective-week Base views (operator-owned)

`.base` views are private user state. They are NOT repository-owned: never
commit them to this repository and never manipulate them from repo
automation. To display one effective-week grouping where a day-scheduled
task shows its scheduled week, a week-planned task shows its planned week,
and everything else falls into a Backlog bucket, define a formula property
that resolves `scheduled` first, then `planned_week`, then the literal
Backlog label:

```yaml
formulas:
  effectiveWeek: 'if(scheduled, date(scheduled).format("YYYY-[W]WW"), if(planned_week, date(planned_week).format("YYYY-[W]WW"), "Backlog"))'
```

Group the view by `formula.effectiveWeek` (Bases `groupBy.property`). Notes:

- Stored task values remain plain Monday dates; the `YYYY-[W]WW` label is
  presentation-only. The formatter follows the pinned TaskNotes 4.11.1
  shipped template convention (see the official default base templates),
  including its calendar-year boundary caveat: around New Year the formatted
  calendar year and the ISO week year can disagree, so treat boundary labels
  as approximate.
- Formula syntax and grouping behavior must be validated in the app
  (Obsidian Bases) against your own vault before relying on them.
- Formula-derived grouping is read-only presentation: dragging a card in a
  formula-grouped view cannot reschedule the underlying task. Rescheduling
  goes through the MCP tools or explicit Obsidian edits.

References: [TaskNotes documentation](https://tasknotes.dev/) (pinned
4.11.1) and its official
[default base templates](https://github.com/callumalpass/tasknotes/blob/main/docs/views/default-base-templates.md);
[Obsidian Bases functions reference](https://help.obsidian.md/bases/functions).

### Legacy `scheduled_week` migration (operator)

Earlier design discussion used a `scheduled_week` metadata key. It is not
retained as a cache or alias. The repository change ships code and docs
only: private `.base` files and vault task state are operator-owned and must
never be committed to or manipulated by this repository change. The
migration is a normal operator rollout — routine adapted MCP access does not
require pausing the owned jobs:

1. **Define the `planned_week` date field.** Add the `userFields` entry
   (type `date`) via Settings → TaskNotes → Task Properties → Custom User
   Fields, or via a backed-up, JSON-validated edit of `data.json` (see
   `skills-factory/tasknotes/references/custom-fields.md`).
2. **Migrate genuine week-only intent to Monday dates.** For each task whose
   legacy metadata expressed week-only planning, run
   `task_update(planned_week="<Monday>")`. Setting the week plan also clears
   `scheduled`; tasks with a real day schedule keep it untouched.
3. **Remove legacy `scheduled_week` metadata.** Delete the stale key from
   task frontmatter (direct Obsidian edit or operator tooling; the MCP never
   writes that key). Stale metadata can mislead callers until removed, so
   treat this as a required rollout step, not cleanup-on-demand.
4. **Update the private Base.** Switch week grouping to the effective-week
   formula above (`scheduled` precedence, then `planned_week`, then
   Backlog).
5. **Retire the old profile field.** Remove the legacy `scheduled_week`
   entry from the TaskNotes configuration so it stops appearing as an
   available property.

**Rollback.** Code rollback restores the prior MCP schema; calls using
`planned_week` disappear and any stored frontmatter keys become inert
metadata. Vault-side rollback is simply to stop using/remove `planned_week`
values and restore the prior private Base if desired. No schema/database
migration and no TaskNotes upgrade is introduced in either direction.

## Current limitations

The adapter does not yet support:

- **Unarchive**: removing the archive tag without a full unarchive workflow.
- **Rename/move, title edits, bulk operations, raw frontmatter,
  inline-task conversion**: these operations are not exposed via the MCP tools.
  Body edits are supported via `task_update`'s optional `body` field.

For these operations, suggest Obsidian or native gbrain (for non-task pages).

### Task deletion

`task_delete` is the only mutation that removes a file from the vault instead of
writing through the source-routed `gbrain capture --stdin` path:

1. **Gbrain soft-delete confirmation gate**: ``gbrain delete <slug>`` hides the
   page from the gbrain index immediately. This call must succeed before the
   adapter touches the file.
2. **``git rm`` removes the file from disk** and stages the removal.
3. **Git commit** records the deletion.

The idempotency check runs before preflight commits any pending edits, so
manual edits on the target file are not silently committed before a destructive
operation. The deleted file is recoverable via ``git revert``; no data is
permanently lost.

If ``gbrain delete`` succeeds but ``git rm`` fails, the adapter returns
``recovery_required`` — the gbrain index has been modified but the file remains
on disk, and the next sync cycle will re-import it.

## Mutation outcomes

- `applied_and_committed`: task and local Git commit verified.
- `not_applied`: idempotent operation or empty update.
- `applied_uncommitted`: task changed, but the target commit failed. Do not
  retry the task mutation; the next successful preflight can commit it.
- `db_updated_disk_failed`: gbrain reported a write-through failure and the
  immediate full-sync reconciliation completed. Inspect before retrying.
- `recovery_required`: state is uncertain. Do not retry.

## Recovery

When `recovery_required` occurs, later mutations are blocked by
`/opt/data/.locks/tasknotes-recovery.marker`.

1. Pause ALL THREE owned jobs for the window (`gbrain-refresh`,
   `gbrain-embedding-refresh`, and `vault-recovery-export`; see
   `docs/gbrain-operations.md` → "Cron
   Pause/Resume for Maintenance Windows"). Stop task mutations and inspect the
   reported task, vault Git status, free
   space, permissions, and gbrain source status.
2. Reconcile deliberately with the on-disk vault as the user-facing source of
   truth. Run operator gbrain refresh/reindex only after reviewing the files.
3. Confirm gbrain and the task file agree and the vault has no unresolved Git
   operation.
4. Remove the marker only after that verification:

   ```bash
   docker compose exec hermes su -s /bin/sh hermes -c \
     'rm -- /opt/data/.locks/tasknotes-recovery.marker'
   ```

Resume all three owned jobs only after step 4 and a successful verification
read.

Never remove the marker merely to retry a failed call.

## Maintenance

Run periodic local Git garbage collection during a maintenance window, outside
task mutations and gbrain refresh. This maintains the same repository native
gbrain already uses; it is not a separate TaskNotes repository:

```bash
docker compose exec hermes su -s /bin/sh hermes -c \
  'git -C "$GBRAIN_BRAIN_REPO" gc'
```

The adapter disables automatic Git maintenance on its own transaction commands
so latency and lock duration remain bounded.
