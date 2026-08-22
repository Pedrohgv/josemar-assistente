# Vault Recovery Operations (Phase 1 + Phase 2 + Phase 3)

This runbook documents the vault-recovery disaster-recovery lane. **Phase 1**
is the Hermes-side export of full, immutable, local staged generations of the
Obsidian vault **plus** the complete `/opt/data/.gbrain` state tree, with a
Docker-gated physical-copy portability proof. **Phase 2** is the encrypted
remote upload/recovery/install lane (rclone crypt overlay): uploader, recover
download, disposable-doctor verify, and a journaled two-tree install with
operator rollback. **Phase 3** makes the encrypted lane the **default
deployment composition**, retires the plaintext `obsidian-backup` service,
and adds the deployment migration sequence plus the full Docker-gated
disaster-recovery drill.

> **Status:** Phase 1 — **local staging, default-enabled** (base compose).
> Phase 2 — encrypted lane, **default deployment composition**: the deploy
> workflow always applies `docker-compose.vault-recovery.yml` and FAILS the
> deployment when the `vault-recovery-crypt` remote is not configured (never
> silently lose backups). Phase 3 — the legacy plaintext `obsidian-backup`
> service is **retired** (removed from the default deployment; existing
> plaintext GDrive slots are NOT deleted automatically — see
> "Migration sequence" and `docs/obsidian-operations.md` → "Retired
> plaintext lane").

## Goal and design (from the DR plan)

Each committed generation must restore the Obsidian vault and the complete
`/opt/data/.gbrain` state **without reindexing or rebuilding**. A full
`.gbrain` physical copy is release-blocked on a pinned-image portability
proof: the restored tree must open with the real doctor, with
DB-only records, embedding/vector-related DB state, config, schema packs,
and markers surviving. Phase 1 delivers the exporter, the generation layout,
the convergence semantics, and that proof harness.

## Phase-2 encrypted upload / recovery / install lane (default composition)

The overlay (`docker-compose.vault-recovery.yml`, layered AFTER the base:
`COMPOSE_FILE=docker-compose.yml:docker-compose.vault-recovery.yml`) adds a
separate pinned rclone uploader daemon, a profile-gated recover step, and
two disposable volumes — mirroring the Mnemosyne encrypted lane boundaries.
The deploy workflow applies the overlay on EVERY deployment (Phase 3): the
uploader is part of the default stack, the export cron is default-enabled
(04:00 local), and retention defaults to the newest 14 committed remote
generations.

```
+-----------------+  crypt remote  +------------------------+   RECOVERY_READY   +----------------------+
| vault-recovery- | -------------> |  uncommitted/<gen> ->  |   (validated,      | short-lived hermes   |
| uploader        |  (upload +     |  verified -> committed |    decrypted)      | run: verify (doctor  |
| (staging RO,    |   remote       |  (only committed gens  |   <----------------| on a DISPOSABLE     |
| seed RO -> own  |   verify,      |   are recoverable)     |  vault-recovery-   | copy) -> VERIFIED_   |
|  active copy RW)|   retention)   |                        |  recover (rclone,  | READY -> install     |
+-----------------+                +------------------------+  profile-gated)    | (journaled two-tree)|
                                                                                  +----------------------+
```

- **Uploader** (`vault-recovery-uploader`): NEVER mounts `hermes-data` or
  `/opt/data`; the staging mount is READ-ONLY and the shared rclone config
  seed (`obsidian-rclone-config`) is READ-ONLY — the uploader copies it into
  a PRIVATE ACTIVE copy inside its own state volume at start and runs rclone
  against that copy, because Google OAuth access-token refresh must be able
  to write the config (see "rclone OAuth-refresh configuration design"
  below); only its own state volume is writable — with one strictly
  bounded exception: the SAME staging volume is additionally mounted
  read-write at `/staging-prune`, used EXCLUSIVELY for ack-based local
  retention (see below). Requires an rclone
  remote of type `crypt` with a NON-EMPTY underlying remote and password
  AND the **metadata-encryption standard** — `filename_encryption` must be
  `standard` (never `off`/`obfuscate`) and `directory_name_encryption`
  must be enabled (never `false`), so plaintext file/directory names can
  never leak in the ciphertext metadata (validated from the real config
  before any transfer, same as the deploy preflight). Uploads to an
  UNCOMMITTED namespace, downloads the remote DECRYPTED content and
  re-validates it (strict JSON manifest well-formedness, entries-index
  digests, full tree/hashes) BEFORE
  the commit move, commits the payload into the COMMITTED namespace, and
  **verifies the COMMITTED payload — only then publishes the `READY`
  sentinel** (moved alone in a final READY-last step). The commit is
  immutable once visible: **a committed generation that already carries a
  valid `READY` marker bound to the manifest (content == generation id ==
  manifest `generation_id`) is never mutated** — a retry (e.g. after a crash
  between READY publication and the local ack) re-downloads the committed
  payload, fully re-validates it, and then acknowledges it or fails; there
  is no upload, no commit move, and no overwrite. The remote READY/manifest
  read distinguishes a **CONFIRMED missing/invalid marker** (rclone
  "file/directory not found", exit 3/4, or content/binding mismatch) from
  an **INDETERMINATE read failure** (rclone transport/auth/backend error:
  any other non-zero exit). An indeterminate marker state is NEVER treated
  as markerless: the upload **aborts BEFORE any remote mutation** (a
  possibly-published payload is never re-uploaded over), and retention
  **skips the entire prune** with a visible error. The local ack ledger is
  written only after verification + commit both succeeded — ledger entry
  first, `last-uploaded` pointer LAST (READY-last: a crash between the two
  writes leaves the pointer stale and the next run re-validates the
  committed payload idempotently, never an unacknowledged claim). **Every
  ledger entry is BOUND to the remote identity it was recorded against**
  (`generation-id TAB remote-name TAB remote-path`): an acknowledgement
  for a DIFFERENT remote (rotation, re-pointed path) or a legacy bare
  generation id is NOT honored by the backlog skip, the dangling-`latest`
  check, or either retention pass — remote rotation or loss can never let
  local retention delete generations the CURRENT remote has not committed.
  A rotated-away ack causes a re-upload (or a READY-protocol re-validation
  of an already-committed payload — never a mutation) and a
  re-acknowledgement under the current identity BEFORE any retention
  applies. Remote
  retention  keeps the newest `VAULT_RECOVERY_RETENTION` (default 14) committed
  generations **that carry a valid `READY` marker bound to the manifest**,
  pruned only after a clean remote inventory listing AND an ack-ledger
  match; a FAILED inventory listing is logged and pruning is skipped — it
  is never treated as an empty namespace. **Incomplete committed dirs
  (interrupted commits: no READY, invalid marker, or unbound manifest) are
  preserved — they are never counted toward retention, never evict valid
  generations, and never pruned.** **LOCAL staged retention (ack-based)**:
  after a remote-acknowledged committed upload, the local staging
  generations beyond the newest `VAULT_RECOVERY_LOCAL_RETENTION` (default
  14) FULL generations are pruned — ONLY generations acknowledged in the
  local ledger are ever removed locally, and only through the dedicated
  writable `/staging-prune` mount (the read-only staging mount is never
  written). Every candidate is FULLY validated before any deletion —
  strict generation id, READY sentinel binding (content == generation id),
  manifest `generation_id`/`schema_version`, entries-index digests, and
  the exact tree/hashes of BOTH trees. Invalid local states (invalid
  directory names, missing/unbound READY, manifest mismatch, tampered or
  unreadable entries/tree) SKIP the ENTIRE local prune with a visible
  error — any doubt -> no prune, and valid old state is never removed
  while any staged state is suspect; `latest` and non-generation artifacts
  are never touched. The
  staging tree validation is mode-exact; the remote verification is
  content-exact with modes relaxed, because rclone crypt cannot round-trip
  POSIX modes — the install re-applies the exact recorded modes from the
  entries index.
- **Backlog reconciliation (every run, daemon AND one-shot).** The
  uploader processes the **FULL staged backlog**, not just the `latest`
  pointer: every staged generation that is **NOT acknowledged in the
  local ledger** is uploaded and acknowledged, **oldest first**
  (generation ids are lexically sortable UTC timestamps, so plain sort ==
  chronological order). The run succeeds (exit 0) ONLY when the whole
  backlog was committed + acknowledged — an uploader that was down for
  several days (or whose earlier uploads failed) catches up
  **incrementally across runs**: acknowledged generations are skipped, so
  the next run resumes at the same oldest unacknowledged generation. The
  staging root is enumerated BEFORE any upload and any directory whose
  name is not a strict generation id (e.g. a crashed export's leftover,
  or a tampered entry) **aborts the entire run** with a visible error —
  a staging root that cannot be fully accounted for is never partially
  processed, and a dangling `latest` pointer (its generation neither
  staged nor acknowledged) fails closed instead of being silently
  dropped. `latest` and non-generation artifacts are never uploaded. The
  per-generation retry-after-READY protocol is unchanged: a backlog
  generation that is already READY-visible in the committed namespace is
  re-validated and acknowledged — never mutated, never re-uploaded.
- **Upload mutual exclusion (kernel-managed lock, audited change).** A
  single uploader-scoped lock around `run_once`/upload prevents manual AND
  daemon invocations from uploading/pruning concurrently. The lock is
  **released by the kernel when the holding process dies**: a `docker kill`
  (SIGKILL), crash, container restart, or redeploy can never leave a stale
  lock behind, and a REAL concurrent uploader (e.g. a manual one-shot
  racing the daemon poll) is genuinely rejected — visible error, non-zero
  exit in one-shot mode — never queued, never racing. The one-time safe
  deployment migration of a legacy empty `.upload.lock` and the
  no-blind-deletion rule are documented in the "Uploader lock:
  kernel-managed release and legacy `.upload.lock` migration" subsection
  below.
- **Recover** (`vault-recovery-recover`, `--profile recovery`, short-lived
  `docker compose run` only; runs rclone against an EPHEMERAL writable copy
  of the config seed — a token refresh during a long download must be able
  to persist, and the copy dies with the container): `list-remote` lists
  only committed
  generations whose **remote `READY` marker is queried and validated as
  bound to the manifest** (content == generation id == manifest
  `generation_id`); markerless or invalid committed dirs (interrupted
  commits) are **invisible** — never listed, never an error. An
  **indeterminate remote READY/manifest read failure** (rclone
  transport/auth/backend error, not a confirmed "not found") **fails the
  listing closed** (exit 2): a possibly-valid generation is never hidden as
  if it were markerless. `download`
  validates the same remote `READY` marker bound to the manifest **before
  any payload transfer** (a markerless/invalid committed dir is refused up
  front; an indeterminate remote read failure is refused the same way but
  reported as an explicit error, not as markerless), downloads the
  committed generation into the disposable
  `vault-recovery-recovery` volume, fully validates it, and writes the
  `RECOVERY_READY` handoff (generation id + manifest sha256).
  Running as root (rclone image default), it chowns the handoff volume root
  to `HERMES_UID` so the short-lived hermes verify/install runs can write
  `VERIFIED_READY`.
- **Verify / install / rollback** (Hermes-side core
  `vault_recovery_restore_core.py` + wrapper `vault-recovery-restore.sh`):
  the LONG-RUNNING hermes service never mounts the recovery volume; only
  short-lived `docker compose run` invocations mount it transiently. Because
  the hermes service runs as root (s6 init) and the restore core enforces
  the issue #110 runtime identity, operators must run these steps as the
  hermes uid:
  `docker compose run --rm --no-deps --user "${HERMES_UID:-10000}:${HERMES_GID:-10000}" -v vault-recovery-recovery:/recovery --entrypoint /opt/josemar/scripts/vault-recovery-restore.sh hermes <command>`.
  `verify-recovery` runs the PINNED doctor against a DISPOSABLE copy of the
  restored `.gbrain` (never the live state; the disposable copy is removed
  when the step finishes) and writes `VERIFIED_READY` carrying the manifest
  sha256 — any stale `VERIFIED_READY` is removed and fsynced up front, so
  the sentinel only ever reflects the most recent completed verification.
  The disposable copy's `config.json` (which records the LIVE absolute
  `database_path`/`sync.repo_path`) is sanitized AND every absolute path in
  the whole config is contained fail-closed with **normalized/resolved
  containment**: live-gbrain-prefixed paths are rewritten into the
  disposable copy (layout preserved), live-vault paths into the bundle
  vault, and ANY unconfinable absolute path REFUSES the verification — the
  doctor never runs on a config that could resolve to the live tree,
  regardless of which key carries the path. Every candidate is lexically
  normalized (`os.path.normpath`, collapsing `.`/`..`) and the containment
  roots are `realpath`-resolved BEFORE any comparison, so a `..` path
  (e.g. `/opt/data/.gbrain/../../x`, or a rewritten
  `<disposable>/../../escape`) can never pass a raw string-prefix check
  and escape the disposable copy. **RELATIVE paths are contained too**:
  the doctor always runs with `cwd` and `HOME` pinned INSIDE the disposable
  root (so any relative resolution lands in the disposable layout), a
  relative `database_path` is resolved against the disposable `.gbrain`
  copy and a relative `sync.repo_path` against the bundle vault, and a
  relative value whose normalized form still escapes (leading `..`, e.g.
  `../../..`) REFUSES the verification — a relative path can never resolve
  into production. The verifier
  holds the same exclusive nonblocking shared TaskNotes/gbrain lock as the
  install (fix 5): a concurrent install or gbrain user refuses the
  verification instead of racing the handoff.
  `install-recovery` requires
  `--i-confirm-this-overwrites-production`, acquires the shared
  TaskNotes/gbrain lock EXCLUSIVELY and nonblocking **BEFORE any handoff
  read** (any active gbrain user refuses the install), and only then reads
  the sentinels, re-validates the bundle (manifest
  `schema_version` bound to 1, entries digests, exact tree re-scan, plus the
  `RECOVERY_READY`/`VERIFIED_READY` manifest-sha bindings — a bundle swapped
  after download or after verification is refused), stages both trees, and
  runs the journaled
  two-tree transaction: the `.gbrain` gets a whole-tree atomic rename swap
  (sibling backup on hermes-data); the vault uses the journaled per-top-level
  entry swap (its backup root must live INSIDE the vault tree — same
  filesystem — so rename(2) of the whole tree fails with EBUSY on the
  production mount root or EINVAL otherwise; both fall back to per-entry).
  The optional `--generation <id>` is bound **in the core, AFTER the lock**:
  the requested id is validated and compared against the `RECOVERY_READY`
  handoff generation before any further handoff read/validation, so a
  lock-less rclone recover step that replaced the handoff with a DIFFERENT
  generation (e.g. a concurrent `download` of another gen) can never be
  installed — the wrapper's own pre-check is only a fast-fail convenience,
  the core check is authoritative.
  Immediately BEFORE the first mutation the handoff is re-checked
  (RECOVERY_READY + VERIFIED_READY + bundle manifest sha256): a lock-less
  rclone recover step replacing the handoff mid-install aborts with the
  live trees untouched (install TOCTOU closure).
  Every rename is journaled WRITE-AHEAD under
  `/opt/data/vault-recovery/install-journal/<gen>/journal.json`: each step
  is fsynced as `pending` BEFORE the rename and flipped to `done` after, so
  a crash mid-transaction leaves a recoverable journal; any failure
  automatically rolls the transaction back; `rollback <gen>` reverses a
  completed or crashed install (crash recovery / operator-driven) under the
  same exclusive nonblocking lock, acquired BEFORE the journal is read (a
  busy gbrain refuses the rollback).
- **Post-install cleanup:** a completed install leaves
  `<live-vault>/.vault-recovery-install/<gen>/` (staged tree + backup of the
  previous live vault) and the journal. The next export would include these
  hidden dirs, so after the rollback window the operator should remove
  `<live-vault>/.vault-recovery-install/` and
  `/opt/data/.vault-recovery-install/` (the journal itself stays until the
  rollback window expires).

### rclone OAuth-refresh configuration design

The encrypted lanes (vault-recovery by default, Mnemosyne in `backup` mode)
share one rclone configuration volume, `obsidian-rclone-config`. It is a
**read-only deploy-published seed**, never a runtime working file:

- **The deploy is the only writer of the seed — and its publish step may
  carry a probe-refreshed config.** Every deployment decodes
  `RCLONE_CONFIG_B64` into a DISPOSABLE WORKING DIRECTORY on the runner,
  validates the remotes with real rclone probes, and then atomically
  publishes the config into `obsidian-rclone-config` (temp file + rename;
  never a partial write). Every consumer mounts the volume READ-ONLY. No
  runtime service ever writes to the seed; the ONLY writer is the deploy's
  atomic publish step, which may legitimately publish the config bytes the
  probes produced (see the probe bullet below).
- **Google OAuth access tokens refresh — rclone must write config.** Google
  Drive remotes use OAuth: access tokens expire (~1h) and rclone refreshes
  them transparently by REWRITING the config file to persist the new token.
  A consumer pointing `RCLONE_CONFIG` directly at the read-only seed fails
  every refresh once the access token expires — uploads and recovery then
  die with auth errors. Real transfers can therefore never run against the
  seed itself.
- **Long-running uploaders use a private persistent ACTIVE copy.** On
  start, the uploader copies the seed into its OWN state volume (its only
  writable mount), chmod 600, and points `RCLONE_CONFIG` at that active
  copy. Refreshed tokens persist there and survive container restarts; the
  seed stays untouched.
- **Deploy probes: disposable WRITABLE working directory — distinct from
  the promoted seed.** The deploy's probe containers mount the runner's
  disposable temp DIRECTORY writable (never the seed volume) and run rclone
  against the config copy inside it. rclone may refresh an OAuth token
  during the probe by REWRITING that working copy (sibling temp file +
  atomic rename), which is exactly why the directory — not a single
  read-only file — is mounted writable. The refreshed working config is
  then ATOMICALLY PROMOTED: the publish step compares the working config's
  checksum against the pre-probe checksum recorded before the probes,
  detects the probe-time refresh, and publishes the UPDATED config into the
  persistent read-only seed volume. The disposable working directory and
  the promoted seed config are therefore two DIFFERENT artifacts: the
  directory is the ephemeral probe workspace (removed on every exit, never
  persisted as a workspace), while the seed volume is the persistent
  deploy-published artifact every consumer mounts read-only — which may
  legitimately carry the refreshed token the probe produced.
- **Recovery steps and operator verification use EPHEMERAL writable
  copies.** Short-lived consumers (recover download / `list-remote`,
  operator verification commands) copy the seed to a container-temp path
  and point `RCLONE_CONFIG` there: writable so a refresh mid-transfer does
  not fail, ephemeral so the copy — and any refreshed token in it — is
  discarded when the container exits, never persisted. Unlike the deploy
  probe, nothing promotes these copies into the seed.
- **Never make the shared seed writable.** The seed is the rotation point
  and the deploy-published artifact. Making it writable breaks the reseed
  contract (a service could leave a half-written or token-dirty config in a
  volume shared by every lane), spreads live tokens across services, and
  makes rotation ineffective. Any write a consumer needs belongs in its
  private active/ephemeral copy.
- **No-secret guarantees hold through the probe/publish flow.** Neither the
  probe step nor the publish step prints config content or hashes: the
  probes validate remote type/fields only, the pre-probe vs post-probe
  checksum is compared (never logged), and the working config is chmod 600
  inside a disposable directory that the steps trap-remove on EVERY exit.
  The disposable working directory, the seed volume, and the active
  config volumes are all secret-bearing: never inspect, log, or archive
  any of them.
- **The vault-recovery active state volume is secret-bearing.** The active
  copy carries the config plus live refresh tokens, so
  `vault-recovery-uploader-state` is secret-bearing: never inspect, log, or
  archive it; treat it like credentials. It is a Docker named volume,
  outside every repo and backup. (Mnemosyne `backup` mode keeps its ACTIVE
  config in the uploader-only `mnemosyne-backup-rclone-config` volume; its
  `mnemosyne-backup-state` volume holds only the non-secret ack ledger and
  is mounted read-only into Hermes.)
- **GitHub secret rotation causes a reseed.** Changing `RCLONE_CONFIG_B64`
  and redeploying republishes the seed into `obsidian-rclone-config`.
  Consumers pick up the new seed on their next start: the deploy recreates
  the uploader, which re-copies the seed to its active copy; ephemeral
  copies are always created fresh from the seed. No manual volume surgery
  is needed — rotation takes effect with the deploy.
- **Troubleshooting read-only config errors.** Symptom: rclone errors such
  as `failed to save config file` / `read-only file system` on refresh,
  `token refresh failed`, or Google `invalid_grant` after a period of
  successful transfers. Cause: the consumer runs rclone against the
  read-only seed directly, so the refreshed token cannot be persisted.
  Fix: run against a writable copy — the uploader's private active copy or
  an ephemeral copy for short-lived steps — verify `RCLONE_CONFIG` points
  at the copy (not `/config/rclone/rclone.conf`, the seed path), and after
  a   rotation restart the uploader so it re-copies the reseeded seed. NEVER
  "fix" it by making the seed writable.
- **Upgrade migration: Mnemosyne `backup` mode only.** The first iteration
  of the fix stored the Mnemosyne ACTIVE config inside
  `mnemosyne-backup-state`; the corrected design moved it to the
  uploader-only `mnemosyne-backup-rclone-config` volume. Deployments
  upgrading from that iteration remove exactly two legacy artifacts
  (`rclone.active.conf` and `rclone.active.conf.seed-fp`) from the Mnemosyne
  state volume before Hermes starts — see `docs/mnemosyne-operations.md` →
  "Upgrade migration: pre-fix active-config cleanup (Oracle fix)". The
  vault-recovery lane needs NO migration: its active config always lived in
  its own uploader-only state volume (`vault-recovery-uploader-state`),
  which Hermes never mounts.

### Phase-2 environment

| Variable | Default | Meaning |
|---|---|---|
| `VAULT_RECOVERY_RCLONE_REMOTE` | `vault-recovery-crypt` *(required)* | rclone crypt remote name (validated: type `crypt`, non-empty underlying + password, `filename_encryption` `standard`, `directory_name_encryption` enabled; the deploy workflow hardcodes and validates this exact name). **IMMUTABLE at runtime**: the overlay wires it as a literal (never `${...}`-interpolated) so the runner environment / `.env` cannot override the validated remote |
| `VAULT_RECOVERY_RCLONE_PATH` | `Josemar/vault-recovery` | remote base namespace (immutable literal in the overlay, same rationale as the remote name) |
| `VAULT_RECOVERY_RETENTION` | `14` | committed generations to retain remotely |
| `VAULT_RECOVERY_LOCAL_RETENTION` | `14` | local staged generations to retain after ack (>= 1); only ack-acknowledged generations beyond this window are pruned locally, through the dedicated writable `/staging-prune` mount |
| `VAULT_RECOVERY_POLL_INTERVAL` | `300` | uploader poll interval (s) |
| `VAULT_RECOVERY_RUN_ON_START` | `true` | uploader runs once on start |
| `VAULT_RECOVERY_ONCE` | `false` | uploader one-shot mode (manual/cron invocations, tests): one full backlog reconciliation pass — every staged unacknowledged generation is uploaded and acknowledged, oldest first — then exit |
| `HERMES_UID` / `HERMES_GID` | `10000` | chown target for the recovery handoff volume |

### Phase-2 operations

- **Upload now:** `docker compose run --rm --no-deps -e VAULT_RECOVERY_ONCE=true vault-recovery-uploader` (the daemon polls otherwise). Every run — manual, cron-style, or daemon poll — reconciles the **full staged backlog**: all staged generations not yet acknowledged in the local ledger are uploaded and committed, oldest first, and the run exits 0 only when the whole backlog was acknowledged (a backlog accumulated while the uploader was down is completed incrementally across runs).
- **List remote generations:** `docker compose --profile recovery run --rm --no-deps vault-recovery-recover list-remote`. Every listed name must be a valid generation id carrying a remote `READY` marker bound to the manifest; a FAILED remote inventory listing fails closed (exit 2) — it is never reported as "no committed generations" (no false negative on backup existence). Markerless/invalid committed dirs (interrupted commits) are invisible; an INDETERMINATE remote READY/manifest read failure (rclone transport/auth/backend error, not a confirmed "not found") also fails the listing closed — a possibly-valid generation is never hidden as markerless.
- **Recover one generation:** `docker compose --profile recovery run --rm --no-deps vault-recovery-recover download <gen-id>` (the remote `READY` marker bound to the manifest is validated BEFORE any payload transfer; an indeterminate remote read failure is refused explicitly, not reported as markerless; writes `RECOVERY_READY` into the recovery volume; partial/failed downloads never produce a sentinel).
- **Verify:** short-lived hermes run (see the `--user` invocation above) with `verify-recovery /recovery` (disposable doctor; writes `VERIFIED_READY`).
- **Install:** same invocation with `install-recovery /recovery --live-vault /opt/data/obsidian --live-gbrain /opt/data/.gbrain [--generation "$GENERATION_ID"] --i-confirm-this-overwrites-production`. Stop Hermes, server Syncthing and all gbrain jobs first. When `--generation` is given, the requested id is bound in the core under the lock: a handoff carrying a different generation refuses the install (a concurrent recover download of another gen can never be installed).
- **Rollback:** `rollback <gen-id>` (reverse the journaled transaction).

### Uploader lock: kernel-managed release and legacy `.upload.lock` migration

Every upload/prune pass (daemon poll, `RUN_ON_START`, and one-shot
manual/cron invocations) is serialized by a single uploader-scoped lock in
the uploader's own state volume (`vault-recovery-uploader-state`). Audited
change: the lock is kernel-managed and self-releasing, replacing the legacy
mkdir-based lock whose stale `.upload.lock` directory survived crashes and
kills.

- **Kernel-managed release across `docker kill`/redeploy.** The lock is
  released automatically by the kernel when the holding process exits, no
  matter how it dies — `docker kill` (SIGKILL), a crash, a container
  restart, or a redeploy. There is no stale lock to remove and no lock
  surgery after a kill/redeploy: the next start simply acquires the lock and
  proceeds.
- **Real concurrent uploader rejection.** While one invocation holds the
  lock, ANY other invocation — a manual one-shot racing the daemon poll, or
  two manual one-shots — is genuinely rejected: the lock is never silently
  ignored and never shared. The rejected invocation logs a visible error;
  in one-shot mode it exits non-zero (the caller observes the lock was not
  taken), in daemon mode it logs and retries at the next poll interval.
- **One-time safe deployment migration of a legacy empty `.upload.lock`.**
  Deployments that ran the previous mkdir-based lock may carry a legacy
  `.upload.lock` DIRECTORY inside the uploader state volume (a residue of a
  container killed while holding the old lock). The deploy workflow
  performs the migration EXACTLY ONCE, in the safe window AFTER all prior
  services are stopped and BEFORE the new services start (no live uploader
  can hold or recreate the lock): it resolves
  `vault-recovery-uploader-state` from the Compose project metadata and
  volume labels (never guessed), verifies NO container — running or
  stopped — still mounts the volume, and removes ONLY an EMPTY legacy
  `.upload.lock` DIRECTORY via `rmdir` inside a disposable container
  mounting the named volume (no broad `rm`/`rm -rf`, never the ledger, the
  active config, or any other state). The migration **fails closed** on
  every abnormal outcome — a lock that is NON-EMPTY, a lock that is not a
  directory, a lock still present after `rmdir`, a volume that is
  ambiguous or unresolvable, or ANY container still mounting the volume
  (IN USE) — and the deploy aborts with a visible error instead of
  deleting anything: any doubt -> no deletion. A non-empty legacy lock
  means something was left inside it and must be inspected by the
  operator, never deleted blind. Clean skip only when the volume or the
  lock is absent.
- **Never delete the lock blindly.** There is no legitimate operator
  procedure that removes the lock by hand. The lock is kernel-managed and
  self-releasing — `docker kill`/redeploy release it on their own, and the
  migration handles the only legacy artifact (an empty, unused
  `.upload.lock`). Blind deletion (`rmdir`/`rm -rf` of `.upload.lock`) can
  race a REAL concurrent uploader that legitimately holds the lock and is
  never a fix. If the uploader reports the lock as in use, wait for the
  holder to finish (or inspect the running uploader containers) — do not
  delete the lock.

## Phase-3 deployment integration (default lane)

- **Composition.** The deploy workflow ALWAYS derives
  `COMPOSE_FILE=docker-compose.yml:docker-compose.vault-recovery.yml[:optional
  overlays...]` — the encrypted lane is the default backup composition.
  Local development may still run the base compose alone (no uploader).
- **Preflight (before any volume mutation or teardown).**
  1. `RCLONE_CONFIG_B64` is **required for every deployment**; a missing
     secret fails the deploy — the encrypted lane must never silently
     disappear.
  2. The `vault-recovery-crypt` remote is validated with the FOUR
     independent field checks (type `crypt`, non-empty `remote`, non-empty
     `password`, metadata-encryption standard: `filename_encryption`
     `standard` + `directory_name_encryption` enabled) **independently of
     `MNEMOSYNE_DEPLOY_MODE`** — toggling the
     Mnemosyne backup mode cannot skip the default lane's preflight. In
     `backup` mode `mnemosyne-crypt` is validated the same way.
  3. `docker compose config --quiet` on the selected file set fails closed.
  4. **Real remote readiness gate** (migration cutover proof): the deploy
     writes a probe object THROUGH the real production crypt remote
     (`rcat`), reads it back (`cat`, content-verified), and deletes it —
     before ANY teardown. A syntactically valid but **UNREACHABLE** remote
     (or a broken crypt round trip) **aborts the deploy before any
     teardown**: the existing deployment and any legacy lane state are
     retained untouched, and the plaintext lane is never retired on top of
     an unproven remote. The probe lives under the remote base path, never
     inside the `committed` namespace, so it can never be mistaken for a
     generation by listing/retention/recovery.
- **Teardown/start.** The teardown superset includes the vault-recovery
  overlay (and the `recovery` profile) so stale uploader/recover containers
  are removed; between the stop and the start, the deploy runs the one-time
  stale-lock migration (legacy empty `.upload.lock` removal — see the
  "Uploader lock: kernel-managed release and legacy `.upload.lock`
  migration" subsection above); `docker compose up -d` starts the uploader.
- **Temp config cleanup.** The decoded `RCLONE_CONFIG_B64` temp file is
  trapped end to end: the decode step removes it on every failure (and
  disarms the trap on success), the readiness gate — the only step between
  them, and one that can itself FAIL (an unreachable remote aborts the
  deploy there) — traps its removal on EVERY exit and disarms it only on
  success, and the publish step (the final owner) traps its removal on
  EVERY exit. The final `Cleanup sensitive files` step (`if: always()`)
  additionally removes `$RCLONE_TEMP_CONF` alongside `.env`: a run
  cancelled BETWEEN the rclone steps (after the decode step exported the
  path, before a later step's trap armed) would otherwise leave the
  decoded secret on the runner — the step-local traps only cover in-step
  failures. No leak path exists: a failure in any of the three steps, or a
  between-step cancellation, removes the decoded secret.
- **Runtime remote immutability.** The overlay wires
  `VAULT_RECOVERY_RCLONE_REMOTE=vault-recovery-crypt` and
  `VAULT_RECOVERY_RCLONE_PATH=Josemar/vault-recovery` as LITERALS in both
  the uploader and the recover service — they are deliberately NOT
  `${...}`-interpolated. Compose interpolation prefers the runner
  environment over the `.env` file, so an interpolated value could
  silently route backups to a different remote than the one the deploy
  preflight validated and the readiness gate probed. The `.env.example`
  values are documentation only.
- **Post-start checks** (the Hermes init logs cron failures nonfatally, so
  health alone is not enough): the `vault-recovery-uploader` service is
  running; exactly one `vault-recovery-export` cron job exists with the real
  `jobs.json` schema (`schedule.kind == "cron"`, `expr == "0 4 * * *"`,
  script `hermes-vault-recovery-export-cron.sh`, `no_agent == true`,
  `workdir == "/opt/data"` exactly); and no
  `*-obsidian-backup` container lingers (plaintext absence).
- **Release gates.** The deploy runs the Phase-1 portability proof
  (`tests/runtime/test_vault_recovery_portability.py`,
  `VAULT_RECOVERY_PORTABILITY_REQUIRED=1`) AND the Phase-3 full
  disaster-recovery drill (`tests/runtime/test_vault_recovery_dr_drill.py`,
  `VAULT_RECOVERY_DR_DRILL_REQUIRED=1`) as MANDATORY pre-mutation release
  gates: a missing docker CLI or any failed assertion FAILS the deploy —
  there is no opt-in bypass and no "recommended" fallback on the release
  path.

## Migration sequence (operator-run, one time)

The plaintext lane is retired by this deployment. Migrating an EXISTING
deployment means proving the encrypted lane end to end BEFORE declaring the
plaintext slots historical. The sequence:

1. **Configure the crypt remote.** Add the `vault-recovery-crypt` remote
   (type `crypt`, non-empty underlying remote and password, filename
   encryption `standard`, directory-name encryption enabled) to the
   `RCLONE_CONFIG_B64` secret and set the GitHub secret.
2. **Deploy.** The deploy preflight validates the remote AND runs the real
   remote readiness gate (a write + read-back probe through the production
   crypt remote) BEFORE any teardown: an unreachable or broken remote
   aborts the deploy with the existing deployment — and any legacy lane
   state — retained untouched. The portability gate runs; the uploader
   starts. If the remote is missing the deployment
   FAILS — there is no degraded mode that silently skips backups.
3. **Prove the lane** (operator, on the server):
   - upload now: `docker compose run --rm --no-deps -e VAULT_RECOVERY_ONCE=true vault-recovery-uploader`
   - list committed generations: `docker compose --profile recovery run --rm --no-deps vault-recovery-recover list-remote`
   - full recovery drill (see "Disaster-recovery drill" below): download →
     verify → install → rollback against the live stack in a maintenance
     window.
   - automated equivalent: `make test-vault-recovery-dr-drill` (Docker-gated).
4. **Declare the migration complete** only after the initial encrypted
   upload, download, full verification, and drill all succeeded — the
   plaintext lane is never retired on top of an unproven remote: the deploy
   gate (2) guarantees a syntactically valid but unreachable remote aborts
   BEFORE the old service is torn down.
5. **Retire the plaintext slots LATER, explicitly, by hand.** Deleting the
   remote plaintext GDrive slots (`Josemar/obsidian-backups/slot-*`) and the
   local `obsidian-backup-state` volume requires an explicit, later operator
   confirmation — deployment automation NEVER deletes them. After the
   rollback window and the operator decision, they can be removed manually;
   see `docs/obsidian-operations.md` → "Retired plaintext lane".

## Disaster-recovery drill (Docker-gated, mandatory release gate)

`tests/runtime/test_vault_recovery_dr_drill.py` (`make
test-vault-recovery-dr-drill`, `RUN_DOCKER_TESTS=1`; the release/deploy
workflow forces it with `VAULT_RECOVERY_DR_DRILL_REQUIRED=1`, which makes a
missing docker CLI a FAILURE — no opt-in bypass) is the full disaster drill
on the pinned images, combining the Phase-1 real-vector proof with the
Phase-2 encrypted lane, an explicit DESTROY step, and the ordered
maintenance-window sequence:

1. disposable isolated Hermes runtime; real gbrain PGLite state with a
   DB-only manual link, config keys, schema-pack files + the
   `active-schema-pack` marker, and a vault note;
2. REAL vector-bearing DB state through the pinned gbrain embedding
   workflow (stub embeddings endpoint, `migrate embeddings --no-embed` +
   `embed --stale --include-null-signature`, completion marker with the real
   model tuple); the live semantic query returns the expected page;
3. production export wrapper -> staged generation; production uploader
   one-shot -> remote decrypted verification -> committed + ack;
4. **MAINTENANCE WINDOW (ordered and asserted, with a REAL server-side
   Syncthing)**: pause/disable ALL THREE
   owned jobs (`gbrain-refresh`, `gbrain-embedding-refresh`,
   `vault-recovery-export`) and assert they are absent from jobs.json; the
   drill STARTS the real Syncthing service, asserts it is running, then
   stops Hermes AND server Syncthing and asserts both are not running. The
   runtime cannot stop the PAIRED device's Syncthing/Obsidian, so the
   runbook models that as an explicit operator gate: before the window,
   the operator confirms **every paired device** paused Syncthing (or is
   offline) and closed Obsidian — the drill asserts the server side, the
   checklist gates the paired side;
5. **DESTROY both live trees** (writers stopped: the complete
   `/opt/data/.gbrain` tree and every vault entry are deleted; the mount
   roots stay so the install can swap into them) — the live state is gone;
6. production recover step -> validated `RECOVERY_READY` handoff; short-lived
   hermes `verify-recovery` (disposable doctor) -> `VERIFIED_READY`; the
   drill then asserts the destroyed live trees are STILL EMPTY — the
   disposable doctor never opened/re-created the live state even though
   the exported config carries the live absolute paths (verifier
   containment proof);
7. short-lived hermes `install-recovery` (journaled two-tree swap; `.gbrain`
   atomic, vault per-entry) into the destroyed mount layout;
8. **CONTROLLED RESTART**: start Hermes again, wait for the init completion
   marker, then the **survival proofs**: the restored live `.gbrain` opens on
   the doctor (connection/jsonb_integrity/schema_version/pgvector ok), the
   DB-only link is readable, page/vault contents are live, config keys
   survive (`search.mcp_keyword_only false`), schema-pack files + markers are
   byte-identical, and the RESTORED vectors answer the semantic query with
   zero stale rows — no reindex/rebuild/sync;
9. operator `rollback` (Hermes stopped again, per the documented
   install/rollback sequence) restores the (destroyed) pre-install state with
   `status: rolled-back`.

If this drill cannot pass, the migration must not be declared complete and
the deployment is BLOCKED — no fallback (plaintext transport or in-place
installs) may be substituted.

## Phase-1 architecture

```
+-------------------+   tasknotes lock held   +----------------------------+
|      hermes       | ----------------------> | vault-recovery-staging     |
|  (no-agent cron,  |   daily export (04:00   | volume (Hermes-only,       |
|   default on)     |   local container time) | allowlisted+writable)      |
+-------------------+                         | <generation-id>/vault/     |
        ^                                     | <generation-id>/.gbrain/   |
        | direct native doctor preflight      | manifest.json, READY,      |
        +--- /opt/josemar/libexec/gbrain-native | latest (atomic)          |
                                              +----------------------------+
```

- The exporter runs **as the hermes runtime user** under the **existing
  shared TaskNotes/gbrain cooperative lock** (`/opt/data/.locks/tasknotes.lock`).
  The Hermes identity is enforced at every boundary: the export cron and the
  thin wrapper resolve the actual runtime uid (configured `HERMES_UID`, else
  the system `hermes` user's uid, else the default `10000`) and refuse root
  and any other uid; the core re-validates the same identity at its own CLI
  boundary before doing any work.
- The export cron and wrapper never call the public `gbrain` adapter or
  `josemar-gbrain` (no nested lock). gbrain access is limited to a direct
  invocation of the private native binary for the doctor preflight.
- The staging volume is mounted **only** in Hermes and is in the
  `HERMES_WRITABLE_VOLUMES` allowlist in `docker-hermes-init.sh` (base
  feature, unconditional — unlike the Mnemosyne opt-in staging dir).

## Generation layout

```
/opt/data/vault-recovery/staging/
├── latest                        # atomic pointer: "<generation-id>\n"
└── <generation-id>/              # e.g. 20260802T012247123456Z-a1b2c3d4
    ├── vault/                    # full Obsidian vault (no-follow copy)
    ├── .gbrain/                  # complete gbrain state tree
    ├── manifest.json             # structural metadata (no note contents)
    └── READY                     # written last, before atomic publication
```

Publication order: the generation is built in a hidden
`. <generation-id>.tmp` directory on the staging volume; `manifest.json`,
then `READY`, are written inside it; the directory is renamed into place;
only then is `latest` atomically replaced. A failed export leaves **no**
generation directory, **no** READY, and **no** `latest` update. Generation
ids are lexically sortable UTC timestamps with a random suffix
(`YYYYmmddTHHMMSSffffffZ-<8 hex>`), strictly validated against traversal.

## Preflight contract

1. **Shared lock.** The core validates that it runs under the shared lock:
   `TASKNOTES_LOCK_FD` must refer to the exact lock path with an exclusive
   flock actually held (same `/proc/self/fdinfo` check as `josemar-gbrain`).
   It fails closed otherwise. A busy lock at cron time is a graceful daily
   skip (wrapper exit 75 → logged, non-fatal; the next day retries).
2. **Doctor DB-open preflight.** The core runs
   `gbrain doctor --json` through the private native binary with the
   canonical env (`GBRAIN_HOME`, `GBRAIN_BRAIN_REPO`, `GBRAIN_SCHEMA_PACK`
   from the `active-schema-pack` marker, `GBRAIN_SKIP_STARTUP_HOOKS=1`) and
   waits for exit. Against the pinned v0.46.26.0 doctor schema the report
   must contain the checks `connection`, `jsonb_integrity`,
   `schema_version`, and `pgvector` **exactly once each** with status `ok`;
   warnings on any other check are allowed, **any** `fail` rejects the
   export. The doctor summary is recorded in the manifest.
3. **Active-PGLite indicators.** After the doctor exits and before the
   physical copy, the `.gbrain` tree is walked no-follow for runtime
   artifacts that indicate an open (or crashed-but-uncleaned) database:
   socket-type entries and the names `postmaster.pid`, `postmaster.opts`,
   `.s.PGSQL.*`. Any hit fails the export closed — the exporter never
   guesses that an indicator is stale. Indicator-scan errors (unreadable
   directory, failed lstat, symlinked component) also fail closed: the
   exporter cannot prove the absence of indicators it could not inspect.
   The Docker proof verifies the real pinned image leaves none behind after
   doctor exit.

## Copy and convergence semantics

- **Directory-fd / openat-style no-follow copy.** Both trees are scanned and
  copied with directory-file-descriptor operations (`os.scandir(fd)`,
  `dir_fd`-relative `O_NOFOLLOW|O_DIRECTORY` opens, mkdirat/openat and
  `dir_fd`-relative renames). The source root itself is opened with
  `O_NOFOLLOW`, so a symlink source root is rejected; every intermediate
  component is opened no-follow, so symlinked components and component races
  (an entry swapped between lstat and open) fail closed with `TreeScanError`.
  Symlinks and special files (fifo/socket/device) are rejected, never
  followed.
- **Modes and empty directories.** File modes and empty directories are
  preserved, and the ROOT mode of each source tree is recorded too (it is
  part of the convergence digest and stored as `trees.<name>.root_mode` in
  the manifest). Directory modes are applied **only after all children are
  copied**, deepest first, so read-only source directories (and read-only
  roots) copy successfully.
- **Durability (fsync).** Every copied file is fsynced before its rename;
  every created directory (including the tree roots), `manifest.json`,
  `READY`, the generation directory, and the staging root are fsynced. Any
  fsync failure aborts the export with nothing published: the temp
  generation dir is removed, a generation dir already renamed into place is
  rolled back, and a `latest` pointer already overwritten is restored to the
  previous generation.
- **Whole-tree convergence.** `source scan A → copy → source scan B → staged
  scan`. Every entry is compared by path/type/mode/content SHA-256/dirs
  (root mode included). The generation is published ONLY when
  `scan A == scan B` and the staged tree equals `scan A`. Any divergence
  triggers a bounded retry (default 3 attempts, fresh copy each time, small
  delay between attempts); after the bound the export **fails with no READY**
  and nothing is published. The lock is held through source/staging
  validation and publication.
- The manifest records both source and staged tree digests
  (`scan_digest`/`staged_digest`) plus the attempt count, so a published
  generation is auditable.

## Manifest

`manifest.json` is structural only — no note contents and no secrets:
`schema_version` (1), `generation_id`, `created_at_utc`, `phase` (1),
`remote.uploaded` (false; the phase-2 uploader's ack ledger is the
remote-truth acknowledgement and the manifest is never rewritten after
publication), `sources`, per-tree stats and digests (`root_mode`,
`scan_digest`, `staged_digest`, `entries_file`, `entries_digest`), the
doctor summary (`required_checks` + `check_counts`), and the convergence
record. The exporter version and Python version are recorded for drift
diagnostics. Each tree also carries a machine-readable per-entry index
(`<tree>.entries.txt`: `file|dir`, octal mode, size, sha256, path — one line
per entry, digest-bound by `entries_digest`) so the shell uploader/recover
steps can validate the full tree entry-by-entry without Python. **Strict
schema validation:** the exporter self-checks the manifest against its
exact schema-version-1 shape before publication, and the Hermes-side
verify/install core re-validates the FULL schema (every block/key/type and
every digest format; unknown keys or structural drift refuse the
generation). The shell uploader/recover steps enforce JSON
well-formedness with a POSIX-awk validator before any field is extracted
(the pinned rclone image has no python3/jq), so a malformed document whose
fields stay grep-visible can never become READY or be acknowledged.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `VAULT_RECOVERY_EXPORT_ENABLED` | `true` | `false`/`0` removes the owned cron job |
| `VAULT_RECOVERY_EXPORT_SCHEDULE` | `0 4 * * *` | 5-field cron, container **local** timezone; malformed values disable the job |
| `VAULT_RECOVERY_EXPORT_TIMEOUT` | `240` | inner export budget (s); capped below `HERMES_CRON_SCRIPT_TIMEOUT` |
| `VAULT_RECOVERY_KILL_GRACE` / `GROUP_DRAIN` / `TIMEOUT_MARGIN` | `5`/`2`/`10` | lock-runner termination bounds |
| `VAULT_RECOVERY_STAGING_DIR` | `/opt/data/vault-recovery/staging` | staging root (compose-fixed path) |

Phase-2 (overlay) variables are in the "Phase-2 environment" table above.

The cron installer in `docker-hermes-init.sh` is idempotent and reconciles
drift by full expected state (schedule, script name, `no_agent`, workdir).
`VAULT_RECOVERY_EXPORT_ENABLED=false` removes only the owned
`vault-recovery-export` job.

## Operations

- **View the latest generation:** `docker compose exec hermes su -s /bin/sh
  hermes -c '/opt/hermes/.venv/bin/python3 -I
  /opt/josemar/scripts/vault_recovery_core.py latest'`
- **List generations:** same invocation with `list`.
- **Manual export (operator):** `docker compose exec hermes su -s /bin/sh
  hermes -c '/opt/josemar/scripts/vault-recovery-export.sh'`. Runs under
  the shared lock with the same preflight/convergence contract as the cron.
- **Observe cron runs:** `docker compose logs hermes | grep vault-recovery`.
  Exit 75 (lock busy) is logged as a skip; convergence/preflight failures
  exit 2 with no generation published.
- **Agent-facing guidance:** chat-facing backup status/recovery guidance
  lives in `skills-factory/backup-operations/SKILL.md`. The only
  chat-visible status action is `josemar-backup-status` — a read-only LOCAL
  STAGING OBSERVATION; remote status and every recovery step are
  operator-only.

## Phase limitations

1. **The encrypted lane is the DEFAULT deployment composition (Phase 3).**
   The deploy workflow always applies the overlay and FAILS when the crypt
   remote is missing. Local base-compose runs (no overlay) stage generations
   locally but do not back them up remotely — without the overlay the staged
   artifacts are not a backup.
2. **Retention is remote AND local.** The uploader prunes the remote
   committed namespace to the newest `VAULT_RECOVERY_RETENTION` (default
   14) generations that carry a valid `READY` marker bound to the manifest
   (after inventory + ack validation; incomplete committed dirs are
   preserved, never counted or pruned). Local staging retention is
   ack-based: after a remote-acknowledged upload, staged generations beyond
   the newest `VAULT_RECOVERY_LOCAL_RETENTION` (default 14) FULL
   generations are pruned through the dedicated writable `/staging-prune`
   mount, only when acknowledged in the local ledger; invalid states skip
   the entire local prune, and `latest`/artifacts are never touched.
3. **Recovery requires the handoff volume and the hermes uid.** The recover
   step chowns the disposable recovery volume to `HERMES_UID` when it runs
   as root; the verify/install/rollback short-lived runs must run as the
   hermes runtime user (`--user`, see Phase-2 operations) or the restore
   core refuses (issue #110 identity enforcement).
4. **The plaintext `obsidian-backup` lane is RETIRED (Phase 3).** Existing
   plaintext GDrive slots are never deleted by automation; they remain for
   manual historical recovery until the operator explicitly retires them
   (see "Migration sequence" and `docs/obsidian-operations.md` → "Retired
   plaintext lane").
5. **Convergence, not a strict point-in-time snapshot.** Daily exports use
   bounded whole-tree convergence: if the vault or `.gbrain` keeps changing
   (e.g. Syncthing/Obsidian activity), the export retries up to the bound and
   then fails without publishing. A strict quiescent snapshot requires a
   manual maintenance window that pauses Syncthing and Obsidian on every
   paired device (see the drill's paired-device writer gate).
6. **Busy-lock skips are silent-ish.** A skipped daily run is visible only in
   cron logs and the absence of a new generation; alerting is not part of
   phase 1/2.
7. **Post-install hidden dirs.** A completed install leaves
   `<live-vault>/.vault-recovery-install/<gen>/` and
   `/opt/data/.vault-recovery-install/<gen>/` (staged trees + backups) plus
   the journal; operators remove them after the rollback window (see
   Phase-2 operations).
8. **Encrypted transport relaxes modes.** rclone crypt cannot round-trip
   POSIX modes, so remote verification/download validation is content-exact
   with modes relaxed; the install re-applies the exact recorded modes from
   the entries index, so the restored tree is mode-exact.

## Portability proof

`tests/runtime/test_vault_recovery_portability.py` is the **release gate**
for the whole feature. Locally it is Docker-gated (`RUN_DOCKER_TESTS=1`,
skips when the docker CLI is unavailable), but the release/deploy workflow
runs it as a **mandatory pre-mutation step** with
`RUN_DOCKER_TESTS=1 VAULT_RECOVERY_PORTABILITY_REQUIRED=1`: the opt-in env
var is then ignored and a missing docker CLI or any failed assertion
**fails the deploy** — there is no opt-in bypass on the release path. On the
real pinned image it:

1. activates a fresh PGLite state in a disposable container,
2. creates DB-only records (pages + a manual link), config keys, and
   schema-pack files + the `active-schema-pack` marker,
3. creates **REAL vector-bearing DB state** through the pinned gbrain
   embedding workflow (issue #65): a stub OpenAI-compatible embeddings
   endpoint, then the exact native sequence that
   `enable-embeddings` + `embed-backfill` run — `migrate embeddings
   --no-embed` persists the model tuple and lifts the `embedding_disabled`
   sentinel, `embed --stale --include-null-signature` stamps actual pgvector
   rows, and the completion marker is written with the real model tuple.
   No config sentinels or no-embedding init stand in for vectors,
4. proves the live vectors are real by answering a semantic query with the
   expected page,
5. validates the live doctor report with the production validator,
6. runs the **production export wrapper** as the hermes user,
7. physically copies the staged `.gbrain` + vault into a fresh restore root,
8. re-runs the real doctor on the restored tree (same required checks),
   reads back the DB-only link, page content, config keys, and byte-compares
   the markers,
 9. proves the **restored** vectors survive: the semantic query still returns
    the page and `embed --stale --dry-run` finds zero stale rows — no
    reindex/rebuild/sync, and
10. asserts the staged trees are byte-identical to the live sources **as of
    the converged scan** via the manifest convergence record: the manifest's
    `scan_digest` equals its `staged_digest` (the exporter enforced
    staged == live before publishing) and a re-scan of the immutable staged
    trees reproduces the manifest's `staged_digest` — both through the
    production `scan_tree`/`scan_digest`. A direct end-of-test live re-scan
    is deliberately NOT compared: PGLite background checkpoints may rewrite
    live files at any moment, so the live tree is not guaranteed to equal
    the staged tree at the end of the test even when the export was
    byte-perfect; the exporter's own pre-publication convergence is the
    deterministic, race-free proof.

If this proof cannot pass on the pinned image, the feature is **blocked** —
no fallback (e.g. logical dumps or reindex-on-restore) may be substituted.
If the pinned runtime cannot create a real vector state in the test, the
test reports the exact blocker (command output) instead of passing.

## Phase-2 encrypted round-trip proof

`tests/runtime/test_vault_recovery_round_trip.py` is the Phase-2 release
gate (`make test-vault-recovery-round-trip`, Docker-gated via
`RUN_DOCKER_TESTS=1`; skipped without a docker CLI). On the pinned images
it exercises the WHOLE lane end to end over a REAL rclone crypt remote with
a local underlying directory:

1. disposable isolated Hermes runtime; real gbrain PGLite state
   (`init --pglite --no-embedding` — the required doctor checks
   connection/pgvector/schema_version/jsonb_integrity all pass) + a real
   vault note + schema-pack marker;
2. production export wrapper -> staged generation (READY + manifest +
   per-tree entries index);
3. production uploader ONE-SHOT -> uncommitted -> remote DECRYPTED
   verification -> commit move -> post-commit verification -> ack ledger;
4. **ciphertext proof:** every path component under the underlying
   (pre-crypt) namespace is encrypted (no plaintext marker, no generation
   id, no `manifest.json`, no `vault/` anywhere), while listing through the
   crypt remote shows the decrypted generation layout. The
   metadata-encryption standard that makes this proof hold is enforced at
   the config level by the uploader/recover `require_remote` and the deploy
   preflight (`filename_encryption` must be `standard`,
   `directory_name_encryption` enabled — `off`/`obfuscate`/`false` are
   rejected before any transfer);
5. production recover step (profile-gated rclone service) -> validated
   `RECOVERY_READY` handoff;
6. short-lived hermes `verify-recovery` (as the hermes uid, recovery volume
   mounted transiently) -> `VERIFIED_READY` with the disposable doctor;
7. short-lived hermes `install-recovery` into the REAL mount layout: the
   live vault at `/opt/data/obsidian` is the root of the obsidian-vault
   volume, so rename(2) on the mount root fails EBUSY and the journaled
   per-entry vault swap takes over while `.gbrain` gets the atomic rename
   swap (`swap_modes == {".gbrain": "atomic", "vault": "per-entry"}`,
   journal status complete);
8. post-install proofs: the restored live `.gbrain` opens on the doctor,
   the restored page/note contents are live, the pre-install-only file was
   moved aside into the vault backup root;
9. operator `rollback` restores the original live content (journal
   status rolled-back).

If this proof cannot pass, Phase 2 is blocked — no fallback (e.g. plaintext
transport or in-place installs) may be substituted.
