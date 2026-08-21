"""Host-side support layer for the opt-in gbrain conformance suite (issue #127 W1b).

This module is NOT a test module. It provides:

  - ``GbrainConformanceRuntime``: a ``ComposeRuntime`` subclass that enforces
    the conformance isolation contract (workspace sync, Telegram/hosted
    provider credentials, and all owned gbrain/vault-recovery jobs disabled),
    non-root ``run_as_hermes`` helpers, baseline/candidate build helpers with
    strict candidate-ref validation, disposable source-state seeding, and
    unconditional ``down -v --remove-orphans`` cleanup.
  - pure helpers for candidate-ref validation, Dockerfile ``GBRAIN_REF``
    parsing, source-state seeding, synthetic vault initialization, and
    report writing under ``dump_folder/gbrain-conformance``.

Safety model (root AGENTS.md + issue #127):
  - everything runs against a disposable Compose project (unique project
    name, disposable agent-state/credentials mounts, repo ``.env`` bypassed)
  - in-container commands run as the ``hermes`` runtime user, never root
  - candidate refs are exact 40-hex Git SHAs only, normalized lower-case
    BEFORE any Docker invocation
  - reports contain synthetic command/result metadata only — never
    environment dumps
  - final cleanup is unconditional ``down -v --remove-orphans``
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import time
from typing import Any

from .helpers import ComposeRuntime, REPO_ROOT


# ---------------------------------------------------------------------------
# Candidate ref validation
# ---------------------------------------------------------------------------

CANDIDATE_REF_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def normalize_candidate_ref(raw: str) -> str:
    """Validate an exact 40-hex Git commit SHA and return it lower-cased.

    Rejects branches, tags, short SHAs, URLs, and shell fragments. The
    lower-case normalization happens BEFORE any Docker invocation so the
    build arg is deterministic and Docker never sees an unvalidated value.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("candidate ref must be a non-empty string")
    value = raw.strip()
    if not CANDIDATE_REF_RE.fullmatch(value):
        raise ValueError(
            "candidate ref must be an exact 40-character hexadecimal Git "
            f"commit SHA, got {value!r}"
        )
    return value.lower()


# ---------------------------------------------------------------------------
# Dockerfile GBRAIN_REF parsing (canonical single default)
# ---------------------------------------------------------------------------

GBRAIN_REF_ARG_RE = re.compile(
    r"^ARG\s+GBRAIN_REF=([0-9a-fA-F]{40})\s*$", re.MULTILINE
)
GBRAIN_REF_ANY_RE = re.compile(r"^ARG\s+GBRAIN_REF\b", re.MULTILINE)


def parse_gbrain_ref_text(text: str, *, source: str = "Dockerfile.hermes") -> str:
    """Parse the canonical single-default ``GBRAIN_REF`` from Dockerfile text.

    Exact and deterministic: requires exactly one ``ARG GBRAIN_REF=<40-hex>``
    definition. Raises a clear error for:
      - no definition at all,
      - a malformed definition (an ``ARG GBRAIN_REF`` line that is not an
        exact 40-hex SHA),
      - multiple definitions (ambiguous).
    """
    valid = [m.group(1) for m in GBRAIN_REF_ARG_RE.finditer(text)]
    if len(valid) > 1:
        raise ValueError(
            f"ambiguous GBRAIN_REF: {len(valid)} ARG GBRAIN_REF=<40-hex> "
            f"definitions found in {source}; expected exactly one"
        )
    if valid:
        return valid[0].lower()
    declared = [
        line.strip()
        for line in text.splitlines()
        if GBRAIN_REF_ANY_RE.match(line.strip())
    ]
    if declared:
        raise ValueError(
            f"malformed GBRAIN_REF definition in {source}: {declared[0]!r} "
            "is not an exact 40-hex SHA"
        )
    raise ValueError(f"no ARG GBRAIN_REF=<40-hex> definition found in {source}")


def parse_dockerfile_gbrain_ref(dockerfile: Path | None = None) -> str:
    """Parse the canonical committed ``GBRAIN_REF`` from ``Dockerfile.hermes``
    (or an explicit path)."""
    path = dockerfile or (REPO_ROOT / "Dockerfile.hermes")
    return parse_gbrain_ref_text(path.read_text(encoding="utf-8"), source=str(path))


# ---------------------------------------------------------------------------
# Disposable source-state seeding (real template files only)
# ---------------------------------------------------------------------------

TEMPLATE_STATE_DIR = REPO_ROOT / "templates" / "agent-state-template"
SYNC_MANIFEST_SOURCE = TEMPLATE_STATE_DIR / ".sync-manifest"
CANONICAL_PACK_SOURCE = (
    TEMPLATE_STATE_DIR / ".gbrain" / "schema-packs" / "josemar" / "pack.yaml"
)

# Expected relative paths inside the disposable source-agent-state mount.
SOURCE_STATE_MANIFEST_REL = Path(".sync-manifest")
SOURCE_STATE_PACK_REL = Path(".gbrain/schema-packs/josemar/pack.yaml")


def _resolve_inside(root: Path, rel: Path) -> Path:
    """Resolve ``rel`` inside ``root``, rejecting escape.

    Rejects absolute ``rel``, any ``..`` component, and any symlink that
    would resolve outside ``root``.
    """
    if rel.is_absolute():
        raise ValueError(f"relative path must not be absolute: {rel}")
    if ".." in rel.parts:
        raise ValueError(f"relative path must not contain '..': {rel}")
    candidate = root / rel
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes disposable dir: {rel}")
    return candidate


def seed_source_state(
    state_dir: Path,
    *,
    manifest_rel: Path = SOURCE_STATE_MANIFEST_REL,
    pack_rel: Path = SOURCE_STATE_PACK_REL,
) -> tuple[Path, Path]:
    """Seed a disposable source-agent-state dir with the repository's real
    template ``.sync-manifest`` and canonical ``josemar`` schema pack.

    Only the two canonical paths are copied, into a disposable dir (never
    into the repo), preserving the expected relative paths so
    ``docker-hermes-init.sh::seed_workspace_from_manifest`` places the pack
    at the canonical runtime location. Escape is rejected. Returns the
    (manifest, pack) destination paths.
    """
    state_dir = state_dir.resolve()
    manifest_dst = _resolve_inside(state_dir, manifest_rel)
    pack_dst = _resolve_inside(state_dir, pack_rel)
    manifest_dst.parent.mkdir(parents=True, exist_ok=True)
    pack_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SYNC_MANIFEST_SOURCE, manifest_dst)
    shutil.copy2(CANONICAL_PACK_SOURCE, pack_dst)
    return manifest_dst, pack_dst


# ---------------------------------------------------------------------------
# Synthetic vault initialization (committed as hermes, nonpersonal identity)
# ---------------------------------------------------------------------------

# Deterministic non-personal git identity for the synthetic vault commit.
VAULT_GIT_USER = "gbrain-conformance"
VAULT_GIT_EMAIL = "gbrain-conformance@localhost"


def synthetic_vault_init_script() -> str:
    """Return a ``sh -lc`` body that initializes ``/opt/data/obsidian`` as a
    git repo and commits the baseline synthetic vault as the hermes runtime
    user, using a deterministic non-personal identity."""
    return f"""set -eu
cd /opt/data/obsidian
git init -q -b main .
git config user.name "{VAULT_GIT_USER}"
git config user.email "{VAULT_GIT_EMAIL}"
mkdir -p notes people projects
cat > notes/welcome.md <<'MD'
# Welcome

Deterministic conformance note with unique search token: conformance-token-welcome.
MD
cat > people/alice.md <<'MD'
# Alice

A synthetic person page for type inference.
MD
cat > projects/atlas.md <<'MD'
# Atlas

A synthetic project page linked from [[notes/welcome]].
MD
git add .
git commit -qm "synthetic conformance vault baseline"
"""


# ---------------------------------------------------------------------------
# Command evidence (complete, no silent truncation)
# ---------------------------------------------------------------------------


@dataclass
class CommandEvidence:
    """Complete subprocess evidence for one in-container command.

    Captures rc, stdout, stderr, and elapsed seconds with NO silent
    truncation: the full text is retained in the object and in the report.
    """

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


# ---------------------------------------------------------------------------
# Reports (synthetic command/result metadata only, never environment dumps)
# ---------------------------------------------------------------------------


def conformance_report_dir() -> Path:
    """Host-side gitignored report directory for conformance evidence."""
    return REPO_ROOT / "dump_folder" / "gbrain-conformance"


def write_report(
    report_dir: Path,
    name: str,
    evidence: Sequence[CommandEvidence],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> Path:
    """Write a conformance report under ``report_dir``.

    Contains synthetic command/result metadata only: each command's argv,
    rc, stdout, stderr, and elapsed time, plus any explicit ``metadata``.
    NEVER includes the process or runtime environment (no env dumps).
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "name": name,
        "results": [e.to_dict() for e in evidence],
    }
    if metadata:
        payload["metadata"] = dict(metadata)
    path = report_dir / f"{name}.json"
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# GbrainConformanceRuntime
# ---------------------------------------------------------------------------

# Telegram / hosted-provider / control-plane credentials that must be empty
# in the conformance runtime. The base ComposeRuntime sanitizer already
# blanks these; the subclass re-asserts them explicitly so the contract is
# self-documenting. Deliberately does NOT include the dashboard credentials
# (the base sets deterministic test-only values that compose requires).
CONFORMANCE_EMPTY_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ENABLED",
    "PRIMARY_TELEGRAM_ID",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_HOME_CHANNEL",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_AUTH_PASSWORD",
    "GATEWAY_AUTH_TOKEN",
    "HERMES_TELEGRAM_BOT_TOKEN",
    "HERMES_TELEGRAM_ALLOWED_USERS",
    "HERMES_TELEGRAM_HOME_CHANNEL",
    "HERMES_GATEWAY_ALLOWED_USERS",
    "ZAI_API_KEY",
    "GLM_API_KEY",
    "DEEPSEEK_API_KEY",
    "OLLAMA_API_KEY",
    "TAVILY_API_KEY",
    "APOLLO_IO_API_KEY",
    "HERMES_MODEL",
    "TS_AUTHKEY",
    "TS_EXTRA_ARGS",
    "GOG_KEYRING_PASSWORD",
    "HERMES_API_SERVER_KEY",
)

# Owned gbrain/vault-recovery cron jobs that must be disabled in the
# conformance runtime (root AGENTS.md maintenance-window invariant).
OWNED_JOB_NAMES = (
    "gbrain-refresh",
    "gbrain-embedding-refresh",
    "vault-recovery-export",
)


class GbrainConformanceRuntime(ComposeRuntime):
    """ComposeRuntime specialized for the gbrain conformance suite (issue #127).

    Enforces the conformance isolation contract on top of the base
    fail-closed ComposeRuntime:

      - workspace sync disabled (WORKSPACE_SYNC_ON_START=false,
        WORKSPACE_SYNC_INTERVAL=0, WORKSPACE_STATE_REPO empty)
      - Telegram / hosted-provider / control-plane credentials blanked
      - all owned gbrain/vault-recovery jobs disabled
        (GBRAIN_REFRESH_INTERVAL=0, GBRAIN_EMBED_REFRESH_SCHEDULE=0,
        VAULT_RECOVERY_EXPORT_ENABLED=false)
      - COMPOSE_PROFILES empty (no aux-ml / embeddings sidecars)
      - ``run_as_hermes`` helpers for non-root in-container commands
      - baseline build with no override; candidate build with an explicit
        validated build arg; safe same-volume recreate
      - disposable source-state seeding from the real template
      - synthetic vault init committed as hermes
      - reports under ``dump_folder/gbrain-conformance`` (no env dumps)
      - unconditional final cleanup via ``down -v --remove-orphans``
    """

    def __init__(self, *, overlays: Sequence[Path | str] = ()) -> None:
        super().__init__(overlays=overlays)
        # 1. Workspace sync disabled (base already sets these; re-asserted
        #    explicitly so the contract is self-documenting).
        self.env.update(
            {
                "WORKSPACE_SYNC_ON_START": "false",
                "WORKSPACE_SYNC_INTERVAL": "0",
                "WORKSPACE_STATE_REPO": "",
                "WORKSPACE_REPO_TOKEN": "",
            }
        )
        # 2. Telegram / hosted-provider / control-plane credentials blanked
        #    (defense in depth on top of the base sanitizer).
        for key in CONFORMANCE_EMPTY_ENV_KEYS:
            self.env[key] = ""
        # 3. All owned gbrain/vault-recovery jobs disabled.
        self.env.update(
            {
                "GBRAIN_REFRESH_INTERVAL": "0",
                "GBRAIN_EMBED_REFRESH_SCHEDULE": "0",
                "VAULT_RECOVERY_EXPORT_ENABLED": "false",
            }
        )
        # 4. No sidecar profiles.
        self.env["COMPOSE_PROFILES"] = ""

    # --- non-root in-container command helpers ---------------------------

    def run_in_container(
        self,
        *command: str,
        as_user: str = "hermes",
        check: bool = True,
        timeout: int = 180,
    ) -> CommandEvidence:
        """Run a command inside the hermes container as ``as_user`` (default
        ``hermes``, never root for gbrain/vault work) and capture complete
        evidence (rc/stdout/stderr/elapsed, no truncation)."""
        full = ["su", "-s", "/bin/sh", "--", as_user, "-c", shlex.join(command)]
        start = time.monotonic()
        proc = self.exec("hermes", *full, check=check, timeout=timeout)
        elapsed = time.monotonic() - start
        return CommandEvidence(
            command=list(command),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            elapsed_seconds=elapsed,
        )

    def run_as_hermes(
        self,
        *command: str,
        check: bool = True,
        timeout: int = 180,
    ) -> CommandEvidence:
        """Run a command inside the hermes container as the hermes runtime
        user (issue #110: never root) and capture complete evidence."""
        return self.run_in_container(
            *command, as_user="hermes", check=check, timeout=timeout
        )

    # --- baseline / candidate build helpers ------------------------------

    def baseline_gbrain_ref(self) -> str:
        """The canonical committed ``GBRAIN_REF`` parsed from
        ``Dockerfile.hermes`` (single default, no override)."""
        return parse_dockerfile_gbrain_ref()

    def build_baseline(self, *services: str, timeout: int = 900) -> None:
        """Build the baseline image with NO build-arg override: the
        Dockerfile default ``GBRAIN_REF`` is used."""
        self.build(*services, timeout=timeout)

    def build_candidate(
        self,
        candidate_ref: str,
        *services: str,
        timeout: int = 900,
    ) -> None:
        """Build with an explicit validated candidate ``GBRAIN_REF`` build
        arg. The ref is validated and normalized lower-case BEFORE any Docker
        invocation."""
        ref = normalize_candidate_ref(candidate_ref)
        self.build(*services, build_args={"GBRAIN_REF": ref}, timeout=timeout)

    def recreate_same_volumes(self, *services: str, timeout: int = 600) -> None:
        """Force-recreate services against the SAME disposable volumes
        without an implicit build and without deleting volumes (only final
        ``cleanup()`` deletes project volumes)."""
        self.recreate(*services, timeout=timeout)

    # --- disposable source-state seeding ---------------------------------

    def seed_source_state(self) -> tuple[Path, Path]:
        """Seed the disposable source-agent-state mount with the repository's
        real template ``.sync-manifest`` and canonical ``josemar`` pack,
        preserving the expected paths. Returns (manifest, pack) host paths."""
        state_dir, _ = self.disposable_mounts()
        return seed_source_state(state_dir)

    # --- synthetic vault --------------------------------------------------

    def init_synthetic_vault(self, *, timeout: int = 120) -> CommandEvidence:
        """Initialize ``/opt/data/obsidian`` as a git repo and commit the
        baseline synthetic vault as the hermes runtime user (nonpersonal
        deterministic identity)."""
        return self.run_as_hermes(
            "sh", "-lc", synthetic_vault_init_script(), timeout=timeout
        )

    # --- owned-jobs verification -----------------------------------------

    def assert_owned_jobs_disabled(self, *, timeout: int = 120) -> CommandEvidence:
        """Assert no owned gbrain/vault-recovery cron job is present in the
        disposable runtime's ``/opt/data/cron/jobs.json`` (or that the file
        is absent). Runs as hermes."""
        names_json = json.dumps(list(OWNED_JOB_NAMES))
        script = (
            "set -eu\n"
            "if [ -f /opt/data/cron/jobs.json ]; then\n"
            "  python3 - /opt/data/cron/jobs.json <<'PY'\n"
            "import json, sys\n"
            f"owned = set({names_json})\n"
            "data = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "present = {j.get('name') for j in data.get('jobs', []) if isinstance(j, dict)} & owned\n"
            "sys.exit(1 if present else 0)\n"
            "PY\n"
            "else\n"
            "  echo 'no jobs.json (owned jobs absent)'\n"
            "fi\n"
        )
        return self.run_as_hermes("sh", "-lc", script, check=True, timeout=timeout)

    # --- unconditional final cleanup -------------------------------------

    def cleanup(self) -> None:
        """Unconditional final teardown: ``docker compose down -v
        --remove-orphans`` (deletes the disposable project volumes) followed
        by disposable-mount cleanup. Idempotent; safe to call from
        ``addCleanup``."""
        self.down()
