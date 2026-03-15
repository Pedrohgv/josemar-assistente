# OpenClaw Setup Reference

This document provides reference information about the OpenClaw setup for Josemar Assistente.

## OpenClaw Concepts

### Gateway
- Single process that routes messages between channels and agents
- Configuration in `openclaw.json5` (JSON5 format)
- Supports hot reload in hybrid mode

### Providers
- LLM service connectors (Z.AI, OpenAI, Anthropic, etc.)
- Configuration includes API keys and model settings
- Z.AI provider supports GLM models (glm-4.7, glm-5)

### Channels
- Communication platforms (Telegram, WhatsApp, Discord, iMessage)
- Telegram uses Bot API with long polling or webhooks
- DM policies: pairing, allowlist, open, disabled

### Agents
- AI personalities with system prompts and capabilities
- Can route to specific channels
- Have default providers and models

### Skills
- Executable tools that agents can use
- Three load locations: bundled, managed, workspace
- Require `SKILL.md` documentation
- Can be gated with requirements (bins, env, config)

## Configuration Details

### Z.AI Provider Setup
1. Get API key from [z.ai](https://z.ai)
2. Set `ZAI_API_KEY` environment variable
3. Configure provider in `openclaw.json5`:
   ```json5
   zai: {
     type: "zai",
     apiKey: "${ZAI_API_KEY}",
     defaultModel: "zai/glm-4.7",
     toolStream: true,
     thinking: true,
   }
   ```

### Telegram Channel Setup
1. Create bot with [@BotFather](https://t.me/botfather)
2. Get bot token
3. Set `TELEGRAM_BOT_TOKEN` environment variable
4. Configure channel in `openclaw.json5`:
   ```json5
   telegram: {
     type: "telegram",
     botToken: "${TELEGRAM_BOT_TOKEN}",
     useWebhook: false,
     dmPolicy: "pairing",
     allowGroupMentions: true,
     language: "pt-BR",
   }
   ```

### Agent Configuration
- System prompt in Brazilian Portuguese
- Default provider: Z.AI with GLM-4.7
- Thinking mode enabled (GLM models support reasoning)
- Routes to Telegram channel
- Has access to PDF extraction skill

## Skill Development

### Creating a New Skill
1. Create skill directory in `skills/`
2. Add `SKILL.md` with documentation
3. Create executable script
4. Update configuration in `openclaw.json5`

### Skill Structure
```
skills/my-skill/
├── SKILL.md          # Skill documentation
└── my-skill          # Executable script (any language)
```

### Skill Configuration
Add to `openclaw.json5`:
```json5
skills: {
  "my-skill": {
    type: "executable",
    command: "/path/to/executable",
    args: [],
    description: "Skill description",
    categories: ["category1", "category2"],
  },
}
```

## Docker Deployment

### Image Layers
1. Base: `ghcr.io/openclaw/openclaw:latest`
2. Python and pymupdf for PDF processing
3. Custom skills baked into image
4. Entrypoint script for configuration handling

### Volume Mounts
- Config: `./config:/root/.openclaw-source:ro` (copied by entrypoint)
- Workspace: `./workspace:/root/.openclaw/workspace` (persistent data)
- Optional development mounts for scripts/skills

### Environment Variables
- `ZAI_API_KEY`: Required for Z.AI provider
- `TELEGRAM_BOT_TOKEN`: Required for Telegram channel
- `DEEPSEEK_API_KEY`: Optional for DeepSeek provider
- `OPENCLAW_LOG_LEVEL`: Log verbosity (debug, info, warn, error)

## Troubleshooting

### Common Issues

1. **Configuration errors**
   - Check JSON5 syntax (comments allowed, trailing commas allowed)
   - Validate with `openclaw validate` command

2. **Provider authentication**
   - Verify API keys in environment variables
   - Check provider documentation for updates

3. **Skill execution**
   - Check skill executable permissions
   - Verify skill dependencies are installed
   - Check OpenClaw logs for skill errors

4. **Telegram bot not responding**
   - Verify bot token
   - Check bot privacy settings (@BotFather -> Bot Settings -> Group Privacy)
   - Start conversation with `/start` command

### Logs and Debugging
```bash
# View logs
docker-compose logs -f openclaw

# Check configuration
docker-compose exec openclaw openclaw validate

# Check version
docker-compose exec openclaw openclaw --version
```

## Resources

- [OpenClaw Documentation](https://docs.openclaw.ai)
- [Z.AI API Documentation](https://z.ai/docs)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [JSON5 Specification](https://json5.org)
