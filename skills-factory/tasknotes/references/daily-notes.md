# Daily Note task-link projection (issue #139)

Deep-dive reference for the opt-in projection that maintains one task
wikilink in the Obsidian core Daily Note matching a task's exact
`scheduled` date. Start with `SKILL.md` (agent-facing summary) and
`docs/tasknotes-mcp.md` (runbook + operator prerequisites); this file holds
the full bounded contract.

## Authority and boundary

- Task files remain gbrain-only. Task Markdown is written only through the
  adapter's short-lived, source-routed native gbrain path under the shared
  `/opt/data/.locks/tasknotes.lock` lock.
- The projection is the adapter's ONLY direct filesystem write: while it
  owns that same lock it may create/edit only the vault-confined Daily Note
  file for a task's exact `scheduled` date, then it reconciles the
  filesystem-originated change with native incremental gbrain sync under the
  same lock. No concurrent PGLite open, no public `gbrain` wrapper nesting,
  no generic note-write tool/API.
- The projection is derived state. The task is authoritative; `scheduled` is
  the sole projection driver. `due`, `planned_week`, recurrence metadata,
  status, archive state, and completion date never move links.

## Feature flag

- `TASKNOTES_DAILY_LINKS_ENABLED` — strict boolean, default `false`.
  Missing/empty = disabled; only case-insensitive `true`/`false` are valid;
  any other non-empty value fails closed at MCP initialization.
- Propagation: deploy workflow → generated `.env` → Compose → Hermes
  TaskNotes MCP env → engine.
- Disabled mode is byte-for-byte the previous behavior: no Daily Notes config
  reads, no prerequisites, no projection work, and mutation results contain
  no `daily_link_*` fields at all.

## Daily Notes configuration (read dynamically, fail closed)

Source: `<vault>/.obsidian/daily-notes.json` (core Daily Notes plugin only;
Periodic Notes config is never read as a fallback). There is deliberately no
second folder/date/template setting anywhere in the adapter.

| Key | Meaning | Missing/empty/null |
|---|---|---|
| `folder` | vault-relative Daily Notes folder | vault root (`""`) |
| `format` | note filename date format | `YYYY-MM-DD` |
| `template` | vault-relative Markdown template path | no template |

- A present config must be a JSON object; unknown keys are ignored (Obsidian
  compatibility). A missing config (or missing `.obsidian/`) when the feature
  is enabled and a projection is needed fails closed — defaults are never
  inferred.
- Freshness: the validated config is loaded lazily on the first enabled
  projection and cached for the lifetime of the TaskNotes MCP process;
  editing `daily-notes.json` applies only after an MCP restart (single
  config source, no reload or watch path).
- Path values must be relative, without backslashes, control characters, or
  `.`/`..` traversal segments, and existing components may not be symlinks.
  The template must be a `.md` file. Malformed/unsafe values fail closed
  before any task side effect.

## Date-format subset

Only deterministic numeric layouts are supported (no locale emulation):

- Tokens: `YYYY` (4-digit year), `YY` (2-digit year), `MM`/`M` (zero-padded /
  plain month), `DD`/`D` (zero-padded / plain day).
- Literals between tokens: only safe path separators `-`, `.`, `_`, `/`
  (supports flat names and numeric hierarchies like `YYYY/MM/DD`).
- Every alphabetic run must be exactly one supported token; the format must
  contain at least one token and stay within a small length bound.
- Unsupported syntax (`MMM`, `MMMM`, weekday/quarter tokens, Moment escaping
  or quoting) is rejected before any task side effect rather than
  approximated. The target is always exactly `<folder>/<formatted>.md`.

## Template rendering (bounded, never executes code)

Used only when a Daily Note must be created:

- Template absent → deterministic default body: `# <date>` plus one
  `## Tasks` section.
- Template present → bounded no-follow read, then exactly three expressions
  are substituted: `{{date}}` (scheduled `YYYY-MM-DD`), `{{title}}` (Daily
  Note filename stem), `{{date:FORMAT}}` (same supported token subset).
  Any other `{{...}}` expression is rejected; Templater/JavaScript is never
  executed.
- After rendering: a null/empty top-level `date` frontmatter key becomes the
  scheduled date and a null/empty top-level `title` becomes the filename
  stem; non-empty values are preserved verbatim and no provenance fields are
  invented.
- The rendered note must contain exactly one `## Tasks` level-2 section or
  creation fails closed (ambiguous templates are never auto-restructured).

## Existing-note structural contract

- Exactly one `## Tasks` (exact text, level-2) heading is required; the
  section ends at the next level-2 heading or EOF. Missing or duplicated
  `## Tasks` fails closed.
- Only generated task-link bullet lines inside that section are edited;
  bytes outside the section are preserved as the bounded edit permits, and
  links/prose elsewhere in the note are never searched or rewritten.

## Canonical link semantics

- Canonical line: `- [[<task-slug>|<task-title>]]`.
- Ownership matching is by exact wikilink target slug on bullet-only lines
  (`- [[slug|alias]]`), never by alias/title text and never prefix-matched.
- Add: normalizes one existing exact-slug bullet to the canonical form
  (indentation preserved), removes duplicate exact-slug bullets, or appends
  one canonical line at the end of the section when absent.
- Remove: deletes every exact-slug bullet line (plus one adjacent newline);
  the section and note remain even if the section becomes empty. Prose
  mentions and similar-but-different slugs are untouched.

## Transition matrix

Transitions derive from the task's actual old and final `scheduled` state,
never caller intent alone.

| Task transition | Projection |
|---|---|
| create Backlog | none |
| create `planned_week` | none |
| create `scheduled=<D>` | add under D |
| Backlog/week → `scheduled=<D>` | add under D |
| reschedule `D1` → `D2` | add D2 **first**, then remove D1 |
| `D1` → `D1` (idempotent) | no duplicate; write skipped if already canonical |
| `scheduled` → `planned_week` | remove old date |
| `scheduled` → Backlog (`clear_scheduled`) | remove old date |
| non-scheduling update | none (`not_applicable`) |
| `task_complete` | none; link retained |
| `task_archive` | none; link retained |
| `task_add_tag` / `task_remove_tag` | none |
| delete scheduled task | remove after **verified** deletion |
| delete Backlog/week task | none |
| recurrence metadata | no future links pre-created |

Ordering guarantees:

- Reschedule: the new date's link is ensured before the old date's link is
  removed, so partial failure yields temporary duplication, never a task
  missing from both notes. Both changed notes are committed together when
  both writes succeed; a partial success commits/syncs what succeeded and
  reports the degraded state.
- Delete: the current page is read and the removal is prevalidated before
  the gbrain soft-delete gate, but the link is only removed after task
  deletion is verified. A failed task deletion never touches the projection.

## Concurrency, atomicity, Git, reconciliation

- Obsidian/Syncthing do not cooperate with the lock. Each existing target is
  transformed in memory against a fingerprint (device/inode/size/mtime-ns
  plus SHA-256 of the bytes read); the fingerprint is re-verified immediately
  before an atomic `os.replace`.
- A detected race triggers one recompute-and-retry (two attempts total); a
  persistent race returns `conflict` and never overwrites the concurrent
  edit. Missing-note creation re-checks absence immediately before publish.
- Writes use a sibling temp file created exclusive and no-follow, fsynced,
  mode-preserved, then atomically replaced; the parent directory fsync is
  best-effort and temp files are cleaned on every failure path. Existing
  file mode is preserved; created notes/folders use `0644`/`0755`.
- Reads and writes are bounded (1 MB per note/template/config) and fully
  no-follow along every path component; created parents are re-opened
  no-follow so a symlink swap fails closed.
- Success is recorded by one content-free Git commit
  (`tasknotes-mcp: daily note projection`) staging only the affected Daily
  Note paths (never `git add -A`, at most 16 targets per commit), separate
  from the task commit, followed by mandatory source-scoped native
  incremental gbrain sync while the lock is still held.
- For `task_delete` the reported `commit_id` remains the task deletion
  commit; the projection commit is additional.

## Projection outcome states

Optional fields on `task_create` / `task_update` / `task_delete` results:
`daily_link_state`, `daily_link_detail` (generic, content-free),
`daily_link_dates` (affected `YYYY-MM-DD` dates). Omitted entirely when the
feature is disabled. The task `state` is never degraded by a projection
problem.

| `daily_link_state` | Meaning |
|---|---|
| `applied_and_committed` | written, committed, and synced |
| `not_applicable` | enabled but no projection for this operation |
| `not_applied` | nothing to change (already canonical/absent) |
| `conflict` | concurrent change persisted; nothing overwritten |
| `write_failed` | filesystem write failed (task outcome unaffected) |
| `commit_failed` | write ok, Git commit failed (uncommitted edits await next preflight) |
| `committed_sync_failed` | committed; incremental sync failed (later locked refresh reconciles) |

Projection-only failures never create the global TaskNotes recovery marker
(`recovery_required` is reserved for task/gbrain state uncertainty) and
never roll back the authoritative task mutation.

## Disabling and v1 limitations

- Setting the flag to `false` stops future projection maintenance only;
  existing links are ordinary Markdown and are never bulk-removed. Rollback
  is a normal redeploy/restart plus flag flip; no schema/frontmatter
  migration exists.
- No watcher, cron, or bulk backfill in v1: direct Obsidian task edits are
  not re-projected until the next MCP scheduling transition touches that
  task, and already-scheduled tasks are not backfilled on first enable.
- No Periodic Notes compatibility; no locale-aware date names; no Templater
  or script execution; no generic note editing.
