# AGENTS.md

**Purpose:** Root guidance for AI assistants working with the Josemar Assistente project. This is your entry point - for detailed topics, see subdirectory AGENTS.md files.

**Quick Navigation:**

| Topic | Location |
|-------|----------|
| OpenClaw configuration | `config/AGENTS.md` |
| Skill development | `skills/AGENTS.md` |
| CI/CD workflows | `.github/workflows/AGENTS.md` |

---

## Project Overview

Josemar Assistente is a self-hosted OpenClaw-based AI assistant bot running in Docker with Telegram integration. It specializes in processing Brazilian credit card invoice PDFs and provides intelligent assistance in Brazilian Portuguese.

**Key Technologies:**
- **OpenClaw**: Self-hosted AI agent gateway
- **Docker & Docker Compose**: Containerized deployment
- **Python 3**: Custom skills and PDF processing
- **GLM 4.7 (Z.AI)**: Primary LLM provider
- **DeepSeek**: Secondary LLM provider (optional)
- **Telegram**: Bot interface via python-telegram-bot
- **pymupdf**: PDF text extraction

**Main Features:**
- Telegram bot interface with natural language interaction
- PDF extraction for Brazilian credit card invoices
- Brazilian Portuguese language support
- JSON5 configuration format
- External LLM API integration (Z.AI, DeepSeek)
- Custom skills system for extensibility
- Workspace persistence for data

## Development Environment Context

**Unless explicitly stated otherwise by the user, assume you are in the user/developer/local machine environment.** This means:
- Commands should target the local development setup
- File operations affect the local filesystem
- Docker operations run locally (unless specified otherwise)
- Testing and debugging are performed in the local environment

When working with deployment commands (like `docker-compose up/down`), always confirm the deployment context with the user if it is not explicitly stated.

## Development Commands

### Important: DO NOT Deploy Locally

**Warning:** The Telegram bot token can only be used by one active deployment at a time. Running `docker compose up` locally will conflict with the production server deployment and cause the bot to stop responding on the server.

**DEFAULT BEHAVIOR: ALWAYS work in branches and push to the server for testing.** Never run the full service locally unless you are using a different Telegram bot token (e.g., a test bot from @BotFather).

**EXCEPTION: If the user EXPLICITLY asks you to deploy locally**, you may proceed with local deployment for debugging purposes. This should only be done when:
- The production server is not running
- You're using a test bot token
- The user specifically instructs you to do so

**Safe local operations (do not start the service):**
- Build the image: `docker compose build` (builds but doesn't run)
- Test configurations: `docker compose config` (validates compose file)
- Lint Dockerfile: `docker run --rm -i hadolint/hadolint < Dockerfile`
- Test scripts locally: `python3 scripts/pdf_extractor.py`

### Local Development

**Start the bot (PRODUCTION SERVER ONLY - NEVER RUN LOCALLY):**
```bash
# ⚠️ WARNING: Only run this on the production server!
# Running locally will disconnect the production bot.
docker-compose up -d

# View logs
docker-compose logs -f openclaw

# Stop services
docker-compose down
```

**Build and rebuild:**
```bash
# Build the image
docker-compose build

# Rebuild without cache
docker-compose build --no-cache
```

**Configuration management:**
```bash
# Edit OpenClaw configuration
nano config/openclaw.json

# Restart after configuration changes
docker-compose restart openclaw

# Check configuration syntax
docker-compose run --rm openclaw openclaw --validate-config
```

**Testing skills:**
```bash
# Test PDF extraction skill
echo "/path/to/test.pdf" | docker-compose run --rm -T openclaw /root/.openclaw/skills/pdf-extractor/pdf-extractor

# Test with raw text
echo "10/12 UBER TRIP 32,75" | docker-compose run --rm -T openclaw /root/.openclaw/skills/pdf-extractor/pdf-extractor
```

### Deployment

**Production deployment:**
```bash
# Deploy via CI/CD (GitHub Actions workflow)
# The deploy-to-home-server workflow handles deployment automatically

# Or deploy manually (on the server):
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Environment Variables

Create a `.env` file with the following variables:

```bash
# Z.AI API Key for GLM models (required)
ZAI_API_KEY=your_zai_api_key_here

# Telegram Bot Token (required)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# DeepSeek API Key (optional)
DEEPSEEK_API_KEY=your_deepseek_api_key_here

# OpenClaw log level (default: info)
OPENCLAW_LOG_LEVEL=info

# Telegram user ID for the primary user (Pedro)
# This user will be pre-approved to chat with the bot without pairing
# Get your ID from @userinfobot on Telegram
PEDRO_TELEGRAM_ID=190731460

# Additional users can be added following the pattern: {NAME}_TELEGRAM_ID
# Example:
# ALICE_TELEGRAM_ID=987654321
# BOB_TELEGRAM_ID=555666777
# Then add them to config/openclaw.json channels.telegram.allowFrom array
```

See `.env.example` for template and detailed descriptions.

## Architecture

### Project Structure

```
josemar-assistente/
├── Dockerfile                  # Custom OpenClaw image with PDF support
├── docker-compose.yml          # Docker Compose deployment configuration
├── docker-entrypoint.sh        # Container entrypoint script
├── .env.example               # Environment variables template
├── config/                     # OpenClaw configuration
│   └── openclaw.json         # Main configuration (JSON5 format in .json file)
├── scripts/                    # Python scripts for skills
│   └── pdf_extractor.py       # PDF extraction implementation
├── skills/                     # Custom OpenClaw skills
│   └── pdf-extractor/          # PDF extraction skill
│       ├── SKILL.md           # Skill documentation
│       └── pdf-extractor      # Skill executable (bash wrapper)
└── README.md                  # Project documentation
```

**Workspace Storage:**
- **Location**: Named Docker volume `openclaw-workspace` (not in git repo)
- **Path**: `/var/lib/docker/volumes/josemar-assistente_openclaw-workspace/_data/`
- **Purpose**: Persistent storage for conversation history, personality files (SOUL.md, MEMORY.md), and session data
- **Management**: Docker automatically manages the volume lifecycle

### OpenClaw Architecture

**OpenClaw Components:**
- **Gateway**: Central AI agent gateway managing channels and skills
- **Providers**: External LLM API integrations (Z.AI, DeepSeek)
- **Agents**: AI agent configurations with prompts and models
- **Channels**: Communication interfaces (Telegram)
- **Skills**: Custom tools and extensions

**Configuration Flow:**
1. Container starts with entrypoint script
2. Configuration is copied from `/root/.openclaw-source` to `/root/.openclaw`
3. OpenClaw validates configuration and starts services
4. Skills are loaded from `/root/.openclaw/skills/`
5. Telegram bot connects and starts accepting messages

### Core Modules

**1. OpenClaw Gateway**
- **Base Image**: `ghcr.io/openclaw/openclaw:latest`
- **Purpose**: Central AI agent orchestration
- **Features**: Multi-provider support, skill system, channel management

**2. PDF Extraction Skill**
- **Location**: `scripts/pdf_extractor.py` + `skills/pdf-extractor/`
- **Purpose**: Extract data from Brazilian credit card invoice PDFs
- **Technology**: Python 3 + pymupdf
- **Input**: PDF file path or raw text
- **Output**: Structured JSON with expense data

**3. Configuration System**
- **Format**: JSON5 (allows comments and trailing commas)
- **Location**: `config/openclaw.json`
- **Features**: Environment variable expansion, modular configuration
- **Reference**: See `config/AGENTS.md` for detailed configuration

**4. Docker Integration**
- **Volumes**: Configuration, workspace, and skills directories
- **Network**: Custom bridge network for service communication
- **Health Check**: Validates OpenClaw installation

### Key Patterns

**1. JSON5 Configuration**
- Supports comments (`//` and `/* */`)
- Allows trailing commas
- More readable than standard JSON
- Environment variable expansion with `${VAR}` syntax

**2. Skill Development**
- Each skill has its own directory
- `SKILL.md` documentation file (frontmatter with metadata)
- Executable script that reads from stdin and outputs JSON
- Skills are called by OpenClaw as external tools

**3. PDF Processing**
- Brazilian currency format: `R$ 1.234,56` (thousands: dot, decimal: comma)
- Date format: `dd/mm` (Brazilian format)
- Transaction lines: Start with date, end with amount
- Total patterns: Multiple regex patterns for different invoice formats

**4. Docker Volume Management**
- **Config Source**: Read-only mount for configuration files
- **Workspace**: Persistent data storage
- **Skills**: Development mount for skill editing
- **Scripts**: Python scripts for skill implementations

**5. LLM Integration**
- **Primary Model**: GLM 4.7 via Z.AI provider
- **Fallback Model**: DeepSeek Reasoner
- **Model Selection**: Automatic fallback on errors
- **Tool Calling**: Both models support tool calling

### Dependencies

**Base Image:**
- `ghcr.io/openclaw/openclaw:latest`: Official OpenClaw image

**Python Dependencies:**
- `pymupdf`: PDF text extraction and processing
- `python3`: Python runtime

**API Providers:**
- **Z.AI**: GLM 4.7 model API (https://z.ai)
- **DeepSeek**: DeepSeek Chat and Reasoner APIs (https://api.deepseek.com)

**Telegram:**
- Bot Token from @BotFather
- python-telegram-bot (bundled with OpenClaw)

## Data Flow

### PDF Processing Flow

1. **User Uploads PDF**: User sends PDF to Telegram bot
2. **File Storage**: Telegram bot saves file to workspace
3. **Skill Invocation**: OpenClaw calls pdf-extractor skill
4. **Text Extraction**: Python script extracts text using pymupdf
5. **Data Parsing**: Regex patterns extract expenses
6. **JSON Output**: Structured data returned as JSON
7. **Response Formatting**: LLM formats response for user

### Message Processing Flow

1. **Message Reception**: Telegram bot receives message
2. **Session Lookup**: OpenClaw retrieves session context
3. **Agent Selection**: Default agent "josemar" is selected
4. **Model Invocation**: LLM processes message with system prompt
5. **Tool Decision**: Model decides if tools are needed
6. **Skill Execution**: External skills called via stdin/stdout
7. **Response Generation**: Model generates final response
8. **Message Sending**: Response sent to Telegram

### Skill Execution Flow

1. **Skill Call**: OpenClaw executes skill executable
2. **Input Processing**: Skill reads JSON from stdin
3. **Processing**: Skill performs task (e.g., PDF extraction)
4. **Output Generation**: Skill outputs JSON to stdout
5. **Result Parsing**: OpenClaw parses JSON result
6. **Context Update**: Result added to conversation context
7. **Next Step**: Model generates response with tool results

## Docker Deployment Architecture

### Services

**OpenClaw Service**
- **Image**: Custom build based on `ghcr.io/openclaw/openclaw:latest`
- **Container Name**: `josemar-assistente`
- **Restart Policy**: `unless-stopped`
- **Ports**: 9090 (metrics, optional)

**Volumes:**
- **Config Source**: `./config:/root/.openclaw-source:ro` (read-only)
- **Workspace**: Named volume `openclaw-workspace:/root/.openclaw/workspace` (persistent, stored in Docker managed location)
- **Skills** (optional): `./skills:/root/.openclaw-source/skills:ro`
- **Scripts** (optional): `./scripts:/app/scripts:ro`

**Environment Variables:**
- `ZAI_API_KEY`: Z.AI API key for GLM models
- `TELEGRAM_BOT_TOKEN`: Telegram bot token
- `DEEPSEEK_API_KEY`: DeepSeek API key (optional)
- `OPENCLAW_LOG_LEVEL`: Logging level (info, debug, warn, error)

**Health Check:**
- **Command**: `openclaw --version`
- **Interval**: 30 seconds
- **Timeout**: 10 seconds
- **Retries**: 3
- **Start Period**: 40 seconds

### Network Configuration

- **Network**: `openclaw-network` bridge network
- **Isolation**: Services communicate within private network
- **External Access**: Only Telegram API needs external access

### Deployment Commands

```bash
# Start all services
docker-compose up -d

# Stop services
docker-compose down

# View logs
docker-compose logs -f openclaw

# Rebuild after changes
docker-compose build
docker-compose up -d

# Backup workspace (named Docker volume)
docker run --rm -v josemar-assistente_openclaw-workspace:/data -v $(pwd):/backup alpine tar czf /backup/workspace-backup-$(date +%Y%m%d).tar.gz /data

# Restore workspace (to named Docker volume)
docker run --rm -v josemar-assistente_openclaw-workspace:/data -v $(pwd):/backup alpine tar xzf /backup/workspace-backup-YYYYMMDD.tar.gz -C /

# Check service status
docker-compose ps

# Access container shell
docker-compose exec openclaw sh
```

### Troubleshooting

**Bot not responding:**
```bash
# Check logs
docker-compose logs -f openclaw

# Verify API keys
docker-compose exec openclaw env | grep API_KEY

# Test OpenClaw CLI
docker-compose exec openclaw openclaw --version
```

**Skill not working:**
```bash
# Check skill permissions
docker-compose exec openclaw ls -la /root/.openclaw/skills/

# Test skill manually
echo "test" | docker-compose exec -T openclaw /root/.openclaw/skills/pdf-extractor/pdf-extractor

# Check Python dependencies
docker-compose exec openclaw python3 -c "import pymupdf; print(pymupdf.__version__)"
```

**Configuration issues:**
```bash
# Validate JSON5 syntax
docker-compose run --rm openclaw sh -c "cat /root/.openclaw/openclaw.json5 | jq ."

# Check environment variables
docker-compose exec openclaw env | grep -E "ZAI|TELEGRAM|DEEPSEEK"

# Reload configuration
docker-compose restart openclaw
```

## Skills System

Skills extend the assistant's capabilities via external executables. Each skill has:
- A directory in `skills/`
- A `SKILL.md` with frontmatter metadata
- An executable that reads stdin and outputs JSON

**Existing Skill:**
- **PDF Extractor** (`skills/pdf-extractor/`) - Extracts data from Brazilian credit card invoice PDFs

See `skills/AGENTS.md` for complete skill development documentation.

## Configuration Reference

For detailed OpenClaw configuration reference, see `config/AGENTS.md`. Key points:
- Configuration uses JSON5 format in `config/openclaw.json`
- Environment variables use `${VAR}` syntax
- Supports comments and trailing commas

See `config/AGENTS.md` for detailed configuration reference including:
- Model providers (DeepSeek, Z.AI)
- Agent definitions
- Telegram channel configuration with user allowlist
- Skills configuration
- Session management
- Logging settings

**Note:** Agent prompts and personality are configured in workspace markdown files (SOUL.md, AGENTS.md), not in the JSON configuration.

## Testing

### Testing Skills

```bash
# Test PDF extraction with local file
echo "/path/to/test.pdf" | python3 scripts/pdf_extractor.py

# Test with raw text
echo "10/12 UBER TRIP 32,75" | python3 scripts/pdf_extractor.py

# Test skill in container
echo "/workspace/test.pdf" | docker-compose run --rm -T openclaw /root/.openclaw/skills/pdf-extractor/pdf-extractor
```

### Testing Telegram Integration

```bash
# Check bot status
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"

# Get bot info
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"

# Send test message
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=YOUR_CHAT_ID" \
  -d "text=Test message from Josemar"
```

### Testing LLM Integration

```bash
# Test Z.AI API (requires API key)
curl -X POST "https://api.z.ai/v1/chat/completions" \
  -H "Authorization: Bearer ${ZAI_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-4.7",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Test DeepSeek API (requires API key)
curl -X POST "https://api.deepseek.com/v1/chat/completions" \
  -H "Authorization: Bearer ${DEEPSEEK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Development Workflow

### Adding New Features

1. **Plan the Feature**: Understand requirements and data flow
2. **Create Skill**: Develop skill with proper structure
3. **Update Configuration**: Add skill to `config/openclaw.json`
4. **Test Locally**: Test skill with Python scripts
5. **Test in Container**: Test in Docker environment
6. **Deploy**: Use GitHub Actions workflow or docker compose commands for production deployment
7. **Monitor**: Check logs and monitor performance

### Debugging

**Enable debug logging:**
```bash
# Edit .env
OPENCLAW_LOG_LEVEL=debug

# Restart service
docker-compose restart openclaw

# View logs
docker-compose logs -f openclaw
```

**Check skill execution:**
```bash
# Enable verbose output in skill
docker-compose exec openclaw sh -c "cat /root/.openclaw/skills/pdf-extractor/pdf-extractor"

# Test with verbose output
echo "test" | docker-compose exec -T openclaw sh -x /root/.openclaw/skills/pdf-extractor/pdf-extractor
```

**Validate configuration:**
```bash
# Check JSON5 syntax
docker-compose run --rm openclaw sh -c "cat /root/.openclaw/openclaw.json5 | jq ."

# Check environment variables
docker-compose exec openclaw env | grep -E "ZAI|TELEGRAM|DEEPSEEK"

# Reload configuration
docker-compose restart openclaw
```

## Web UI Access

The OpenClaw Gateway provides a web interface accessible via browser:

### Accessing the UI

**URL:** `http://localhost:18789/__openclaw__/canvas/?token=YOUR_TOKEN`

**Requirements:**
- Gateway authentication token (set in `GATEWAY_AUTH_TOKEN` env var)
- Port 18789 exposed in Docker Compose

### Configuration

**1. Generate a secure token:**
```bash
openssl rand -hex 32
```

**2. Add to `.env`:**
```bash
GATEWAY_AUTH_TOKEN=your-generated-token
```

**3. Access the UI:**
```
http://localhost:18789/__openclaw__/canvas/?token=your-generated-token
```

### Remote Access via Cloudflared Tunnel

For secure remote access to the Web UI from outside your local network, you can use a Cloudflare Tunnel. This setup uses your existing Cloudflared tunnel (running in an LXC container on Proxmox) to provide HTTPS termination, resolving the "control ui requires device identity" browser security error.

**Prerequisites:**
- Existing Cloudflared tunnel running in an LXC container on Proxmox
- A Cloudflare-managed domain (e.g., `casabanana.casa`)
- OpenClaw running on a VM accessible from the Cloudflared LXC

**1. Add DNS Record in Cloudflare Dashboard:**
1. Log into [dash.cloudflare.com](https://dash.cloudflare.com)
2. Select your domain: `casabanana.casa`
3. Go to **DNS** → **Records**
4. Add new CNAME:
   - **Name:** `josemar`
   - **Target:** `<your-tunnel-id>.cfargotunnel.com`
   - **TTL:** Auto
   - **Proxy status:** Orange cloud (Proxied) ✓

**2. Update OpenClaw Configuration:**
The configuration already includes the HTTPS origin in `allowedOrigins`:
```json5
controlUi: {
  allowedOrigins: [
    "http://192.168.15.200:18789",
    "http://localhost:18789",
    "https://josemar.casabanana.casa",
  ],
},
```

**3. Add Ingress Rule to Cloudflared Config (in your LXC):**

SSH into your Cloudflared LXC container and update the config:

```bash
# SSH into the LXC container
ssh <your-user>@<cloudflared-lxc-ip>

# Navigate to config directory
cd /etc/cloudflared/

# Backup current config
sudo cp config.yml config.yml.backup.$(date +%Y%m%d)

# Edit config
sudo nano config.yml
```

Add this ingress rule to your existing `config.yml`:

```yaml
ingress:
  # Your existing Home Assistant rule
  - hostname: ha.casabanana.casa
    service: http://192.168.X.X:8123
  
  # NEW: OpenClaw rule
  - hostname: josemar.casabanana.casa
    service: http://<DOCKER_VM_IP>:18789
  
  # Catch-all
  - service: http_status:404
```

Replace `<DOCKER_VM_IP>` with the IP address of the VM running your OpenClaw Docker container (e.g., `192.168.15.200`).

**4. Validate and Restart Cloudflared:**

```bash
# Validate the config
sudo cloudflared tunnel ingress validate

# Restart the service
sudo systemctl restart cloudflared
```

**5. Access the Web UI:**

Open your browser and navigate to:
```
https://josemar.casabanana.casa/__openclaw__/canvas/?token=YOUR_GATEWAY_AUTH_TOKEN
```

**Why This Works:**
- Cloudflare provides SSL termination (HTTPS)
- Browser sees a secure context (HTTPS origin)
- OpenClaw accepts the origin via `allowedOrigins`
- No "device identity" error since browser requirements are satisfied

### Security Notes

- The token is required when `gateway.bind` is set to "lan" (non-loopback)
- Never share your token publicly
- The UI provides full control over the bot - protect it accordingly
- Cloudflare Tunnel provides secure access without exposing ports publicly

## Best Practices

### Configuration Management
- Use environment variables for secrets (never commit to git)
- Keep `.env.example` updated with all required variables
- Document configuration changes in README.md
- **Verify path accessibility**: When configuration references file paths (avatars, certificates, data files, etc.), always verify those paths are accessible within the container by checking volume mounts in `docker-compose.yml` and the entrypoint script.

### Skill Development
- Always include `SKILL.md` with proper frontmatter
- Skills should read from stdin and output JSON to stdout
- Handle errors gracefully with meaningful error messages
- Include usage information in error responses
- Test skills both locally and in container

### Docker Usage
- Use named volumes for persistent data
- Keep images small by using multi-stage builds
- Implement health checks for service monitoring
- Use `.dockerignore` to exclude unnecessary files

### Logging and Monitoring
- Use appropriate log levels (debug, info, warn, error)
- Log important events and errors
- Monitor Docker volume usage: `docker volume inspect josemar-assistente_openclaw-workspace`
- Set up log rotation for production

### Security
- Never commit API keys or secrets
- Use `.env` file for sensitive data
- Restrict Telegram bot with user ID filtering
- Keep dependencies updated
- Use read-only mounts where appropriate

## Documentation Maintenance

When making changes to this repository, update the relevant AGENTS.md files:

**Update triggers by file:**

| File | Update When |
|------|-------------|
| Root AGENTS.md | Project structure, development commands, Docker deployment, testing procedures |
| skills/AGENTS.md | Adding/modifying skills, skill structure changes, new frontmatter fields |
| config/AGENTS.md | Configuration structure changes, new options, schema updates |
| .github/workflows/AGENTS.md | Workflow changes, CI/CD modifications |

**Cross-references:** Keep subdirectory AGENTS.md links in sync. When adding a new subdirectory with its own AGENTS.md, add a reference in the root file's Quick Navigation table.

## Git Workflow for Agents

### Git CLI Usage
- **Always use the `gh` CLI** for git operations when working in this repository
- The `gh` CLI provides better integration with GitHub workflows and is the preferred tool

### Commit and Push Behavior
- **ALWAYS ask the user** before executing `gh` commits and pushes
- Do NOT automatically commit or push changes using `gh` CLI without explicit user confirmation
- Exception: If the user explicitly stated in their instructions to "commit" or "push" without asking, then proceed directly
- When asking for confirmation, clearly state what files will be committed and what the commit message will be

Example confirmation prompt:
```
I'm ready to commit the following changes:
- Modified: config/openclaw.json
- Added: .gitea/workflows/test.yaml

Commit message: "Update configuration and add test workflow"

Should I proceed with commit and push? (yes/no)
```

## Additional Documentation

- **Skills Documentation**: `skills/AGENTS.md` - Detailed skills system documentation
- **Configuration Reference**: `config/AGENTS.md` - Complete OpenClaw configuration reference
- **OpenClaw Documentation**: https://docs.openclaw.dev
- **Z.AI API Documentation**: https://docs.z.ai
- **DeepSeek API Documentation**: https://api-docs.deepseek.com

## Support

For issues or questions:
1. Check logs: `docker-compose logs -f openclaw`
2. Validate configuration: Check JSON5 syntax
3. Review documentation in this file and subdirectories
4. Check OpenClaw documentation at https://docs.openclaw.dev
