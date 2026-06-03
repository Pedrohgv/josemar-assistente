# Josemar Assistente

Self-hosted OpenClaw assistant infrastructure for running a private AI assistant with Telegram, Web UI access, git-backed agent state, an Obsidian vault, and optional queue-based local ML jobs.

This repository is the public/platform layer. Personal identity, memories, private workflows, and user-specific skills live in a separate private `agent-state` repository so this repo can evolve independently from each user's assistant state.

## What This Repo Provides

- **OpenClaw Gateway**: self-hosted agent gateway with password-protected Web UI.
- **Telegram channel**: allowlisted Telegram DM access, with `TELEGRAM_ENABLED=false` support for safe local testing.
- **Independent agent state**: private git-backed workspace for personality files, memory logs, cron jobs, avatars, and user-owned skills.
- **Two-scope skills model**: repo-owned platform skills in `skills-factory/`, user-owned skills in `agent-state/skills/`.
- **Obsidian vault infrastructure**: dedicated Docker volume mounted into OpenClaw, synchronized with Syncthing over a Tailscale sidecar.
- **Google Drive vault backups**: daily rotating backup slots via rclone.
- **Optional auxiliary ML service**: internal `aux-ml` container for FIFO, one-at-a-time long-running OCR jobs through llama.cpp.
- **Multi-provider LLM config**: Ollama Cloud, Z.AI/GLM, DeepSeek, and other OpenAI-compatible providers can be configured.
- **Security checks**: gitleaks and a custom PII guard in CI and optional pre-commit hooks.

Domain-specific behavior, such as Brazilian credit-card invoice extraction, belongs in a user's private state-repo skills unless it is explicitly added to `skills-factory/`. The public repo currently ships the infrastructure needed to support OCR and custom skills, not that personal extraction workflow itself.

## Architecture

```mermaid
flowchart LR
  User[User] --> Telegram[Telegram Bot]
  User --> WebUI[OpenClaw Web UI]

  Telegram --> Gateway[OpenClaw Gateway]
  WebUI --> Gateway

  Gateway --> Agent[Josemar Agent]
  Agent --> Models[LLM Providers<br/>Ollama Cloud / Z.AI / DeepSeek]
  Agent --> CoreSkills[Repo Core Skills<br/>/opt/josemar/skills]
  Agent --> StateSkills[User State Skills<br/>workspace/skills]
  Agent --> Vault[Obsidian Vault<br/>obsidian-vault volume]

  CoreSkills --> AuxML[aux-ml API<br/>optional]
  AuxML --> Llama[llama.cpp Router<br/>OCR models]

  Agent --> Workspace[OpenClaw Workspace<br/>openclaw-workspace volume]
  Workspace <--> StateRepo[Private Agent State Repo]
  Vault <--> Syncthing[Syncthing]
  Syncthing <--> Tailscale[Tailscale Sidecar]
  Vault --> Backup[rclone Backup]
  Backup --> GDrive[Google Drive Slots]
```

## State Separation

The main repository can stay public because user-specific assistant state is isolated in a private nested repo mounted at `agent-state/`.

```mermaid
flowchart TB
  PublicRepo[Public Platform Repo] --> Image[Docker Image]
  PublicRepo --> CoreSkills[skills-factory<br/>repo-owned skills]
  PublicRepo --> Config[config/openclaw.json]
  PublicRepo --> Compose[docker-compose.yml]

  PrivateRepo[Private Agent State Repo] --> Personality[SOUL.md / IDENTITY.md / USER.md]
  PrivateRepo --> Memory[memory/YYYY-MM-DD.md]
  PrivateRepo --> UserSkills[skills/*]
  PrivateRepo --> Cron[cron/jobs.json]
  PrivateRepo --> Avatars[avatars/*]

  Image --> Runtime[OpenClaw Runtime]
  CoreSkills --> Runtime
  Config --> Runtime
  Compose --> Runtime
  PrivateRepo <--> Workspace[Runtime Workspace Git Repo]
  Workspace --> Runtime
```

The workspace sync script only versions paths listed in `.sync-manifest`, uses the remote state repo as the blessed conflict winner, rotates memory logs, and can auto-commit/push state changes from the running assistant.

## Obsidian Vault Flow

```mermaid
flowchart LR
  OpenClaw[OpenClaw Container<br/>/root/.openclaw/obsidian] <--> Vault[(obsidian-vault volume)]
  Vault <--> Syncthing[Syncthing Container]
  Syncthing <--> Tailscale[Tailscale Sidecar<br/>private network]
  Tailscale <--> Devices[Laptop / Mobile Devices]
  Vault --> Backup[obsidian-backup Container]
  Backup --> RcloneConfig[(obsidian-rclone-config)]
  Backup --> SlotState[(obsidian-backup-state)]
  Backup --> Drive[Google Drive<br/>slot-1 ... slot-N]
```

The vault is not git-versioned. It persists in its own Docker volume, syncs through Syncthing, and is backed up by rotating rclone snapshots.

## Quick Start

### 1. Clone and Prepare State

```bash
git clone <this-repo-url> josemar-assistente
cd josemar-assistente
cp .env.example .env
```

Clone your private state repo into `agent-state/`:

```bash
git clone <your-private-agent-state-repo-url> agent-state
```

If you do not have a state repo yet, initialize from the template:

```bash
cp -r templates/agent-state-template/ agent-state
cd agent-state
git init
git add -A
git commit -m "Initial state"
cd ..
```

### 2. Configure `.env`

For production with Telegram and automatic state sync, set:

```bash
GATEWAY_AUTH_PASSWORD=your-secure-password
TELEGRAM_BOT_TOKEN=your-telegram-token
PRIMARY_TELEGRAM_ID=123456789
WORKSPACE_STATE_REPO=https://github.com/username/private-agent-state.git
WORKSPACE_REPO_TOKEN=your-github-pat
```

Set the model provider keys used by your configured primary and fallback models. The current default config uses `ollama/kimi-k2.6:cloud` as the primary model, with Z.AI, DeepSeek, and `ollama/glm-5.1:cloud` fallbacks:

```bash
OLLAMA_API_KEY=your-ollama-cloud-key
ZAI_API_KEY=your-zai-key
DEEPSEEK_API_KEY=your-deepseek-key
```

For local testing, disable Telegram so you do not conflict with a production bot using the same token:

```bash
TELEGRAM_ENABLED=false
```

For Web UI-only local testing, Telegram credentials are optional when Telegram is disabled. If you are testing without remote state sync, `WORKSPACE_STATE_REPO` and `WORKSPACE_REPO_TOKEN` can also be left empty.

See `.env.example` for the full variable list.

### 3. Start Locally

```bash
docker compose build
docker compose up -d
docker compose logs -f openclaw
```

Access the Web UI at:

```text
http://operator:YOUR_GATEWAY_AUTH_PASSWORD@localhost:18789/
```

If the browser shows a pairing-required message, approve the device:

```bash
docker compose exec openclaw openclaw devices list
docker compose exec openclaw openclaw devices approve <requestId>
```

### 4. Optional Aux-ML

Enable the auxiliary ML container locally only when needed:

```bash
# In .env
AUX_ML_ENABLED=true
COMPOSE_PROFILES=aux-ml

docker compose up -d --build
```

The deploy workflow treats unset `AUX_ML_ENABLED` as enabled and writes `COMPOSE_PROFILES=aux-ml`. Set `AUX_ML_ENABLED=false` explicitly in deployment variables if you do not want the auxiliary service.

## Repository Layout

```text
josemar-assistente/
├── agent-state/                  # Nested private git repo for assistant state
├── aux-ml/                       # Optional FastAPI + llama.cpp queue service
├── config/                       # OpenClaw JSON5 configuration
├── credentials/                  # Local credentials, not versioned
├── docs/                         # Operations runbooks
├── scripts/                      # Workspace sync, backup, privacy tooling
├── skills-factory/               # Repo-owned core skills shipped in image
├── templates/agent-state-template/ # Starter private state repo template
├── tests/                        # Python unit tests
├── .github/workflows/            # Deploy, stop, runner test, privacy scan
├── docker-compose.yml            # Service topology and persistent volumes
├── docker-entrypoint.sh          # Runtime config/state bootstrap
└── Dockerfile                    # Custom OpenClaw image
```

## Runtime Services and Volumes

| Service | Purpose |
| --- | --- |
| `openclaw` | Main OpenClaw gateway, Telegram channel, Web UI, agent runtime. |
| `aux-ml` | Optional internal queue API for long-running OCR jobs. |
| `tailscale` | Private-network sidecar for Syncthing connectivity. |
| `syncthing` | Syncs the Obsidian vault to trusted devices. |
| `obsidian-backup` | Runs daily rclone backups into rotating Google Drive slots. |

| Volume | Purpose |
| --- | --- |
| `openclaw-workspace` | OpenClaw runtime state, sessions, devices, workspace git repo. |
| `obsidian-vault` | Obsidian notes and attachments, not git-versioned. |
| `syncthing-config` | Syncthing identity and folder/device config. |
| `tailscale-state` | Tailscale node identity and login state. |
| `obsidian-rclone-config` | rclone config used by vault backup container. |
| `obsidian-backup-state` | Rotating backup slot pointer. |

## Skills

Skills are intentionally split by ownership:

| Scope | Location | Owner | Use |
| --- | --- | --- | --- |
| Core platform skills | `skills-factory/` copied to `/opt/josemar/skills` | This repo | Stable runtime capabilities shared by all deployments. |
| User state skills | `agent-state/skills/` synced to workspace | Private state repo | Personal workflows, user-specific automations, domain-specific processors. |

Current repo-shipped skills:

- `vault-gateway`: entrypoint for vault routing and operations.
- `aux-ml`: skill interface for queue-based auxiliary ML jobs.
- `workspace-sync`: skill interface for workspace git sync, status, commit, and push flows.

Keep personal or sensitive workflows in `agent-state/skills/`, not in this repository.
The workspace sync script also protects `skills/vault-gateway` from private state-repo overrides so the repo-shipped vault gateway remains the canonical platform entrypoint.

## Agent State Sync

```mermaid
sequenceDiagram
  participant Entrypoint as docker-entrypoint.sh
  participant Workspace as Runtime Workspace
  participant Remote as Private State Repo
  participant OpenClaw as OpenClaw

  Entrypoint->>Workspace: Ensure workspace git repo exists
  Entrypoint->>Workspace: Commit local tracked changes
  Entrypoint->>Remote: Fetch configured branch
  Remote-->>Workspace: Pull remote state
  Entrypoint->>Workspace: Resolve conflicts with remote winning
  Entrypoint->>Workspace: Stage only .sync-manifest paths
  Workspace->>Remote: Push resulting state
  Entrypoint->>OpenClaw: Start gateway
```

The sync logic also removes private `skills/vault-gateway` overrides before staging so the repo-owned gateway skill remains authoritative.

Important state-sync variables:

- `WORKSPACE_STATE_REPO`: private git repo URL.
- `WORKSPACE_REPO_TOKEN`: PAT with access to that private repo.
- `WORKSPACE_GIT_BRANCH`: branch to sync, default `main`.
- `WORKSPACE_SYNC_ON_START`: run sync during container startup.
- `WORKSPACE_SYNC_INTERVAL`: periodic sync interval in minutes; `0` disables periodic sync.
- `WORKSPACE_MEMORY_DAYS`: memory daily log retention window.
- `WORKSPACE_GIT_USER_EMAIL` and `WORKSPACE_GIT_USER_NAME`: auto-commit identity.

## Obsidian Sync and Backup

OpenClaw mounts the vault at `/root/.openclaw/obsidian`. Syncthing mounts the same Docker volume and syncs it to trusted devices through the `tailscale` sidecar. The backup container mounts the vault read-only and writes rotating snapshots to Google Drive.

Key settings:

- `TS_AUTHKEY`: optional Tailscale auth key for unattended sidecar login.
- `SYNCTHING_GUI_BIND_IP`: defaults to `127.0.0.1`; avoid exposing the GUI publicly.
- `OBSIDIAN_BACKUP_TIME`: daily backup time, default `03:15`.
- `OBSIDIAN_BACKUP_SLOTS`: number of rotating Google Drive slots, default `5`.
- `OBSIDIAN_GDRIVE_REMOTE` and `OBSIDIAN_GDRIVE_PATH`: rclone destination.

Full runbook: `docs/obsidian-operations.md`.

## Auxiliary ML Service

`aux-ml` is optional and internal-only by default. It exposes a FastAPI queue on `http://aux-ml:8091` for jobs that can take minutes, currently focused on OCR.

```mermaid
sequenceDiagram
  participant Skill as aux-ml Skill
  participant API as aux-ml API
  participant Queue as FIFO Queue
  participant Worker as Single Worker
  participant Router as llama.cpp Router

  Skill->>API: POST /jobs or /run
  API->>Queue: Enqueue job
  Worker->>Queue: Pop next job
  Worker->>Router: Load required model
  Worker->>Router: Run OCR request
  Router-->>Worker: OCR result
  Worker-->>API: Store terminal job result
  Skill->>API: GET /jobs/{job_id}
```

Useful endpoints:

- `GET /health`: service and memory status.
- `GET /queue`: queued/running jobs and loaded model.
- `POST /jobs`: submit asynchronous job.
- `GET /jobs/{job_id}`: fetch job status/result.
- `POST /run`: submit and wait for terminal state.

Full runbook: `docs/aux-ml.md`.

## Deployment

Production deployment is handled by GitHub Actions on a self-hosted runner.

Workflows:

- `deploy-to-home-server.yml`: builds, writes `.env` from secrets/variables, loads rclone config, starts services, and checks service status.
- `stop-service.yml`: stops services without deleting data.
- `privacy-scan.yml`: runs gitleaks and PII checks on changes.
- `test-workflow.yml`: verifies self-hosted runner setup.

The deploy workflow has a `fresh_start` option. It removes only the `openclaw-workspace` volume after a safety countdown; it does not remove Obsidian vault, Syncthing, Tailscale, rclone config, or backup state volumes.

Workflow details: `.github/workflows/AGENTS.md`.

## Configuration Notes

- Source config lives in `config/openclaw.json` and uses JSON5 syntax.
- The entrypoint copies source config into the runtime volume on startup and aligns `openclaw.json`, `.last-good`, `.bak`, and `.bak.*` snapshots.
- `TELEGRAM_ENABLED` is normalized to a JSON boolean during startup.
- Agent prompts and personality are not in `openclaw.json`; they live in private state-repo workspace files such as `SOUL.md`, `IDENTITY.md`, `USER.md`, and `MEMORY.md`.

Config recovery runbook: `docs/config-recovery.md`.

## Development

Run unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Run a scoped test file:

```bash
python3 -m unittest tests.vault_gateway.test_gateway_contract -v
```

Set up optional pre-commit hooks:

```bash
./scripts/setup-pre-commit.sh
```

Manual privacy checks:

```bash
python3 scripts/pii_guard.py --staged --fail-on medium
```

## Credentials

Credentials go under `credentials/<service>/` and are mounted read-only into the OpenClaw container. Do not commit real credentials.

Common examples:

- `credentials/rclone/rclone.conf`: Google Drive backup configuration, usually loaded into the `obsidian-rclone-config` Docker volume.
- `credentials/gogcli/`: optional gogcli OAuth/keyring material.

See `credentials/README.md` for setup details.

## Documentation Index

- `AGENTS.md`: root project architecture and assistant guidance.
- `config/AGENTS.md`: OpenClaw configuration reference.
- `credentials/README.md`: credential setup and storage rules.
- `docs/aux-ml.md`: auxiliary ML API, queue, model lifecycle, and OCR operations.
- `docs/config-recovery.md`: OpenClaw config snapshot recovery behavior.
- `docs/obsidian-operations.md`: Syncthing, Tailscale, rclone backup, and restore runbook.
- `.github/workflows/AGENTS.md`: deployment, stop, privacy scan, and runner workflow documentation.
- `templates/agent-state-template/README.md`: starting point for a private state repo.

## License

MIT
