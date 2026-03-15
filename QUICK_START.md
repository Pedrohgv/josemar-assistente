# Quick Start Guide: Josemar Assistente

## Prerequisites Checklist

Before deploying, ensure you have:

- [ ] Docker and Docker Compose installed
- [ ] Z.AI API key (get from https://z.ai)
- [ ] Telegram Bot Token (get from @BotFather)
- [ ] DeepSeek API key (optional, get from https://api.deepseek.com)

## Step 1: Configure Environment Variables

```bash
cd repos/josemar-assistente

# Copy the example environment file
cp .env.example .env

# Edit with your API keys
nano .env
```

Add your API keys to `.env`:

```bash
# Required
ZAI_API_KEY=your_zai_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# Optional
DEEPSEEK_API_KEY=your_deepseek_api_key_here
OPENCLAW_LOG_LEVEL=info
```

## Step 2: Build and Deploy

### Option A: Automated Deployment (Recommended)

```bash
# The deploy script handles everything
./deploy.sh
```

### Option B: Manual Deployment

```bash
# Build the Docker image
docker compose build

# Start the services
docker compose up -d

# Check the logs
docker compose logs -f openclaw
```

## Step 3: Verify Deployment

```bash
# Check if the service is running
docker compose ps

# Should show:
# josemar-assistente   Up    ...
```

## Step 4: Test Telegram Bot

1. **Start a conversation** with your Telegram bot
2. **Send the command**: `/start`
3. **Send a test message**: "Olá, Josémar!"
4. **Verify response**: Bot should reply in Brazilian Portuguese

## Step 5: Test PDF Extraction

1. **Send a PDF** of a Brazilian credit card invoice to the bot
2. **Ask for extraction**: "Extraia os dados desta fatura"
3. **Verify result**: Bot should return structured expense data

## Common Issues and Solutions

### Issue: "docker compose: command not found"

**Solution**: Use `docker-compose` (with hyphen) instead:

```bash
docker-compose build
docker-compose up -d
```

Or install Docker Compose V2:

```bash
# For Linux
sudo apt-get install docker-compose-plugin

# Or follow: https://docs.docker.com/compose/install/
```

### Issue: "Service failed to start"

**Solution**: Check the logs for errors:

```bash
docker compose logs -f openclaw
```

Common causes:
- Missing API keys in `.env` file
- Invalid Telegram bot token
- Network connectivity issues
- Configuration syntax errors

### Issue: "Bot not responding"

**Solution**: Verify bot token and check privacy settings:

```bash
# Check bot token is valid
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"

# Should return bot information
```

Also check bot privacy settings:
1. Open @BotFather in Telegram
2. Go to Bot Settings -> Group Privacy
3. Turn off "Group Privacy"

### Issue: "PDF extraction not working"

**Solution**: Test the skill manually:

```bash
# Enter the container
docker compose exec openclaw sh

# Test the skill
echo "test" | /root/.openclaw/skills/pdf-extractor/pdf-extractor
```

## Monitoring and Maintenance

### View Logs

```bash
# Follow logs in real-time
docker compose logs -f openclaw

# View last 50 lines
docker compose logs --tail=50 openclaw
```

### Restart Service

```bash
# Restart without rebuilding
docker compose restart openclaw

# Rebuild and restart
docker compose down
docker compose build
docker compose up -d
```

### Stop Service

```bash
# Stop all services
docker compose down

# Stop and remove volumes
docker compose down -v
```

### Backup Workspace

```bash
# Create a backup of workspace data
tar -czf workspace-backup-$(date +%Y%m%d).tar.gz workspace/
```

### Restore Workspace

```bash
# Restore from backup
tar -xzf workspace-backup-YYYYMMDD.tar.gz
```

## Testing the Configuration

### Test Configuration Validation

The project includes a configuration validation script. Note: The json5 Python library may have strict parsing requirements. If you encounter validation errors, you can skip this step and proceed with deployment, as the OpenClaw application will validate the configuration on startup.

```bash
# Optional: Test configuration (may not work on all systems)
cd repos/josemar-assistente
python3 -m venv venv
source venv/bin/activate
pip install json5
python3 test-config.py
```

### Test PDF Skill Locally

```bash
# Test with sample invoice text
cd repos/josemar-assistente
./test-pdf-skill.sh
```

## Next Steps After Deployment

1. **Customize the agent**: Edit the system prompt in `config/openclaw.json5`
2. **Add more skills**: Create new skills in the `skills/` directory
3. **Configure providers**: Add more LLM providers as needed
4. **Set up monitoring**: Configure log rotation and monitoring
5. **Test with real data**: Test with actual PDF invoices

## Getting Help

If you encounter issues:

1. Check the logs: `docker compose logs -f openclaw`
2. Review the documentation:
   - `README.md` - Project overview
   - `IMPLEMENTATION_SUMMARY.md` - Detailed implementation notes
   - `AGENTS.md` - Root documentation
   - `config/AGENTS.md` - Configuration reference
   - `skills/AGENTS.md` - Skills development guide
3. Consult OpenClaw documentation: https://docs.openclaw.ai
4. Check your API key configurations

## Architecture Overview

```
┌─────────────────────────────────────┐
│       Docker Compose Network        │
└─────────────────────────────────────┘
                    │
        ┌───────────┴───────────┐
        │                       │
┌───────▼────────┐   ┌────────▼────────┐
│   Workspace     │   │   Config       │
│   (persistent)  │   │   (read-only)  │
└────────────────┘   └────────────────┘
        │
┌───────▼──────────────────────────────────────┐
│    josemar-assistente Container           │
│  ┌────────────────────────────────────┐   │
│  │   OpenClaw Gateway              │   │
│  │   - Z.AI (GLM 4.7)          │   │
│  │   - DeepSeek (fallback)       │   │
│  │   - Telegram Integration       │   │
│  │   - Skills System             │   │
│  │   - PDF Extractor Skill       │   │
│  └────────────────────────────────────┘   │
└──────────────────────────────────────────────┘
        │
┌───────▼──────────────────────────────────────┐
│       External Services                   │
│  ┌─────────────┐  ┌────────────────┐ │
│  │ Z.AI API    │  │ DeepSeek API   │ │
│  └─────────────┘  └────────────────┘ │
│  ┌────────────────────────────────┐      │
│  │ Telegram Bot API            │      │
│  └────────────────────────────────┘      │
└──────────────────────────────────────────────┘
```

## Success Indicators

You'll know the deployment is successful when:

✅ `docker compose ps` shows the service as "Up"
✅ Bot responds to `/start` command in Telegram
✅ Bot replies in Brazilian Portuguese
✅ PDF files are processed successfully
✅ Logs show no critical errors
✅ Configuration is loaded correctly

## Maintenance Tasks

### Weekly
- Check disk usage in workspace
- Review logs for warnings or errors
- Test bot functionality

### Monthly
- Backup workspace data
- Update Docker images: `docker compose pull`
- Review and update dependencies

### Quarterly
- Audit API key usage and costs
- Review and optimize configuration
- Plan and implement new features

---

**Ready to deploy!** Follow the steps above to get your Josemar Assistente bot running.
