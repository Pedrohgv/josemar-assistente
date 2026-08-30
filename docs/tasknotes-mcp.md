# TaskNotes MCP Operations

Josemar ships a bounded stdio MCP adapter for durable TaskNotes tasks. The
authoritative TaskNotes documentation is [tasknotes.dev](https://tasknotes.dev/).

The adapter uses short-lived native gbrain CLI commands. It does not run
`gbrain serve`, open PGLite directly, or write task files directly. Gbrain is
the sole task-file writer. The adapter's only direct vault-file write — and
the only sanctioned exception to gbrain-only task Markdown — is the default-on
Daily Note task-link projection (issue #139): a derived,
lock-held, vault-confined wikilink maintained in Obsidian core Daily Notes
while the adapter owns its transaction lock, always followed by native
incremental gbrain reconciliation. Its bounded reconciliation additionally
keeps two private cursor/pending state files under `/opt/data/.gbrain` —
structural metadata only, never task or note content. It is never a generic note writer. See "Daily Note task links (issue #139)" below. The MCP exposes only:

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
  This includes the Daily Note projection (issue #139): its direct filesystem
  write runs only inside the adapter's own lock and is reconciled with native
  incremental sync — never through the public wrapper, and never with a
  concurrent PGLite open. Task-file mutations go through the `task_*` MCP
  tools only.
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
Backlog bucket:

```yaml
formulas:
  effectiveWeek: 'if((scheduled.isEmpty() == false), (date(scheduled) - (duration("1d") * (number(date(scheduled).format("E")) - 1))).format("YYYY-MM-DD"), if((planned_week.isEmpty() == false), date(planned_week).format("YYYY-MM-DD"), "Backlog"))'
```

Group the view by `formula.effectiveWeek` (Bases `groupBy.property`). The
formula value IS the grouping key: a canonical ISO `YYYY-MM-DD` Monday date,
not a week-number label. It works because:

- `format("E")` yields the ISO weekday number with Monday=`1` through
  Sunday=`7`; subtracting `duration("1d") * (E - 1)` days from any date
  lands on that week's Monday — exactly the value week planning stores.
  Worked example across a calendar-year boundary: `scheduled` 2026-01-02
  (Friday, `E`=5) derives 2026-01-02 − 4 days = **2025-12-29**, and a task
  week-planned as `planned_week` 2025-12-29 formats to **2025-12-29** —
  both land in the same group.
- Pinned Bases provides no native start-of-week/week arithmetic, so the
  duration arithmetic above uses only officially supported primitives
  (`isEmpty`, `date`, `duration`, `number`, `format` with the `E` token).
- Formula syntax and grouping behavior must be validated in the app
  (Obsidian Bases) against your own vault before relying on them; CI cannot
  execute Bases formulas.
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
3. **Clear legacy `scheduled_week` metadata through the bounded MCP path.**
   While the legacy field is still configured as a user field, clear it per
   task with `task_update(custom_fields={"scheduled_week": null})`. This
   must happen BEFORE retiring the configuration entry: once the field
   leaves the profile, the generic `custom_fields` argument rejects unknown
   keys. Stale metadata can mislead callers until cleared, so treat this as
   a required rollout step, not cleanup-on-demand.
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

## Daily Note task links (issue #139)

The adapter can mechanically maintain one task wikilink in the Obsidian core
Daily Note matching a task's exact `scheduled` date. The projection is
derived state: the TaskNotes task remains authoritative, and `scheduled` is
the sole projection source. `due`, `planned_week`, recurrence metadata,
status, archive state, and completion date never create, move, or remove a
link.

### Architectural boundary

Task Markdown remains gbrain-only. The projection is the adapter's only
direct filesystem write: while it owns `/opt/data/.locks/tasknotes.lock` it
may create/edit only the Daily Note file(s) implied by the validated
configured Daily Notes path for the task's exact `scheduled` date(s), then
reconcile the filesystem-originated change through source-scoped native
incremental gbrain sync under the same lock. It never opens PGLite
concurrently and never routes through (or is invoked from) the public `gbrain`
wrapper. A resolved Daily Note target under the configured `tasksFolder` —
or under the active archive folder (when `moveArchivedTasks` is true and
`archiveFolder` is configured) — is rejected before any task side effect,
because task Markdown is gbrain-only and the projection writer must never
mutate TaskNotes-managed files. This is not a generic Markdown writer: no
tool, API, or arbitrary note path is exposed.

### Feature flag (default on)

The feature is controlled by the strict boolean master
`TASKNOTES_DAILY_LINKS_ENABLED`, propagated deploy workflow → `.env` →
Compose → Hermes TaskNotes MCP env → engine. The deploy workflow and
Compose both default it to `true`, and the engine's own strict parse
shares that default: Missing or empty resolves to enabled at every layer.
Only case-insensitive
`true`/`false` are accepted and any other value fails closed at deploy
validation before any deploy mutation and again at MCP initialization
(so even if the deployment chain is bypassed entirely, no disabled
fallback exists). Explicit `false` rolls back or opts
out: no Daily Notes config is read or required, no projection work runs,
reconciliation is fully inert (no cursor/pending I/O; the refresh cycle
proceeds unchanged), and mutation results carry no `daily_link_*` fields.

The slave `TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED` (default `true`) uses
the same strict parse and propagation. It is effective only while the
master is also `true` and gates both reconciliation lanes — the adapter's
pre-mutation reconciliation AND the refresh-cycle lane (see below); with
the master on and the slave `false`, projections still run at mutation
time but both reconciliation lanes are inert.

### Reconciliation (cursor/pending)

Projection maintenance is not limited to mutation-time projections: the
adapter also reconciles the derived links against actual task state, so
drift — e.g. from direct Obsidian task edits — is repaired by the next
locked reconciliation cycle. Reconciliation in either lane is active only
while the master and slave flags are both `true`; when the master is
`false` everything is inert, and with the master on but the slave off
both lanes are inert too.
State lives in two fixed private runtime files under `/opt/data/.gbrain`
(never under the vault, never in the gbrain database): the reconcile
cursor and its pending sibling. They hold structural metadata only —
schema id/version, a reconciled HEAD SHA, the prior Daily Notes
folder/format, and the projection format version — never titles, bodies,
or any task/note content; task Markdown stays gbrain-only. A missing
cursor bootstraps with an ensure-only pass for currently scheduled tasks
(the first-enable backfill); a present-but-invalid document fails closed.

Two locked lanes run the same bounded lifecycle with one validated
`DailyNotesConfig` snapshot through prepare → apply → sync → finalize:

- **MCP pre-mutation reconciliation (slave, default on).** For every
  normal mutation (`task_create`, `task_update`, `task_complete`,
  `task_archive`, `task_add_tag`, `task_remove_tag`) reconciliation runs
  inside the transaction lock BEFORE the Git preflight and any mutation
  side effect: a bounded prepare, then apply with one targeted commit
  staging only the changed Daily Note paths (plus the tracked-and-dirty
  `daily-notes.json` when the plan needs it), then a required native
  source-scoped incremental gbrain sync, and only then the cursor
  finalize. Any failure raises before the preflight/mutation: the
  mutation is blocked and the cursor/pending state stays replayable by
  the next locked attempt; the recovery marker is never touched.
- **Refresh-cycle reconciliation.** `josemar-gbrain refresh` runs the
  same cycle under the shared lock through a fixed-purpose CLI:
  `reconcile` (prepare/apply/targeted commit) → the wrapper's native
  sync → `finalize` strictly after that sync succeeded. Any failure
   fails refresh without advancing the cursor (replayable). This lane is
   gated by both flags — the master AND the slave — and is inert when
   either flag is off.

**Delete exception.** `task_delete` checks target cleanliness BEFORE any
reconciliation: a dirty (uncommitted) delete target rejects with zero
reconciliation I/O, so manual edits are never silently committed before a
destructive operation. Once the target is Git-clean, reconciliation runs,
then the preflight/delete lifecycle proceeds.

### Daily Notes configuration (dynamic, fail-closed)

The adapter reads `<vault>/.obsidian/daily-notes.json` dynamically — there is
no second folder/date/template configuration. A validated config is required
when the feature is enabled and a projection is needed: a missing config (or
missing `.obsidian/`) fails closed rather than inferring defaults. Supported
keys: `folder` (vault-relative; missing/empty → vault root), `format`
(missing/empty → `YYYY-MM-DD`), `template` (optional vault-relative Obsidian
Markdown note reference). Unknown keys are ignored for Obsidian compatibility.
Path values must be relative, free of backslashes/control characters/traversal
segments, and must not pass through symlink components where they exist.

A non-empty `template` value is a note reference, not necessarily a physical
filename: its canonical physical target appends `.md` iff the complete
reference does not already end exactly in `.md` — `templates/daily-note` →
`templates/daily-note.md`, `templates/daily-note.md` unchanged, and
`templates/daily.v2` → `templates/daily.v2.md`. All path and safety checks
apply to that canonical target only; an extensionless physical file is never
probed or read as the template. The canonical target must be a regular
Markdown file when present (no symlink components; template reads are
no-follow and bounded); a missing canonical target may pass config load, but
the required template read fails closed before any task side effect.
Malformed JSON, non-object roots, or unsafe paths fail closed before any
task side effect. The Periodic Notes plugin configuration is
never read as a fallback.

Config freshness: there is no engine or MCP lifetime cache. Each
projection-bearing task transaction reads and strictly validates
`daily-notes.json` exactly once, before any task side effect, and that
immutable snapshot is used through apply/commit/sync. Editing the file
therefore applies to the next projection-bearing task operation — a
single config source with no reload or watch path, consistent with the
no-watcher behavior below.

### Supported date-format subset

`format` supports exactly the numeric tokens `YYYY YY MM M DD D` with literal
separators limited to safe path characters `- . _ /` (so both flat names and
numeric hierarchies such as `YYYY/MM/DD` work). Every alphabetic run must be
exactly one supported token; anything else — locale month/weekday names
(`MMM`, `MMMM`, `ddd`, ...), quarters, escaping/quoting syntax — is rejected
before any task side effect instead of being approximated.

### Template rendering (bounded, no execution)

If the target Daily Note does not exist, the adapter creates it from the
configured template (if any) or a deterministic default body (`# <date>` plus
one `## Tasks` section). Only three core template expressions are supported:
`{{date}}` (the scheduled `YYYY-MM-DD`), `{{title}}` (the Daily Note filename
stem), and `{{date:FORMAT}}` (rendered with the same supported token subset).
Any other `{{...}}` expression is rejected before the projection runs;
Templater or JavaScript execution never happens. After rendering — only
when a missing note is created — a top-level `date` frontmatter value
that is null/empty is set to the scheduled date and a null/empty
top-level `title` becomes the filename stem; non-empty values are
preserved and no provenance fields are invented. The rendered note must
contain exactly one `## Tasks` section, otherwise the projection fails
closed.

### Existing-note structural contract

An existing Daily Note must contain exactly one level-2 `## Tasks`
heading; its section extends to the next level-2 heading or end of file.
All bytes outside that section's transformation are retained verbatim —
in particular, existing frontmatter is never normalized or reserialized
(the null/empty `date`/`title` fill above applies only to notes created
from the template/default body). A missing or duplicated `## Tasks`
section fails closed — the adapter never restructures a human-authored
note. Only exact generated task-link bullet lines inside that section are
edited; nothing outside it (and no prose) is ever touched.

### Canonical link semantics

The canonical generated line is exactly `- [[<task-slug>]]` — a bare
wikilink with no display alias. The task title remains authoritative and
unchanged in task Markdown and is never serialized into the projection,
so the link cannot drift from the title. Matching is by exact wikilink
target slug on bullet-only lines, never by the text after the target, so
normalization, dedup, and removal stay idempotent regardless of any
display text. Exact-slug ownership accepts legacy
`- [[<task-slug>|<alias>]]` lines only as normalization/removal inputs:
adding deduplicates exact-slug bullets and normalizes the first to the
bare canonical form (preserving its indentation and unrelated section
lines), removal deletes every exact-slug bullet line whether bare or
aliased, and newly generated lines are always bare. The section and note
remain even when the section becomes empty; similar-prefix slugs and
prose mentions are never matched or modified.

Display trade-off (TaskNotes 4.11.1): TaskNotes resolves task links
against the linked task file and renders its authoritative frontmatter,
so the title keeps surfacing in TaskNotes' own card rendering; plain
Obsidian source/reading view shows only the bare target (the slug).
Keeping the link bare avoids serializing a derived display copy of the
title that could drift from the authoritative frontmatter on manual
edits — the accepted revision-3 trade-off.

### Transition matrix

| Task transition | Daily Note projection |
|---|---|
| create Backlog task | none |
| create `planned_week` task | none |
| create with `scheduled=<D>` | add link under D |
| update from Backlog/week to `scheduled=<D>` | add link under D |
| reschedule `D1` → `D2` | add under D2 **first**, then remove from D1 |
| update with unchanged `scheduled=<D>` | idempotent; no duplicate link |
| `scheduled` → `planned_week` | remove old date's link |
| `clear_scheduled` → Backlog | remove old date's link |
| non-scheduling update | none (`not_applicable`) |
| `task_complete` / `task_archive` / tag changes | none; link retained while `scheduled` remains |
| `task_delete` of a scheduled task | remove link **after** verified deletion |
| `task_delete` of a Backlog/week task | none |
| recurrence creation | no future links pre-created |

Transitions are computed from the task's actual old and final scheduling
state, never from caller intent alone. Plans are composed by resolved
target path: when a date format maps the old and new dates to the same
Daily Note (e.g. `YYYY-MM`), the transition performs one ensure for that
note — never an ensure followed by a remove of the same link.
Rescheduling adds the new date's link before removing the old one, so a
partial projection failure can duplicate visibility but never lose the
task from both Daily Notes. `task_delete`
reads the current page and prevalidates the removal before the gbrain
soft-delete gate, but only removes the link after task deletion is verified;
a failed task deletion never touches the projection.

### Concurrency, atomicity, Git, and reconciliation

Obsidian and Syncthing do not honor the TaskNotes lock. The projection
writer therefore transforms bytes in memory from a fingerprinted source
(identity metadata plus content hash), re-verifies that fingerprint
immediately before an atomic `os.replace`, retries once on a detected race
(two attempts total), and reports a persistent conflict instead of
overwriting concurrent edits. Missing-note creation re-checks absence and
publishes atomically with no-clobber semantics: a note created by a
competing writer at the publication boundary is never overwritten and
triggers the same bounded recompute-and-retry. Writes go through a
sibling temp file
(exclusive, no-follow) with fsync and cleanup on every failure path; note
and template reads are bounded (1 MB) and no-follow. A successful projection
is recorded as one content-free Git commit
(`tasknotes-mcp: daily note projection`) staging only the affected Daily
Note paths — never the whole vault — and then reconciled with native
incremental gbrain sync while the lock is still held. A projection commit is
separate from the task commit; for deletions the reported `commit_id`
remains the task deletion commit.

### Projection outcomes

When enabled, `task_create`, `task_update`, and `task_delete` additionally
report optional projection fields (`daily_link_state`, `daily_link_detail`,
`daily_link_dates`); disabled mode omits them and existing consumers are
unaffected. The task `state` stays the authoritative mutation outcome and is
never degraded by a projection problem:

- `applied_and_committed`: projection written, committed, and synced.
- `not_applicable`: enabled, but this operation has no projection
  (e.g. non-scheduling update, Backlog/week task).
- `not_applied`: nothing to change (already canonical/absent).
- `conflict`: the Daily Note changed concurrently twice; nothing was
  overwritten. Retry the mutation later.
- `write_failed` / `commit_failed`: filesystem/Git failure. The task outcome
  remains authoritative; `commit_failed` may leave uncommitted Daily Note
  edits for the next preflight.
- `committed_sync_failed`: vault/Git projection succeeded but incremental
  sync failed; the links are already committed and a later locked refresh
  reconciles the gbrain index.

Projection-only failures never create the TaskNotes global recovery marker
and never roll back the authoritative task mutation; partial multi-date
projections report the degraded state truthfully (e.g. a reschedule whose
new link was committed but whose old-link removal failed).

### Disabling, manual edits, and v1 limitations

Setting the master flag to `false` stops future projection maintenance and
disables reconciliation entirely; links already written are ordinary
Markdown and are never bulk-removed. There is no real-time watcher:
changes to task files made outside the adapter — including direct Obsidian
edits and manual reschedules — are repaired by the next locked
reconciliation cycle (pre-mutation or refresh), not projected instantly.
First enable bootstraps with an ensure-only pass for currently scheduled
tasks. No Periodic Notes compatibility is
provided — only the core Daily Notes plugin configuration is read.

## Current limitations

The adapter does not yet support:

- **Unarchive**: removing the archive tag without a full unarchive workflow.
- **Rename/move, title edits, bulk operations, raw frontmatter,
  inline-task conversion**: these operations are not exposed via the MCP tools.
  Body edits are supported via `task_update`'s optional `body` field.
- **Daily Note projection watcher (issue #139)**: maintenance runs only
  inside locked MCP mutations and locked reconciliation cycles — there is
  no real-time watcher, so Daily Notes can drift from task state between
  cycles; the next mutation or `josemar-gbrain refresh` reconciliation
  repairs it. Disabling the master flag stops future maintenance (and
  reconciliation) without removing existing links.

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

With the Daily Note projection enabled (issue #139), `task_create`,
`task_update`, and `task_delete` also carry the optional `daily_link_state`,
`daily_link_detail`, and `daily_link_dates` fields described in "Projection
outcomes" above. They never change the authoritative task `state`.

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
