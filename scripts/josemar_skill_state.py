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
- A second profile-aware sidecar family, ``hermes/command-allowlist/``,
  holds the state-owned runtime command allowlist with the strict v1
  schema ``{"version": 1, "command_allowlist": ["..."]}`` (exactly these
  keys, both required; sorted/deduped non-empty strings). ``default.json``
  mirrors the workspace root and ``profiles/<canonical>.json`` a named
  profile. Presence is authoritative: an explicit ``[]`` keeps the
  ROOT-LEVEL runtime ``command_allowlist`` key durably empty while an
  ABSENT sidecar removes that key. Every present allowlist sidecar is
  validated BEFORE any runtime config write in an apply cycle
  (fail-closed, no partial apply), and errors/statuses never include
  allowlist contents. ``validate_command_allowlist_state_from_text`` is
  the canonical text validator, reused by workspace_sync (local staging
  and remote candidate validation) so the schema rules are never
  duplicated.
- Startup migration also extracts a NON-EMPTY runtime command_allowlist
  into an absent allowlist sidecar before the template overwrite (never
  overwrites an existing sidecar, never invents an explicit-empty one).
- ``save_command_allowlist_stateful`` is the stateful runtime write path:
  it accepts ONLY a real list (str/tuple/set/generators are rejected
  before any write; W2 explicitly converts its set to list). Under the
  shared advisory lock it atomically writes the sidecar FIRST and then
  the raw runtime config via native ``save_config``, passing
  ``preserve_keys={("command_allowlist",)}`` per the pinned upstream
  signature so an explicit empty ``command_allowlist: []`` survives
  upstream persistence. A state write failure fails the save
  before the runtime config is touched; a runtime failure afterward
  propagates and the next reconcile cycle repairs the runtime config
  from the durable sidecar.
  ``clear_command_allowlist_stateful`` is the exact-key unset path: under
  the same lock it deletes the sidecar first, then removes only the
  root-level ``command_allowlist`` key from the raw runtime config and
  persists it natively (absent-sidecar deletion is a no-op success).
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
from typing import Any, Dict, List, Optional, Set, Tuple

SCHEMA_VERSION = 1
SIDECAR_DIR_NAME = "hermes"
SIDECAR_SUBDIR = "skill-toggles"
DEFAULT_SIDECAR_NAME = "default.json"
PROFILES_SUBDIR = "profiles"
# Command-allowlist sidecar family: <workspace>/hermes/command-allowlist/.
# Same default/profile layout as the skill-toggle family.
ALLOWLIST_SIDECAR_SUBDIR = "command-allowlist"
# One value-free global migration-completion marker (exact filename, strict v1
# schema). Presence means the one-time legacy runtime import has been finalized
# for this state history; runtime config must never again be treated as a
# command-allowlist source. See the migration-marker section below.
MIGRATION_MARKER_VERSION = 1
MIGRATION_MARKER_NAME = "migration-v1.json"

# Advisory lock file lives next to the sidecar directory so dashboard
# writes and the periodic workspace sync never race. Named explicitly to
# avoid colliding with Hermes' own config lock.
LOCK_FILENAME = ".skill-toggles.lock"

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# ---------------------------------------------------------------------------
# State-owned model authoring overlay
# ---------------------------------------------------------------------------
#
# The canonical tracked state file ``agent-state/hermes/models.yaml``
# (version: 1) lets the operator author the model/fallback/auxiliary/cron
# model selection in git-backed state, layered onto the runtime config
# AFTER the repo template is copied and workspace sync has run. Only
# root-level model selection is supported (no named profiles/multiplexing).
#
# Strict selection-only v1 schema (every unknown nested key is rejected):
#   version: 1
#   model:                       # root model selection
#     provider: "<id>"           # required, nonempty
#     default: "<model>"         # required, nonempty
#   fallback_providers:         # optional list
#     - provider: "<id>"         # required, nonempty
#       model: "<model>"         # required, nonempty
#   auxiliary:                   # optional mapping of task configs
#     vision:                    # allowlisted task (see ALLOWED_AUXILIARY_TASKS)
#       provider: "<id>"         # required, nonempty
#       model: "<model>"         # '' exactly iff provider == 'auto'; else nonempty
#   cron:                        # optional
#     model: "<model>"           # optional string (blank = inherit default)
#     model_provider: "<id>"    # optional string (blank = inherit default)
#
# Forbidden in this file (validation rejects the file if present):
#   base_url, api_mode, extra_body, context_length, max_tokens, timeouts,
#   token limits, fallback_chain, credentials/secret keys, provider
#   definitions, deployment topology, or any other Hermes config. Those
#   stay in config.yaml / .env and are never versioned here.
#
# The overlay validates the full file BEFORE mutating the runtime config
# and leaves the config untouched on any validation failure (fail-closed).
# When models.yaml is absent/empty, state-owned keys are restored to the
# repo template defaults (rollback), preserving unrelated runtime keys.
# Application happens only through the shared advisory lock so dashboard
# writes and sync never race; no unrelated-field loss and no changes
# occur from validation failures.

MODELS_SCHEMA_VERSION = 1
MODELS_SIDECAR_NAME = "models.yaml"
# Allowlisted auxiliary task names, in deterministic upstream dashboard
# order. Pinned to the reviewed Hermes base image
# (nousresearch/hermes-agent:v2026.8.31); extending this set requires an
# upstream review. A fast tripwire test
# (tests/skill_state/test_models_overlay.py) fails if either the image pin
# or this exact list drifts.
ALLOWED_AUXILIARY_TASKS: Tuple[str, ...] = (
    "vision",
    "compression",
    "skills_hub",
    "approval",
    "mcp",
    "title_generation",
    "review",
    "triage_specifier",
    "kanban_decomposer",
    "profile_describer",
    "curator",
)
# State-owned config keys (owned by the overlay). Used for rollback: when
# models.yaml is absent/empty, these keys are restored to template defaults.
# Only provider/model selection is owned — template-owned sibling fields
# (api_key, download_timeout, base_url, etc.) are preserved by deep merge.
MODEL_SELECTION_KEYS: Tuple[str, ...] = ("provider", "default")
FALLBACK_SELECTION_KEYS: Tuple[str, ...] = ("provider", "model")
AUXILIARY_SELECTION_KEYS: Tuple[str, ...] = ("provider", "model")
CRON_SELECTION_KEYS: Tuple[str, ...] = ("model", "model_provider")
# Default repo template config path (copied to runtime config at init).
# Used for rollback when models.yaml is absent/empty. May be overridden via
# the JOSEMAR_TEMPLATE_CONFIG env var or the --template-config CLI flag.
DEFAULT_TEMPLATE_CONFIG_PATH = "/opt/josemar/hermes/config.yaml"
# Secret-looking key substrings that must never appear in models.yaml.
_SECRET_KEY_RES = (
    re.compile(r"(?:^|_)key$", re.IGNORECASE),
    re.compile(r"(?:^|_)token$", re.IGNORECASE),
    re.compile(r"^secret", re.IGNORECASE),
    re.compile(r"_secret$", re.IGNORECASE),
)
# Explicitly rejected key names (api_key/key_env/api_key_env and friends).
_EXPLICIT_REJECTED_KEYS: Tuple[str, ...] = (
    "api_key", "key_env", "api_key_env", "apikey", "secret", "token",
)


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


def _models_sidecar_path() -> Path:
    """Return ``<workspace>/hermes/models.yaml`` (canonical tracked state)."""
    return _workspace_root() / SIDECAR_DIR_NAME / MODELS_SIDECAR_NAME


def _allowlist_sidecar_root() -> Path:
    """Return ``<workspace>/hermes/command-allowlist``."""
    return _workspace_root() / SIDECAR_DIR_NAME / ALLOWLIST_SIDECAR_SUBDIR


def _allowlist_default_sidecar_path() -> Path:
    return _allowlist_sidecar_root() / DEFAULT_SIDECAR_NAME


def _allowlist_profiles_sidecar_dir() -> Path:
    return _allowlist_sidecar_root() / PROFILES_SUBDIR


def _migration_marker_path() -> Path:
    """Return ``<workspace>/hermes/command-allowlist/migration-v1.json``."""
    return _allowlist_sidecar_root() / MIGRATION_MARKER_NAME


def _template_config_path() -> Path:
    """Return the repo template config path (for rollback defaults).

    Honors the ``JOSEMAR_TEMPLATE_CONFIG`` env var; defaults to the
    container's mounted template path.
    """
    value = os.environ.get("JOSEMAR_TEMPLATE_CONFIG", "").strip()
    if value:
        return Path(value)
    return Path(DEFAULT_TEMPLATE_CONFIG_PATH)


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


def _resolve_state_path_for_hermes_home(hermes_home: Path, sidecar_root: Path) -> Path:
    """Shared mapping of a ``HERMES_HOME`` directory to its sidecar path.

    Mapping (relative to ``sidecar_root``):
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
        return sidecar_root / DEFAULT_SIDECAR_NAME

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
    return sidecar_root / PROFILES_SUBDIR / f"{canonical}.json"


def resolve_sidecar_for_hermes_home(hermes_home: Path) -> Path:
    """Map a ``HERMES_HOME`` directory to its skill-toggle sidecar path."""
    return _resolve_state_path_for_hermes_home(hermes_home, _sidecar_root())


def resolve_allowlist_sidecar_for_hermes_home(hermes_home: Path) -> Path:
    """Map a ``HERMES_HOME`` directory to its command-allowlist sidecar path."""
    return _resolve_state_path_for_hermes_home(hermes_home, _allowlist_sidecar_root())


def resolve_allowlist_sidecar_for_profile(profile: Optional[str]) -> Path:
    """Map a profile name (or None/empty for default) to its allowlist sidecar."""
    canonical = _canonical_profile_name(profile)
    if canonical == "default":
        return _allowlist_default_sidecar_path()
    return _allowlist_profiles_sidecar_dir() / f"{canonical}.json"


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
# Command-allowlist state (strict v1)
# ---------------------------------------------------------------------------
#
# Sidecar schema (exactly, one line, sorted/deduped non-empty strings):
#
#     {"version": 1, "command_allowlist": ["..."]}
#
# BOTH keys are required: a document missing ``command_allowlist`` is
# malformed (rejected), not an implicit empty allowlist. The runtime
# projection is the ROOT-LEVEL ``command_allowlist`` key. Presence
# semantics: an explicit empty list is authoritative (the runtime key is
# kept durably empty) while an ABSENT sidecar removes that key.
# Validation rejects a wrong version, unknown keys, wrong types, and
# empty/non-string entries. Error messages and statuses NEVER include
# allowlist contents. ``_canonical_allowlist_items`` is the single rule
# source shared by validation, runtime-config extraction, and the
# canonical text validator reused by workspace_sync.


def _canonical_allowlist_items(value: Any) -> List[str]:
    """Validate and canonicalize a raw allowlist value.

    Strict: must be a list whose entries are all non-empty strings (empty
    and whitespace-only entries are rejected, not silently dropped).
    Returns a sorted, deduped list. Raises ``ValueError`` otherwise; error
    messages identify the offending index but never include the value.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            f"'command_allowlist' must be a list of strings, got {type(value).__name__}"
        )
    seen: Set[str] = set()
    out: List[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"command_allowlist[{idx}]: must be a string")
        if not item.strip():
            raise ValueError(
                f"command_allowlist[{idx}]: must be a non-empty string"
            )
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return sorted(out)


def validate_command_allowlist_state(data: Any) -> Dict[str, Any]:
    """Validate a parsed command-allowlist sidecar against the strict v1 schema.

    Returns the canonical ``{"version": 1, "command_allowlist": [...]}``
    dict. BOTH keys are required: a document missing ``command_allowlist``
    is rejected rather than defaulted to empty. Raises ``ValueError`` on
    any violation (wrong version, missing keys, unknown keys, wrong types,
    empty/non-string entries) so callers can fail closed and leave the
    runtime config untouched.
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"command-allowlist sidecar root must be an object, got "
            f"{type(data).__name__}"
        )
    version = data.get("version")
    # Strict integer check (bool is an int subclass and must be rejected).
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != SCHEMA_VERSION
    ):
        raise ValueError(
            f"unsupported command-allowlist sidecar version: {version!r} "
            f"(expected {SCHEMA_VERSION})"
        )
    _reject_unknown_keys(
        data, {"version", "command_allowlist"}, "command-allowlist sidecar"
    )
    if "command_allowlist" not in data:
        raise ValueError(
            "command-allowlist sidecar must contain 'command_allowlist'"
        )
    raw_items = data["command_allowlist"]
    if raw_items is None:
        # Explicit JSON null is a wrong type, not an implicit empty list.
        raise ValueError(
            f"'command_allowlist' must be a list of strings, got "
            f"{type(raw_items).__name__}"
        )
    return {
        "version": SCHEMA_VERSION,
        "command_allowlist": _canonical_allowlist_items(raw_items),
    }


def serialize_command_allowlist_state(state: Dict[str, Any]) -> str:
    """Render a command-allowlist state dict as one canonical JSON line."""
    validated = validate_command_allowlist_state(state)
    return json.dumps(validated, separators=(",", ":"), ensure_ascii=False)


def validate_command_allowlist_state_from_text(text: str) -> Dict[str, Any]:
    """Parse and validate a command-allowlist sidecar from raw text.

    Canonical single validation entry point, reused by sidecar reads and
    by workspace_sync so the schema rules are never duplicated.
    Raises ``ValueError`` on empty content, malformed JSON, or any schema
    violation. Unlike models.yaml, an empty document is NOT valid: absence
    semantics are file-level (an absent file removes the runtime key), so
    an empty file is malformed, not a rollback signal.
    """
    if not text or not text.strip():
        raise ValueError("empty command-allowlist sidecar content")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid command-allowlist sidecar JSON: {exc}") from exc
    return validate_command_allowlist_state(data)


# ---------------------------------------------------------------------------
# One-time legacy migration-completion marker (strict v1, value-free)
# ---------------------------------------------------------------------------
#
# Marker schema (exactly, one line, no command values / profile names /
# timestamps / host identifiers or any other mutable metadata):
#
#     {"version": 1, "legacy_runtime_import_complete": true}
#
# BOTH keys are required and the marker root must contain no other keys. It
# is monotonic: presence means the one-time legacy runtime import has been
# finalized for this state history and ``config.yaml`` must never again be
# treated as a command-allowlist source. It is architecture metadata (safe to
# ship in the public template) and never holds private state. Errors are
# structural/value-free.


def validate_migration_marker(data: Any) -> Dict[str, Any]:
    """Validate a parsed migration marker against the strict v1 schema.

    Returns the canonical ``{"version": 1, "legacy_runtime_import_complete":
    true}`` dict. Both keys are required and no other keys are allowed.
    Raises ``ValueError`` on any violation (wrong version, missing keys,
    unknown keys, wrong types) so callers can fail closed. Errors are
    VALUE-FREE: no marker-controlled value (e.g. a wrong ``version``) and
    no unknown key NAME is ever included — only fixed schema key names and
    structural/type-level diagnostics.
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"migration marker root must be an object, got {type(data).__name__}"
        )
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        # Type-level diagnostic only: the marker-controlled value is
        # never echoed back.
        raise ValueError(
            "migration marker version must be an integer "
            f"(expected {MIGRATION_MARKER_VERSION})"
        )
    if version != MIGRATION_MARKER_VERSION:
        # Value-free: a malformed marker-controlled version must never
        # appear in the error text.
        raise ValueError(
            "unsupported migration marker version "
            f"(expected {MIGRATION_MARKER_VERSION})"
        )
    # Unknown keys are counted, never named: marker-controlled key names
    # must not flow into validation errors. Deliberately does NOT delegate
    # to _reject_unknown_keys, which formats the raw key names.
    extra_keys = set(data.keys()) - {"version", "legacy_runtime_import_complete"}
    if extra_keys:
        raise ValueError(
            f"migration marker: {len(extra_keys)} unknown key(s) present"
        )
    if "legacy_runtime_import_complete" not in data:
        raise ValueError(
            "migration marker must contain 'legacy_runtime_import_complete'"
        )
    if data["legacy_runtime_import_complete"] is not True:
        raise ValueError(
            "'legacy_runtime_import_complete' must be exactly true"
        )
    return {
        "version": MIGRATION_MARKER_VERSION,
        "legacy_runtime_import_complete": True,
    }


def serialize_migration_marker() -> str:
    """Render the canonical migration-marker JSON as one line."""
    state = {
        "version": MIGRATION_MARKER_VERSION,
        "legacy_runtime_import_complete": True,
    }
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False)


def validate_command_allowlist_migration_marker_from_text(text: str) -> Dict[str, Any]:
    """Parse and validate a migration marker from raw text.

    Canonical single validation entry point for the marker. Raises
    ``ValueError`` on empty content, malformed JSON, or any schema violation.
    Errors are value-free: no marker-controlled value or unknown key name is
    ever included (malformed-JSON errors carry position info only).
    """
    if not text or not text.strip():
        raise ValueError("empty migration marker content")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid migration marker JSON: {exc}") from exc
    return validate_migration_marker(data)


# Alias matching the documented W2 workspace_sync import surface.
validate_migration_marker_from_text = validate_command_allowlist_migration_marker_from_text


def read_migration_marker(path: Path) -> Optional[Dict[str, Any]]:
    """Read and validate the migration marker. ``None`` if the file is absent.

    A present but malformed/unreadable marker raises ``ValueError`` so callers
    fail closed before template overwrite. Errors are value-free.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read migration marker {path}: {exc}") from exc
    return validate_migration_marker_from_text(text)


def finalize_migration_marker() -> None:
    """Atomically write the one-time migration-completion marker under the lock.

    Validates the existing marker if present (a malformed present marker is
    fatal) and otherwise writes the canonical marker atomically. Called by
    the init script only after every existing default/named profile has been
    safely examined/migrated and BEFORE the template overwrite. A write
    failure raises so init aborts. Errors are value-free.
    """
    marker_path = _migration_marker_path()
    with SkillStateLock():
        # Validate a present marker first; malformed/unreadable is fatal.
        if marker_path.exists():
            read_migration_marker(marker_path)
            return
        _atomic_write(marker_path, serialize_migration_marker() + "\n")


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


def write_command_allowlist_sidecar(path: Path, state: Dict[str, Any]) -> None:
    """Atomically write a canonical command-allowlist sidecar to ``path``."""
    _atomic_write(path, serialize_command_allowlist_state(state))


def read_command_allowlist_sidecar(path: Path) -> Optional[Dict[str, Any]]:
    """Read and validate a command-allowlist sidecar. ``None`` if absent.

    Malformed sidecars raise ``ValueError`` so callers can surface a clear
    error (never containing allowlist contents) and leave the runtime
    config untouched.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"cannot read command-allowlist sidecar {path}: {exc}"
        ) from exc
    return validate_command_allowlist_state_from_text(text)


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


def config_has_command_allowlist(config: Dict[str, Any]) -> bool:
    """Return True if the config has a ROOT-LEVEL ``command_allowlist`` key."""
    return "command_allowlist" in config


def extract_command_allowlist_from_config(config: Dict[str, Any]) -> List[str]:
    """Project the runtime ROOT-LEVEL ``command_allowlist`` (canonical, strict).

    Raises ``ValueError`` on a malformed runtime value so migration and
    reconcile fail closed instead of persisting invalid state. Unrelated
    keys (including the whole ``skills`` section) are never consulted.
    """
    return _canonical_allowlist_items(config.get("command_allowlist"))


def apply_command_allowlist_to_config(
    config: Dict[str, Any], items: List[str]
) -> Dict[str, Any]:
    """Set the ROOT-LEVEL ``command_allowlist`` preserving every unrelated key.

    An explicit empty list is authoritative: the key is kept with ``[]``
    so the clear is durable (only an ABSENT sidecar removes the key).
    Top-level and ``skills`` sibling fields are never touched.
    """
    config["command_allowlist"] = list(items)
    return config


def remove_command_allowlist_from_config(config: Dict[str, Any]) -> bool:
    """Remove the ROOT-LEVEL ``command_allowlist`` (absent-sidecar semantics).

    Returns True if the key was present and removed. Every unrelated
    top-level and ``skills`` key is preserved.
    """
    if "command_allowlist" in config:
        del config["command_allowlist"]
        return True
    return False


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
# State-owned model authoring overlay (models.yaml)
# ---------------------------------------------------------------------------


def _is_secret_looking_key(key: str) -> bool:
    """Return True for keys that look like secrets (api_key, token, etc.)."""
    if key in _EXPLICIT_REJECTED_KEYS:
        return True
    return any(pattern.search(key) for pattern in _SECRET_KEY_RES)


def _validate_no_secret_keys(node: Any, path: str) -> None:
    """Recursively reject any secret-looking keys anywhere in ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise ValueError(f"{path}: non-string key {key!r}")
            if _is_secret_looking_key(key):
                raise ValueError(f"{path}: secret-looking key {key!r} is not allowed")
            _validate_no_secret_keys(value, f"{path}.{key}")
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _validate_no_secret_keys(item, f"{path}[{idx}]")


def _validate_str(value: Any, path: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path}: must be a string, got {type(value).__name__}")
    if required and not value.strip():
        raise ValueError(f"{path}: must be a non-empty string")
    return value


def _reject_unknown_keys(node: Dict[str, Any], allowed: Set[str], path: str) -> None:
    extra = set(node.keys()) - allowed
    if extra:
        raise ValueError(f"{path}: unknown key(s) {sorted(extra)!r}")


def _validate_root_model(node: Any) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"model: must be a mapping, got {type(node).__name__}")
    # Strict selection-only: only provider + default.
    _reject_unknown_keys(node, set(MODEL_SELECTION_KEYS), "model")
    _validate_str(node.get("provider"), "model.provider", required=True)
    _validate_str(node.get("default"), "model.default", required=True)


def _validate_fallback_entry(node: Any, path: str) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"{path}: must be a mapping, got {type(node).__name__}")
    # Strict selection-only: only provider + model.
    _reject_unknown_keys(node, set(FALLBACK_SELECTION_KEYS), path)
    _validate_str(node.get("provider"), f"{path}.provider", required=True)
    _validate_str(node.get("model"), f"{path}.model", required=True)


def _validate_fallback_providers(node: Any) -> None:
    if node is None:
        return
    if not isinstance(node, list):
        raise ValueError(f"fallback_providers: must be a list, got {type(node).__name__}")
    for idx, entry in enumerate(node):
        _validate_fallback_entry(entry, f"fallback_providers[{idx}]")


def _validate_auxiliary_task(node: Any, task: str) -> None:
    path = f"auxiliary.{task}"
    if not isinstance(node, dict):
        raise ValueError(f"{path}: must be a mapping, got {type(node).__name__}")
    # Strict selection-only: only provider + model. Template-owned sibling
    # fields (api_key, download_timeout, base_url, etc.) are NOT allowed
    # here — they stay in config.yaml and are preserved by deep merge.
    _reject_unknown_keys(node, set(AUXILIARY_SELECTION_KEYS), path)
    provider = _validate_str(node.get("provider"), f"{path}.provider", required=True)
    model = _validate_str(node.get("model"), f"{path}.model")
    if provider == "auto":
        # Auto provider routing: upstream selects the model, so model must
        # be exactly the empty string. A non-empty model is rejected.
        if model != "":
            raise ValueError(
                f"{path}.model: must be exactly '' when provider is 'auto'"
            )
    else:
        # Every non-auto provider requires a non-empty model.
        _validate_str(model, f"{path}.model", required=True)


def _validate_auxiliary(node: Any) -> None:
    if node is None:
        return
    if not isinstance(node, dict):
        raise ValueError(f"auxiliary: must be a mapping, got {type(node).__name__}")
    _reject_unknown_keys(node, set(ALLOWED_AUXILIARY_TASKS), "auxiliary")
    for task in ALLOWED_AUXILIARY_TASKS:
        if task in node:
            _validate_auxiliary_task(node[task], task)


def _validate_cron(node: Any) -> None:
    if node is None:
        return
    if not isinstance(node, dict):
        raise ValueError(f"cron: must be a mapping, got {type(node).__name__}")
    # Only model + model_provider (blank = inherit default).
    _reject_unknown_keys(node, set(CRON_SELECTION_KEYS), "cron")
    if "model" in node:
        _validate_str(node["model"], "cron.model")
    if "model_provider" in node:
        _validate_str(node["model_provider"], "cron.model_provider")


def validate_models_state(data: Any) -> Dict[str, Any]:
    """Validate a parsed models.yaml document against the strict v1 schema.

    Returns the validated dict. Raises ``ValueError`` on any violation so
    callers can surface a clear error and leave the runtime config untouched
    (fail-closed). Rejects unknown keys, secret-looking keys, forbidden
    fields (base_url/api_mode/extra_body/timeouts/fallback_chain/credentials),
    and invalid shapes anywhere in the document.
    """
    if not isinstance(data, dict):
        raise ValueError(f"models.yaml: must be a mapping, got {type(data).__name__}")
    if data.get("version") != MODELS_SCHEMA_VERSION:
        raise ValueError(
            f"models.yaml: unsupported version {data.get('version')!r} "
            f"(expected {MODELS_SCHEMA_VERSION})"
        )
    # Reject unknown top-level keys.
    _reject_unknown_keys(
        data, {"version", "model", "fallback_providers", "auxiliary", "cron"},
        "models.yaml",
    )
    # Reject secret-looking keys anywhere in the document (deep scan).
    _validate_no_secret_keys(data, "models.yaml")
    if "model" in data:
        _validate_root_model(data["model"])
    _validate_fallback_providers(data.get("fallback_providers"))
    _validate_auxiliary(data.get("auxiliary"))
    _validate_cron(data.get("cron"))
    return data


def load_models_state(path: Path) -> Optional[Dict[str, Any]]:
    """Load and validate ``models.yaml``. Returns ``None`` if absent.

    Malformed YAML or schema violations raise ``ValueError`` so callers can
    surface a clear error and leave the runtime config untouched. An empty
    file (YAML null) returns ``None`` (rollback semantics: restore template
    defaults).
    """
    if not path.exists():
        return None
    return validate_models_state_from_text(path.read_text(encoding="utf-8"))


def validate_models_state_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Parse and validate a models.yaml document from raw text.

    Returns the validated dict, or ``None`` for an empty document (YAML
    null). Raises ``ValueError`` on malformed YAML or schema violations.
    This is the canonical single validation entry point reused by
    workspace_sync (local staging + remote candidate validation) so the
    rules are never duplicated.
    """
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required for the models overlay but is not available "
            "in this Python environment"
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"models.yaml: invalid YAML: {exc}") from exc
    if data is None:
        return None
    return validate_models_state(data)


def _merge_fallback_by_provider(
    state_entries: List[Dict[str, Any]],
    existing_entries: List[Any],
) -> List[Dict[str, Any]]:
    """Provider-matched fallback merge with consumed-entry semantics.

    For each state entry (in order), find the first unconsumed existing
    entry with the SAME provider. If found, preserve that existing entry's
    runtime-only sibling fields (base_url/api_mode/api_key/extra_body) and
    update only the state-owned provider/model. If no match (new provider,
    duplicate beyond available count, or existing list exhausted), create a
    minimal ``{provider, model}`` dict — runtime-only siblings are NEVER
    transferred from one provider to another. Existing entries beyond the
    state list length are dropped (the overlay owns the full chain length).

    This handles duplicate providers (each state entry consumes one
    matching existing entry) and reordering (siblings follow the provider
    match, not the index).
    """
    # Build a list of (index, entry) for consumable existing entries.
    consumable: List[Tuple[int, Dict[str, Any]]] = [
        (i, e) for i, e in enumerate(existing_entries) if isinstance(e, dict)
    ]
    new_fb: List[Dict[str, Any]] = []
    for state_entry in state_entries:
        state_provider = state_entry.get("provider")
        # Find the first unconsumed existing entry with the same provider.
        match_idx: Optional[int] = None
        for ci, (orig_idx, existing_entry) in enumerate(consumable):
            if existing_entry.get("provider") == state_provider:
                match_idx = ci
                break
        if match_idx is not None:
            _, existing_entry = consumable.pop(match_idx)
            merged = dict(existing_entry)
            # Update only state-owned keys present in the state entry.
            for key in FALLBACK_SELECTION_KEYS:
                if key in state_entry:
                    merged[key] = state_entry[key]
            new_fb.append(merged)
        else:
            # No matching provider — minimal dict with only state-owned
            # keys present in the state entry. No sibling transfer.
            minimal = {k: state_entry[k] for k in FALLBACK_SELECTION_KEYS if k in state_entry}
            new_fb.append(minimal)
    return new_fb


def apply_models_to_config(
    config: Dict[str, Any], models: Dict[str, Any]
) -> bool:
    """Merge a validated models.yaml state into a runtime config dict.

    Overlays ``model.{provider,default}``, ``fallback_providers`` (full
    list replacement — the overlay owns the chain), ``auxiliary.<task>``
    (deep merge — only provider/model are set, template-owned sibling
    fields like api_key/download_timeout are preserved), and
    ``cron.{model,model_provider}`` onto ``config``, preserving every
    unrelated key. Returns True if any key was changed. Only the keys
    present in ``models`` are written; absent keys in ``models`` do NOT
    clear the corresponding config key (overlay semantics, not full
    replacement). Rollback (absent/empty models) is handled by
    :func:`restore_template_models_defaults`, not this function.
    """
    changed = False

    def _set_nested(path: Tuple[str, ...], value: Any) -> None:
        nonlocal changed
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

    if "model" in models:
        model = models["model"]
        _set_nested(("model", "provider"), model["provider"])
        _set_nested(("model", "default"), model["default"])

    if "fallback_providers" in models:
        fb = models["fallback_providers"]
        if fb is None:
            fb = []
        # Provider-matched merge: preserve runtime-only sibling fields
        # (base_url/api_mode/api_key/extra_body) ONLY when the state entry's
        # provider matches an existing entry's provider. This prevents
        # transferring credentials/base_url from one provider to another.
        # A consumed-entry mechanism handles duplicate providers and
        # reordering: each existing entry is consumed at most once. State
        # entries with no matching unconsumed existing entry (new provider,
        # or a duplicate beyond the available count) become minimal
        # {provider, model} dicts. Existing entries beyond the state list
        # length are removed (the overlay owns the full chain length).
        existing = config.get("fallback_providers")
        if not isinstance(existing, list):
            existing = []
        new_fb = _merge_fallback_by_provider(fb, existing)
        if config.get("fallback_providers") != new_fb:
            config["fallback_providers"] = new_fb
            changed = True

    if "auxiliary" in models and models["auxiliary"] is not None:
        for task in ALLOWED_AUXILIARY_TASKS:
            if task in models["auxiliary"]:
                task_state = models["auxiliary"][task]
                # Deep merge: set only provider/model, preserve sibling
                # template-owned fields (api_key, download_timeout, etc.).
                _set_nested(("auxiliary", task, "provider"), task_state["provider"])
                _set_nested(("auxiliary", task, "model"), task_state["model"])

    if "cron" in models and models["cron"] is not None:
        cron = models["cron"]
        if "model" in cron:
            _set_nested(("cron", "model"), cron["model"])
        if "model_provider" in cron:
            _set_nested(("cron", "model_provider"), cron["model_provider"])

    return changed


def restore_template_models_defaults(
    config: Dict[str, Any], template_config: Dict[str, Any]
) -> bool:
    """Restore state-owned model keys to repo template defaults.

    Used when models.yaml is absent/empty (rollback). Resets only the
    state-owned selection keys to the template values, preserving every
    unrelated runtime key. When the template does not define a state-owned
    key, that key is removed from the runtime config (restored to absent,
    which is the template default). Returns True if any key was changed.

    State-owned keys (the overlay owns these; rollback resets them):
      - model.provider, model.default
      - fallback_providers (full list)
      - auxiliary.<task>.provider, auxiliary.<task>.model (per allowlisted task)
      - cron.model, cron.model_provider
    Template-owned sibling fields (api_key, download_timeout, base_url, etc.)
    are preserved — only the selection keys above are reset.
    """
    changed = False

    def _set_nested(path: Tuple[str, ...], value: Any) -> None:
        nonlocal changed
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

    def _del_nested(path: Tuple[str, ...]) -> None:
        """Remove a nested key if present (restore to absent)."""
        nonlocal changed
        node: Any = config
        for key in path[:-1]:
            if not isinstance(node, dict) or key not in node:
                return
            node = node[key]
        if isinstance(node, dict) and path[-1] in node:
            del node[path[-1]]
            changed = True

    # model.provider, model.default
    tmpl_model = template_config.get("model")
    if isinstance(tmpl_model, dict):
        for key in MODEL_SELECTION_KEYS:
            if key in tmpl_model:
                _set_nested(("model", key), tmpl_model[key])
            else:
                _del_nested(("model", key))
    else:
        # Template has no model section — remove state-owned model keys.
        for key in MODEL_SELECTION_KEYS:
            _del_nested(("model", key))
    # fallback_providers (provider-matched merge; absent in template = remove)
    # Restore template state-owned selection (provider/model) while
    # preserving runtime-only sibling fields (base_url/api_mode/api_key/
    # extra_body) ONLY when the template entry's provider matches an
    # existing entry's provider. This prevents transferring credentials/
    # base_url from one provider to another during rollback. Uses the
    # same consumed-entry mechanism as apply (handles duplicates and
    # reordering). Template entries with no match become minimal dicts.
    # Existing entries beyond the template list length are removed.
    tmpl_fb = template_config.get("fallback_providers")
    if tmpl_fb is not None:
        existing_fb = config.get("fallback_providers")
        if not isinstance(existing_fb, list):
            existing_fb = []
        # Filter template entries to dicts with provider/model keys for
        # the merge; the rollback restores only state-owned keys.
        tmpl_entries: List[Dict[str, Any]] = [
            e for e in tmpl_fb if isinstance(e, dict)
        ]
        new_fb = _merge_fallback_by_provider(tmpl_entries, existing_fb)
        # For matched entries, _merge_fallback_by_provider already set
        # provider/model from the template entry. For unmatched entries
        # it created minimal {provider, model} dicts. But rollback must
        # also handle the case where a template entry lacks a state-owned
        # key (e.g. template has provider but no model) — remove that key
        # from the merged entry. Re-process to enforce template key set.
        # Build a provider->template-keys map for the removal pass.
        tmpl_key_sets: Dict[str, set] = {}
        for te in tmpl_entries:
            p = te.get("provider")
            if p is not None:
                tmpl_key_sets.setdefault(p, set()).update(
                    k for k in FALLBACK_SELECTION_KEYS if k in te
                )
        for entry in new_fb:
            p = entry.get("provider")
            if p is None:
                continue
            allowed = tmpl_key_sets.get(p, set(FALLBACK_SELECTION_KEYS))
            for key in FALLBACK_SELECTION_KEYS:
                if key not in allowed and key in entry:
                    del entry[key]
        if config.get("fallback_providers") != new_fb:
            config["fallback_providers"] = new_fb
            changed = True
    else:
        if "fallback_providers" in config:
            del config["fallback_providers"]
            changed = True
    # auxiliary.<task>.provider/model (deep merge — preserve siblings)
    tmpl_aux = template_config.get("auxiliary")
    for task in ALLOWED_AUXILIARY_TASKS:
        tmpl_task = tmpl_aux.get(task) if isinstance(tmpl_aux, dict) else None
        if isinstance(tmpl_task, dict):
            for key in AUXILIARY_SELECTION_KEYS:
                if key in tmpl_task:
                    _set_nested(("auxiliary", task, key), tmpl_task[key])
                else:
                    _del_nested(("auxiliary", task, key))
        else:
            for key in AUXILIARY_SELECTION_KEYS:
                _del_nested(("auxiliary", task, key))
    # cron.model, cron.model_provider
    tmpl_cron = template_config.get("cron")
    if isinstance(tmpl_cron, dict):
        for key in CRON_SELECTION_KEYS:
            if key in tmpl_cron:
                _set_nested(("cron", key), tmpl_cron[key])
            else:
                _del_nested(("cron", key))
    else:
        for key in CRON_SELECTION_KEYS:
            _del_nested(("cron", key))
    return changed


def apply_models_overlay(
    config_path: Path,
    *,
    template_config_path: Optional[Path] = None,
) -> str:
    """Apply the models.yaml overlay to ``config_path`` under the shared lock.

    Loads ``<workspace>/hermes/models.yaml``, validates it fully, and only
    then merges it into the runtime config at ``config_path``. On any
    validation failure the config is left untouched (fail-closed — no
    mutation until complete validation succeeds).

    When models.yaml is absent/empty (rollback), state-owned keys are
    restored to the repo template defaults from ``template_config_path``
    when provided, preserving unrelated runtime keys. When no template
    path is provided, rollback is a no-op (the init template-copy step
    already restored defaults).

    Returns a short status string for logging:
      - ``applied-models``: overlay applied
      - ``no-models-sidecar``: absent and no template path (no-op)
      - ``restored-template-defaults``: absent, restored from template
    """
    models_path = _models_sidecar_path()
    models = load_models_state(models_path)  # raises on malformed
    if models is None:
        # Rollback: restore state-owned keys to template defaults.
        if template_config_path is not None and template_config_path.exists():
            template_config = _load_yaml(template_config_path)
            if not isinstance(template_config, dict):
                raise ValueError(
                    f"{template_config_path} does not contain a YAML mapping"
                )
            config = _load_yaml(config_path) if config_path.exists() else {}
            if not isinstance(config, dict):
                raise ValueError(f"{config_path} does not contain a YAML mapping")
            restore_template_models_defaults(config, template_config)
            _dump_yaml(config_path, config)
            return "restored-template-defaults"
        return "no-models-sidecar"

    config = _load_yaml(config_path) if config_path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError(f"{config_path} does not contain a YAML mapping")

    apply_models_to_config(config, models)
    _dump_yaml(config_path, config)
    return "applied-models"


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


def save_command_allowlist_stateful(
    config: Dict[str, Any],
    commands: List[str],
) -> None:
    """Stateful replacement for a raw runtime command-allowlist save.

    ``commands`` must be a REAL list (strict schema input): ``str``,
    ``tuple``, ``set``, generators, and any other non-list type are
    rejected BEFORE any sidecar or runtime write. The W2 permanent
    approval path explicitly converts its set to ``list`` first.

    Validates and canonicalizes ``commands``, sets the ROOT-LEVEL
    ``config["command_allowlist"]``, then — under one advisory lock shared
    with the toggle writes and reconcile — atomically writes the allowlist
    sidecar FIRST and only then invokes the native raw ``save_config``
    with ``preserve_keys={("command_allowlist",)}`` (pinned upstream
    signature) so an explicit empty ``[]`` survives upstream persistence
    (SET path only; the clear path never preserves).

    Failure ordering (fixed, do not redesign):
      - A state (sidecar) write failure propagates BEFORE the runtime
        config is touched, so the caller's save fails and runtime state
        never silently diverges from tracked state.
      - If the native runtime save fails afterward, the sidecar is
        already durable; the error propagates and the next reconcile
        cycle repairs the runtime config from the sidecar.
    """
    if not isinstance(commands, list):
        raise ValueError(
            "commands must be a real list of strings (str/tuple/set/"
            "generators are rejected; convert sets with list(...) explicitly)"
        )
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")
    items = _canonical_allowlist_items(commands)
    config["command_allowlist"] = items
    sidecar = resolve_allowlist_sidecar_for_hermes_home(_active_hermes_home())
    state = {"version": SCHEMA_VERSION, "command_allowlist": items}
    with SkillStateLock():
        write_command_allowlist_sidecar(sidecar, state)
        _native_save_config(
            config, preserve_keys=_stateful_allowlist_preserve_keys()
        )


def clear_command_allowlist_stateful(config: Dict[str, Any]) -> None:
    """Stateful exact-key unset of the runtime command allowlist (W2).

    Under one advisory lock shared with the toggle writes and reconcile:
    deletes the allowlist sidecar FIRST (an absent sidecar is a no-op
    success), then removes ONLY the root-level ``command_allowlist`` key
    from the passed raw config and persists it via the native save helper.
    Unlike the SET path, NO ``preserve_keys`` argument is passed: the
    key must be dropped from the saved config.

    Failure ordering (fixed, do not redesign):
      - A state-deletion failure propagates before the runtime config is
        persisted, so the clear fails without diverging tracked state.
      - If the native runtime save fails afterward, the sidecar is
        already deleted; the error propagates and the next reconcile
        cycle repairs the runtime config (the absent-sidecar rule removes
        the key; migration can never resurrect it — non-empty value only,
        absent sidecar only).
    """
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")
    sidecar = resolve_allowlist_sidecar_for_hermes_home(_active_hermes_home())
    with SkillStateLock():
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        remove_command_allowlist_from_config(config)
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


def _native_save_config(
    config: Dict[str, Any],
    *,
    preserve_keys: Optional[Set[Tuple[str, ...]]] = None,
) -> None:
    """Invoke the upstream ``save_config`` from ``hermes_cli.config``.

    Pinned upstream signature (Hermes v2026.8.31)::

        save_config(config, *, strip_defaults=True,
                    preserve_keys: Optional[Set[Tuple[str, ...]]] = None,
                    merge_existing=False)

    ``preserve_keys`` names root-relative key paths (a set of tuples) that
    upstream ``save_config`` must keep even when they hold default/empty
    values (it otherwise strips such keys). Only the stateful
    command-allowlist SET path passes it (``{("command_allowlist",)}``), so
    an explicit ``command_allowlist: []`` is durably persisted; the UNSET
    (clear) path and the toggle path never pass it — there the key must be
    dropped from the saved config / nothing needs preserving.
    """
    from hermes_cli.config import save_config  # type: ignore

    if preserve_keys is not None:
        save_config(config, preserve_keys=preserve_keys)
        return
    save_config(config)


def _stateful_allowlist_preserve_keys() -> Set[Tuple[str, ...]]:
    """Root key paths preserved through the stateful SET persistence path."""
    return {("command_allowlist",)}


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


def migrate_existing_command_allowlist_to_absent_sidecar(
    config_path: Path, hermes_home: Path
) -> bool:
    """Extract a non-empty runtime command_allowlist into an absent sidecar.

    Used BEFORE the repo template overwrites the runtime config so a
    pre-feature deployment's allowlist survives the upgrade. Migrates a
    NON-EMPTY runtime value only: an absent or empty runtime allowlist
    must NOT create an explicit-empty sidecar (presence is authoritative).
    Never overwrites an existing sidecar. Returns True if a sidecar was
    created.

    If the one-time migration marker is present, legacy runtime import has
    been finalized: this function is a no-op (returns False) so stale
    runtime values can never resurrect a deliberately deleted sidecar. This
    is the authoritative marker gate, independent of any caller-level guard.
    """
    if read_migration_marker(_migration_marker_path()) is not None:
        # Legacy import already finalized — never re-read stale runtime state.
        return False
    if not config_path.exists():
        return False
    config = _load_yaml(config_path)
    if not config_has_command_allowlist(config):
        return False
    items = extract_command_allowlist_from_config(config)
    if not items:
        return False
    sidecar = resolve_allowlist_sidecar_for_hermes_home(hermes_home)
    if sidecar.exists():
        # Sidecar already present — never overwrite (e.g. a named profile
        # that was already migrated, or state that predates this run).
        return False
    write_command_allowlist_sidecar(
        sidecar, {"version": SCHEMA_VERSION, "command_allowlist": items}
    )
    return True


def apply_sidecar_and_enforce_policy(
    config_path: Path, hermes_home: Path
) -> str:
    """Apply toggle sidecar + command-allowlist state and enforce policy.

    Applies the skill-toggle sidecar when present, applies the
    command-allowlist state for ``hermes_home``, and enforces the Josemar
    policy — one config load, one write. Allowlist presence semantics: a
    present sidecar is authoritative (canonical items written to the
    ROOT-LEVEL ``command_allowlist`` key); an ABSENT sidecar removes that
    key while an explicit-empty sidecar keeps it durably empty. Preserves
    all unrelated top-level and ``skills`` config keys. Malformed sidecars
    (either family) leave the config untouched and surface a clear error.
    Returns a short status string for logging that never includes
    allowlist contents.
    """
    sidecar = resolve_sidecar_for_hermes_home(hermes_home)
    allowlist_sidecar = resolve_allowlist_sidecar_for_hermes_home(hermes_home)
    config = _load_yaml(config_path) if config_path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError(f"{config_path} does not contain a YAML mapping")

    # Validate both sidecar families fully BEFORE mutating the config
    # (fail-closed: no partial apply).
    state: Optional[Dict[str, Any]] = None
    if sidecar.exists():
        state = read_sidecar(sidecar)  # raises on malformed
    allowlist_items: Optional[List[str]] = None
    if allowlist_sidecar.exists():
        allowlist_state = read_command_allowlist_sidecar(allowlist_sidecar)
        assert allowlist_state is not None  # sidecar exists; read returns dict
        allowlist_items = allowlist_state["command_allowlist"]

    if state is not None:
        apply_state_to_config(config, state)

    # Command-allowlist state application (absent sidecar removes the key).
    allowlist_suffix = ""
    if allowlist_items is None:
        if remove_command_allowlist_from_config(config):
            allowlist_suffix = "+command-allowlist-removed"
    else:
        if config.get("command_allowlist") != allowlist_items:
            apply_command_allowlist_to_config(config, allowlist_items)
            allowlist_suffix = "+command-allowlist"

    policy_changed = enforce_policy(config)

    if state is None and not policy_changed and not allowlist_suffix:
        return "no-sidecar-no-policy-change"
    _dump_yaml(config_path, config)
    if state is not None and policy_changed:
        base = "applied-sidecar-and-policy"
    elif state is not None:
        base = "applied-sidecar"
    else:
        base = "enforced-policy"
    return base + allowlist_suffix


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


def _prevalidate_command_allowlist_sidecars_unlocked() -> None:
    """Validate every present command-allowlist sidecar; raise before writes.

    Fail-closed pre-pass for an apply cycle: every present allowlist
    sidecar (default + every canonically named profile, including sidecars
    whose profile config is currently absent) and the migration marker (if
    present) is fully validated BEFORE any runtime config is written in that
    cycle, so a malformed sidecar/marker can never produce a partial apply.
    Must be called while holding :class:`SkillStateLock`. Validation errors
    never include allowlist contents. Non-canonical filenames are skipped
    (they cannot map to a reconcilable profile).
    """
    marker = _migration_marker_path()
    if marker.exists():
        read_migration_marker(marker)  # raises on malformed/unreadable
    default_sidecar = _allowlist_default_sidecar_path()
    if default_sidecar.exists():
        read_command_allowlist_sidecar(default_sidecar)
    profiles_dir = _allowlist_profiles_sidecar_dir()
    if profiles_dir.is_dir():
        for sidecar in sorted(profiles_dir.glob("*.json")):
            if not _PROFILE_ID_RE.match(sidecar.stem):
                continue
            read_command_allowlist_sidecar(sidecar)


def _apply_all_sidecars_and_policy_unlocked() -> List[str]:
    """Unlocked internal apply: reconcile every reconcilable profile config.

    Fail-closed first: every present command-allowlist sidecar is
    prevalidated BEFORE any runtime config write in this cycle. Then, for
    each profile with a ``config.yaml``: apply its toggle sidecar and
    command-allowlist state when present (an absent allowlist sidecar
    removes the runtime key) and enforce the Josemar policy. Named
    profiles without a toggle sidecar still get allowlist reconciliation
    and policy enforcement. Orphan toggle sidecars (no matching config)
    are reported. Must be called while holding :class:`SkillStateLock`
    (or inside a critical section that already holds it) to avoid racing
    with dashboard writes.
    """
    statuses: List[str] = []
    workspace = _workspace_root()
    # Fail-closed: validate every present command-allowlist sidecar BEFORE
    # any runtime config write in this cycle (toggle/policy/models applies
    # below must never run against unvalidated allowlist state).
    _prevalidate_command_allowlist_sidecars_unlocked()
    reconciled = {canonical: hermes_home for canonical, hermes_home, _ in _iter_reconcilable_profiles()}

    for canonical, hermes_home, config_path in _iter_reconcilable_profiles():
        try:
            status = apply_sidecar_and_enforce_policy(config_path, hermes_home)
        except ValueError as exc:
            statuses.append(f"{canonical}:error:{exc}")
            continue
        statuses.append(f"{canonical}:{status}")

    # Apply the state-owned model authoring overlay (models.yaml) to the
    # default profile config only — root-only model selection, no named
    # profile multiplexing. Layered AFTER sidecar+policy so the operator's
    # model choices win over the template defaults. Fail-closed: a
    # malformed models.yaml raises ValueError (propagated to make apply-all
    # fail nonzero) and leaves the runtime config untouched (last-known-good
    # preserved — no mutation until complete validation succeeds). When
    # models.yaml is absent/empty, state-owned keys are restored to the
    # repo template defaults (rollback), preserving unrelated runtime keys.
    default_config = workspace / "config.yaml"
    if default_config.exists():
        models_status = apply_models_overlay(
            default_config, template_config_path=_template_config_path()
        )
        statuses.append(f"models:{models_status}")

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
    command's exit status is preserved exactly when sync fails. When sync
    succeeds but apply fails (including a models overlay validation
    failure), the returned exit status is nonzero (``1``) so the cron run
    fails closed — invalid model state must not silently boot the template
    configuration. The apply failure is captured in ``apply_statuses`` (each
    entry carries an ``error:`` segment). The runtime config is left
    untouched (last-known-good preserved) because the overlay validates
    fully before mutating.

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
            # Apply failure (including models overlay validation failure)
            # must fail the cron run nonzero. The runtime config is left
            # untouched (last-known-good preserved) because the overlay
            # validates fully before mutating.
            return 1, [f"apply:error:{exc}"], sync_output
        # If any apply status carries an error segment, fail nonzero.
        if any(":error:" in s for s in statuses):
            return 1, statuses, sync_output
        return 0, statuses, sync_output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_migrate(args: argparse.Namespace) -> int:
    hermes_home = Path(args.hermes_home) if args.hermes_home else _workspace_root()
    config_path = Path(args.config_path) if args.config_path else hermes_home / "config.yaml"
    # Toggle-key migration keeps the historical non-fatal behavior: ANY
    # ordinary failure (including a sidecar-write OSError) is warned about
    # and migration continues. KeyboardInterrupt/SystemExit are NOT caught.
    created = False
    try:
        created = migrate_existing_toggles_to_absent_sidecars(config_path, hermes_home)
    except Exception as exc:  # noqa: BLE001 - ordinary toggle failures are non-fatal
        print(f"migrate: warning: toggle migration failed: {exc}", file=sys.stderr)
    # Command-allowlist migration runs INDEPENDENTLY of the toggle path: a
    # toggle failure must not mask a command-allowlist failure, and a
    # command-allowlist-safe config with a toggle-only failure must not make
    # startup unavailable.
    #
    # Fail-closed: a command-allowlist migration failure must return nonzero
    # so the init caller aborts BEFORE the template overwrite (the template
    # copy would otherwise erase a non-empty pre-feature runtime allowlist).
    # Error text is sanitized/structural/value-free: it never includes
    # allowlist contents or raw config/YAML content.
    #
    # If the one-time migration marker is present, legacy runtime import is
    # already finalized: skip it for this profile (the shell gates the whole
    # loop on marker status, but this keeps the CLI defensively correct).
    created_allowlist = False
    try:
        if read_migration_marker(_migration_marker_path()) is not None:
            created_allowlist = False
        else:
            created_allowlist = migrate_existing_command_allowlist_to_absent_sidecar(
                config_path, hermes_home
            )
    except ValueError as exc:
        print(
            f"migrate: error: command-allowlist migration failed: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - ordinary failure is fatal, value-free
        print(
            f"migrate: error: command-allowlist migration failed: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print(
        f"migrate: {'created' if (created or created_allowlist) else 'no-op'} {config_path}"
    )
    return 0


def _cli_marker_present(args: argparse.Namespace) -> int:
    """Exit 0 if the one-time migration marker is present and valid.

    A malformed/unreadable present marker is fatal (exit 2) so the init
    script fails closed before template overwrite. Exit 1 means the marker
    is absent (legacy runtime import still pending).
    """
    try:
        present = read_migration_marker(_migration_marker_path()) is not None
    except ValueError as exc:
        print(f"marker-present: error: {exc}", file=sys.stderr)
        return 2
    return 0 if present else 1


def _cli_finalize_migration_marker(args: argparse.Namespace) -> int:
    """Write/validate the one-time migration-completion marker (fatal on error)."""
    try:
        finalize_migration_marker()
    except ValueError as exc:
        print(f"finalize-migration-marker: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # A marker write failure must be a clean fatal (nonzero) so the
        # init caller aborts BEFORE the template overwrite; never leak
        # allowlist contents (the marker holds none).
        print(f"finalize-migration-marker: error: {exc}", file=sys.stderr)
        return 2
    print(f"finalize-migration-marker: {'created' if _migration_marker_path().exists() else 'present'}")
    return 0


def _cli_apply(args: argparse.Namespace) -> int:
    hermes_home = Path(args.hermes_home) if args.hermes_home else _workspace_root()
    config_path = Path(args.config_path) if args.config_path else hermes_home / "config.yaml"
    # Stateful runtime-write invariant: this one-profile reconcile mutates
    # the runtime config, so it holds the same shared advisory lock as
    # apply-all, sync-and-apply, and the dashboard stateful writes.
    with SkillStateLock():
        status = apply_sidecar_and_enforce_policy(config_path, hermes_home)
    print(f"apply: {status} {config_path}")
    return 0


def _cli_apply_all(args: argparse.Namespace) -> int:
    try:
        statuses = apply_all_sidecars_and_policy()
    except ValueError as exc:
        # Models overlay validation failure must fail nonzero (fail-closed).
        print(f"apply-all: error: {exc}", file=sys.stderr)
        return 1
    if not statuses:
        print("apply-all: no profiles to reconcile")
    for entry in statuses:
        print(f"apply-all: {entry}")
    # Any error segment in statuses means fail-closed nonzero.
    if any(":error:" in s for s in statuses):
        return 1
    return 0


def _cli_apply_models(args: argparse.Namespace) -> int:
    hermes_home = Path(args.hermes_home) if args.hermes_home else _workspace_root()
    # Root-only: reject named profiles or any hermes home other than the
    # workspace root. The overlay does not support profile multiplexing.
    workspace = _workspace_root()
    try:
        home_resolved = hermes_home.resolve()
        workspace_resolved = workspace.resolve()
    except OSError as exc:
        print(f"apply-models: error: cannot resolve paths: {exc}", file=sys.stderr)
        return 2
    if home_resolved != workspace_resolved:
        print(
            "apply-models: error: models overlay is root-only; "
            "rejecting named profile or non-workspace hermes home",
            file=sys.stderr,
        )
        return 2
    # Root-only config path: --config-path must be exactly
    # <workspace-root>/config.yaml. Reject profile, arbitrary, symlink,
    # and path-alias config paths so the overlay never writes outside the
    # workspace root. The check is on the LITERAL/non-resolved path (not
    # just resolved equivalence) so a symlink that resolves to the root
    # config is still rejected.
    expected_config = workspace / "config.yaml"
    config_path = Path(args.config_path) if args.config_path else expected_config
    # Literal path check: the non-resolved string must equal the expected
    # path. This rejects path aliases and relative paths that resolve to
    # the same target but are not the canonical literal path.
    if str(config_path) != str(expected_config):
        print(
            "apply-models: error: --config-path must be exactly "
            f"{expected_config} for the root-only overlay; rejecting "
            f"{config_path}",
            file=sys.stderr,
        )
        return 2
    # Symlink check: reject if the config path (or any parent component)
    # is a symlink. A symlink that resolves to the root config is still
    # rejected — do not rely only on resolved equivalence.
    try:
        if config_path.is_symlink():
            print(
                "apply-models: error: --config-path must not be a symlink; "
                f"rejecting {config_path}",
                file=sys.stderr,
            )
            return 2
    except OSError as exc:
        print(f"apply-models: error: cannot stat config path: {exc}", file=sys.stderr)
        return 2
    # Also resolve to confirm the final target matches the workspace root
    # config (catches parent-directory symlinks and other aliases).
    try:
        config_resolved = config_path.resolve()
        expected_resolved = expected_config.resolve()
    except OSError as exc:
        print(f"apply-models: error: cannot resolve config path: {exc}", file=sys.stderr)
        return 2
    if config_resolved != expected_resolved:
        print(
            "apply-models: error: --config-path must resolve to "
            f"{expected_config} for the root-only overlay; rejecting "
            f"{config_path}",
            file=sys.stderr,
        )
        return 2
    template_path = Path(args.template_config) if args.template_config else _template_config_path()
    with SkillStateLock():
        status = apply_models_overlay(config_path, template_config_path=template_path)
    print(f"apply-models: {status} {config_path}")
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
        help="Extract existing toggle/allowlist keys into absent sidecars (init pre-overwrite).",
    )
    p_migrate.add_argument("--hermes-home", default=None)
    p_migrate.add_argument("--config-path", default=None)
    p_migrate.set_defaults(func=_cli_migrate)

    p_marker_present = sub.add_parser(
        "marker-present",
        help="Exit 0 if the migration marker is present+valid; 1 absent; 2 malformed.",
    )
    p_marker_present.set_defaults(func=_cli_marker_present)

    p_finalize = sub.add_parser(
        "finalize-migration-marker",
        help="Write/validate the one-time migration marker (init pre-overwrite).",
    )
    p_finalize.set_defaults(func=_cli_finalize_migration_marker)

    p_apply = sub.add_parser(
        "apply",
        help=(
            "Apply toggle sidecar, command-allowlist state, and policy for "
            "one profile config (under the shared lock)."
        ),
    )
    p_apply.add_argument("--hermes-home", default=None)
    p_apply.add_argument("--config-path", default=None)
    p_apply.set_defaults(func=_cli_apply)

    p_apply_all = sub.add_parser(
        "apply-all",
        help="Apply all present sidecars + policy (init, under one lock).",
    )
    p_apply_all.set_defaults(func=_cli_apply_all)

    p_apply_models = sub.add_parser(
        "apply-models",
        help="Apply the state-owned models.yaml overlay to the default config (init post-sync).",
    )
    p_apply_models.add_argument("--hermes-home", default=None)
    p_apply_models.add_argument("--config-path", default=None)
    p_apply_models.add_argument("--template-config", default=None,
                                help="Repo template config path for rollback defaults.")
    p_apply_models.set_defaults(func=_cli_apply_models)

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