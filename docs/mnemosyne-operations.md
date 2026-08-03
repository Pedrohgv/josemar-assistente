# Mnemosyne Encrypted Backup Operations (Phase 3)

This runbook documents the Mnemosyne encrypted-backup core: the
Hermes-side exporter, the separate rclone uploader service, the operator
recovery lane (download + verify/install), and the compose overlay.

> **Status:** Opt-in. Backup is **disabled by default**: the overlay is
> layered explicitly and the Hermes-side export cron defaults to `0` (off).

## Overview

The backup system has strictly separated services:

1. **Exporter (Hermes-side)** — `scripts/mnemosyne-backup-export.sh` →
   `scripts/mnemosyne_backup_core.py`. Creates one immutable backup
   generation on a staging volume using the pinned supported DR seam
   `mnemosyne.dr.recovery` signature contract, with a native binary SQLite
   snapshot implementation (sqlite-vec-aware online backup).
2. **Uploader (separate rclone service)** —
   `scripts/mnemosyne-backup-uploader.sh`. Reads the staging volume
   **read-only**, uploads through an rclone `crypt` remote to rotating
   full-snapshot slots, writes only its own state volume.
3. **Recovery lane (operator-only, on demand)** —
   `scripts/mnemosyne-backup-recover.sh` (rclone image, download + verify one
   slot into a disposable handoff) then `scripts/mnemosyne-backup-restore.sh`
   (two separate Hermes image runs, verify/install without any rclone credentials).

The services never share a writable mount of live state. The uploader and the
recover step never mount `hermes-data` or `/opt/data`; Hermes never receives
rclone credentials.

## Architecture

```
+-------------------+   staging (RW)   +-------------------+
|      hermes       | ----------------> | mnemosyne-backup- |
|  (exporter runs   |                   | staging volume    |
|   here, no agent) |                   +-------------------+
+-------------------+                            | read-only
        ^                                        v
        | uploader-state (RO)            +-------------------+
        +------------------------------ | mnemosyne-backup- |
                                         | uploader service  |
                                         +-------------------+
                                                  | rclone crypt
                                                  v
                                         +-------------------+
                                         | remote slots 1..N |
                                         +-------------------+
```

### Why this split

- The uploader cannot corrupt live state because it cannot see it.
- The exporter uses the supported DR seam (online, sqlite-vec-aware backup)
  instead of raw-copying live SQLite/WAL/SHM, which would produce torn or
  vec0-corrupt backups.
- The remote is encrypted at rest via rclone `crypt`.
- **The staging artifact is NOT encrypted.** `mnemosyne.db.gz` is a compressed
  backup of plaintext memory data; only the crypt remote (and the recovery
  handoff AFTER download, which the operator must treat as sensitive) is
  encrypted. The manifest contains no memory contents (metadata only).

## Exporter

### DR seam contract

The exporter imports `mnemosyne.dr.recovery` and validates exactly:

- `create_backup(db_path, backup_dir) -> Dict` — sqlite-vec-aware online
  backup via the sqlite3 backup API (lock-aware, includes WAL frames).
- `restore_backup(backup_path, db_path) -> Dict` — rebuild + integrity.
- `verify_integrity(db_path) -> bool` — `PRAGMA integrity_check`.

The exporter inspects the actual installed signatures at runtime and **fails
clearly on drift** (missing function or changed parameter names). The pinned
package's current `create_backup`/`restore_backup` SQL-dump format is not used:
on real Beam databases it can lose the `fts_working` virtual-table schema
during dump restore. The exporter instead uses a narrow binary
`sqlite3.Connection.backup()` snapshot with sqlite-vec loaded on both
connections, then gzip-compresses the binary file. It never uses a generic
`_safe_copy_db` and never raw-copies live SQLite/WAL/SHM.

### Generation layout

Each generation lives in `<staging>/<generation_id>/` where `generation_id`
is a collision-resistant, lexically sortable UTC timestamp with microsecond
precision plus a short random UUID suffix:

```
<staging>/
  20260802T012247123456Z-a1b2c3d4/
    mnemosyne.db.gz        # gzip-compressed binary SQLite snapshot (plaintext)
    manifest.json          # timestamp, package versions, source contract, SHA-256 (NO memory contents)
    READY                  # sentinel: <generation_id>\n<sha256>\n
  latest                  # atomic pointer: <generation_id>\n
  latest.manifest.json    # atomic copy of the latest manifest
  .export.lock/           # mkdir + flock export lock
```

The format is `YYYYmmddTHHMMSSffffffZ-<hex8>` (exactly 31 chars). The
microsecond timestamp makes ids lexically sortable by creation time; the
random 8-hex suffix from `uuid4` makes two processes (or two sequential cron
invocations in the same wall-clock microsecond) collide-resistant without a
per-process counter that resets. Strict validation (`is_valid_generation_id`)
rejects slashes, `..`, and any malformed input, which guards the uploader
against path traversal via a malicious `latest` pointer.

### Atomicity

1. Build in a temp dir `.<generation_id>.tmp`.
2. Native online backup → verify restore into a disposable temp DB → SHA-256
   every artifact → write manifest → write `READY` sentinel.
3. `os.replace` the temp dir to the final generation dir (atomic publish).
4. Write the atomic `latest` pointer and `latest.manifest.json` **last**.

### Export lock

A `mkdir`-based lock (`.export.lock/`) with an additional `flock` on a lock
file inside provides cross-process exclusion (flock-equivalent for shell +
Python).

### Conservative local pruning

The exporter prunes **only** old generation dirs on its own staging volume
(default keep 5), never the remote, never `READY`/`latest`/artifacts. A
generation is eligible only after its id appears in the uploader's JSONL
acknowledgement ledger, which Hermes observes through the uploader-state
mount read-only. The latest/current generation and at least the configured
retention window are always preserved; an absent or unreadable ledger means
no automatic pruning.

## Uploader

### Boundary contract

- **Never** mounts `hermes-data` or `/opt/data`.
- Staging mount is **read-only**.
- rclone config is **read-only**.
- Only the `mnemosyne-backup-state` volume is writable.
- Reuses the existing secret-managed `obsidian-rclone-config` volume because
  it can hold a separate `mnemosyne-crypt` remote.

### Remote validation

The uploader **requires** `MNEMOSYNE_BACKUP_RCLONE_REMOTE` and validates it is
rclone type `crypt` before upload (not just naming convention):

```sh
remote_type="$(rclone config show "$REMOTE_NAME:" --config "$RCLONE_CONFIG_FILE" \
    | awk -F'=' '/^type[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}')"
[ "$remote_type" = "crypt" ] || exit 2
```

### Upload sequence

1. Poll the staging mount for the `latest` pointer.
2. If the latest generation equals `last-uploaded-generation` in state →
   **no-op**.
3. Verify the manifest SHA-256 against the artifact before upload.
4. Upload one immutable generation to `slot-N` and its slot manifest.
5. **Advance slot** and write `last-uploaded-generation` **only after
   complete success**.
6. Do **not** delete `READY`/`latest`/artifacts from the read-only staging
   mount.

### Slot rotation

Default 5 rotating full-snapshot remote slots (`slot-1` .. `slot-5`). The
next slot wraps to 1 after the max. Each slot holds one immutable generation
plus a `slot-N.json` manifest.

### Idempotency / failure

- Idempotent: re-running after a partial failure does not advance state
  unless the full upload succeeds.
- Does not depend on deleting sentinels; uses the `latest` pointer and
  `last-uploaded-generation` state file.

## Recovery Lane

Restore is split into two short-lived, least-privilege steps plus an explicit
install step. **No automated production overwrite is ever performed.**

Hermes has **no rclone and no rclone config**, and the rclone uploader has no
Python/Mnemosyne/live DB — so recovery is a two-container handoff:

```
   rclone image (short-lived)            hermes image (short-lived)
   mnemosyne-backup-recover.sh           mnemosyne-backup-restore.sh
  +----------------------------+        +-----------------------------+
  | read-only crypt config     |        | NO rclone / NO crypt config |
  | disposable recovery volume | =====> | disposable recovery volume  |
  | download slot-N + verify   | handoff| verify-restore to NEW path  |
  | SHA/manifest, write READY  |        | install-restore (explicit)  |
  +----------------------------+        +-----------------------------+
```

### 1. Download step (rclone image, `recovery` profile)

Downloads one immutable slot through crypt into a disposable recovery handoff
volume and verifies its manifest generation_id and artifact SHA-256 **before**
writing the `RECOVERY_READY` sentinel. It never mounts `hermes-data`/`/opt/data`
and never touches a live DB.

```sh
# From the repo root (or on the operator host with the compose files):
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml \
  -f docker-compose.mnemosyne.yml -f docker-compose.mnemosyne-backup.yml \
  --profile recovery run --rm mnemosyne-backup-recover <slot>
```

On success this writes to the `mnemosyne-backup-recovery` volume:
`mnemosyne.db.gz`, `manifest.json`, and `RECOVERY_READY`
(`<generation_id>\n<sha256>\n`). Any failure leaves **no** `RECOVERY_READY`, so
the Hermes-side step refuses to continue.

### 2. Verify step (hermes image, no rclone credentials)

Consumes the handoff, re-verifies SHA/manifest, and restores to a **NEW
disposable path**. Requires NO rclone and NO rclone config:

```sh
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml \
  -f docker-compose.mnemosyne.yml -f docker-compose.mnemosyne-backup.yml \
  run --rm --no-deps \
  -v mnemosyne-backup-recovery:/recovery \
  hermes /opt/josemar/scripts/mnemosyne-backup-restore.sh \
    verify-restore /recovery /recovery/verified.db
```

This:
1. Requires `RECOVERY_READY` in the handoff dir (produced by step 1).
2. Re-verifies the artifact SHA-256 against the manifest and the sentinel.
3. Restores the gzip-compressed binary snapshot via the backup core to
   `/recovery/verified.db` (a NEW path), preserving FTS/vec schema.
4. Runs `verify_integrity` and writes `VERIFIED_READY` containing the
   generation, artifact SHA, and verified DB SHA.
5. NEVER touches the live DB.

The recovery volume is mounted **transiently** here; the long-running `hermes`
service never mounts it.

### 3. Install step (operator-only, writers stopped)

**Preconditions:** writers stopped, explicit confirmation, rollback copy
retained. The same recovery volume is mounted into this separate short-lived
Hermes container.

```sh
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml \
  -f docker-compose.mnemosyne.yml -f docker-compose.mnemosyne-backup.yml \
  run --rm --no-deps \
  -v mnemosyne-backup-recovery:/recovery \
  hermes /opt/josemar/scripts/mnemosyne-backup-restore.sh install-restore \
    /recovery \
    /opt/data/mnemosyne/data/mnemosyne.db \
    --generation <generation-id> \
    --i-confirm-this-overwrites-production
```

This:
1. Requires `--i-confirm-this-overwrites-production`.
2. Retains a rollback copy of the current live DB (`.rollback` suffix) plus
   any `-wal`/`-shm` files.
3. Re-verifies the source, securely copies it to a temporary file in the live
   DB's parent directory, fsyncs and re-verifies that staged copy.
4. Atomically replaces the live DB with the same-parent staged copy (never a
   cross-filesystem `os.replace`), preserving the verified input.
5. Removes stale `-wal`/`-shm` so SQLite does not replay old frames.

`verify-restore` and `install-restore` are separate commands and separate
short-lived container invocations. Install requires matching `RECOVERY_READY`,
`manifest.json`, `VERIFIED_READY`, and verified DB SHA for the explicitly
selected generation. The long-running Hermes service never mounts recovery.

### Drill (exact steps)

1. **Stop writers** (stop the hermes container or pause the agent).
2. Download + verify the handoff:
   `docker compose -f docker-compose.yml -f docker-compose.embeddings.yml -f docker-compose.mnemosyne.yml -f docker-compose.mnemosyne-backup.yml --profile recovery run --rm mnemosyne-backup-recover <slot>`.
3. In a new short-lived Hermes container, verify restore to the durable handoff
   (`verify-restore /recovery /recovery/verified.db`) and confirm integrity and
   marker recall.
4. Set `GENERATION_ID` to the first line of `RECOVERY_READY`, then install in
   a fresh container with explicit confirmation:
   `docker compose -f docker-compose.yml -f docker-compose.embeddings.yml -f docker-compose.mnemosyne.yml -f docker-compose.mnemosyne-backup.yml run --rm --no-deps -v mnemosyne-backup-recovery:/recovery hermes /opt/josemar/scripts/mnemosyne-backup-restore.sh install-restore /recovery /opt/data/mnemosyne/data/mnemosyne.db --generation "$GENERATION_ID" --i-confirm-this-overwrites-production`.
5. Restart writers.
6. If anything is wrong, **rollback**: copy the `.rollback` file back over
   the live DB (and restore its `-wal`/`-shm` if present), then restart
   writers.

## Compose Overlay

`docker-compose.mnemosyne-backup.yml` is opt-in and layered last:

```sh
COMPOSE_FILE=docker-compose.yml:docker-compose.embeddings.yml:docker-compose.mnemosyne.yml:docker-compose.mnemosyne-backup.yml
```

It adds only:
- the `mnemosyne-backup-uploader` service,
- the `mnemosyne-backup-recover` service (`recovery` profile; on demand only,
  never started by `docker compose up`),
- `mnemosyne-backup-staging`, `mnemosyne-backup-state`, and
  `mnemosyne-backup-recovery` (disposable handoff) named volumes,
- hermes overlay: staging RW + uploader-state RO mounts + backup env.

No host ports, no live state mount in the uploader/recover services, no
deployment workflow changes. The recovery volume is mounted into hermes only
transiently via `docker compose run`, never into the long-running service.

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Default | Description |
|---|---|---|
| `MNEMOSYNE_BACKUP_STAGING_DIR` | `/opt/data/mnemosyne-backup/staging` | Staging volume root (exporter) |
| `MNEMOSYNE_BACKUP_UPLOADER_STATE_DIR` | `/opt/data/mnemosyne-backup/uploader-state` | Uploader state (RO in hermes) |
| `MNEMOSYNE_BACKUP_GENERATIONS_KEEP` | `5` | Local staging generations to retain |
| `MNEMOSYNE_BACKUP_EXPORT_INTERVAL` | `0` (disabled) | Export interval **minutes** (opt-in Hermes cron) |
| `MNEMOSYNE_BACKUP_RCLONE_REMOTE` | `mnemosyne-crypt` | REQUIRED crypt remote name |
| `MNEMOSYNE_BACKUP_RCLONE_PATH` | `Josemar/mnemosyne-backups` | Remote base path |
| `MNEMOSYNE_BACKUP_SLOTS` | `5` | Rotating full-snapshot remote slots |
| `MNEMOSYNE_BACKUP_POLL_INTERVAL` | `300` | Uploader poll seconds |
| `MNEMOSYNE_BACKUP_RUN_ON_START` | `true` | Run uploader once on start |
| `MNEMOSYNE_BACKUP_RECOVERY_DIR` | `/recovery` | Disposable recovery handoff dir (recover step) |

### Secret recovery requirement

The rclone config (in the `obsidian-rclone-config` volume) must contain a
`crypt` remote named per `MNEMOSYNE_BACKUP_RCLONE_REMOTE`. The crypt remote's
password is the recovery secret: **without it, backups cannot be decrypted.**
Store the rclone config via the existing deployment secret mechanism
(`RCLONE_CONFIG_B64`). No secrets are stored in this repo or in `.env.example`.

## Integration Status

- The opt-in Hermes no-agent export cron (interval in minutes, `0` disabled)
  is wired and covered by tests.
- The staging path is in `HERMES_WRITABLE_VOLUMES` (activation/init).
- Pruning is exporter-local, acknowledgement-gated by the uploader ledger.

## Testing

See `tests/runtime/test_mnemosyne_backup.py`. Tests include:

- Fast source/contract tests (no Docker): DR seam signatures, manifest
  schema, SHA-256, atomicity, lock, pruning, restore verify/install
  separation, compose overlay boundary, uploader one-shot/daemon behavior,
  recovery download step behavior.
- Docker-gated synthetic full round trip (requires `RUN_DOCKER_TESTS=1`):
  create real provider/Beam sqlite-vec + FTS data in a disposable home, native online
  backup while source can remain open, restore/integrity, manifest SHA
  validation, separate uploader with NO hermes-data mount, disposable local
  rclone remote wrapped by an actual temporary `crypt` config, prove
  ciphertext does not contain a unique plaintext marker, download/decrypt,
  verify and restore into a new path, verify marker recall/data, slot
  rotation/idempotency/failure does not advance state, staging is read-only
  in uploader, cleanup, AND the recovery lane: recover download step under
  the rclone image → Hermes-side verify-restore (no rclone config) →
  explicit install-restore with rollback.

```sh
# Fast source/contract tests (no Docker, no mnemosyne package required for
# the pure-source subset; the DR-seam subset needs the package on PYTHONPATH):
python3 -m unittest tests.runtime.test_mnemosyne_backup -v

# Docker-gated full round trip:
RUN_DOCKER_TESTS=1 python3 -m unittest tests.runtime.test_mnemosyne_backup -v
```
