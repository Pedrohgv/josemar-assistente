# AGENTS.md

Semantic gbrain embeddings are initialized by a manual backfill. The daily
no-agent refresh follows that backfill; Josemar may invoke
`josemar-gbrain refresh-embeddings` only after an explicit user request.

Purpose: Root guidance for AI assistants working with the Josemar Assistente project.

## Prompt Language Policy

- Author all LLM-facing prompt and instruction files in English, even when the assistant is expected to interact with users in another language.
- This applies to `AGENTS.md`, `SKILL.md`, playbooks, starter-state templates, harness instructions, and prompt examples committed to this repo.
- Runtime interactions may still follow the user's preferred language; keep the source prompt files language-neutral by writing them in English.

## Project Overview

Josemar Assistente is a self-hosted AI assistant built on Hermes, running in Docker with Telegram integration.

Core architecture:
- Hermes gateway runtime (dashboard/API/Telegram/cron/skills)
- Two-scope skills (repo-owned `skills-factory/` + user-owned `agent-state/skills/`)
- Git-backed state sync (`scripts/workspace-sync.sh`)
- Obsidian vault operations (native gbrain + bounded TaskNotes MCP + Syncthing + backup)
- Optional `aux-ml` queue service for long OCR jobs

## Directory Structure

```text
josemar-assistente/
├── agent-state/            # Nested private repo: user state (memory/persona/skills)
├── credentials/            # Service credentials (not versioned)
├── docs/                   # Operations runbooks (gbrain, obsidian, aux-ml)
├── scripts/                # Workspace sync, backup, privacy tooling, gbrain wrapper
├── aux-ml/                 # Auxiliary ML service
├── skills-factory/         # Repo-owned core skills
├── templates/              # Bootstrap template for private state repo
├── tests/                  # Python unit and contract tests
├── .github/workflows/      # CI/CD automation
├── docker-compose.yml      # Runtime stack
├── Dockerfile.hermes       # Hermes runtime image
└── .env.example            # Environment template
```

## Runtime Storage

- `hermes-data`: Hermes runtime state plus private state git worktree (`/opt/data`). Includes gbrain state at `/opt/data/.gbrain` (PGLite database, config, cache).
- `aux-ml-shared`: explicit file handoff area for aux-ml (`/shared` in both Hermes and aux-ml)
- `obsidian-vault`: notes/attachments plus local-only Git history already required by native gbrain sync; there is no remote consumer, `.git/` is excluded from Syncthing, and the history is unrelated to agent-state sync
- `syncthing-config`, `tailscale-state`, `obsidian-rclone-config`, `vault-recovery-staging`, `vault-recovery-uploader-state`, `vault-recovery-recovery`

Docker named volumes default to `root:root 0755`, but the Hermes gateway runs as `HERMES_UID` (default 10000). `docker-hermes-init.sh` chowns an explicit allowlist of Hermes-writable volumes (`HERMES_HOME` and `/shared`) at startup and verifies write access. When adding a new Hermes-writable volume, add its mount path to `HERMES_WRITABLE_VOLUMES` in `docker-hermes-init.sh`. Do not chown bind mounts, read-only mounts, or cross-service volumes (e.g. `obsidian-vault`).

## Local Development

Use local Docker compose in this repo by default.

When starting the Hermes service locally for validation, explicitly disable Telegram
so the local container cannot contend with the production Telegram bot deployment.
Set `TELEGRAM_BOT_TOKEN=`, `PRIMARY_TELEGRAM_ID=`, and the `HERMES_TELEGRAM_*`
/ `HERMES_GATEWAY_ALLOWED_USERS` variants to empty values for the local run.

```bash
docker compose up -d
docker compose logs -f hermes
```

For optional aux-ml:

```bash
COMPOSE_PROFILES=aux-ml docker compose up -d
```

## Git Workflow

- Create feature branches for non-trivial work.
- Do not commit directly to `main` unless explicitly requested.
- Keep commits focused and scoped.

## Agent State Repo Rules

`agent-state/` is a nested private repo and source of truth for user-owned state.

- Personality/context files live there using Hermes-native paths (`SOUL.md`, `memories/MEMORY.md`, `memories/USER.md`, `AGENTS.md`, etc.).
- User-owned skills live there (`agent-state/skills/*`).
- Only paths in `.sync-manifest` are auto-versioned by sync.

When modifying user state, commit/push inside `agent-state` repo when requested.

## Skills Ownership

- Repo-owned skills: `skills-factory/*` -> copied to `/opt/josemar/skills`.
- User-owned skills: `agent-state/skills/*` -> synced into `/opt/data/skills`.
- Keep native gbrain (`skills-factory/gbrain` + `scripts/josemar-gbrain`) as the canonical vault interface. The bounded `tasknotes` MCP is the only specialized exception and still uses short-lived native gbrain commands as the sole task writer. `josemar-gbrain` provides operator-only `reindex` activation and lightweight `refresh` for periodic manual Obsidian edit reconciliation. Agent-facing vault access (chat, skills, external general vault actions) runs through the public `gbrain` command, which is safe by default (issue #110): it transparently provides the safe-adapter behavior — `hermes` runtime user, shared lock. The internal private native gbrain path is never presented as an agent command; it is limited to the locked operator/cron paths (`josemar-gbrain` wrapper and both refresh crons) and the TaskNotes MCP implementation, which cooperate on the same lock and do NOT use the public wrapper (see the safe-access non-negotiables below).
- Automatic skill creation, patching, and curation are intentionally disabled (`skills.creation_nudge_interval: 0`, `skills.write_approval: true`, `curator.enabled: false`). Keep these guards until issue #69's re-enable criteria pass against a pinned Hermes release.
- Until issue #69 is resolved, intentional user-skill authoring must be explicit and use the flat `/opt/data/skills/<name>/SKILL.md` layout so workspace sync can version it. Never route runtime writes into `/opt/josemar/skills`.
- Per-profile skill enable/disable choices are user state under `hermes/skill-toggles/`; never version the full Hermes `config.yaml`.

## gbrain Safe-Access Non-Negotiables (issue #110)

Applies to every assistant, cron, and skill that touches gbrain state. The
operator runbook is `docs/gbrain-operations.md` → "Issue #110: Safe gbrain
Adapter"; TaskNotes specifics are in `docs/tasknotes-mcp.md`.

1. **No root execution.** Never run gbrain, `josemar-gbrain`, or vault Git
   operations as root. Always run as the Hermes runtime user (e.g.
   `docker compose exec hermes su -s /bin/sh hermes -c '...'`). Runtime gbrain
   state under `/opt/data/.gbrain` belongs to that user.
2. **Public `gbrain` is the safe agent-facing command.** ALL chat, skill, and
   external general vault actions use the public `gbrain` command, which
   transparently provides the issue #110 safe-adapter behavior (runs as the
   `hermes` runtime user under the shared lock). `gbrain-chat-run` is a
   temporary compatibility alias for that behavior and is not recommended in
   new instructions. The internal private native gbrain path
   (`/opt/josemar/libexec/gbrain-native`; used by the `josemar-gbrain`
   operator wrapper, both refresh crons, and the TaskNotes MCP) must never be
   presented as an agent command; those paths cooperate on the same lock
   (rules 4–6) and must avoid nesting. The wrapper prevents accidental,
   prompt-driven, and cooperative-concurrency misuse — it is NOT a security
   boundary against a compromised same-UID container/shell (defense in depth,
   not a complete security boundary); do not overstate protection.
3. **No concurrent PGLite opens.** The gbrain database is single-writer PGLite.
   Never open or mutate it from two processes at once.
4. **Cooperative flock.** The global lock at `/opt/data/.locks/tasknotes.lock`
   serializes cooperative access today: TaskNotes transactions, both refresh
   crons, backfills, and every other gbrain-touching path cooperate on it.
 5. **Pause all owned jobs for maintenance windows.** For recovery, reindex/rebuild,
    migrations, vault swaps, and unadapted/third-party diagnostics, the
    operator pauses ALL THREE owned jobs: `gbrain-refresh`,
    `gbrain-embedding-refresh`, AND the `vault-recovery-export` cron (a
    lock-held export would repopulate state inside the window), plus stops
    Hermes and server Syncthing before any destructive restore/install (the
    full disaster-recovery drill asserts this exact ordering).
    Routine adapted access does NOT require pausing the jobs.
6. **No nested wrapper usage in TaskNotes.** TaskNotes remains a bounded MCP
   adapter on short-lived native gbrain commands and is the sole task-file
   writer. It retains its transaction-level global lock and internal native
   invocation; it must never route through the public `gbrain` wrapper's lock
   path internally, nor be invoked from it. Task mutations go through the
   `task_*` MCP tools only.

### Skill Organization: SKILL.md vs. references/

**Policy: main `SKILL.md` is core, `references/` is deep-dive.** All skills (repo-owned and user-owned) should follow this split:

- **`SKILL.md`** — short, core content. Always loaded when the skill is active. Contains: the skill's purpose, core commands/operations, critical rules, the most-used reference paths, and a pointer to the references/ directory for full details. Target: under ~150 lines so it fits comfortably in always-loaded context.
- **`references/<topic>.md`** — detailed, rarely-needed content. Invisible to the skill system — loaded on demand via `skill_view("<skill>", file_path="references/<topic>.md")`. Contains: full schemas, taxonomies, edge cases, command output structures, deep-dive material that would bloat the main skill if always loaded.

This pattern keeps the always-loaded skill context small while making all information reachable when needed. The umbrella skill should state "for X details, load the reference" so the agent knows what's available.

Examples in this repo:
- `skills-factory/gbrain/SKILL.md` (compact) + `references/chronicle.md` (full event schema, kind taxonomy, ontology model)
- `agent-state/skills/client-workflows/SKILL.md` (umbrella router) + `references/client-transcription.md` (detailed workflow)

When adding or editing a skill, if a section exceeds ~30 lines of detail, consider moving it to `references/<topic>.md` and replacing it in the main skill with a brief summary + `skill_view` pointer.

## Security Rules

1. Never commit secrets.
2. Keep credentials under `credentials/<service>/`.
3. Keep `agent-state` private.
4. Respect `.sync-manifest` boundaries.
5. Run staged PII checks before commit when requested.

## Testing

- New and changed behavior must include new or updated tests, and relevant tests should pass during development cycles before work is considered complete. If a change is not practically testable, surface that limitation to the user before proceeding.
- Do not invoke bare system `python3` for test runs. Use the repo's supported test entrypoint (`make test` / `make verify`) or the repo virtualenv interpreter (`venv/bin/python3`, the interpreter the Makefile's `PYTHON` prefers when present), which includes the `requirements-test.txt` dependencies (e.g. httpx). Bare system `python3` may lack those dependencies.

```bash
make test
venv/bin/python3 -m unittest discover -s tests -v
venv/bin/python3 -m unittest tests.gbrain.test_gbrain_wrapper_contract -v
```

## Key References

- `README.md` - top-level runtime and operations guide
- `.github/workflows/AGENTS.md` - deploy/stop/privacy workflow documentation
- `credentials/README.md` - credential setup
- `docs/aux-ml.md` - aux-ml operations
- `docs/gbrain-operations.md` - gbrain activation, reindex, vault swap, and schema workflow
- `docs/tasknotes-mcp.md` - TaskNotes MCP prerequisites, profile gate, locking, and recovery
- `docs/vault-recovery-operations.md` - vault-recovery export (default-on): daily local staged generations, doctor preflight, convergence semantics, portability proof; encrypted upload/recovery/install lane (DEFAULT deployment composition, 14 committed remote generations, fail-closed deploy when the crypt remote is missing); Phase-3 migration sequence and the full Docker-gated disaster-recovery drill
- `docs/obsidian-operations.md` - Obsidian sync/backup runbook
