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

### 1. Server-local vault Git history

`$GBRAIN_BRAIN_REPO` must be an initialized Git repository with a valid `HEAD`.
This history is a local transaction journal for the adapter; it is not the
private agent-state repository and does not need a remote.

For a new local history, review the vault contents first, then initialize and
create the first commit as the Hermes runtime user. Do not run this blindly on
an existing repository.

```bash
docker compose exec hermes su -s /bin/sh hermes -c '
  git -C "$GBRAIN_BRAIN_REPO" init &&
  git -C "$GBRAIN_BRAIN_REPO" add -A &&
  git -C "$GBRAIN_BRAIN_REPO" \
    -c user.name=tasknotes-mcp \
    -c user.email=tasknotes-mcp@local \
    commit -m "Initialize local vault history"
'
```

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
- `storeTitleInFilename: false`;
- `taskFilenameFormat: zettel`;
- `moveArchivedTasks: false`;
- exactly one completed custom status and valid default status;
- valid custom priorities and default priority;
- unique, non-conflicting mappings for title, status, priority, due, scheduled,
  projects, and completed date;
- a valid archive tag different from the task-identification tag.

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

Before a mutation, the adapter takes `/opt/data/.locks/tasknotes.lock`, commits
pending vault edits, and runs incremental source-scoped gbrain sync. It then
performs one whole-page gbrain put, verifies both gbrain and the on-disk task,
and commits only the target task file.

The periodic `gbrain-refresh` cron uses the same lock nonblockingly. If a task
operation holds the lock, refresh logs a skip and exits successfully rather
than queueing behind it. `GBRAIN_REFRESH_TIMEOUT` bounds the refresh child
process (default `240` seconds).

Hermes registers the server from `config/hermes-config.yaml` with parallel tool
calls disabled. One author at a time per task file remains an operating rule;
do not mutate the same task concurrently from Obsidian and chat.

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
task mutations and gbrain refresh:

```bash
docker compose exec hermes su -s /bin/sh hermes -c \
  'git -C "$GBRAIN_BRAIN_REPO" gc'
```

The adapter disables automatic Git maintenance on its own transaction commands
so latency and lock duration remain bounded.
