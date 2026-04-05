# Obsidian Operations Runbook

This runbook documents the full setup and operations flow for the Obsidian vault stack:

- Syncthing device sync between server and laptop
- Daily rotating backups to Google Drive (rclone)
- Restore and troubleshooting procedures

Use this file as the source of truth for future operators.

## Architecture Summary

- `obsidian-vault` Docker volume stores notes and attachments (not git-versioned).
- `syncthing` container syncs the vault to client devices.
- `obsidian-backup` container uploads one rotating snapshot per day to Google Drive.
- `openclaw` mounts the same vault volume at `/root/.openclaw/obsidian`.

Key volumes:

- `josemar-assistente_obsidian-vault`
- `josemar-assistente_syncthing-config`
- `josemar-assistente_obsidian-backup-state`

## Required GitHub Configuration

### Secrets

- `RCLONE_CONFIG_B64`: base64-encoded `rclone.conf` containing remote `gdrive`

### Variables

- `LAN_BIND_IP`: server LAN IPv4 (example: `192.168.15.200`)
- `TZ`: optional, defaults to `America/Sao_Paulo`

Backup behavior defaults are defined in `docker-compose.yml`:

- `OBSIDIAN_BACKUP_TIME=03:15`
- `OBSIDIAN_BACKUP_RUN_ON_START=false`
- `OBSIDIAN_BACKUP_SLOTS=5`
- `OBSIDIAN_GDRIVE_REMOTE=gdrive`
- `OBSIDIAN_GDRIVE_PATH=Josemar/obsidian-backups`

## Network Requirement (Proxmox + Router)

The VM should keep a stable LAN IP.

Recommended approach:

1. In Proxmox, copy the VM NIC MAC address.
2. In the router DHCP settings, create a reservation for that MAC.
3. Reserve the IP used in `LAN_BIND_IP`.

If `LAN_BIND_IP` changes, laptop sync settings must be updated.

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
dc logs --tail=80 syncthing
dc logs --tail=80 obsidian-backup
```

Expected:

- `openclaw`, `syncthing`, `obsidian-backup` are `Up`
- Syncthing ports bound to `LAN_BIND_IP`

Check Syncthing bind explicitly:

```bash
ss -lntup | grep -E ':8384|:22000|:21027'
```

## One-Time Pairing: Laptop <-> Server

### 1) Install Syncthing on laptop

```bash
sudo apt update
sudo apt install -y syncthing
```

### 2) Start Syncthing on laptop

Preferred (persistent):

```bash
systemctl --user enable --now syncthing
systemctl --user status syncthing --no-pager
```

Open laptop UI: `http://127.0.0.1:8384`

### 3) Open server Syncthing UI

`http://<LAN_BIND_IP>:8384`

### 4) Add server on laptop

- Device ID: server Syncthing device ID
- Address: `tcp://<LAN_BIND_IP>:22000`

Important: do not use `http://...:8384` as device address.

### 5) Accept device on server

Approve the laptop in server UI.

### 6) Share folder

On server UI, share `obsidian-vault` with laptop.

On laptop UI, accept folder and choose local path, for example:

- `/home/<user>/Obsidian/JosemarVault`

Open that folder in Obsidian desktop.

## LAN-Only Syncthing Policy

Server and laptop should both use:

- `Global Discovery`: disabled
- `Relaying`: disabled
- `NAT Traversal`: disabled
- `Local Discovery`: enabled

## Backup Operations

### Manual backup test

```bash
dc exec -T obsidian-backup sh /scripts/obsidian-backup.sh
dc logs --tail=100 obsidian-backup
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
  -v "$PWD/credentials/rclone:/config/rclone:ro" \
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
- Vault files (`obsidian-vault`)
- Backup ring pointer (`obsidian-backup-state`)

Workflow `fresh_start=true` removes only:

- `openclaw-workspace`

It does not remove Obsidian volumes.

## Troubleshooting

### Symptom: `Disconnected (Unused)` on laptop

Check connectivity from laptop:

```bash
nc -vz <LAN_BIND_IP> 22000
curl -sS http://<LAN_BIND_IP>:8384/rest/noauth/health
```

If reachable, verify device address is TCP, not HTTP:

```bash
syncthing cli --gui-address=127.0.0.1:8384 --gui-apikey=<LAPTOP_API_KEY> \
  config devices <SERVER_DEVICE_ID> addresses 0 get
```

Expected value:

- `tcp://<LAN_BIND_IP>:22000`

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
