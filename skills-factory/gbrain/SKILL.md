---
name: gbrain
description: Native gbrain vault interface. Direct authoring, retrieval, and linking via the pinned native CLI. Keyword-only search, no embeddings. No auto indexing.
categories:
  - retrieval
  - search
  - knowledge
  - authoring
---

# GBrain Skill

Josemar uses the pinned native `gbrain` CLI directly as the canonical Obsidian
vault interface for general retrieval, authoring, and linking. The bounded
TaskNotes MCP is the only specialized exception: it invokes short-lived native
gbrain commands and must be used for TaskNotes task-file mutations. Operator-only
activation (init/sync/extract/schema setup) is provided by the
`josemar-gbrain reindex` maintenance command and is not exposed from chat.

## Important Notes

- **General vault work uses the native CLI.** Use the `gbrain` binary directly (`/usr/local/bin/gbrain`
  in the container). Do not call `josemar-gbrain` from chat — it is an
  operator maintenance convenience for reindex/activation only. For TaskNotes
  task files, use the bounded `task_*` MCP tools instead of native capture/put.
- **Keyword-only search, no embeddings.** Activation configures
  `search.mcp_keyword_only=true` and runs with `--no-embedding`, so search uses
  `engine.searchKeyword` and never the vector/hybrid provider path. Embeddings
  remain deferred in MVP. `gbrain doctor` will warn that embeddings are not
  configured; this is expected and intentional.
- **Periodic refresh, no chat reindex.** Chat does not run activation/reindex.
  Operators run `josemar-gbrain reindex` manually for activation, schema
  changes, or vault swaps. A Hermes cron runs `josemar-gbrain refresh` every 5
  minutes by default to pick up manual Obsidian/Syncthing edits. Refresh uses
  `gbrain sync --no-embed` while embeddings are deferred; revisit when enabling
  embeddings. See `docs/gbrain-operations.md`.
- **put is whole-page replacement.** `gbrain` upserts the entire page content.
  Rename, template instantiation, surgical section/frontmatter patching, and
  physical move are NOT offered natively; use Obsidian manually for those.
- **Updating an existing note — always read-then-put.** There is no patch or
  section-append API. To change any existing page (add a task, edit a section,
  update frontmatter), you MUST: (1) read the full current page with
  `gbrain get <slug>`, (2) apply the change in memory preserving all existing
  frontmatter and sections, (3) write the complete result back with
  `gbrain put <slug>`. Never edit vault note content through direct filesystem
  tools (`write_file`, `open()`, `cp`) — that bypasses gbrain's index, leaves
  the DB stale until the next 5-min refresh cron, and breaks link extraction.
  If `gbrain get` fails or returns incomplete content, retry once; if it still
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
  To backfill existing meetings: `gbrain chronicle-backfill` (enqueues
  extraction jobs; on PGLite, run `gbrain jobs submit chronicle_extract
  --params '{"slug":"<meeting-slug>","sourceId":"default"}' --follow` to
  process inline). See `docs/gbrain-operations.md` for setup details.

  For the full chronicle reference (event schema, kind taxonomy, ontology
  model, the note→atom relationship, query output structure, common
  workflows), load the reference on demand:
  `skill_view("gbrain", file_path="references/chronicle.md")`.

## Actions

All actions are invoked directly via the native `gbrain` CLI. Run `gbrain --help`
for the full command surface. The commands below are the ones Josemar uses
routinely from chat.

### `gbrain status`

Report native gbrain runtime/config status. Safe to call at any time.

```bash
gbrain status
```

### `gbrain search`

Keyword-only native search. Activation sets `search.mcp_keyword_only=true`, so
search uses `engine.searchKeyword` and never the vector/hybrid provider path.

```bash
gbrain search "notes on obsidian sync" --limit 10
```

Common flags:
- `--limit` (integer, optional, result cap)
- `--offset` (integer, optional, pagination)

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

Whole-page upsert by slug. Supports stdin for longer content.

```bash
printf '%s' "$FULL_MARKDOWN" | gbrain put inbox/my-note
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

`gbrain tags`, `gbrain timeline`, `gbrain graph`, `gbrain delete`,
`gbrain history`, and `gbrain revert` are available natively where useful.
Run `gbrain --help` for the full surface and per-command help.

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
| `capture` | `skills/capture/SKILL.md` | `gbrain capture` as single ingestion entrypoint, slug/idempotency contract, `--type` routing |
| `brain-filing-rules` | `skills/_brain-filing-rules.md` | "File by primary subject, not format/source"; `sources/` is only for bulk data; notability gate |
| `conventions/quality` | `skills/conventions/quality.md` | Citation format, source precedence, back-link iron law |
| `conventions/brain-first` | `skills/conventions/brain-first.md` | 4-step brain-first lookup chain before external APIs |
| `brain-ops` | `skills/brain-ops/SKILL.md` | Read→enrich→write loop, brain-first lookup protocol |
| `repo-architecture` | `skills/repo-architecture/SKILL.md` | Where new brain files go, directory conventions |
| `reports` | `skills/reports/SKILL.md` | Save/load timestamped reports |

### Requires disabled features (embeddings / dream cycle / LLM synthesis)

These document features that do NOT work in our keyword-only/no-embedding
setup. Read for awareness, but do not attempt to invoke:

| Skill | Requires |
|---|---|
| `query` | Semantic search (embeddings) |
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
stale content, and refreshes links without init/schema work.
