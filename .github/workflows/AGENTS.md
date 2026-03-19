# GitHub Workflows Documentation

This directory contains GitHub Actions workflows for the Josemar Assistente project.

## Runner Configuration

All workflows in this directory **MUST** use `runs-on: self-hosted` unless explicitly specified otherwise. The project uses a self-hosted runner to deploy to the home server.

## Required GitHub Secrets

The following secrets must be configured in the GitHub repository settings:

| Secret | Description | Required |
|--------|-------------|----------|
| `ZAI_API_KEY` | API key for Z.AI provider (GLM models) | Yes |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather | Yes |
| `DEEPSEEK_API_KEY` | DeepSeek API key (optional fallback) | No |
| `PEDRO_TELEGRAM_ID` | Telegram user ID for the primary user | Yes |
| `GATEWAY_AUTH_TOKEN` | Authentication token for OpenClaw web UI | Yes |

### Generating GATEWAY_AUTH_TOKEN

Generate a secure random token for accessing the OpenClaw web UI:

```bash
openssl rand -hex 32
```

Copy the output and add it as a GitHub secret named `GATEWAY_AUTH_TOKEN`.

This token is required to access the web interface at:
`http://your-server:18789/__openclaw__/canvas/?token=YOUR_TOKEN`

## Deployment Workflow

### deploy-to-home-server

Deploys the Josemar Assistente to the self-hosted server.

**Trigger:** Manual only (`workflow_dispatch`)

**Behavior:**
1. Checks out the repository
2. Creates `.env` file from GitHub secrets
3. Stops existing Docker services
4. Cleans up old Docker images (preserves volumes)
5. Builds the Docker image with no cache
6. Starts the services
7. Verifies the container is running

**Data Safety:**
- **DO NOT** use `docker system prune` (too broad)
- **DO NOT** use `docker volume prune` (deletes data)
- **DO NOT** use `--volumes` flag with `docker compose down`
- Only Docker images are cleaned up, never volumes
- Workspace data and configuration are preserved

This ensures that user data, session data, and configuration remain intact during deployment.
