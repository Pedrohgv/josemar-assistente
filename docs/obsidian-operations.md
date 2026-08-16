# Obsidian Operations Runbook

This runbook documents the full setup and operations flow for the Obsidian vault stack:

- Syncthing device sync between server and laptop
- Encrypted disaster-recovery backups (vault-recovery lane)
- Restore and troubleshooting procedures

Use this file as the source of truth for future operators.

## Architecture Summary

- `obsidian-vault` Docker volume stores notes and attachments (not git-versioned).
- `tailscale` sidecar provides private network connectivity for sync.
- `syncthing` container syncs the vault to client devices through the sidecar namespace.
- `hermes` mounts the same vault volume at `/opt/data/obsidian`.
- The **vault-recovery lane** (default deployment composition) exports the
  vault **plus** the complete `/opt/data/.gbrain` state into immutable
  encrypted remote generations every day and retains the newest 14. It is the
  replacement for the retired plaintext `obsidian-backup` service. See
  `docs/vault-recovery-operations.md` for the full runbook (export, upload,
  recovery, install, rollback, drills).

Key volumes:

- `josemar-assistente_obsidian-vault`
- `josemar-assistente_syncthing-config`
- `josemar-assistente_tailscale-state`
- `josemar-assistente_vault-recovery-staging`
- `josemar-assistente_vault-recovery-uploader-state`
- `josemar-assistente_vault-recovery-recovery`

## Required GitHub Configuration

### Secrets

- `RCLONE_CONFIG_B64`: base64-encoded `rclone.conf` containing the
  `vault-recovery-crypt` remote (type `crypt`, non-empty underlying remote and
  password). **Required for every deployment**: the deploy workflow fails when
  it is missing or invalid — the encrypted backup lane must never silently
  disappear.
- `TS_AUTHKEY` (optional, recommended): Tailscale auth key for unattended server bootstrap/login during deploy

### Variables

- `TZ`: optional, defaults to `America/Sao_Paulo`
- `SYNCTHING_GUI_BIND_IP`: optional, defaults to `127.0.0.1` (recommended)
- `TAILSCALE_HOSTNAME`: optional node name for sidecar (default `josemar-server`)
- `TS_EXTRA_ARGS`: optional extra flags passed to `tailscale up`

Notes:

- Server-side Tailscale runs as a Docker sidecar (`tailscale` service), not as a host package.
- If `TS_AUTHKEY` is set in GitHub secrets, sidecar login is unattended during deploy.
- Server-side Syncthing runs as the same UID/GID as Hermes (`HERMES_UID`/`HERMES_GID`, default `10000`) so synced vault files remain writable by the Hermes runtime and native gbrain.

Existing deployments that previously ran Syncthing as root need a one-time volume ownership migration before restarting Syncthing with the non-root user:

```bash
docker run --rm \
  -v josemar-assistente_obsidian-vault:/vault \
  -v josemar-assistente_syncthing-config:/syncthing-config \
  alpine:3.20 \
  chown -R 10000:10000 /vault /syncthing-config
```

Fresh deployments do not need this migration.

## Retired plaintext lane (Phase 3)

The plaintext `obsidian-backup` service (rclone rotating snapshots to Google
Drive, `OBSIDIAN_BACKUP_*` / `OBSIDIAN_GDRIVE_*` variables,
`scripts/obsidian-backup.sh` / `scripts/obsidian-backup-daemon.sh`, the
`obsidian-backup-state` volume) is **retired** and removed from the default
deployment. It had no crypt boundary and no manifest. The encrypted
vault-recovery lane is the default backup composition; the deploy workflow
fails when the crypt remote is not configured, so backups are never silently
lost.

**Existing remote plaintext slots are NOT deleted automatically.** The slots
under `Josemar/obsidian-backups` (`slot-1/` … `slot-5/` plus the slot pointer
files) remain on Google Drive as historical material. Deleting them is an
**explicit operator decision** after the migration is proven (see
`docs/vault-recovery-operations.md` → "Migration sequence"), and must be done
manually — no deployment automation ever deletes them.

### Manual historical recovery from plaintext slots (operator-only)

This reads an old plaintext slot into a scratch dir; it never re-creates the
plaintext lane:

```bash
# 1. Stop vault writers.
dc stop hermes syncthing

# 2. List the historical slots.
dc exec -T tailscale sh -c 'ls' >/dev/null 2>&1 || true
docker run --rm \
  -v josemar-assistente_obsidian-rclone-config:/config/rclone:ro \
  -e RCLONE_CONFIG=/config/rclone/rclone.conf \
  rclone/rclone:latest lsf gdrive:Josemar/obsidian-backups

# 3. Download the chosen slot into a scratch dir and inspect it.
mkdir -p /tmp/obsidian-historical-slot-3
docker run --rm \
  -v josemar-assistente_obsidian-rclone-config:/config/rclone:ro \
  -v /tmp/obsidian-historical-slot-3:/restore \
  -e RCLONE_CONFIG=/config/rclone/rclone.conf \
  rclone/rclone:latest sync gdrive:Josemar/obsidian-backups/slot-3 /restore
```

`dc exec` against the retired container is no longer possible — the service
does not exist in the deployment. The `rclone/rclone` image is pulled on
demand for these operator commands.

## Local/Manual rclone Config Loading

When not deploying through GitHub Actions, load `rclone.conf` into Docker volume `obsidian-rclone-config`:

```bash
mkdir -p credentials/rclone
# Place your config at credentials/rclone/rclone.conf

docker volume create josemar-assistente_obsidian-rclone-config
docker run --rm \
  -v "$PWD/credentials/rclone:/src:ro" \
  -v "josemar-assistente_obsidian-rclone-config:/config/rclone" \
  alpine:3.20 \
  sh -c 'cp /src/rclone.conf /config/rclone/rclone.conf && chmod 600 /config/rclone/rclone.conf'
```

## Tailscale Auth Key (for unattended setup)

If you want non-interactive `tailscale up` in the server sidecar, create a pre-auth key:

1. Open Tailscale admin: `https://login.tailscale.com/admin/settings/keys`
2. Click **Generate auth key**.
3. Recommended settings for this server use case:
   - Reusable: enabled
   - Ephemeral: disabled
   - Expiry: choose a controlled window (or no expiry only if your policy allows it)
   - Tags: optional (for ACL-driven server identity)
4. Save the key securely (you will only see the full value once).

Usage:

```bash
sudo tailscale up --auth-key=<TS_AUTHKEY>
```

## Network Requirement (Tailscale Sidecar)

Recommended topology:

1. Server runs `tailscale` as a Docker sidecar service.
2. `syncthing` runs in the same network namespace (`network_mode: service:tailscale`).
3. Laptop runs native Tailscale client.
4. In Syncthing, configure each device address as `tcp://<tailscale-ip>:22000`.

This keeps sync traffic on your private tailnet without opening router/firewall ports and avoids host-level Tailscale installation.

## How To Find The Active Compose Path On Server

GitHub Actions deploys from the runner workspace path, which may differ from `/root/...`.

Run this on the server:

```bash
export COMPOSE_FILE=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' josemar-assistente)
export COMPOSE_PROJECT=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' josemar-assistente)
alias dc='docker compose -f "$COMPOSE_FILE" --project-name "$COMPOSE_PROJECT"'
```

Then use `dc` for all operational commands.

## Post-Deploy Validation

Run on server:

```bash
dc ps
dc logs --tail=80 tailscale
dc logs --tail=80 syncthing
```

Expected:

- `hermes`, `tailscale`, `syncthing`, `vault-recovery-uploader` are `Up`
- Tailscale reports a `100.x.y.z` address

Check the vault-recovery lane explicitly (the deploy workflow runs these as
post-start checks; they are repeated here for manual validation):

```bash
# Uploader running + exactly one export cron.
dc ps vault-recovery-uploader
dc exec -T hermes /opt/hermes/.venv/bin/python3 -c \
  'import json; data=json.load(open("/opt/data/cron/jobs.json")); print([j for j in data["jobs"] if j.get("name")=="vault-recovery-export"])'

# Plaintext lane absence: the retired service must not exist.
docker ps -a --format '{{.Names}}' | grep -F obsidian-backup || echo "obsidian-backup absent (retired)"
```

Check runtime state explicitly:

```bash
dc exec -T tailscale tailscale ip -4
dc exec -T tailscale tailscale status
ss -lntup | grep -E ':8384'
```

Expected model:

- `8384` on `127.0.0.1` (GUI/API)
- Syncthing sync port `22000` reachable via server Tailscale IP

## One-Time Pairing: Laptop <-> Server

### 1) Install Tailscale on laptop

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled
sudo tailscale up
tailscale ip -4
```

Save both Tailscale IPv4 addresses.

### 1.1) Ensure server sidecar is connected

On server:

```bash
dc ps
dc logs --tail=80 tailscale
dc exec -T tailscale tailscale ip -4
```

If no IP is returned, verify `TS_AUTHKEY` in deploy secrets and redeploy.

Manual fallback (without `TS_AUTHKEY`):

```bash
dc exec -T tailscale tailscale up
```

Open the login URL printed by the command, approve the node, then re-run `dc exec -T tailscale tailscale ip -4`.

### 1.2) Ensure Tailscale survives laptop reboots

On the laptop, verify the daemon is enabled as a system service:

```bash
systemctl is-enabled tailscaled
systemctl status tailscaled --no-pager
```

If it is not enabled, run:

```bash
sudo systemctl enable --now tailscaled
```

After reboot, validate connection state:

```bash
tailscale status
tailscale ip -4
```

### 2) Install Syncthing on laptop

```bash
sudo apt update
sudo apt install -y syncthing
```

### 3) Start Syncthing on laptop

Preferred (persistent):

```bash
systemctl --user enable --now syncthing
systemctl --user status syncthing --no-pager
```

Open laptop UI: `http://127.0.0.1:8384`

### 4) Open server Syncthing UI

If `SYNCTHING_GUI_BIND_IP=127.0.0.1` (default), use SSH tunnel:

```bash
ssh <server-host> -L 8384:127.0.0.1:8384
```

Then open:

- `http://127.0.0.1:8384`

If GUI was intentionally exposed, use:

- `http://<SERVER_PRIVATE_IP>:8384`

### 5) Add server on laptop

- Device ID: server Syncthing device ID
- Address: `tcp://<SERVER_TAILSCALE_IP>:22000` (server IP from `dc exec -T tailscale tailscale ip -4`)

Important: do not use `http://...:8384` as device address.

### 6) Accept device on server

Approve the laptop in server UI.

### 7) Share folder

On server UI, share `obsidian-vault` with laptop.

On laptop UI, accept folder and choose local path, for example:

- `/home/<user>/Obsidian/JosemarVault`

Open that folder in Obsidian desktop.

## Tailscale-Only Syncthing Policy

Server and laptop should both use:

- `Global Discovery`: disabled
- `Relaying`: disabled
- `NAT Traversal`: disabled
- `Local Discovery`: disabled

For each device, set explicit peer address to the other Tailscale endpoint:

- `tcp://<peer-tailscale-ip>:22000`

## Backup Operations

The backup lane is **vault-recovery** (encrypted, default-on): a daily export
cron (04:00 local) stages the full vault + `.gbrain` state on the
`vault-recovery-staging` volume; the `vault-recovery-uploader` service
uploads each generation through the `vault-recovery-crypt` rclone remote and
retains the newest 14 committed remote generations. Full operations,
verification commands, recovery, install and rollback are in
`docs/vault-recovery-operations.md`.

Quick health checks:

```bash
# Latest staged generation and uploader ack.
dc exec -T hermes su -s /bin/sh hermes -c \
  '/opt/hermes/.venv/bin/python3 -I /opt/josemar/scripts/vault_recovery_core.py latest'
dc exec -T vault-recovery-uploader cat /state/last-uploaded-generation 2>/dev/null || true
# Remote committed generations.
dc exec -T vault-recovery-uploader rclone --config /config/rclone/rclone.conf lsf \
  vault-recovery-crypt:Josemar/vault-recovery/committed
```

## Restore Procedure

Full restore of the vault **and** the `.gbrain` state from an encrypted
generation is documented in `docs/vault-recovery-operations.md` →
"Phase-2 operations" (recover download → disposable-doctor verify → journaled
install → rollback). Restore is an operator-only sequence: stop Hermes, server
Syncthing and all gbrain jobs, pause paired-device writers, and follow the
procedure — it is never automated.

> Historical plaintext slots (retired lane) are restored manually with the
> operator-only procedure under "Retired plaintext lane" above; those slots
> contain ONLY vault files, never `.gbrain` state, and have no manifest or
> checksums.

## What Persists Across Redeploy

Normal redeploy preserves:

- Syncthing identity, pairing, folder settings (`syncthing-config`)
- Tailscale node state (`tailscale-state`)
- Vault files (`obsidian-vault`)
- Vault-recovery staged generations, uploader ledger and retention state
  (`vault-recovery-staging`, `vault-recovery-uploader-state`,
  `vault-recovery-recovery`)

The retired plaintext lane's `obsidian-backup-state` volume (ring pointer)
may still exist on the host; it is no longer mounted or used. It can be
removed manually after the migration is proven (see
`docs/vault-recovery-operations.md` → "Migration sequence") — deployment
automation never deletes it.

Workflow `fresh_start=true` is disabled after moving private state into `/opt/data`.
Use a manual, reviewed cleanup instead so runtime state and credentials are not removed accidentally. Obsidian volumes should not be removed during agent-state cleanup.

## Troubleshooting

### Symptom: `Disconnected (Unused)` on laptop

Check connectivity from laptop:

```bash
nc -vz <SERVER_TAILSCALE_IP> 22000
```

If reachable, verify device address is TCP, not HTTP:

```bash
syncthing cli --gui-address=127.0.0.1:8384 --gui-apikey=<LAPTOP_API_KEY> \
  config devices <SERVER_DEVICE_ID> addresses 0 get
```

Expected value:

- `tcp://<SERVER_TAILSCALE_IP>:22000`

### Symptom: Server log says `unknown device`

Server has not approved/added laptop device yet.

Check pending requests:

```bash
dc exec -T syncthing syncthing cli --gui-address=127.0.0.1:8384 --gui-apikey=<SERVER_API_KEY> show pending devices
```

Approve in server UI or add explicitly.

### Symptom: sync connects then drops repeatedly

Usually duplicate Syncthing instances on laptop.

Check process list:

```bash
pgrep -af syncthing
```

Keep one managed instance (prefer user service).

## Weekly Health Check

Run on server:

```bash
dc ps
dc exec -T syncthing syncthing cli --gui-address=127.0.0.1:8384 --gui-apikey=<SERVER_API_KEY> show connections
# Vault-recovery lane: uploader up, latest staged generation, remote list.
dc ps vault-recovery-uploader
dc exec -T hermes su -s /bin/sh hermes -c \
  '/opt/hermes/.venv/bin/python3 -I /opt/josemar/scripts/vault_recovery_core.py latest'
dc exec -T vault-recovery-uploader rclone --config /config/rclone/rclone.conf lsf \
  vault-recovery-crypt:Josemar/vault-recovery/committed
# A monthly encrypted drill is recommended; see docs/vault-recovery-operations.md
# → "Disaster-recovery drill" (automated: make test-vault-recovery-dr-drill).
```

If all commands succeed and connection is `connected: true`, setup is healthy.
