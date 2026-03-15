# AGENTS.md

This file provides guidance to AI coding assistants when working with code in this repository.

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

## Development Commands

### Local Development

**Start the bot:**
```bash
# Start all services
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
nano config/openclaw.json5

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
# Use the deployment script
./deploy.sh

# Deploy specific branch
./deploy.sh main

# Deploy with custom environment
./deploy.sh main /path/to/custom.env
```

The deployment script:
1. Checks out to the specified branch
2. Pulls the latest changes
3. Rebuilds the Docker image
4. Restarts the services
5. Validates configuration

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

# Telegram user ID for pairing (optional)
TELEGRAM_USER_ID=123456789
```

See `.env.example` for template and detailed descriptions.

## Architecture

### Project Structure

```
josemar-assistente/
├── Dockerfile                  # Custom OpenClaw image with PDF support
├── docker-compose.yml          # Docker Compose deployment configuration
├── docker-entrypoint.sh        # Container entrypoint script
├── deploy.sh                   # Automated deployment script
├── .env.example               # Environment variables template
├── config/                     # OpenClaw configuration
│   └── openclaw.json5         # Main configuration (JSON5 format)
├── scripts/                    # Python scripts for skills
│   └── pdf_extractor.py       # PDF extraction implementation
├── skills/                     # Custom OpenClaw skills
│   └── pdf-extractor/          # PDF extraction skill
│       ├── SKILL.md           # Skill documentation
│       └── pdf-extractor      # Skill executable (bash wrapper)
├── workspace/                  # OpenClaw workspace (persistent data)
└── README.md                  # Project documentation
```

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
- **Location**: `config/openclaw.json5`
- **Features**: Environment variable expansion, modular configuration

**4. Docker Integration**
- **Volumes**: Configuration, workspace, and skills directories
- **Network**: Custom bridge network for service communication
- **Health Check**: Validates OpenClaw installation

### OpenClaw Configuration

**Main Configuration Sections:**

1. **Environment Variables**: API keys and secrets
   ```json5
   env: {
     ZAI_API_KEY: "${ZAI_API_KEY}",
     TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}",
     DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",
   }
   ```

2. **Model Providers**: LLM API configurations
   - **Z.AI**: Primary provider with GLM 4.7 model
   - **DeepSeek**: Optional provider with chat and reasoner models
   - **Custom Providers**: Can add OpenAI-compatible providers

3. **Agents**: AI agent configurations
   - **Default Agent**: "josemar" - Brazilian Portuguese assistant
   - **Models**: Primary model with fallback options
   - **Workspace**: Persistent data storage
   - **Identity**: Name, theme, emoji

4. **Channels**: Communication interfaces
   - **Telegram**: Bot token, policies, language settings
   - **Direct Message Policy**: "pairing" requires user authorization
   - **Group Policy**: "open" with mention requirement

5. **Skills**: Custom tools and extensions
   - **Enabled Skills**: List of active skills
   - **Skill Discovery**: Automatic loading from skills directory

6. **Prompts**: System prompts for agents
   - **Portuguese Prompts**: Brazilian Portuguese instructions
   - **Context**: Tools, personality, and working methods

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
- **Workspace**: `./workspace:/root/.openclaw/workspace` (persistent)
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

# Backup workspace
tar -czf workspace-backup-$(date +%Y%m%d).tar.gz workspace/

# Restore workspace
tar -xzf workspace-backup-YYYYMMDD.tar.gz

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

### Skill Structure

Each skill follows this structure:
```
skills/<skill-name>/
├── SKILL.md          # Documentation with frontmatter
└── <skill-name>      # Executable script
```

### Skill Frontmatter

```markdown
---
name: skill-name
description: Brief description of what the skill does
categories:
  - category1
  - category2
---
```

### Skill Development

**Creating a new skill:**

1. Create skill directory:
```bash
mkdir -p skills/my-skill
```

2. Create SKILL.md:
```markdown
---
name: my-skill
description: Description of my skill
categories:
  - category1
---
# My Skill

Documentation here...
```

3. Create executable:
```bash
#!/bin/bash
# Read JSON input from stdin
input=$(cat)

# Process input
# ...

# Output JSON result
echo '{"result": "success"}'
```

4. Make executable:
```bash
chmod +x skills/my-skill/my-skill
```

5. Update configuration in `config/openclaw.json5`:
```json5
skills: {
  entries: {
    "my-skill": {
      enabled: true,
    },
  },
}
```

6. Rebuild and restart:
```bash
docker-compose build
docker-compose up -d
```

### Existing Skills

**PDF Extractor**
- **Location**: `skills/pdf-extractor/`
- **Purpose**: Extract data from Brazilian credit card invoice PDFs
- **Input**: PDF file path or raw text (via stdin)
- **Output**: JSON with extracted expenses
- **Dependencies**: pymupdf

See `skills/AGENTS.md` for detailed skill documentation.

## Configuration Reference

### OpenClaw Configuration (JSON5)

**Complete Configuration Structure:**

```json5
{
  // Environment variables
  env: {
    ZAI_API_KEY: "${ZAI_API_KEY}",
    TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}",
    DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",
  },

  // Model providers
  models: {
    mode: "merge",
    providers: {
      deepseek: {
        baseUrl: "https://api.deepseek.com/v1",
        apiKey: "${DEEPSEEK_API_KEY}",
        api: "openai-completions",
        models: [...],
      },
    },
  },

  // Agents
  agents: {
    defaults: {
      workspace: "~/.openclaw/workspace",
      model: {
        primary: "zai/glm-4.7",
        fallbacks: ["deepseek/deepseek-reasoner"],
      },
    },
    list: [
      {
        id: "josemar",
        default: true,
        name: "Josemar",
        workspace: "~/.openclaw/workspace",
        model: "zai/glm-4.7",
        identity: {...},
        description: "Assistente pessoal em Português Brasileiro",
      },
    ],
  },

  // Channels
  channels: {
    telegram: {
      enabled: true,
      botToken: "${TELEGRAM_BOT_TOKEN}",
      useWebhook: false,
      dmPolicy: "pairing",
      groupPolicy: "open",
      language: "pt-BR",
    },
  },

  // Skills
  skills: {
    entries: {
      "pdf-extractor": {
        enabled: true,
      },
    },
  },

  // Prompts
  prompts: {
    "josemar": "System prompt in Portuguese...",
  },

  // Session management
  session: {
    scope: "per-sender",
    reset: {
      mode: "idle",
      idleMinutes: 60,
    },
    store: "~/.openclaw/agents/josemar/sessions/sessions.json",
  },

  // Logging
  logging: {
    level: "info",
    consoleLevel: "info",
    consoleStyle: "pretty",
    redactSensitive: "tools",
  },
}
```

See `config/AGENTS.md` for detailed configuration reference.

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
3. **Update Configuration**: Add skill to `config/openclaw.json5`
4. **Test Locally**: Test skill with Python scripts
5. **Test in Container**: Test in Docker environment
6. **Deploy**: Use `./deploy.sh` for production deployment
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
docker-compose exec openclaw env | sort
```

## Best Practices

### Configuration Management
- Use environment variables for secrets (never commit to git)
- Keep `.env.example` updated with all required variables
- Document configuration changes in README.md
- Use JSON5 comments to explain complex configurations

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
- Monitor disk usage (workspace can grow)
- Set up log rotation for production

### Security
- Never commit API keys or secrets
- Use `.env` file for sensitive data
- Restrict Telegram bot with user ID filtering
- Keep dependencies updated
- Use read-only mounts where appropriate

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
- Modified: config/openclaw.json5
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
