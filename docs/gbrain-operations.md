# GBrain Operations Runbook

This runbook covers safe initial production activation of the native gbrain
interface and later vault swaps.

## Overview

The Josemar gbrain integration is intentionally minimal:

- The public `/usr/local/bin/gbrain` is the issue #110 safe adapter
  (transparent wrapper). The pinned native `gbrain` CLI is installed privately
  at `/opt/josemar/libexec/gbrain-native`; it must never be used as an agent
  command — only the locked operator/cron paths (`josemar-gbrain`, both
  refresh crons) and the TaskNotes MCP implementation invoke it.
- Chat and external general vault actions use the public `gbrain` command,
  which is safe by default (issue #110): it transparently provides the
  safe-adapter behavior — it executes the
  native `gbrain` CLI under the shared TaskNotes/gbrain lock as the `hermes`
  runtime user. Chat-facing invocations use `gbrain status`,
  `gbrain search`, `gbrain get`, `gbrain capture`,
  `gbrain put`, `gbrain link`, `gbrain backlinks`,
  plus `gbrain tags`, `gbrain timeline`,
  `gbrain graph`, `gbrain delete`,
  `gbrain history`, `gbrain revert` where useful (the
  public command passes arguments through unchanged; see "Issue #110: Safe
  gbrain Adapter" below). The bounded TaskNotes MCP is
  the only specialized exception; it uses short-lived native gbrain commands
  and is the required interface for TaskNotes task-file mutations.
- `scripts/josemar-gbrain` (installed at `/usr/local/bin/josemar-gbrain`) is
  retained only as an operator maintenance convenience. It exposes two
  operator-only subcommands: `reindex` (init, config, full sync, content/link
  extraction, and schema setup) and `refresh` (incremental reconciliation of
  manual Obsidian/Syncthing edits via `gbrain sync --no-embed`, stale
  extraction, and link extraction, without init/schema work). Neither is used
  from chat.
- **Keyword-only native gbrain search by default.** Activation configures
  `search.mcp_keyword_only=true` and runs with `--no-embedding`, so search uses
  `engine.searchKeyword` and never the vector/hybrid provider path; text queries
  are keyword-only, image/cross-modal queries are rejected, and `gbrain put`/
  `capture` do not embed (the `embedding_disabled` sentinel makes `embed`/
  `import` refuse). Semantic/hybrid retrieval is opt-in via issue #65 (see
  "Issue #65: Opt-in TEI E5 Semantic/Hybrid Retrieval" below); the base deploy
  remains keyword-only. `GBRAIN_SKIP_STARTUP_HOOKS=1` is exported by the
  operator wrapper to prevent gbrain's detached update-check network call
  during reindex.
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

## Issue #110: Safe gbrain Adapter — Access Non-Negotiables

The public `gbrain` command is the single agent-facing gateway for vault
access and is safe by default: it transparently provides the issue #110
safe-adapter behavior — it executes only the pinned native `gbrain` CLI,
never as root (it drops to the `hermes` runtime user before the shared lock is
touched), and it serializes the call through the shared TaskNotes/gbrain lock
with bounded lock acquisition and process runtime. `gbrain-chat-run` is a
temporary compatibility alias for this behavior (source
`scripts/gbrain_chat_run.py`); existing instructions and scripts may still
invoke it, but new instructions MUST use the public `gbrain` command. The
safe-adapter behavior is deliberately NOT used internally by the TaskNotes
MCP (already the lock owner) and not by the `josemar-gbrain` operator wrapper
(which self-locks its own gbrain access).

The wrapper is being hardened separately. Implementation details (flags,
timeouts, exported environment, exit codes) are not a stable contract: read
`scripts/gbrain_chat_run.py` for the current behavior, and treat only the
non-negotiables below as policy.

**Threat model and deployment scope.** The issue #110 wrapper prevents
accidental, prompt-driven, and cooperative-concurrency PGLite access: it
enforces the `hermes` runtime user and serializes every call through the
shared lock (single-writer). It is NOT a security boundary against a
compromised process running as the same UID (`hermes`) in the container or on
the host shell — the private native path (`/opt/josemar/libexec/gbrain-native`)
is defense in depth, not a complete security boundary, and issue #110 does not
protect against a fully compromised container/session. Do not overstate the
protection in agent-facing instructions. Deployment paths are fixed:
`/opt/data` (runtime state), `/opt/data/obsidian` (vault), and the global lock
at `/opt/data/.locks/tasknotes.lock`; no relocatable overrides are supported
in this deployment.

**Startup-hook suppression (issue #112).** The private native launcher
(`/opt/josemar/libexec/gbrain-native`) enforces `GBRAIN_SKIP_STARTUP_HOOKS=1`
on every invocation, so gbrain's startup upgrade notice is never emitted and
cannot corrupt notes when a caller merges stderr into stdin (`2>&1`). This is
defense in depth, not generic stderr filtering: `put --stdin` remains
forbidden, public agent-facing calls use `gbrain`, and TaskNotes uses the
private native launcher only under its transaction-level lock.

Immediate non-negotiables:

1. **No root execution.** Never run gbrain, `josemar-gbrain`, or vault Git
   operations as root. All commands run as the Hermes runtime user, exactly as
   the activation sections below do (`docker compose exec hermes su -s /bin/sh
   hermes -c '...'`). Runtime gbrain state under `/opt/data/.gbrain` belongs
   to that user; root-run writes corrupt ownership and break every later path.
2. **Public `gbrain` is mandatory for agent-facing access.** ALL chat, skill,
   and external general vault actions use the public `gbrain` command (safe by
   default). The internal private native gbrain path
   (`/opt/josemar/libexec/gbrain-native`; raw CLI execution used by the
   `josemar-gbrain` operator wrapper, both refresh crons, and the
   TaskNotes MCP) must never be presented as an agent command; those paths
   already own or self-serialize their gbrain access through the same lock and
   must avoid nesting.
3. **No concurrent PGLite opens.** The gbrain database is single-writer PGLite.
   No two processes may open or mutate it concurrently — including during
   maintenance. Plan maintenance inside a paused window (see "Cron
   Pause/Resume for Maintenance Windows" below).
4. **Cooperative flock.** The global lock at `/opt/data/.locks/tasknotes.lock`
   serializes cooperative access: TaskNotes transactions, both refresh crons,
   backfills, and every adapted path take it (crons nonblockingly, skipping
   when busy). The public `gbrain` command acquires it through the same lock
   runner.
5. **No nested wrapper usage in TaskNotes.** TaskNotes remains a bounded MCP
   adapter implemented on short-lived native gbrain commands, the sole
   task-file writer. It retains its transaction-level global lock and internal
   native invocation; it must never route through the public `gbrain`
   wrapper's lock path internally, nor be invoked from it. Task mutations go
   through the `task_*` MCP tools only. See `docs/tasknotes-mcp.md`.

## Pinned Values

- Bun: `1.3.14`
- gbrain ref: `15b9863d13635d173562a54f55a1d388bfcf546b`
- gbrain version: `0.42.73.2`
- Schema pack: `josemar` (state-owned custom pack extending `gbrain-base-v2`; the active schema marker in this deployment)

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
   chronicle commands (`gbrain day`, `gbrain since`,
   `gbrain last-seen`, `gbrain orient`) still exist and behave as
   documented; `gbrain chronicle-backfill` (operator-only) is verified the
   same way. As of v0.42.73.2, `auto_chronicle` is a registered
   `KNOWN_CONFIG_KEYS` entry, so it no longer requires the `--force` flag that
   the v0.42.57.0 config-key registry bug forced. The chronicle judge token
   limit is now raised through the supported `chronicle.judge_max_tokens=8000`
   configuration key (see item 7 below) instead of a source patch/sed. Re-verify
   these behaviors against the rebuilt image and refresh the version-sensitive
   chronicle caveats in `skills-factory/gbrain/references/chronicle.md` only
   when a behavior change is actually established.

4. **Read-then-write behavior.** Verify `put_page` still re-runs auto-link
   on updates (including `status='skipped'`). The read-then-write rule in the
   gbrain skill and AGENTS.md depends on this behavior.

5. **TaskNotes mutation path.** Verify
   `gbrain capture --stdin --slug <slug> --source <id> --json` remains the
   documented source-routed write path, preserves the complete body on both
   create and update, returns a top-level boolean `written` field, and injects
   only the provenance fields excluded by TaskNotes semantic verification
   (`captured_via`, `captured_at`, `ingested_via`, `ingested_at`, and
   `source_kind`). If this contract changes, update
   `scripts/tasknotes_mcp_core.py` and its focused tests before deployment.
   Run `python3 -m unittest tests.tasknotes_mcp.test_core -v` and the opt-in
   real-gbrain TaskNotes E2E against the rebuilt image.

6. **Pinned values.** Update the version and ref in this section and in
   `skills-factory/gbrain/SKILL.md` if it references the version.

7. **Local patch.** The Dockerfile applies one patch to gbrain source
   after the git clone:
   - `patches/gbrain-inline-worker-gateway.patch` (git apply) — configures the
     AI gateway in the PGLite inline worker (`--follow`) path. If this patch
     fails to apply, the build will fail loudly — re-create the patch against
     the new gbrain source.
   - **Chronicle judge token limit.** As of v0.42.73.2, the chronicle judge
     token limit is raised through the supported
     `chronicle.judge_max_tokens=8000` configuration key (set during
     activation) instead of the previous source patch/sed on
     `src/core/chronicle/extract-events.ts`. The old
     `sed -i 's/maxTokens: 1500/maxTokens: 8000/'` workaround is no longer
     used; if it is still present in the Dockerfile it should be removed. If
     gbrain changes the default or the config key name, verify the effective
     judge token limit is still 8000 after build.
   The patch is documented inline in `Dockerfile.hermes`. Remove individual
   hunks when their corresponding behavior is fixed upstream.

8. **Migrations / activation.** The v0.42.73.2 upgrade introduces database
   migrations v123–v125. These MUST be applied by running the activation
   (`josemar-gbrain reindex`) against the rebuilt image before the upgraded
   gbrain is considered ready. Reindex runs `gbrain init`/schema setup, which
   applies pending migrations to the PGLite database under
   `$GBRAIN_HOME/.gbrain`. Skipping activation leaves the database on an
   older schema and may cause runtime errors or silent behavior changes.
   See "Safe Initial Production Activation" below for the activation
   procedure.

9. **gbrain conformance — mechanical gates (issue #127).** Before changing the
   pin, run the opt-in conformance suites against the CURRENT committed pin to
   establish a baseline, then against the exact candidate SHA. The five targets
   are documented in `tests/README.md` → "gbrain Conformance":
   - `make test-gbrain-conformance` — core provider-free suite. **Required for
     every upgrade.**
   - `make test-gbrain-conformance-embeddings` — real TEI/E5 gate. **Required
     when semantic mode is part of the deployed state/upgrade surface.**
   - `make test-gbrain-conformance-chronicle` — provider-gated Chronicle
     lifecycle against a credential-free loopback LiteLLM mock (no external
     network/provider): real `chronicle_extract`, timeline projection, and
     semantic reads on deterministic synthetic state. **Required when
     timeline/Chronicle behavior is part of the deployed state/upgrade
     surface.**
   - `make test-gbrain-upgrade-conformance GBRAIN_CONFORMANCE_CANDIDATE_REF=<40-hex-sha>`
     — candidate upgrade against the same disposable volumes. **Required.**
   - `make test-gbrain-upgrade-conformance-embeddings GBRAIN_CONFORMANCE_CANDIDATE_REF=<40-hex-sha>`
     — candidate upgrade with the real TEI gate. **Required when semantic mode
     is deployed.**
   The candidate SHA must be an exact 40-hex Git commit SHA; the Make targets
   reject an empty ref before Python and the conformance support layer
   validates the exact form. A candidate build failure because a local patch no
   longer applies is an upgrade incompatibility to record, not a harness
   failure.

10. **Existing TaskNotes real-gbrain E2E.** Run the existing real-gbrain
    TaskNotes lifecycle against the final rebuilt image using its documented
    command (`tests/README.md` → "TaskNotes real-gbrain lifecycle").

11. **Fast suites.** Run `make test` and `make verify` (fast unit/contract plus
    compose validation) before and after the change. The conformance targets
    are never invoked by them (they are gated on their own `RUN_GBRAIN_*`
    vars).

12. **Probe reporting.** Review the JSON reports under
    `dump_folder/gbrain-conformance/` for the #124/#125 probe classifications
    (`fixed` / `present` / `changed_failure_mode` / `inconclusive`) and
    summarize them in the dependency-upgrade PR/issue.

13. **Then change the pin.** Only after the required core (and embedding, when
    deployed, and Chronicle, when timeline/Chronicle behavior matters) candidate
    suites are green, update `GBRAIN_REF` in
    `Dockerfile.hermes` and the pinned values in this section and
    `skills-factory/gbrain/SKILL.md` if it references the version.

14. **Complement, not replace.** The conformance gates complement — they do not
    replace — the recovery/deploy validation (vault-recovery DR drill, deploy
    workflow) and the operator runbook checks above.

## Environment Defaults

| Variable | Default | Notes |
|---|---|---|
| `GBRAIN_HOME` | `/opt/data` | Parent directory; gbrain stores state under `$GBRAIN_HOME/.gbrain` (PGLite DB, config, cache). Lives inside the existing writable `/opt/data` volume. |
| `GBRAIN_BRAIN_REPO` | `/opt/data/obsidian` | Vault path gbrain indexes. |
| `GBRAIN_SCHEMA_PACK` | `josemar` | Active schema pack selector: the state-owned custom pack extending `gbrain-base-v2` (the active schema marker in this deployment). Bundled fallbacks: `gbrain-base`, `gbrain-base-v2`, `gbrain-recommended`. |
| `GBRAIN_SCHEMA_SOURCE_ROOT` | `/opt/data/.gbrain/schema-packs` | Source root for custom schema packs. The source pack for the selected `GBRAIN_SCHEMA_PACK` must exist at `<root>/<pack>/pack.yaml`. |
| `GBRAIN_REFRESH_INTERVAL` | `5` | Hermes cron interval, in minutes, for `josemar-gbrain refresh`. Set to `0` to disable. Refresh deliberately uses `gbrain sync --no-embed` even after issue #65 activation; embedding stale pages is a separate scheduled job, not folded into the periodic refresh. See "Issue #65: Opt-in TEI E5 Semantic/Hybrid Retrieval" below and `docs/memory-embeddings-evaluation.md` for the issue #86/#65 evaluation. |
| `GBRAIN_REFRESH_TIMEOUT` | `240` | Maximum seconds for the refresh child while it holds the shared TaskNotes/gbrain lock. |
| `GBRAIN_EMBED_REFRESH_SCHEDULE` | `0 5 * * *` | Cron expression (local time) for the daily `gbrain-embedding-refresh` job. Set to `0` to disable (removes the owned job at init). See "Cron Pause/Resume for Maintenance Windows". |

No new Docker volume is added. `GBRAIN_HOME` (`/opt/data`) is the parent;
gbrain stores its state under `$GBRAIN_HOME/.gbrain`, which lives inside the
existing `/opt/data` (hermes-data) volume, already writable by the Hermes
runtime user. `HERMES_WRITABLE_VOLUMES` is not altered. `.gbrain` is the
protected workspace runtime path (never versioned by workspace sync).
Deployment paths are fixed — `/opt/data` (state), `/opt/data/obsidian`
(vault), `/opt/data/.locks/tasknotes.lock` (global lock); no relocatable
overrides are supported in this deployment.

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

5. **Verify from chat (public `gbrain`):**
   ```bash
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain status'
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain search "test query" --limit 5'
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain capture "smoke test" --slug inbox/smoke-test --json'
   docker compose exec hermes su -s /bin/sh hermes -c 'gbrain get inbox/smoke-test'
   ```
   This exercises the exact path chat agents use (public wrapper → shared lock →
   native CLI).

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

5. **Smoke-test from chat** with `gbrain search` and
   `gbrain get` as above.

## Periodic Refresh for Manual Obsidian Edits

Manual Obsidian/Syncthing edits change the markdown files on disk, but native
gbrain search/get/backlinks read from gbrain's indexed state. The Hermes image
therefore installs a script-only cron job named `gbrain-refresh` that runs every
`GBRAIN_REFRESH_INTERVAL` minutes (default: `5`):

```bash
josemar-gbrain refresh
```

`refresh` is intentionally lighter than `reindex`: it assumes activation already
happened and runs only an incremental `gbrain sync --no-embed` (reconciling
vault files changed since the stored `last_commit` bookmark), plus stale
content extraction and link extraction. It does **not** run init, schema
install, or schema sync. `reindex` remains the only full activation/rebuild
path (init, config, full sync, content/link extraction, schema setup).

Refresh acquires `/opt/data/.locks/tasknotes.lock` nonblockingly through the
repo-owned lock runner. If a task operation is active, the cron logs a skip and
exits successfully. The refresh child is bounded by `GBRAIN_REFRESH_TIMEOUT`
(default `240` seconds). See `docs/tasknotes-mcp.md` for the task transaction and
recovery model.

Refresh currently calls `gbrain sync --no-embed` and **must remain `--no-embed`
even after issue #65 activation**. Embedding stale pages is a separate scheduled
job, not folded into the periodic refresh; manual vault edits made after the
one-time `embed-backfill` need a later explicit `embed-backfill` in phase one to
be vectorized. See "Issue #65: Opt-in TEI E5 Semantic/Hybrid Retrieval" below
and `docs/memory-embeddings-evaluation.md` for the issue #86/#65 evaluation, the
shared embedding service design, and the staged activation/rollback plan.

## Cron Pause/Resume for Maintenance Windows

Two Hermes script crons touch gbrain state and BOTH must be paused together for
exclusive maintenance windows:

- `gbrain-refresh` — every `GBRAIN_REFRESH_INTERVAL` minutes (default `5`);
  runs `josemar-gbrain refresh` (incremental `sync --no-embed`).
- `gbrain-embedding-refresh` — daily at `GBRAIN_EMBED_REFRESH_SCHEDULE`
  (default `0 5 * * *`, local time); runs `josemar-gbrain refresh-embeddings`
  (stale-only embed, concurrency 1).

Neither cron uses the public `gbrain` wrapper: each runs the self-locking
`josemar-gbrain` wrapper, which serializes its own gbrain access through the
same shared lock. Crons and the public wrapper never nest inside each other.

**When to pause (both crons):** recovery (TaskNotes recovery marker, corrupted
or uncertain state), reindex/rebuild, database migrations, vault swaps, and
unadapted/third-party diagnostics (any tool that opens the PGLite database
outside the wrapper/flock contract). Public `gbrain` is safe by default, but
that does not replace maintenance windows: operator maintenance still pauses
BOTH crons exactly as documented here. **Routine adapted access does NOT
require pausing** — the crons are nonblocking flock cooperators and skip
cleanly when
the lock is busy (exit 0).

**Pause (immediate, no restart)** — as the Hermes runtime user:

```bash
docker compose exec hermes su -s /bin/sh hermes -c \
  '/opt/hermes/.venv/bin/hermes cron remove gbrain-refresh'
docker compose exec hermes su -s /bin/sh hermes -c \
  '/opt/hermes/.venv/bin/hermes cron remove gbrain-embedding-refresh'
```

Also set `GBRAIN_REFRESH_INTERVAL=0` and `GBRAIN_EMBED_REFRESH_SCHEDULE=0` in
`.env` for the duration of the window so a container restart during maintenance
does not recreate the jobs. Note that `GBRAIN_REFRESH_INTERVAL=0` only prevents
creation at init — it does not remove an already-installed `gbrain-refresh`
job, so remove the job at runtime as shown above.

**Resume** — restore the normal env values (`GBRAIN_REFRESH_INTERVAL=5`,
`GBRAIN_EMBED_REFRESH_SCHEDULE=0 5 * * *`) and restart the container:
`docker-hermes-init.sh` reconciles and recreates both owned jobs. To resume
without a restart, recreate the jobs manually with the same flags the init
script uses:

```bash
docker compose exec hermes su -s /bin/sh hermes -c \
  '/opt/hermes/.venv/bin/hermes cron create "every 5m" --no-agent \
   --script hermes-gbrain-refresh-cron.sh --workdir /opt/data \
   --name gbrain-refresh'
docker compose exec hermes su -s /bin/sh hermes -c \
  '/opt/hermes/.venv/bin/hermes cron create "0 5 * * *" --no-agent \
   --script hermes-gbrain-embedding-refresh-cron.sh --workdir /opt/data \
   --name gbrain-embedding-refresh'
```

Verify both jobs are scheduled again before ending the maintenance window.

## Native Write-Through

For TaskNotes task files, use the bounded `task_*` MCP tools rather than direct
native capture/put. The tools preserve TaskNotes fields and add Git/profile/read-
back guards. The native commands below describe the underlying CLI behavior;
from chat they are invoked through the public `gbrain` command (safe by
default),
while operator maintenance may call them natively.

Native gbrain writes (`gbrain capture`, `gbrain put`) aim to
update both the
database and the on-disk vault files, but a successful process exit does NOT
by itself guarantee the on-disk write-through completed. A write-through can
fail or be skipped (disk error, permission issue, or config) even when the
command returns a successful exit. Structured callers should inspect the
result status rather than relying on exit code alone:

- For `gbrain put`, inspect `write_through.written` in the result envelope.
- For `gbrain capture`, inspect the top-level `written` field in the result
  envelope.

Treat the write as durable only when the corresponding status field reports a
successful write-through. A failure may present as either a non-zero exit OR
a structured result whose write-through status is unsuccessful — both must be
checked. The vault files remain the user-facing artifact. Native
write-through does not commit the changed file. TaskNotes MCP supplies
bounded local preflight and target commits; general native gbrain writes
still follow their caller's normal Git workflow. Operators should inspect the
vault if a write-through failure is reported.

### Optional native source hardening

Pinned gbrain provides `gbrain sources harden` for GitHub-backed sources, but
Josemar does not use that topology. The vault is synchronized as files through
Syncthing, has no Git remote consumer, and `josemar-gbrain refresh` passes
`--no-pull`. The TaskNotes adapter commits locally with hooks disabled and never
pushes. A future remote-backed vault would be a separate integration requiring
explicit design and validation.

## Doctor Warns in No-Embedding Mode

`gbrain doctor` will warn that embeddings are not configured (no embedding
provider key, `embedding_disabled` mode) while the base deploy runs
keyword-only. This is expected and intentional in the base deploy. Activation
configures keyword-only native gbrain search via `search.mcp_keyword_only=true`,
which calls `engine.searchKeyword` and never the vector/hybrid provider path;
text queries are keyword-only, and image/cross-modal queries are rejected.
Issue #65 (gbrain embeddings) is opt-in; see "Issue #65: Opt-in TEI E5
Semantic/Hybrid Retrieval" below and `docs/memory-embeddings-evaluation.md` for
the evaluation and the prerequisites landed in this branch (the optional
`docker-compose.embeddings.yml` overlay and the pinned gbrain E5 preprocessing
patch are present but inert until the operator opts in).

## Issue #65: Opt-in TEI E5 Semantic/Hybrid Retrieval

Issue #65 adds opt-in semantic/hybrid retrieval to gbrain using a local TEI
(Text Embeddings Inference) service running the pinned
`intfloat/multilingual-e5-small` model. This is **opt-in and not enabled by
default**; the base deploy remains keyword-only. The prerequisites (the
optional `docker-compose.embeddings.yml` overlay and the pinned gbrain E5
preprocessing patch) are present in this branch but inert until the operator
opts in. No secrets are required (the model is a public Hugging Face model; the
cache volume holds only downloaded weights).

### Intended operator flow

The deployment overlay is controlled by the optional repository variable
`GBRAIN_EMBEDDINGS_ENABLED` (strict `true`/`false`, default `false`). The deploy
workflow also selects the overlay for `MNEMOSYNE_DEPLOY_MODE=pilot` or `backup`.
It writes the effective value to `.env` and `GITHUB_ENV`, verifies the TEI
service is healthy when selected (and absent otherwise), and **does not**
enable gbrain embeddings or start a backfill.

After a deploy with the overlay enabled and healthy, run the manual
`.github/workflows/gbrain-embedding-backfill.yml` workflow. Type the exact
destructive confirmation `ENABLE_AND_BACKFILL`. It requires
`GBRAIN_EMBEDDINGS_ENABLED=true`, validates the existing Hermes container and
its non-empty embedding settings, then preflights the `hermes` identity with
workdir `/opt/data`, `HOME=HERMES_HOME=GBRAIN_HOME=/opt/data`, and
`XDG_CONFIG_HOME=/opt/data/.config`. It runs activation and backfill with
`docker exec --user hermes --workdir /opt/data` and those same environment
values, followed by a same-identity smoke test. Direct root-default
`docker exec` is intentionally not used: the container Config.User is root,
while the runtime gbrain state belongs to hermes under `/opt/data`. The
workflow is fail-closed, retry-safe, never exposes secrets, and never rebuilds
or deploys. Do not run it before the overlay-enabled deploy.

1. **Deploy the embedding compose overlay.** Add
   `docker-compose.embeddings.yml` to `COMPOSE_FILE` so the `embeddings` TEI
   service and the dedicated `embeddings-net` network are present. The overlay
   wires `GBRAIN_EMBEDDING_MODEL` (as `llama-server:<model>`),
   `GBRAIN_EMBEDDING_DIMENSIONS`, and `LLAMA_SERVER_BASE_URL` into hermes; these
   are inert until gbrain embeddings are enabled. See
   `docker-compose.embeddings.yml` and `.env.example`.

2. **Verify TEI health/config/preflight.** Before enabling gbrain embeddings,
   confirm the TEI service is healthy and serving the expected model tuple:
   ```bash
   docker compose exec hermes curl -fsS http://embeddings:80/health
   docker compose exec hermes curl -fsS http://embeddings:80/info
   ```
   `/health` must report healthy; `/info` must report the pinned model id,
   dimensions, and revision matching the migration tuple
   (`EMBEDDING_MODEL_ID`, `EMBEDDING_MODEL_REVISION`,
   `EMBEDDING_MODEL_DIMENSIONS`). Mismatched revision/dimensions is tuple drift
   and must be resolved before proceeding (see "Model tuple immutability"
   below).

3. **Run `josemar-gbrain enable-embeddings`** (native in-place migration).
   This performs the native gbrain embedding migration in place: it forces
   `search.mcp_keyword_only=true` first (so any error path leaves keyword-only
   enabled), configures the embedding provider and runs the database migration
   with `--no-embed` (no vectors produced), then only on success sets
   `search.mcp_keyword_only=false` and clears the `embedding_disabled` sentinel.
   It **preserves DB-only records** (e.g. chronicle timeline atoms) and **leaves
   hybrid/semantic retrieval disabled until the migration succeeds**, so a
   failed migration does not leave search in a half-enabled state. Hybrid/
   semantic retrieval is only reachable after this step completes
   successfully. The migration does not by itself vectorize the existing vault;
   the explicit `embed-backfill` finalizes the native migration state.

4. **Run `josemar-gbrain embed-backfill`** (one-time existing-vault
   vectorization). This is the operator-only one-shot backfill that vectorizes
   the existing vault. It requires `GBRAIN_EMBEDDING_MODEL` and
   `GBRAIN_EMBEDDING_DIMENSIONS` to be set, acquires the shared TaskNotes lock
   nonblocking, runs `gbrain embed --stale --include-null-signature` at
   concurrency 1, then asserts no stale embeddings remain (dry-run verify).
   It does NOT init or reinit PGLite. It is retryable: re-running it resumes
   the backfill of any remaining stale/null-signature rows. Do not run it
   through the periodic refresh path.

5. **`gbrain search` and `gbrain query --no-expand` both
   become hybrid/semantic.**
   `enable-embeddings` sets `search.mcp_keyword_only=false` and clears the
   `embedding_disabled` sentinel on migration success, so both `gbrain search`
   and `gbrain query --no-expand` use the hybrid/semantic provider path (not
   exact keyword). The migration runs with `--no-embed` (no vectors produced);
   the explicit `embed-backfill` finalizes the native migration state by
   vectorizing the existing vault.

### Keyword-only mode (before enable or after disable-embeddings)

Before `enable-embeddings` (base deploy) or after `disable-embeddings`,
`search.mcp_keyword_only=true` and the `embedding_disabled` sentinel are in
effect: text queries are keyword-only (`engine.searchKeyword`), image/cross-
modal queries are rejected, and `gbrain put`/`capture` do not embed (the
sentinel makes `embed`/`import` refuse via `assertEmbeddingEnabled`). This is
the safe default; `enable-embeddings` clears the sentinel on migration success.

### Refresh stays `--no-embed`

The periodic `josemar-gbrain refresh` (every `GBRAIN_REFRESH_INTERVAL` minutes,
default 5) **continues to use `gbrain sync --no-embed`** even after issue #65
activation. Embedding stale pages is a separate scheduled job, not folded into
refresh. In phase one, manual vault edits made after the one-time
`embed-backfill` need a later explicit `embed-backfill` to be vectorized; the
5-min refresh reconciles the keyword index only.

After the first manual `embed-backfill`, the daily no-agent Hermes job
(`gbrain-embedding-refresh`, `0 5 * * *`) runs
`josemar-gbrain refresh-embeddings`. It owns the TaskNotes lock, validates
semantic mode and the completion tuple, and runs the exact stale-only embed
at concurrency 1. Josemar may invoke this path from chat only after an
explicit user request.

### Model tuple immutability

The model tuple — model id + revision + dimensions + query/passage prefixes +
normalization — is **immutable once embeddings are enabled**. Changing any
element requires a full migration/backfill event (re-run `enable-embeddings`
then `embed-backfill`), because mixing vectors from different models/spaces is
invalid. **Revision drift** (the TEI-served revision no longer matches the
pinned `EMBEDDING_MODEL_REVISION`) is a tuple change: the gbrain E5 signature
includes the revision, so a changed revision produces stale vectors and
requires the full `enable-embeddings` + `embed-backfill` to re-vectorize, not
just a restart. The pinned gbrain E5 preprocessing patch stamps a versioned
embedding signature (`e5-query-passage-v1`) for E5 models so raw and prefixed
vectors are detected as incompatible; non-E5 signatures are byte-identical to
the pre-patch form.

### Prefixes are model-gated in the Josemar patch

The overlay's `EMBEDDING_QUERY_PREFIX` / `EMBEDDING_PASSAGE_PREFIX` variables
feed **Mnemosyne and TEI only**; they do **not** configure gbrain. gbrain's E5
`query: ` / `passage: ` prefixing is **model-gated** in the Josemar patch
(`patches/gbrain-inline-worker-gateway.patch`): the `isE5EmbeddingModel` /
`preprocessE5Input` helpers apply the prefixes exactly once for E5 models and
are a no-op for non-E5 models. Do not assume setting the overlay prefix vars
re-shapes gbrain's prefixing — they are independent config surfaces that must
be kept aligned to the same model tuple.

### Safe rollback

To roll back from embeddings to keyword-only:

1. **Run `josemar-gbrain disable-embeddings`.** This sets
   `search.mcp_keyword_only=true` first (so text queries fall back to
   keyword-only and image/cross-modal queries are rejected), then atomically
   writes the `embedding_disabled=true` sentinel into the file-plane config so
   `gbrain put`/`capture` stop embedding (`embed`/`import` refuse via
   `assertEmbeddingEnabled`). Vectors and the TEI service are preserved (not
   deleted); a future re-activation re-runs `enable-embeddings` (which clears
   the sentinel on migration success) and `embed-backfill` as needed.
2. **Then, optionally, remove the TEI overlay.** Only after `disable-embeddings`
   is confirmed should you remove `docker-compose.embeddings.yml` from
   `COMPOSE_FILE` and stop the `embeddings` service. Removing TEI first while
   gbrain still expects it can leave retrieval in a degraded state.

The stores are independent: rolling back gbrain embeddings does not require
re-indexing Mnemosyne and vice versa. The gbrain DB and its vectors remain; a
future re-activation re-runs `enable-embeddings` / `embed-backfill` as needed.

### No secrets

The embeddings overlay requires no secrets. The model is a public Hugging Face
model; the `embedding-model-cache` volume holds only downloaded public weights.
No `/shared`, Obsidian, credentials, or Hermes state mounts are added to the
`embeddings` service. See `docker-compose.embeddings.yml` and
`docs/memory-embeddings-evaluation.md`.

## User-Owned Schema Pack Workflow

The schema pack defines page types, link types, filing rules, and other
taxonomy for the brain. Custom schema packs are user-owned source files that
live in the private agent-state repo and are installed into gbrain's native
user-pack directory during operator activation. This deployment's active
schema marker is `josemar` (see "Pinned Values"); the source pack lives at
`gbrain/schema-packs/josemar/pack.yaml` in the agent-state repo.

### Source-First Approval Workflow

Schema editing is **never** done silently or from chat. The workflow is:

1. **Propose**: Josemar (or the user) proposes an exact diff to the source
   `pack.yaml` with impact analysis.
2. **Approve**: The user explicitly approves the change.
3. **Update**: The source `pack.yaml` is edited and committed to agent-state
   under `gbrain/schema-packs/josemar/pack.yaml`.
4. **Activate**: An operator runs `josemar-gbrain reindex` to validate the
   source pack, install it to `$GBRAIN_HOME/.gbrain/schema-packs/josemar/`,
   and run native schema sync. **No redeploy is required** — activation
   happens in the running deployment.

### Switching to the Custom Pack

1. Set `GBRAIN_SCHEMA_PACK=josemar` in `.env`.
2. Ensure the source pack exists at
   `$GBRAIN_SCHEMA_SOURCE_ROOT/josemar/pack.yaml` (default:
   `/opt/data/.gbrain/schema-packs/josemar/pack.yaml`, which maps to the
   agent-state `gbrain/schema-packs/josemar/` path).
3. Run `josemar-gbrain reindex`.

### Bundled Pack Fallback

If `GBRAIN_SCHEMA_PACK` is set to a bundled pack (`gbrain-base`,
`gbrain-base-v2`, `gbrain-recommended`), no source pack is required and
`schema sync --apply` is skipped. Switching back to a bundled pack is the same
workflow: set the env, run reindex.

## Troubleshooting

- **`gbrain` command not found** — Confirm the image was rebuilt with the new
  Dockerfile. The public safe adapter lives at `/usr/local/bin/gbrain`; the
  private native CLI lives at `/opt/josemar/libexec/gbrain-native` and must
  never be invoked as an agent command.
- **Non-zero exit from `gbrain search`** — Check the native CLI error message.
  Confirm the PGLite database at `$GBRAIN_HOME/.gbrain` is intact and that
  reindex has been run.
- **Non-zero exit from `gbrain put` or `gbrain capture`** —
  Check the native CLI
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
- **Embeddings warning from `gbrain doctor`** — Expected in the base
  (keyword-only) deploy. Text queries are keyword-only, image/cross-modal
  queries are rejected, and `put`/`capture` do not embed. Embeddings are opt-in
  via issue #65; see "Issue #65: Opt-in TEI E5 Semantic/Hybrid Retrieval" below.
