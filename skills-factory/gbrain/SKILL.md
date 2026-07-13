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
vault interface for retrieval, authoring, and linking. There is no custom
wrapper between chat and `gbrain`; the CLI is invoked directly from the Hermes
runtime. Operator-only activation (init/sync/extract/schema setup) is provided
by the `josemar-gbrain reindex` maintenance command and is not exposed from
chat.

## Important Notes

- **Native CLI only.** Use the `gbrain` binary directly (`/usr/local/bin/gbrain`
  in the container). Do not call `josemar-gbrain` from chat — it is an
  operator maintenance convenience for reindex/activation only.
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
- **Wikilinks and backlinks.** Obsidian `[[wikilinks]]` in page content are
  resolved automatically when a page is written (basename resolution is
  enabled). `gbrain backlinks` returns all incoming links, including
  wikilink-resolved edges. Cross-page link extraction for pre-existing pages
  is an operator action (`josemar-gbrain reindex` or
  `gbrain extract links --source db`); see `docs/gbrain-operations.md`.
- **Runtime state.** gbrain state lives under `/opt/data/.gbrain` (PGLite
  database, config, cache). It is runtime-only and never versioned by
  workspace sync.

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
