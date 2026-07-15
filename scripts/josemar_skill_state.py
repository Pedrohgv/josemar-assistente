#!/usr/bin/env python3
"""Josemar state-backed Hermes skill toggle helper.

This module is the single source of truth for translating between Hermes'
runtime skill configuration (``config["skills"]["disabled"]`` and
``config["skills"]["platform_disabled"][platform]``) and the canonical
git-backed sidecars that survive redeploys and sync across hosts.

Architecture (fixed, do not redesign):

- The full ``/opt/data/config.yaml`` is sensitive/noisy and deliberately
  NOT tracked. Only canonical JSON sidecars under
  ``<workspace>/hermes/skill-toggles/`` are versioned.
- ``default.json`` mirrors the workspace-root (base ``HERMES_HOME``)
  profile. ``profiles/<canonical>.json`` mirrors a named profile's
  ``HERMES_HOME``. Other paths are rejected.
- Sidecar schema (exactly, one line, sorted/deduped string lists):

      {"version": 1,
       "disabled": ["..."],
       "platform_disabled": {"<platform>": ["..."]}}

  Explicit empty arrays are retained so a clear is durable.
- Dashboard ``PUT /api/skills/toggle`` and the CLI ``hermes skills`` flow
  through ``save_disabled_skills`` in ``hermes_cli/skills_config.py``.
  A pinned build-time patch replaces the final ``save_config`` call with
  :func:`save_disabled_skills_stateful`, which atomically writes the
  sidecar first and then invokes native ``save_config`` under one
  advisory lock. State write failure fails the save rather than silently
  diverging. The helper is copied into the ``hermes_cli`` package
  (``/opt/hermes/hermes_cli/josemar_skill_state.py``) and imported as
  ``hermes_cli.josemar_skill_state`` — the same package-relative import
  style every ``hermes_cli`` sibling module uses.
- Startup migration (before the repo template overwrites the runtime
  config) extracts existing toggle keys into absent sidecars only when
  the keys exist. After template overwrite + workspace sync/seed, the
  sidecars are applied back and the policy keys are enforced.
- Periodic workspace sync holds the same advisory lock around git sync
  plus applying merged sidecars so dashboard writes and sync never race.

The helper may use PyYAML from the Hermes venv for runtime config
projections and stdlib JSON for sidecars. It exposes testable functions
and a small CLI used by the init script and the cron wrapper.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

SCHEMA_VERSION = 1
SIDECAR_DIR_NAME = "hermes"
SIDECAR_SUBDIR = "skill-toggles"
DEFAULT_SIDECAR_NAME = "default.json"
PROFILES_SUBDIR = "profiles"

# Advisory lock file lives next to the sidecar directory so dashboard
# writes and the periodic workspace sync never race. Named explicitly to
# avoid colliding with Hermes' own config lock.
LOCK_FILENAME = ".skill-toggles.lock"

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _workspace_root() -> Path:
    """Return the workspace root (base ``HERMES_HOME``)."""
    value = os.environ.get("WORKSPACE_DIR", "").strip()
    if value:
        return Path(value)
    value = os.environ.get("HERMES_HOME", "").strip()
    if value:
        return Path(value)
    return Path("/opt/data")


def _sidecar_root() -> Path:
    """Return ``<workspace>/hermes/skill-toggles``."""
    return _workspace_root() / SIDECAR_DIR_NAME / SIDECAR_SUBDIR


def _default_sidecar_path() -> Path:
    return _sidecar_root() / DEFAULT_SIDECAR_NAME


def _profiles_sidecar_dir() -> Path:
    return _sidecar_root() / PROFILES_SUBDIR


def _profile_sidecar_path(canonical: str) -> Path:
    if canonical == "default":
        return _default_sidecar_path()
    return _profiles_sidecar_dir() / f"{canonical}.json"


def _canonical_profile_name(name: Optional[str]) -> str:
    """Normalize a profile name to its canonical on-disk id."""
    raw = (name or "").strip()
    if not raw:
        return "default"
    if raw.casefold() == "default":
        return "default"
    canon = raw.lower()
    if not _PROFILE_ID_RE.match(canon):
        raise ValueError(f"Invalid profile name {name!r}")
    return canon


def resolve_sidecar_for_hermes_home(hermes_home: Path) -> Path:
    """Map a ``HERMES_HOME`` directory to its sidecar path.

    Mapping:
      - workspace root (base ``HERMES_HOME``) -> ``default.json``
      - ``<workspace>/profiles/<canonical>`` -> ``profiles/<canonical>.json``
      - any other path -> ``ValueError``
    """
    workspace = _workspace_root()
    try:
        home_resolved = hermes_home.resolve()
        workspace_resolved = workspace.resolve()
    except OSError as exc:
        raise ValueError(f"Cannot resolve paths: {exc}") from exc

    if home_resolved == workspace_resolved:
        return _default_sidecar_path()

    try:
        rel = home_resolved.relative_to(workspace_resolved / PROFILES_SUBDIR)
    except ValueError as exc:
        raise ValueError(
            f"HERMES_HOME {hermes_home} is neither the workspace root "
            f"nor a named profile under {workspace / PROFILES_SUBDIR}"
        ) from exc

    parts = rel.parts
    if len(parts) != 1 or not parts[0]:
        raise ValueError(f"Unexpected profile path {hermes_home}")
    canonical = parts[0]
    if not _PROFILE_ID_RE.match(canonical):
        raise ValueError(f"Invalid profile id in path {hermes_home}")
    return _profile_sidecar_path(canonical)


def resolve_sidecar_for_profile(profile: Optional[str]) -> Path:
    """Map a profile name (or None/empty for default) to its sidecar path."""
    return _profile_sidecar_path(_canonical_profile_name(profile))


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


class SkillStateLock:
    """Re-entrant advisory lock guarding sidecar + config writes.

    Uses ``flock`` on a lock file inside the sidecar directory. The lock
    file is created on demand and never tracked (it lives under
    ``hermes/skill-toggles/`` which is allowlisted only for the JSON
    sidecars via narrow manifest/gitignore rules; the ``.lock`` extension
    is not in the allowlist).
    """

    def __init__(self, lock_path: Optional[Path] = None) -> None:
        self.lock_path = lock_path if lock_path is not None else _sidecar_root() / LOCK_FILENAME
        self._fh: Optional[io.IOBase] = None
        self._depth = 0

    def _ensure_parent(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "SkillStateLock":
        if self._depth > 0:
            self._depth += 1
            return self
        self._ensure_parent()
        self._fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            self._fh.close()
            self._fh = None
            raise
        self._depth = 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._depth <= 0:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


# ---------------------------------------------------------------------------
# Normalization / serialization
# ---------------------------------------------------------------------------


def _normalize_string_list(value: Any) -> List[str]:
    """Coerce a config value into a sorted, deduped list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        raise ValueError(f"Expected a list of strings, got {type(value).__name__}")
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item is None:
            continue
        s = str(item).strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return sorted(out)


def _normalize_platform_disabled(value: Any) -> Dict[str, List[str]]:
    """Coerce ``platform_disabled`` into a dict of sorted/deduped string lists."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"platform_disabled must be a mapping, got {type(value).__name__}"
        )
    out: Dict[str, List[str]] = {}
    for platform, items in value.items():
        if not isinstance(platform, str) or not platform.strip():
            raise ValueError("platform_disabled keys must be non-empty strings")
        out[platform.strip()] = _normalize_string_list(items)
    return dict(sorted(out.items()))


def normalize_state(disabled: Any, platform_disabled: Any) -> Dict[str, Any]:
    """Build a canonical sidecar dict from raw config values."""
    return {
        "version": SCHEMA_VERSION,
        "disabled": _normalize_string_list(disabled),
        "platform_disabled": _normalize_platform_disabled(platform_disabled),
    }


def empty_state() -> Dict[str, Any]:
    """Return a canonical empty sidecar dict (explicit empty arrays)."""
    return {"version": SCHEMA_VERSION, "disabled": [], "platform_disabled": {}}


def serialize_sidecar(state: Dict[str, Any]) -> str:
    """Render a sidecar dict as a single canonical JSON line."""
    if not isinstance(state, dict):
        raise ValueError("sidecar state must be a dict")
    if state.get("version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported sidecar version: {state.get('version')!r}"
        )
    normalized = normalize_state(
        state.get("disabled", []),
        state.get("platform_disabled", {}),
    )
    return json.dumps(normalized, separators=(",", ":"), sort_keys=False, ensure_ascii=False)


def deserialize_sidecar(text: str) -> Dict[str, Any]:
    """Parse a sidecar string, validating schema strictly.

    Raises ``ValueError`` on any malformed input so callers can surface a
    clear error and leave runtime config untouched.
    """
    if not text or not text.strip():
        raise ValueError("empty sidecar content")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid sidecar JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("sidecar root must be an object")
    if data.get("version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported sidecar version: {data.get('version')!r}"
        )
    disabled = data.get("disabled")
    if disabled is None:
        disabled = []
    if not isinstance(disabled, list):
        raise ValueError("'disabled' must be a list")
    platform_disabled = data.get("platform_disabled")
    if platform_disabled is None:
        platform_disabled = {}
    if not isinstance(platform_disabled, dict):
        raise ValueError("'platform_disabled' must be a mapping")
    return normalize_state(disabled, platform_disabled)


# ---------------------------------------------------------------------------
# Atomic sidecar I/O
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` via temp+replace.

    Preserves the existing file mode where practical so permissions set
    by the operator survive a rewrite.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o644
    try:
        st = path.stat()
        mode = st.st_mode & 0o777
    except FileNotFoundError:
        pass
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            if not content.endswith("\n"):
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_sidecar(path: Path, state: Dict[str, Any]) -> None:
    """Atomically write a canonical sidecar to ``path``."""
    _atomic_write(path, serialize_sidecar(state))


def read_sidecar(path: Path) -> Optional[Dict[str, Any]]:
    """Read and validate a sidecar. Returns ``None`` if the file is absent.

    Malformed sidecars raise ``ValueError`` so callers can surface a clear
    error and leave runtime config untouched.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read sidecar {path}: {exc}") from exc
    return deserialize_sidecar(text)


# ---------------------------------------------------------------------------
# Config <-> state projection
# ---------------------------------------------------------------------------


def _skills_section(config: Dict[str, Any]) -> Dict[str, Any]:
    section = config.get("skills")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError("config 'skills' section must be a mapping")
    return section


def extract_state_from_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Project the toggle keys out of a Hermes config dict."""
    skills = _skills_section(config)
    return normalize_state(
        skills.get("disabled", []),
        skills.get("platform_disabled", {}),
    )


def config_has_toggle_keys(config: Dict[str, Any]) -> bool:
    """Return True if the config has ``disabled`` or ``platform_disabled`` keys."""
    skills = _skills_section(config)
    return "disabled" in skills or "platform_disabled" in skills


def apply_state_to_config(
    config: Dict[str, Any], state: Dict[str, Any]
) -> Dict[str, Any]:
    """Apply a sidecar state to a config dict, preserving unrelated keys.

    Returns the (mutated) config. Only ``skills.disabled`` and
    ``skills.platform_disabled`` are touched; every other config key is
    preserved verbatim.
    """
    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        raise ValueError("config 'skills' section must be a mapping")
    skills["disabled"] = list(state.get("disabled", []))
    platform_disabled = state.get("platform_disabled", {})
    if platform_disabled:
        skills["platform_disabled"] = {
            platform: list(items) for platform, items in platform_disabled.items()
        }
    else:
        # Explicit empty mapping so a clear is durable and matches the
        # sidecar's explicit-empty-array semantics.
        skills["platform_disabled"] = {}
    return config


# ---------------------------------------------------------------------------
# Policy enforcement
# ---------------------------------------------------------------------------

POLICY_KEYS: Tuple[Tuple[Tuple[str, ...], Any], ...] = (
    (("skills", "creation_nudge_interval"), 0),
    (("skills", "write_approval"), True),
    (("curator", "enabled"), False),
)


def enforce_policy(config: Dict[str, Any]) -> bool:
    """Force the Josemar skill policy keys onto ``config``.

    Returns True if any key was changed. Does NOT touch memory nudge or
    any other unrelated key.
    """
    changed = False
    for path, value in POLICY_KEYS:
        node: Any = config
        for key in path[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        if node.get(path[-1]) != value:
            node[path[-1]] = value
            changed = True
    return changed


def policy_violations(config: Dict[str, Any]) -> List[str]:
    """Return a list of policy keys whose values do not match."""
    violations: List[str] = []
    for path, value in POLICY_KEYS:
        node: Any = config
        for key in path[:-1]:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if not isinstance(node, dict) or node.get(path[-1]) != value:
            violations.append(".".join(path))
    return violations


# ---------------------------------------------------------------------------
# Hermes config load/save (runtime, uses PyYAML from Hermes venv)
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required for runtime config projection but is not "
            "available in this Python environment"
        ) from exc
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a YAML mapping")
    return data


def _dump_yaml(path: Path, config: Dict[str, Any]) -> None:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required for runtime config projection but is not "
            "available in this Python environment"
        ) from exc
    _atomic_write_yaml(path, config, yaml)


def _atomic_write_yaml(path: Path, config: Dict[str, Any], yaml_module: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    try:
        st = path.stat()
        mode = st.st_mode & 0o777
    except FileNotFoundError:
        pass
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml_module.safe_dump(
                config, fh, default_flow_style=False, sort_keys=False, allow_unicode=True
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# High-level operations used by the patched skills_config + init + cron
# ---------------------------------------------------------------------------


def save_disabled_skills_stateful(
    config: Dict[str, Any],
    disabled: Set[str],
    platform: Optional[str] = None,
) -> None:
    """Josemar replacement for ``skills_config.save_disabled_skills``.

    Mutates ``config["skills"]`` exactly like the upstream helper, then
    atomically writes the sidecar first and finally invokes native
    ``save_config``. Both writes happen under one advisory lock. A sidecar
    write failure propagates so the dashboard/CLI save fails rather than
    silently diverging from the tracked state.
    """
    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        raise ValueError("config 'skills' section must be a mapping")
    if platform is None:
        skills["disabled"] = sorted(disabled)
    else:
        skills.setdefault("platform_disabled", {})
        if not isinstance(skills["platform_disabled"], dict):
            raise ValueError("config 'skills']['platform_disabled'] must be a mapping")
        skills["platform_disabled"][platform] = sorted(disabled)

    sidecar = resolve_sidecar_for_hermes_home(_active_hermes_home())
    state = extract_state_from_config(config)
    with SkillStateLock():
        write_sidecar(sidecar, state)
        _native_save_config(config)


def _active_hermes_home() -> Path:
    """Return the currently active ``HERMES_HOME``.

    Honors the context-local override used by ``_profile_scope`` so a
    dashboard request for a named profile resolves the right sidecar.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return get_hermes_home()
    except Exception:
        value = os.environ.get("HERMES_HOME", "").strip()
        return Path(value) if value else _workspace_root()


def _native_save_config(config: Dict[str, Any]) -> None:
    """Invoke the upstream ``save_config`` from ``hermes_cli.config``."""
    from hermes_cli.config import save_config  # type: ignore

    save_config(config)


# ---------------------------------------------------------------------------
# Migration / reconciliation (init script)
# ---------------------------------------------------------------------------


def migrate_existing_toggles_to_absent_sidecars(
    config_path: Path, hermes_home: Path
) -> bool:
    """Extract existing toggle keys into an absent sidecar, only if keys exist.

    Used BEFORE the repo template overwrites the runtime config so a
    pre-feature deployment's toggles survive the upgrade. Returns True if a
    sidecar was created. Does NOT create an empty ``default.json`` for a
    feature-less config (production migration preserves pre-feature state).
    """
    if not config_path.exists():
        return False
    config = _load_yaml(config_path)
    if not config_has_toggle_keys(config):
        return False
    sidecar = resolve_sidecar_for_hermes_home(hermes_home)
    if sidecar.exists():
        # Sidecar already present (e.g. named profile already migrated).
        return False
    state = extract_state_from_config(config)
    # Only write if there is something to preserve; an empty state for the
    # default profile is deliberately NOT created so production migration
    # can preserve pre-feature toggles.
    if not state["disabled"] and not state["platform_disabled"]:
        return False
    write_sidecar(sidecar, state)
    return True


def apply_sidecar_and_enforce_policy(
    config_path: Path, hermes_home: Path
) -> str:
    """Apply the sidecar for ``hermes_home`` to ``config_path`` and enforce policy.

    Preserves all unrelated config keys. Malformed sidecar leaves the
    config untouched and surfaces a clear error. Returns a short status
    string for logging.
    """
    sidecar = resolve_sidecar_for_hermes_home(hermes_home)
    config = _load_yaml(config_path) if config_path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError(f"{config_path} does not contain a YAML mapping")

    state: Optional[Dict[str, Any]] = None
    if sidecar.exists():
        state = read_sidecar(sidecar)  # raises on malformed
        apply_state_to_config(config, state)  # type: ignore[arg-type]

    policy_changed = enforce_policy(config)

    if state is None and not policy_changed:
        return "no-sidecar-no-policy-change"
    _dump_yaml(config_path, config)
    if state is not None and policy_changed:
        return "applied-sidecar-and-policy"
    if state is not None:
        return "applied-sidecar"
    return "enforced-policy"


# ---------------------------------------------------------------------------
# Periodic sync helper (cron)
# ---------------------------------------------------------------------------


def _iter_reconcilable_profiles() -> List[Tuple[str, Path, Path]]:
    """Return ``(canonical, hermes_home, config_path)`` for every reconcilable profile.

    A profile is reconcilable when its ``config.yaml`` exists. The default
    profile is the workspace root itself; named profiles live under
    ``<workspace>/profiles/<canonical>/``. This walks the filesystem
    (not the sidecar directory) so existing named profiles WITHOUT a
    sidecar still get policy enforcement. Invalid profile directory names
    are skipped.
    """
    workspace = _workspace_root()
    out: List[Tuple[str, Path, Path]] = []
    default_config = workspace / "config.yaml"
    if default_config.exists():
        out.append(("default", workspace, default_config))

    profiles_root = workspace / PROFILES_SUBDIR
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            canonical = entry.name
            if not _PROFILE_ID_RE.match(canonical):
                continue
            config_path = entry / "config.yaml"
            if config_path.exists():
                out.append((canonical, entry, config_path))
    return out


def _apply_all_sidecars_and_policy_unlocked() -> List[str]:
    """Unlocked internal apply: reconcile every reconcilable profile config.

    For each profile with a ``config.yaml``: apply its sidecar when present
    and enforce the Josemar policy. Named profiles without a sidecar get
    policy enforcement alone. Orphan sidecars (no matching config) are
    reported. Must be called while holding :class:`SkillStateLock` (or
    inside a critical section that already holds it) to avoid racing with
    dashboard writes.
    """
    statuses: List[str] = []
    workspace = _workspace_root()
    reconciled = {canonical: hermes_home for canonical, hermes_home, _ in _iter_reconcilable_profiles()}

    for canonical, hermes_home, config_path in _iter_reconcilable_profiles():
        try:
            status = apply_sidecar_and_enforce_policy(config_path, hermes_home)
        except ValueError as exc:
            statuses.append(f"{canonical}:error:{exc}")
            continue
        statuses.append(f"{canonical}:{status}")

    # Report orphan sidecars: a named-profile sidecar with no matching
    # config.yaml. The default sidecar is always reconciled when the
    # default config exists, so it is not orphaned here.
    profiles_sidecar_dir = _profiles_sidecar_dir()
    if profiles_sidecar_dir.is_dir():
        for sidecar in sorted(profiles_sidecar_dir.glob("*.json")):
            canonical = sidecar.stem
            if not _PROFILE_ID_RE.match(canonical):
                statuses.append(f"{canonical}:skipped-invalid-name")
                continue
            if canonical not in reconciled:
                statuses.append(f"{canonical}:orphan-sidecar")
    return statuses


def apply_all_sidecars_and_policy() -> List[str]:
    """Apply every present sidecar + enforce policy for every profile config.

    Public locked wrapper: acquires :class:`SkillStateLock` and delegates
    to :func:`_apply_all_sidecars_and_policy_unlocked`. Use this from the
    init script and standalone CLI invocations. Cron sync should use
    :func:`sync_and_apply` so the same lock covers git sync + apply.
    """
    with SkillStateLock():
        return _apply_all_sidecars_and_policy_unlocked()


def sync_and_apply(
    sync_command: List[str],
    *,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, List[str], str]:
    """Run ``sync_command`` and apply sidecars/policy under one advisory lock.

    Acquires :class:`SkillStateLock`, runs the workspace sync command with
    the inherited environment (optionally overridden by ``env``), and only
    after a successful sync applies merged sidecars + policy via
    :func:`_apply_all_sidecars_and_policy_unlocked` before releasing the
    lock. This guarantees the dashboard's toggle writes and the periodic
    sync+apply never race: git sync, remote merge, and the sidecar apply
    all happen inside one critical section.

    Returns ``(sync_exit_status, apply_statuses, sync_output)``. The sync
    command's exit status is preserved exactly. If apply fails after a
    successful sync, the failure is captured in ``apply_statuses`` (each
    entry carries an ``error:`` segment) but does NOT change the returned
    exit status — sync succeeded and that fact is reported faithfully.
    Callers that want apply failures to fail the cron run can inspect
    ``apply_statuses`` for ``error:`` segments.

    The lock is acquired BEFORE the sync command runs, so a concurrent
    dashboard toggle write is blocked for the whole sync+apply window.
    Nested acquisition of the same flock is avoided because the apply
    path uses the unlocked internal helper.
    """
    import subprocess

    inherit_env = os.environ.copy()
    if env is not None:
        inherit_env.update(env)

    with SkillStateLock():
        proc = subprocess.run(
            sync_command,
            env=inherit_env,
            capture_output=True,
            text=True,
            check=False,
        )
        sync_output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return proc.returncode, [], sync_output
        try:
            statuses = _apply_all_sidecars_and_policy_unlocked()
        except Exception as exc:
            statuses = [f"apply:error:{exc}"]
        return 0, statuses, sync_output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_migrate(args: argparse.Namespace) -> int:
    hermes_home = Path(args.hermes_home) if args.hermes_home else _workspace_root()
    config_path = Path(args.config_path) if args.config_path else hermes_home / "config.yaml"
    created = migrate_existing_toggles_to_absent_sidecars(config_path, hermes_home)
    print(f"migrate: {'created' if created else 'no-op'} {config_path}")
    return 0


def _cli_apply(args: argparse.Namespace) -> int:
    hermes_home = Path(args.hermes_home) if args.hermes_home else _workspace_root()
    config_path = Path(args.config_path) if args.config_path else hermes_home / "config.yaml"
    status = apply_sidecar_and_enforce_policy(config_path, hermes_home)
    print(f"apply: {status} {config_path}")
    return 0


def _cli_apply_all(args: argparse.Namespace) -> int:
    statuses = apply_all_sidecars_and_policy()
    if not statuses:
        print("apply-all: no profiles to reconcile")
    for entry in statuses:
        print(f"apply-all: {entry}")
    return 0


def _cli_sync_and_apply(args: argparse.Namespace) -> int:
    # The sync command is passed as remaining argv after the subcommand.
    # ``argparse.REMAINDER`` captures a leading ``--`` separator as a
    # literal argument; drop it so callers can write
    # ``sync-and-apply -- workspace-sync.sh``.
    sync_command = [a for a in args.sync_command if a != "--"]
    if not sync_command:
        print("sync-and-apply: no sync command provided", file=sys.stderr)
        return 2
    exit_status, statuses, sync_output = sync_and_apply(sync_command)
    if sync_output:
        sys.stdout.write(sync_output)
        if not sync_output.endswith("\n"):
            sys.stdout.write("\n")
    for entry in statuses:
        print(f"sync-and-apply: {entry}")
    return exit_status


def _cli_resolve(args: argparse.Namespace) -> int:
    if args.hermes_home:
        path = resolve_sidecar_for_hermes_home(Path(args.hermes_home))
    else:
        path = resolve_sidecar_for_profile(args.profile)
    print(str(path))
    return 0


def _cli_show(args: argparse.Namespace) -> int:
    path = resolve_sidecar_for_profile(args.profile)
    if not path.exists():
        print(f"show: {path} does not exist", file=sys.stderr)
        return 1
    state = read_sidecar(path)
    print(serialize_sidecar(state))  # type: ignore[arg-type]
    return 0


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="josemar_skill_state",
        description="Josemar state-backed Hermes skill toggle helper.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_migrate = sub.add_parser(
        "migrate",
        help="Extract existing toggle keys into an absent sidecar (init pre-overwrite).",
    )
    p_migrate.add_argument("--hermes-home", default=None)
    p_migrate.add_argument("--config-path", default=None)
    p_migrate.set_defaults(func=_cli_migrate)

    p_apply = sub.add_parser(
        "apply",
        help="Apply a sidecar + policy to a single profile config (init post-sync).",
    )
    p_apply.add_argument("--hermes-home", default=None)
    p_apply.add_argument("--config-path", default=None)
    p_apply.set_defaults(func=_cli_apply)

    p_apply_all = sub.add_parser(
        "apply-all",
        help="Apply all present sidecars + policy (init, under one lock).",
    )
    p_apply_all.set_defaults(func=_cli_apply_all)

    p_sync = sub.add_parser(
        "sync-and-apply",
        help=(
            "Run a workspace sync command then apply sidecars + policy "
            "under one advisory lock (cron). The remaining argv is the "
            "sync command, e.g. sync-and-apply -- workspace-sync.sh."
        ),
    )
    p_sync.add_argument("sync_command", nargs=argparse.REMAINDER)
    p_sync.set_defaults(func=_cli_sync_and_apply)

    p_resolve = sub.add_parser(
        "resolve", help="Resolve a sidecar path for a profile or HERMES_HOME."
    )
    p_resolve.add_argument("--profile", default=None)
    p_resolve.add_argument("--hermes-home", default=None)
    p_resolve.set_defaults(func=_cli_resolve)

    p_show = sub.add_parser("show", help="Print a canonical sidecar.")
    p_show.add_argument("--profile", default=None)
    p_show.set_defaults(func=_cli_show)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_cli()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())