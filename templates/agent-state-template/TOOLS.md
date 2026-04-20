# TOOLS.md

Core capability notes for a fresh state repo.

## Skill Ownership

- Core platform skills are shipped by the main repository image.
- User-specific skills belong only in this private state repo under `skills/`.

## Repo-Shipped Core Skills

### vault-gateway

- Source of truth: `skills-factory/vault-gateway/` in the main repo
- Runtime path: `/opt/josemar/skills/vault-gateway/`
- Purpose: single public entrypoint for Obsidian vault operations
- Usage style: strict `route` + `payload` contract

### aux-ml

- Source of truth: `skills-factory/aux-ml/` in the main repo
- Runtime path: `/opt/josemar/skills/aux-ml/`
- Purpose: submit/poll queue-based OCR and long-running auxiliary ML jobs

### workspace-sync

- Source of truth: `skills-factory/workspace-sync/` in the main repo
- Runtime path: `/opt/josemar/skills/workspace-sync/`
- Purpose: workspace git status/diff/log/commit/push/pull/sync operations

## Auxiliary ML Integration

The auxiliary ML service is optional and profile-gated.

Enable in `.env`:

```bash
AUX_ML_ENABLED=true
COMPOSE_PROFILES=aux-ml
```

Then start/restart services:

```bash
docker compose up -d --build
```

## Runtime Checks

Before using a capability, verify it exists in the active runtime environment:

- Confirm repo-shipped skill is present in `/opt/josemar/skills/`
- Confirm optional services (like `aux-ml`) are running when required
- If a capability is missing, report it clearly and continue with available paths
