# GBrain Operations Runbook

This runbook covers safe initial production activation of the gated gbrain
native interface and later vault swaps.

## Overview

The Josemar gbrain integration is intentionally minimal and gated:

- The pinned `gbrain` CLI is installed in the Hermes image under `/opt/gbrain`
  with a `/usr/local/bin/gbrain` wrapper.
- A thin chat-facing skill (`skills-factory/gbrain`) exposes bounded native
  actions: `status`, `schema_status`, `search`, `get`, `capture`, `put`,
  `link`, and `backlinks`.
- Retrieval is **enabled by default** (`GBRAIN_ENABLED=true`), but all actions
  except `status` are blocked by the activation/config readiness marker until
  a valid reindex has been run. Set `GBRAIN_ENABLED=false` to force-close the
  gate.
- **Keyword-only native gbrain search.** Before every chat search the wrapper
  sets `search.mcp_keyword_only=true` and dispatches via
  `gbrain call search <json>`. In pinned gbrain this operation reads the
  config and calls `engine.searchKeyword`, never the vector/hybrid provider
  path. The wrapper forces this native keyword-only operation; it does not
  claim provider config cannot exist. Provider credentials are also unset
  before every gbrain invocation as defense-in-depth, and
  `GBRAIN_SKIP_STARTUP_HOOKS=1` is exported to prevent gbrain's detached
  update-check network call.
- **No auto indexing.** Nothing in startup, deploy, or chat triggers a
  reindex. Operators run `josemar-gbrain reindex` manually.
- **Activation/config readiness marker.** A marker
  (`$GBRAIN_HOME/.gbrain/readiness.json`) records the pinned gbrain ref,
  gbrain version, schema pack, `GBRAIN_HOME` realpath, `GBRAIN_BRAIN_REPO`
  realpath, configured `sync.repo_path`, and `search.mcp_keyword_only=true`.
  The gate opens when `GBRAIN_ENABLED=true` and the marker exists, is valid,
  and matches the current config. The marker does NOT track vault Git HEAD or
  clean worktree status. A successful native `capture`/`put` does not stale
  readiness — native gbrain writes are expected and safe.

## Pinned Values

- Bun: `1.3.14`
- gbrain ref: `058f448b9a4ba3d522e2c2a7a4615bccdd00ae76`
- gbrain version: `0.42.57.0`
- Schema pack: `gbrain-base-v2`

## Environment Defaults

| Variable | Default | Notes |
|---|---|---|
| `GBRAIN_ENABLED` | `true` | Master gate. Defaults to `true`; the readiness marker still blocks actions until a successful reindex. Set to `false` to force-close the gate. |
| `GBRAIN_HOME` | `/opt/data` | Parent directory; gbrain stores state under `$GBRAIN_HOME/.gbrain` (PGLite DB, config, readiness marker). Lives inside the existing writable `/opt/data` volume. |
| `GBRAIN_BRAIN_REPO` | `/opt/data/obsidian` | Vault path gbrain indexes. |
| `GBRAIN_SCHEMA_PACK` | `gbrain-base-v2` | Schema pack selector. Set to `josemar-user` to use the custom user-owned pack. |
| `GBRAIN_SCHEMA_SOURCE_ROOT` | `/opt/data/gbrain/schema-packs` | Source root for custom schema packs. The source pack for the selected `GBRAIN_SCHEMA_PACK` must exist at `<root>/<pack>/pack.yaml`. |
| `GBRAIN_QUERY_TIMEOUT_SECONDS` | `30` | Wrapper action timeout. |
| `GBRAIN_QUERY_MAX_INPUT_CHARS` | `2000` | Max search query input length. |
| `GBRAIN_QUERY_MAX_OUTPUT_CHARS` | `20000` | Max action output length. |
| `GBRAIN_QUERY_MAX_LIMIT` | `20` | Max search result limit. |
| `GBRAIN_CONTENT_MAX_CHARS` | `50000` | Max capture/put content length. |

No new Docker volume is added. `GBRAIN_HOME` (`/opt/data`) is the parent;
gbrain stores its state under `$GBRAIN_HOME/.gbrain`, which lives inside the
existing `/opt/data` (hermes-data) volume, already writable by the Hermes
runtime user. `HERMES_WRITABLE_VOLUMES` is not altered. `.gbrain` is the
protected workspace runtime path (never versioned by workspace sync).

## Safe Initial Production Activation

1. **Deploy the updated image** (includes pinned Bun, pinned gbrain, the
   `josemar-gbrain` wrapper, and the `gbrain` skill). `GBRAIN_ENABLED` defaults
   to `true`, but actions remain blocked by the readiness marker until
   reindex is run.

2. **Verify the vault exists and is accessible:**
   ```bash
   docker compose exec hermes ls -la /opt/data/obsidian
   ```
   The vault directory must exist and be readable.

3. **Run the manual reindex/activation** (operator-only, on the host/container
   shell):
   ```bash
   docker compose exec hermes /usr/local/bin/josemar-gbrain reindex
   ```
    This validates and installs the schema source pack (if a custom pack is
    selected), runs `gbrain init --pglite --no-embedding`, configures global
    basename resolution, configures `sync.repo_path`, runs a full sync with
    `--no-embed`, runs stale content extraction, runs link extraction
    (`extract links --source db`) to populate the wikilink/backlink graph,
    runs native `gbrain schema sync --apply` to backfill page.type for rows
    matching pack prefixes, sets `search.mcp_keyword_only=true` to force the
    native keyword-only operation, and atomically writes the activation/config
    marker with source/installed schema pack hashes. If any step fails, the
    marker is not written.

4. **Check status:**
   ```bash
   docker compose exec hermes /usr/local/bin/josemar-gbrain status
   ```
   Confirm `gate_open` is `true`. If `GBRAIN_ENABLED` was left at its default
   (`true`), the gate opens once `marker_ok` and `marker_matches` are `true`.
   If `GBRAIN_ENABLED=false`, set it to `true` (see step 5).

5. **If the gate was force-closed**, open it by setting `GBRAIN_ENABLED=true`
   in `.env` and restarting the Hermes container:
   ```bash
   # .env
   GBRAIN_ENABLED=true
   ```
   ```bash
   docker compose up -d hermes
   ```
   With the default (`true`), this step is only needed if the flag was
   explicitly set to `false`.

6. **Verify from chat:**
   ```bash
   gbrain-skill status
   gbrain-skill search "test query" --limit 5
   gbrain-skill capture --slug inbox/smoke-test --content "test capture"
   gbrain-skill get inbox/smoke-test
   ```

## Later Vault Swaps

When the Obsidian vault is swapped or materially changed:

1. **Disable/Revoke readiness.** Set `GBRAIN_ENABLED=false` in `.env` and
   restart the Hermes container so the gate closes immediately:
   ```bash
   # .env
   GBRAIN_ENABLED=false
   ```
   ```bash
   docker compose up -d hermes
   ```

2. **Backup / manual copy.** Take a backup or manual copy of the new vault
   state as appropriate for your deployment.

3. **Ensure the new vault directory exists and is accessible.**

4. **Run the manual reindex** (operator-only):
   ```bash
   docker compose exec hermes /usr/local/bin/josemar-gbrain reindex
   ```
   The reindex removes the old marker first, so a failed reindex leaves no
   stale marker.

5. **Check status:**
   ```bash
   docker compose exec hermes /usr/local/bin/josemar-gbrain status
   ```
   Confirm `marker_ok` and `marker_matches` are `true`.

6. **Reopen the gate** by setting `GBRAIN_ENABLED=true` in `.env` and
   restarting:
   ```bash
   # .env
   GBRAIN_ENABLED=true
   ```
   ```bash
   docker compose up -d hermes
   ```

## Native Write-Through

Native gbrain writes (`capture`, `put`) update both the database and the
on-disk vault files. The activation/config marker does not track vault file
state, so a successful write does not stale readiness. If a write-through
fails (disk error, permission issue), the action returns a failure envelope
and the vault files remain the user-facing artifact. Operators should inspect
the vault if a write-through failure is reported.

## Doctor Warns in No-Embedding Mode

`gbrain doctor` will warn that embeddings are not configured (no embedding
provider key, `embedding_disabled` mode). This is expected and intentional in
MVP. The wrapper forces keyword-only native gbrain search via
`search.mcp_keyword_only=true` and `gbrain call search`, which calls
`engine.searchKeyword` and never the vector/hybrid provider path. Do not
attempt to enable embeddings in MVP.

## User-Owned Schema Pack Workflow

The schema pack defines page types, link types, filing rules, and other
taxonomy for the brain. Custom schema packs are user-owned source files that
live in the private agent-state repo and are installed into gbrain's native
user-pack directory during operator activation.

### Source-First Approval Workflow

Schema editing is **never** done silently or from chat. The workflow is:

1. **Propose**: Josemar (or the user) proposes an exact diff to the source
   `pack.yaml` with impact analysis.
2. **Approve**: The user explicitly approves the change.
3. **Update**: The source `pack.yaml` is edited and committed to agent-state
   under `gbrain/schema-packs/josemar-user/pack.yaml`.
4. **Activate**: An operator runs `josemar-gbrain reindex` to validate the
   source pack, install it to `$GBRAIN_HOME/.gbrain/schema-packs/josemar-user/`,
   run native schema sync, and record source/installed hashes in the
   readiness marker. **No redeploy is required** — activation happens in the
   running deployment.

### Switching to the Custom Pack

1. Set `GBRAIN_SCHEMA_PACK=josemar-user` in `.env`.
2. Ensure the source pack exists at
   `$GBRAIN_SCHEMA_SOURCE_ROOT/josemar-user/pack.yaml` (default:
   `/opt/data/gbrain/schema-packs/josemar-user/pack.yaml`, which maps to the
   agent-state `gbrain/schema-packs/josemar-user/` path).
3. Run `josemar-gbrain reindex`.
4. Check `josemar-gbrain schema-status` to verify source/installed hashes
   match.

### Bundled Pack Fallback

If `GBRAIN_SCHEMA_PACK` is set to a bundled pack (`gbrain-base`,
`gbrain-base-v2`, `gbrain-recommended`), no source pack is required. The
marker records `bundled` sentinel values for source/installed paths and empty
hashes. Switching back to a bundled pack is the same workflow: set the env,
run reindex.

### Read-Only Schema Introspection

Use `schema_status` from chat to inspect the current schema state:

```bash
gbrain-skill schema-status
```

This reports the selected pack, whether it is bundled or custom, source and
installed paths/hashes, marker match status, gate status (`marker_ok`,
`marker_matches`, `gate_open`), and validation state. It does NOT expose
mutation, `schema use`, `schema sync --apply`, or generic schema args.
`schema_status` does not require the gate to be open — it is safe to call at
any time to diagnose readiness issues.

### Gate Behavior

The gate closes when:
- The source pack hash changes (source was edited but reindex not run).
- The installed hash differs from the marker (installed copy was modified).
- The source pack is missing or invalid for a custom pack selection.
- `GBRAIN_SCHEMA_PACK` does not match the marker's source pack name.

## Troubleshooting

- **`gate_open: false`** — Check `status` output. Common causes:
  `enabled: false`, `marker_ok: false` (no reindex run), or
  `marker_matches: false` (config changed since reindex — e.g. different
  `GBRAIN_HOME` or `GBRAIN_BRAIN_REPO` realpath, or `search.mcp_keyword_only`
  was unset).
- **`marker_stale`** — The activation config changed since the last reindex
  (pinned ref/version, schema pack, home/repo realpath, or keyword-only
  setting). Run `josemar-gbrain reindex` again.
- **`gbrain_search_failed`** — The native gbrain CLI returned an error. Check
  the `message` field for details. Confirm the PGLite database at
  `$GBRAIN_HOME/.gbrain` is intact.
- **`gbrain_put_failed` / `gbrain_capture_failed`** — A native write failed.
  Check the `message` field. The vault files remain the user-facing artifact;
  inspect the vault if needed.
- **`write_through_degraded`** — The native gbrain write succeeded in the
  database but the on-disk write-through failed or was skipped. The `message`
  field contains the underlying error. The Obsidian files are the user-facing
  artifact; inspect the vault and re-run the write if needed. Common causes:
  disk full, permission denied, or write-through explicitly skipped by config.
- **`wrapper_missing`** — The `josemar-gbrain` wrapper is not on PATH. Confirm
  the image was rebuilt with the new Dockerfile.
- **`rejected_action`** — An old `note.*` route name or `query` was used.
  Use the native gbrain actions: `status`, `schema_status`, `search`, `get`,
  `capture`, `put`, `link`, `backlinks`.
- **`schema_source_missing`** — A custom `GBRAIN_SCHEMA_PACK` was selected but
  the source `pack.yaml` was not found under `GBRAIN_SCHEMA_SOURCE_ROOT`. Create
  the source pack or switch to a bundled pack.
- **`schema_source_path_escape` / `schema_source_symlink`** — The source pack
  path is outside the source root or is a symlink. Only regular files under
  the source root are allowed.
- **`schema_validate_failed`** — The installed pack failed native gbrain
  `schema validate`. Check `pack.yaml` syntax and structure.
- **`marker_stale` (schema)** — The source pack hash changed since the last
  reindex (source was edited), or the installed hash differs from the marker.
  Run `josemar-gbrain reindex` to re-install and re-hash.