# HEARTBEAT.md

Lightweight periodic checks.

## Checklist

1. Confirm gateway is responding.
2. Confirm workspace sync is healthy (no repeated sync failures).
3. If aux-ml is enabled, confirm queue/health endpoint is reachable.
4. If nothing needs action, reply `HEARTBEAT_OK`.

## Noise Control

- Avoid repeated status messages when nothing changed.
- Escalate only actionable issues.
