# Daily Note task-link projection (issue #139)

Deep-dive reference for the default-on projection that maintains one task
wikilink in the Obsidian core Daily Note matching a task's exact
`scheduled` date. Start with `SKILL.md` (agent-facing summary) and
`docs/tasknotes-mcp.md` (runbook + operator prerequisites); this file holds
the full bounded contract.

## Authority and boundary

- Task files remain gbrain-only. Task Markdown is written only through the
  adapter's short-lived, source-routed native gbrain path under the shared
  `/opt/data/.locks/tasknotes.lock` lock.
- The projection is the adapter's ONLY direct vault-file write: while it
  owns that same lock it may create/edit only the vault-confined Daily Note
  file for a task's exact `scheduled` date, then it reconciles the
  filesystem-originated change with native incremental gbrain sync under the
  same lock. Its bounded reconciliation additionally keeps private
  cursor/pending state files under `/opt/data/.gbrain` (structural metadata
  only, never vault or task content). No concurrent PGLite open, no public
  `gbrain` wrapper nesting, no generic note-write tool/API.
- Task Markdown is gbrain-only, so a resolved Daily Note target under the
  configured `tasksFolder` — or under the active archive folder (when
  `moveArchivedTasks` is true and `archiveFolder` is configured) — is
  rejected before any task side effect; the projection writer must never
  mutate TaskNotes-managed files.
- The projection is derived state. The task is authoritative; `scheduled` is
  the sole projection driver. `due`, `planned_week`, recurrence metadata,
  status, archive state, and completion date never move links.

## Feature flags

- `TASKNOTES_DAILY_LINKS_ENABLED` (master) — strict boolean, default
  `true`. The deploy workflow and Compose both default a missing/empty
  repository variable/`.env` entry to enabled; only case-insensitive
  `true`/`false` are valid; any other non-empty value fails closed at
  deploy validation (before any deploy mutation) and again at MCP
  initialization. `false` disables the projection and makes
  reconciliation fully inert (no cursor/pending I/O; refresh proceeds
  unchanged).
- `TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED` (slave) — strict boolean,
  default `true`, same parse rules. Effective only while the master is
  also `true`; it gates both reconciliation lanes — the adapter's
  pre-mutation reconciliation AND the refresh-cycle lane. With the slave
  `false` both lanes are inert.
- Propagation: deploy workflow → generated `.env` → Compose → Hermes
  TaskNotes MCP env → engine.
- Master-disabled mode is byte-for-byte the previous behavior: no Daily
  Notes config reads, no prerequisites, no projection work, no
  reconciliation, and mutation results contain no `daily_link_*` fields
  at all.

## Reconciliation (cursor/pending, issue #139 W2–W4)

The adapter reconciles the derived links against actual task state so
drift — e.g. from direct Obsidian task edits — is repaired by the next
locked reconciliation cycle. Two lanes run the same bounded lifecycle
with one validated `DailyNotesConfig` snapshot through prepare → apply →
sync → finalize:

- **MCP pre-mutation reconciliation** (master AND slave `true`): runs
  inside the transaction lock BEFORE the Git preflight and any mutation
  side effect for every normal mutation (create/update/complete/archive/
  tag changes): a bounded prepare, then apply with one targeted commit
  staging only the changed Daily Note paths (plus the tracked-and-dirty
  `daily-notes.json` when the plan needs it), then a required native
  source-scoped incremental gbrain sync, and only then the cursor
  finalize. Any failure raises before the preflight/mutation: the
  mutation is blocked and the cursor/pending state stays replayable by
  the next locked attempt; the recovery marker is never touched.
- **Refresh-cycle reconciliation** (`josemar-gbrain refresh`): the same
  cycle under the shared lock through a fixed-purpose CLI — `reconcile`
  (prepare/apply/targeted commit) → the wrapper's native sync →
   `finalize` strictly after that sync succeeded. Any failure fails
   refresh without advancing the cursor. This lane is gated by both flags
   (master AND slave) and is inert when either flag is off.

State lives in two fixed private runtime files under `/opt/data/.gbrain`
(never under the vault, never in the gbrain DB): the reconcile cursor and
its pending sibling — structural metadata only (schema id/version, a
reconciled HEAD SHA, the prior Daily Notes folder/format, the projection
format version), never titles, bodies, or any content. A missing cursor
bootstraps with an ensure-only pass for currently scheduled tasks (the
first-enable backfill); a present-but-invalid document fails closed.
Candidate enumeration and reads are bounded; enumeration overflow fails
closed, and established-cursor reconciliation stays fail-closed above 16
composed transitions.
These files are private runtime state, not vault content: task Markdown
stays gbrain-only and the projection remains the adapter's only direct
vault-file write.

**First-activation batching (issue #144).** The bootstrap pass alone may
carry more than 16 total ensure transitions. Apply then splits the plan
deterministically into batches of at most 16 distinct resolved Daily Note
targets — all transitions for one target stay together, and a coarse date
format (e.g. `YYYY-MM`) counts as one target — with one targeted commit per
batch and no config rider (bootstrap never re-homes routing). It remains one
lock-held lifecycle (prepare once, native sync once, finalize once): the
pending sibling is written once, only after all batches succeeded, and the
cursor advances only via the post-sync finalize. Full-candidate and HEAD
rechecks before the first batch, between batches, and immediately before
the pending make any external task add/remove/schedule change or HEAD
movement fail closed; partial batch commits are retained (no rollback, and
the recovery marker is never touched), and an unchanged retry is an
ordinary bootstrap replay that converges without duplicate links.

**Delete exception:** `task_delete` checks target cleanliness BEFORE any
reconciliation — a dirty (uncommitted) delete target rejects with zero
reconciliation I/O (manual edits are never silently committed before a
destructive operation); once Git-clean, reconciliation runs before the
preflight/delete lifecycle.

## Daily Notes configuration (read dynamically, fail closed)

Source: `<vault>/.obsidian/daily-notes.json` (core Daily Notes plugin only;
Periodic Notes config is never read as a fallback). There is deliberately no
second folder/date/template setting anywhere in the adapter.

| Key | Meaning | Missing/empty/null |
|---|---|---|
| `folder` | vault-relative Daily Notes folder | vault root (`""`) |
| `format` | note filename date format | `YYYY-MM-DD` |
| `template` | vault-relative Obsidian Markdown note reference (canonical physical target appends `.md` unless the complete reference already ends in `.md`) | no template |

- A present config must be a JSON object; unknown keys are ignored (Obsidian
  compatibility). A missing config (or missing `.obsidian/`) when the feature
  is enabled and a projection is needed fails closed — defaults are never
  inferred.
- Freshness: there is no engine or MCP lifetime cache. Each
  projection-bearing task transaction reads and strictly validates the
  config exactly once, before any task side effect; that immutable
  snapshot is used through apply/commit/sync. Editing
  `daily-notes.json` therefore applies to the next projection-bearing
  task operation (single config source, no reload or watch path).
- Path values must be relative, without backslashes, control characters, or
  `.`/`..` traversal segments, and existing components may not be symlinks.
- A non-empty `template` is a note reference, not necessarily a physical
  filename: its canonical physical target appends `.md` iff the complete
  reference does not already end exactly in `.md` — `templates/daily-note` →
  `templates/daily-note.md`, `templates/daily-note.md` unchanged,
  `templates/daily.v2` → `templates/daily.v2.md`. All path and safety checks
  apply to that canonical target only; an extensionless physical file is
  never probed or read as the template. The canonical target must be a
  regular Markdown file when present (no symlink components; template reads
  are no-follow and bounded); a missing canonical target may pass config
  load, but the required template read fails closed before any task side
  effect. Malformed/unsafe values fail closed before any task side effect.

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
- Template present → bounded no-follow read of the canonical `.md` target
  (see configuration above), then exactly three expressions
  are substituted: `{{date}}` (scheduled `YYYY-MM-DD`), `{{title}}` (Daily
  Note filename stem), `{{date:FORMAT}}` (same supported token subset).
  Any other `{{...}}` expression is rejected; Templater/JavaScript is never
  executed.
- After rendering — only when a missing note is created — a null/empty
  top-level `date` frontmatter key becomes the scheduled date and a
  null/empty top-level `title` becomes the filename stem; non-empty
  values are preserved verbatim and no provenance fields are invented.
- The rendered note must contain exactly one `## Tasks` level-2 section or
  creation fails closed (ambiguous templates are never auto-restructured).

## Existing-note structural contract

- Exactly one `## Tasks` (exact text, level-2) heading is required; the
  section ends at the next level-2 heading or EOF. Missing or duplicated
  `## Tasks` fails closed.
- All bytes outside the section's transformation are retained verbatim;
  existing frontmatter is never normalized or reserialized (the
  null/empty `date`/`title` fill above applies only when a missing note
  is created from the template/default body).
- Only generated task-link bullet lines inside that section are edited;
  bytes outside the section are preserved as the bounded edit permits, and
  links/prose elsewhere in the note are never searched or rewritten.

## Canonical link semantics

- Canonical line: exactly `- [[<task-slug>]]` — a bare wikilink with no
  display alias. The task title stays authoritative and unchanged in task
  Markdown and is never serialized into the projection, so the link
  cannot drift from the title.
- Ownership matching is by exact wikilink target slug on bullet-only
  lines, never by display/title text and never prefix-matched; the text
  after the target is never matched or compared, so normalization, dedup,
  and removal stay idempotent regardless of any display text.
- Legacy alias lines: `- [[<task-slug>|<alias>]]` bullets are accepted
  only as normalization/removal inputs. Add recognizes an existing
  exact-slug bullet (bare or aliased), rewrites the first occurrence in
  place to the bare canonical form (indentation preserved), removes
  duplicate exact-slug bullets, or appends one bare canonical line at the
  end of the section when absent. Remove deletes every exact-slug bullet
  line (plus one adjacent newline) whether bare or aliased. Newly
  generated lines are always bare.
- Display trade-off (TaskNotes 4.11.1): TaskNotes resolves task links
  against the linked task file and renders its authoritative frontmatter,
  so the title keeps surfacing in TaskNotes' own card rendering; plain
  Obsidian source/reading view shows only the bare target (the slug).
  Keeping the link bare avoids serializing a derived display copy of the
  title that could drift from the authoritative frontmatter on manual
  edits — the accepted revision-3 trade-off.
- Prose mentions and similar-but-different slugs are untouched; the
  section and note remain even if the section becomes empty.

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

- Plans are composed by resolved target path, not merely by date: dates
  that map to the same note (e.g. a `YYYY-MM` format) collapse to one
  step — a reschedule whose old and new dates share one note performs a
  single ensure for it, never an ensure followed by a remove of the same
  link.
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
  edit. Missing-note creation re-checks absence immediately before
  publish and publishes atomically with no-clobber semantics: a competing
  creator is never overwritten; its bytes trigger the same bounded
  recompute-and-retry.
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

## Disabling and limitations

- Setting the master flag to `false` stops future projection maintenance
  and disables reconciliation entirely; existing links are ordinary
  Markdown and are never bulk-removed. Rollback is a normal
  redeploy/restart plus flag flip; no schema/frontmatter migration
  exists.
- no real-time watcher: changes made outside the adapter — including
  direct Obsidian task edits and manual reschedules — are reconciled by
  the next locked cycle (pre-mutation or refresh), not projected
  instantly. First enable bootstraps with an ensure-only pass for
  currently scheduled tasks.
- No Periodic Notes compatibility; no locale-aware date names; no Templater
  or script execution; no generic note editing.
