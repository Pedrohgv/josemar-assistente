# OpenClaw Config Recovery Runbook

## Problem Statement

OpenClaw maintains backup snapshots of its gateway config (`openclaw.json`) in the runtime volume:

| File | Purpose |
|------|---------|
| `openclaw.json` | Active config |
| `openclaw.json.last-good` | Last known-good snapshot |
| `openclaw.json.bak` | Backup snapshot |
| `openclaw.json.bak.*` | Additional numbered backups |

On startup, if OpenClaw detects a mismatch between the active config and its `last-good` snapshot (a condition called `missing-meta-vs-last-good`), it **auto-restores** from the backup lineage — falling through `.last-good` → `.bak` → `.bak.*`.

When env variable names change in the repo source (e.g. `PEDRO_TELEGRAM_ID` → `PRIMARY_TELEGRAM_ID`), the entrypoint overwrites the active config from source. But before the fix, `.bak` files were left untouched in the Docker volume. This meant that any `missing-meta-vs-last-good` recovery would restore stale content with obsolete env keys.

## Root Cause

The `docker-entrypoint.sh` copied source config over `openclaw.json` and `.last-good`, but **not** `.bak`. The stale `.bak` was a recovery path that could revert the config to pre-migration state on any startup that triggered recovery.

## Fix

The entrypoint now overwrites **all** config snapshots from repo source before OpenClaw starts:

```sh
if [ -f "/root/.openclaw-source/openclaw.json" ]; then
    cp /root/.openclaw-source/openclaw.json /root/.openclaw/openclaw.json
    cp /root/.openclaw-source/openclaw.json /root/.openclaw/openclaw.json.last-good
    for bak in /root/.openclaw/openclaw.json.bak /root/.openclaw/openclaw.json.bak.*; do
        [ -f "$bak" ] && cp /root/.openclaw-source/openclaw.json "$bak"
    done
fi
```

Additionally, all snapshots pass through `TELEGRAM_ENABLED` boolean normalization to ensure the string `"${TELEGRAM_ENABLED}"` in JSON5 is replaced with the proper boolean value (`true`/`false`).

## Verification

### Before Fix (old entrypoint)

Planted a stale `openclaw.json.bak` containing `PEDRO_TELEGRAM_ID` and `192.168.15.200` into the Docker volume. Started the container with the old entrypoint:

| Snapshot | Size | `PEDRO_TELEGRAM_ID` | `PRIMARY_TELEGRAM_ID` |
|----------|------|---------------------|-----------------------|
| `openclaw.json` | 7382 B | 0 | 1 |
| `.last-good` | 7382 B | 0 | 1 |
| `.bak` | 1191 B | **1** | **0** |

The `.bak` file was **not overwritten** and remained stale.

### After Fix (new entrypoint)

Same volume with stale `.bak`. Rebuilt image with the fixed entrypoint and restarted:

| Snapshot | Size | `PEDRO_TELEGRAM_ID` | `PRIMARY_TELEGRAM_ID` |
|----------|------|---------------------|-----------------------|
| `openclaw.json` | 7382 B | 0 | 1 |
| `.last-good` | 7382 B | 0 | 1 |
| `.bak` | 7382 B | 0 | **1** |

All three snapshots aligned with repo source. Stable across multiple restarts.

## Config Flow

```
repo: config/openclaw.json (JSON5, source of truth)
  → mounted read-only at /root/.openclaw-source/
  → entrypoint copies to /root/.openclaw/openclaw.json
  → entrypoint overwrites .last-good, .bak, .bak.*
  → entrypoint normalizes TELEGRAM_ENABLED to boolean
  → OpenClaw starts with clean, aligned config
```

## Troubleshooting

### "Config auto-restored from backup" on startup

This indicates OpenClaw detected `missing-meta-vs-last-good` and restored from a backup. If it happens repeatedly after deploying new config:

1. Check all snapshots for stale content:
   ```bash
   docker compose exec openclaw sh -c \
     'for f in /root/.openclaw/openclaw.json*; do echo "==$f"; grep -c "OBSOLETE_KEY" "$f" 2>/dev/null; done'
   ```

2. Verify the entrypoint is overwriting all snapshots (check lines 25-35 of `docker-entrypoint.sh`).

3. If stale content persists, the entrypoint may not be reaching the overwrite block. Check that `/root/.openclaw-source/openclaw.json` exists in the container.

### Changing env variable names in config

When renaming env vars referenced in `openclaw.json`:

1. Update `config/openclaw.json` in the repo with the new var name.
2. Update `.env.example` and `docker-compose.yml` environment section.
3. Update the deployment secrets/vars (GitHub Actions, `.env` on server).
4. Rebuild and redeploy — the entrypoint will overwrite all snapshots from source.

No manual volume cleanup is needed. The entrypoint handles it automatically on next startup.