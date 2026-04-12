# Josemar Assistente - OpenClaw Bot

A self-hosted OpenClaw bot running in Docker with Telegram integration and PDF extraction capabilities.

## Features

- **OpenClaw Gateway**: Self-hosted AI agent gateway
- **Telegram Integration**: Native Telegram bot support
- **PDF Extraction**: Process Brazilian credit card invoice PDFs
- **Multi-Provider LLM Support**: GLM, DeepSeek, and OpenAI-compatible APIs
- **Brazilian Portuguese**: Native language interaction
- **Docker Deployment**: Containerized with persistent workspace storage
- **Git-Backed Agent State**: Workspace files versioned in a private git repo
- **Obsidian Vault Sync**: Syncthing-based sync between server and laptop
- **Google Drive Backups**: Daily rotating Obsidian vault backups via rclone
- **Auxiliary ML Batch Container**: Optional llama.cpp service with FIFO queue for long-running OCR/transcription tasks

## Prerequisites

- Docker and Docker Compose
- Z.AI API key (for GLM models - e.g., GLM-5, GLM-4.7)
- Telegram Bot Token (from @BotFather)
- DeepSeek API key (optional, for alternative LLM)
- A **private** GitHub repo for agent state versioning

## Quick Start

### 1. Create Agent State Repo

Create a private GitHub repo for agent state (this stores personality, skills, memory):

```bash
# Use the template in templates/agent-state-template/
# See templates/agent-state-template/README.md for instructions
```

### 2. Clone and Configure

```bash
cd repos/josemar-assistente
git clone <your-private-repo-url> agent-state
cp .env.example .env
# Edit .env with your API keys and agent state repo URL
```

If you do not have a private state repo yet, initialize from template:

```bash
cp -r templates/agent-state-template/ agent-state
cd agent-state && git init && git add -A && git commit -m "Initial state"
```

### 3. Build and Run

```bash
docker compose build
docker compose up -d
```

**Note**: Use `docker compose` (with space) for Docker Compose V2, or `docker-compose` (with hyphen) for V1.

### 4. Check Logs

```bash
docker compose logs -f
```

### 5. Interact with Bot

- Start a conversation with your Telegram bot
- Send a PDF credit card invoice for processing
- Ask questions in Brazilian Portuguese

## Configuration

### Environment Variables

Create a `.env` file with:

```bash
# LLM Provider
ZAI_API_KEY=your_zai_api_key_here
DEEPSEEK_API_KEY=your_deepseek_api_key_here

# Telegram
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_ENABLED=true
PEDRO_TELEGRAM_ID=190731460

# Web UI
GATEWAY_AUTH_PASSWORD=your-secure-password-here

# Agent State Repo
WORKSPACE_STATE_REPO=https://github.com/username/josemar-agent-state.git
WORKSPACE_REPO_TOKEN=your_github_pat_here

# Sync Configuration
WORKSPACE_SYNC_ON_START=true
WORKSPACE_SYNC_INTERVAL=60
WORKSPACE_MEMORY_DAYS=30

# Auxiliary ML service (optional)
AUX_ML_ENABLED=false
COMPOSE_PROFILES=
AUX_ML_GLM_OCR_URL=
AUX_ML_GLM_OCR_SHA256=
AUX_ML_GLM_OCR_MMPROJ_URL=
AUX_ML_GLM_OCR_MMPROJ_SHA256=
AUX_ML_URL=http://aux-ml:8091
AUX_ML_MEMORY_LIMIT=8192m
AUX_ML_MEMORY_LIMIT_MB=8192
AUX_ML_MAX_QUEUE=50
AUX_ML_JOB_TIMEOUT_SECONDS=1800
AUX_ML_POLL_INTERVAL_SECONDS=2
AUX_ML_LLAMACPP_TIMEOUT_SECONDS=1800
AUX_ML_ALLOWED_INPUT_DIRS=/root/.openclaw/workspace
AUX_ML_ENFORCE_MEMORY_LIMIT=true
AUX_ML_OCR_MAX_PAGES=50

# Obsidian Sync/Backup
LAN_BIND_IP=192.168.1.50
SYNCTHING_GUI_BIND_IP=127.0.0.1
TZ=America/Sao_Paulo
```

See `.env.example` for the complete list.

### OpenClaw Configuration

The main configuration is in `config/openclaw.json` (JSON5 format). See `config/AGENTS.md` for complete reference.

### Auxiliary ML Service (Optional)

The auxiliary ML service runs in a dedicated container and is designed for queue-based, long-running jobs (minutes are acceptable). It currently starts with OCR (`glm-ocr`) and keeps a modular model registry for future additions.

- **Single worker**: exactly one job runs at a time
- **FIFO queue**: requests are processed in order
- **Model lifecycle**: load on demand, unload when the next queued job is a different model (or queue is empty)
- **Internal only**: no host port exposure by default (`http://aux-ml:8091`)

To enable locally:

```bash
# In .env
AUX_ML_ENABLED=true
COMPOSE_PROFILES=aux-ml

docker compose up -d --build
```

Place model files in `aux-ml/models/` before building (see `aux-ml/models/README.md`).
If files are not present locally, build auto-downloads default Q8 model + mmproj from Hugging Face.
You can override URLs/checksums with `AUX_ML_GLM_OCR_URL`, `AUX_ML_GLM_OCR_SHA256`, `AUX_ML_GLM_OCR_MMPROJ_URL`, and `AUX_ML_GLM_OCR_MMPROJ_SHA256`.

## Skills

All skills live in `agent-state/skills/` and are versioned in the agent state git repo.

### Current Skills

- **finance-assistant**: Complete financial tracking - extraction, classification, Google Sheets
- **aux-ml**: Queue-based auxiliary ML jobs (OCR now, additional tasks later)
- **gogcli-tables**: Google Workspace CLI with Sheets Table manipulation
- **workspace-sync**: Manage workspace git operations - sync, commit, push, pull, and GitHub CLI

### Adding Skills

1. Create skill in `agent-state/skills/<skill-name>/`
2. Add `SKILL.md` with YAML frontmatter
3. Add executable script
4. No config update needed (skills are auto-discovered from `agent-state/skills/`)
5. Changes sync automatically via git

See `agent-state/skills/AGENTS.md` for detailed skill development guide.

## Credential Management

Credentials are stored in `credentials/<service>/` and mounted into the container:

```
credentials/
├── README.md
└── gogcli/
    ├── README.md
    └── josemar-assistente-openclaw-credentials.json
```

See `credentials/README.md` for setup instructions.

## Project Structure

```
josemar-assistente/
├── agent-state/                    # Nested git repo: agent workspace (private repo)
│   ├── .sync-manifest              # Files to version
│   ├── .gitignore                  # Security ignore list
│   └── skills/                     # Unified skills
├── config/                         # OpenClaw configuration
│   ├── AGENTS.md                   # Config reference
│   └── openclaw.json               # Main config
├── credentials/                    # Service credentials (NOT versioned)
│   └── README.md                   # Setup guide
├── scripts/
│   ├── workspace-sync.sh           # Git sync logic
│   ├── obsidian-backup.sh          # Obsidian backup and slot rotation
│   └── obsidian-backup-daemon.sh   # Daily backup scheduler
├── aux-ml/                         # Auxiliary llama.cpp batch processing service
├── docs/
│   ├── obsidian-operations.md      # Syncthing/backup setup and operations runbook
│   └── aux-ml.md                   # Auxiliary ML operations runbook
├── templates/
│   └── agent-state-template/       # Template for new agent state repos
├── .github/workflows/              # CI/CD
│   └── deploy-to-home-server.yml   # Deployment workflow
├── Dockerfile                      # Custom OpenClaw image
├── docker-compose.yml              # Deployment config
├── docker-entrypoint.sh            # Container startup
└── .env.example                    # Environment variables template
```

## Deployment

Deployment is handled via GitHub Actions:

1. Set required secrets (see `.github/workflows/AGENTS.md`)
2. Set required variables: `WORKSPACE_STATE_REPO` and `LAN_BIND_IP` (plus optional `AUX_ML_*` variables if enabling aux-ml)
3. Run the `deploy-to-home-server` workflow

**Fresh Start:** The workflow has a `fresh_start` option that erases ALL data (with a safety countdown).

## Development

### Building the Image

```bash
docker compose build
```

### Viewing Logs

```bash
docker compose logs -f openclaw
```

### Local Testing

Disable Telegram to avoid conflicts with production:

```bash
# In .env
TELEGRAM_ENABLED=false

docker compose up -d
```

If testing the auxiliary ML service, also set `COMPOSE_PROFILES=aux-ml` in `.env`.

Access Web UI at `http://operator:YOUR_PASSWORD@localhost:18789/`

## Architecture

### Docker Deployment

- **Image**: Based on `ghcr.io/openclaw/openclaw:latest` with Python, pymupdf, git, gogcli
- **Auxiliary ML**: Optional dedicated `aux-ml` container based on `llama.cpp` server for queued batch inference
- **Volumes**:
  - `openclaw-workspace` for OpenClaw runtime state
  - `obsidian-vault` for Obsidian notes and attachments
  - `syncthing-config` for Syncthing identity and config
  - `obsidian-backup-state` for backup slot pointer
- **Entrypoint**: Copies config, mounts credentials, runs git sync, starts OpenClaw

### Agent State Sync

- **On start**: Commits local changes, fetches remote, merges (remote wins conflicts), pushes
- **Periodic**: Auto-commits and pushes at configurable interval
- **Security**: Only files in `.sync-manifest` are versioned
- **Memory rotation**: Logs older than N days are automatically removed

### Skills System

Single unified skill directory (`agent-state/skills/`):
- All skills versioned in the agent state git repo
- Modified by agent at runtime, changes sync back automatically
- No "repo" vs "runtime" distinction

## Documentation

- **AGENTS.md**: Root project documentation
- **config/AGENTS.md**: Configuration reference
- **agent-state/skills/AGENTS.md**: Skills development guide
- **credentials/README.md**: Credential management
- **.github/workflows/AGENTS.md**: CI/CD documentation
- **docs/obsidian-operations.md**: Obsidian sync/backup operations runbook
- **docs/aux-ml.md**: Auxiliary ML queue/model lifecycle operations
- **templates/agent-state-template/README.md**: Agent state setup

## License

MIT
