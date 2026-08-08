# GitHub Workflows Documentation

This directory contains GitHub Actions workflows for the Hermes-based Josemar Assistente deployment.

## Prompt Language Policy

All AI-harness-facing instructions in this directory must be written in English, even when the deployed assistant interacts with users in another language.

## Prerequisites

All workflows run on a self-hosted runner.

1. Install a GitHub Actions runner on the deployment server.
2. Ensure the runner user can run Docker commands.
3. Verify runner status in GitHub Settings > Actions > Runners.

## Workflows

- `deploy-to-home-server.yml`: build and deploy Hermes stack.
- `gbrain-embedding-backfill.yml`: manual, destructive gbrain embedding activation and retry-safe backfill; it never deploys or rebuilds.
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
| `DEEPSEEK_API_KEY` | Yes | DeepSeek provider key for the default model |
| `OLLAMA_API_KEY` | No | Optional provider key |
| `TAVILY_API_KEY` | No | Optional Tavily key; enables `web_search` and `web_extract` via auto-detection |
| `GOG_KEYRING_PASSWORD` | No | Optional gog keyring passphrase |
| `TS_AUTHKEY` | No | Optional unattended tailscale login |
| `HERMES_API_SERVER_KEY` | No | Required when `HERMES_API_SERVER_ENABLED=true` |
| `HERMES_DASHBOARD_SESSION_TOKEN` | Yes | Dashboard session token used by Hermes Desktop for REST/WebSocket access |
| `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` | Yes | Browser dashboard Basic Auth password |
| `HERMES_DASHBOARD_BASIC_AUTH_SECRET` | Yes | Stable dashboard session signing secret |
| `BROWSER_TUNNEL_AUTHORIZED_KEY` | No | Required when `BROWSER_CONTROL_ENABLED=true`; single-line SSH public key for the reverse tunnel sidecar |

## Required Variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `WORKSPACE_STATE_REPO` | Yes | Private state repo URL |
| `JOSEMAR_CONTAINER_PREFIX` | No | Docker container name prefix (default `josemar`) |
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
| `HERMES_API_SERVER_ENABLED` | No | Enable Hermes API server for clients such as Hermes Desktop (default `false`) |
| `HERMES_API_SERVER_BIND_IP` | No | Host bind IP for the API server port (default `127.0.0.1`) |
| `HERMES_API_SERVER_PORT` | No | Host/API server port (default `8642`) |
| `HERMES_API_SERVER_CORS_ORIGINS` | No | Optional comma-separated CORS origins |
| `HERMES_API_SERVER_MODEL_NAME` | No | Display/model name advertised to clients such as Hermes One (default `Josemar`) |
| `HERMES_DEFAULT_PROFILE_DISPLAY_NAME` | No | URL-safe dashboard profile label for the base Hermes profile (default `Josemar`) |
| `HERMES_DASHBOARD_BIND_IP` | No | Host bind IP for the dashboard port (default `127.0.0.1`; `0.0.0.0` is rejected) |
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` | No | Browser dashboard Basic Auth username (default `admin`) |
| `BROWSER_CONTROL_ENABLED` | No | Enable the optional `browser-control` overlay (default `false`). See `docs/browser-control.md`. |
| `BROWSER_CONTROL_SUBNET` | No | Override the internal `browser-control` Docker subnet (default `172.31.250.0/29`). Set together with the gateway/IP overrides if the default collides. |
| `BROWSER_CONTROL_GATEWAY` | No | Override the `browser-control` gateway IPv4 (default `172.31.250.1`). |
| `BROWSER_CONTROL_HERMES_IP` | No | Override Hermes's static IPv4 on `browser-control` (default `172.31.250.2`); the SSH daemon binds only to this IP and Tailscale Serve forwards to it. |
| `BROWSER_CONTROL_TAILSCALE_IP` | No | Override Tailscale's static IPv4 on `browser-control` (default `172.31.250.3`). |
| `MNEMOSYNE_DEPLOY_MODE` | No | Select the Mnemosyne overlays (default `off`). Allowed: `off`, `pilot`, `backup`. `pilot` = base + embeddings + mnemosyne; `backup` = pilot + mnemosyne-backup. There is no embeddings-only mode. Any other value is rejected before any volume mutation or teardown. See `docs/mnemosyne-operations.md`. |
| `MNEMOSYNE_BACKUP_EXPORT_INTERVAL` | No (required when `MNEMOSYNE_DEPLOY_MODE=backup`) | Positive integer export interval in minutes, no leading zeros, <= 10080 (1 week). The deploy workflow verifies exactly one `mnemosyne-backup-export` cron job with a matching interval schedule. |
| `GBRAIN_EMBEDDINGS_ENABLED` | No | Strict `true`/`false` switch (default `false`) for the embeddings Compose overlay. `pilot` and `backup` select the overlay independently. This does not enable gbrain embeddings or backfill vectors. |

Security note: keep `SYNCTHING_GUI_BIND_IP` on localhost unless explicitly secured. Keep `HERMES_DASHBOARD_INSECURE=0`; if `HERMES_DASHBOARD_BIND_IP` is set beyond localhost for a Cloudflare Tunnel running on another host, ensure the dashboard Basic Auth secrets are configured and avoid binding to `0.0.0.0`. Do not set `HERMES_API_SERVER_BIND_IP=0.0.0.0` unless `HERMES_API_SERVER_KEY` is set and the network path is trusted.

## Deploy Workflow Notes

- Deploy writes `.env` from repository secrets/variables.
- Deploy uses `docker compose down --remove-orphans` before rebuild/start.
- `fresh_start=true` is disabled after moving state into `/opt/data`; use a manual, reviewed cleanup instead.
- Deploy verifies Hermes container health (`${JOSEMAR_CONTAINER_PREFIX:-josemar}-hermes`).
- Deploy verifies repo-owned skills under `/opt/josemar/skills`.
- Browser control is an optional Compose overlay (`docker-compose.browser-control.yml`). When `BROWSER_CONTROL_ENABLED=true`, deploy populates the `tailscale-serve-config` and `browser-tunnel-authorized-keys` named volumes atomically via a pinned Alpine image (real legacy TCPForward `{"TCP":{"2222":{"TCPForward":"<HERMES_IP>:2222"}}}` and the operator's SSH public key), sets `COMPOSE_FILE=docker-compose.yml:docker-compose.browser-control.yml`, adds `browser-control` to `COMPOSE_PROFILES`, and verifies the `browser-tunnel` sidecar is running plus Tailscale Serve tcp:2222 targets the exact Hermes IP with no Funnel. It does not require the laptop/Chrome to be online.
- When `BROWSER_CONTROL_ENABLED=false`, deploy writes `{}` into `tailscale-serve-config` and clears `browser-tunnel-authorized-keys` so stale tcp:2222 is deterministically removed on restart/redeploy, uses base Compose only, and verifies tcp:2222 is absent.
- Deploy always tears down with the maximal superset of overlays (base + browser-control + embeddings + mnemosyne + mnemosyne-backup, plus the `aux-ml`, `browser-control`, and `recovery` profiles — recovery only to remove a stale recovery service) before the selected config, with no `-v` so named volumes are preserved. The teardown is fail-closed (`set -euo pipefail`, no `|| true`): a teardown failure stops the deploy. This removes any prior overlay service when switching to `off` or dropping an overlay.
- Deploy runs ALL preflight validation before any `docker volume create`, volume write, or service teardown, in order: input validation; `COMPOSE_FILE` derivation; `docker compose config --quiet` on the selected files; RCLONE_CONFIG_B64 base64 decode + remote validation (backup mode). Only after all pass does it create/populate volumes and tear down prior services.
- Deploy renders the selected `COMPOSE_FILE` with `docker compose config --quiet` before any volume mutation or teardown so an invalid overlay combination fails closed without mutating volumes or stopping services.
- Mnemosyne deployment is gated by the `MNEMOSYNE_DEPLOY_MODE` repo variable (default `off`). Allowed values: `off`, `pilot`, `backup`. `pilot` applies base + embeddings + mnemosyne; `backup` adds the mnemosyne-backup overlay. There is no embeddings-only mode. Any other value is rejected before any volume mutation or teardown. `COMPOSE_FILE` is built with strict ordering: base; optional browser-control; embeddings; mnemosyne; backup last. The `recovery` profile is never enabled for a normal deploy.
- In `backup` mode, deploy requires `MNEMOSYNE_BACKUP_EXPORT_INTERVAL` (positive integer, no leading zeros, <= 10080 minutes) and `RCLONE_CONFIG_B64`. It strict-base64-decodes the secret to a `0600` temp file, validates the `mnemosyne-crypt` rclone remote with THREE independent field checks (type `crypt`, nonempty `remote`, nonempty `password`) plus the baseline `gdrive` remote, using the pinned rclone image `rclone/rclone@sha256:b06aed988cf5967de7c25be5925240983981c757f4ed1ac9d2fa659d51d60548` (direct `rclone` invocation — image entrypoint is `["rclone"]`, no `sh -c`), and publishes the config atomically into the shared `obsidian-rclone-config` volume. The crypt remote name `mnemosyne-crypt` is hardcoded in validation and written explicitly to `.env`. No config/secrets are printed. The existing non-backup behavior for the shared rclone config remains supported.
- Deploy adds mode-specific post-start checks because the Hermes init logs Mnemosyne activation failures nonfatally (container health alone is not enough), using `hermes_cli.config.load_config()` to load `/opt/data/config.yaml` and assert the exact `memory` subtree: `off` confirms overlays absent (queried with the MAXIMAL compose file set so orphan containers from a failed teardown are detected), provider blank, static flags restored, nested mnemosyne config absent, and no export cron; `pilot`/`backup` confirm TEI healthy (600s wait budget, immediate failure if embeddings exits) plus provider/policy activation and no init activation-failure log; `backup` adds uploader running and exactly one `mnemosyne-backup-export` cron job matching the real `jobs.json` schema (`schedule.kind == "interval"`, integer `minutes`, `script`, `no_agent == true`, `workdir`).
- Deploy removes plaintext `.env` at the end. Persistent named volumes (`tailscale-serve-config`, `browser-tunnel-authorized-keys`, `browser-tunnel-state`) are never deleted by cleanup.
- The embeddings overlay is selected when `GBRAIN_EMBEDDINGS_ENABLED=true` or `MNEMOSYNE_DEPLOY_MODE` is `pilot`/`backup`, exactly once and after browser-control in `COMPOSE_FILE`. The effective strict boolean is written to `.env` and `GITHUB_ENV`. Deploy verifies the selected `embeddings` service is healthy, or verifies it is absent when not selected; deploy never runs `enable-embeddings` or `embed-backfill`.

## Manual gbrain embedding activation

Run `gbrain-embedding-backfill.yml` only after a successful deploy with the
embeddings overlay selected and healthy. Enter the exact confirmation
`ENABLE_AND_BACKFILL`. The workflow requires the repository variable
`GBRAIN_EMBEDDINGS_ENABLED=true`, validates the existing container prefix and
running Hermes container, verifies the container has non-empty
`GBRAIN_EMBEDDING_MODEL` and `GBRAIN_EMBEDDING_DIMENSIONS`, then runs
`josemar-gbrain enable-embeddings` followed by `embed-backfill` with `docker exec`.
It is fail-closed and retry-safe, never prints environment values or secrets,
and never rebuilds, deploys, or removes containers.

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
