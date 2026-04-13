# AGENTS.md

**Purpose:** Root guidance for AI assistants working with the Josemar Assistente project. This is your entry point - for detailed implementation, navigate to the relevant subdirectory.

---

## Project Overview

Josemar Assistente is a self-hosted AI assistant bot built on OpenClaw, running in Docker with Telegram integration. It specializes in processing Brazilian credit card invoice PDFs and provides intelligent assistance in Brazilian Portuguese.

**Core Architecture:**
- **OpenClaw Gateway** - AI agent orchestration (multi-provider LLM support)
- **Telegram Channel** - Bot interface for user interaction
- **Unified Skills System** - Extensible tools for PDF processing and other tasks
- **Auxiliary ML Container** - Optional queue-based `llama.cpp` service for long-running OCR/transcription jobs
- **Git-Backed Agent State** - Workspace files versioned in a private git repo
- **Obsidian Vault Infrastructure** - Dedicated vault volume synced with Syncthing
- **Google Drive Vault Backups** - Daily rotating backup slots managed by rclone
- **Docker Deployment** - Containerized with persistent workspace storage

**Key Technologies:**
- **OpenClaw** - Self-hosted AI agent gateway (supports multiple LLM providers: GLM, DeepSeek, Ollama Cloud, OpenAI-compatible APIs)
- **Docker & Docker Compose** - Containerized deployment
- **Python 3** - Custom skills and PDF processing (pymupdf)
- **llama.cpp** - Self-hosted local inference server for auxiliary models
- **Telegram** - Bot interface
- **Git** - Workspace state versioning
- **JSON5** - Configuration format

---

## Directory Structure

```
josemar-assistente/
├── agent-state/            # Nested git repo: agent workspace state (private repo)
│   ├── .sync-manifest      # Explicit list of files to version
│   ├── .gitignore          # Security: prevents secret commits
│   ├── skills/             # Unified skills (versioned)
│   │   ├── finance-assistant/  # Expense tracking (PDF + Google Sheets)
│   │   ├── aux-ml/             # Auxiliary ML queue skill (OCR/transcription jobs)
│   │   ├── gogcli-tables/      # Google Sheets CLI
│   │   └── workspace-sync/     # Git operations skill
│   ├── memory/             # Daily memory logs (rotated)
│   └── avatars/            # Agent avatars
├── config/                 # OpenClaw configuration
├── credentials/            # Service credentials (NOT versioned)
│   ├── README.md           # Credential setup guide
│   └── <service>/          # One folder per service
├── scripts/                # Helper scripts
│   ├── workspace-sync.sh          # Git sync logic
│   ├── obsidian-backup.sh         # Vault backup + slot rotation logic
│   └── obsidian-backup-daemon.sh  # Daily backup scheduler
├── aux-ml/                 # Auxiliary ML service (queue + model lifecycle)
├── templates/              # Templates for new deployments
│   └── agent-state-template/
├── .github/workflows/      # CI/CD automation
├── Dockerfile              # Custom OpenClaw image
├── docker-compose.yml      # Deployment configuration
└── .env.example            # Environment variables template
```

**Workspace Storage:**
- `openclaw-workspace` stores OpenClaw runtime state (workspace repo, sessions, paired devices)
- `obsidian-vault` stores Obsidian markdown files and attachments (not git-versioned)
- `syncthing-config` stores Syncthing device identity and sync settings
- `obsidian-rclone-config` stores rclone auth for Obsidian backups
- `obsidian-backup-state` stores backup slot rotation pointer (`next-slot`)
- Docker volume paths on host use `/var/lib/docker/volumes/<volume-name>/_data/`
- Workspace repo files are automatically synced to a private git repo (`agent-state/`)

**Agent State Repo:**
- A private git repo that version-controls workspace files (personality, skills, memory)
- Synced automatically on container start and periodically
- Only files listed in `.sync-manifest` are versioned (security-first)
- Merge strategy: remote wins on conflicts (remote is the "blessed" version)

---

## Development Environment

**Context:** Unless explicitly stated otherwise, assume you are in the **local development environment** (user/developer machine).

**Local Testing:** You can safely test locally by disabling Telegram (see "Local Development & Testing" section below). This avoids conflicts with the production bot while allowing full functionality testing via the Web UI.

---

## Local Development & Testing

You can safely test locally without disconnecting the production bot:

1. **Disable Telegram in `.env`:**
   ```bash
   TELEGRAM_ENABLED=false
   ```

2. **Start the service locally:**
   ```bash
   docker-compose up -d
   ```

   To include auxiliary ML batch processing locally, set `COMPOSE_PROFILES=aux-ml` in `.env` before starting.

3. **Access the Web UI:**
   The browser will prompt for HTTP Basic Auth. Enter:
   - Username: `operator` (or any username - OpenClaw ignores it)
   - Password: Your `GATEWAY_AUTH_PASSWORD` from `.env`
   
   Or access via URL:
   ```
   http://operator:YOUR_GATEWAY_AUTH_PASSWORD@localhost:18789/
   ```

4. **Approve device pairing** (first time only):
   When you first access the Web UI, you'll see "pairing required". Approve your browser:
   ```bash
   # List pending devices
   docker compose exec openclaw openclaw devices list
   
   # Approve the pending device
   docker compose exec openclaw openclaw devices approve <requestId>
   ```
   The device will be remembered for future connections.

5. **Test via browser interface** - All functionality works except Telegram

   Optional Obsidian stack testing:
   - Set `LAN_BIND_IP=127.0.0.1` in `.env`
   - Access Syncthing UI at `http://127.0.0.1:8384`
   - Load rclone config into volume `obsidian-rclone-config` before testing backups (see `docs/obsidian-operations.md`)

6. **Stop when done:**
   ```bash
   docker-compose down
   ```

**Why this works:** The Telegram bot token can only be used by one active deployment. By setting `TELEGRAM_ENABLED=false`, the Telegram channel is disabled locally, avoiding conflicts while still allowing full testing through the Web UI.

---

## Git Workflow

### Branch Strategy

**Always create a new branch for:**
- New features
- Bug fixes
- Configuration changes
- Documentation updates

**Branch naming:**
- `feature/description` - e.g., `feature/add-gog-integration`
- `fix/description` - e.g., `fix/pdf-extractor-error`
- `update/description` - e.g., `update/documentation`

**Never commit directly to main** (except trivial fixes like typos)

### Workflow

```bash
# 1. Create and switch to new branch
git checkout -b feature/my-feature-name

# 2. Make your changes
# ... edit files ...

# 3. Stage and commit
git add .
git commit -m "Description of changes"

# 4. Push to remote
git push -u origin feature/my-feature-name
```

### After Pushing

1. Changes are tested on the branch
2. Deploy via GitHub Actions workflow when ready
3. Production deployment happens from main after merge

---

## Architecture Overview

### Components

1. **OpenClaw Gateway** (`ghcr.io/openclaw/openclaw:latest`)
   - Manages channels, agents, and skills
   - Multi-provider LLM support (Z.AI, DeepSeek, etc.)
   - Session management and conversation routing

2. **Agents**
   - Default agent: "Josemar"
   - Configurable models: `{provider}/{model-id}` pattern
   - Identity and personality defined in workspace files (SOUL.md, etc.)

3. **Channels**
   - Telegram bot integration (can be disabled via `TELEGRAM_ENABLED`)
   - User access controlled via allowlist

4. **Skills (Unified System)**
   - All skills live in `agent-state/skills/` (versioned in private git repo)
   - Skills can be modified at runtime; changes are synced back to git
   - Loaded from `/root/.openclaw/skills/` inside container

5. **Workspace State (Git-Backed)**
   - Personality files (SOUL.md, IDENTITY.md, etc.) versioned
   - Skills versioned
   - Memory logs versioned with rotation
   - Synced via `scripts/workspace-sync.sh`

6. **Credentials**
   - Stored in `credentials/<service>/` on host (not versioned)
   - Mounted read-only into container at `/root/.openclaw/credentials/<service>/`
   - See `credentials/README.md` for setup guide

7. **Syncthing (Vault Sync)**
   - Syncs `obsidian-vault` volume to laptop/mobile devices
   - Uses LAN-only bindings with `LAN_BIND_IP` to avoid internet exposure
   - Device trust model: explicit device ID pairing

8. **Obsidian Backup (rclone)**
   - Daily scheduler runs in `obsidian-backup` container
   - Syncs vault into rotating slots (`slot-1` ... `slot-5`) in Google Drive
   - Auth loaded from `credentials/rclone/rclone.conf` (or CI secret restore)

9. **Auxiliary ML Service (`aux-ml`)**
   - Optional container running `llama.cpp` server in router mode
   - Provides FIFO queue and one-at-a-time batch processing for long tasks
   - Loads model on demand; unloads when queue is empty or next job uses another model

### Data Flow

1. User sends message/PDF via Telegram (or Web UI if testing locally)
2. Gateway routes to appropriate agent
3. Agent invokes LLM with system prompts and context
4. LLM may call skills (PDF extraction, etc.)
5. Skill processes and returns structured data
6. LLM formats response
7. Response sent to user via Telegram (or displayed in Web UI)
8. Periodically, workspace changes are committed and pushed to agent-state repo
9. Syncthing mirrors the Obsidian vault to paired devices
10. Daily backup job uploads current vault snapshot to rotating Google Drive slots
11. For heavy jobs, Josemar skill submits task to `aux-ml`, polls completion, and returns result

---

## Configuration

**Main Config:** `config/openclaw.json` (JSON5 format)

**Environment Variables:**
- `ZAI_API_KEY` - Primary LLM provider
- `TELEGRAM_BOT_TOKEN` - Bot authentication
- `TELEGRAM_ENABLED` - Enable/disable Telegram (`true`/`false`, default: `true`)
- `DEEPSEEK_API_KEY` - Optional fallback provider
- `OLLAMA_API_KEY` - Optional Ollama Cloud provider key
- `GATEWAY_AUTH_PASSWORD` - Web UI access password (HTTP Basic Auth)
- `PEDRO_TELEGRAM_ID` - Primary user (add more as needed)
- `GOG_KEYRING_PASSWORD` - Optional passphrase used by gogcli keyring to decrypt Google OAuth token storage
- `WORKSPACE_STATE_REPO` - Private git repo URL for agent state
- `WORKSPACE_REPO_TOKEN` - GitHub PAT for agent state repo (use GitHub secret in deployment)
- `WORKSPACE_SYNC_ON_START` - Enable/disable git sync on start (`true`/`false`)
- `WORKSPACE_SYNC_INTERVAL` - Minutes between periodic syncs (0 = disabled)
- `WORKSPACE_MEMORY_DAYS` - Days to keep memory logs
- `LAN_BIND_IP` - LAN interface IP for Syncthing ports (`127.0.0.1` for local-only testing)
- `SYNCTHING_GUI_BIND_IP` - Bind IP for Syncthing GUI/API (`127.0.0.1` recommended)
- `TZ` - Timezone used by Syncthing and backup scheduler
- `OBSIDIAN_BACKUP_TIME` - Daily backup time (`HH:MM`, default `03:15`)
- `OBSIDIAN_BACKUP_RUN_ON_START` - Run one backup when backup container starts
- `OBSIDIAN_BACKUP_SLOTS` - Number of rotating slots kept in Drive (default `5`)
- `OBSIDIAN_GDRIVE_REMOTE` - rclone remote name (default `gdrive`)
- `OBSIDIAN_GDRIVE_PATH` - Google Drive destination folder for slot backups
- `AUX_ML_ENABLED` - Feature flag for aux-ml skill integration (`true`/`false`)
- `COMPOSE_PROFILES` - Set to `aux-ml` to start/build auxiliary ML service
- `AUX_ML_GLM_OCR_URL` - Optional build-time download URL override for `glm-ocr.gguf` model
- `AUX_ML_GLM_OCR_SHA256` - Optional SHA256 checksum override for downloaded `glm-ocr.gguf`
- `AUX_ML_GLM_OCR_MMPROJ_URL` - Optional build-time download URL override for `mmproj-glm-ocr.gguf`
- `AUX_ML_GLM_OCR_MMPROJ_SHA256` - Optional SHA256 checksum override for downloaded `mmproj-glm-ocr.gguf`
- `AUX_ML_URL` - Internal URL for auxiliary ML API (default `http://aux-ml:8091`)
- `AUX_ML_MEMORY_LIMIT` - Docker memory limit for `aux-ml` container (should match largest model requirement)
- `AUX_ML_MEMORY_LIMIT_MB` - Numeric memory budget used for runtime validation
- `AUX_ML_MAX_QUEUE` - Maximum queued auxiliary ML jobs
- `AUX_ML_JOB_TIMEOUT_SECONDS` - Per-job timeout for long-running tasks
- `AUX_ML_POLL_INTERVAL_SECONDS` - Poll interval for queue/model status checks
- `AUX_ML_LLAMACPP_TIMEOUT_SECONDS` - llama.cpp server read/write timeout for long OCR inference requests
- `AUX_ML_ALLOWED_INPUT_DIRS` - Comma-separated allowed roots for OCR file inputs
- `AUX_ML_ENFORCE_MEMORY_LIMIT` - Fail fast if configured memory budget is insufficient
- `AUX_ML_OCR_MAX_PAGES` - Max pages allowed per OCR PDF request

**Agent Personality:** Configured in workspace markdown files (SOUL.md, MEMORY.md) - not in JSON config.

---

## Where to Find Details

**For specific tasks, navigate to the relevant directory:**

- **Configuration details, Web UI setup, troubleshooting** -> `config/`
- **Skill development, testing, deployment** -> `agent-state/skills/`
- **Credential setup and management** -> `credentials/`
- **CI/CD workflows, GitHub Actions, runner setup** -> `.github/workflows/`
- **Docker deployment, environment setup** -> See `docker-compose.yml` and `.env.example`
- **Auxiliary ML service API and operations** -> `docs/aux-ml.md` and `aux-ml/`
- **Obsidian sync/backup operations** -> `docs/obsidian-operations.md`
- **Agent state template** -> `templates/agent-state-template/`
- **External docs:** https://docs.openclaw.dev

**The subdirectory AGENTS.md files contain complete implementation details for their domains.**

---

## Agent State Repository

The `agent-state/` directory is a **nested git repo** (not a submodule) containing Josemar's personality, skills, and memory. It is gitignored by the main repo - each user should set up their own private state repo.

### First-Time Setup

```bash
# Clone your private state repo into agent-state/
git clone https://github.com/YOUR_USER/your-agent-state.git agent-state
```

If you don't have a state repo yet, copy the template:
```bash
cp -r templates/agent-state-template/ agent-state
cd agent-state && git init && git add -A && git commit -m "Initial state"
```

### Structure

```
agent-state/
├── .sync-manifest      # ONLY files listed here are versioned (security-first)
├── .gitignore          # Blocks secrets, PDFs, runtime files
├── skills/             # Unified skills (all skills live here)
│   ├── AGENTS.md       # Skill development guide
│   ├── finance-assistant/
│   ├── aux-ml/
│   ├── gogcli-tables/
│   └── workspace-sync/
├── memory/             # Daily memory logs (YYYY-MM-DD.md)
└── avatars/            # Agent avatars
```

### Making Changes to Agent State

When the user asks you to modify Josemar's personality, skills, memory, or any workspace file:

1. **Navigate to the state repo:**
   ```bash
   cd agent-state
   ```

2. **Make your changes** (edit personality files, create/modify skills, etc.)

3. **Commit and push** in the state repo:
   ```bash
   git add .
   git commit -m "description of changes"
   git push
   ```

No need to update the main repo - `agent-state/` is gitignored.

### What Gets Versioned

Only files matching patterns in `.sync-manifest` are committed by the auto-sync script. This is a security measure to prevent accidental secret leaks.

Current tracked patterns:
- Core personality files (SOUL.md, MEMORY.md, IDENTITY.md, USER.md, TOOLS.md, HEARTBEAT.md, BOOT.md, AGENTS.md)
- Skills: `skills/*/*`
- Memory logs: `memory/YYYY-MM-DD.md`
- Avatars: `avatars/*`

### Merge Strategy

Remote wins on conflicts. The remote repo is the authoritative version.

### Runtime Behavior

On container start, the workspace sync script (`scripts/workspace-sync.sh`) clones or syncs the state repo based on the `WORKSPACE_STATE_REPO` env var. See `docker-entrypoint.sh` for details.

---

## Critical Warnings

1. **Never commit secrets** - Use `.env` file (already in .gitignore) and GitHub Secrets
2. **Always disable Telegram for local testing** - Set `TELEGRAM_ENABLED=false` to avoid production conflicts
3. **Always work in branches** - Push to server for production deployment
4. **Agent state repo must be private** - Contains personality and memory files
5. **Only files in `.sync-manifest` are versioned** - This prevents accidental secret leaks
6. **Credentials go in `credentials/`** - Never in workspace or agent-state
7. **Docker volume is the primary storage** - Git repo is backup/sync, not the source of truth
8. **Keep Syncthing LAN-only** - Bind ports to `LAN_BIND_IP` and disable global discovery/relays for local trust boundary

---

## Security: Secret Scanning

This repository uses **gitleaks** to prevent accidental commits of secrets (API keys, tokens, passwords, etc.).

### Automated Scanning

- **CI/CD:** Every push to any branch is scanned via GitHub Actions (`.github/workflows/gitleaks.yml`)
- **Local:** Pre-commit hooks scan staged changes before each commit (optional but recommended)

### Setup Pre-commit Hooks (One-Time)

```bash
./scripts/setup-pre-commit.sh
```

This script creates `venv/` if needed, installs pre-commit, and installs git hooks. After setup, gitleaks runs automatically on every `git commit`.

### Usage

```bash
# Normal commit - gitleaks runs automatically
git commit -m "your message"

# Skip gitleaks (emergency only)
SKIP=gitleaks git commit -m "your message"

# Manual full scan
source venv/bin/activate && pre-commit run gitleaks --all-files
```
