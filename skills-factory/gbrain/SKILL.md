---
name: gbrain
description: Native gbrain vault interface. Direct authoring, retrieval, and linking via the public `gbrain` command, which transparently provides the issue #110 safe-adapter behavior. Search mode follows the live runtime; check `gbrain status`, use semantic/hybrid retrieval when embeddings are active, and fall back to keyword search when they are not. No auto indexing.
categories:
  - retrieval
  - search
  - knowledge
  - authoring
---

# GBrain Skill

All agent-facing vault access runs through the public `gbrain` command, which
is safe by default: it transparently provides the issue #110 safe-adapter
behavior — it executes the pinned native `gbrain` CLI as the `hermes` runtime
user under the shared TaskNotes/gbrain lock. `gbrain-chat-run` is a temporary
compatibility alias for this behavior and is not recommended in new
instructions.
The bounded TaskNotes MCP is the only specialized exception: it invokes
short-lived native gbrain commands and must be used for TaskNotes task-file
mutations. Operator-only activation (init/sync/extract/schema setup) is
provided by the `josemar-gbrain reindex` maintenance command and is not
exposed from chat.

## Issue #110: Safe gbrain Access Non-Negotiables

Immediate rules; full runbook: `docs/gbrain-operations.md` → "Issue #110:
Safe gbrain Adapter".

- **No root execution.** Run gbrain and vault Git operations as the Hermes
  runtime user (`hermes`), never as root.
- **Public `gbrain` is mandatory for agent-facing access.** ALL chat, skill,
  and external general vault actions use the public `gbrain` command (safe by
  default). The internal private native gbrain path
  (`/opt/josemar/libexec/gbrain-native`; used by the `josemar-gbrain`
  operator wrapper, both refresh crons, and the TaskNotes
  MCP) must never be presented as an agent command; those paths cooperate on
  the same lock and must avoid nesting.
- **No concurrent PGLite opens.** The gbrain DB is single-writer PGLite; never
  open or mutate it from two processes at once.
- **Cooperative flock.** `/opt/data/.locks/tasknotes.lock` is the global
  serialization point today: TaskNotes transactions, both refresh crons,
  backfills, and every adapted path cooperate on it.
- **Pause all owned jobs for maintenance.** For recovery, reindex/rebuild,
  migrations, vault swaps, and unadapted/third-party diagnostics, the operator
  pauses all three owned jobs: `gbrain-refresh`, `gbrain-embedding-refresh`,
  and `vault-recovery-export`. A lock-held recovery export can repopulate state
  inside the maintenance window. Routine adapted access does not require
  pausing the jobs.
- **Threat model.** The safe wrapper prevents accidental, prompt-driven, and
  cooperative-concurrency PGLite access — NOT a security boundary against a
  compromised same-UID container/shell. The private native path is defense in
  depth, not a complete security boundary; do not overstate protection.
- **TaskNotes: no nested wrapper usage.** TaskNotes is the sole task-file
  writer on short-lived native gbrain commands. It retains its
  transaction-level global lock and internal native invocation; it must never
  route through the public `gbrain` wrapper's lock path internally, nor be
  invoked from it. Task mutations go through the `task_*` MCP tools only.

## Important Notes

- **Agent-facing vault work runs through the public `gbrain` command**
  (safe by default, issue #110): it executes the native CLI under the shared
  lock as the `hermes` user. Never bypass it via the internal private native
  gbrain path from chat. Do not call
  `josemar-gbrain` from chat — it is an
  operator maintenance convenience for reindex/activation only. For TaskNotes
  task files, use the bounded `task_*` MCP tools instead of native capture/put.
- **`josemar-gbrain` wrapper commands are forbidden from chat, with exactly one
  exception.** Chat MUST NOT invoke `josemar-gbrain` for vault work; use
  `gbrain`. The sole chat-allowed wrapper invocation is
  `josemar-gbrain refresh-embeddings`, and it may be run ONLY after an explicit
  user request. Every other wrapper subcommand — `reindex`, `refresh`,
  `enable-embeddings`, `disable-embeddings`, and `embed-backfill` — is
  operator-only and remains forbidden from chat even when the user asks for it
  directly.
- **Search mode follows the live runtime; base activation starts keyword-only
  (issue #65).** Do not infer the current search capability from the base
  activation defaults. Use `gbrain status` when search mode matters. If the
  runtime has embeddings configured and backfilled, `gbrain search` and
  `gbrain query --no-expand` use the hybrid/semantic provider path, so
  concept-based queries are appropriate. If embeddings are disabled or not
  configured, `gbrain search` is keyword-only and `gbrain query --no-expand`
  is unavailable. The operator lifecycle intentionally starts from
  `search.mcp_keyword_only=true` plus the `embedding_disabled` sentinel, then
  `josemar-gbrain enable-embeddings` and `josemar-gbrain embed-backfill`
  activate semantic/hybrid retrieval. Those base activation defaults describe
  initialization and rollback, not necessarily the current deployed state.
  See `docs/gbrain-operations.md` → "Issue #65: Opt-in TEI E5 Semantic/Hybrid
  Retrieval".
- **Periodic refresh, no chat reindex.** Chat does not run activation/reindex.
  Operators run `josemar-gbrain reindex` manually for activation, schema
  changes, or vault swaps. A Hermes cron runs `josemar-gbrain refresh` every 5
  minutes by default to pick up manual Obsidian/Syncthing edits. Refresh uses
  `gbrain sync --no-embed` even after issue #65 activation; embedding stale
  pages is a separate daily job, not folded into refresh. The first backfill
  remains manual; the daily job follows it. Per the primary chat prohibition
  above, the only wrapper command chat may run is
  `josemar-gbrain refresh-embeddings`, and only after an explicit user request;
  `reindex`, `refresh`, `enable-embeddings`, `disable-embeddings`, and
  `embed-backfill` remain operator-only and forbidden from chat.
  See `docs/gbrain-operations.md`.
- **put is whole-page replacement.** The native `put` (via `gbrain`)
  upserts the entire page content.
  Rename, template instantiation, surgical section/frontmatter patching, and
  physical move are NOT offered natively; use Obsidian manually for those.
- **Updating an existing note — always read-then-write.** There is no patch or
  section-append API. To change any existing page (add a task, edit a section,
  update frontmatter), you MUST: (1) read the full current page with
  `gbrain get <slug>`, (2) apply the change in memory preserving all existing
  frontmatter and sections, (3) write the complete result back. This is the
  official gbrain write model, not a workaround — upstream documents the same
  loop as READ → ENRICH → WRITE (`skills/brain-ops/SKILL.md`) and WRITE → SYNC
  (`docs/guides/brain-agent-loop.md`): the agent owns the writing layer and
  rewrites the compiled truth when new information arrives. For inline
  content use `gbrain put <slug> --content "<full page>"` (see the
  `gbrain put` section below); for large or file-based content use
  `gbrain capture --file PATH --slug SLUG` (works for existing pages
  too).
  Never edit vault note content through direct filesystem
  tools (`write_file`, `open()`, `cp`) — that bypasses gbrain's index, leaves
  the DB stale until the next 5-min refresh cron, and breaks link extraction.
  Note: this filesystem-edit ban is a local mitigation for the stale-index bug
  (josemar-assistente#94 P2), not an upstream gbrain rule — upstream expects
  external edits and reconciles them via `sync` (see `docs/guides/live-sync.md`).
  Writes via `gbrain put`/`capture` are write-through — the index is
  current
  immediately, no sync needed. The 5-min refresh cron reconciles external edits
  (Obsidian/Syncthing). If a read does not reflect the on-disk file, that is
  the stale-index bug (josemar-assistente#94 P2); incremental sync will not fix
  it, and `sync --full` is the expensive last resort (full re-import,
  failure-gated) — reserved for post-reorganization reconciliation, not
  routine stale reads.
  If `gbrain get` fails or returns incomplete content, retry once; if
  it still
  fails, stop and report rather than overwriting from partial content.
  Direct filesystem access is reserved ONLY for operations gbrain genuinely
  cannot do (rename with wikilink rewrites, template bootstrap) — never for
  content creation or mutation.
- **Page types are inferred from path.** gbrain assigns each page a `type`
  (used by search, chronicle, link extraction). The type comes from
  frontmatter `type:` if present, otherwise from the directory path. You do
  not need to set `type` in frontmatter if the note lives under the right
  directory — the path does it automatically. Do NOT override `type` in
  frontmatter with a value that fights the path (e.g. `type: note` on a file
  under `meetings/`).

  Path-prefix → type inference table (gbrain-base schema pack):

  | Path prefix | Inferred type |
  |---|---|
  | `people/`, `person/` | `person` |
  | `companies/`, `company/` | `company` |
  | `deals/`, `deal/` | `deal` |
  | `projects/`, `project/` | `project` |
  | `sources/`, `source/` | `source` |
  | `notes/`, `note/` | `note` |
  | `meetings/`, `meeting/` | `meeting` |
  | `conversations/` | `conversation` |
  | `cal/`, `calendar/` | `calendar-event` |
  | `life/diary/` | `diary` |
  | `life/events/` | `event` |
  | `inbox/` | `concept` (default — no match) |
  | (nothing matches) | `concept` (default) |

  Where you write determines what gbrain sees. Choose the directory to match
  the intended type; let path inference do the work instead of setting
  frontmatter `type:` manually.

  **Caveat — type inference is not re-evaluated on idempotent re-upserts.**
  gbrain short-circuits `put` when the new content hash matches the existing
  page's hash (idempotency check in `import-file.ts`). In that case, the
  type is NOT re-inferred from the path. If a page was created with the wrong
  type (e.g. `concept` because it was first ingested before the path-prefix
  table was added, or by a sync that didn't infer correctly), subsequent
  `put` operations with the same content keep the old type forever. To fix:
  either set `type: <correct-type>` in the frontmatter explicitly, or change
  the content slightly so the hash differs and the inference re-runs.
- **Wikilinks and backlinks.** Obsidian `[[wikilinks]]` in page content are
  resolved automatically when a page is written (basename resolution is
  enabled). `gbrain backlinks` returns all incoming links, including
  wikilink-resolved edges. Cross-page link extraction for pre-existing pages
  is an operator action (`josemar-gbrain reindex` or
  `gbrain extract links --source db`); see `docs/gbrain-operations.md`.
- **Runtime state.** gbrain state lives under `/opt/data/.gbrain` (PGLite
  database, config, cache). It is runtime-only and never versioned by
  workspace sync.
- **Life Chronicle (enabled).** gbrain chronicle auto-extracts structured
  events from meeting/conversation/calendar-event pages. When a meeting note
  is written under `meetings/`, the chronicle LLM judge segments it into
  discrete timeline atoms — each decision, commitment, or action item becomes
  its own event with a `kind` (meeting, decision, commitment, call, milestone,
  etc.), `when`, `who`, and `what`. Events are stored in the gbrain DB (pages
  table + timeline_entries index) and are queryable via gbrain commands, but
  are NOT written as `.md` files to the vault filesystem — they are DB-only.
  Each event backlinks to the original meeting note via the `depth` field.

  Chronicle processes these page types: `meeting` (or `meetings/` prefix),
  `conversation` (or `conversations/`), `calendar-event` (or `cal/` /
  `calendar/`). Diary pages (`life/diary/`) are excluded by design (privacy).

  Query the timeline with:
  - `gbrain day <YYYY-MM-DD> [--week] [--narrative]` — events on a date (or ISO week)
  - `gbrain since <YYYY-MM-DD> [--kind <kind>]` — events on/after a date
  - `gbrain last-seen <entity-slug>` — when you last interacted with an entity
  - `gbrain on-this-day` — prior-year events on this month-day
  - `gbrain orient [--days 7] [--entities a,b]` — recent timeline + entity
    ontology in one zero-LLM call (good for session startup context)

  Chronicle auto-emission requires `auto_chronicle=true` (operator config).
  Backfill is operator-only and NOT a chat action: the operator runs
  `gbrain chronicle-backfill` (enqueues extraction jobs) or, on PGLite,
  `gbrain jobs submit chronicle_extract
  --params '{"slug":"<meeting-slug>","sourceId":"default"}' --follow`
  (inline processing) inside a maintenance window with all three owned jobs
  (`gbrain-refresh`, `gbrain-embedding-refresh`, and `vault-recovery-export`)
  paused. Normal agents must NOT invoke these commands. See
  `docs/gbrain-operations.md` for setup details.

  For the full chronicle reference (event schema, kind taxonomy, ontology
  model, the note→atom relationship, query output structure, common
  workflows), load the reference on demand:
  `skill_view("gbrain", file_path="references/chronicle.md")`.

## Actions

All actions are invoked through the public `gbrain` command, which
transparently provides the safe-adapter behavior and passes
the arguments through to the native `gbrain` CLI under the shared lock. Run
`gbrain <command> --help` for per-command help; for the wrapper's
current guarantees and flags, see `docs/gbrain-operations.md` → "Issue #110:
Safe gbrain Adapter" (the wrapper is being hardened separately — do not rely
on implementation details beyond the guarantees stated there). The commands
below are the ones Josemar uses routinely from chat.

### `gbrain status`

Report native gbrain runtime/config status. Safe to call at any time. Use this
before assuming whether search is keyword-only or semantic/hybrid; live runtime
status is authoritative over the base activation defaults.

```bash
gbrain status
```

### `gbrain search`

Native search. Search behavior follows the live runtime. Check `gbrain status`
when capability matters. With embeddings configured and backfilled, `gbrain
search` uses the hybrid/semantic provider path (not exact keyword), so use
concept-based queries when useful. With embeddings disabled or not configured,
search uses the keyword-only path and image/cross-modal queries are rejected.
Base activation starts in that keyword-only state until the operator completes
the issue #65 embeddings lifecycle.

```bash
gbrain search "notes on obsidian sync" --limit 10
```

Common flags:
- `--limit` (integer, optional, result cap)
- `--offset` (integer, optional, pagination)

### `gbrain query --no-expand` (when semantic/hybrid retrieval is active, issue #65)

Availability follows the live runtime, not a permanent base-deploy assumption.
Check `gbrain status` first when uncertain. When embeddings are configured and
backfilled, `gbrain query --no-expand` uses the hybrid/semantic provider path
and is appropriate for concept-based vault exploration. When embeddings are
disabled or not configured, this command is unavailable; use `gbrain search`
with keyword queries instead. The operator activation/backfill and rollback
lifecycle is documented in `docs/gbrain-operations.md` → "Issue #65: Opt-in
TEI E5 Semantic/Hybrid Retrieval".

```bash
gbrain query --no-expand "notes on obsidian sync"
```

### `gbrain get`

Read a page by slug.

```bash
gbrain get inbox/my-note
```

### `gbrain capture`

Write content as a new page. Provide content positionally, with `--stdin`, or
with `--file`.

```bash
gbrain capture "remember to follow up on X" --slug inbox/custom --type note --json
printf '%s' "remember to follow up on X" | gbrain capture --stdin --json
```

Common flags:
- `--slug` (optional, target slug)
- `--type` (optional, lowercase kebab; e.g. `note`, `diary`)
- `--stdin` (read content from stdin)
- `--json` (emit JSON)

### `gbrain put`

Whole-page upsert by slug. Content is passed via `--content` (inline string).
For large content or file-based updates, use
`gbrain capture --file PATH --slug SLUG` instead — `capture --file`
works for
both new and existing pages.

**Never use `gbrain put --stdin`** (including through the public wrapper). The
`--stdin` path is unsafe and has
caused silent page corruption (josemar-assistente#71/#82): piped input can be
truncated or contaminated by stderr diagnostics, replacing real content with
error stubs. The sanctioned mutation path is `gbrain capture --stdin` /
`gbrain capture --file PATH --slug SLUG` (see `gbrain capture`
below).

```bash
gbrain put inbox/my-note --content "# Updated content"
```

This is a whole-page replacement. There is no patch, section-append, or
frontmatter-surgical API. Rename and physical move are not offered natively.

### `gbrain link`

Create a manual link between two pages.

```bash
gbrain link inbox/a people/b --link-type mentions --context "meeting notes" --link-source manual
```

Reconciliation-managed sources (`markdown`, `frontmatter`, `mentions`,
`wikilink-resolved`) are managed by gbrain itself; use `manual` (the default)
or a custom kebab tag for chat-created links.

### `gbrain backlinks`

List incoming links to a page.

```bash
gbrain backlinks people/b
```

### Other useful commands

`gbrain tags`, `gbrain timeline`, `gbrain graph`,
`gbrain delete`, `gbrain history`, `gbrain revert`, and
`gbrain restore` (undoes a soft-delete) are available where useful.
Run `gbrain <command> --help` for the full surface and per-command
help.

## gbrain Skillpack Reference (On-Demand)

The installed gbrain source tree at `/opt/gbrain/skills/` ships ~50 skills
documenting gbrain's conventions, workflows, and features. These are the
canonical reference for how gbrain expects pages to be structured, how
frontmatter should be written, and what features are available.

**When to consult them:** Before writing skills, cron jobs, or prompts that
use gbrain, read the relevant skillpack skill to understand if there is an
established convention, frontmatter format, or workflow to follow. Use
`read_file /opt/gbrain/skills/<skill>/SKILL.md` on demand — do NOT load all
of them into context at once.

**Priority:** These skills are reference material, not authoritative
workflow overrides. If another Josemar skill (client-workflows, calendar-
report, TaskNotes MCP, etc.) conflicts with a gbrain skillpack skill, the
Josemar skill wins. The skillpack documents gbrain's generic model; Josemar
skills document Pedro's specific setup.

**Compatibility filter — what is safe vs. what requires disabled features:**

### Safe to use as reference (works in our setup)

| Skill | Path | What it documents |
|---|---|---|
| `meeting-ingestion` | `skills/meeting-ingestion/SKILL.md` | Meeting page frontmatter format (`date`, `attendees`, section structure), 6-phase ingest workflow |
| `frontmatter-guard` | `skills/frontmatter-guard/SKILL.md` | YAML frontmatter writing conventions, array canonical form, quoting rules, 8 validation classes |
| `capture` | `skills/capture/SKILL.md` | `gbrain capture` as single ingestion entrypoint, slug/idempotency contract, `--type` routing (invoked as `gbrain capture`) |
| `brain-filing-rules` | `skills/_brain-filing-rules.md` | "File by primary subject, not format/source"; `sources/` is only for bulk data; notability gate |
| `conventions/quality` | `skills/conventions/quality.md` | Citation format, source precedence, back-link iron law |
| `conventions/brain-first` | `skills/conventions/brain-first.md` | 4-step brain-first lookup chain before external APIs |
| `brain-ops` | `skills/brain-ops/SKILL.md` | Read→enrich→write loop, brain-first lookup protocol |
| `repo-architecture` | `skills/repo-architecture/SKILL.md` | Where new brain files go, directory conventions |
| `reports` | `skills/reports/SKILL.md` | Save/load timestamped reports |

### Requires runtime features (embeddings / dream cycle / LLM synthesis)

These features depend on the live runtime rather than a permanent
keyword-only assumption. Embedding-backed features such as `query` are
available when embeddings are configured and backfilled; a deployment may
already be in that state. Check `gbrain status` before deciding they are
unavailable. Other entries below require features Josemar does not enable.

| Skill | Requires |
|---|---|
| `query` | Semantic search (embeddings) — use `gbrain query --no-expand` whenever runtime status shows embeddings active; operator lifecycle is issue #65 |
| `briefing` | `gbrain recall --since-last-run` (embeddings) |
| `concept-synthesis` | Dream cycle + LLM synthesis |
| `enrich`, `article-enrichment`, `book-mirror`, `strategic-reading` | LLM synthesis calls |
| `idea-lineage` | Embeddings + graph traversal |
| `perplexity-research`, `data-research`, `academic-verify` | External API keys not configured |
| `gbrain-advisor` | Embeddings for full functionality |

### Conflicts with Josemar's setup (do NOT use)

| Skill | Why it conflicts |
|---|---|
| `daily-task-manager` | Uses `ops/tasks.md` model; Josemar uses TaskNotes MCP + daily notes |
| `daily-task-prep` | Assumes calendar integration Josemar doesn't have |
| `signal-detector` | Always-on ambient capture on every message — too aggressive for MVP |
| `schema-author`, `schema-unify` | Chat-driven schema mutation; Josemar forbids this (issue #69) |
| `soul-audit` | Generates SOUL.md/USER.md; Josemar has its own in agent-state |
| `cron-scheduler`, `minion-orchestrator` | Assume gbrain's own scheduling, not Hermes cron |
| `skill-creator`, `skillify`, `skill-optimizer`, `skillpack-harvest` | Auto skill creation; Josemar disables this (issue #69) |

### Maintenance

The skillpack files at `/opt/gbrain/skills/` are the exact copy from the
pinned gbrain version. When upgrading gbrain (`GBRAIN_REF` in
`Dockerfile.hermes`), the operator must verify this compatibility filter
is still accurate — skills may be added, renamed, or change their feature
requirements. See `docs/gbrain-operations.md` → "gbrain Upgrade Checklist".

## Operator-Only Activation

`reindex` is not a chat action. It is a manual operator command run on the
host/container shell:

```bash
josemar-gbrain reindex
```

This performs init, config, full sync, content/link extraction, and schema
setup. See `docs/gbrain-operations.md` for the full activation and operations
runbook.

`josemar-gbrain refresh` is also operator-only, but it is scheduled by Hermes
cron for recurring manual-file reconciliation. It syncs vault files, extracts
stale content, and refreshes links without init/schema work. It uses
`gbrain sync --no-embed` even after issue #65 activation.

### Opt-in semantic/hybrid retrieval (issue #65)

`josemar-gbrain enable-embeddings` and `josemar-gbrain embed-backfill` are
operator-only and never called by chat/cron/startup. They are the activation
path for opt-in TEI E5 semantic/hybrid retrieval:

- `enable-embeddings`: native in-place embedding migration; forces
  keyword-only first, runs `migrate embeddings --no-embed` (no vectors
  produced), then only on success sets `search.mcp_keyword_only=false` and
  clears the `embedding_disabled` sentinel; preserves DB-only records.
- `embed-backfill`: one-time existing-vault vectorization that finalizes the
  native migration state; acquires the shared TaskNotes lock, runs at
  concurrency 1, verifies zero stale embeddings, and is retryable.

After both succeed, `gbrain search` and
`gbrain query --no-expand` both use
the hybrid/semantic provider path (not exact keyword). Before enable or after
`disable-embeddings`, text queries are keyword-only, image/cross-modal queries
are rejected, and `put`/`capture` do not embed. See
`docs/gbrain-operations.md` → "Issue #65: Opt-in TEI E5 Semantic/Hybrid
Retrieval" for the full flow, TEI health/config/preflight, model-tuple
immutability, and safe rollback (`disable-embeddings` sets keyword-only first
and writes the sentinel; vectors/TEI preserved; do not remove TEI first).