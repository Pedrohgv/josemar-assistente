# Credentials Directory

This directory contains credential files for services used by skills.
Files here are mounted into the container at runtime and are NEVER committed to git.

## Structure

```
credentials/
├── README.md                          # This file
├── .gitignore                         # Ignores all credential files
└── <service-name>/                    # One folder per service
    ├── README.md                      # Service-specific setup instructions
    └── <credential-file>              # Actual credential files
```

## Adding Credentials for a New Service

Follow these steps to add credentials for a new service:

1. **Create the service directory:**
   ```bash
   mkdir -p credentials/<service-name>
   ```

2. **Obtain the credential file** from the service provider

3. **Place the credential file** in the service directory:
   ```bash
   cp ~/Downloads/credential-file.json credentials/<service-name>/
   ```

4. **Create a README.md** in the service directory documenting:
   - What the credential is for
   - How to obtain it
   - Container path where it will be available

5. **Restart the container** to pick up new credentials:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.vault-recovery.yml restart hermes
   ```
   `hermes` is the only consumer of the `./credentials` bind mount, so it is
   the only service restarted. The explicit file set mirrors the default
   deployment composition (base + `docker-compose.vault-recovery.yml`
   overlay) and works whether or not `COMPOSE_FILE` is set in the shell
   environment.

## Container Paths

The Hermes init hook (`docker-hermes-init.sh`) copies the mounted credentials
source into the Hermes data volume at `/opt/data/credentials/<service-name>/`,
chmod 700 (go-rwx). The bind mount
`./credentials:/opt/josemar/credentials-source:ro` is read-only.

## Current Services

| Service | Directory | Purpose | Credential Location |
|---------|-----------|---------|-------------------|
| gogcli | `credentials/gogcli/` | Google Workspace CLI (Gmail, Calendar, Sheets, Drive) | `/opt/data/credentials/gogcli/` |
| rclone | `credentials/rclone/` (local helper path) | Encrypted vault-recovery backup lane (default deployment composition) and the optional Mnemosyne backup lane | `/config/rclone/rclone.conf` from Docker volume `obsidian-rclone-config` |

### rclone Credential Setup (Encrypted Backup Lanes)

The rclone config is the recovery secret: the `vault-recovery-crypt` remote
(type `crypt`, non-empty underlying + password) is required for EVERY
deployment — the deploy workflow FAILS when it is missing rather than
silently losing backups. The `mnemosyne-crypt` remote is validated the same
way in `MNEMOSYNE_DEPLOY_MODE=backup`.

1. Create directory:
   ```bash
   mkdir -p credentials/rclone
   ```
2. Generate rclone config locally (helper file):
   ```bash
   # Native binary (if installed)
   rclone config

   # Docker-only alternative (no host install required)
   docker run --rm -it -v "$PWD/credentials/rclone:/config/rclone" -e RCLONE_CONFIG=/config/rclone/rclone.conf rclone/rclone:latest config
   ```
3. Copy config file to project path (if generated outside project):
   ```bash
   cp ~/.config/rclone/rclone.conf credentials/rclone/rclone.conf
   ```

4. For CI deployments, set GitHub secret `RCLONE_CONFIG_B64` with base64 of
   `rclone.conf`. The deploy workflow base64-decodes it, validates the crypt
   remotes, and publishes it atomically into the shared
   `obsidian-rclone-config` Docker volume.

   Local one-off load into the volume (helper only; the deploy workflow does
   this automatically):
   ```bash
   docker volume create josemar-assistente_obsidian-rclone-config
   docker run --rm \
     -v "$PWD/credentials/rclone:/src:ro" \
     -v "josemar-assistente_obsidian-rclone-config:/config/rclone" \
     alpine:3.20 \
     sh -c 'cp /src/rclone.conf /config/rclone/rclone.conf && chmod 600 /config/rclone/rclone.conf'
   ```

5. Restart the long-running services that consume the config volume (the
   profile-gated recover steps read the config on every short-lived
   `docker compose run`, so they need no restart):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.vault-recovery.yml restart vault-recovery-uploader
   ```
   `hermes` does NOT mount `obsidian-rclone-config` and must not be
   restarted here. When the optional Mnemosyne backup lane is deployed
   (`MNEMOSYNE_DEPLOY_MODE=backup`), restart its uploader too, with the full
   deployment file set:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.vault-recovery.yml -f docker-compose.embeddings.yml -f docker-compose.mnemosyne.yml -f docker-compose.mnemosyne-backup.yml restart vault-recovery-uploader mnemosyne-backup-uploader
   ```

The volume is mounted **read-only into every consumer** (the
`vault-recovery-uploader`, the profile-gated `vault-recovery-recover`, and
the Mnemosyne uploader/recover services): the config contains remote
credentials, not refresh tokens, so consumers never write to it. Optional
verification:

```bash
docker run --rm -v "$PWD/credentials/rclone:/config/rclone" -e RCLONE_CONFIG=/config/rclone/rclone.conf rclone/rclone:latest lsd vault-recovery-crypt:
```

## Security Rules

- **NEVER** commit credential files to git (this directory is in `.gitignore`)
- **NEVER** store API keys, tokens, or passwords in plaintext outside this directory
- Use GitHub Secrets for deployment-time secrets
- Credential mounts are read-only; the `obsidian-rclone-config` volume is
  read-only for all consuming services (no token-refresh writes)
