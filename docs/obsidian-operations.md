# Obsidian Operations Runbook

This runbook documents the full setup and operations flow for the Obsidian vault stack:

- Syncthing device sync between server and laptop
- Daily rotating backups to Google Drive (rclone)
- Restore and troubleshooting procedures

Use this file as the source of truth for future operators.

## Architecture Summary

- `obsidian-vault` Docker volume stores notes and attachments (not git-versioned).
- `tailscale` sidecar provides private network connectivity for sync.
- `syncthing` container syncs the vault to client devices through the sidecar namespace.
- `obsidian-backup` container uploads one rotating snapshot per day to Google Drive.
- `openclaw` mounts the same vault volume at `/root/.openclaw/obsidian`.

Key volumes:

- `josemar-assistente_obsidian-vault`
- `josemar-assistente_syncthing-config`
- `josemar-assistente_tailscale-state`
- `josemar-assistente_obsidian-backup-state`

## Required GitHub Configuration

### Secrets

- `RCLONE_CONFIG_B64`: base64-encoded `rclone.conf` containing remote `gdrive`
- `TS_AUTHKEY` (optional, recommended): Tailscale auth key for unattended server bootstrap/login during deploy

### Variables

- `TZ`: optional, defaults to `America/Sao_Paulo`
- `SYNCTHING_GUI_BIND_IP`: optional, defaults to `127.0.0.1` (recommended)
- `TAILSCALE_HOSTNAME`: optional node name for sidecar (default `josemar-server`)
- `TS_EXTRA_ARGS`: optional extra flags passed to `tailscale up`

Notes:

- Server-side Tailscale runs as a Docker sidecar (`tailscale` service), not as a host package.
- If `TS_AUTHKEY` is set in GitHub secrets, sidecar login is unattended during deploy.

Backup behavior defaults are defined in `docker-compose.yml`:

- `OBSIDIAN_BACKUP_TIME=03:15`
- `OBSIDIAN_BACKUP_RUN_ON_START=false`
- `OBSIDIAN_BACKUP_SLOTS=5`
- `OBSIDIAN_GDRIVE_REMOTE=gdrive`
- `OBSIDIAN_GDRIVE_PATH=Josemar/obsidian-backups`

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
dc logs --tail=80 obsidian-backup
```

Expected:

- `openclaw`, `tailscale`, `syncthing`, `obsidian-backup` are `Up`
- Tailscale reports a `100.x.y.z` address

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

### Manual backup test

```bash
dc exec -T obsidian-backup sh /scripts/obsidian-backup.sh
dc logs --tail=100 obsidian-backup
```

Verify runtime config path:

```bash
dc exec -T obsidian-backup ls -l /config/rclone/rclone.conf
```

### Verify slots in Google Drive

```bash
dc exec -T obsidian-backup rclone lsf gdrive:Josemar/obsidian-backups
```

Expected files/directories:

- `slot-1/` ... `slot-5/`
- `slot-1.json` ... `slot-5.json`

### Rotation behavior

- Daily run writes to one slot.
- After slot 5, it wraps to slot 1.
- The newest snapshot replaces the oldest slot in the cycle.

## Restore Procedure (Safe)

1. Stop writers:

```bash
dc stop openclaw syncthing
```

2. Get vault volume mountpoint:

```bash
VAULT_PATH=$(docker volume inspect josemar-assistente_obsidian-vault --format '{{ .Mountpoint }}')
echo "$VAULT_PATH"
```

3. Restore selected slot (example `slot-3`) into vault:

```bash
docker run --rm \
  -v "$VAULT_PATH:/restore" \
  -v "josemar-assistente_obsidian-rclone-config:/config/rclone:ro" \
  -e RCLONE_CONFIG=/config/rclone/rclone.conf \
  rclone/rclone:latest sync gdrive:Josemar/obsidian-backups/slot-3 /restore
```

4. Start services:

```bash
dc up -d syncthing openclaw
```

5. Confirm sync health:

```bash
dc logs --tail=80 syncthing
```

## What Persists Across Redeploy

Normal redeploy preserves:

- Syncthing identity, pairing, folder settings (`syncthing-config`)
- Tailscale node state (`tailscale-state`)
- Vault files (`obsidian-vault`)
- Backup ring pointer (`obsidian-backup-state`)

Workflow `fresh_start=true` removes only:

- `openclaw-workspace`

It does not remove Obsidian volumes.

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
dc exec -T obsidian-backup sh /scripts/obsidian-backup.sh
dc logs --tail=80 obsidian-backup
```

If all commands succeed and connection is `connected: true`, setup is healthy.
