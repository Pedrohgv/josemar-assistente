#!/usr/bin/env python3
"""Canonical workspace sync — one executable for tool, terminal, and lifecycle modes.

Standard-library only. Installed at /usr/local/bin/workspace-sync.

Modes:
  No arg:  JSON stdin/stdout tool (Hermes command-dispatch contract).
  status|diff|push|pull:  Terminal actions — exact action argv only.
  log [COUNT]:  Terminal log; COUNT defaults to 10, otherwise exactly
                one positive decimal integer.
  commit [MESSAGE...]:  Terminal commit; zero args default to
                        "Manual commit", otherwise args joined with
                        single spaces.
  sync [MESSAGE...]:  Terminal sync; zero args default to "Auto-sync",
                      otherwise args joined with single spaces.
  gh ARGS...:  Terminal gh; argv is passed losslessly to the gh
               binary (never re-serialized through a shell).
  startup: Initial clone or bidirectional sync (WORKSPACE_SYNC_ON_START).
  periodic: Bidirectional sync with remote-wins merge.

Terminal modes validate the full argv BEFORE any chdir/lock/manifest/
git/stdin access, never read stdin, and reject invalid action/arity/
count with nonzero, concise stderr usage, and zero stdout.

Authentication:
  Remotes stay credential-free. HTTPS auth uses ephemeral GIT_ASKPASS
  that reads WORKSPACE_REPO_TOKEN from the environment (no token literal
  in the helper script). WORKSPACE_REPO_TOKEN is translated to child-only
  GH_TOKEN for gh commands. No ~/.git-credentials, no gh auth login, no
  token in URLs/logs/args.

Locking:
  One fcntl workspace lock under <workspace>/.locks/workspace-sync.lock
  serializes all canonical workspace operations. The lock file is opened
  with O_NOFOLLOW and fstat-verified as a regular file before flock.
  The .locks directory is lstat-checked to reject symlinks.

Compatibility symlink /usr/local/bin/workspace-sync.sh is explicit-mode
path compatibility only (startup/periodic); it does not emulate old
no-arg lifecycle behavior.
"""

from __future__ import annotations

import datetime
import errno
import fcntl
import fnmatch
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/opt/data"))
MANIFEST_PATH = WORKSPACE_DIR / ".sync-manifest"
REPO_URL = os.environ.get("WORKSPACE_STATE_REPO", "")
REPO_TOKEN = os.environ.get("WORKSPACE_REPO_TOKEN", "")
BRANCH = os.environ.get("WORKSPACE_GIT_BRANCH", "main")
GIT_EMAIL = os.environ.get("WORKSPACE_GIT_USER_EMAIL", "agent@josemar.local")
GIT_NAME = os.environ.get("WORKSPACE_GIT_USER_NAME", "Josemar Agent")
SYNC_ON_START = os.environ.get("WORKSPACE_SYNC_ON_START", "true")

# Protected runtime entries — the canonical manifest policy.
PROTECTED_RUNTIME_PATHS: tuple[str, ...] = (
    "config.yaml",
    "credentials",
    ".config",
    "obsidian",
    "sessions",
    "logs",
    ".env",
    "auth.json",
    ".locks",
    ".gbrain/config.json",
    ".gbrain/brain.pglite",
    ".gbrain/last-update-check",
    ".gbrain/readiness.json",
    ".gbrain/audit",
    ".gbrain/migrations",
)

# Bare/broad .gbrain forms that must be rejected as protected paths.
BARE_GBRAIN_FORMS: tuple[str, ...] = (".gbrain", ".gbrain/", ".gbrain/*", ".gbrain/**")

# Explicit allowed schema pack path.
ALLOWED_SCHEMA_PACK = ".gbrain/schema-packs/josemar/pack.yaml"

# Intentional template wildcard pathspecs (the only allowed globs; exact
# string membership, so every broader form is rejected). Plain explicit
# paths such as ``hermes/models.yaml`` and
# ``hermes/command-allowlist/default.json`` carry no glob chars and are
# validated as normal manifest entries.
ALLOWED_WILDCARD_PATHSPECS: frozenset[str] = frozenset({
    "avatars/*",
    "hermes/skill-toggles/profiles/*.json",
    "hermes/command-allowlist/profiles/*.json",
})

VALID_ACTIONS = ("status", "diff", "log", "commit", "push", "pull", "sync", "gh")

# Maximum digit length accepted for a terminal `log` COUNT. The count
# is bounded for numeric safety: `git log -n` consumes it as a number,
# so anything beyond 6 digits (999,999 — far beyond any repo history)
# is rejected before dispatch instead of being passed to git.
MAX_LOG_COUNT_DIGITS = 6

LOCK_DIR = WORKSPACE_DIR / ".locks"
LOCK_FILE = LOCK_DIR / "workspace-sync.lock"

# Canonical models.yaml path inside the workspace (state-owned model
# selections). Validated locally before staging/commit and remotely before
# merge so invalid/malformed/secret model state never reaches the runtime
# config. Validation reuses the canonical helper (no duplicated rules).
MODELS_YAML_REL = "hermes/models.yaml"
MODELS_YAML_PATH = WORKSPACE_DIR / MODELS_YAML_REL
# Helper path: the josemar_skill_state module. In the container it is
# installed at /opt/hermes/hermes_cli/josemar_skill_state.py; for tests it
# is the repo's scripts/josemar_skill_state.py. Resolved lazily so the
# import only happens when models.yaml is actually present.
JOSEMAR_SKILL_STATE_PATH = os.environ.get(
    "JOSEMAR_SKILL_STATE",
    "/opt/hermes/hermes_cli/josemar_skill_state.py",
)

# Canonical command-allowlist sidecar family (state-owned runtime command
# allowlist, issue #151). Same default/profile layout as the skill-toggle
# family: exactly ``default.json`` (workspace root profile) plus
# ``profiles/<canonical>.json`` (named profiles). The manifest wildcard
# enumerates the family; it never authorizes noncanonical filenames —
# every profile sidecar must carry a canonical profile stem (the exact
# W1 helper contract, consumed below, never duplicated). Only these exact
# shapes are enumerated and transport-validated; every other ``hermes/``
# artifact stays ignored/rejected by the deny-by-default gitignore and
# manifest policy. Sidecar content schema is validated exclusively by the
# canonical helper validator.
ALLOWLIST_SIDECAR_REL_DIR = "hermes/command-allowlist"
ALLOWLIST_DEFAULT_REL = "hermes/command-allowlist/default.json"
ALLOWLIST_PROFILES_REL_DIR = "hermes/command-allowlist/profiles"

# Value-free command-allowlist migration-completion marker (plan v2).
# Architecture metadata only: exactly
# ``{"version":1,"legacy_runtime_import_complete":true}``. Monotonic
# durable state — once the marker exists in reachable state-repo history
# it must never be removed: working-copy deletion (when HEAD has it),
# a HEAD that removes it (before push), and a remote candidate that
# removes it (before merge) are all rejected. A history that never
# contained the marker is valid (first upgrade); normal merge semantics
# retain a local first-time marker addition. Allowlist-sidecar deletion
# is unaffected. Content is validated by the canonical helper marker
# validator at the same three boundaries as the sidecars.
ALLOWLIST_MARKER_REL = "hermes/command-allowlist/migration-v1.json"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ManifestError(Exception):
    """Manifest validation failure."""


class RemoteTreeError(Exception):
    """Remote tree contains protected paths."""


class SyncError(Exception):
    """Sync operation failure."""


# ---------------------------------------------------------------------------
# Logging (stderr only — stdout is reserved for JSON tool mode)
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[workspace-sync] {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"[workspace-sync] WARNING: {msg}", file=sys.stderr)


def _error(msg: str) -> None:
    print(f"[workspace-sync] ERROR: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Workspace lock — O_NOFOLLOW, lstat .locks, fstat verify, fchmod 0600
# ---------------------------------------------------------------------------


class WorkspaceLock:
    """fcntl-based exclusive lock for all canonical workspace operations.

    Security:
    - The .locks directory is lstat-checked to reject symlinks.
    - The lock file is opened with O_NOFOLLOW (where available) to
      prevent final-component symlink attacks.
    - On ELOOP (symlink on final component), raises SyncError — never
      retries without O_NOFOLLOW.
    - fstat-verifies the fd is a regular file before flock.
    - fchmods the lock file to 0600 before flock.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def acquire(self) -> None:
        """Acquire the workspace lock.

        All OS failures (mkdir, lstat, open, fstat, fchmod, flock) are
        wrapped as SyncError. Any partially opened file descriptor is
        closed before raising. Explicit symlink/non-regular errors
        are preserved with clear messages.
        """
        # lstat the .locks directory to reject symlinks.
        try:
            dir_st = os.lstat(str(LOCK_DIR))
        except FileNotFoundError:
            try:
                LOCK_DIR.mkdir(parents=True, exist_ok=True)
                dir_st = os.lstat(str(LOCK_DIR))
            except OSError as exc:
                raise SyncError(f"Cannot create lock directory {LOCK_DIR}: {exc}") from exc
        except OSError as exc:
            raise SyncError(f"Cannot stat lock directory {LOCK_DIR}: {exc}") from exc
        if not stat.S_ISDIR(dir_st.st_mode):
            raise SyncError(f"Lock directory {LOCK_DIR} is not a directory (symlink?)")

        # Open the lock file with O_NOFOLLOW where available.
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self._fd = os.open(str(LOCK_FILE), flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                # Final component is a symlink — never retry without O_NOFOLLOW.
                raise SyncError(f"Lock file {LOCK_FILE} is a symlink (refusing to follow)") from exc
            raise SyncError(f"Cannot open lock file {LOCK_FILE}: {exc}") from exc

        # fstat verify: must be a regular file.
        try:
            st = os.fstat(self._fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(self._fd)
                self._fd = None
                raise SyncError(f"Lock file {LOCK_FILE} is not a regular file")
            # fchmod to 0600 before flock.
            os.fchmod(self._fd, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError as exc:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise SyncError(f"Lock file {LOCK_FILE} verification/lock failed: {exc}") from exc

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "WorkspaceLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command, returning the CompletedProcess."""
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=run_env,
        check=False,
    )
    if check and result.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _git_output(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None,
                check: bool = True) -> str:
    return _git(*args, cwd=cwd, env=env, check=check).stdout.strip()


def _rev_parse(ref: str, cwd: Path | None = None, check: bool = False) -> str:
    """Safely resolve a git ref. Returns empty string on failure when check=False.

    Uses --verify to ensure the ref exists; without --verify, git rev-parse
    returns the literal ref string for nonexistent refs.
    """
    proc = _git("rev-parse", "--verify", ref, cwd=cwd or WORKSPACE_DIR, check=check)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _configure_git() -> None:
    _git("config", "user.email", GIT_EMAIL, cwd=WORKSPACE_DIR)
    _git("config", "user.name", GIT_NAME, cwd=WORKSPACE_DIR)


def _get_clean_remote_url() -> str:
    proc = _git("remote", "get-url", "origin", cwd=WORKSPACE_DIR, check=False)
    url = proc.stdout.strip()
    if not url:
        return ""
    return _sanitize_url(url)


def _sanitize_url(url: str) -> str:
    """Remove embedded userinfo from HTTP(S) URLs; preserve SSH and local paths."""
    if not url.startswith("http://") and not url.startswith("https://"):
        return url
    parsed = urlparse(url)
    clean_netloc = parsed.hostname or ""
    if parsed.port:
        clean_netloc += f":{parsed.port}"
    return urlunparse((
        parsed.scheme, clean_netloc, parsed.path,
        parsed.params, parsed.query, parsed.fragment,
    ))


def _sanitize_origin() -> None:
    proc = _git("remote", "get-url", "origin", cwd=WORKSPACE_DIR, check=False)
    url = proc.stdout.strip()
    if not url:
        return
    clean = _sanitize_url(url)
    if clean != url:
        _git("remote", "set-url", "origin", clean, cwd=WORKSPACE_DIR)


def _is_https(url: str) -> bool:
    return url.startswith("https://")


# ---------------------------------------------------------------------------
# Ephemeral auth — token-free askpass helper, operation-specific URL
# ---------------------------------------------------------------------------


_ASKPASS_HELPER = """#!/bin/sh
# GIT_ASKPASS helper — reads WORKSPACE_REPO_TOKEN from environment.
# Returns a fixed username for username prompts, token only for password prompts.
# This file contains NO token literal.
# Uses printf (not echo) to avoid interpretation of backslash sequences.
prompt="$1"
case "$prompt" in
    *Username*)
        printf '%s\\n' "x-access-token"
        ;;
    *Password*)
        printf '%s\\n' "$WORKSPACE_REPO_TOKEN"
        ;;
    *)
        printf '%s\\n' "$WORKSPACE_REPO_TOKEN"
        ;;
esac
"""


def _make_git_env(operation_url: str = "") -> dict[str, str]:
    """Build ephemeral auth env for HTTPS git operations.

    Uses a static token-free GIT_ASKPASS helper that reads
    WORKSPACE_REPO_TOKEN from the environment at call time. The helper
    script contains no token literal. GIT_TERMINAL_PROMPT=0 prevents
    interactive prompts.

    The ``operation_url`` parameter is the actual URL for this operation
    (clean clone URL for clone, sanitized origin URL for fetch/push).
    Auth is only configured when the operation URL is HTTPS and a
    token is present.
    """
    env: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0"}
    url = operation_url or REPO_URL
    if REPO_TOKEN and _is_https(url):
        askpass = tempfile.NamedTemporaryFile(
            mode="w", suffix="_askpass.sh", delete=False, prefix="ws_askpass_"
        )
        askpass.write(_ASKPASS_HELPER)
        askpass.close()
        os.chmod(askpass.name, 0o700)
        env["GIT_ASKPASS"] = askpass.name
    return env


def _cleanup_git_env(env: dict[str, str]) -> None:
    askpass = env.get("GIT_ASKPASS")
    if askpass and askpass.startswith(tempfile.gettempdir()):
        try:
            os.unlink(askpass)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Authenticated fetch — centralized
# ---------------------------------------------------------------------------


def _authenticated_fetch(ref: str = BRANCH) -> subprocess.CompletedProcess[str]:
    """Perform an authenticated fetch of origin/<ref>.

    Returns the CompletedProcess. Does NOT raise on failure — caller
    checks returncode.
    """
    origin_url = _get_clean_remote_url()
    git_env = _make_git_env(origin_url)
    try:
        return _git("fetch", "origin", ref, cwd=WORKSPACE_DIR, env=git_env, check=False)
    finally:
        _cleanup_git_env(git_env)


# ---------------------------------------------------------------------------
# Checked push — centralized, returns nonzero on failure
# ---------------------------------------------------------------------------


def _checked_push(refspec: str) -> subprocess.CompletedProcess[str]:
    """Perform an authenticated push, returning the CompletedProcess.

    Does NOT raise on failure — caller checks returncode.

    Push gate (issue #151 / plan v2): validates HEAD-committed
    command-allowlist sidecars BEFORE the push, plus the migration
    marker (content when present, and the monotonic gate: a HEAD that
    removes a marker previously contained in reachable history must not
    push). Every push path in this module goes through this helper, so
    committed allowlist state is always gated at the source — including
    commits made by other paths (manual ``git commit``, lifecycle
    auto-commit). Fail-closed: raises SyncError (nonzero) when HEAD
    carries an invalid sidecar/marker or removes the marker; absence of
    sidecars is valid and a markerless history is valid.
    """
    _validate_head_command_allowlist_state()
    _validate_head_command_allowlist_marker()
    _sanitize_origin()
    origin_url = _get_clean_remote_url()
    git_env = _make_git_env(origin_url)
    try:
        return _git("push", "origin", refspec, cwd=WORKSPACE_DIR, env=git_env, check=False)
    finally:
        _cleanup_git_env(git_env)


# ---------------------------------------------------------------------------
# Safe merge — centralized, abort on failure
# ---------------------------------------------------------------------------


def _safe_merge(remote_ref: str, merge_msg: str = "Merge remote with conflict resolution") -> None:
    """Attempt a remote-wins merge. On any failure: abort and raise SyncError.

    Uses ``git merge --no-edit -X theirs``. If the merge fails, aborts
    the in-progress merge and raises SyncError with diagnostics. Never
    stages/commits blindly or reports success with unmerged paths.
    """
    merge_proc = _git(
        "merge", "--no-edit", "-X", "theirs", remote_ref,
        "-m", merge_msg, cwd=WORKSPACE_DIR, check=False,
    )
    if merge_proc.returncode != 0:
        # Abort the in-progress merge if active.
        _git("merge", "--abort", cwd=WORKSPACE_DIR, check=False)
        # Verify no unmerged paths remain.
        diff_proc = _git("diff", "--name-only", "--diff-filter=U", cwd=WORKSPACE_DIR, check=False)
        unmerged = diff_proc.stdout.strip()
        if unmerged:
            raise SyncError(
                f"Merge of {remote_ref} failed and abort left unmerged paths: {unmerged}"
            )
        raise SyncError(f"Merge of {remote_ref} failed (aborted): {merge_proc.stderr.strip()}")


# ---------------------------------------------------------------------------
# Manifest management
# ---------------------------------------------------------------------------


def _normalize_candidate(entry: str) -> str:
    """POSIX-normalize a manifest candidate, stripping leading ./ and collapsing.

    Returns empty string for root-equivalent entries (``.``, ``./``, ``././``,
    repeated slashes, empty components).
    """
    candidate = entry.strip()
    while candidate.startswith("./"):
        candidate = candidate[2:]
    parts: list[str] = []
    for part in candidate.split("/"):
        if part == "" or part == ".":
            continue
        parts.append(part)
    return "/".join(parts)


def _read_manifest() -> list[str]:
    if not MANIFEST_PATH.exists():
        return []
    lines = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _manifest_contains(path: str) -> bool:
    if not MANIFEST_PATH.exists():
        return False
    return path in _read_manifest()


def _append_manifest(path: str) -> None:
    if _manifest_contains(path):
        return
    with MANIFEST_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"{path}\n")
    _log(f"Registered user-owned skill path in .sync-manifest: {path}")


def _ensure_skills_gitignore() -> None:
    gitignore = WORKSPACE_DIR / ".gitignore"
    if not gitignore.exists():
        return
    content = gitignore.read_text(encoding="utf-8")
    if "!skills/**" in content:
        return
    with gitignore.open("a", encoding="utf-8") as fh:
        fh.write("\n# Allow explicit user-owned skill files to be tracked via .sync-manifest.\n!skills/**\n")


def _register_user_skill_files() -> None:
    skills_dir = WORKSPACE_DIR / "skills"
    if not skills_dir.is_dir():
        return
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        if not (skill_dir / "SKILL.md").exists():
            continue
        _ensure_skills_gitignore()
        for f in sorted(skill_dir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(WORKSPACE_DIR).as_posix()
                _append_manifest(rel)


def _is_unsafe_pathspec(candidate: str) -> bool:
    """Check for absolute paths, path traversal, empty/root-equivalent, and colon prefixes."""
    if not candidate:
        return True
    if candidate.startswith("/"):
        return True
    if candidate.startswith(".."):
        return True
    if "/../" in candidate or candidate.endswith("/.."):
        return True
    if candidate == "..":
        return True
    if re.match(r"^[a-zA-Z]:", candidate):
        return True
    if candidate.startswith(":"):
        return True
    return False


def _is_protected_path(candidate: str) -> bool:
    if candidate == ALLOWED_SCHEMA_PACK:
        return False
    for form in BARE_GBRAIN_FORMS:
        if candidate == form:
            return True
        if form.endswith("*") and fnmatch.fnmatch(candidate, form):
            return True
    for protected in PROTECTED_RUNTIME_PATHS:
        if candidate == protected or candidate.startswith(protected + "/"):
            return True
    return False


def _is_skill_wildcard(candidate: str) -> bool:
    if not candidate.startswith("skills/"):
        return False
    return any(c in candidate for c in "*?[]")


def _is_allowed_wildcard(candidate: str) -> bool:
    return candidate in ALLOWED_WILDCARD_PATHSPECS


def _has_glob_chars(path: str) -> bool:
    return any(c in path for c in "*?[]")


def _is_ignored(path: str) -> bool:
    if _has_glob_chars(path):
        return False
    proc = _git("check-ignore", "-q", "--", path, cwd=WORKSPACE_DIR, check=False)
    return proc.returncode == 0


def _validate_manifest() -> list[str]:
    """Validate all manifest entries and return the validated normalized list.

    Raises ManifestError on any violation. The returned list contains
    only the normalized candidates that passed validation — callers must
    use this list for staging, never reread the raw manifest.
    """
    validated: list[str] = []
    for entry in _read_manifest():
        candidate = _normalize_candidate(entry)
        if _is_unsafe_pathspec(entry) or _is_unsafe_pathspec(candidate):
            raise ManifestError(f".sync-manifest contains unsafe pathspec: {entry}")
        if _is_protected_path(candidate):
            raise ManifestError(f".sync-manifest includes protected runtime path: {candidate}")
        if _is_skill_wildcard(candidate):
            raise ManifestError(f".sync-manifest must use explicit skills paths: {candidate}")
        if _has_glob_chars(candidate) and not _is_allowed_wildcard(candidate):
            raise ManifestError(f".sync-manifest contains disallowed wildcard pathspec: {candidate}")
        if _is_ignored(candidate):
            raise ManifestError(f".sync-manifest path is ignored by .gitignore: {candidate}")
        validated.append(candidate)
    return validated


def _validate_manifest_if_present() -> list[str]:
    """Validate manifest if present, returning the validated list (empty if no manifest)."""
    if MANIFEST_PATH.exists():
        return _validate_manifest()
    return []


# ---------------------------------------------------------------------------
# models.yaml validation — reuse canonical helper (no duplicated rules)
# ---------------------------------------------------------------------------


def _load_skill_state_module():
    """Import the canonical ``josemar_skill_state`` helper module.

    Returns the module, or ``None`` if the helper is unavailable. The
    helper path is resolved lazily so the import only happens when
    validated state is actually present.
    """
    helper = os.environ.get("JOSEMAR_SKILL_STATE", JOSEMAR_SKILL_STATE_PATH)
    if not helper or not os.path.exists(helper):
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location("josemar_skill_state", helper)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_models_validator():
    """Import the canonical models.yaml validator from the helper.

    Returns ``validate_models_state_from_text`` from
    ``josemar_skill_state``, or ``None`` if the helper is unavailable.
    Callers MUST treat ``None`` as fail-closed when a local or remote
    ``hermes/models.yaml`` exists (validation cannot be skipped for a
    present file). Absence of models.yaml remains valid regardless.
    """
    module = _load_skill_state_module()
    if module is None:
        return None
    return getattr(module, "validate_models_state_from_text", None)


def _load_command_allowlist_validator():
    """Import the canonical command-allowlist text validator from the helper.

    Returns ``validate_command_allowlist_state_from_text`` from
    ``josemar_skill_state``, or ``None`` if the helper is unavailable or
    does not provide the validator. Callers MUST treat ``None`` as
    fail-closed when any command-allowlist sidecar is present (local
    working copy, committed HEAD, or remote candidate): validation cannot
    be skipped for present state. Absence of every sidecar (including
    after deletion) remains valid regardless.
    """
    module = _load_skill_state_module()
    if module is None:
        return None
    return getattr(module, "validate_command_allowlist_state_from_text", None)


def _load_command_allowlist_marker_validator():
    """Import the canonical migration-marker text validator from the helper.

    Documented expected W1 public API (plan v2):
    ``validate_command_allowlist_migration_marker_from_text(text)`` parses
    and validates the exact marker schema
    ``{"version":1,"legacy_runtime_import_complete":true}`` and raises
    ``ValueError`` with structural, value-free messages on any malformed
    input. Returns ``None`` if the helper is unavailable or does not
    provide the validator yet. Callers MUST treat ``None`` as fail-closed
    when the marker is present (INTEGRATION DEPENDENCY: until W1 lands
    this validator, any state carrying the marker fails sync closed).
    Marker absence remains valid regardless.
    """
    module = _load_skill_state_module()
    if module is None:
        return None
    return getattr(
        module, "validate_command_allowlist_migration_marker_from_text", None
    )


def _validate_models_yaml_text(text: str) -> None:
    """Validate models.yaml content using the canonical helper.

    Raises ``SyncError`` on any validation failure (malformed YAML, schema
    violation, secret keys, forbidden fields). An empty document (YAML
    null) is valid (rollback semantics). Fail-closed: when the helper is
    unavailable, validation MUST NOT be skipped — a present models.yaml
    that cannot be validated is an error (nonzero, working/runtime config
    unchanged). Absence of models.yaml is the only valid skip condition
    and is handled by callers before invoking this function.
    """
    validator = _load_models_validator()
    if validator is None:
        # Fail-closed: a present models.yaml that cannot be validated
        # (helper unavailable) is an error, not a skip.
        raise SyncError(
            "models.yaml validation required but josemar_skill_state helper "
            "is unavailable (JOSEMAR_SKILL_STATE path missing or unreadable)"
        )
    try:
        validator(text)
    except ValueError as exc:
        raise SyncError(f"models.yaml validation failed: {exc}") from exc


def _validate_local_models_yaml() -> None:
    """Validate the local working-copy models.yaml before staging/commit.

    Fail-closed: invalid/malformed/secret model state must make sync fail
    nonzero and leave the working/runtime config unchanged. Skipped when
    models.yaml is absent (rollback — no state to validate). When the
    helper is unavailable but models.yaml is present, raises SyncError
    (fail-closed — validation cannot be skipped for a present file).
    """
    if not MODELS_YAML_PATH.exists():
        return
    _validate_models_yaml_text(MODELS_YAML_PATH.read_text(encoding="utf-8"))


def _validate_head_models_yaml() -> None:
    """Validate the HEAD-committed models.yaml before pushing.

    Covers commits made by other paths (e.g. manual ``git commit``) so
    invalid/malformed/secret model state never reaches the remote via a
    pure push. Reads the HEAD file content via ``git show`` and validates
    it with the canonical helper. Fail-closed: raises SyncError (nonzero)
    if HEAD carries an invalid models.yaml or if a present models.yaml
    cannot be validated. Skipped when HEAD does not carry models.yaml.
    """
    proc = _git("show", f"HEAD:{MODELS_YAML_REL}", cwd=WORKSPACE_DIR, check=False)
    if proc.returncode != 0:
        # HEAD does not carry models.yaml — acceptable.
        return
    _validate_models_yaml_text(proc.stdout)


def _validate_remote_models_yaml(remote_ref: str) -> None:
    """Validate the candidate remote models.yaml before accepting/merging.

    Reads the remote file content via ``git show`` and validates it with
    the canonical helper. Fail-closed: invalid remote model state must
    make sync fail nonzero and leave the working/runtime config unchanged.
    Skipped when the remote does not carry models.yaml (rollback — absent
    remote means restore template defaults on merge).
    """
    proc = _git("show", f"{remote_ref}:{MODELS_YAML_REL}", cwd=WORKSPACE_DIR, check=False)
    if proc.returncode != 0:
        # Remote does not carry models.yaml — acceptable (rollback).
        return
    _validate_models_yaml_text(proc.stdout)


# ---------------------------------------------------------------------------
# command-allowlist sidecar validation — reuse canonical helper (issue #151)
# ---------------------------------------------------------------------------


def _profiles_json_relpath_stem(rel: str) -> str | None:
    """Return the raw stem if ``rel`` is ``<profiles dir>/<stem>.json``.

    Returns ``None`` for anything else. The returned stem may be empty or
    noncanonical — callers must still apply the canonical profile
    contract; a stem containing ``/`` (deeper paths) is noncanonical by
    definition.
    """
    prefix = ALLOWLIST_PROFILES_REL_DIR + "/"
    if not rel.startswith(prefix) or not rel.endswith(".json"):
        return None
    return rel[len(prefix):-len(".json")]


def _is_command_allowlist_family_relpath(rel: str) -> bool:
    """True for the exact default sidecar or any ``profiles/*.json`` path."""
    if rel == ALLOWLIST_DEFAULT_REL:
        return True
    return _profiles_json_relpath_stem(rel) is not None


def _require_canonical_profile_id_pattern():
    """Return the canonical profile-ID pattern from the W1 helper.

    Single source of truth: the exact ``_PROFILE_ID_RE`` contract the
    helper enforces for profile sidecar paths. Raises ``SyncError`` when
    the helper (or the pattern) is unavailable — fail-closed, never a
    skip.
    """
    module = _load_skill_state_module()
    pattern = getattr(module, "_PROFILE_ID_RE", None) if module is not None else None
    if pattern is None:
        raise SyncError(
            "command-allowlist sidecar validation required but the "
            "josemar_skill_state helper is unavailable (JOSEMAR_SKILL_STATE "
            "path missing, unreadable, or lacking the canonical validator)"
        )
    return pattern


def _canonical_command_allowlist_relpaths(rels: list[str], pattern) -> list[str]:
    """Value-free name gate over family relpaths.

    Every family path must be the exact ``default.json`` shape or
    ``profiles/<canonical>.json`` (single-level stem matching the
    canonical profile contract). Noncanonical filenames are rejected
    value-free — before any content is read or echoed. Returns the
    canonical sidecar relpaths.
    """
    canonical: list[str] = []
    for rel in rels:
        if rel == ALLOWLIST_DEFAULT_REL:
            canonical.append(rel)
            continue
        stem = _profiles_json_relpath_stem(rel)
        if stem and pattern.match(stem):
            canonical.append(rel)
            continue
        raise SyncError(
            "noncanonical command-allowlist profile sidecar filename "
            f"rejected (must match the canonical profile contract): {rel}"
        )
    return canonical


def _validate_command_allowlist_entries(entries: list[tuple[str, str]]) -> None:
    """Validate enumerated ``(relpath, text)`` allowlist sidecars fail-closed.

    Every enumerated sidecar must fully validate (canonical helper, strict
    v1 schema) before any staging, push, or remote merge proceeds. When
    any sidecar is present, a missing/unusable helper is an error, never
    a skip. An empty entry list (absence/deletion) is valid without
    consulting the helper. The canonical validator's error messages are
    structural only and never include allowlist contents; the labels here
    are sidecar paths only.
    """
    if not entries:
        return
    validator = _load_command_allowlist_validator()
    if validator is None:
        raise SyncError(
            "command-allowlist sidecar validation required but the "
            "josemar_skill_state helper is unavailable (JOSEMAR_SKILL_STATE "
            "path missing, unreadable, or lacking the canonical validator)"
        )
    for rel, text in entries:
        try:
            validator(text)
        except ValueError as exc:
            raise SyncError(
                f"command-allowlist sidecar validation failed for {rel}: {exc}"
            ) from exc


def _local_command_allowlist_entries() -> list[tuple[str, str]]:
    """Enumerate present local allowlist sidecars as ``(relpath, text)``.

    Name gate first: every ``profiles/*.json`` present in the working
    copy must carry a canonical profile stem (value-free rejection before
    any content is read). Reads the working copy directly (not the
    index/HEAD) so every present sidecar is validated BEFORE it can be
    staged or committed.
    """
    entries: list[tuple[str, str]] = []
    default_path = WORKSPACE_DIR / ALLOWLIST_DEFAULT_REL
    if default_path.is_file():
        entries.append(
            (ALLOWLIST_DEFAULT_REL, default_path.read_text(encoding="utf-8"))
        )
    profiles_dir = WORKSPACE_DIR / ALLOWLIST_PROFILES_REL_DIR
    if profiles_dir.is_dir():
        json_children = [
            path for path in sorted(profiles_dir.glob("*.json")) if path.is_file()
        ]
        if json_children:
            pattern = _require_canonical_profile_id_pattern()
            rels = [
                path.relative_to(WORKSPACE_DIR).as_posix()
                for path in json_children
            ]
            _canonical_command_allowlist_relpaths(rels, pattern)
            for rel, path in zip(rels, json_children):
                entries.append((rel, path.read_text(encoding="utf-8")))
    return entries


def _validate_local_command_allowlist_state() -> None:
    """Validate every present local allowlist sidecar before staging/commit.

    Name gate first (value-free noncanonical filename rejection), then
    full content validation via the canonical helper. Fail-closed: any
    violation raises SyncError (nonzero) with nothing staged and the
    working copy unchanged. Absence of every sidecar (including after
    deletion) is valid and needs no helper.
    """
    _validate_command_allowlist_entries(_local_command_allowlist_entries())


def _head_command_allowlist_relpaths() -> list[str]:
    """List command-allowlist family paths carried by ``HEAD`` (empty if none)."""
    proc = _git(
        "ls-tree", "-r", "--name-only", "HEAD", "--", ALLOWLIST_SIDECAR_REL_DIR,
        cwd=WORKSPACE_DIR, check=False,
    )
    if proc.returncode != 0:
        # No resolvable HEAD tree — nothing committed to validate.
        return []
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if _is_command_allowlist_family_relpath(line.strip())
    ]


def _validate_head_command_allowlist_state() -> None:
    """Validate HEAD-committed allowlist sidecars before every push.

    Covers commits made by other paths (manual ``git commit``, lifecycle
    auto-commit): invalid or malformed committed sidecar state must never
    reach the remote via a pure push. Name gate first: noncanonical
    profile sidecar filenames are rejected value-free. Then every
    canonical sidecar's content is validated with the canonical helper.
    Fail-closed: raises SyncError when HEAD carries a noncanonical
    filename, an invalid sidecar, or a present sidecar that cannot be
    read or validated. HEAD absence is valid (deletion/rollback).
    """
    rels = _head_command_allowlist_relpaths()
    if not rels:
        return
    pattern = _require_canonical_profile_id_pattern()
    canonical = _canonical_command_allowlist_relpaths(rels, pattern)
    entries: list[tuple[str, str]] = []
    for rel in canonical:
        proc = _git("show", f"HEAD:{rel}", cwd=WORKSPACE_DIR, check=False)
        if proc.returncode != 0:
            raise SyncError(
                f"cannot read committed command-allowlist sidecar {rel} from HEAD"
            )
        entries.append((rel, proc.stdout))
    _validate_command_allowlist_entries(entries)


def _validate_remote_command_allowlist_state(
    remote_ref: str, remote_paths: list[str]
) -> None:
    """Validate the candidate remote allowlist sidecars before merge/acceptance.

    ``remote_paths`` is the already-listed remote tree from
    ``_assert_remote_tree_safe``. Name gate first: noncanonical profile
    sidecar filenames are rejected value-free. Then every canonical
    sidecar's content is validated with the canonical helper. Fail-closed:
    invalid or malformed remote sidecar state must make sync fail nonzero
    before the merge, leaving the working/runtime config unchanged.
    Remote absence is valid (deletion/rollback merges cleanly).
    """
    rels = [p for p in remote_paths if _is_command_allowlist_family_relpath(p)]
    if not rels:
        return
    pattern = _require_canonical_profile_id_pattern()
    canonical = _canonical_command_allowlist_relpaths(rels, pattern)
    entries: list[tuple[str, str]] = []
    for rel in canonical:
        proc = _git("show", f"{remote_ref}:{rel}", cwd=WORKSPACE_DIR, check=False)
        if proc.returncode != 0:
            raise SyncError(
                f"cannot read remote command-allowlist sidecar {rel} "
                f"from {remote_ref}"
            )
        entries.append((rel, proc.stdout))
    _validate_command_allowlist_entries(entries)


# ---------------------------------------------------------------------------
# command-allowlist migration marker — value-free monotonic state (plan v2)
# ---------------------------------------------------------------------------


def _validate_command_allowlist_marker_text(rel: str, text: str) -> None:
    """Validate marker content with the canonical helper marker validator.

    Fail-closed: when the marker is present, a missing/unusable helper is
    an error, never a skip. Errors are structural (path + shape) and
    never echo marker or allowlist values.
    """
    validator = _load_command_allowlist_marker_validator()
    if validator is None:
        raise SyncError(
            "command-allowlist migration marker validation required but the "
            "josemar_skill_state helper is unavailable (JOSEMAR_SKILL_STATE "
            "path missing, unreadable, or lacking the canonical marker "
            "validator)"
        )
    try:
        validator(text)
    except ValueError as exc:
        raise SyncError(
            f"command-allowlist migration marker validation failed for {rel}: "
            f"{exc}"
        ) from exc


def _history_contains_path(rev: str, rel: str) -> bool:
    """True if any commit reachable from ``rev`` touches ``rel``.

    Used for the marker's history-aware monotonicity gate only. Fail-closed:
    an unresolvable/failing history query raises SyncError rather than
    reporting "no history".
    """
    proc = _git(
        "rev-list", "-n", "1", rev, "--", rel, cwd=WORKSPACE_DIR, check=False
    )
    if proc.returncode != 0:
        raise SyncError(
            f"cannot inspect history of {rev} for {rel}: {proc.stderr.strip()}"
        )
    return bool(proc.stdout.strip())


def _head_has_path(rel: str) -> bool | None:
    """Return True/False when ``HEAD`` carries ``rel``, None if no HEAD.

    ``None`` (fresh workspace, no commits yet) means the marker gates are
    trivially satisfied — there is no committed state to lose.
    """
    if not _rev_parse("HEAD"):
        return None
    proc = _git(
        "cat-file", "-e", f"HEAD:{rel}", cwd=WORKSPACE_DIR, check=False
    )
    return proc.returncode == 0


def _validate_local_command_allowlist_marker() -> None:
    """Validate the local migration marker before staging/commit.

    Monotonicity (plan v2): a working-copy deletion is rejected iff
    committed HEAD contains the marker. First-time addition (marker
    present, HEAD without it) and unchanged presence are valid; present
    content is always validated with the canonical helper (fail-closed).
    Marker absence with no committed marker is valid and needs no helper.
    """
    marker_path = WORKSPACE_DIR / ALLOWLIST_MARKER_REL
    local_has = marker_path.is_file()
    head_has = _head_has_path(ALLOWLIST_MARKER_REL)
    if head_has is None:
        # No HEAD yet: nothing committed to delete; only content applies.
        head_has = False
    if head_has and not local_has:
        raise SyncError(
            "command-allowlist migration marker deletion rejected: the marker "
            f"is committed at HEAD and must remain present ({ALLOWLIST_MARKER_REL})"
        )
    if local_has:
        _validate_command_allowlist_marker_text(
            ALLOWLIST_MARKER_REL, marker_path.read_text(encoding="utf-8")
        )


def _validate_head_command_allowlist_marker() -> None:
    """Validate the committed marker before every push.

    Content gate: a marker carried by HEAD is validated with the
    canonical helper (covers commits made by other paths). Monotonicity
    gate: a HEAD that no longer carries the marker is invalid when the
    reachable state-repo history previously contained it (manually
    committed deletion) and must not push. A history that never contained
    the marker is valid (first upgrade).
    """
    head_has = _head_has_path(ALLOWLIST_MARKER_REL)
    if head_has is None:
        return
    if head_has:
        proc = _git(
            "show", f"HEAD:{ALLOWLIST_MARKER_REL}", cwd=WORKSPACE_DIR, check=False
        )
        if proc.returncode != 0:
            raise SyncError(
                f"cannot read committed command-allowlist migration marker "
                f"{ALLOWLIST_MARKER_REL} from HEAD"
            )
        _validate_command_allowlist_marker_text(ALLOWLIST_MARKER_REL, proc.stdout)
        return
    if _history_contains_path("HEAD", ALLOWLIST_MARKER_REL):
        raise SyncError(
            "command-allowlist migration marker deletion rejected: reachable "
            f"history contains {ALLOWLIST_MARKER_REL}; a commit that removes "
            "it must not be pushed"
        )


def _validate_remote_command_allowlist_marker(
    remote_ref: str, remote_paths: list[str]
) -> None:
    """Validate the candidate remote marker before merge/acceptance.

    Content gate: a marker on the remote candidate is validated with the
    canonical helper. Monotonicity gate: a remote candidate that no longer
    carries the marker is rejected when the remote history previously
    contained it. A remote history that never contained the marker is
    valid (first upgrade/old remote) — normal merge semantics then retain
    a local first-time marker addition; no remote-wins deletion is
    implemented. Remote absence with markerless history is valid.
    """
    remote_has = ALLOWLIST_MARKER_REL in remote_paths
    if remote_has:
        proc = _git(
            "show", f"{remote_ref}:{ALLOWLIST_MARKER_REL}",
            cwd=WORKSPACE_DIR, check=False,
        )
        if proc.returncode != 0:
            raise SyncError(
                f"cannot read remote command-allowlist migration marker "
                f"{ALLOWLIST_MARKER_REL} from {remote_ref}"
            )
        _validate_command_allowlist_marker_text(ALLOWLIST_MARKER_REL, proc.stdout)
        return
    if _history_contains_path(remote_ref, ALLOWLIST_MARKER_REL):
        raise SyncError(
            "command-allowlist migration marker deletion rejected: remote "
            f"history contains {ALLOWLIST_MARKER_REL}; a remote candidate "
            "that removes it must not be merged"
        )


def _stage_manifest_files() -> None:
    if not MANIFEST_PATH.exists():
        _warn("No .sync-manifest found, skipping selective staging")
        return
    # Validate the local models.yaml before staging so invalid/malformed/
    # secret model state never reaches the remote. Fail-closed: raises
    # SyncError (nonzero) and leaves the working copy unchanged.
    _validate_local_models_yaml()
    # Validate every present command-allowlist sidecar before staging so
    # invalid/malformed allowlist state never reaches the remote (issue
    # #151). Fail-closed: raises SyncError (nonzero) and leaves the
    # working copy unchanged with nothing staged. Absence/deletion is
    # valid and requires no helper.
    _validate_local_command_allowlist_state()
    # Validate the command-allowlist migration marker (plan v2): content
    # when present, plus the monotonic working-copy deletion gate
    # (deletion rejected iff HEAD contains the marker). Fail-closed:
    # raises SyncError (nonzero) with nothing staged. First-time addition
    # and marker absence are valid.
    _validate_local_command_allowlist_marker()
    _register_user_skill_files()
    validated = _validate_manifest()
    _git("add", "-A", "--", ".gitignore", ".sync-manifest", cwd=WORKSPACE_DIR, check=False)
    for candidate in validated:
        _git("add", "-A", "--", candidate, cwd=WORKSPACE_DIR, check=False)


def _has_staged_changes() -> bool:
    proc = _git("diff", "--cached", "--quiet", cwd=WORKSPACE_DIR, check=False)
    return proc.returncode != 0


def _commit_changes(message: str) -> bool:
    _stage_manifest_files()
    if not _has_staged_changes():
        _log("No changes to commit")
        return False
    _git("commit", "-m", message, cwd=WORKSPACE_DIR)
    return True


# ---------------------------------------------------------------------------
# Remote tree validation — one-pass, exact policy, fail-closed
# ---------------------------------------------------------------------------


def _assert_remote_tree_safe(remote_ref: str) -> None:
    """Validate that a remote ref does not track protected paths.

    Lists the remote tree once and applies the exact policy:
    - Under .gbrain/, ONLY the exact ALLOWED_SCHEMA_PACK is permitted.
    - Reject lookalikes (pack.yaml.evil), alternate packs, runtime
      files/descendants, and broad roots.
    - Reject all other protected runtime entries and their descendants.

    Also validates the candidate remote ``hermes/models.yaml`` content
    using the canonical helper so invalid/malformed/secret model state is
    rejected before merge (fail-closed: nonzero, working/runtime config
    unchanged), the candidate remote command-allowlist sidecars
    (issue #151) so invalid/malformed/noncanonically named allowlist
    state is rejected before merge/acceptance, and the candidate remote
    migration marker (plan v2: content plus monotonic remote-deletion
    gate); remote absence (deletion) is valid for sidecars, and a
    markerless remote history is valid for the marker.

    Fail-closed: if ``git ls-tree`` fails for a ref that is expected to
    exist after a successful fetch, raises SyncError (not RemoteTreeError)
    rather than returning as safe.
    """
    proc = _git("ls-tree", "-r", "--name-only", remote_ref, cwd=WORKSPACE_DIR, check=False)
    if proc.returncode != 0:
        # Fail-closed: if ls-tree fails for a ref expected after fetch,
        # this is an error, not "safe".
        raise SyncError(f"Failed to list remote tree for {remote_ref}: {proc.stderr.strip()}")
    paths = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    for path in paths:
        for protected in PROTECTED_RUNTIME_PATHS:
            if path == protected or path.startswith(protected + "/"):
                raise RemoteTreeError(f"State repo tracks protected runtime path: {path}")
        if path.startswith(".gbrain/"):
            if path != ALLOWED_SCHEMA_PACK:
                raise RemoteTreeError(f"State repo tracks protected runtime path: {path}")
        if path == ".gbrain":
            raise RemoteTreeError("State repo tracks protected runtime path: .gbrain")
    # Validate the candidate remote models.yaml before accepting/merging.
    _validate_remote_models_yaml(remote_ref)
    # Validate the candidate remote command-allowlist sidecars before
    # accepting/merging (issue #151). Fail-closed: invalid or malformed
    # remote allowlist state is rejected before the merge; remote absence
    # (deletion) is valid.
    _validate_remote_command_allowlist_state(remote_ref, paths)
    # Validate the candidate remote migration marker (plan v2): content
    # when present, plus the monotonic gate — a remote candidate that
    # removes a marker previously contained in the remote history must
    # not merge. A remote history that never contained the marker is
    # valid; the merge then retains a local first-time marker.
    _validate_remote_command_allowlist_marker(remote_ref, paths)


# ---------------------------------------------------------------------------
# Tool mode (JSON stdin/stdout)
# ---------------------------------------------------------------------------


def _parse_slash_command(command_text: str, command_name: str) -> str:
    raw = command_text.strip()
    name = command_name.strip().lower()
    if raw.startswith("/") and name:
        raw_lower = raw.lower()
        prefix = f"/{name}"
        if raw_lower.startswith(prefix):
            remainder = raw[len(prefix):]
            if not remainder:
                raw = ""
            elif remainder.startswith(":"):
                raw = remainder[1:].strip()
            elif remainder[0].isspace():
                raw = remainder.strip()
    return raw


def _emit_json(doc: dict[str, Any]) -> None:
    json.dump(doc, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _emit_error(error: str, **extra: Any) -> None:
    doc = {"success": False, "error": error}
    doc.update(extra)
    _emit_json(doc)


def _do_status(payload: dict[str, Any]) -> None:
    if MANIFEST_PATH.exists():
        _register_user_skill_files()
        _validate_manifest()
    tracked = _read_manifest()
    status_proc = _git("status", "--short", cwd=WORKSPACE_DIR, check=False)
    branch_proc = _git("branch", "--show-current", cwd=WORKSPACE_DIR, check=False)
    remote_url = _get_clean_remote_url()
    _emit_json({
        "success": True,
        "action": "status",
        "branch": branch_proc.stdout.strip() or "unknown",
        "remote": remote_url,
        "auth_configured": bool(REPO_TOKEN),
        "tracked_patterns": tracked,
        "status": [line for line in status_proc.stdout.strip().split("\n") if line],
    })


def _do_diff(payload: dict[str, Any]) -> None:
    if MANIFEST_PATH.exists():
        _register_user_skill_files()
        _validate_manifest()
    diff1 = _git("diff", cwd=WORKSPACE_DIR, check=False)
    diff2 = _git("diff", "--cached", cwd=WORKSPACE_DIR, check=False)
    diff_output = diff1.stdout + diff2.stdout
    _emit_json({
        "success": True, "action": "diff",
        "diff": diff_output if diff_output.strip() else "(no changes)",
    })


def _do_log(payload: dict[str, Any], command_payload: str, action_source: str) -> None:
    count = str(payload.get("count", ""))
    if not count and action_source == "command" and command_payload:
        if command_payload.isdigit():
            count = command_payload
    if not count:
        count = "10"
    log_proc = _git("log", "--oneline", "-n", count, cwd=WORKSPACE_DIR, check=False)
    _emit_json({
        "success": True, "action": "log",
        "commits": [line for line in log_proc.stdout.strip().split("\n") if line],
    })


def _do_commit(payload: dict[str, Any], command_payload: str, action_source: str) -> None:
    message = str(payload.get("message", ""))
    if not message and action_source == "command" and command_payload:
        message = command_payload
    if not message:
        message = "Manual commit"
    _stage_manifest_files()
    if not _has_staged_changes():
        _emit_json({"success": True, "action": "commit", "message": "No changes to commit"})
        return
    _git("commit", "-m", message, cwd=WORKSPACE_DIR)
    commit_sha = _git_output("rev-parse", "--short", "HEAD", cwd=WORKSPACE_DIR)
    _emit_json({"success": True, "action": "commit", "commit": commit_sha, "message": message})


def _do_push(payload: dict[str, Any]) -> None:
    branch = _git_output("branch", "--show-current", cwd=WORKSPACE_DIR) or "main"
    # Validate the HEAD-committed models.yaml before pushing so invalid/
    # malformed/secret model state never reaches the remote, including
    # commits made by other paths (e.g. manual git commit, lifecycle
    # auto-commit). This re-checks the HEAD file content, not just the
    # working copy, so a models.yaml committed by an external path is
    # still gated. Fail-closed: raises SyncError (nonzero) if HEAD
    # carries an invalid models.yaml or if a present models.yaml cannot
    # be validated.
    _validate_head_models_yaml()
    proc = _checked_push(f"HEAD:{branch}")
    if proc.returncode == 0:
        _emit_json({"success": True, "action": "push", "branch": branch})
    else:
        _emit_json({"success": False, "action": "push", "error": "Push failed",
                     "details": proc.stderr.strip()})
        sys.exit(1)


def _do_pull(payload: dict[str, Any]) -> None:
    branch = _git_output("branch", "--show-current", cwd=WORKSPACE_DIR) or "main"
    # Use checked commit so errors are caught, preserving no-change behavior.
    _commit_changes("Pre-merge commit")
    fetch_proc = _authenticated_fetch(branch)
    if fetch_proc.returncode != 0:
        _emit_json({"success": False, "error": "Fetch failed"})
        sys.exit(1)
    try:
        _assert_remote_tree_safe(f"origin/{branch}")
    except RemoteTreeError as exc:
        _error(str(exc))
        _emit_json({"success": False, "error": str(exc)})
        sys.exit(1)
    except SyncError as exc:
        _error(str(exc))
        _emit_json({"success": False, "action": "pull", "error": str(exc)})
        sys.exit(1)
    try:
        _safe_merge(f"origin/{branch}", "Merge remote (remote wins)")
    except SyncError as exc:
        _error(str(exc))
        _emit_json({"success": False, "action": "pull", "error": str(exc)})
        sys.exit(1)
    _emit_json({"success": True, "action": "pull", "branch": branch})


def _do_sync(payload: dict[str, Any], command_payload: str, action_source: str) -> None:
    message = str(payload.get("message", ""))
    if not message and action_source == "command" and command_payload:
        message = command_payload
    if not message:
        message = "Auto-sync"
    _stage_manifest_files()
    if not _has_staged_changes():
        # No new files — but if HEAD is ahead of origin, still push.
        branch = _git_output("branch", "--show-current", cwd=WORKSPACE_DIR) or "main"
        remote_sha = _rev_parse(f"origin/{branch}")
        head_sha = _rev_parse("HEAD")
        # Only retry push if origin/{branch} exists and differs from HEAD.
        if remote_sha and head_sha and remote_sha != head_sha:
            # Validate HEAD models.yaml before pushing (covers commits by
            # other paths). Fail-closed: raises SyncError on invalid content.
            _validate_head_models_yaml()
            push_proc = _checked_push(f"HEAD:{branch}")
            if push_proc.returncode == 0:
                _emit_json({"success": True, "action": "sync", "push": True, "branch": branch})
                return
            _emit_json({"success": False, "action": "sync", "push": False,
                        "error": "Push failed", "details": push_proc.stderr.strip()})
            sys.exit(1)
        _emit_json({"success": True, "action": "sync", "message": "No changes to sync"})
        return
    _git("commit", "-m", message, cwd=WORKSPACE_DIR)
    commit_sha = _git_output("rev-parse", "--short", "HEAD", cwd=WORKSPACE_DIR)
    branch = _git_output("branch", "--show-current", cwd=WORKSPACE_DIR) or "main"
    # Validate HEAD models.yaml before pushing (covers commits by other
    # paths). Fail-closed: raises SyncError on invalid content.
    _validate_head_models_yaml()
    push_proc = _checked_push(f"HEAD:{branch}")
    if push_proc.returncode == 0:
        _emit_json({"success": True, "action": "sync", "commit": commit_sha,
                     "push": True, "branch": branch})
    else:
        _emit_json({"success": False, "action": "sync", "commit": commit_sha,
                     "push": False, "error": "Push failed after commit",
                     "details": push_proc.stderr.strip()})
        sys.exit(1)


def _do_gh(payload: dict[str, Any], command_payload: str, action_source: str,
           argv: list[str] | None = None) -> None:
    """Run ``gh`` with exactly the resolved argv.

    JSON/slash paths resolve the ``command`` string exactly as before
    and shlex-split it. The terminal path passes argv losslessly —
    the list is handed straight to subprocess, never joined and
    re-parsed through a shell. The echoed ``command`` field is display
    only (single-space join); it is never executed.
    """
    if argv is None:
        command_str = str(payload.get("command", ""))
        if action_source == "command":
            command_str = command_payload
        if not command_str.strip():
            _emit_json({"success": False, "error": "No gh command specified"})
            sys.exit(1)
        try:
            argv = shlex.split(command_str)
        except ValueError as exc:
            _emit_json({"success": False, "error": f"Invalid gh command syntax: {exc}"})
            sys.exit(1)
    else:
        command_str = " ".join(argv)
    gh_env = os.environ.copy()
    if REPO_TOKEN and not gh_env.get("GH_TOKEN"):
        gh_env["GH_TOKEN"] = REPO_TOKEN
    try:
        completed = subprocess.run(["gh", *argv], capture_output=True, text=True, env=gh_env)
    except FileNotFoundError:
        _emit_json({"success": False, "error": "gh CLI not found"})
        sys.exit(1)
    output = (completed.stdout or "") + (completed.stderr or "")
    _emit_json({
        "success": completed.returncode == 0, "action": "gh",
        "command": command_str, "exit_code": completed.returncode, "output": output,
    })
    if completed.returncode != 0:
        sys.exit(1)


def _run_tool_payload(payload: dict[str, Any], *, gh_argv: list[str] | None = None) -> int:
    """Dispatch one validated JSON tool payload.

    Shared by the no-argv JSON stdin mode and every bare-argv terminal
    mode so all route through the exact same action resolution, locking,
    and dispatch path (no JSON re-serialization, no stdin replacement).
    ``gh_argv`` carries lossless terminal gh arguments; when None the gh
    command string is resolved from the payload/slash command as before.
    """
    action = str(payload.get("action", ""))
    command_text = str(payload.get("command", ""))
    command_name = str(payload.get("commandName", ""))
    command_normalized = _parse_slash_command(command_text, command_name)
    command_action = ""
    command_payload = ""
    if command_normalized:
        parts = command_normalized.split(None, 1)
        command_action = parts[0].lower() if parts else ""
        command_payload = parts[1] if len(parts) > 1 else ""

    action_source = "json"
    if not action and command_action:
        action = command_action
        action_source = "command"
    if not action and command_name:
        action = "sync"
        action_source = "slash-default"
    if not action:
        _emit_error("No action specified",
                     hint="Use an action field or slash command args (status|diff|log|commit|push|pull|sync|gh)")
        return 1
    if action not in VALID_ACTIONS:
        _emit_error(f"Unknown action: {action}", valid_actions=list(VALID_ACTIONS))
        return 1

    # Catch chdir/lock setup failures and emit exactly one error JSON.
    try:
        os.chdir(str(WORKSPACE_DIR))
    except OSError as exc:
        _emit_error(f"Cannot access workspace directory: {exc}")
        return 1

    try:
        with WorkspaceLock():
            try:
                if action == "status":
                    _do_status(payload)
                elif action == "diff":
                    _do_diff(payload)
                elif action == "log":
                    _do_log(payload, command_payload, action_source)
                elif action == "commit":
                    _do_commit(payload, command_payload, action_source)
                elif action == "push":
                    _do_push(payload)
                elif action == "pull":
                    _do_pull(payload)
                elif action == "sync":
                    _do_sync(payload, command_payload, action_source)
                elif action == "gh":
                    _do_gh(payload, command_payload, action_source, argv=gh_argv)
            except (ManifestError, RemoteTreeError) as exc:
                _error(str(exc))
                _emit_error(str(exc))
                return 1
            except SyncError as exc:
                _error(str(exc))
                _emit_error(str(exc))
                return 1
    except SyncError as exc:
        _error(str(exc))
        _emit_error(str(exc))
        return 1
    return 0


def _run_tool_mode() -> int:
    """No-argv JSON stdin/stdout tool mode (Hermes command-dispatch contract)."""
    raw_input = sys.stdin.read()
    try:
        payload = json.loads(raw_input)
    except (json.JSONDecodeError, ValueError):
        _emit_error("Malformed JSON input")
        return 1
    if not isinstance(payload, dict):
        _emit_error("Input must be a JSON object")
        return 1
    return _run_tool_payload(payload)


# ---------------------------------------------------------------------------
# Lifecycle modes
# ---------------------------------------------------------------------------


def _do_initial_clone() -> None:
    _log("No git repo found. Cloning from remote...")
    tmp_clone = tempfile.mkdtemp(prefix="ws-clone-")
    try:
        # Use the clean REPO_URL for auth env creation.
        clone_url = _sanitize_url(REPO_URL) if _is_https(REPO_URL) else REPO_URL
        git_env = _make_git_env(clone_url)
        try:
            proc = _git("clone", "--branch", BRANCH, "--single-branch", clone_url, tmp_clone,
                       env=git_env, check=False)
        finally:
            _cleanup_git_env(git_env)
        if proc.returncode != 0:
            raise SyncError(f"git clone failed: {proc.stderr.strip()}")
        clone_git = Path(tmp_clone) / ".git"
        if clone_git.is_dir():
            shutil.copytree(clone_git, WORKSPACE_DIR / ".git")
            _log("Git repo initialized from remote")
        else:
            raise SyncError("git clone did not produce a .git directory")
        _configure_git()
        _sanitize_origin()
        _assert_remote_tree_safe(f"origin/{BRANCH}")
        _git("reset", "--hard", f"origin/{BRANCH}", cwd=WORKSPACE_DIR)
        _validate_manifest_if_present()
        _log(f"Workspace files restored from remote ({_git_output('log', '--oneline', '-1', cwd=WORKSPACE_DIR)})")
        _log("Initial clone complete; skipping bootstrap auto-commit")
    finally:
        shutil.rmtree(tmp_clone, ignore_errors=True)


def _do_sync_start() -> None:
    """Startup sync: commit, authenticated fetch, remote-tree check, merge, push.

    Fetch failure and push failure raise SyncError (nonzero exit).
    """
    _configure_git()
    _sanitize_origin()
    _validate_manifest_if_present()

    _log("Committing local changes before sync...")
    _commit_changes(f"Auto-commit before sync: {datetime.datetime.now().isoformat()}")

    _log("Fetching from remote...")
    fetch_proc = _authenticated_fetch(BRANCH)
    if fetch_proc.returncode != 0:
        raise SyncError(f"Failed to fetch from remote: {fetch_proc.stderr.strip()}")

    # Inspect the fetched local remote-tracking ref.
    remote_ref = f"refs/remotes/origin/{BRANCH}"
    ref_proc = _git("rev-parse", "--verify", remote_ref, cwd=WORKSPACE_DIR, check=False)
    if ref_proc.returncode != 0:
        # Remote branch doesn't exist after fetch — push local state.
        _log("Remote branch has no commits yet, pushing local state")
        # Validate HEAD models.yaml before pushing (covers commits by
        # other paths). Fail-closed: raises SyncError on invalid content.
        _validate_head_models_yaml()
        push_proc = _checked_push(f"HEAD:{BRANCH}")
        if push_proc.returncode != 0:
            raise SyncError(f"Failed to push to remote: {push_proc.stderr.strip()}")
        return

    _assert_remote_tree_safe(f"origin/{BRANCH}")

    local_sha = _rev_parse("HEAD")
    remote_sha = _rev_parse(f"origin/{BRANCH}")

    if local_sha == remote_sha:
        _log("Local and remote are in sync")
        return

    _log("Merging remote changes (conflicts: remote wins)...")
    _safe_merge(f"origin/{BRANCH}", "Merge remote with conflict resolution")

    _log("Pushing merged result...")
    # Validate HEAD models.yaml before pushing (covers commits by other
    # paths). Fail-closed: raises SyncError on invalid content.
    _validate_head_models_yaml()
    push_proc = _checked_push(f"HEAD:{BRANCH}")
    if push_proc.returncode != 0:
        raise SyncError(f"Failed to push to remote: {push_proc.stderr.strip()}")


def _do_periodic_sync() -> None:
    """Periodic sync: commit, authenticated fetch, remote-tree check, merge, push.

    Fetch failure and push failure raise SyncError (nonzero exit).
    """
    _configure_git()
    _sanitize_origin()
    _validate_manifest_if_present()

    committed = False
    _log("Periodic sync: committing changes...")
    if _commit_changes(f"Auto-sync: {datetime.datetime.now().isoformat()}"):
        committed = True

    _log("Fetching from remote...")
    fetch_proc = _authenticated_fetch(BRANCH)
    if fetch_proc.returncode != 0:
        raise SyncError(f"Failed to fetch from remote: {fetch_proc.stderr.strip()}")

    _assert_remote_tree_safe(f"origin/{BRANCH}")
    local_sha = _rev_parse("HEAD")
    remote_sha = _rev_parse(f"origin/{BRANCH}")
    if local_sha != remote_sha:
        _log("Merging remote changes (conflicts: remote wins)...")
        _safe_merge(f"origin/{BRANCH}", "Merge remote with conflict resolution")

    local_sha = _rev_parse("HEAD")
    remote_sha = _rev_parse(f"origin/{BRANCH}")

    if committed or local_sha != remote_sha:
        _log("Pushing to remote...")
        # Validate HEAD models.yaml before pushing (covers commits by
        # other paths). Fail-closed: raises SyncError on invalid content.
        _validate_head_models_yaml()
        push_proc = _checked_push(f"HEAD:{BRANCH}")
        if push_proc.returncode != 0:
            raise SyncError(f"Failed to push to remote: {push_proc.stderr.strip()}")


def _run_lifecycle(mode: str) -> int:
    if not REPO_URL:
        _log("WORKSPACE_STATE_REPO not configured, skipping git sync")
        return 0
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    with WorkspaceLock():
        try:
            if not (WORKSPACE_DIR / ".git").is_dir():
                _do_initial_clone()
            elif mode == "periodic":
                _do_periodic_sync()
            elif SYNC_ON_START == "true":
                _do_sync_start()
            else:
                _configure_git()
                _sanitize_origin()
                _log("Sync on start disabled, configuring git only")
        except (ManifestError, RemoteTreeError) as exc:
            _error(str(exc))
            return 1
        except SyncError as exc:
            _error(str(exc))
            return 1
    return 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _terminal_usage_line() -> str:
    return (
        f"Usage: {sys.argv[0]} [startup|periodic] | "
        f"{sys.argv[0]} status | {sys.argv[0]} diff | {sys.argv[0]} push | "
        f"{sys.argv[0]} pull | {sys.argv[0]} log [COUNT] | "
        f"{sys.argv[0]} commit [MESSAGE...] | {sys.argv[0]} sync [MESSAGE...] | "
        f"{sys.argv[0]} gh ARGS..."
    )


def _reject_terminal_argv(args: list[str], reason: str) -> int:
    _error(f"{reason}: {' '.join(args)}")
    _error(_terminal_usage_line())
    return 1


def _is_positive_decimal_count(value: str) -> bool:
    # ASCII-only digits: isdigit() alone admits unicode digits (e.g. ²)
    # that int() cannot convert. No int() conversion is performed at
    # all, so an ASCII digit string beyond Python's 4300-digit int()
    # limit can never raise ValueError; the MAX_LOG_COUNT_DIGITS bound
    # rejects oversized counts before dispatch. Positive means at least
    # one nonzero digit (all-zeros is "0" and stays rejected).
    if not value.isascii() or not value.isdigit():
        return False
    if len(value) > MAX_LOG_COUNT_DIGITS:
        return False
    return any(char != "0" for char in value)


def _run_terminal_mode(args: list[str]) -> int:
    """Bare-argv terminal dispatch (issue #114).

    The full argv is validated BEFORE any chdir/lock/manifest/git/stdin
    access; invalid action/arity/count returns nonzero with concise
    stderr usage and zero stdout (no workspace mutation). Never reads
    stdin. Actions route through the exact JSON payload dispatch path.
    """
    action = args[0]
    rest = args[1:]
    if action in ("status", "diff", "push", "pull"):
        if rest:
            return _reject_terminal_argv(args, f"'{action}' takes no arguments")
        return _run_tool_payload({"action": action})
    if action == "log":
        if len(rest) > 1:
            return _reject_terminal_argv(args, "'log' takes at most one COUNT argument")
        payload = {"action": "log"}
        if rest:
            if not _is_positive_decimal_count(rest[0]):
                return _reject_terminal_argv(
                    args, "'log' COUNT must be a positive decimal integer"
                )
            payload["count"] = rest[0]
        return _run_tool_payload(payload)
    if action in ("commit", "sync"):
        payload = {"action": action}
        if rest:
            payload["message"] = " ".join(rest)
        return _run_tool_payload(payload)
    # action == "gh": require at least one token; argv passed losslessly.
    if not rest:
        return _reject_terminal_argv(args, "'gh' requires a command")
    return _run_tool_payload({"action": "gh"}, gh_argv=rest)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return _run_tool_mode()
    if len(args) == 1 and args[0] in ("startup", "periodic"):
        return _run_lifecycle(args[0])
    if args[0] in VALID_ACTIONS:
        return _run_terminal_mode(args)
    _error(f"Unknown mode: {' '.join(args)}")
    _error(_terminal_usage_line())
    return 1


if __name__ == "__main__":
    sys.exit(main())