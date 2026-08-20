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
- `fast-tests.yml`: run `make verify` on pull requests, pushes to `main`, and manual dispatch with pinned test dependencies installed into an isolated runner temp directory.
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
| `RCLONE_CONFIG_B64` | Yes | Base64 `rclone.conf` for the encrypted backup lanes. **Required for every deployment** since Phase 3: it must contain the `vault-recovery-crypt` remote (type `crypt`, non-empty underlying + password); the deploy fails without it rather than silently losing backups |
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
- Deploy runs the vault-recovery portability proof as a MANDATORY pre-mutation release gate: `tests/runtime/test_vault_recovery_portability.py` with `RUN_DOCKER_TESTS=1 VAULT_RECOVERY_PORTABILITY_REQUIRED=1`. It builds a disposable isolated Hermes runtime, creates REAL vector-bearing `.gbrain` state through the pinned embedding workflow, exports through the production wrapper, physically copies the staged generation into a fresh restore root, and requires the restored tree to open on the doctor with vectors/DB-only records/config/markers intact (no reindex/rebuild/sync). A missing docker CLI or any failed assertion FAILS the deploy — there is no opt-in bypass on the release path.
- Deploy ALSO runs the Phase-3 full disaster-recovery drill as a MANDATORY pre-mutation release gate (not a recommendation): `tests/runtime/test_vault_recovery_dr_drill.py` with `RUN_DOCKER_TESTS=1 VAULT_RECOVERY_DR_DRILL_REQUIRED=1`. It is the same drill end to end on the pinned images: real vector-bearing state, export + encrypted upload, the ordered maintenance window (all three owned jobs — `gbrain-refresh`, `gbrain-embedding-refresh`, `vault-recovery-export` — paused and asserted absent from `jobs.json`; Hermes and server Syncthing stopped and asserted not running), DESTROY of both live trees, recover/verify/install, controlled restart, survival proofs, and rollback. A missing docker CLI or any failed assertion FAILS the deploy — there is no opt-in bypass.
- Deploy always includes the vault-recovery overlay (`docker-compose.vault-recovery.yml`) in `COMPOSE_FILE` (default encrypted backup lane, Phase 3): it requires `RCLONE_CONFIG_B64` on EVERY deployment and validates the hardcoded `vault-recovery-crypt` remote with FOUR independent checks (type `crypt`, non-empty `remote`, non-empty `password`, and the metadata-encryption standard: `filename_encryption` `standard` + `directory_name_encryption` enabled — `off`/`obfuscate`/`false` rejected) INDEPENDENTLY of `MNEMOSYNE_DEPLOY_MODE`; `mnemosyne-crypt` gets the same validation only in `backup` mode. It writes `VAULT_RECOVERY_RCLONE_REMOTE=vault-recovery-crypt` to `.env`. After the decode step it runs the REAL remote readiness gate (write + read-back probe through the production crypt remote, before any teardown): a syntactically valid but unreachable remote ABORTS the deploy with the existing deployment and any legacy lane state retained untouched. Post-start it verifies the `vault-recovery-uploader` is running, exactly one `vault-recovery-export` cron job exists with the real `jobs.json` schema (`schedule.kind == "cron"`, `expr == "0 4 * * *"`, script `hermes-vault-recovery-export-cron.sh`, `no_agent == true`, `workdir == "/opt/data"` exactly), and no `*-obsidian-backup` container lingers (plaintext absence).
- The decoded `RCLONE_CONFIG_B64` temp config never lingers on the runner: the decode step traps its removal on every failure path (disarmed on success); the readiness gate — the only step between them, and one that can itself fail (an unreachable remote aborts the deploy there) — traps its removal on EVERY exit and disarms it only on success; and the publish step traps its removal on EVERY exit. The final `Cleanup sensitive files` step (`if: always()`) additionally removes `$RCLONE_TEMP_CONF` alongside `.env`, covering a run cancelled BETWEEN the rclone steps (a step-local trap only fires inside its own shell). A failure in any of the three steps, or a between-step cancellation, removes the decoded secret — there is no leak path. The vault-recovery overlay wires `VAULT_RECOVERY_RCLONE_REMOTE=vault-recovery-crypt` and `VAULT_RECOVERY_RCLONE_PATH=Josemar/vault-recovery` as LITERALS in both services (never `${...}`-interpolated): Compose interpolation would prefer the runner environment over the `.env` file, so an interpolated value could silently route backups to a different remote than the one the deploy validated and probed.
- The plaintext `obsidian-backup` service is RETIRED (Phase 3): removed from the base compose, `stop-service.yml`, `.env.example`, and the runbooks; the legacy scripts are deleted. Existing plaintext GDrive slots are never deleted by automation; manual historical recovery is operator-only (see `docs/obsidian-operations.md` → "Retired plaintext lane").
- Deploy verifies repo-owned skills under `/opt/josemar/skills`.
- Browser control is an optional Compose overlay (`docker-compose.browser-control.yml`). When `BROWSER_CONTROL_ENABLED=true`, deploy populates the `tailscale-serve-config` and `browser-tunnel-authorized-keys` named volumes atomically via a pinned Alpine image (real legacy TCPForward `{"TCP":{"2222":{"TCPForward":"<HERMES_IP>:2222"}}}` and the operator's SSH public key), sets `COMPOSE_FILE=docker-compose.yml:docker-compose.browser-control.yml`, adds `browser-control` to `COMPOSE_PROFILES`, and verifies the `browser-tunnel` sidecar is running plus Tailscale Serve tcp:2222 targets the exact Hermes IP with no Funnel. It does not require the laptop/Chrome to be online.
- When `BROWSER_CONTROL_ENABLED=false`, deploy writes `{}` into `tailscale-serve-config` and clears `browser-tunnel-authorized-keys` so stale tcp:2222 is deterministically removed on restart/redeploy, uses base Compose only, and verifies tcp:2222 is absent.
- Deploy always tears down with the maximal superset of overlays (base + vault-recovery + browser-control + embeddings + mnemosyne + mnemosyne-backup, plus the `aux-ml`, `browser-control`, and `recovery` profiles — recovery only to remove a stale recovery service) before the selected config, with no `-v` so named volumes are preserved. The teardown is fail-closed (`set -euo pipefail`, no `|| true`): a teardown failure stops the deploy. This removes any prior overlay service when switching to `off` or dropping an overlay.
- Deploy runs ALL preflight validation before any `docker volume create`, volume write, or service teardown, in order: input validation (including the mandatory `RCLONE_CONFIG_B64` requirement for the default vault-recovery lane); `COMPOSE_FILE` derivation; `docker compose config --quiet` on the selected files; RCLONE_CONFIG_B64 base64 decode + remote validation (`vault-recovery-crypt` always with the FOUR checks incl. the metadata-encryption standard, `mnemosyne-crypt` in backup mode); the REAL vault-recovery remote readiness gate (write + read-back probe through the production crypt remote); then the rclone config publish (which traps temp-file removal). Only after all pass does it create/populate volumes and tear down prior services.
- Deploy renders the selected `COMPOSE_FILE` with `docker compose config --quiet` before any volume mutation or teardown so an invalid overlay combination fails closed without mutating volumes or stopping services.
- Mnemosyne deployment is gated by the `MNEMOSYNE_DEPLOY_MODE` repo variable (default `off`). Allowed values: `off`, `pilot`, `backup`. `pilot` applies base + embeddings + mnemosyne; `backup` adds the mnemosyne-backup overlay. There is no embeddings-only mode. Any other value is rejected before any volume mutation or teardown. `COMPOSE_FILE` is built with strict ordering: base; optional browser-control; embeddings; mnemosyne; backup last. The `recovery` profile is never enabled for a normal deploy.
- In `backup` mode, deploy requires `MNEMOSYNE_BACKUP_EXPORT_INTERVAL` (positive integer, no leading zeros, <= 10080 minutes) and validates the `mnemosyne-crypt` rclone remote with THREE independent field checks (type `crypt`, nonempty `remote`, nonempty `password`) using the pinned rclone image `rclone/rclone@sha256:b06aed988cf5967de7c25be5925240983981c757f4ed1ac9d2fa659d51d60548` (direct `rclone` invocation — image entrypoint is `["rclone"]`, no `sh -c`), and publishes the config atomically into the shared `obsidian-rclone-config` volume. The crypt remote name `mnemosyne-crypt` is hardcoded in validation and written explicitly to `.env`. The `vault-recovery-crypt` remote gets FOUR checks on EVERY deployment (type, remote, password + the metadata-encryption standard: `filename_encryption` `standard` and `directory_name_encryption` enabled — default lane, independent of this mode). No config/secrets are printed.
- Deploy adds mode-specific post-start checks because the Hermes init logs Mnemosyne activation failures nonfatally (container health alone is not enough), using `hermes_cli.config.load_config()` to load `/opt/data/config.yaml` and assert the exact `memory` subtree: `off` confirms overlays absent (queried with the MAXIMAL compose file set so orphan containers from a failed teardown are detected), provider blank, static flags restored, nested mnemosyne config absent, and no export cron; `pilot`/`backup` confirm TEI healthy (600s wait budget, immediate failure if embeddings exits) plus provider/policy activation and no init activation-failure log; `backup` adds uploader running and exactly one `mnemosyne-backup-export` cron job matching the real `jobs.json` schema (`schedule.kind == "interval"`, integer `minutes`, `script`, `no_agent == true`, `workdir == "/opt/data"` exactly).
- Deploy removes plaintext `.env` at the end. Persistent named volumes (`tailscale-serve-config`, `browser-tunnel-authorized-keys`, `browser-tunnel-state`) are never deleted by cleanup.
- The embeddings overlay is selected when `GBRAIN_EMBEDDINGS_ENABLED=true` or `MNEMOSYNE_DEPLOY_MODE` is `pilot`/`backup`, exactly once and after browser-control in `COMPOSE_FILE`. The effective strict boolean is written to `.env` and `GITHUB_ENV`. Deploy verifies the selected `embeddings` service is healthy, or verifies it is absent when not selected; deploy never runs `enable-embeddings` or `embed-backfill`.

## Manual gbrain embedding activation

Run `gbrain-embedding-backfill.yml` only after a successful deploy with the
embeddings overlay selected and healthy. Enter the exact confirmation
`ENABLE_AND_BACKFILL`. The workflow requires the repository variable
`GBRAIN_EMBEDDINGS_ENABLED=true`, validates the existing container prefix and
running Hermes container, verifies the container has non-empty
`GBRAIN_EMBEDDING_MODEL` and `GBRAIN_EMBEDDING_DIMENSIONS`, then runs
`josemar-gbrain enable-embeddings` followed by `embed-backfill` with
`docker exec --user hermes --workdir /opt/data`, explicitly setting
`HOME=/opt/data`, `HERMES_HOME=/opt/data`, `GBRAIN_HOME=/opt/data`, and
`XDG_CONFIG_HOME=/opt/data/.config`. The workflow preflights that identity and
path, then performs a same-identity post-backfill smoke test. It is fail-closed
and retry-safe, never prints environment values or secrets, and never rebuilds,
deploys, or removes containers.

## Stop Workflow Notes

- `stop-service.yml` tears down with the MAXIMAL overlay superset (base +
  vault-recovery + browser-control + embeddings + mnemosyne +
  mnemosyne-backup) plus the `aux-ml`, `browser-control`, and `recovery`
  profiles, with `--remove-orphans`, so ANY prior overlay service is removed
  even when the current deployment runs a smaller composition.
- It verifies hermes/aux-ml/syncthing/tailscale/vault-recovery-uploader/
  vault-recovery-recover/browser-tunnel/embeddings/
  mnemosyne-backup-uploader/mnemosyne-backup-recover containers are no longer
  running.
- Volumes are preserved.

## Privacy Workflow Notes

- Runs gitleaks and `scripts/pii_guard.py`.
- Fails on medium/high-confidence PII findings.

## Troubleshooting

- If workflow is queued: check runner online status.
- If Docker permission errors occur: add runner user to `docker` group and restart runner.
- If deploy fails health checks: inspect `docker compose logs` on the runner host.
- If state sync fails: verify `WORKSPACE_STATE_REPO` and `WORKSPACE_REPO_TOKEN`.
