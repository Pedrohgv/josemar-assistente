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

There is no unarchive, delete, search, rename/move, recurrence, bulk, raw
Markdown/frontmatter, body edit, or arbitrary tag replacement API.

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

The adapter passes that source ID explicitly to every get, put, and sync.

## Runtime behavior

Gbrain uses Git `HEAD` and its stored `last_commit` as the native incremental
sync boundary. Native gbrain write-through updates the database and Markdown
file but does not create a Git commit. The TaskNotes adapter fills that local
commit gap: before a mutation, it takes `/opt/data/.locks/tasknotes.lock`,
commits pending vault edits, and runs incremental source-scoped gbrain sync. It
then performs one whole-page gbrain put, verifies both gbrain and the on-disk
task, and commits only the target task file.

The periodic `gbrain-refresh` cron uses the same lock nonblockingly. If a task
operation holds the lock, refresh logs a skip and exits successfully rather
than queueing behind it. `GBRAIN_REFRESH_TIMEOUT` bounds the refresh child
process (default `240` seconds).

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
  or special characters) provides human readability and is safe for gbrain's
  `put` command, which does not slugify the slug argument.

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

Gbrain's `put` command does not slugify the explicit slug argument — it only
lowercases and rejects unsafe characters. Gbrain's `sync` command, however,
does slugify file paths (lowercases, replaces spaces with hyphens, strips
special characters). This means a file created by the plugin with spaces in its
filename would be indexed under a different slug than the same content written
via `gbrain put`. See <https://github.com/garrytan/gbrain/issues/3034>.

The adapter avoids this mismatch by always generating gbrain-safe slugs
(lowercase, hyphens, no spaces) for adapter-created tasks. The recommended
plugin `taskFilenameFormat: "timestamp"` also produces gbrain-safe filenames.

## Current limitations

The adapter does not yet support:

- **Custom user fields** (e.g. `pipeline_stage`): the plugin's `customUserFields`
  are read from the profile but cannot be set via the MCP tools. This is planned.
- **Recurrence rules**: TaskNotes supports native recurrence but the adapter
  does not yet pass recurrence data through `task_create`. This is planned.
- **Tag add/remove**: only the task-identification tag and archive tag are
  managed automatically. Custom tags (e.g. `#cliente`) cannot be added or
  removed via the MCP tools. This is planned.
- **Search/filter**: `task_list` returns bounded metadata but does not support
  filtering by tag, status, or custom field. This is planned.

For these operations, suggest Obsidian or native gbrain (for non-task pages).

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

1. Stop task mutations and inspect the reported task, vault Git status, free
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
