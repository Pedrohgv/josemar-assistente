# AGENTS.md

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
- `syncthing-config`, `tailscale-state`, `obsidian-rclone-config`, `obsidian-backup-state`

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
- Keep native gbrain (`skills-factory/gbrain` + `scripts/josemar-gbrain`) as the canonical vault interface. Josemar uses the pinned `gbrain` CLI directly for general vault work; the bounded `tasknotes` MCP is the only specialized exception and still uses short-lived native gbrain commands as the sole task writer. `josemar-gbrain` provides operator-only `reindex` activation and lightweight `refresh` for periodic manual Obsidian edit reconciliation.
- Automatic skill creation, patching, and curation are intentionally disabled (`skills.creation_nudge_interval: 0`, `skills.write_approval: true`, `curator.enabled: false`). Keep these guards until issue #69's re-enable criteria pass against a pinned Hermes release.
- Until issue #69 is resolved, intentional user-skill authoring must be explicit and use the flat `/opt/data/skills/<name>/SKILL.md` layout so workspace sync can version it. Never route runtime writes into `/opt/josemar/skills`.
- Per-profile skill enable/disable choices are user state under `hermes/skill-toggles/`; never version the full Hermes `config.yaml`.

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

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest tests.gbrain.test_gbrain_wrapper_contract -v
```

## Key References

- `README.md` - top-level runtime and operations guide
- `.github/workflows/AGENTS.md` - deploy/stop/privacy workflow documentation
- `credentials/README.md` - credential setup
- `docs/aux-ml.md` - aux-ml operations
- `docs/gbrain-operations.md` - gbrain activation, reindex, vault swap, and schema workflow
- `docs/tasknotes-mcp.md` - TaskNotes MCP prerequisites, profile gate, locking, and recovery
- `docs/obsidian-operations.md` - Obsidian sync/backup runbook
