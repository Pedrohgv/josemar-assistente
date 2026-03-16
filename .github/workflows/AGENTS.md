# GitHub Workflows Documentation

This directory contains GitHub Actions workflows for the Josemar Assistente project.

## Runner Configuration

All workflows in this directory **MUST** use `runs-on: self-hosted` unless explicitly specified otherwise. The project uses a self-hosted runner to deploy to the home server.

## Required GitHub Secrets

The following secrets must be configured in the GitHub repository settings:

| Secret | Description |
|--------|-------------|
| `ZAI_API_KEY` | API key for Z.AI provider (GLM models) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `DEEPSEEK_API_KEY` | (Optional) DeepSeek API key |
| `TELEGRAM_USER_ID` | (Optional) Telegram user ID for pairing |

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
