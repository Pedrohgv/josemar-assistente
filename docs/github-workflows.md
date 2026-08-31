# GitHub Workflows: Configuration and Operations

This document is the canonical operator/catalog reference for `.github/workflows/**`. The scoped `.github/workflows/AGENTS.md` contains the harness change rules and tells workers when this document or a subsystem runbook must be consulted and updated.

Source workflow YAML remains executable truth. If a catalog entry here disagrees with a workflow, fix the documentation in the same change that establishes the intended behavior.

## Prerequisites

All workflows run on a self-hosted runner.

1. Install a GitHub Actions runner on the deployment server.
2. Ensure the runner user can run Docker commands.
3. Verify runner status in GitHub Settings > Actions > Runners.

## Workflow index

- `deploy-to-home-server.yml`: build and deploy the Hermes stack.
- `fast-tests.yml`: run `make verify` on pull requests, pushes to `main`, and manual dispatch with pinned test dependencies installed into an isolated runner temp directory.
- `gbrain-embedding-backfill.yml`: manual destructive gbrain embedding activation and retry-safe backfill; it never deploys or rebuilds.
- `stop-service.yml`: stop all project services safely.
- `privacy-scan.yml`: secret and PII scanning on changes.
- `test-workflow.yml`: basic runner connectivity test.

When adding, deleting, or renaming a workflow, update this index and `.github/workflows/AGENTS.md` if the change creates a new class of documentation dependency.

## Repository secrets catalog

| Secret | Required | Purpose |
| --- | --- | --- |
| `ZAI_API_KEY` | Yes | Z.AI/GLM provider key |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token |
| `PRIMARY_TELEGRAM_ID` | Yes | Primary allowlisted Telegram user |
| `WORKSPACE_REPO_TOKEN` | Yes | PAT for private state repo sync |
| `RCLONE_CONFIG_B64` | Yes | Base64 `rclone.conf` for encrypted backup lanes. Required for every deployment; it must contain the validated `vault-recovery-crypt` remote. |
| `DEEPSEEK_API_KEY` | Yes | DeepSeek provider key for the default model |
| `OLLAMA_API_KEY` | No | Optional provider key |
| `TAVILY_API_KEY` | No | Optional Tavily key enabling `web_search` / `web_extract` through auto-detection |
| `GOG_KEYRING_PASSWORD` | No | Optional gog keyring passphrase |
| `TS_AUTHKEY` | No | Optional unattended Tailscale login |
| `HERMES_API_SERVER_KEY` | No | Required when `HERMES_API_SERVER_ENABLED=true` |
| `HERMES_DASHBOARD_SESSION_TOKEN` | Yes | Dashboard session token used by Hermes Desktop for REST/WebSocket access |
| `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` | Yes | Browser dashboard Basic Auth password |
| `HERMES_DASHBOARD_BASIC_AUTH_SECRET` | Yes | Stable dashboard session signing secret |
| `BROWSER_TUNNEL_AUTHORIZED_KEY` | No | Required when `BROWSER_CONTROL_ENABLED=true`; single-line SSH public key for the reverse-tunnel sidecar |

Never document secret values. A workflow change that adds/removes/renames a secret or changes when it is required must update this table and any affected setup/runbook documentation in the same PR.

## Repository variables catalog

| Variable | Required | Purpose |
| --- | --- | --- |
| `WORKSPACE_STATE_REPO` | Yes | Private state repo URL |
| `JOSEMAR_CONTAINER_PREFIX` | No | Docker container name prefix (default `josemar`) |
| `TZ` | No | Timezone (default `America/Sao_Paulo`) |
| `SYNCTHING_GUI_BIND_IP` | No | Syncthing GUI bind IP (default `127.0.0.1`) |
| `TAILSCALE_HOSTNAME` | No | Tailscale node name |
| `TS_EXTRA_ARGS` | No | Extra flags for the Tailscale sidecar |
| `AUX_ML_ENABLED` | No | Enable/disable aux-ml profile |
| `AUX_ML_MEMORY_LIMIT` | No | aux-ml container memory limit |
| `AUX_ML_MEMORY_LIMIT_MB` | No | Runtime memory budget for aux-ml |
| `AUX_ML_MAX_QUEUE` | No | Maximum aux-ml queue length |
| `AUX_ML_JOB_TIMEOUT_SECONDS` | No | aux-ml job timeout |
| `AUX_ML_POLL_INTERVAL_SECONDS` | No | aux-ml poll interval |
| `AUX_ML_LLAMACPP_TIMEOUT_SECONDS` | No | llama.cpp timeout |
| `AUX_ML_ENFORCE_MEMORY_LIMIT` | No | Enforce aux-ml memory check |
| `AUX_ML_OCR_MAX_PAGES` | No | OCR maximum pages per file |
| `HERMES_BASE_IMAGE` | No | Production Hermes base image; must match the reviewed `Dockerfile.hermes` pin. This is a compatibility tripwire, not a free override. |
| `GOGCLI_REF` | No | Override pinned gogcli ref |
| `HERMES_API_SERVER_ENABLED` | No | Enable Hermes API server for clients such as Hermes Desktop (default `false`) |
| `HERMES_API_SERVER_BIND_IP` | No | Host bind IP for API server port (default `127.0.0.1`) |
| `HERMES_API_SERVER_PORT` | No | Host/API server port (default `8642`) |
| `HERMES_API_SERVER_CORS_ORIGINS` | No | Optional comma-separated CORS origins |
| `HERMES_API_SERVER_MODEL_NAME` | No | Display/model name advertised to clients (default `Josemar`) |
| `HERMES_DEFAULT_PROFILE_DISPLAY_NAME` | No | URL-safe dashboard profile label for the base Hermes profile (default `Josemar`) |
| `HERMES_DASHBOARD_BIND_IP` | No | Host bind IP for dashboard port (default `127.0.0.1`; `0.0.0.0` is rejected) |
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` | No | Browser dashboard Basic Auth username (default `admin`) |
| `BROWSER_CONTROL_ENABLED` | No | Enable optional browser-control overlay (default `false`). See `browser-control.md`. |
| `BROWSER_CONTROL_SUBNET` | No | Internal browser-control Docker subnet (default `172.31.250.0/29`) |
| `BROWSER_CONTROL_GATEWAY` | No | Browser-control gateway IPv4 (default `172.31.250.1`) |
| `BROWSER_CONTROL_HERMES_IP` | No | Hermes static IPv4 on browser-control network (default `172.31.250.2`) |
| `BROWSER_CONTROL_TAILSCALE_IP` | No | Tailscale static IPv4 on browser-control network (default `172.31.250.3`) |
| `MNEMOSYNE_DEPLOY_MODE` | No | Mnemosyne overlay mode, default `off`; allowed `off`, `pilot`, `backup`. See `mnemosyne-operations.md`. |
| `MNEMOSYNE_BACKUP_EXPORT_INTERVAL` | Conditional | Required when `MNEMOSYNE_DEPLOY_MODE=backup`; positive integer minutes, no leading zeros, maximum 10080. |
| `GBRAIN_EMBEDDINGS_ENABLED` | No | Strict `true`/`false` switch (default `false`) selecting the gbrain embeddings Compose overlay; it does not enable/backfill vectors by itself. |
| `TASKNOTES_DAILY_LINKS_ENABLED` | No | Strict master switch, default `true`, for TaskNotes Daily Note link projection/reconciliation. See `tasknotes-mcp.md`. |
| `TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED` | No | Strict slave switch, default `true`, gating both pre-mutation and refresh-cycle Daily Note link reconciliation; effective only while the master switch is enabled. |

Security defaults: keep Syncthing/dashboard/API binds on localhost unless the documented secured network path requires otherwise. Do not expose the API on `0.0.0.0` without its key and a trusted network path.

A workflow change that adds/removes/renames a repository variable, changes its default, validation, allowed values, or operational meaning must update this catalog and every affected subsystem runbook in the same PR.

## Deployment invariants and routing

`deploy-to-home-server.yml` contains several high-risk fail-closed contracts. Keep detailed subsystem behavior in its canonical runbook rather than duplicating the full implementation here.

- Deployment writes `.env` from repository secrets/variables and removes plaintext `.env` after use.
- All input/preflight validation must complete before destructive service teardown or mutation where the workflow currently guarantees that ordering.
- Compose selection must be validated before mutation; teardown uses the documented maximal overlay/profile superset without deleting named volumes.
- Vault recovery is a default deployment lane. Its crypt-remote validation, remote readiness gate, portability proof, disaster-recovery drill, export job verification, secret-temp-file cleanup, and recovery ordering are canonical in `vault-recovery-operations.md`. Any workflow change touching those behaviors must update that runbook.
- Browser control is optional. Overlay selection, network/Tailscale Serve behavior, authorized-key handling, stale-route cleanup, and post-start verification are canonical in `browser-control.md`.
- Mnemosyne deployment modes, overlay ordering, backup interval validation, rclone validation, activation checks, and rollback/recovery are canonical in `mnemosyne-operations.md`.
- gbrain embeddings overlay selection and manual activation/backfill behavior are canonical in `gbrain-operations.md` and `memory-embeddings-evaluation.md` where applicable.
- TaskNotes Daily Note projection/reconciliation flags and deployment validation are canonical in `tasknotes-mcp.md`.
- aux-ml deployment variables/profile behavior are canonical in `aux-ml.md`.
- Workspace/state-repo deployment behavior must stay consistent with root `AGENTS.md`, `.sync-manifest`, and the relevant state-sync documentation/tests.

When changing a release gate, teardown/preflight ordering, runtime verification, overlay composition, or secret cleanup behavior, inspect the corresponding contract/runtime tests and update `tests/README.md` if the supported validation procedure changes.

## Manual gbrain embedding activation

Run `gbrain-embedding-backfill.yml` only after a successful deploy with the embeddings overlay selected and healthy. The workflow requires explicit destructive confirmation and `GBRAIN_EMBEDDINGS_ENABLED=true`, validates the existing Hermes container/runtime identity and embedding configuration, then runs the operator gbrain activation/backfill path and a same-identity smoke test.

It is fail-closed and retry-safe, never prints environment values or secrets, and never rebuilds, deploys, or removes containers. Detailed activation semantics belong in `gbrain-operations.md`.

## Stop workflow

`stop-service.yml` tears down the documented maximal overlay/profile superset with `--remove-orphans` so prior optional services are removed even if the current deployment runs a smaller composition. Named volumes are preserved. If the set of optional overlays/services changes, update the maximal teardown/verification logic and this summary together; update subsystem runbooks when their operational stop behavior changes.

## Privacy workflow

`privacy-scan.yml` runs repository secret/PII scanning, including gitleaks and `scripts/pii_guard.py`, and fails on findings at the configured enforcement threshold. Changes to public-artifact privacy policy must remain consistent with root `AGENTS.md` and the implementation/tests.

## Troubleshooting

- Queued workflow: check self-hosted runner online status.
- Docker permission error: verify the runner user has the documented Docker access and restart the runner after group changes.
- Deploy health failure: inspect the relevant Compose/container logs on the runner host.
- State-sync failure: verify the state repository configuration and token are configured; never print credential values.

## Documentation dependency matrix

| Change in `.github/workflows/**` | Required durable documentation inspection/update |
| --- | --- |
| Workflow add/delete/rename/purpose | This file's workflow index; `docs/README.md` when the documentation domain/routing changes |
| Secret add/delete/rename/requiredness/meaning | Secret catalog here + affected setup/operator runbook |
| Variable add/delete/rename/default/validation/meaning | Variable catalog here + affected subsystem runbook |
| Vault recovery deployment/release gate | `vault-recovery-operations.md` + relevant tests documentation |
| Browser-control overlay/deploy behavior | `browser-control.md` |
| Mnemosyne overlay/deploy/backup behavior | `mnemosyne-operations.md` |
| gbrain embedding activation/deploy behavior | `gbrain-operations.md`; `memory-embeddings-evaluation.md` when evaluation/activation criteria change |
| TaskNotes feature flags/deploy behavior | `tasknotes-mcp.md` and TaskNotes skill if runtime-agent behavior changes |
| aux-ml deploy behavior | `aux-ml.md` |
| Test/release validation invocation or gate | `tests/README.md` when contributor/harness validation procedure changes |
| Harness-facing workflow instructions | `.github/workflows/AGENTS.md` |

The workflow source may reveal additional affected documents. This matrix is a discovery floor, not an exhaustive exemption list.
