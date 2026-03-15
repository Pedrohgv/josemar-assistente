# Josemar Assistente - OpenClaw Bot

A self-hosted OpenClaw bot running in Docker with Telegram integration and PDF extraction capabilities.

## Features

- **OpenClaw Gateway**: Self-hosted AI agent gateway
- **Telegram Integration**: Native Telegram bot support
- **PDF Extraction**: Process Brazilian credit card invoice PDFs
- **GLM 4.7 Support**: Via Z.AI provider (built-in)
- **DeepSeek Support**: Via custom provider configuration
- **Brazilian Portuguese**: Native language interaction
- **Docker Deployment**: Easy deployment with Docker Compose

## Prerequisites

- Docker and Docker Compose
- Z.AI API key (for GLM 4.7 model)
- Telegram Bot Token (from @BotFather)
- DeepSeek API key (optional, for alternative LLM)

## Quick Start

### 1. Clone and Configure

```bash
cd repos/josemar-assistente
cp .env.example .env
# Edit .env with your API keys
```

### 2. Build and Run

```bash
./deploy.sh
```

Or manually:

```bash
docker compose build
docker compose up -d
```

**Note**: Use `docker compose` (with space) for Docker Compose V2, or `docker compose` (with hyphen) for V1. The deployment script will detect which is available.

### 3. Check Logs

```bash
docker compose logs -f
```

### 4. Interact with Bot

- Start a conversation with your Telegram bot
- Send a PDF credit card invoice for processing
- Ask questions in Brazilian Portuguese

## Configuration

### Environment Variables

Create a `.env` file with:

```bash
# Z.AI API Key for GLM 4.7 (get from https://z.ai)
ZAI_API_KEY=your_zai_api_key_here

# Telegram Bot Token (get from @BotFather)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# DeepSeek API Key (optional, get from https://api.deepseek.com)
DEEPSEEK_API_KEY=your_deepseek_api_key_here

# OpenClaw log level (debug, info, warn, error)
OPENCLAW_LOG_LEVEL=info
```

### OpenClaw Configuration

The main configuration is in `config/openclaw.json5` (JSON5 format):

```json5
{
  env: {
    ZAI_API_KEY: "${ZAI_API_KEY}",
    TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}",
    DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",
  },
  models: {
    mode: "merge",
    providers: {
      deepseek: {
        baseUrl: "https://api.deepseek.com/v1",
        apiKey: "${DEEPSEEK_API_KEY}",
        api: "openai-completions",
        models: [
          {
            id: "deepseek-chat",
            name: "DeepSeek Chat",
            // ... model configuration
          },
        ],
      },
    },
  },
  agents: {
    defaults: {
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
        // ... agent configuration
      },
    ],
  },
  channels: {
    telegram: {
      enabled: true,
      botToken: "${TELEGRAM_BOT_TOKEN}",
      dmPolicy: "pairing",
      language: "pt-BR",
    },
  },
  skills: {
    entries: {
      "pdf-extractor": { enabled: true },
    },
  },
}
```

See `config/AGENTS.md` for complete configuration reference.

## Skills

### PDF Extractor

Extracts data from Brazilian credit card invoice PDFs:

- Extracts total expense amount
- Parses individual transactions
- Returns structured JSON data

Usage in Telegram:
```
Send a PDF file or ask: "Extraia os dados desta fatura"
```

### Custom Skills

Add your own skills in `skills/` directory:

1. Create skill folder with `SKILL.md` (including YAML frontmatter)
2. Add executable script
3. Enable in `skills.entries` configuration

Example skill structure:
```
skills/my-skill/
├── SKILL.md          # Required: YAML frontmatter + documentation
└── my-skill          # Executable script (any language)
```

See `skills/AGENTS.md` for detailed skill development guide.

## Project Structure

```
josemar-assistente/
├── Dockerfile              # Custom OpenClaw image
├── docker compose.yml      # Deployment configuration
├── .env.example           # Environment variables template
├── deploy.sh              # Automated deployment script
├── AGENTS.md              # Root project documentation
├── config/
│   ├── AGENTS.md          # Configuration reference
│   └── openclaw.json5    # OpenClaw configuration
├── scripts/
│   └── pdf_extractor.py   # PDF extraction script
├── skills/
│   ├── AGENTS.md          # Skills development guide
│   └── pdf-extractor/     # PDF extraction skill
│       ├── SKILL.md       # Skill documentation
│       └── pdf-extractor  # Skill executable
├── workspace/             # OpenClaw workspace (mounted)
├── test-config.py         # Configuration validation
├── test-pdf-skill.sh     # PDF skill testing
└── README.md              # This file
```

## Development

### Building the Image

```bash
docker compose build
```

### Viewing Logs

```bash
# All logs
docker compose logs

# Follow logs
docker compose logs -f

# Specific service
docker compose logs openclaw
```

### Stopping the Service

```bash
docker compose down
```

### Updating Configuration

1. Edit `config/openclaw.json5`
2. Restart service:
   ```bash
   docker compose restart
   ```

### Testing Configuration

```bash
# Validate JSON5 syntax
python3 test-config.py

# Test PDF extraction skill
./test-pdf-skill.sh
```

## Model Providers

### Z.AI (GLM 4.7)

- **Built-in provider**: No additional configuration needed
- **Model**: `zai/glm-4.7`
- **API Key**: Set `ZAI_API_KEY` environment variable
- **Features**: Tool calling, reasoning mode

### DeepSeek

- **Custom provider**: Configured in `models.providers`
- **Models**: `deepseek-chat`, `deepseek-reasoner`
- **API Key**: Set `DEEPSEEK_API_KEY` environment variable
- **Features**: OpenAI-compatible API, tool calling

## Troubleshooting

### Common Issues

1. **Missing API keys**: Ensure `.env` file exists with correct keys
2. **Configuration errors**: Validate JSON5 syntax with `python3 test-config.py`
3. **Skill not working**: Check OpenClaw logs for skill execution errors
4. **Telegram bot not responding**: Verify bot token and check privacy settings
5. **Provider authentication**: Verify API keys are set and valid

### Checking Logs

```bash
# Full logs
docker compose logs

# Follow logs
docker compose logs -f

# Recent logs
docker compose logs --tail=50
```

### Configuration Validation

The configuration file is in JSON5 format, which allows:
- Comments (single-line `//` and multi-line `/* */`)
- Trailing commas
- Environment variable substitution (`${VAR_NAME}`

If the service fails to start, check:
1. JSON5 syntax (comments, trailing commas)
2. Environment variables are set correctly
3. Required sections exist (`env`, `agents`, `channels`)

## Architecture

### OpenClaw Gateway

- **Single Process**: Gateway routes messages between channels and agents
- **Hot Reload**: Configuration changes picked up without restart (in hybrid mode)
- **Session Management**: Per-user conversations with configurable reset policies
- **Multi-Agent**: Support for multiple agents with different capabilities

### Docker Deployment

- **Official Image**: Based on `ghcr.io/openclaw/openclaw:latest`
- **Custom Layer**: Adds Python and pymupdf for PDF processing
- **Volume Mounts**:
  - Config: `./config:/root/.openclaw-source` (copied by entrypoint)
  - Workspace: `./workspace:/root/.openclaw/workspace` (persistent)
- **Environment**: API keys and configuration passed via environment variables

### Skills System

Three load locations (in precedence order):
1. **Workspace**: `<workspace>/skills/` (highest priority)
2. **Managed**: `~/.openclaw/skills/` (baked into image)
3. **Bundled**: Built-in OpenClaw skills (lowest priority)

## Documentation

- **AGENTS.md**: Root project documentation for AI assistants
- **config/AGENTS.md**: Complete configuration reference
- **skills/AGENTS.md**: Skills development guide

## License

MIT
