# AGENTS.md

Purpose: Root guidance for AI assistants working with the Josemar Assistente project.

## Project Overview

Josemar Assistente is a self-hosted AI assistant built on Hermes, running in Docker with Telegram integration.

Core architecture:
- Hermes gateway runtime (dashboard/API/Telegram/cron/skills)
- Two-scope skills (repo-owned `skills-factory/` + user-owned `agent-state/skills/`)
- Git-backed state sync (`scripts/workspace-sync.sh`)
- Obsidian vault operations (`vault-gateway` + Syncthing + backup)
- Optional `aux-ml` queue service for long OCR jobs

## Directory Structure

```text
josemar-assistente/
├── agent-state/            # Nested private repo: user state (memory/persona/skills)
├── credentials/            # Service credentials (not versioned)
├── scripts/                # Workspace sync, backup, privacy tooling
├── aux-ml/                 # Auxiliary ML service
├── skills-factory/         # Repo-owned core skills
├── templates/              # Bootstrap template for private state repo
├── .github/workflows/      # CI/CD automation
├── docker-compose.yml      # Runtime stack
├── Dockerfile.hermes       # Hermes runtime image
└── .env.example            # Environment template
```

## Runtime Storage

- `hermes-data`: Hermes native state (`/opt/data`)
- `hermes-workspace`: git-synced workspace (`/opt/data/workspace`)
- `obsidian-vault`: notes/attachments (not git-versioned)
- `syncthing-config`, `tailscale-state`, `obsidian-rclone-config`, `obsidian-backup-state`

## Local Development

Use local Docker compose in this repo by default.

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

- Personality/context files live there (`SOUL.md`, `MEMORY.md`, `USER.md`, `AGENTS.md`, etc.).
- User-owned skills live there (`agent-state/skills/*`).
- Only paths in `.sync-manifest` are auto-versioned by sync.

When modifying user state, commit/push inside `agent-state` repo when requested.

## Skills Ownership

- Repo-owned skills: `skills-factory/*` -> copied to `/opt/josemar/skills`.
- User-owned skills: `agent-state/skills/*` -> synced into workspace.
- Keep `vault-gateway` as the canonical vault mutation entrypoint.

## Security Rules

1. Never commit secrets.
2. Keep credentials under `credentials/<service>/`.
3. Keep `agent-state` private.
4. Respect `.sync-manifest` boundaries.
5. Run staged PII checks before commit when requested.

## Testing

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest tests.vault_gateway.test_gateway_contract -v
```

## Key References

- `README.md` - top-level runtime and operations guide
- `.github/workflows/AGENTS.md` - deploy/stop/privacy workflow documentation
- `credentials/README.md` - credential setup
- `docs/aux-ml.md` - aux-ml operations
- `docs/obsidian-operations.md` - Obsidian sync/backup runbook
