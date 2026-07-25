# GBrain Operations Runbook

This runbook covers safe initial production activation of the native gbrain
interface and later vault swaps.

## Overview

The Josemar gbrain integration is intentionally minimal:

- The pinned `gbrain` CLI is installed in the Hermes image under `/opt/gbrain`
  with a `/usr/local/bin/gbrain` wrapper that `cd`s to `/opt/gbrain` and runs
  `bun src/cli.ts`.
- Josemar uses the native `gbrain` CLI directly from chat for general retrieval,
  authoring, and linking (`gbrain status`, `gbrain search`, `gbrain get`,
  `gbrain capture`, `gbrain put`, `gbrain link`, `gbrain backlinks`, plus
  `gbrain tags`, `gbrain timeline`, `gbrain graph`, `gbrain delete`,
  `gbrain history`, `gbrain revert` where useful). The bounded TaskNotes MCP is
  the only specialized exception; it uses short-lived native gbrain commands
  and is the required interface for TaskNotes task-file mutations.
- `scripts/josemar-gbrain` (installed at `/usr/local/bin/josemar-gbrain`) is
  retained only as an operator maintenance convenience. It exposes a single
  `reindex` subcommand that performs init, config, full sync, content/link
  extraction, and schema setup. It is not used from chat.
- **Keyword-only native gbrain search.** Activation configures
  `search.mcp_keyword_only=true` and runs with `--no-embedding`, so search uses
  `engine.searchKeyword` and never the vector/hybrid provider path.
  `GBRAIN_SKIP_STARTUP_HOOKS=1` is exported by the operator wrapper to prevent
  gbrain's detached update-check network call during reindex.
- **No auto indexing.** Nothing in startup, deploy, or chat triggers a
  reindex. Operators run `josemar-gbrain reindex` manually.
- **Runtime state.** gbrain state lives under `$GBRAIN_HOME/.gbrain` (PGLite
  database, config, cache). It is runtime-only and never versioned by
  workspace sync.
- **Git-native file sync.** The configured vault is a Git working tree with a
  valid `HEAD`. Gbrain sync uses Git commits and its `last_commit` bookmark for
  incremental reconciliation. `gbrain init` creates the database/configuration;
  it does not initialize the vault repository. A successful Josemar reindex or
  refresh therefore proves the vault Git prerequisite already exists. This
  repository is local-only and has no remote consumer.

## Pinned Values

- Bun: `1.3.14`
- gbrain ref: `058f448b9a4ba3d522e2c2a7a4615bccdd00ae76`
- gbrain version: `0.42.57.0`
- Schema pack: `gbrain-base-v2`

## gbrain Upgrade Checklist

When changing `GBRAIN_REF` in `Dockerfile.hermes` (upgrading gbrain), the
operator MUST verify the following after rebuild and before deploying:

1. **Skillpack compatibility filter.** The `gbrain` skill
   (`skills-factory/gbrain/SKILL.md` → "gbrain Skillpack Reference") has a
   compatibility filter listing which gbrain skills are safe, which require
   disabled features, and which conflict with Josemar's setup. The installed
   gbrain source tree at `/opt/gbrain/skills/` is the exact copy from the
   pinned version. On upgrade, skills may be added, renamed, removed, or
   change their feature requirements. Re-read `/opt/gbrain/skills/manifest.json`
   and spot-check the skills listed in the filter to confirm the categorization
   is still accurate. Update the filter if needed.

2. **Path-prefix inference table.** The `gbrain` skill documents the
   path-prefix → type inference table (from `src/core/markdown.ts`). On
   upgrade, verify the table in `src/core/markdown.ts::GBRAIN_BASE_PATH_PREFIXES`
   still matches what the skill documents. Update the skill if gbrain added,
   removed, or changed any prefix mappings.

3. **Chronicle / Life Chronicle features.** If chronicle is enabled by the
   time of the upgrade, verify the `auto_chronicle` config key and the
   chronicle commands (`gbrain day`, `gbrain since`, `gbrain last-seen`,
   `gbrain orient`, `gbrain chronicle-backfill`) still exist and behave as
   documented. Check if the `auto_chronicle` key was added to
   `KNOWN_CONFIG_KEYS` (the `--force` workaround in v0.42.57.0 may be fixed).

4. **Read-then-put behavior.** Verify `put_page` still re-runs auto-link
   on updates (including `status='skipped'`). The read-then-put rule in the
   gbrain skill and AGENTS.md depends on this behavior.

5. **Pinned values.** Update the version and ref in this section and in
   `skills-factory/gbrain/SKILL.md` if it references the version.

6. **Local patches.** The Dockerfile applies two patches to gbrain source
   after the git clone:
   - `patches/gbrain-inline-worker-gateway.patch` (git apply) — configures the
     AI gateway in the PGLite inline worker (`--follow`) path. If this patch
     fails to apply, the build will fail loudly — re-create the patch against
     the new gbrain source.
   - `sed -i 's/maxTokens: 1500/maxTokens: 8000/'` on
     `src/core/chronicle/extract-events.ts` — increases the chronicle judge
     token limit for reasoning models. If gbrain changes the value (e.g. to
     2000), the sed silently no-ops; verify the value is still 8000 after build.
   Both patches are documented inline in `Dockerfile.hermes`. If either has
   been fixed upstream, remove the patch from the Dockerfile.

## Environment Defaults

| Variable | Default | Notes |
|---|---|---|
| `GBRAIN_HOME` | `/opt/data` | Parent directory; gbrain stores state under `$GBRAIN_HOME/.gbrain` (PGLite DB, config, cache). Lives inside the existing writable `/opt/data` volume. |
| `GBRAIN_BRAIN_REPO` | `/opt/data/obsidian` | Vault path gbrain indexes. |
| `GBRAIN_SCHEMA_PACK` | `gbrain-base-v2` | Schema pack selector. Set to `josemar-user` to use the custom user-owned pack. |
| `GBRAIN_SCHEMA_SOURCE_ROOT` | `/opt/data/gbrain/schema-packs` | Source root for custom schema packs. The source pack for the selected `GBRAIN_SCHEMA_PACK` must exist at `<root>/<pack>/pack.yaml`. |
| `GBRAIN_REFRESH_INTERVAL` | `5` | Hermes cron interval, in minutes, for `josemar-gbrain refresh`. Set to `0` to disable. Refresh deliberately uses `gbrain sync --no-embed` while embeddings are deferred; revisit when issue #65 enables embeddings. |
| `GBRAIN_REFRESH_TIMEOUT` | `240` | Maximum seconds for the refresh child while it holds the shared TaskNotes/gbrain lock. |

No new Docker volume is added. `GBRAIN_HOME` (`/opt/data`) is the parent;
gbrain stores its state under `$GBRAIN_HOME/.gbrain`, which lives inside the
existing `/opt/data` (hermes-data) volume, already writable by the Hermes
runtime user. `HERMES_WRITABLE_VOLUMES` is not altered. `.gbrain` is the
protected workspace runtime path (never versioned by workspace sync).

## Safe Initial Production Activation

1. **Deploy the updated image** (includes pinned Bun, pinned gbrain, the
   `josemar-gbrain` operator wrapper, and the `gbrain` skill's `SKILL.md`).

2. **Verify the vault exists and is accessible:**
   ```bash
   docker compose exec hermes ls -la /opt/data/obsidian
   ```
   The vault directory must exist and be readable.

3. **Run the manual reindex/activation** (operator-only, as the Hermes runtime
   user so `$GBRAIN_HOME/.gbrain` remains writable by chat/runtime commands):
   ```bash
   docker compose exec hermes su -s /bin/sh hermes -c '/usr/local/bin/josemar-gbrain reindex'
   ```
   This validates and installs the schema source pack (if a custom pack is
   selected), runs `gbrain init --pglite --no-embedding`, configures
   `sync.repo_path`, configures `link_resolution.global_basename`, configures
   `search.mcp_keyword_only=true`, runs a full sync with `--no-embed`, runs
   stale content extraction, runs link extraction
   (`extract links --source db`) to populate the wikilink/backlink graph,
   runs native `gbrain schema sync --apply` (only when a custom schema pack
   is in use) to backfill page.type for rows matching pack prefixes, and marks
   the vault repo as a git `safe.directory`. If any step fails, the reindex
   returns a failure envelope.

4. **Verify gbrain is ready:**
   ```bash
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain status'
   ```
   Confirm gbrain reports a healthy runtime/config state.

5. **Verify from chat (native CLI):**
   ```bash
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain status'
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain search "test query" --limit 5'
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain capture "smoke test" --slug inbox/smoke-test --json'
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain get inbox/smoke-test'
   ```

## Later Vault Swaps

When the Obsidian vault is swapped or materially changed:

1. **Backup / manual copy.** Take a backup or manual copy of the new vault
   state as appropriate for your deployment.

2. **Ensure the new vault directory exists and is accessible.**

3. **Run the manual reindex** (operator-only):
   ```bash
   docker compose exec hermes su -s /bin/sh hermes -c '/usr/local/bin/josemar-gbrain reindex'
   ```

4. **Verify gbrain is ready:**
   ```bash
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain status'
   ```

5. **Smoke-test from chat** with `gbrain search` and `gbrain get` as above.

## Periodic Refresh for Manual Obsidian Edits

Manual Obsidian/Syncthing edits change the markdown files on disk, but native
gbrain search/get/backlinks read from gbrain's indexed state. The Hermes image
therefore installs a script-only cron job named `gbrain-refresh` that runs every
`GBRAIN_REFRESH_INTERVAL` minutes (default: `5`):

```bash
josemar-gbrain refresh
```

`refresh` is intentionally lighter than `reindex`: it assumes activation already
happened and runs only native sync, stale extraction, and link extraction. It
does **not** run init, schema install, or schema sync.

Refresh acquires `/opt/data/.locks/tasknotes.lock` nonblockingly through the
repo-owned lock runner. If a task operation is active, the cron logs a skip and
exits successfully. The refresh child is bounded by `GBRAIN_REFRESH_TIMEOUT`
(default `240` seconds). See `docs/tasknotes-mcp.md` for the task transaction and
recovery model.

Refresh currently calls `gbrain sync --no-embed` because Josemar is in
keyword-only/no-embedding mode. When embeddings are enabled (see issue #65),
revisit whether refresh should drop `--no-embed` or whether embeddings should
remain a separate scheduled job.

## Native Write-Through

For TaskNotes task files, use the bounded `task_*` MCP tools rather than direct
native capture/put. The tools preserve TaskNotes fields and add Git/profile/read-
back guards. The native commands below remain valid for general vault pages.

Native gbrain writes (`gbrain capture`, `gbrain put`) update both the database
and the on-disk vault files. If a write-through fails (disk error, permission
issue), the command returns a non-zero exit and an error message; the vault
files remain the user-facing artifact. Native write-through does not commit the
changed file. TaskNotes MCP supplies bounded local preflight and target commits;
general native gbrain writes still follow their caller's normal Git workflow.
Operators should inspect the vault if a write-through failure is reported.

### Optional native source hardening

Pinned gbrain provides `gbrain sources harden` for GitHub-backed sources, but
Josemar does not use that topology. The vault is synchronized as files through
Syncthing, has no Git remote consumer, and `josemar-gbrain refresh` passes
`--no-pull`. The TaskNotes adapter commits locally with hooks disabled and never
pushes. A future remote-backed vault would be a separate integration requiring
explicit design and validation.

## Doctor Warns in No-Embedding Mode

`gbrain doctor` will warn that embeddings are not configured (no embedding
provider key, `embedding_disabled` mode). This is expected and intentional in
MVP. Activation configures keyword-only native gbrain search via
`search.mcp_keyword_only=true`, which calls `engine.searchKeyword` and never
the vector/hybrid provider path. Do not attempt to enable embeddings in MVP.

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
   and run native schema sync. **No redeploy is required** — activation
   happens in the running deployment.

### Switching to the Custom Pack

1. Set `GBRAIN_SCHEMA_PACK=josemar-user` in `.env`.
2. Ensure the source pack exists at
   `$GBRAIN_SCHEMA_SOURCE_ROOT/josemar-user/pack.yaml` (default:
   `/opt/data/gbrain/schema-packs/josemar-user/pack.yaml`, which maps to the
   agent-state `gbrain/schema-packs/josemar-user/` path).
3. Run `josemar-gbrain reindex`.

### Bundled Pack Fallback

If `GBRAIN_SCHEMA_PACK` is set to a bundled pack (`gbrain-base`,
`gbrain-base-v2`, `gbrain-recommended`), no source pack is required and
`schema sync --apply` is skipped. Switching back to a bundled pack is the same
workflow: set the env, run reindex.

## Troubleshooting

- **`gbrain` command not found** — Confirm the image was rebuilt with the new
  Dockerfile. The native CLI lives at `/usr/local/bin/gbrain`.
- **Non-zero exit from `gbrain search`** — Check the native CLI error message.
  Confirm the PGLite database at `$GBRAIN_HOME/.gbrain` is intact and that
  reindex has been run.
- **Non-zero exit from `gbrain put` or `gbrain capture`** — Check the native CLI
  error message. The vault files remain the user-facing artifact; inspect the
  vault if needed.
- **Write-through failure** — The native gbrain write succeeded in the
  database but the on-disk write-through failed or was skipped. The error
  message contains the underlying cause. The Obsidian files are the
  user-facing artifact; inspect the vault and re-run the write if needed.
  Common causes: disk full, permission denied, or write-through explicitly
  skipped by config.
- **`schema_source_missing`** — A custom `GBRAIN_SCHEMA_PACK` was selected but
  the source `pack.yaml` was not found under `GBRAIN_SCHEMA_SOURCE_ROOT`. Create
  the source pack or switch to a bundled pack.
- **`schema_source_path_escape` / `schema_source_symlink`** — The source pack
  path is outside the source root or is a symlink. Only regular files under
  the source root are allowed.
- **`schema_validate_failed`** — The installed pack failed native gbrain
  `schema validate`. Check `pack.yaml` syntax and structure.
- **Embeddings warning from `gbrain doctor`** — Expected in MVP. Search is
  keyword-only by configuration; do not enable embeddings.
