---
name: gbrain
description: Gated native gbrain vault interface. Bounded authoring, retrieval, and linking via the pinned native CLI. Keyword-only search, no embeddings or provider calls. No auto indexing.
categories:
  - retrieval
  - search
  - knowledge
  - authoring
---

# GBrain Skill

Thin, gated chat-facing wrapper around the pinned native `gbrain` CLI. Exposes
bounded native authoring, retrieval, and linking actions. `reindex` (initial
activation) is intentionally not exposed from chat.

## Important Notes

- **Gate defaults to enabled, marker-gated.** The gate (`GBRAIN_ENABLED`)
  defaults to `true`, but all actions except `status` are rejected until the
  activation/config readiness marker is valid and matches the current config.
  Set `GBRAIN_ENABLED=false` to force-close the gate.
- **Activation marker, not a vault snapshot.** Readiness is an activation/config
  invariant: pinned gbrain ref/version, schema pack, `GBRAIN_HOME` realpath,
  `GBRAIN_BRAIN_REPO` realpath, configured `sync.repo_path`, and
  `search.mcp_keyword_only=true`. The marker does NOT track vault Git HEAD or
  clean worktree status. A successful native `capture`/`put` does not stale
  readiness.
- **Keyword-only native gbrain search.** Before every chat search the wrapper
  sets `search.mcp_keyword_only=true` and dispatches via
  `gbrain call search <json>`. In pinned gbrain this operation reads the
  config and calls `engine.searchKeyword`, never the vector/hybrid provider
  path. The wrapper forces this native keyword-only operation; it does not
  claim provider config cannot exist. Provider credentials are also unset
  before every gbrain invocation as defense-in-depth.
- **No auto indexing.** Nothing in startup, deploy, or chat triggers a
  reindex. Operators run `josemar-gbrain reindex` manually.
- **put is whole-page replacement.** `put` upserts the entire page content.
  Rename, template instantiation, surgical section/frontmatter patching, and
  physical move are NOT offered (gbrain has no native equivalent; use Obsidian
  manually for those).
- **Old note.* routes are rejected.** This skill does not support the legacy
  vault-gateway `note.*` route API. Use the native gbrain actions below.

## Actions

### `status`

Return machine-readable gate status. Safe to call at any time (no gate
required). Status may read live gbrain config to verify the activation marker.

```bash
echo '{"action":"status"}' | gbrain
```

### `schema_status`

Read-only schema pack introspection. Safe to call at any time (no gate
required). Reports the selected pack, whether it is bundled or custom, source
and installed paths/hashes, marker match status, and validation state.

```bash
echo '{"action":"schema_status"}' | gbrain
```

Schema mutation is NOT exposed from chat. To change the schema pack, follow
the source-first approval workflow: propose a diff, get explicit approval,
update the source pack in agent-state, commit, then run
`josemar-gbrain reindex` to validate, install, and activate. See
`docs/gbrain-operations.md` for the schema workflow.

### `search`

Bounded keyword-only native gbrain search. Requires the gate to be open.

```bash
echo '{"action":"search","query":"notes on obsidian sync","limit":10,"offset":0}' | gbrain
```

Fields:
- `query` (string, required, non-empty, max `GBRAIN_QUERY_MAX_INPUT_CHARS` chars)
- `limit` (integer, optional, default 10, range 1..`GBRAIN_QUERY_MAX_LIMIT`)
- `offset` (integer, optional, default 0, non-negative)

### `get`

Read a page by slug. Requires the gate to be open.

```bash
echo '{"action":"get","slug":"inbox/my-note"}' | gbrain
```

Fields:
- `slug` (string, required, validated; no `..`, leading `/`, or newlines)

### `capture`

Native gbrain capture: write content as a new page. Requires the gate to be
open.

```bash
echo '{"action":"capture","content":"remember to follow up on X","slug":"inbox/custom","type":"note"}' | gbrain
```

Fields:
- `content` (string, required, non-empty, max `GBRAIN_CONTENT_MAX_CHARS` chars)
- `slug` (string, optional, validated)
- `type` (string, optional, lowercase kebab; e.g. `note`, `diary`)

Does not expose `--file` or arbitrary `--source`.

### `put`

Whole-page upsert by slug. Requires the gate to be open.

```bash
echo '{"action":"put","slug":"inbox/my-note","content":"---\\ntitle: My Note\\n---\\nFull content here"}' | gbrain
```

Fields:
- `slug` (string, required, validated)
- `content` (string, required, full markdown with YAML frontmatter, max `GBRAIN_CONTENT_MAX_CHARS` chars)

This is a whole-page replacement. There is no patch, section-append, or
frontmatter-surgical API. Rename and physical move are not offered.

### `link`

Create a manual link between two pages. Requires the gate to be open.

```bash
echo '{"action":"link","from":"inbox/a","to":"people/b","link_type":"mentions","context":"meeting notes","link_source":"manual"}' | gbrain
```

Fields:
- `from` (string, required, validated slug)
- `to` (string, required, validated slug)
- `link_type` (string, optional, lowercase kebab)
- `context` (string, optional, max 500 chars)
- `link_source` (string, optional, lowercase kebab; defaults to `manual`)

Reconciliation-managed sources (`markdown`, `frontmatter`, `mentions`,
`wikilink-resolved`) are rejected.

### `backlinks`

List incoming links to a page. Requires the gate to be open.

```bash
echo '{"action":"backlinks","slug":"people/b"}' | gbrain
```

Fields:
- `slug` (string, required, validated)

## Excluded Actions

`reindex` is not exposed from chat. It is a manual operator action run on the
host/container shell:

```bash
josemar-gbrain reindex
```

The following are intentionally NOT exposed from chat: generic `call`, `query`,
`sync`, admin, raw uploads, `delete`, `restore`, `purge-deleted`, and all old
`note.*` vault-gateway route names.

See `docs/gbrain-operations.md` for the safe activation and operations runbook.