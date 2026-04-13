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
| `OLLAMA_API_KEY` | Ollama Cloud API key (optional fallback/provider) | No |
| `PEDRO_TELEGRAM_ID` | Telegram user ID for the primary user | Yes |
| `GATEWAY_AUTH_PASSWORD` | HTTP Basic Auth password for OpenClaw web UI | Yes |
| `GOG_KEYRING_PASSWORD` | Optional passphrase for gogcli keyring (decrypts Google OAuth token store) | No |
| `WORKSPACE_REPO_TOKEN` | GitHub PAT for agent state repo (needs `repo` scope) | Yes |
| `RCLONE_CONFIG_B64` | Base64-encoded `rclone.conf` used by Obsidian backup container | Yes (for backups) |

## Required GitHub Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `WORKSPACE_STATE_REPO` | HTTPS URL of the private agent state repo | Yes |
| `LAN_BIND_IP` | Server LAN IP for Syncthing port binding | Yes (for laptop access) |
| `TZ` | Timezone used by Syncthing and backup scheduler | No (default `America/Sao_Paulo`) |
| `SYNCTHING_GUI_BIND_IP` | Syncthing GUI/API bind IP | No (default `127.0.0.1`) |
| `AUX_ML_ENABLED` | Optional aux-ml toggle (`true`/`false`) | No (default enabled) |
| `AUX_ML_MEMORY_LIMIT` | Docker memory limit for aux-ml service | No (default `8192m`) |
| `AUX_ML_MEMORY_LIMIT_MB` | Numeric memory budget used by aux-ml runtime validation | No (default `8192`) |
| `AUX_ML_MAX_QUEUE` | Maximum aux-ml queued jobs | No (default `50`) |
| `AUX_ML_JOB_TIMEOUT_SECONDS` | Aux-ml per-job timeout | No (default `1800`) |
| `AUX_ML_POLL_INTERVAL_SECONDS` | Queue/model poll interval for aux-ml | No (default `2`) |
| `AUX_ML_LLAMACPP_TIMEOUT_SECONDS` | llama.cpp server read/write timeout for long OCR requests | No (default `1800`) |
| `AUX_ML_ALLOWED_INPUT_DIRS` | Comma-separated allowed input roots for OCR | No (default `/root/.openclaw/workspace`) |
| `AUX_ML_ENFORCE_MEMORY_LIMIT` | Fail fast when memory budget is insufficient | No (default `true`) |
| `AUX_ML_OCR_MAX_PAGES` | Max pages per OCR PDF job | No (default `50`) |

Security note: avoid setting `SYNCTHING_GUI_BIND_IP=0.0.0.0`.

When `AUX_ML_ENABLED` is unset, the workflow treats it as `true` and writes `COMPOSE_PROFILES=aux-ml`.
Set `AUX_ML_ENABLED=false` only when you explicitly want to disable aux-ml for a deployment.
When aux-ml is enabled, workflow logs whether model files are local or will be downloaded from compose defaults (`glm-ocr` + `mmproj`).

### Obsidian Backup Defaults (from Compose)

The deployment workflow does not inject `OBSIDIAN_*` variables into `.env`.
Backup behavior is controlled by defaults in `docker-compose.yml`:

- `OBSIDIAN_BACKUP_TIME=03:15`
- `OBSIDIAN_BACKUP_RUN_ON_START=false`
- `OBSIDIAN_BACKUP_SLOTS=5`
- `OBSIDIAN_GDRIVE_REMOTE=gdrive`
- `OBSIDIAN_GDRIVE_PATH=Josemar/obsidian-backups`

To change these values globally, update `docker-compose.yml` defaults.

### Generating GATEWAY_AUTH_PASSWORD

Generate a secure password for accessing the OpenClaw web UI:

```bash
openssl rand -hex 32
```

Copy the output and add it as a GitHub secret named `GATEWAY_AUTH_PASSWORD`.

### Generating WORKSPACE_REPO_TOKEN

1. Go to GitHub Settings > Developer settings > Personal access tokens
2. Create a new token (classic) with `repo` scope
3. The token needs read/write access to the **private agent state repo**
4. Add the token as a GitHub secret named `WORKSPACE_REPO_TOKEN`

### Generating RCLONE_CONFIG_B64

1. Configure rclone locally (either native install or Docker):
   ```bash
   # Native binary (if installed)
   rclone config

   # Docker-only alternative (no host install required)
   docker run --rm -it -v "$PWD/credentials/rclone:/config/rclone" -e RCLONE_CONFIG=/config/rclone/rclone.conf rclone/rclone:latest config
   ```
2. Encode `rclone.conf` as base64 (single line):
   ```bash
   base64 -w 0 ~/.config/rclone/rclone.conf
   ```
3. Add the output as GitHub secret `RCLONE_CONFIG_B64`

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
| `fresh_start` | boolean | `false` | **WARNING: IRREVERSIBLE.** Erases OpenClaw workspace data: memory, skills, sessions, uploaded files, personality files. Obsidian vault volume is not removed. Has a 10-second countdown before proceeding. |
| `skip_git_sync` | boolean | `false` | Skip git sync on this deployment. Use when you want to work with local workspace state only. |

**Agent State Sync:**

The deployment handles agent state via git sync:

- **Normal deploy:** Container starts, syncs workspace with remote agent-state repo
  - Local agent changes are committed first
  - Remote changes are merged in (remote wins on conflicts)
  - Result is pushed back to remote
- **With `skip_git_sync`:** Git sync is disabled for this deployment
- **With `fresh_start`:** Docker volume is deleted entirely, workspace is re-cloned from remote

**Skills Deployment:**

Skills are versioned in the agent-state repo (`agent-state/skills/`). On container start:
1. The entrypoint runs the git sync script
2. Skills in the workspace are updated from the git repo
3. No separate "repo skills" vs "runtime skills" distinction

**Behavior:**
1. Checks out the repository (with submodules for `agent-state/`)
2. Creates `.env` file from GitHub secrets and variables
3. Loads `rclone.conf` from `RCLONE_CONFIG_B64` into Docker volume `obsidian-rclone-config`
4. Stops existing Docker services
5. Optionally removes workspace volume (if `fresh_start: true`, with safety countdown)
6. Cleans up old Docker images (preserves volumes)
7. Builds Docker images with no cache (includes `aux-ml` image only when aux profile is enabled)
8. Starts the services
9. Verifies the container is running and healthy
10. Verifies skill deployment
11. Removes plaintext `.env` from runner workspace

**Data Safety:**
- **DO NOT** use `docker system prune` (too broad)
- **DO NOT** use `docker volume prune` (deletes data)
- **DO NOT** use `--volumes` flag with `docker compose down`
- Only Docker images are cleaned up, never volumes
- Workspace data (stored in named Docker volume) and configuration are preserved
- Agent state is backed up to private git repo on every sync

**Workspace Persistence:**
- Workspace data is stored in a named Docker volume (`openclaw-workspace`)
- Obsidian vault data is stored in a separate named Docker volume (`obsidian-vault`)
- rclone config is stored in `obsidian-rclone-config`
- Backup slot state is stored in `obsidian-backup-state`
- The volume persists across deployments and container rebuilds
- Unlike bind mounts, named volumes are stored outside the git repository at `/var/lib/docker/volumes/`
- This avoids permission conflicts and checkout issues
- The volume contains the workspace git repo which syncs with the remote agent-state repo

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
| Git sync | No | Yes (unless skip_git_sync) |

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
   - `OLLAMA_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `PEDRO_TELEGRAM_ID`
   - `WORKSPACE_REPO_TOKEN`
   - `RCLONE_CONFIG_B64`
3. Verify `WORKSPACE_STATE_REPO` is set as a **Repository variable** (not a secret)
4. Verify `LAN_BIND_IP` is set as a **Repository variable**
5. Re-save secrets if recently added (may take a moment to propagate)

### Deployment Failures

**Symptoms:** Deploy workflow runs but bot doesn't respond

**Solutions:**
1. Check container logs:
   ```bash
   docker-compose logs -f openclaw
   ```
2. Verify environment variables in the running container:
   ```bash
   docker-compose exec openclaw env | grep -E "ZAI|OLLAMA|TELEGRAM"
   ```
3. Validate configuration:
   ```bash
   docker-compose run --rm openclaw openclaw --validate-config
   ```
4. Check if Telegram bot token is valid (only one deployment can use the same token)

### Skills Not Deploying

**Symptoms:** Skills not appearing after deployment

**Solutions:**
1. Check workspace sync logs in container logs:
   ```bash
   docker compose logs openclaw | grep workspace-sync
   ```
2. Verify skills exist in workspace:
   ```bash
   docker compose exec openclaw ls -la /root/.openclaw/skills/
   ```
3. Check git status in workspace:
   ```bash
   docker compose exec openclaw sh -c "cd /root/.openclaw/workspace && git status"
   ```
4. Verify WORKSPACE_STATE_REPO and WORKSPACE_REPO_TOKEN are set correctly

### Git Sync Issues

**Symptoms:** Agent state not syncing, conflicts not resolving

**Solutions:**
1. Check sync logs:
   ```bash
   docker compose logs openclaw | grep workspace-sync
   ```
2. Verify git remote is configured:
   ```bash
   docker compose exec openclaw sh -c "cd /root/.openclaw/workspace && git remote -v"
   ```
3. Verify token has correct permissions (`repo` scope)
4. Check if workspace is a valid git repo:
   ```bash
   docker compose exec openclaw sh -c "ls -la /root/.openclaw/workspace/.git"
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
