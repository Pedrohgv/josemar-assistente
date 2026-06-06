# GitHub Workflows Documentation

This directory contains GitHub Actions workflows for the Hermes-based Josemar Assistente deployment.

## Prerequisites

All workflows run on a self-hosted runner.

1. Install a GitHub Actions runner on the deployment server.
2. Ensure the runner user can run Docker commands.
3. Verify runner status in GitHub Settings > Actions > Runners.

## Workflows

- `deploy-to-home-server.yml`: build and deploy Hermes stack.
- `stop-service.yml`: stop all project services safely.
- `privacy-scan.yml`: secret and PII scanning on changes.
- `test-workflow.yml`: basic runner connectivity test.

## Required Secrets

| Secret | Required | Purpose |
| --- | --- | --- |
| `ZAI_API_KEY` | Yes | Z.AI/GLM provider key |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token |
| `PRIMARY_TELEGRAM_ID` | Yes | Primary allowlisted Telegram user |
| `WORKSPACE_REPO_TOKEN` | Yes | PAT for private state repo sync |
| `RCLONE_CONFIG_B64` | Yes (for backups) | Base64 `rclone.conf` for backup container |
| `DEEPSEEK_API_KEY` | No | Optional provider key |
| `OLLAMA_API_KEY` | No | Optional provider key |
| `GOG_KEYRING_PASSWORD` | No | Optional gog keyring passphrase |
| `TS_AUTHKEY` | No | Optional unattended tailscale login |
| `HERMES_API_SERVER_KEY` | No | Required only when Hermes API auth is enabled |

## Required Variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `WORKSPACE_STATE_REPO` | Yes | Private state repo URL |
| `TZ` | No | Timezone (default `America/Sao_Paulo`) |
| `SYNCTHING_GUI_BIND_IP` | No | Syncthing GUI bind IP (default `127.0.0.1`) |
| `TAILSCALE_HOSTNAME` | No | Tailscale node name |
| `TS_EXTRA_ARGS` | No | Extra flags for tailscale sidecar |
| `AUX_ML_ENABLED` | No | Enable/disable aux-ml profile |
| `AUX_ML_MEMORY_LIMIT` | No | aux-ml container memory limit |
| `AUX_ML_MEMORY_LIMIT_MB` | No | Runtime memory budget for aux-ml |
| `AUX_ML_MAX_QUEUE` | No | Max aux-ml queue length |
| `AUX_ML_JOB_TIMEOUT_SECONDS` | No | aux-ml job timeout |
| `AUX_ML_POLL_INTERVAL_SECONDS` | No | aux-ml poll interval |
| `AUX_ML_LLAMACPP_TIMEOUT_SECONDS` | No | llama.cpp timeout |
| `AUX_ML_ENFORCE_MEMORY_LIMIT` | No | Enforce aux-ml memory check |
| `AUX_ML_OCR_MAX_PAGES` | No | OCR max pages per file |
| `HERMES_BASE_IMAGE` | No | Override pinned Hermes base image |
| `GOGCLI_REF` | No | Override pinned gogcli ref |

Security note: keep `SYNCTHING_GUI_BIND_IP` on localhost unless explicitly secured.

## Deploy Workflow Notes

- Deploy writes `.env` from repository secrets/variables.
- Deploy uses `docker compose down --remove-orphans` before rebuild/start.
- `fresh_start=true` removes only the `hermes-workspace` volume.
- Deploy verifies Hermes container health (`josemar-hermes`).
- Deploy verifies repo-owned skills under `/opt/josemar/skills`.
- Deploy removes plaintext `.env` at the end.

## Stop Workflow Notes

- `stop-service.yml` runs `docker compose down`.
- It verifies Hermes/aux-ml/syncthing/tailscale/backup containers are no longer running.
- Volumes are preserved.

## Privacy Workflow Notes

- Runs gitleaks and `scripts/pii_guard.py`.
- Fails on medium/high-confidence PII findings.

## Troubleshooting

- If workflow is queued: check runner online status.
- If Docker permission errors occur: add runner user to `docker` group and restart runner.
- If deploy fails health checks: inspect `docker compose logs` on the runner host.
- If state sync fails: verify `WORKSPACE_STATE_REPO` and `WORKSPACE_REPO_TOKEN`.
