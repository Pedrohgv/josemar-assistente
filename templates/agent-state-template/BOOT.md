# BOOT.md

Startup checklist for fresh deployments.

## First Run

1. Complete bootstrap if `SOUL.md`, `IDENTITY.md`, `USER.md`, and `AGENTS.md` are missing.
2. Verify gateway access via Web UI.
3. Approve device pairing if prompted.

## Core Capability Sanity Checks

1. Confirm core repo-shipped skills are present at `/opt/josemar/skills/`.
2. Confirm workspace sync is configured (`WORKSPACE_STATE_REPO`, token/branch settings).
3. Confirm `.sync-manifest` exists in workspace root.

## Optional Service Checks

Run these only when optional features are enabled:

1. If `AUX_ML_ENABLED=true`, ensure `COMPOSE_PROFILES=aux-ml` is set.
2. Confirm `aux-ml` container is healthy before requesting OCR jobs.

## Safe Defaults

- Do not assume user-specific skills exist.
- Use only verified capabilities.
- Report missing dependencies with exact next steps.
