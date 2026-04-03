# AGENTS.md

**Purpose:** Root guidance for AI assistants working with the Josemar Assistente project. This is your entry point - for detailed implementation, navigate to the relevant subdirectory.

---

## Project Overview

Josemar Assistente is a self-hosted AI assistant bot built on OpenClaw, running in Docker with Telegram integration. It specializes in processing Brazilian credit card invoice PDFs and provides intelligent assistance in Brazilian Portuguese.

**Core Architecture:**
- **OpenClaw Gateway** - AI agent orchestration (multi-provider LLM support)
- **Telegram Channel** - Bot interface for user interaction
- **Two-Tier Skills System** - Extensible tools for PDF processing and other tasks
- **Docker Deployment** - Containerized with persistent workspace storage

**Key Technologies:**
- **OpenClaw** - Self-hosted AI agent gateway (supports multiple LLM providers: GLM, DeepSeek, OpenAI-compatible APIs)
- **Docker & Docker Compose** - Containerized deployment
- **Python 3** - Custom skills and PDF processing (pymupdf)
- **Telegram** - Bot interface
- **JSON5** - Configuration format

---

## Directory Structure

```
josemar-assistente/
├── config/                 # OpenClaw configuration
├── repo-skills/           # Version-controlled skills (production-ready)
├── scripts/               # Python scripts for skills
├── .github/workflows/     # CI/CD automation
├── Dockerfile             # Custom OpenClaw image
├── docker-compose.yml     # Deployment configuration
└── .env.example          # Environment variables template
```

**Workspace Storage:**
- Persistent data stored in Docker volume `openclaw-workspace`
- Contains: conversation history, personality files (SOUL.md, MEMORY.md), session data
- Location on host: `/var/lib/docker/volumes/josemar-assistente_openclaw-workspace/_data/`
- This is runtime state - not version controlled

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

4. **Skills (Two-Tier System)**
   - **Repo Skills** (`repo-skills/`): Version-controlled, production-ready
   - **Runtime Skills** (`/root/.openclaw/skills/`): Created by assistant during conversations
   - Runtime skills take precedence over repo skills

### Data Flow

1. User sends message/PDF via Telegram (or Web UI if testing locally)
2. Gateway routes to appropriate agent
3. Agent invokes LLM with system prompts and context
4. LLM may call skills (PDF extraction, etc.)
5. Skill processes and returns structured data
6. LLM formats response
7. Response sent to user via Telegram (or displayed in Web UI)

---

## Configuration

**Main Config:** `config/openclaw.json` (JSON5 format)

**Environment Variables:**
- `ZAI_API_KEY` - Primary LLM provider
- `TELEGRAM_BOT_TOKEN` - Bot authentication
- `TELEGRAM_ENABLED` - Enable/disable Telegram (`true`/`false`, default: `true`)
- `DEEPSEEK_API_KEY` - Optional fallback provider
- `GATEWAY_AUTH_PASSWORD` - Web UI access password (HTTP Basic Auth)
- `PEDRO_TELEGRAM_ID` - Primary user (add more as needed)
- `GOG_KEYRING_PASSWORD` - Optional game library integration

**Agent Personality:** Configured in workspace markdown files (SOUL.md, MEMORY.md) - not in JSON config.

---

## Where to Find Details

**For specific tasks, navigate to the relevant directory:**

- **Configuration details, Web UI setup, troubleshooting** → `config/`
- **Skill development, testing, deployment** → `repo-skills/`
- **CI/CD workflows, GitHub Actions, runner setup** → `.github/workflows/`
- **Docker deployment, environment setup** → See `docker-compose.yml` and `.env.example`
- **External docs:** https://docs.openclaw.dev

**The subdirectory AGENTS.md files contain complete implementation details for their domains.**

---

## Critical Warnings

1. **Never commit secrets** - Use `.env` file (already in .gitignore)
2. **Always disable Telegram for local testing** - Set `TELEGRAM_ENABLED=false` to avoid production conflicts
3. **Always work in branches** - Push to server for production deployment
4. **Agent state repo must be private** - Contains personality and memory files
5. **Only files in `.sync-manifest` are versioned** - This prevents accidental secret leaks
6. **Credentials go in `credentials/`** - Never in workspace or agent-state
7. **Docker volume is the primary storage** - Git repo is backup/sync, not the source of truth

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
