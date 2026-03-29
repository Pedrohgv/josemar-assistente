# GitHub Workflows Documentation

This directory contains GitHub Actions workflows for the Josemar Assistente project.

## Prerequisites

**Self-Hosted Runner Required**

All workflows require a self-hosted runner configured on your deployment server:

1. Install GitHub Actions runner on the server
2. Configure runner with repository access
3. Ensure runner user has Docker permissions (member of `docker` group)
4. Start and verify runner is online in GitHub Settings > Actions > Runners

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
| `GATEWAY_AUTH_PASSWORD` | HTTP Basic Auth password for OpenClaw web UI | Yes |
| `GOG_KEYRING_PASSWORD` | GOG keyring password for Galaxy integration | No |

### Generating GATEWAY_AUTH_PASSWORD

Generate a secure password for accessing the OpenClaw web UI:

```bash
openssl rand -hex 32
```

Copy the output and add it as a GitHub secret named `GATEWAY_AUTH_PASSWORD`.

This password is required to access the web interface via HTTP Basic Auth at:
`http://your-server:18789/`

## Test Workflow

### test-workflow

Simple workflow for testing the self-hosted runner setup.

**Trigger:** Manual only (`workflow_dispatch`)

**Use Case:** Verify runner is working after setup or during troubleshooting.

**Behavior:**
1. Runs on self-hosted runner
2. Echoes test messages
3. Verifies runner connectivity and permissions

## Deployment Workflow

### deploy-to-home-server

Deploys the Josemar Assistente to the self-hosted server.

**Trigger:** Manual only (`workflow_dispatch`)

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fresh_start` | boolean | `false` | When enabled, removes the existing workspace volume and starts fresh. **Warning:** This deletes all conversation history, personality files (SOUL.md, MEMORY.md), and accumulated data. |
| `force_overwrite_skills` | string | `""` | Comma-separated list of skill names to force overwrite from repo (e.g., `pdf-extractor,web-scraper`). Use to reset specific skills to their original repo version, discarding any agent modifications. |

**Two-Tier Skill System:**

The deployment workflow supports a two-tier skill system:

- **Runtime Skills** (`/root/.openclaw/skills/`): Skills created by the assistant during runtime. These are preserved across deployments and take priority over repo skills.
- **Repo Skills** (`/root/.openclaw/repo-skills/`): Skills maintained in the `repo-skills/` directory of the repository. These are version-controlled and deployed on container startup.

**Smart Deployment Behavior:**
- First deployment: Repo skills are copied to the container
- Subsequent deployments: Repo skills are skipped if they already exist (to preserve agent modifications)
- Force overwrite: Set `force_overwrite_skills` to specific skill names to reset them to repo version

**Behavior:**
1. Checks out the repository
2. Creates `.env` file from GitHub secrets
3. Stops existing Docker services
4. Optionally removes workspace volume (if `fresh_start: true`)
5. Cleans up old Docker images (preserves volumes)
6. Builds the Docker image with no cache
7. Starts the services
8. Verifies the container is running
9. Verifies skill deployment (logs both repo and runtime skills directories)

**Data Safety:**
- **DO NOT** use `docker system prune` (too broad)
- **DO NOT** use `docker volume prune` (deletes data)
- **DO NOT** use `--volumes` flag with `docker compose down`
- Only Docker images are cleaned up, never volumes
- Workspace data (stored in named Docker volume) and configuration are preserved

**Workspace Persistence:**
- Workspace data is stored in a named Docker volume (`openclaw-workspace`)
- The volume persists across deployments and container rebuilds
- Unlike bind mounts, named volumes are stored outside the git repository at `/var/lib/docker/volumes/`
- This avoids permission conflicts and checkout issues

This ensures that user data, session data, and configuration remain intact during deployment.

## Stop Service Workflow

### stop-service

Safely stops the Josemar Assistente service without deleting data.

**Trigger:** Manual only (`workflow_dispatch`)

**Parameters:** None

**Behavior:**
1. Runs on self-hosted runner
2. Checks out repository (with `clean: false` to preserve any local changes)
3. Runs `docker compose down` to stop services
4. Verifies container is stopped by checking `docker ps`
5. Fails if container is still running

**Use Cases:**
- Emergency shutdown
- Before maintenance
- When you need to free up resources
- Before running fresh deployment (alternative to deploy workflow's built-in stop)

**Data Safety:**
- Does **NOT** remove workspace volume (data preserved)
- Does **NOT** delete images or configuration
- Only stops running containers

**Comparison with deploy-to-home-server:**

| Aspect | stop-service | deploy-to-home-server |
|--------|--------------|----------------------|
| Action | Just stops | Stops, rebuilds, and restarts |
| Data | Preserved | Preserved (unless fresh_start) |
| Images | Kept | Rebuilt (with --no-cache) |
| Configuration | Unchanged | Applied from repo |

## Troubleshooting

### Workflow Not Starting

**Symptoms:** Workflow stays in "Queued" state indefinitely

**Solutions:**
1. Verify self-hosted runner is online:
   - Go to GitHub Settings > Actions > Runners
   - Check if runner shows as "Idle" (green dot)
2. Check runner service on server:
   ```bash
   sudo systemctl status actions.runner.*
   ```
3. View runner logs:
   ```bash
   sudo journalctl -u actions.runner.* -f
   ```

### Permission Denied Errors

**Symptoms:** Workflow fails with "permission denied" when running Docker commands

**Solutions:**
1. Add runner user to docker group:
   ```bash
   sudo usermod -aG docker $USER
   ```
2. Restart runner service:
   ```bash
   sudo systemctl restart actions.runner.*
   ```
3. Or reboot the server to apply group changes

### Secrets Not Found

**Symptoms:** Workflow fails with "secret not found" or empty values

**Solutions:**
1. Verify secrets are set in **Repository** settings (not Environment secrets)
2. Check secret names match exactly (case-sensitive):
   - `ZAI_API_KEY` (not `zai_api_key`)
   - `TELEGRAM_BOT_TOKEN`
   - `PEDRO_TELEGRAM_ID`
3. Re-save secrets if recently added (may take a moment to propagate)

### Deployment Failures

**Symptoms:** Deploy workflow runs but bot doesn't respond

**Solutions:**
1. Check container logs:
   ```bash
   docker-compose logs -f openclaw
   ```
2. Verify `.env` file was created correctly:
   ```bash
   docker-compose exec openclaw env | grep -E "ZAI|TELEGRAM"
   ```
3. Validate configuration:
   ```bash
   docker-compose run --rm openclaw openclaw --validate-config
   ```
4. Check if Telegram bot token is valid (only one deployment can use the same token)

### Skills Not Deploying

**Symptoms:** Repo skills not appearing or runtime skills being overwritten

**Solutions:**
1. Check skill deployment logs in workflow output
2. Verify repo skills are mounted correctly:
   ```bash
   docker-compose exec openclaw ls -la /root/.openclaw/repo-skills/
   ```
3. Check runtime skills:
   ```bash
   docker-compose exec openclaw ls -la /root/.openclaw/skills/
   ```
4. Force overwrite specific skills by re-running deployment with `force_overwrite_skills` parameter
5. Check OpenClaw skill configuration:
   ```bash
   docker-compose exec openclaw openclaw skills list
   ```

## Additional Resources

- **GitHub Actions Documentation**: https://docs.github.com/en/actions
- **Self-Hosted Runners**: https://docs.github.com/en/actions/hosting-your-own-runners
- **OpenClaw Documentation**: https://docs.openclaw.dev

## Support

For workflow issues:
1. Check runner status in GitHub Settings > Actions > Runners
2. View workflow logs in GitHub Actions tab
3. Check runner service: `sudo systemctl status actions.runner.*`
4. Review runner logs: `sudo journalctl -u actions.runner.* -f`
