# AGENTS.md

This file provides guidance to AI coding assistants when working with OpenClaw configuration in this repository.

## Configuration Overview

OpenClaw uses JSON5 format for configuration, which is a superset of JSON that allows comments, trailing commas, and more readable syntax. The main configuration file is `openclaw.json`.

The system supports the GLM family of models (e.g., GLM-5, GLM-4.7, GLM-5-Turbo) via Z.AI provider, with support for DeepSeek and other OpenAI-compatible providers. Any provider following the OpenAI completions API can be configured using the `{provider-id}/{model-id}` pattern.

**Configuration Characteristics:**
- **Format**: JSON5 (JSON with extensions)
- **Location**: `config/openclaw.json`
- **Environment Variable Expansion**: `${VAR}` syntax
- **Modular Structure**: Organized into logical sections
- **Validation**: OpenClaw validates configuration on startup

**Important Notes:**
- The file extension must be `.json`, not `.json5`
- While the file supports JSON5 features (comments, trailing commas, unquoted keys), some JSON5 features do NOT work:
  - ❌ **Backtick strings** (`` ` ``) - Use escaped newlines (`\n`) instead
  - ❌ **ES6 Unicode escapes** (`\u{1F915}`) - Use actual emoji characters (🤕) instead
- Always use `openclaw doctor --fix` after editing to validate the configuration

## Essential Operations

### Configuration Management

**Edit configuration:**
```bash
nano config/openclaw.json
```

**Validate JSON5 syntax:**
```bash
# Using jq
cat config/openclaw.json | jq .

# Using OpenClaw
docker-compose run --rm openclaw openclaw --validate-config
```

**Apply configuration changes:**
```bash
# Restart the service (no rebuild needed)
docker-compose restart openclaw

# Or for major changes
docker-compose down && docker-compose up -d
```

**Check configuration in container:**
```bash
# View loaded config
docker-compose exec openclaw cat /root/.openclaw/openclaw.json

# List environment variables
docker-compose exec openclaw env | sort
```

### Service Operations

**Start/stop/restart:**
```bash
# Start services
docker-compose up -d

# Stop services
docker-compose down

# Restart after config changes
docker-compose restart openclaw

# View logs
docker-compose logs -f openclaw
```

**Check service status:**
```bash
docker-compose ps
docker-compose exec openclaw openclaw --version
```

### Environment Variables

**Create .env file:**
```bash
cp .env.example .env
nano .env
```

**Required variables:**
- `ZAI_API_KEY` - Primary LLM provider
- `TELEGRAM_BOT_TOKEN` - Bot authentication
- `TELEGRAM_ENABLED` - Enable/disable Telegram channel (`true`/`false`, default: `true`)
- `DEEPSEEK_API_KEY` - Optional fallback provider
- `GATEWAY_AUTH_PASSWORD` - Web UI access password (HTTP Basic Auth)
- `PEDRO_TELEGRAM_ID` - Primary user ID

**Local Testing:**
Set `TELEGRAM_ENABLED=false` in `.env` to disable Telegram for local testing. This prevents conflicts with the production bot while allowing full testing via the Web UI.

**Reload after .env changes:**
```bash
docker-compose restart openclaw
```

## JSON5 Format

### Key Features

**Comments:**
```json5
{
  // This is a single-line comment
  multi_line /* This is a multi-line comment */
  key: "value"
}
```

**Trailing Commas:**
```json5
{
  "key1": "value1",  // Trailing comma is allowed
  "key2": "value2",
}
```

**Unquoted Keys:**
```json5
{
  name: "Josemar",  // Keys don't need quotes
  age: 30
}
```

**Multiline Strings:**
```json5
{
  description: "This is a multiline string\nthat spans multiple lines\nwith escaped newlines."
}
```

**Note:** Backtick strings (`` ` ``) and ES6 Unicode escapes (`\u{1F915}`) are NOT supported. Use actual characters and escaped newlines instead.

### Environment Variable Expansion

Use `${VARIABLE_NAME}` syntax to reference environment variables:

```json5
{
  env: {
    API_KEY: "${ZAI_API_KEY}",
    BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}"
  }
}
```

**Notes:**
- Variables are expanded at startup
- Missing variables are replaced with empty strings
- Use `.env` file to set variables
- Never commit `.env` file with real values

## Configuration Structure

### Complete Configuration Template

```json5
{
  // Environment variables for API keys and secrets
  env: {
    ZAI_API_KEY: "${ZAI_API_KEY}",
    TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}",
    DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",
    PEDRO_TELEGRAM_ID: "${PEDRO_TELEGRAM_ID}",
  },

  // Gateway configuration
  gateway: {
    mode: "local",
    bind: "lan",
    port: 18789,
    auth: {
      mode: "password",
      password: "${GATEWAY_AUTH_PASSWORD}",
    },
  },

  // Model providers configuration
  models: {
    mode: "merge",
    providers: {
      deepseek: {
        baseUrl: "https://api.deepseek.com/v1",
        apiKey: "${DEEPSEEK_API_KEY}",
        api: "openai-completions",
        models: [...]
      },
      zai: {
        baseUrl: "https://api.z.ai/api/paas/v4",
        apiKey: "${ZAI_API_KEY}",
        api: "openai-completions",
        models: [...]
      }
    }
  },

  // Agent configurations
  agents: {
    defaults: {
      workspace: "~/.openclaw/workspace",
      model: {
        // Use any provider/model pattern: {provider-id}/{model-id}
        // Example: zai/glm-5, deepseek/deepseek-reasoner, custom-provider/model
        primary: "zai/glm-5",
        fallbacks: ["zai/glm-4.7", "deepseek/deepseek-reasoner"]
      },
      models: {
        // Model aliases - use {provider}/{model-id} pattern
        "zai/glm-5": { alias: "GLM 5" },
        "zai/glm-4.7": { alias: "GLM 4.7" },
        "zai/glm-5-turbo": { alias: "GLM 5 Turbo" },
        "deepseek/deepseek-reasoner": { alias: "DeepSeek Reasoner" },
        "deepseek/deepseek-chat": { alias: "DeepSeek Chat" }
      }
    },
    list: [
      {
        id: "josemar",
        default: true,
        name: "Josemar",
        workspace: "~/.openclaw/workspace",
        model: "zai/glm-5",
        identity: {
          name: "Josemar",
          theme: "helpful assistant",
          emoji: "🤕"
        }
      }
    ]
  },

  // Channel configurations
  channels: {
    telegram: {
      enabled: true,
      botToken: "${TELEGRAM_BOT_TOKEN}",
      dmPolicy: "allowlist",
      allowFrom: [
        "${PEDRO_TELEGRAM_ID}",
      ]
    }
  },

  // Skills configuration
  skills: {
    entries: {
      "finance-assistant": {
        enabled: true
      },
      "gogcli-tables": {
        enabled: true
      },
      "workspace-sync": {
        enabled: true
      }
    }
  },

  // Session configuration
  session: {
    scope: "per-sender",
    reset: {
      mode: "idle",
      idleMinutes: 60
    },
    store: "~/.openclaw/agents/josemar/sessions/sessions.json"
  },

  // Logging configuration
  logging: {
    level: "info",
    consoleLevel: "info",
    consoleStyle: "pretty",
    redactSensitive: "tools"
  }
}
```

## Configuration Sections

### 1. Environment Variables

Define environment variables for secrets and configuration:

```json5
env: {
  // API Keys
  ZAI_API_KEY: "${ZAI_API_KEY}",
  TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}",
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",
  
  // Telegram User IDs (see Channels section for usage pattern)
  PEDRO_TELEGRAM_ID: "${PEDRO_TELEGRAM_ID}",
}
```

**Best Practices:**
- Use environment variables for all secrets
- Reference them in configuration sections
- Never commit actual values to git
- Document required variables in `.env.example`

### 2. Model Providers

Supports multiple LLM providers through a unified interface. Configure any OpenAI-compatible API.

**Example: Z.AI provider for GLM models:**
```json5
models: {
  mode: "merge",
  providers: {
    zai: {
      baseUrl: "https://api.z.ai/api/paas/v4",
      apiKey: "${ZAI_API_KEY}",
      api: "openai-completions",
      models: [
        {
          id: "glm-5",
          name: "GLM 5",
          reasoning: false,
          input: ["text"],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 200000,
          maxTokens: 8192,
        },
        {
          id: "glm-4.7",
          name: "GLM 4.7",
          reasoning: false,
          input: ["text"],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 128000,
          maxTokens: 8192,
        },
        {
          id: "glm-5-turbo",
          name: "GLM 5 Turbo",
          reasoning: false,
          input: ["text"],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 128000,
          maxTokens: 4096,
        }
      ]
    }
  }
}
```

**Example: DeepSeek provider:**
```json5
models: {
  mode: "merge",  // Options: "merge", "replace"
  providers: {
    // DeepSeek provider (OpenAI-compatible)
    deepseek: {
      baseUrl: "https://api.deepseek.com/v1",
      apiKey: "${DEEPSEEK_API_KEY}",
      api: "openai-completions",
      models: [
        {
          id: "deepseek-chat",
          name: "DeepSeek Chat",
          reasoning: false,
          input: ["text"],
          cost: {
            input: 0,
            output: 0,
            cacheRead: 0,
            cacheWrite: 0,
          },
          contextWindow: 128000,
          maxTokens: 8192,
        },
        {
          id: "deepseek-reasoner",
          name: "DeepSeek Reasoner",
          reasoning: true,
          input: ["text"],
          cost: {
            input: 0,
            output: 0,
            cacheRead: 0,
            cacheWrite: 0,
          },
          contextWindow: 128000,
          maxTokens: 64000,
        }
      ]
    }
  }
}
```

**Provider Configuration Fields:**
- `baseUrl`: API endpoint URL
- `apiKey`: Environment variable reference
- `api`: API type (e.g., "openai-completions")
- `models`: Array of model definitions

**Model Definition Fields:**
- `id`: Unique model identifier
- `name`: Human-readable name
- `reasoning`: Boolean, if model supports reasoning
- `input`: Array of input types
- `cost`: Cost information (input, output, cache)
- `contextWindow`: Maximum context size
- `maxTokens`: Maximum output tokens

**Adding Custom Providers:**

Any provider following the OpenAI completions API can be added. Use the pattern `{provider-id}/{model-id}` when referencing models:

```json5
providers: {
  // Use any provider-id (e.g., custom-provider, anthropic, openai)
  custom-provider: {
    baseUrl: "https://api.custom.com/v1",
    apiKey: "${CUSTOM_API_KEY}",
    api: "openai-completions",
    models: [
      {
        // Model ID - use this with provider-id: {provider-id}/{model-id}
        id: "custom-model",
        name: "Custom Model",
        reasoning: false,
        input: ["text"],
        cost: { input: 0.001, output: 0.002, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 64000,
        maxTokens: 4096
      }
    ]
  }
}
```

**Usage:** Reference models as `{provider-id}/{model-id}`, e.g., `custom-provider/custom-model`

### 3. Agents

Configure AI agents with their settings. The model references use the pattern `{provider-id}/{model-id}`, allowing any OpenAI-compatible provider to be used.

```json5
agents: {
  defaults: {
    // Default workspace location
    workspace: "~/.openclaw/workspace",
    
    // Default model configuration
    // Use any provider/model pattern: {provider-id}/{model-id}
    // Example: zai/glm-5, deepseek/deepseek-reasoner
    model: {
      primary: "zai/glm-5",
      fallbacks: ["zai/glm-4.7", "deepseek/deepseek-reasoner"]
    },
    
    // Model aliases for easier reference
    // Format: "{provider-id}/{model-id}": { alias: "Display Name" }
    models: {
      "zai/glm-5": {
        alias: "GLM 5"
      },
      "zai/glm-4.7": {
        alias: "GLM 4.7"
      },
      "zai/glm-5-turbo": {
        alias: "GLM 5 Turbo"
      },
      "deepseek/deepseek-reasoner": {
        alias: "DeepSeek Reasoner"
      },
      "deepseek/deepseek-chat": {
        alias: "DeepSeek Chat"
      }
    }
  },
  
  list: [
    {
      id: "josemar",
      default: true,
      name: "Josemar",
      workspace: "~/.openclaw/workspace",
      model: "zai/glm-5",
      identity: {
        name: "Josemar",
        theme: "helpful assistant",
        emoji: "🤖"
      }
    }
  ]
}
```

**Agent Fields:**
- `id`: Unique agent identifier
- `default`: Boolean, if this is the default agent
- `name`: Display name
- `workspace`: Path to workspace directory
- `model`: Primary model to use
- `identity`: Agent personality (name, theme, emoji)

**Adding a New Agent:**

```json5
list: [
  {
    id: "specialist",
    default: false,
    name: "Specialist Agent",
    workspace: "~/.openclaw/workspace",
    model: "deepseek/deepseek-reasoner",
    identity: {
      name: "Specialist",
      theme: "technical expert",
      emoji: "🔬"
    }
  }
]
```

### 4. Channels

Configure communication channels:

```json5
channels: {
  telegram: {
    enabled: true,
    botToken: "${TELEGRAM_BOT_TOKEN}",
    dmPolicy: "allowlist",  // "pairing" | "allowlist" | "open" | "disabled"
    // User IDs are loaded from environment variables (see below)
    allowFrom: [
      "${PEDRO_TELEGRAM_ID}",
      // Add more users by adding their env var: "${ALICE_TELEGRAM_ID}",
    ],
  }
}
```

**Telegram User Access Configuration:**

Users are managed via environment variables and explicitly listed in `allowFrom`:

1. **Add the user's Telegram ID to `.env`:**
   ```bash
   # Primary user (required)
   PEDRO_TELEGRAM_ID=123456789
   
   # Additional users (optional)
   ALICE_TELEGRAM_ID=987654321
   BOB_TELEGRAM_ID=555666777
   ```

2. **Reference them in `config/openclaw.json`:**
   ```json5
   allowFrom: [
     "${PEDRO_TELEGRAM_ID}",
     "${ALICE_TELEGRAM_ID}",  // Uncomment to add
     // "${BOB_TELEGRAM_ID}",  // Add more as needed
   ]
   ```

**Direct Message Policies:**
- `allowlist`: Only users in `allowFrom` can send messages (recommended)
- `pairing`: Users must pair with the bot first
- `open`: Anyone can send messages (not recommended for production)
- `disabled`: DMs are disabled

**Why this pattern?**
- Explicit: Every allowed user is visible in one place (the config file)
- Flexible: Easy to add/remove users without touching code
- Safe: No Docker rebuild needed when changing users (just restart)

**Important:** After changing the configuration file, you only need to restart the container:
```bash
docker compose restart
```
No Docker image rebuild is required!
- `mention`: Bot only responds when mentioned
- `closed`: Bot doesn't respond to groups

### 5. Skills

Configure custom skills. OpenClaw auto-discovers skills from the workspace directory.

```json5
skills: {
  // Keep bundled defaults disabled; load workspace/state skills only
  allowBundled: [],
  // Auto-refresh skills when files change
  load: {
    watch: true,
    watchDebounceMs: 250,
  },
  // Per-skill overrides only (optional)
  entries: {}
}
```

**Skill Configuration Fields:**
- `allowBundled`: optional allowlist for bundled skills (`[]` disables bundled skills)
- `load.watch`: auto-refresh discovered skills
- `load.watchDebounceMs`: debounce for file watcher events
- `entries`: per-skill override map (optional)
  - `enabled`: set `false` to disable a discovered skill

**Unified Skills System:**

All skills live in `agent-state/skills/` and are versioned in the agent state git repo. OpenClaw auto-discovers them from the workspace directory.

- **Location**: `/root/.openclaw/workspace/skills/` (inside container)
- **Source**: `agent-state/skills/` in the repository
- **Deployment**: Synced via git on container start
- **Persistence**: Changes are auto-committed and pushed back to the remote repo

See `agent-state/skills/AGENTS.md` for detailed skill development guide.

**Adding a New Skill:**

Just add the skill folder to `agent-state/skills/` (or `<workspace>/skills/`). No `openclaw.json` edit is required unless you want per-skill overrides.

**Note:** Skills are automatically discovered from the workspace directory. `entries` is only for overrides like `enabled: false`, `env`, or skill-specific config.

### 6. Agent Prompts and Personality

**Important:** Agent prompts and personality are **NOT** configured in `openclaw.json`. Instead, they are defined in **workspace markdown files** that OpenClaw reads on startup.

**Workspace Files:**
- `workspace/SOUL.md` - Core personality, boundaries, and "who you are"
- `workspace/AGENTS.md` - Workspace-specific instructions and behavior
- `workspace/USER.md` - Information about the user (optional)
- `workspace/MEMORY.md` - Long-term memory and continuity

**Key Points:**
- These files live in the `workspace/` directory (inside Docker volume)
- They ARE versioned in the agent-state git repo (private repo)
- Only files listed in `.sync-manifest` are committed (security-first)
- OpenClaw reads these files on startup to configure the agent
- Changes to these files require a container restart to take effect

**Example:** See `workspace/SOUL.md` in your local workspace for the bot's current personality configuration.

**Note:** Agent prompts and personality are configured in workspace markdown files (SOUL.md, AGENTS.md), not in the JSON configuration.

### 7. Session Management

Configure session behavior:

```json5
session: {
  scope: "per-sender",
  reset: {
    mode: "idle",
    idleMinutes: 60
  },
  store: "~/.openclaw/agents/josemar/sessions/sessions.json"
}
```

**Session Fields:**
- `scope`: Session scope ("per-sender", "global")
- `reset.mode`: Reset mode ("idle", "manual", "never")
- `reset.idleMinutes`: Idle time before reset (if mode is "idle")
- `store`: Path to session storage file

**Session Scopes:**
- `per-sender`: Each user has their own session
- `global`: All users share the same session

**Reset Modes:**
- `idle`: Reset after idle time
- `manual`: Only reset manually
- `never`: Never reset sessions

### 8. Logging

Configure logging behavior:

```json5
logging: {
  level: "info",
  consoleLevel: "info",
  consoleStyle: "pretty",
  redactSensitive: "tools"
}
```

**Logging Fields:**
- `level`: Global log level (debug, info, warn, error)
- `consoleLevel`: Console log level
- `consoleStyle`: Console style ("pretty", "json")
- `redactSensitive`: What to redact ("tools", "all", "none")

**Log Levels:**
- `debug`: Detailed debugging information
- `info`: General informational messages
- `warn`: Warning messages
- `error`: Error messages only

**Console Styles:**
- `pretty`: Human-readable format
- `json`: Structured JSON format

## Configuration Management

### Editing Configuration

**1. Edit configuration file:**
```bash
nano config/openclaw.json
```

**2. Validate JSON5 syntax:**
```bash
# Use docker-compose to validate
docker-compose run --rm openclaw sh -c "cat /root/.openclaw/openclaw.json5 | jq ."
```

**3. Test configuration:**
```bash
# Check if OpenClaw accepts the configuration
docker-compose run --rm openclaw openclaw --validate-config
```

**4. Apply changes:**
```bash
# Restart the service
docker-compose restart openclaw
```

### Environment Variables

**1. Create `.env` file:**
```bash
cp .env.example .env
nano .env
```

**2. Set required variables:**
```bash
ZAI_API_KEY=your_zai_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
DEEPSEEK_API_KEY=your_deepseek_api_key_here
```

**3. Verify variables are loaded:**
```bash
docker-compose exec openclaw env | grep -E "ZAI|TELEGRAM|DEEPSEEK"
```

**4. Restart to apply:**
```bash
docker-compose restart openclaw
```

### Configuration Best Practices

**1. Use Comments:**
```json5
{
  // Primary model for the Josemar agent (use {provider}/{model-id} pattern)
  // Fallback to GLM-4.7 then DeepSeek if Z.AI is unavailable
  model: {
    primary: "zai/glm-5",
    fallbacks: ["zai/glm-4.7", "deepseek/deepseek-reasoner"]
  }
}
```

**2. Group Related Settings:**
```json5
{
  // Telegram Configuration
  channels: {
    telegram: {
      enabled: true,
      botToken: "${TELEGRAM_BOT_TOKEN}",
      language: "pt-BR"
    }
  }
}
```

**3. Use Multiline Strings for Prompts:**
```json5
{
  prompts: {
    "josemar": `
Você é um assistente em Português Brasileiro.

Seja amigável e prestativo.
    `
  }
}
```

**4. Document Changes:**
```json5
{
  // Added custom provider on 2025-01-15
  // See: https://docs.custom-api.com
  customProvider: {...}
}
```

## Configuration Validation

### JSON5 Validation

**Using jq:**
```bash
# Validate JSON5 syntax
cat config/openclaw.json | jq .

# Check for specific field
cat config/openclaw.json | jq '.agents.list[0].id'
```

**Using OpenClaw:**
```bash
# Validate configuration
docker-compose run --rm openclaw openclaw --validate-config

# Show configuration
docker-compose run --rm openclaw openclaw config show
```

### Environment Variable Validation

**Check required variables:**
```bash
# List all environment variables
docker-compose exec openclaw env | sort

# Check specific variables
docker-compose exec openclaw env | grep ZAI_API_KEY
docker-compose exec openclaw env | grep TELEGRAM_BOT_TOKEN
```

**Test variable expansion:**
```bash
# Create test configuration
cat <<EOF > test-config.json5
{
  test: "${TEST_VAR}",
  missing: "${MISSING_VAR}"
}
EOF

# Parse and check
echo "TEST_VAR=value" | docker-compose run --rm -T openclaw sh -c "export \$(cat) && cat test-config.json5 | jq ."
```

### Model Validation

**Test model connectivity:**
```bash
# Test Z.AI API (use model ID matching the provider's model list)
# Note: Model ID should match the pattern configured in providers
curl -X POST "https://api.z.ai/v1/chat/completions" \
  -H "Authorization: Bearer ${ZAI_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model": "glm-5", "messages": [{"role": "user", "content": "Test"}]}'

# Test DeepSeek API
curl -X POST "https://api.deepseek.com/v1/chat/completions" \
  -H "Authorization: Bearer ${DEEPSEEK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-chat", "messages": [{"role": "user", "content": "Test"}]}'
```

**Check model in OpenClaw:**
```bash
# List available models
docker-compose exec openclaw openclaw models list

# Test model (use {provider}/{model-id} pattern)
docker-compose exec openclaw openclaw models test zai/glm-5
```

## Configuration Examples

### Adding a New Provider

**1. Define provider in configuration:**
```json5
providers: {
  anthropic: {
    baseUrl: "https://api.anthropic.com/v1",
    apiKey: "${ANTHROPIC_API_KEY}",
    api: "anthropic-completions",
    models: [
      {
        id: "claude-3-opus",
        name: "Claude 3 Opus",
        reasoning: true,
        toolCall: true,
        input: ["text", "image"],
        cost: { input: 0.015, output: 0.075, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 200000,
        maxTokens: 4096
      }
    ]
  }
}
```

**2. Add model to agent:**
```json5
models: {
  "anthropic/claude-3-opus": {
    alias: "Claude 3 Opus"
  }
}
```

**3. Use in agent:**
```json5
model: {
  primary: "anthropic/claude-3-opus",
  fallbacks: ["zai/glm-4.7"]
}
```

**4. Set API key:**
```bash
echo "ANTHROPIC_API_KEY=your_key_here" >> .env
```

**5. Restart and test:**
```bash
docker-compose restart openclaw
docker-compose exec openclaw openclaw models test anthropic/claude-3-opus
```

### Configuring Multiple Agents

```json5
agents: {
  list: [
    {
      id: "josemar",
      default: true,
      name: "Josemar",
      model: "zai/glm-5",
      workspace: "~/.openclaw/workspace/josemar",
      description: "Assistente pessoal em Português"
    },
    {
      id: "assistant-en",
      default: false,
      name: "Assistant EN",
      model: "deepseek/deepseek-chat",
      workspace: "~/.openclaw/workspace/assistant-en",
      description: "English speaking assistant"
    },
    {
      id: "coder",
      default: false,
      name: "Coder",
      model: "anthropic/claude-3-opus",
      workspace: "~/.openclaw/workspace/coder",
      description: "Coding assistant"
    }
  ]
}
```

### Adding Skills with Configuration

```json5
skills: {
  entries: {
    "finance-assistant": {
      enabled: true,
      config: {
        maxFileSize: "10MB",
        supportedFormats: ["pdf"]
      }
    },
    "web-scraper": {
      enabled: true,
      config: {
        timeout: 30,
        maxPages: 10
      }
    }
  }
}
```

### Configuring Multiple Channels

**Note:** Currently only Telegram is fully implemented. Discord and Slack are shown below as examples of how to configure multiple channels.

```json5
channels: {
  telegram: {
    enabled: true,
    botToken: "${TELEGRAM_BOT_TOKEN}",
    language: "pt-BR"
  },
  discord: {
    enabled: true,
    botToken: "${DISCORD_BOT_TOKEN}",
    language: "en-US"
  },
  slack: {
    enabled: false,
    botToken: "${SLACK_BOT_TOKEN}"
  }
}
```

## Troubleshooting

### Configuration Errors

**Error: "Configuration file not found"**
```bash
# Check if config directory is mounted
docker-compose exec openclaw ls -la /root/.openclaw/

# Check if configuration file exists
docker-compose exec openclaw ls -la /root/.openclaw/openclaw.json5

# Rebuild container
docker-compose down
docker-compose build
docker-compose up -d
```

**Error: "Invalid JSON5 syntax"**
```bash
# Validate syntax with jq
cat config/openclaw.json | jq .

# Check for syntax errors
# - Missing quotes
# - Trailing commas in wrong places
# - Unclosed brackets/braces
# - Invalid escape sequences

# Fix and restart
nano config/openclaw.json
docker-compose restart openclaw
```

**Error: "Environment variable not found"**
```bash
# Check if variable is set
docker-compose exec openclaw env | grep VARIABLE_NAME

# Add to .env if missing
echo "VARIABLE_NAME=value" >> .env

# Restart to apply
docker-compose restart openclaw
```

### Model Connection Errors

**Error: "Failed to connect to model provider"**
```bash
# Check API key
docker-compose exec openclaw env | grep API_KEY

# Test API directly
curl -X POST "https://api.provider.com/v1/chat/completions" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model": "model-id", "messages": [{"role": "user", "content": "Test"}]}'

# Check network connectivity
docker-compose exec openclaw ping -c 3 api.provider.com

# Verify provider configuration
docker-compose exec openclaw openclaw config show | grep -A 10 providers
```

**Error: "Model not found"**
```bash
# List available models
docker-compose exec openclaw openclaw models list

# Check model ID in configuration
cat config/openclaw.json | jq '.models.providers'

# Verify model is in provider's model list
```

### Skill Loading Errors

**Error: "Skill not found"**
```bash
# Check skill directory
docker-compose exec openclaw ls -la /root/.openclaw/workspace/skills/

# Check skill configuration
cat config/openclaw.json | jq '.skills.entries'
```

**Error: "Skill execution failed"**
```bash
# Test skill manually
echo '{"test": "data"}' | docker-compose exec -T openclaw /root/.openclaw/workspace/skills/skill-name/skill-name

# Check skill permissions
docker-compose exec openclaw ls -la /root/.openclaw/workspace/skills/skill-name/

# Check skill dependencies
docker-compose exec openclaw python3 -c "import required_module"
```

**Error: "Wrong skill version loaded"**
```bash
# Check skill info
docker-compose exec openclaw openclaw skills info skill-name

# List all available skills
docker-compose exec openclaw openclaw skills list

# Check git sync status
docker compose logs openclaw | grep workspace-sync
```

## Web UI Access

The OpenClaw Gateway provides a web interface for managing the bot.

### Access URL

**Local:**
The browser will prompt for HTTP Basic Auth. Enter:
- Username: `operator` (or any username - OpenClaw ignores it)
- Password: Your `GATEWAY_AUTH_PASSWORD` from `.env`

Or access via URL:
```
http://operator:YOUR_GATEWAY_AUTH_PASSWORD@localhost:18789/
```

**Remote (via Cloudflare Tunnel or similar):**
```
https://operator:YOUR_GATEWAY_AUTH_PASSWORD@your-domain.com/
```

### Setup

**1. Generate authentication password:**
```bash
openssl rand -hex 32
```

**2. Add to `.env`:**
```bash
GATEWAY_AUTH_PASSWORD=your-secure-password-here
```

**3. Update `config/openclaw.json`:**
```json5
gateway: {
  controlUi: {
    allowedOrigins: [
      "http://localhost:18789",
      "https://your-domain.com",
    ],
  },
}
```

**4. Restart service:**
```bash
docker-compose restart openclaw
```

### Security

- Token required when `gateway.bind` is set to "lan" (non-loopback)
- Never share your token publicly
- UI provides full bot control - protect access accordingly

### Troubleshooting

**Issue: "Pairing Required" Error**

When accessing the Web UI for the first time from a browser, you may see:
- "disconnected (1008): pairing required"
- "unauthorized: device token mismatch"

**Solution:**
OpenClaw requires a one-time device pairing approval for security. Run these commands:

```bash
# List pending devices
docker-compose exec openclaw openclaw devices list

# Approve the browser device (use the requestId from the list)
docker-compose exec openclaw openclaw devices approve <requestId>
```

Notes:
- Once approved, the device is remembered in the workspace volume
- You won't need to re-pair unless you clear browser data or use a different browser profile
- The CLI uses `127.0.0.1` and is auto-approved
- Browser connections via `localhost` require explicit approval

**Issue: "Unauthorized" After Pairing**

If you see "unauthorized" even after pairing, the browser may have a stale device token. Try:
1. Clear browser localStorage for `localhost:18789`
2. Use Incognito/Private mode
3. Or approve the new pairing request that appears

## Additional Resources

- **OpenClaw Documentation**: https://docs.openclaw.dev
- **JSON5 Format**: https://json5.org
- **Z.AI API Documentation**: https://docs.z.ai
- **DeepSeek API Documentation**: https://api-docs.deepseek.com
- **Telegram Bot API**: https://core.telegram.org/bots/api

## Support

For configuration issues:
1. Validate JSON5 syntax: `cat config/openclaw.json | jq .`
2. Check environment variables: `docker-compose exec openclaw env | sort`
3. Review OpenClaw logs: `docker-compose logs -f openclaw`
4. Test configuration: `docker-compose run --rm openclaw openclaw --validate-config`
5. Consult this documentation and OpenClaw docs at https://docs.openclaw.dev
