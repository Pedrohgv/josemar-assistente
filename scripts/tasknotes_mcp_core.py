#!/usr/bin/env python3
"""TaskNotes MCP core engine.

A stdlib+PyYAML core engine for a gbrain-backed TaskNotes lifecycle API.
Importable without the MCP SDK. Exposes seven operations (create/get/list/
update/complete/archive/delete) as plain methods for Phase 2 MCP wrappers.

Design invariants (fixed, do not redesign):
  - gbrain is the sole writer; the core never writes task files directly.
  - Mutations fail closed before side effects unless the exact TaskNotes
    4.11.1 profile is compatible and stable (re-checked immediately before
    capture). gbrain source routing is verified under the shared lock
    before any side effect.
  - All operations that invoke gbrain (including get) run under a shared
    ``fcntl.flock`` exclusive lock with a bounded wait at a configurable
    runtime-only path. List is lock-free only because it never invokes
    gbrain.
  - Git transactions disable hooks, signing, gc, and maintenance
    command-locally; use generic content-free messages; never run
    checkout/reset/clean/merge/pull/push. Preflight stages all pending
    edits; post-write stages only the target path. After an operation only
    the target must be clean; unrelated edits may remain pending.
  - Subprocesses run with a minimal env that excludes all provider/API
    credentials; stdout/stderr are streamed with a hard memory cap (not
    buffered-then-truncated); the process group is killed and reaped on
    timeout; errors are redacted/capped; content is never logged.
  - File reads are race-safe no-follow: directories and files are opened
    with ``O_NOFOLLOW`` and ``fstat`` on the opened fd; no check-then-read.
  - Partial failures return structured outcomes (``not_applied``,
    ``applied_and_committed``, ``applied_uncommitted``,
    ``db_updated_disk_failed``, ``recovery_required``). Pre-capture
    failures raise typed core errors. Once capture begins, the handler
    always returns a structured outcome and never escapes generically. A
    recovery marker blocks all later mutations until operator recovery.
  - Completion date defaults to today in the configured TZ; explicit dates
    must be valid ``YYYY-MM-DD``. Already-completed tasks preserve the
    existing completion date.
"""

from __future__ import annotations

import datetime
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TASKNOTES_REQUIRED_VERSION = "4.11.1"
PLUGIN_SUBPATH = ".obsidian/plugins/tasknotes"
MANIFEST_NAME = "manifest.json"
DATA_NAME = "data.json"

# Required logical field mappings (must all be present, unique, non-conflicting).
REQUIRED_MAPPINGS: Tuple[str, ...] = (
    "title",
    "status",
    "priority",
    "due",
    "scheduled",
    "projects",
    "completedDate",
)

# Structural/provenance keys that mapped field values must not collide with.
# Collisions would corrupt canonical reconstruction (gbrain re-injects
# type/tags/slug at top level and provenance on disk). Includes both the
# legacy ``put_page`` provenance keys and the ``capture`` provenance keys
# (``captured_via``/``captured_at``) injected by ``gbrain capture --stdin``.
RESERVED_FRONTMATTER_KEYS: Tuple[str, ...] = (
    "type",
    "tags",
    "slug",
    "ingested_via",
    "ingested_at",
    "source_kind",
    "captured_via",
    "captured_at",
)

# Locking defaults (runtime-only, under /opt/data/.locks/).
DEFAULT_LOCK_DIR = Path("/opt/data/.locks")
DEFAULT_LOCK_NAME = "tasknotes.lock"
DEFAULT_LOCK_TIMEOUT = 10.0  # seconds, bounded wait

# Recovery marker (runtime-only, under the lock dir).
RECOVERY_MARKER_NAME = "tasknotes-recovery.marker"

# Subprocess bounds.
MAX_OUTPUT = 64 * 1024  # 64 KB hard cap per stream
DEFAULT_TIMEOUT = 30.0  # seconds
SYNC_TIMEOUT = 120.0  # seconds
GIT_TIMEOUT = 30.0  # seconds
SOURCES_TIMEOUT = 15.0  # seconds

# Listing bounds.
LIST_MAX_FILES = 1000
LIST_MAX_FILE_SIZE = 1024 * 1024  # 1 MB
LIST_MAX_RESULTS = 1000
PROFILE_MAX_FILE_SIZE = 1024 * 1024  # 1 MB per TaskNotes JSON file

# Input bounds.
MAX_SLUG_LEN = 200
MAX_TITLE_LEN = 500
MAX_BODY_LEN = 100_000
MAX_TAG_LEN = 100
MAX_PROJECT_LEN = 200
MAX_TAGS_COUNT = 50
MAX_PROJECTS_COUNT = 50
MAX_MARKDOWN_LEN = 200_000  # constructed markdown + stdin cap
MAX_RECURRENCE_LEN = 500

# Slug validation: lowercase, single segment (no '/' for MVP), starts with
# alphanumeric, contains only lowercase alphanumeric, hyphen, or underscore.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TAG_RE = re.compile(r"^[^\s\x00-\x1f\x7f]+$")  # non-empty, no whitespace/control
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Slug generation: timestamp prefix + slugified title.
_SLUGIFY_RE = re.compile(r"[^a-z0-9]+")


def slugify_title(title: str) -> str:
    """Slugify a human-readable title into a gbrain-safe slug segment.

    Lowercases, replaces non-alphanumeric runs with single hyphens,
    strips leading/trailing hyphens. Returns an empty string if the
    title contains no alphanumeric characters.
    """
    slugified = _SLUGIFY_RE.sub("-", title.lower().strip())
    slugified = slugified.strip("-")
    if len(slugified) > MAX_SLUG_LEN // 2:
        slugified = slugified[: MAX_SLUG_LEN // 2].rstrip("-")
    return slugified


def generate_slug(title: str, *, tz: str = "UTC") -> str:
    """Generate a task slug from a title and the current timestamp.

    Format: ``YYYY-MM-DD-HHmmss-slugified-title``. The timestamp prefix
    ensures chronological ordering by filename. The slugified title
    provides human readability. If the title has no alphanumeric content,
    the slug is just the timestamp.

    Examples:
        "Buy Groceries" → "2026-07-18-143000-buy-groceries"
        "Review Q3 Report!" → "2026-07-18-143000-review-q3-report"
        "日本語" → "2026-07-18-143000" (no ASCII alphanumeric content)
    """
    timestamp = datetime.datetime.now(_get_zoneinfo(tz)).strftime("%Y-%m-%d-%H%M%S")
    title_slug = slugify_title(title)
    if title_slug:
        slug = f"{timestamp}-{title_slug}"
    else:
        slug = timestamp
    if len(slug) > MAX_SLUG_LEN:
        slug = slug[:MAX_SLUG_LEN].rstrip("-")
    return slug

# Pinned gbrain timeline sentinel (markdown.ts: `<!-- timeline -->`).
TIMELINE_SENTINEL = "<!-- timeline -->"

# Git state markers that indicate an in-progress operation.
_GIT_BAD_MARKERS = (
    "MERGE_HEAD",
    "REBASE_HEAD",
    "REVERT_HEAD",
    "CHERRY_PICK_HEAD",
    "rebase-merge",
    "rebase-apply",
)

# Generic content-free commit messages.
PREFLIGHT_COMMIT_MSG = "tasknotes-mcp: preflight sync"
POSTWRITE_COMMIT_MSG = "tasknotes-mcp: task update"
POSTWRITE_DELETE_COMMIT_MSG = "tasknotes-mcp: task delete"

# Mutation outcome states.
NOT_APPLIED = "not_applied"
APPLIED_AND_COMMITTED = "applied_and_committed"
APPLIED_UNCOMMITTED = "applied_uncommitted"
DB_UPDATED_DISK_FAILED = "db_updated_disk_failed"
RECOVERY_REQUIRED = "recovery_required"

# Documented write-through provenance keys injected on disk by gbrain
# put_page (operations.ts) and capture (capture --stdin). These appear on
# disk but NOT in get_page JSON frontmatter, so disk semantic comparison
# excludes them. ``captured_via``/``captured_at`` are added by capture.
WRITE_THROUGH_PROVENANCE_KEYS: Tuple[str, ...] = (
    "ingested_via",
    "ingested_at",
    "source_kind",
    "captured_via",
    "captured_at",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CoreError(Exception):
    """Base class for all core engine errors."""


class ProfileIncompatible(CoreError):
    """The TaskNotes profile is incompatible or drifted during an operation."""


class PathError(CoreError):
    """A slug or path failed strict confinement validation."""


class GitError(CoreError):
    """A Git transaction failed."""


class SubprocessError(CoreError):
    """A sanitized subprocess failed, timed out, or exceeded memory bounds."""


class GbrainError(CoreError):
    """A gbrain call returned an error or unexpected response."""


class GbrainPageNotFound(GbrainError):
    """A source-routed gbrain get_page call did not find the requested page."""


class RecoveryRequired(CoreError):
    """A recovery marker is present; mutations are blocked until operator recovery."""


class ValidationError(CoreError):
    """An input field failed validation."""


# ---------------------------------------------------------------------------
# Profile loading and validation (real TaskNotes 4.11.1 schema)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskNotesProfile:
    """A validated TaskNotes 4.11.1 profile.

    All fields are derived from ``manifest.json`` and ``data.json`` under
    ``<vault>/.obsidian/plugins/tasknotes/``. The ``profile_hash`` is a
    stable SHA-256 over the canonical (sorted-keys) JSON of the raw
    manifest+data and is used for drift detection. ``source_id`` is the
    gbrain source id whose ``local_path`` equals the vault (verified under
    lock before any side effect).
    """

    version: str
    tasks_folder: str
    task_tag: str
    archive_tag: str
    statuses: Tuple[str, ...]
    completed_status: str
    default_status: str
    priorities: Tuple[str, ...]
    default_priority: str
    mappings: Mapping[str, str]
    brain_repo: str
    profile_hash: str
    source_id: Optional[str]
    raw_manifest: Mapping[str, Any]
    raw_data: Mapping[str, Any]
    move_archived_tasks: bool = False
    archive_folder: Optional[str] = None
    user_fields: Tuple[Dict[str, Any], ...] = ()


def _read_json_from_directory(directory_fd: int, name: str) -> Mapping[str, Any]:
    try:
        text = _read_directory_entry_no_follow(
            directory_fd, name, max_size=PROFILE_MAX_FILE_SIZE
        )
    except CoreError as exc:
        raise ProfileIncompatible(f"cannot read {name}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileIncompatible(f"invalid JSON in {name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileIncompatible(f"{name} root must be an object")
    return data


def _validate_tasks_folder(value: Any, vault: Path) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileIncompatible("tasksFolder must be a non-empty string")
    if value != value.lower():
        raise ProfileIncompatible("tasksFolder must be lowercase")
    if value.startswith("/"):
        raise ProfileIncompatible("tasksFolder must be relative (no leading '/')")
    if "\\" in value:
        raise ProfileIncompatible("tasksFolder must not contain backslash")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        raise ProfileIncompatible("tasksFolder must not contain control characters")
    parts = value.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ProfileIncompatible("tasksFolder must not contain traversal segments")
    try:
        fd = _open_relative_directory_no_follow(vault, value)
    except PathError as exc:
        raise ProfileIncompatible(
            "tasksFolder must already exist inside the vault without symlinks"
        ) from exc
    os.close(fd)
    return value


def _validate_archive_folder(value: Any, vault: Path) -> str:
    """Validate ``archiveFolder``: same constraints as tasksFolder but may not exist yet.

    The plugin creates the archive folder on first archive move. If it does
    not exist yet, the adapter accepts it as long as the path is a valid
    relative path inside the vault with no symlink components on existing
    parents.
    """
    if not isinstance(value, str) or not value:
        raise ProfileIncompatible("archiveFolder must be a non-empty string")
    if value != value.lower():
        raise ProfileIncompatible("archiveFolder must be lowercase")
    if value.startswith("/"):
        raise ProfileIncompatible("archiveFolder must be relative (no leading '/')")
    if "\\" in value:
        raise ProfileIncompatible("archiveFolder must not contain backslash")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        raise ProfileIncompatible("archiveFolder must not contain control characters")
    parts = value.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ProfileIncompatible("archiveFolder must not contain traversal segments")
    # Check that existing parent components are not symlinks. The final
    # component may not exist yet (the plugin creates it on first move).
    try:
        resolved = (vault / value).resolve()
        vault_resolved = vault.resolve()
        resolved.relative_to(vault_resolved)
    except ValueError:
        raise ProfileIncompatible("archiveFolder escapes the vault")
    # If the folder exists, validate it with no-follow (same as tasksFolder).
    candidate = vault / value
    if candidate.is_dir():
        try:
            fd = _open_relative_directory_no_follow(vault, value)
        except PathError as exc:
            raise ProfileIncompatible(
                "archiveFolder must not contain symlink components"
            ) from exc
        os.close(fd)
    return value


def _validate_tag(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileIncompatible(f"{field_name} must be a non-empty string")
    if not _TAG_RE.match(value):
        raise ProfileIncompatible(f"{field_name} contains whitespace or control characters")
    return value


def _validate_statuses(value: Any) -> Tuple[Tuple[str, ...], str]:
    """Validate ``customStatuses``: unique id, unique value, exactly one isCompleted.

    The YAML status written to frontmatter is the ``value`` field.
    """
    if not isinstance(value, list) or not value:
        raise ProfileIncompatible("customStatuses must be a non-empty list")
    ids: List[str] = []
    values: List[str] = []
    completed: List[str] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ProfileIncompatible("each customStatus must be an object")
        sid = entry.get("id")
        if not isinstance(sid, str) or not sid:
            raise ProfileIncompatible("each customStatus must have a non-empty id")
        sval = entry.get("value")
        if not isinstance(sval, str) or not sval:
            raise ProfileIncompatible("each customStatus must have a non-empty value")
        ids.append(sid)
        values.append(sval)
        is_completed = entry.get("isCompleted", False)
        if not isinstance(is_completed, bool):
            raise ProfileIncompatible("customStatus isCompleted must be a boolean")
        if is_completed:
            completed.append(sval)
    if len(set(ids)) != len(ids):
        raise ProfileIncompatible("customStatus ids must be unique")
    if len(set(values)) != len(values):
        raise ProfileIncompatible("customStatus values must be unique")
    if len(completed) != 1:
        raise ProfileIncompatible(
            f"exactly one customStatus must be isCompleted, found {len(completed)}"
        )
    return tuple(values), completed[0]


def _validate_priorities(value: Any) -> Tuple[str, ...]:
    """Validate ``customPriorities``: unique id, unique value."""
    if not isinstance(value, list) or not value:
        raise ProfileIncompatible("customPriorities must be a non-empty list")
    ids: List[str] = []
    values: List[str] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ProfileIncompatible("each customPriority must be an object")
        sid = entry.get("id")
        if not isinstance(sid, str) or not sid:
            raise ProfileIncompatible("each customPriority must have a non-empty id")
        sval = entry.get("value")
        if not isinstance(sval, str) or not sval:
            raise ProfileIncompatible("each customPriority must have a non-empty value")
        ids.append(sid)
        values.append(sval)
    if len(set(ids)) != len(ids):
        raise ProfileIncompatible("customPriority ids must be unique")
    if len(set(values)) != len(values):
        raise ProfileIncompatible("customPriority values must be unique")
    return tuple(values)


def _validate_mappings(value: Any) -> Mapping[str, str]:
    """Validate ``fieldMapping``: direct string values, required + unique + no reserved collisions."""
    if not isinstance(value, dict):
        raise ProfileIncompatible("fieldMapping must be an object")
    mappings: Dict[str, str] = {}
    for logical in REQUIRED_MAPPINGS:
        prop = value.get(logical)
        if not isinstance(prop, str) or not prop:
            raise ProfileIncompatible(
                f"fieldMapping.{logical} must be a non-empty string"
            )
        mappings[logical] = prop
    # All mapped property names must be unique (no two logical fields map to
    # the same frontmatter key).
    if len(set(mappings.values())) != len(mappings):
        raise ProfileIncompatible("fieldMapping values must be unique")
    # Mapped values must not collide with structural/provenance keys.
    for logical, prop in mappings.items():
        if prop in RESERVED_FRONTMATTER_KEYS:
            raise ProfileIncompatible(
                f"fieldMapping.{logical} collides with reserved key {prop!r}"
            )
        if prop == "title" and logical != "title":
            raise ProfileIncompatible(
                f"fieldMapping.{logical} collides with canonical title"
            )
    # archiveTag is a direct string under fieldMapping.
    archive_tag = value.get("archiveTag")
    if not isinstance(archive_tag, str) or not archive_tag:
        raise ProfileIncompatible("fieldMapping.archiveTag must be a non-empty string")
    if not _TAG_RE.match(archive_tag):
        raise ProfileIncompatible("fieldMapping.archiveTag contains whitespace or control characters")
    mappings["archiveTag"] = archive_tag
    # Optional mappings (not required, but extracted when present so callers
    # can use them via profile.mappings). recurrence is an optional RFC 5545
    # RRULE field; only extracted when the profile declares it.
    recurrence = value.get("recurrence")
    if isinstance(recurrence, str) and recurrence:
        mappings["recurrence"] = recurrence
    return mappings


_USER_FIELD_TYPES: Tuple[str, ...] = (
    "text",
    "list",
    "date",
    "number",
    "boolean",
    "link",
    "enum",
)


def _validate_user_fields(value: Any) -> Tuple[Dict[str, Any], ...]:
    """Validate ``userFields``: list of objects with id/key/type (and optional label).

    Each field must have a non-empty string ``id``, ``key``, and ``type``
    belonging to the allowed set. Returns a tuple of plain dicts with the
    keys ``id``, ``key``, ``type``, and ``label`` (label defaults to "").
    Enum fields additionally carry an ``options`` key.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProfileIncompatible("userFields must be a list")
    out: List[Dict[str, Any]] = []
    seen_keys: set = set()
    seen_ids: set = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ProfileIncompatible("each userField must be an object")
        fid = entry.get("id")
        if not isinstance(fid, str) or not fid:
            raise ProfileIncompatible("each userField must have a non-empty id")
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            raise ProfileIncompatible("each userField must have a non-empty key")
        ftype = entry.get("type")
        if not isinstance(ftype, str) or ftype not in _USER_FIELD_TYPES:
            raise ProfileIncompatible(
                f"userField type {ftype!r} not in allowed set {_USER_FIELD_TYPES}"
            )
        label = entry.get("label", "")
        if not isinstance(label, str):
            raise ProfileIncompatible("userField label must be a string")
        field: Dict[str, Any] = {"id": fid, "key": key, "type": ftype, "label": label}
        if ftype == "enum":
            options = entry.get("options")
            if not isinstance(options, list) or not options:
                raise ProfileIncompatible(
                    f"userField {key!r} (enum) must have a non-empty 'options' list"
                )
            if not all(isinstance(o, str) and o for o in options):
                raise ProfileIncompatible(
                    f"userField {key!r} (enum) 'options' must be non-empty strings"
                )
            if len(set(options)) != len(options):
                raise ProfileIncompatible(
                    f"userField {key!r} (enum) 'options' must be unique"
                )
            field["options"] = list(options)
        if key in seen_keys:
            raise ProfileIncompatible(f"userField key {key!r} must be unique")
        if fid in seen_ids:
            raise ProfileIncompatible(f"userField id {fid!r} must be unique")
        if key in RESERVED_FRONTMATTER_KEYS:
            raise ProfileIncompatible(
                f"userField key {key!r} collides with reserved key"
            )
        seen_keys.add(key)
        seen_ids.add(fid)
        out.append(field)
    return tuple(out)


def _git_state_ok(vault: Path, git_env: Dict[str, str]) -> None:
    """Reject bad Git repo state. Raises GitError on any disqualifying state."""
    git_dir = vault / ".git"
    if not git_dir.is_dir():
        raise GitError(f"{vault} is not a Git repository")
    for marker in _GIT_BAD_MARKERS:
        if (git_dir / marker).exists():
            raise GitError(f"Git operation in progress: {marker} present")
    # Check for unmerged index entries.
    result = _run_git(vault, git_env, ["ls-files", "--unmerged"], timeout=GIT_TIMEOUT)
    if result.stdout.strip():
        raise GitError("Git index has unmerged entries")
    # Verify HEAD exists.
    result = _run_git(vault, git_env, ["rev-parse", "--verify", "HEAD"], timeout=GIT_TIMEOUT)
    if result.returncode != 0:
        raise GitError("Git HEAD does not exist (no commits)")


def compute_profile_hash(
    manifest: Mapping[str, Any], data: Mapping[str, Any]
) -> str:
    """Compute a stable SHA-256 over canonical JSON of manifest+data."""
    canonical = json.dumps(
        {"manifest": dict(manifest), "data": dict(data)},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_profile(
    vault: Path,
    brain_repo: Path,
    *,
    git_env: Optional[Dict[str, str]] = None,
) -> TaskNotesProfile:
    """Load and validate the TaskNotes 4.11.1 profile (no gbrain source verification).

    Fails closed (raises ``ProfileIncompatible`` or ``GitError``) before
    any side effects if any compatibility requirement is not met. Source
    verification is performed separately under the lock by
    :func:`verify_gbrain_source`.
    """
    try:
        plugin_fd = _open_relative_directory_no_follow(vault, PLUGIN_SUBPATH)
    except PathError as exc:
        raise ProfileIncompatible(f"cannot open TaskNotes plugin directory: {exc}") from exc
    try:
        manifest = _read_json_from_directory(plugin_fd, MANIFEST_NAME)
        data = _read_json_from_directory(plugin_fd, DATA_NAME)
    finally:
        os.close(plugin_fd)

    # Version check (exact).
    version = manifest.get("version")
    if version != TASKNOTES_REQUIRED_VERSION:
        raise ProfileIncompatible(
            f"TaskNotes version {version!r} != required {TASKNOTES_REQUIRED_VERSION!r}"
        )

    # Task identification must be tag-based.
    if data.get("taskIdentificationMethod") != "tag":
        raise ProfileIncompatible("taskIdentificationMethod must be 'tag'")
    task_tag = _validate_tag(data.get("taskTag"), "taskTag")

    # tasksFolder.
    tasks_folder = _validate_tasks_folder(data.get("tasksFolder"), vault)

    # Filename settings: the adapter writes files via gbrain with explicit
    # slugs, so the plugin's filename generation does not apply to adapter-
    # created tasks. The frontmatter title always takes precedence over
    # filename-based title extraction, so storeTitleInFilename does not
    # affect adapter-created tasks either. No hard requirement here.

    # Archive settings: moveArchivedTasks is config-adaptive. When true, the
    # plugin moves archived tasks to archiveFolder. The adapter reads
    # archiveFolder and handles both locations.
    move_archived = bool(data.get("moveArchivedTasks", False))
    archive_folder: Optional[str] = None
    if move_archived:
        raw_archive_folder = data.get("archiveFolder")
        if not isinstance(raw_archive_folder, str) or not raw_archive_folder:
            raise ProfileIncompatible(
                "moveArchivedTasks is true but archiveFolder is not set"
            )
        archive_folder = _validate_archive_folder(raw_archive_folder, vault)

    # Statuses (customStatuses).
    statuses, completed_status = _validate_statuses(data.get("customStatuses"))

    # Default status.
    default_status = data.get("defaultTaskStatus")
    if not isinstance(default_status, str) or default_status not in statuses:
        raise ProfileIncompatible("defaultTaskStatus must belong to the status set")

    # Priorities (customPriorities).
    priorities = _validate_priorities(data.get("customPriorities"))
    default_priority = data.get("defaultTaskPriority")
    if not isinstance(default_priority, str) or default_priority not in priorities:
        raise ProfileIncompatible("defaultTaskPriority must belong to the priority set")

    # Mappings (fieldMapping, direct strings).
    mappings = _validate_mappings(data.get("fieldMapping"))
    archive_tag = mappings["archiveTag"]
    if archive_tag == task_tag:
        raise ProfileIncompatible("archiveTag must differ from taskTag")

    # Custom user fields (userFields).
    user_fields = _validate_user_fields(data.get("userFields", []))
    # User field keys must not collide with modeled field mappings.
    mapped_values = set(mappings.values())
    for uf in user_fields:
        if uf["key"] in mapped_values:
            raise ProfileIncompatible(
                f"userField key {uf['key']!r} collides with a fieldMapping value"
            )

    # Vault equals configured brain repo.
    try:
        vault_resolved = vault.resolve()
        brain_resolved = brain_repo.resolve()
    except OSError as exc:
        raise ProfileIncompatible(f"cannot resolve vault/brain_repo: {exc}") from exc
    if vault_resolved != brain_resolved:
        raise ProfileIncompatible(
            f"vault {vault_resolved} != configured brain_repo {brain_resolved}"
        )

    # Git state.
    if git_env is None:
        git_env = _build_git_env()
    _git_state_ok(vault, git_env)

    profile_hash = compute_profile_hash(manifest, data)
    return TaskNotesProfile(
        version=version,
        tasks_folder=tasks_folder,
        task_tag=task_tag,
        archive_tag=archive_tag,
        statuses=statuses,
        completed_status=completed_status,
        default_status=default_status,
        priorities=priorities,
        default_priority=default_priority,
        mappings=mappings,
        brain_repo=str(brain_repo),
        profile_hash=profile_hash,
        source_id=None,  # set by verify_gbrain_source under lock
        raw_manifest=manifest,
        raw_data=data,
        move_archived_tasks=move_archived,
        archive_folder=archive_folder,
        user_fields=user_fields,
    )


# ---------------------------------------------------------------------------
# gbrain source verification and routing
# ---------------------------------------------------------------------------


def gbrain_sources_list(
    gbrain_bin: str,
    env: Dict[str, str],
    *,
    timeout: float = SOURCES_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Run ``gbrain sources list --json`` and return the parsed sources list."""
    result = run_subprocess(
        [gbrain_bin, "sources", "list", "--json"],
        env=env,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise GbrainError(f"gbrain sources list failed: {result.stderr[:200]}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GbrainError(f"gbrain sources list returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GbrainError("gbrain sources list returned non-object")
    sources = data.get("sources")
    if not isinstance(sources, list):
        raise GbrainError("gbrain sources list: 'sources' must be a list")
    return sources


def verify_gbrain_source(
    gbrain_bin: str,
    env: Dict[str, str],
    vault: Path,
    profile: TaskNotesProfile,
) -> TaskNotesProfile:
    """Verify exactly one gbrain source whose ``local_path`` equals the vault.

    Returns a new profile with ``source_id`` set. Raises ``GbrainError`` on
    no/ambiguous/mismatched sources. Must be called under the shared lock
    before any side effect.
    """
    sources = gbrain_sources_list(gbrain_bin, env)
    if not sources:
        raise GbrainError("no gbrain sources registered")
    try:
        vault_resolved = vault.resolve()
    except OSError as exc:
        raise GbrainError(f"cannot resolve vault: {exc}") from exc
    matching: List[str] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        local_path = src.get("local_path")
        if not isinstance(local_path, str) or not local_path:
            continue
        try:
            src_resolved = Path(local_path).resolve()
        except OSError:
            continue
        if src_resolved == vault_resolved:
            sid = src.get("id")
            if isinstance(sid, str) and sid:
                matching.append(sid)
    if len(matching) == 0:
        raise GbrainError("no gbrain source matches the vault local_path")
    if len(matching) > 1:
        raise GbrainError(f"ambiguous gbrain sources for vault: {matching}")
    return TaskNotesProfile(
        version=profile.version,
        tasks_folder=profile.tasks_folder,
        task_tag=profile.task_tag,
        archive_tag=profile.archive_tag,
        statuses=profile.statuses,
        completed_status=profile.completed_status,
        default_status=profile.default_status,
        priorities=profile.priorities,
        default_priority=profile.default_priority,
        mappings=profile.mappings,
        brain_repo=profile.brain_repo,
        profile_hash=profile.profile_hash,
        source_id=matching[0],
        raw_manifest=profile.raw_manifest,
        raw_data=profile.raw_data,
        move_archived_tasks=profile.move_archived_tasks,
        archive_folder=profile.archive_folder,
        user_fields=profile.user_fields,
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def validate_slug(slug: str) -> str:
    """Validate a bare task slug (lowercase, single segment, no '/').

    For MVP, ``/`` is rejected in task slugs so listing and create semantics
    match. The slug must be lowercase, must not start with ``/``, must not
    contain ``..``, backslash, control characters, or ``/``, and must match
    ``[a-z0-9][a-z0-9_-]*``.
    """
    if not isinstance(slug, str) or not slug:
        raise PathError("slug must be a non-empty string")
    if len(slug) > MAX_SLUG_LEN:
        raise PathError("slug exceeds length bound")
    if slug != slug.lower():
        raise PathError("slug must be lowercase")
    if slug.startswith("/"):
        raise PathError("slug must be relative (no leading '/')")
    if "\\" in slug:
        raise PathError("slug must not contain backslash")
    if "/" in slug:
        raise PathError("slug must not contain '/' (single segment only)")
    if ".." in slug:
        raise PathError("slug must not contain traversal segments")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in slug):
        raise PathError("slug must not contain control characters")
    if not _SLUG_RE.match(slug):
        raise PathError(f"slug {slug!r} is invalid")
    return slug


def resolve_gbrain_slug(profile: TaskNotesProfile, slug: str) -> str:
    """Return the full gbrain slug (``<tasks_folder>/<slug>``)."""
    return f"{profile.tasks_folder}/{slug}"


def resolve_task_path(
    vault: Path, profile: TaskNotesProfile, slug: str
) -> Path:
    """Return the primary on-disk task path (in the tasks folder).

    Does not follow symlinks. For archived tasks that may have been moved
    by the plugin to the archive folder, use :func:`resolve_task_path_any`.
    """
    validate_slug(slug)
    return vault / profile.tasks_folder / f"{slug}.md"


def resolve_task_path_any(
    vault: Path, profile: TaskNotesProfile, slug: str
) -> Optional[Path]:
    """Return the on-disk task path, checking tasks folder then archive folder.

    When ``moveArchivedTasks`` is true, the plugin may have moved an
    archived task to ``archiveFolder``. This helper checks both locations
    and returns the first existing path (no-follow). Returns ``None`` if
    the task file does not exist in either location.
    """
    validate_slug(slug)
    primary = vault / profile.tasks_folder / f"{slug}.md"
    if target_exists_no_follow(primary):
        return primary
    if profile.move_archived_tasks and profile.archive_folder:
        archived = vault / profile.archive_folder / f"{slug}.md"
        if target_exists_no_follow(archived):
            return archived
    return None


# ---------------------------------------------------------------------------
# Race-safe no-follow file reads
# ---------------------------------------------------------------------------


def _open_no_follow(path: Path) -> int:
    """Open a path with ``O_NOFOLLOW`` and return the fd.

    Raises ``PathError`` if the path is a symlink (the open fails with
    ``ELOOP``) or cannot be opened.
    """
    try:
        return os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathError(f"path is a symlink: {path}") from exc
        raise PathError(f"cannot open {path}: {exc}") from exc


def _read_fd_bounded(fd: int, max_size: int) -> str:
    """Read at most ``max_size + 1`` bytes from ``fd`` and decode.

    Raises ``CoreError`` if the file exceeds ``max_size``.
    """
    data = b""
    try:
        while len(data) <= max_size:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        if len(data) > max_size:
            raise CoreError(f"file exceeds size bound ({max_size} bytes)")
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace")


def read_file_no_follow(path: Path, max_size: int = LIST_MAX_FILE_SIZE) -> str:
    """Open a file no-follow, fstat the fd, read at most ``max_size`` bytes."""
    fd = _open_no_follow(path)
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise CoreError(f"cannot fstat {path}: {exc}") from exc
    if not stat_is_regular(st):
        os.close(fd)
        raise CoreError(f"{path} is not a regular file")
    if st.st_size > max_size:
        os.close(fd)
        raise CoreError(f"{path} exceeds size bound")
    return _read_fd_bounded(fd, max_size)


def _open_directory_no_follow(path: Path) -> int:
    """Open a directory itself without following a symlink."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathError(f"directory is a symlink: {path}") from exc
        raise PathError(f"cannot open directory {path}: {exc}") from exc
    try:
        import stat as stat_mod
        if not stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            raise PathError(f"{path} is not a directory")
    except Exception:
        os.close(fd)
        raise
    return fd


def _open_relative_directory_no_follow(base: Path, relative: str) -> int:
    """Open every component of a relative directory path with ``O_NOFOLLOW``."""
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise PathError("invalid relative directory path")
    current_fd = _open_directory_no_follow(base)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PathError(f"directory component is a symlink: {part}") from exc
                raise PathError(f"cannot open directory component {part}: {exc}") from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _read_directory_entry_no_follow(
    directory_fd: int, name: str, *, max_size: int
) -> str:
    """Read one regular directory entry using ``dir_fd`` and ``O_NOFOLLOW``."""
    if not name or "/" in name or name in (".", ".."):
        raise PathError("invalid directory entry name")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathError(f"entry is a symlink: {name}") from exc
        raise PathError(f"cannot open entry {name}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat_is_regular(st):
            raise CoreError(f"entry is not a regular file: {name}")
        if st.st_size > max_size:
            raise CoreError(f"entry exceeds size bound: {name}")
        return _read_fd_bounded(fd, max_size)
    except Exception:
        # _read_fd_bounded owns the descriptor once called.
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def stat_is_regular(st: os.stat_result) -> bool:
    return stat_is_regular_mode(st.st_mode)


def stat_is_regular_mode(mode: int) -> bool:
    import stat as stat_mod
    return stat_mod.S_ISREG(mode)


def list_dir_no_follow(dir_path: Path) -> List[str]:
    """Open a directory no-follow and return sorted entry names.

    Raises ``PathError`` if the directory is a symlink.
    """
    fd = _open_directory_no_follow(dir_path)
    try:
        return sorted(os.listdir(fd))
    except OSError as exc:
        raise PathError(f"cannot list {dir_path}: {exc}") from exc
    finally:
        os.close(fd)


def target_exists_no_follow(path: Path) -> bool:
    """Return True if ``path`` exists (no-follow; symlink counts as exists)."""
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PathError(f"cannot lstat {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


class Lock:
    """Exclusive ``fcntl.flock`` with a bounded wait.

    The lock file lives at a configurable runtime-only path (default
    ``/opt/data/.locks/tasknotes.lock``). Acquisition uses ``LOCK_NB`` in a
    bounded polling loop so a busy lock raises ``CoreError`` after the
    timeout instead of blocking forever.
    """

    def __init__(
        self,
        lock_path: Optional[Path] = None,
        *,
        timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self.lock_path = lock_path if lock_path is not None else DEFAULT_LOCK_DIR / DEFAULT_LOCK_NAME
        self.timeout = timeout
        self._fh: Any = None

    def __enter__(self) -> "Lock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as exc:
            raise CoreError(f"cannot open lock file safely: {exc}") from exc
        self._fh = os.fdopen(fd, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise CoreError(
                        f"lock acquisition timed out after {self.timeout}s"
                    )
                time.sleep(0.1)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


# ---------------------------------------------------------------------------
# Bounded subprocess runner (streamed I/O, hard memory cap)
# ---------------------------------------------------------------------------


@dataclass
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_capped: bool = False


def _redact(text: str) -> str:
    """Cap output to MAX_OUTPUT bytes (UTF-8) with a truncation marker."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT:
        return text
    return encoded[:MAX_OUTPUT].decode("utf-8", errors="replace") + "...[truncated]"


def run_subprocess(
    argv: List[str],
    *,
    stdin: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[Path] = None,
) -> SubprocessResult:
    """Run a subprocess with a minimal env, streamed bounded I/O, and process-group kill.

    Uses ``start_new_session=True`` so the child leads a new process group;
    on timeout the whole group is killed (``SIGKILL``) and reaped. stdout
    and stderr are streamed concurrently with a hard memory cap per stream;
    exceeding the cap raises ``SubprocessError`` and the process group is
    killed/reaped. Never logs content.
    """
    stdin_bytes = stdin.encode("utf-8") if stdin is not None else None
    if stdin_bytes is not None and len(stdin_bytes) > MAX_MARKDOWN_LEN:
        raise SubprocessError("subprocess stdin exceeds size bound")

    # Disk-backed temporary files avoid unbounded pipe buffering and pipe-reader
    # threads. The parent polls file sizes and kills the process group as soon as
    # either stream exceeds the configured output limit.
    with (
        tempfile.TemporaryFile() as stdin_file,
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        if stdin_bytes is not None:
            stdin_file.write(stdin_bytes)
        stdin_file.seek(0)
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=stderr_file,
                env=env,
                cwd=str(cwd) if cwd is not None else None,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SubprocessError(f"executable not found: {argv[0]}") from exc
        except OSError as exc:
            raise SubprocessError(f"cannot start subprocess: {exc}") from exc

        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if (
                os.fstat(stdout_file.fileno()).st_size > MAX_OUTPUT
                or os.fstat(stderr_file.fileno()).st_size > MAX_OUTPUT
            ):
                _kill_reap(proc)
                raise SubprocessError(
                    f"subprocess output exceeded memory cap ({MAX_OUTPUT} bytes)"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_reap(proc)
                raise SubprocessError(
                    f"subprocess timed out after {timeout}s (argv[0]={argv[0]!r})"
                )
            try:
                proc.wait(timeout=min(0.01, remaining))
            except subprocess.TimeoutExpired:
                pass

        if (
            os.fstat(stdout_file.fileno()).st_size > MAX_OUTPUT
            or os.fstat(stderr_file.fileno()).st_size > MAX_OUTPUT
        ):
            raise SubprocessError(
                f"subprocess output exceeded memory cap ({MAX_OUTPUT} bytes)"
            )
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(MAX_OUTPUT + 1).decode("utf-8", errors="replace")
        stderr = stderr_file.read(MAX_OUTPUT + 1).decode("utf-8", errors="replace")
        return SubprocessResult(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            output_capped=False,
        )


def _kill_reap(proc: subprocess.Popen) -> None:
    """Kill the whole process group and reap."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _kill_reap_if_alive(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        _kill_reap(proc)


# ---------------------------------------------------------------------------
# Environment builders (minimal, no credentials)
# ---------------------------------------------------------------------------


def _build_gbrain_env(
    gbrain_home: Path,
    brain_repo: Path,
) -> Dict[str, str]:
    """Build a minimal env for gbrain subprocesses.

    Only HOME, PATH, LANG, LC_ALL, TZ, GBRAIN_HOME, GBRAIN_BRAIN_REPO,
    and GBRAIN_SKIP_STARTUP_HOOKS=1 are set by default. The four non-secret
    embedding configuration variables (LLAMA_SERVER_BASE_URL,
    GBRAIN_EMBEDDING_MODEL_REVISION, GBRAIN_EMBEDDING_MODEL, and
    GBRAIN_EMBEDDING_DIMENSIONS) are forwarded only when inherited with
    non-empty values. No provider/API credentials are inherited.
    """
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "TZ": os.environ.get("TZ", "UTC"),
        "GBRAIN_HOME": str(gbrain_home),
        "GBRAIN_BRAIN_REPO": str(brain_repo),
        "GBRAIN_SKIP_STARTUP_HOOKS": "1",
    }
    for key in (
        "LLAMA_SERVER_BASE_URL",
        "GBRAIN_EMBEDDING_MODEL_REVISION",
        "GBRAIN_EMBEDDING_MODEL",
        "GBRAIN_EMBEDDING_DIMENSIONS",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _build_git_env() -> Dict[str, str]:
    """Build a minimal env for Git subprocesses (no credentials)."""
    return {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "TZ": os.environ.get("TZ", "UTC"),
        "GIT_AUTHOR_NAME": "tasknotes-mcp",
        "GIT_AUTHOR_EMAIL": "tasknotes-mcp@local",
        "GIT_COMMITTER_NAME": "tasknotes-mcp",
        "GIT_COMMITTER_EMAIL": "tasknotes-mcp@local",
    }


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

# Both preflight and post-write commits disable hooks, signing, gc, and
# maintenance command-locally.
_GIT_BASE_ARGS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "commit.gpgsign=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
]


def _run_git(
    vault: Path,
    git_env: Dict[str, str],
    args: List[str],
    *,
    timeout: float = GIT_TIMEOUT,
) -> SubprocessResult:
    argv = ["git"] + _GIT_BASE_ARGS + args
    result = run_subprocess(argv, env=git_env, cwd=vault, timeout=timeout)
    return result


def check_git_state(vault: Path, git_env: Optional[Dict[str, str]] = None) -> None:
    """Reject bad Git repo state (merge/rebase/cherry-pick/revert/unmerged)."""
    if git_env is None:
        git_env = _build_git_env()
    _git_state_ok(vault, git_env)


def git_preflight_commit(vault: Path, git_env: Optional[Dict[str, str]] = None) -> bool:
    """Preflight commit: stage all pending edits and commit iff dirty.

    Returns True if a commit was created, False if there was nothing to
    commit. Uses ``git add -A`` so pending manual edits are committed
    before the incremental gbrain sync. Hooks, signing, gc, and
    maintenance are disabled command-locally. Never runs
    checkout/reset/clean/merge/pull/push.
    """
    if git_env is None:
        git_env = _build_git_env()
    r = _run_git(vault, git_env, ["add", "-A"])
    if r.returncode != 0:
        raise GitError(f"git add -A failed: {_redact(r.stderr)[:200]}")
    r = _run_git(vault, git_env, ["diff", "--cached", "--quiet"])
    if r.returncode == 0:
        return False  # nothing staged
    r = _run_git(vault, git_env, ["commit", "-m", PREFLIGHT_COMMIT_MSG])
    if r.returncode != 0:
        raise GitError(f"git preflight commit failed: {_redact(r.stderr)[:200]}")
    return True


def git_commit_target(
    vault: Path,
    target_path: Path,
    git_env: Optional[Dict[str, str]] = None,
) -> bool:
    """Post-write commit: stage only the target path and commit iff dirty.

    Returns True if a commit was created, False if the target was already
    clean. Hooks, signing, gc, and maintenance are disabled
    command-locally. Never runs checkout/reset/clean/merge/pull/push.
    """
    if git_env is None:
        git_env = _build_git_env()
    rel = target_path.relative_to(vault) if target_path.is_absolute() else target_path
    r = _run_git(vault, git_env, ["add", "--", str(rel)])
    if r.returncode != 0:
        raise GitError(f"git add target failed: {_redact(r.stderr)[:200]}")
    r = _run_git(vault, git_env, ["diff", "--cached", "--quiet"])
    if r.returncode == 0:
        return False
    r = _run_git(vault, git_env, ["commit", "-m", POSTWRITE_COMMIT_MSG, "--", str(rel)])
    if r.returncode != 0:
        raise GitError(f"git post-write commit failed: {_redact(r.stderr)[:200]}")
    return True


def git_target_clean(
    vault: Path,
    target_path: Path,
    git_env: Optional[Dict[str, str]] = None,
) -> bool:
    """Return True if the target path has no uncommitted changes."""
    if git_env is None:
        git_env = _build_git_env()
    rel = target_path.relative_to(vault) if target_path.is_absolute() else target_path
    r = _run_git(vault, git_env, ["status", "--porcelain", "--", str(rel)])
    if r.returncode != 0:
        raise GitError(f"git status failed: {_redact(r.stderr)[:200]}")
    return r.stdout.strip() == ""


def git_head_id(vault: Path, git_env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Return the current HEAD commit id, or None if no HEAD."""
    if git_env is None:
        git_env = _build_git_env()
    r = _run_git(vault, git_env, ["rev-parse", "--verify", "HEAD"])
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def git_rm_and_commit(
    vault: Path,
    target_path: Path,
    git_env: Optional[Dict[str, str]] = None,
) -> bool:
    """Remove target from working tree and index, then commit iff staged.

    Uses ``git rm -- <path>`` to delete the file from disk and stage the
    removal, then commits with the delete message. Hooks, signing, gc, and
    maintenance are disabled command-locally.

    Returns True if a commit was created, False if nothing was staged.
    """
    if git_env is None:
        git_env = _build_git_env()
    rel = target_path.relative_to(vault) if target_path.is_absolute() else target_path
    r = _run_git(vault, git_env, ["rm", "--", str(rel)])
    if r.returncode != 0:
        raise GitError(f"git rm failed: {_redact(r.stderr)[:200]}")
    r = _run_git(vault, git_env, ["diff", "--cached", "--quiet"])
    if r.returncode == 0:
        return False
    r = _run_git(
        vault, git_env,
        ["commit", "-m", POSTWRITE_DELETE_COMMIT_MSG, "--", str(rel)],
    )
    if r.returncode != 0:
        raise GitError(f"git post-delete commit failed: {_redact(r.stderr)[:200]}")
    return True


# ---------------------------------------------------------------------------
# Gbrain helpers (source-routed)
# ---------------------------------------------------------------------------


def gbrain_get_page(
    gbrain_bin: str,
    env: Dict[str, str],
    slug: str,
    source_id: str,
) -> Dict[str, Any]:
    """Call ``gbrain call --source <id> get_page <json>`` and return parsed JSON.

    On a page_not_found error, raises ``GbrainError`` whose message contains
    ``page_not_found`` so callers can distinguish missing pages from other
    failures.
    """
    payload = json.dumps({"slug": slug})
    result = run_subprocess(
        [gbrain_bin, "call", "--source", source_id, "get_page", payload],
        env=env,
        timeout=DEFAULT_TIMEOUT,
    )
    # Try to parse stdout as JSON regardless of exit code; gbrain writes
    # error JSON to stdout on OperationError.
    data: Optional[Dict[str, Any]] = None
    if result.stdout:
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            pass
    if data is not None and "error" in data:
        error = data["error"]
        if error == "page_not_found":
            raise GbrainPageNotFound("gbrain page not found")
        raise GbrainError(f"gbrain get_page error: {error}")
    if result.returncode != 0:
        if result.stderr.strip().lower().startswith("page not found:"):
            raise GbrainPageNotFound("gbrain page not found")
        raise GbrainError(f"gbrain get_page failed: {_redact(result.stderr)[:200]}")
    if data is None:
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GbrainError(f"gbrain get_page returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GbrainError("gbrain get_page returned non-object")
    return data


def gbrain_capture(
    gbrain_bin: str,
    env: Dict[str, str],
    slug: str,
    source_id: str,
    markdown: str,
) -> Dict[str, Any]:
    """Call ``gbrain capture --stdin --slug <slug> --source <id> --json`` with markdown on stdin; return parsed JSON.

    Body content is sent through stdin (never argv) so it stays out of the
    process argument vector and any process-table/audit surface. ``--slug``,
    ``--source``, ``--stdin`` and ``--json`` are all documented by live
    ``gbrain capture --help``. Capture reports write-through success as a
    top-level ``written`` boolean (unlike the legacy ``put`` shape which
    nested it under ``write_through.written``).
    """
    result = run_subprocess(
        [gbrain_bin, "capture", "--stdin", "--slug", slug, "--source", source_id, "--json"],
        stdin=markdown,
        env=env,
        timeout=DEFAULT_TIMEOUT,
    )
    if result.returncode != 0:
        raise GbrainError(f"gbrain capture failed: {_redact(result.stderr)[:200]}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GbrainError(f"gbrain capture returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GbrainError("gbrain capture returned non-object")
    return data


def gbrain_untag(
    gbrain_bin: str,
    env: Dict[str, str],
    slug: str,
    tag: str,
    source_id: str,
) -> None:
    """Call ``gbrain untag <slug> <tag> --source <id>`` to remove a tag from the DB.

    Gbrain's write-through (put_page/capture) reconciles tags additively
    and does not remove tags that are absent from the new frontmatter.
    After a capture that removes a tag from frontmatter, this call is
    needed to sync the DB tag index.
    """
    result = run_subprocess(
        [gbrain_bin, "untag", slug, tag, "--source", source_id],
        env=env,
        timeout=DEFAULT_TIMEOUT,
    )
    if result.returncode != 0:
        raise GbrainError(f"gbrain untag failed: {_redact(result.stderr)[:200]}")


def gbrain_sync_incremental(
    gbrain_bin: str,
    env: Dict[str, str],
    vault: Path,
    source_id: str,
) -> Dict[str, Any]:
    """Run incremental gbrain sync and ignore human single-source stdout.

    Pinned gbrain documents ``--json`` globally, but the single-source path
    still renders text such as ``Already up to date.``. The adapter only needs
    the process outcome, so successful stdout is intentionally not parsed.
    """
    result = run_subprocess(
        [gbrain_bin, "sync", "--source", source_id, "--no-embed", "--no-extract",
         "--yes", "--no-pull", "--json", "--repo", str(vault)],
        env=env,
        timeout=SYNC_TIMEOUT,
    )
    if result.returncode != 0:
        raise GbrainError(f"gbrain sync failed: {_redact(result.stderr)[:200]}")
    return {}


def gbrain_sync_full(
    gbrain_bin: str,
    env: Dict[str, str],
    vault: Path,
    source_id: str,
) -> Dict[str, Any]:
    """Run full gbrain sync (recovery path), ignoring human stdout."""
    result = run_subprocess(
        [gbrain_bin, "sync", "--source", source_id, "--full", "--no-embed",
         "--yes", "--no-pull", "--json", "--repo", str(vault)],
        env=env,
        timeout=SYNC_TIMEOUT,
    )
    if result.returncode != 0:
        raise GbrainError(f"gbrain full sync failed: {_redact(result.stderr)[:200]}")
    return {}


def gbrain_delete(
    gbrain_bin: str,
    env: Dict[str, str],
    slug: str,
    source_id: str,
) -> Dict[str, Any]:
    """Call ``gbrain delete <slug> --source <id>`` and return parsed JSON.

    Gbrain delete is a soft-delete (sets ``deleted_at = NOW()``). The DB
    row is hidden from search/get/list but recoverable for 72h via
    ``restore_page``. The on-disk ``.md`` file is NOT touched by this
    command — the caller (``task_delete``) must follow up with ``git rm``
    and commit.
    """
    result = run_subprocess(
        [gbrain_bin, "delete", slug, "--source", source_id],
        env=env,
        timeout=DEFAULT_TIMEOUT,
    )
    if result.returncode != 0:
        raise GbrainError(f"gbrain delete failed: {_redact(result.stderr)[:200]}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GbrainError(f"gbrain delete returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GbrainError("gbrain delete returned non-object")
    return data


# ---------------------------------------------------------------------------
# Markdown frontmatter parsing and reconstruction (faithful gbrain model)
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from markdown text.

    Returns ``(frontmatter, body)``. If no frontmatter is present,
    returns ``({}, text)``. Uses ``yaml.safe_load``.
    """
    if not text.startswith("---\n"):
        return {}, text
    try:
        end = text.index("\n---\n", 4)
    except ValueError:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 5 :]
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise CoreError("PyYAML is required but not available") from exc
    data = yaml.safe_load(fm_text)
    if data is None:
        return {}, body
    if not isinstance(data, dict):
        raise CoreError("frontmatter must be a YAML mapping")
    return data, body


def _split_body_timeline(body: str) -> Tuple[str, str]:
    """Split body at the pinned timeline sentinel ``<!-- timeline -->``.

    Returns ``(compiled_truth, timeline)``. If no sentinel is present,
    returns ``(body, "")``. Matches the pinned gbrain ``splitBody`` for
    the preferred sentinel only (no fallback to ``--- timeline ---`` or
    bare ``---``).
    """
    lines = body.split("\n")
    for i, line in enumerate(lines):
        trimmed = line.strip()
        if trimmed == TIMELINE_SENTINEL or trimmed == "<!--timeline-->":
            compiled = "\n".join(lines[:i])
            timeline = "\n".join(lines[i + 1 :])
            return compiled, timeline
    return body, ""


def _serialize_frontmatter(fm: Dict[str, Any]) -> str:
    """Serialize frontmatter as YAML with ``---`` fences."""
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise CoreError("PyYAML is required but not available") from exc
    dumped = yaml.safe_dump(
        fm, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
    if dumped.endswith("\n"):
        dumped = dumped[:-1]
    return f"---\n{dumped}\n---\n"


def decode_page(page: Dict[str, Any]) -> Dict[str, Any]:
    """Strictly type-check and decode a gbrain get_page response.

    Extracts: type (str), title (str), tags (list[str]), frontmatter
    (mapping without structural fields), compiled_truth (str), timeline
    (str). Raises ``GbrainError`` on shape violations.
    """
    if not isinstance(page, dict):
        raise GbrainError("get_page response must be an object")
    ptype = page.get("type", "note")
    if not isinstance(ptype, str):
        raise GbrainError("get_page type must be a string")
    title = page.get("title", "")
    if not isinstance(title, str):
        raise GbrainError("get_page title must be a string")
    tags = page.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise GbrainError("get_page tags must be a list of strings")
    frontmatter = page.get("frontmatter", {}) or {}
    if not isinstance(frontmatter, dict):
        raise GbrainError("get_page frontmatter must be a mapping")
    # Strip structural fields from frontmatter (gbrain keeps them top-level).
    clean_fm = {k: v for k, v in frontmatter.items() if k not in ("type", "title", "tags", "slug")}
    compiled = page.get("compiled_truth", "")
    if compiled is None:
        compiled = ""
    if not isinstance(compiled, str):
        raise GbrainError("get_page compiled_truth must be a string")
    timeline = page.get("timeline", "")
    if timeline is None:
        timeline = ""
    if not isinstance(timeline, str):
        raise GbrainError("get_page timeline must be a string")
    return {
        "type": ptype,
        "title": title,
        "tags": list(tags),
        "frontmatter": clean_fm,
        "compiled_truth": compiled,
        "timeline": timeline,
    }


def reconstruct_markdown(
    page: Dict[str, Any],
    profile: TaskNotesProfile,
    updates: Mapping[str, Any],
    *,
    body_override: Optional[str] = None,
) -> str:
    """Reconstruct semantic markdown preserving unknown frontmatter and body/timeline.

    ``updates`` maps logical field names (title, status, priority, due,
    scheduled, projects, completedDate, tags) to their new values. A
    value of ``None`` removes the field. Unknown frontmatter from the
    existing page is preserved. The body (``compiled_truth``) and
    ``timeline`` are preserved verbatim. Emits the pinned gbrain
    timeline sentinel ``<!-- timeline -->`` exactly when timeline is
    non-empty.

    ``body_override`` is a narrow optional body replacement. When
    ``None`` (the default) the existing body is preserved verbatim,
    including the empty-body case. When a string (including the empty
    string), it replaces only the body content; frontmatter, unknown
    frontmatter, and the timeline are preserved exactly as without it.
    The caller is responsible for validating the override (e.g. via
    :func:`validate_body`) before passing it.
    """
    decoded = decode_page(page)
    fm: Dict[str, Any] = dict(decoded["frontmatter"])

    # Separate custom field updates (applied by raw key) from modeled
    # field updates (applied via profile.mappings).
    custom_updates: Dict[str, Any] = {}
    modeled_updates: Dict[str, Any] = {}
    for logical, value in updates.items():
        if logical in profile.mappings or logical in ("tags", "title"):
            modeled_updates[logical] = value
        else:
            custom_updates[logical] = value

    # Apply updates to mapped fields.
    for logical, value in modeled_updates.items():
        if logical == "tags":
            if value is None:
                fm.pop("tags", None)
            else:
                fm["tags"] = list(value)
            continue
        if logical == "title":
            # title is top-level in gbrain but also mapped; set in frontmatter
            # so the serialized markdown carries it (gbrain re-extracts).
            if value is None:
                fm.pop(profile.mappings["title"], None)
            else:
                fm[profile.mappings["title"]] = value
            continue
        if logical not in profile.mappings:
            continue
        key = profile.mappings[logical]
        if value is None:
            fm.pop(key, None)
        else:
            fm[key] = value

    # Apply custom field updates by raw key.
    for key, value in custom_updates.items():
        if value is None:
            fm.pop(key, None)
        else:
            fm[key] = value

    # Rehydrate structural fields that gbrain returns only at the page level.
    fm["type"] = decoded["type"]
    fm["title"] = decoded["title"]
    if profile.mappings["title"] != "title":
        fm.setdefault(profile.mappings["title"], decoded["title"])
    if "tags" not in updates:
        fm["tags"] = list(decoded["tags"])

    body = decoded["compiled_truth"]
    if body_override is not None:
        body = body_override
    timeline = decoded["timeline"]

    parts = [_serialize_frontmatter(fm)]
    if body:
        parts.append(body)
        if not body.endswith("\n"):
            parts.append("\n")
    if timeline:
        parts.append("\n")
        parts.append(TIMELINE_SENTINEL)
        parts.append("\n")
        parts.append(timeline)
        if not timeline.endswith("\n"):
            parts.append("\n")
    return "".join(parts)


def build_create_markdown(
    profile: TaskNotesProfile,
    title: str,
    status: str,
    priority: str,
    due: Optional[str],
    scheduled: Optional[str],
    projects: Optional[List[str]],
    tags: Optional[List[str]],
    body: str,
    custom_fields: Optional[Dict[str, Any]] = None,
    recurrence: Optional[str] = None,
) -> str:
    """Build markdown for a new task (no existing page)."""
    m = profile.mappings
    fm: Dict[str, Any] = {
        "type": "note",
        "title": title,
        m["status"]: status,
        m["priority"]: priority,
    }
    if m["title"] != "title":
        fm[m["title"]] = title
    if due is not None:
        fm[m["due"]] = due
    if scheduled is not None:
        fm[m["scheduled"]] = scheduled
    if projects is not None:
        fm[m["projects"]] = list(projects)
    if recurrence is not None:
        if "recurrence" not in m:
            raise ValidationError("recurrence is not configured in the TaskNotes profile")
        fm[m["recurrence"]] = recurrence
    all_tags = list(tags or [])
    if profile.task_tag not in all_tags:
        all_tags.append(profile.task_tag)
    fm["tags"] = sorted(all_tags)
    if custom_fields:
        for key, value in custom_fields.items():
            if value is not None:
                fm[key] = value
    parts = [_serialize_frontmatter(fm)]
    if body:
        parts.append(body)
        if not body.endswith("\n"):
            parts.append("\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Semantic document model (for strong read-back verification)
# ---------------------------------------------------------------------------


@dataclass
class SemanticDocument:
    """Canonical semantic document for comparison.

    Contains type, title, tags, and full frontmatter excluding only the
    documented write-through provenance keys (``ingested_via``,
    ``ingested_at``, ``source_kind``, ``captured_via``, ``captured_at``)
    and the body/timeline (which are verified separately when relevant).
    """

    type: str
    title: str
    tags: Tuple[str, ...]
    frontmatter: Dict[str, Any]
    body: str
    timeline: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "title": self.title,
            "tags": list(self.tags),
            "frontmatter": dict(self.frontmatter),
            "body": self.body,
            "timeline": self.timeline,
        }


def _strip_provenance(fm: Mapping[str, Any]) -> Dict[str, Any]:
    """Return frontmatter without write-through provenance keys."""
    return {k: v for k, v in fm.items() if k not in WRITE_THROUGH_PROVENANCE_KEYS}


def _normalize_semantic_text(value: str) -> str:
    """Ignore serializer-only boundary newlines while preserving text content."""
    return value.strip("\n")


def _normalize_semantic_frontmatter(
    frontmatter: Mapping[str, Any], profile: TaskNotesProfile
) -> Dict[str, Any]:
    """Canonicalize frontmatter for gbrain/disk/request comparisons."""
    normalized = _strip_provenance(frontmatter)
    for key in ("type", "title", "tags", "slug", profile.mappings["title"]):
        normalized.pop(key, None)
    # Pinned gbrain normalizes bare TaskNotes dates to midnight UTC strings.
    for logical in ("due", "scheduled", "completedDate"):
        key = profile.mappings[logical]
        value = normalized.get(key)
        if isinstance(value, str) and _DATE_RE.fullmatch(value):
            normalized[key] = value + "T00:00:00.000Z"
    # Custom user fields of type "date" are also normalized by gbrain on disk.
    for uf in profile.user_fields:
        if uf["type"] != "date":
            continue
        key = uf["key"]
        value = normalized.get(key)
        if isinstance(value, str) and _DATE_RE.fullmatch(value):
            normalized[key] = value + "T00:00:00.000Z"
    return normalized


def semantic_from_gbrain(page: Dict[str, Any], profile: TaskNotesProfile) -> SemanticDocument:
    """Build a semantic document from a gbrain get_page response."""
    decoded = decode_page(page)
    return SemanticDocument(
        type=decoded["type"],
        title=decoded["title"],
        tags=tuple(decoded["tags"]),
        frontmatter=_normalize_semantic_frontmatter(decoded["frontmatter"], profile),
        body=_normalize_semantic_text(decoded["compiled_truth"]),
        timeline=_normalize_semantic_text(decoded["timeline"]),
    )


def semantic_from_disk(
    vault: Path,
    profile: TaskNotesProfile,
    slug: str,
    *,
    max_size: int = LIST_MAX_FILE_SIZE,
) -> SemanticDocument:
    """Strictly parse a task file from disk (no-follow, bounded) into a semantic document.

    Requires valid frontmatter and the configured task tag. Raises
    ``PathError`` if the path is a symlink or escapes, ``CoreError`` if
    the file is too large, has no valid frontmatter, or is missing the
    task tag.

    When ``moveArchivedTasks`` is true, checks the tasks folder first,
    then the archive folder.
    """
    validate_slug(slug)
    # Try the tasks folder first.
    folder = profile.tasks_folder
    try:
        directory_fd = _open_relative_directory_no_follow(vault, folder)
    except PathError:
        if not profile.move_archived_tasks or not profile.archive_folder:
            raise
        directory_fd = _open_relative_directory_no_follow(vault, profile.archive_folder)
        folder = profile.archive_folder
    try:
        try:
            text = _read_directory_entry_no_follow(
                directory_fd, f"{slug}.md", max_size=max_size
            )
        except PathError:
            if not profile.move_archived_tasks or not profile.archive_folder or folder == profile.archive_folder:
                raise
            os.close(directory_fd)
            directory_fd = _open_relative_directory_no_follow(vault, profile.archive_folder)
            folder = profile.archive_folder
            text = _read_directory_entry_no_follow(
                directory_fd, f"{slug}.md", max_size=max_size
            )
    finally:
        os.close(directory_fd)
    fm, raw_body = _parse_frontmatter(text)
    body, timeline = _split_body_timeline(raw_body)
    # Extract type/title/tags.
    ptype = fm.get("type", "note")
    title = fm.get(profile.mappings["title"], "")
    tags = fm.get("tags", [])
    if not isinstance(ptype, str) or not isinstance(title, str):
        raise CoreError("task file type/title must be strings")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise CoreError("task file tags must be a list of strings")
    tags_t = tuple(tags)
    # Require the task tag.
    if profile.task_tag not in tags_t:
        raise CoreError("task file missing the configured task tag")
    return SemanticDocument(
        type=ptype,
        title=title,
        tags=tags_t,
        frontmatter=_normalize_semantic_frontmatter(fm, profile),
        body=_normalize_semantic_text(body),
        timeline=_normalize_semantic_text(timeline),
    )


def semantic_from_markdown(
    markdown: str, profile: TaskNotesProfile
) -> SemanticDocument:
    """Build the intended semantic document from adapter-produced markdown."""
    fm, raw_body = _parse_frontmatter(markdown)
    body, timeline = _split_body_timeline(raw_body)
    ptype = fm.get("type", "note")
    title = fm.get("title", fm.get(profile.mappings["title"], ""))
    tags = fm.get("tags", [])
    if not isinstance(ptype, str) or not isinstance(title, str):
        raise CoreError("constructed markdown has invalid type/title")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise CoreError("constructed markdown has invalid tags")
    if profile.task_tag not in tags:
        raise CoreError("constructed markdown is missing the task tag")
    return SemanticDocument(
        type=ptype,
        title=title,
        tags=tuple(tags),
        frontmatter=_normalize_semantic_frontmatter(fm, profile),
        body=_normalize_semantic_text(body),
        timeline=_normalize_semantic_text(timeline),
    )


def semantic_documents_agree(
    gbrain_doc: SemanticDocument,
    disk_doc: SemanticDocument,
    profile: TaskNotesProfile,
) -> bool:
    """Return True if the gbrain and disk semantic documents agree.

    Compares type, title, tags, and full frontmatter (excluding provenance).
    """
    if gbrain_doc.type != disk_doc.type:
        return False
    if gbrain_doc.title != disk_doc.title:
        return False
    if sorted(gbrain_doc.tags) != sorted(disk_doc.tags):
        return False
    if gbrain_doc.frontmatter != disk_doc.frontmatter:
        return False
    if gbrain_doc.body != disk_doc.body:
        return False
    if gbrain_doc.timeline != disk_doc.timeline:
        return False
    return True


# ---------------------------------------------------------------------------
# Read-only structured listing (race-safe no-follow)
# ---------------------------------------------------------------------------


def list_tasks(
    vault: Path,
    profile: TaskNotesProfile,
    *,
    max_files: int = LIST_MAX_FILES,
    max_size: int = LIST_MAX_FILE_SIZE,
    max_results: int = LIST_MAX_RESULTS,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    tag: Optional[str] = None,
    archived: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Read-only structured listing of the tasks folder.

    Follows no symlinks; only regular ``.md`` files are listed. File size
    and result count are bounded. Frontmatter is parsed with
    ``yaml.safe_load``. Filters by the configured task tag: only files
    whose frontmatter ``tags`` include the task tag are returned. Modeled
    fields are extracted using the profile mappings; unknown frontmatter
    keys are dropped from the listing output.

    Optional filters (combined with AND logic):
      - ``status``: keep only tasks whose mapped status equals this value.
      - ``priority``: keep only tasks whose mapped priority equals this value.
      - ``tag``: keep only tasks whose tags list contains this tag.
      - ``archived``: ``True`` keeps only archived tasks (those carrying
        the configured archive tag), ``False`` keeps only non-archived
        tasks, ``None`` disables the archive filter.
    """
    results: List[Dict[str, Any]] = []
    count = 0
    try:
        directory_fd = _open_relative_directory_no_follow(
            vault, profile.tasks_folder
        )
    except PathError:
        return []
    try:
        entries = sorted(os.listdir(directory_fd))
        for name in entries:
            if len(results) >= max_results:
                break
            if count >= max_files:
                break
            if not name.endswith(".md"):
                continue
            count += 1
            try:
                text = _read_directory_entry_no_follow(
                    directory_fd, name, max_size=max_size
                )
            except (PathError, CoreError):
                continue
            try:
                fm, _body = _parse_frontmatter(text)
            except Exception:
                continue
            tags = fm.get("tags", [])
            if not isinstance(tags, list) or not all(
                isinstance(tag_item, str) for tag_item in tags
            ):
                continue
            tags_t = tuple(tags)
            if profile.task_tag not in tags_t:
                continue
            # Apply optional filters (AND logic).
            if status is not None:
                if fm.get(profile.mappings["status"]) != status:
                    continue
            if priority is not None:
                if fm.get(profile.mappings["priority"]) != priority:
                    continue
            if tag is not None:
                if tag not in tags_t:
                    continue
            if archived is not None:
                is_archived = profile.archive_tag in tags_t
                if archived and not is_archived:
                    continue
                if not archived and is_archived:
                    continue
            slug = name[:-3]
            modeled = _extract_modeled_fields(fm, profile)
            modeled["slug"] = slug
            modeled["type"] = str(fm.get("type", "note"))
            modeled["tags"] = list(tags_t)
            results.append(modeled)
    finally:
        os.close(directory_fd)
    return results


def _extract_modeled_fields(
    frontmatter: Mapping[str, Any],
    profile: TaskNotesProfile,
) -> Dict[str, Any]:
    """Extract modeled fields from frontmatter using profile mappings."""
    out: Dict[str, Any] = {}
    m = profile.mappings
    for logical in REQUIRED_MAPPINGS:
        key = m[logical]
        if key in frontmatter:
            out[logical] = frontmatter[key]
    return out


# ---------------------------------------------------------------------------
# Input validation (before preflight, no side effects)
# ---------------------------------------------------------------------------


def validate_title(title: Any) -> str:
    if not isinstance(title, str):
        raise ValidationError("title must be a string")
    title = title.strip()
    if not title:
        raise ValidationError("title must be non-empty")
    if len(title) > MAX_TITLE_LEN:
        raise ValidationError("title exceeds length bound")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in title):
        raise ValidationError("title must not contain control characters")
    return title


def validate_body(body: Any) -> str:
    if not isinstance(body, str):
        raise ValidationError("body must be a string")
    if len(body) > MAX_BODY_LEN:
        raise ValidationError("body exceeds length bound")
    return body


def _validate_markdown_bound(markdown: str) -> None:
    """Reject constructed markdown that exceeds the stdin byte bound before any gbrain mutation.

    The subprocess stdin cap (``MAX_MARKDOWN_LEN``) is byte-limited, while
    earlier field validation is character-based. For multibyte Unicode a
    string within the character count can still exceed the byte cap when
    encoded as UTF-8. This check makes the accepted-input contract
    deterministic: too-large content is rejected up front with the existing
    ``ValidationError`` taxonomy, before any gbrain side effect.
    """
    if len(markdown.encode("utf-8")) > MAX_MARKDOWN_LEN:
        raise ValidationError("constructed markdown exceeds length bound")


def validate_status_value(value: Any, profile: TaskNotesProfile) -> str:
    if not isinstance(value, str) or value not in profile.statuses:
        raise ValidationError(f"status {value!r} not in status set")
    return value


def validate_priority_value(value: Any, profile: TaskNotesProfile) -> str:
    if not isinstance(value, str) or value not in profile.priorities:
        raise ValidationError(f"priority {value!r} not in priority set")
    return value


def validate_date(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValidationError(f"{field_name} must be a YYYY-MM-DD date string")
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field_name} is not a valid date: {exc}") from exc
    return value


def validate_optional_date(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return validate_date(value, field_name)


def validate_recurrence(value: Any) -> str:
    """Validate an RFC 5545 RRULE recurrence string.

    Must be a non-empty string of at most ``MAX_RECURRENCE_LEN`` characters
    with no control characters. The adapter does not parse the RRULE; the
    TaskNotes plugin validates it on read.
    """
    if not isinstance(value, str):
        raise ValidationError("recurrence must be a string")
    if not value:
        raise ValidationError("recurrence must be a non-empty string")
    if len(value) > MAX_RECURRENCE_LEN:
        raise ValidationError("recurrence exceeds length bound")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        raise ValidationError("recurrence must not contain control characters")
    return value


def validate_tags(value: Any, profile: TaskNotesProfile, *, allow_archive: bool) -> List[str]:
    if not isinstance(value, list):
        raise ValidationError("tags must be a list")
    if len(value) > MAX_TAGS_COUNT:
        raise ValidationError("tags exceed count bound")
    out: List[str] = []
    seen: set = set()
    for t in value:
        if not isinstance(t, str) or not t:
            raise ValidationError("each tag must be a non-empty string")
        if len(t) > MAX_TAG_LEN:
            raise ValidationError("tag exceeds length bound")
        if not _TAG_RE.match(t):
            raise ValidationError("tag contains whitespace or control characters")
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    if not allow_archive and profile.archive_tag in out:
        raise ValidationError("archive tag is not allowed here")
    return out


def validate_projects(value: Any) -> List[str]:
    if not isinstance(value, list):
        raise ValidationError("projects must be a list")
    if len(value) > MAX_PROJECTS_COUNT:
        raise ValidationError("projects exceed count bound")
    out: List[str] = []
    seen: set = set()
    for p in value:
        if not isinstance(p, str) or not p:
            raise ValidationError("each project must be a non-empty string")
        if len(p) > MAX_PROJECT_LEN:
            raise ValidationError("project exceeds length bound")
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in p):
            raise ValidationError("project must not contain control characters")
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def validate_custom_fields(
    custom_fields: Any, profile: TaskNotesProfile
) -> Dict[str, Any]:
    """Validate a ``{field_key: value}`` dict against the profile user fields.

    Returns a normalized dict of ``{field_key: value}`` ready to write into
    frontmatter. A value of ``None`` is preserved (means "clear the field"
    in update). Raises ``ValidationError`` on unknown keys or type
    mismatches.
    """
    if custom_fields is None:
        return {}
    if not isinstance(custom_fields, dict):
        raise ValidationError("custom_fields must be a dict")
    # Build a lookup by key.
    by_key: Dict[str, Dict[str, Any]] = {uf["key"]: uf for uf in profile.user_fields}
    out: Dict[str, Any] = {}
    for key, value in custom_fields.items():
        if not isinstance(key, str) or not key:
            raise ValidationError("custom_fields keys must be non-empty strings")
        field = by_key.get(key)
        if field is None:
            raise ValidationError(f"custom field {key!r} is not defined in profile")
        ftype = field["type"]
        # None means "clear" and is always allowed.
        if value is None:
            out[key] = None
            continue
        if ftype == "text":
            if not isinstance(value, str):
                raise ValidationError(f"custom field {key!r} (text) must be a string")
            if len(value) > 500:
                raise ValidationError(f"custom field {key!r} (text) exceeds 500 chars")
        elif ftype == "list":
            if not isinstance(value, list):
                raise ValidationError(f"custom field {key!r} (list) must be a list")
            if len(value) > 50:
                raise ValidationError(f"custom field {key!r} (list) exceeds 50 items")
            for item in value:
                if not isinstance(item, str):
                    raise ValidationError(
                        f"custom field {key!r} (list) items must be strings"
                    )
                if len(item) > 200:
                    raise ValidationError(
                        f"custom field {key!r} (list) item exceeds 200 chars"
                    )
        elif ftype == "date":
            if not isinstance(value, str) or not _DATE_RE.match(value):
                raise ValidationError(
                    f"custom field {key!r} (date) must be a YYYY-MM-DD string"
                )
            try:
                datetime.date.fromisoformat(value)
            except ValueError as exc:
                raise ValidationError(
                    f"custom field {key!r} (date) is not a valid date: {exc}"
                ) from exc
        elif ftype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(
                    f"custom field {key!r} (number) must be a number"
                )
        elif ftype == "boolean":
            if not isinstance(value, bool):
                raise ValidationError(
                    f"custom field {key!r} (boolean) must be a bool"
                )
        elif ftype == "link":
            if not isinstance(value, str):
                raise ValidationError(f"custom field {key!r} (link) must be a string")
            if len(value) > 500:
                raise ValidationError(f"custom field {key!r} (link) exceeds 500 chars")
        elif ftype == "enum":
            if not isinstance(value, str):
                raise ValidationError(f"custom field {key!r} (enum) must be a string")
            options = field.get("options", [])
            if value not in options:
                raise ValidationError(
                    f"custom field {key!r} (enum) value {value!r} not in options {options}"
                )
        else:  # pragma: no cover - guarded by profile validation
            raise ValidationError(f"custom field {key!r} has unknown type {ftype!r}")
        out[key] = value
    return out


def _get_zoneinfo(tzname: str) -> datetime.tzinfo:
    """Return a tzinfo for the given timezone name, falling back to UTC."""
    try:
        import zoneinfo  # type: ignore
        return zoneinfo.ZoneInfo(tzname or "UTC")  # type: ignore
    except Exception:
        return datetime.timezone.utc


def today_in_tz(tz: str) -> str:
    """Return today's date as YYYY-MM-DD in the configured TZ."""
    return datetime.datetime.now(_get_zoneinfo(tz)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Mutation result
# ---------------------------------------------------------------------------


@dataclass
class MutationResult:
    """Structured outcome of a mutation operation."""

    state: str
    slug: str
    commit_id: Optional[str] = None
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TaskNotesEngine:
    """Core engine for the seven TaskNotes operations.

    All operations that invoke gbrain (including get) take the shared
    lock. List is lock-free because it never invokes gbrain. Mutations
    verify the profile, verify the gbrain source under the lock, run
    preflight (commit pending + incremental sync), re-check the profile
    immediately before capture, execute the gbrain capture, verify
    read-back against a strict disk parse, and commit the target path.
    """

    def __init__(
        self,
        *,
        vault: Path,
        gbrain_bin: str,
        gbrain_home: Path,
        lock_dir: Optional[Path] = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        tz: str = "UTC",
    ) -> None:
        self.vault = Path(vault)
        self.gbrain_bin = gbrain_bin
        self.gbrain_home = Path(gbrain_home)
        self.lock_dir = Path(lock_dir) if lock_dir is not None else DEFAULT_LOCK_DIR
        self.lock_timeout = lock_timeout
        self.lock_path = self.lock_dir / DEFAULT_LOCK_NAME
        self.recovery_marker = self.lock_dir / RECOVERY_MARKER_NAME
        self.tz = tz
        self._gbrain_env = _build_gbrain_env(self.gbrain_home, self.vault)
        self._git_env = _build_git_env()
        # Test seam: callable invoked after get_page/reconstruct and before
        # the pre-capture target/profile re-check, to simulate races.
        self._pre_capture_hook: Optional[Callable[["TaskNotesEngine", str, TaskNotesProfile], None]] = None

    # -- profile ----------------------------------------------------------

    def load_profile(self) -> TaskNotesProfile:
        return load_profile(self.vault, self.vault, git_env=self._git_env)

    def _check_recovery_marker(self) -> None:
        if target_exists_no_follow(self.recovery_marker):
            raise RecoveryRequired(
                "recovery marker present; mutations blocked until operator recovery"
            )

    def _write_recovery_marker(self) -> None:
        try:
            self.recovery_marker.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self.recovery_marker,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(fd, f"recovery required at {time.time()}\n".encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            # Marker-write failure still returns recovery_required.
            pass

    def _verify_source(self, profile: TaskNotesProfile) -> TaskNotesProfile:
        """Verify gbrain source under the lock; return profile with source_id."""
        return verify_gbrain_source(self.gbrain_bin, self._gbrain_env, self.vault, profile)

    # -- preflight --------------------------------------------------------

    def _preflight(self, profile: TaskNotesProfile) -> None:
        """Check Git state, commit pending edits, run incremental sync."""
        check_git_state(self.vault, self._git_env)
        git_preflight_commit(self.vault, self._git_env)
        gbrain_sync_incremental(
            self.gbrain_bin, self._gbrain_env, self.vault, profile.source_id  # type: ignore[arg-type]
        )

    def _verify_profile_stable(self, original_hash: str) -> TaskNotesProfile:
        """Reload the profile and verify the hash matches."""
        current = self.load_profile()
        if current.profile_hash != original_hash:
            raise ProfileIncompatible(
                "TaskNotes profile drifted during operation"
            )
        return current

    # -- pre-capture guards ------------------------------------------------

    def _create_pre_capture_guard(self, profile: TaskNotesProfile, slug: str) -> None:
        """Create pre-capture guard: target must be absent (no-follow) and get_page must report page_not_found."""
        target = resolve_task_path(self.vault, profile, slug)
        # lstat/no-follow: any disk entry (including symlink) => failure.
        try:
            st = os.lstat(str(target))
            raise ValidationError(f"create target already exists on disk: {target}")
        except FileNotFoundError:
            pass  # good
        except OSError as exc:
            raise ValidationError(f"cannot lstat target: {exc}") from exc
        # get_page must report page_not_found.
        gbrain_slug = resolve_gbrain_slug(profile, slug)
        try:
            page = gbrain_get_page(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id)  # type: ignore[arg-type]
            # If we got here, the page exists in the DB => failure.
            raise ValidationError(f"create target already exists in gbrain DB: {gbrain_slug}")
        except GbrainPageNotFound:
            pass

    def _mutation_pre_capture_guard(self, profile: TaskNotesProfile, slug: str) -> None:
        """Update/complete/archive pre-capture guard: target must exist and be Git-clean; profile hash re-read."""
        target = resolve_task_path_any(self.vault, profile, slug)
        # Target must exist (no-follow).
        if target is None:
            raise ValidationError(f"target does not exist on disk: {slug}")
        # Target must be Git-clean.
        if not git_target_clean(self.vault, target, self._git_env):
            raise ValidationError(f"target has uncommitted changes: {target}")
        # Re-read profile manifest/data and verify hash.
        current = self.load_profile()
        if current.profile_hash != profile.profile_hash:
            raise ProfileIncompatible("TaskNotes profile drifted before capture")
        current = self._verify_source(current)
        if current.source_id != profile.source_id:
            raise ProfileIncompatible("gbrain source routing drifted before capture")

    # -- read-back verification ------------------------------------------

    def _verify_readback(
        self,
        profile: TaskNotesProfile,
        slug: str,
        gbrain_slug: str,
        *,
        expected_document: Optional[SemanticDocument] = None,
    ) -> None:
        """Verify structured gbrain and strict disk semantic documents agree."""
        page = gbrain_get_page(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id)  # type: ignore[arg-type]
        gbrain_doc = semantic_from_gbrain(page, profile)
        disk_doc = semantic_from_disk(self.vault, profile, slug)
        if not semantic_documents_agree(gbrain_doc, disk_doc, profile):
            raise CoreError("read-back semantic mismatch between gbrain and disk")
        if expected_document is not None and not semantic_documents_agree(
            gbrain_doc, expected_document, profile
        ):
            raise CoreError("read-back does not match the intended task document")

    # -- consolidated post-capture handling -----------------------------------

    def _handle_post_capture(
        self,
        profile: TaskNotesProfile,
        slug: str,
        gbrain_slug: str,
        capture_result: Dict[str, Any],
        *,
        expected_document: Optional[SemanticDocument] = None,
    ) -> MutationResult:
        """Consolidated post-capture handling.

        Once capture begins, this method always returns a MutationResult and
        never escapes generically. Reconciles gbrain + disk against the
        pre-capture snapshot and intended state. Outcomes:
          - verified applied + commit ok => applied_and_committed
          - verified applied + commit fail => applied_uncommitted
          - DB changed + disk unchanged => full sync recovery; if verified
            => db_updated_disk_failed; if not => recovery marker + recovery_required
          - uncertain/mismatch => recovery marker + recovery_required

        ``gbrain capture --json`` reports write-through success as a
        top-level ``written`` boolean (not nested under ``write_through``).
        A structured ``written: false`` remains a hard failure before any
        post-write Git operations.
        """
        written = capture_result.get("written", False)
        if not isinstance(written, bool):
            written = False
        if not written:
            # DB updated, disk failed: run immediate locked full sync from
            # unchanged disk and verify recovery.
            return self._recover_from_disk_failure(profile, slug, gbrain_slug)
        # Verify read-back.
        try:
            self._verify_readback(
                profile,
                slug,
                gbrain_slug,
                expected_document=expected_document,
            )
        except Exception:
            return self._recover_from_disk_failure(profile, slug, gbrain_slug)
        # Commit target (may be in archive folder if the plugin moved it).
        target = resolve_task_path_any(self.vault, profile, slug)
        if target is None:
            target = resolve_task_path(self.vault, profile, slug)
        try:
            git_commit_target(self.vault, target, self._git_env)
        except Exception:
            return MutationResult(state=APPLIED_UNCOMMITTED, slug=slug)
        try:
            target_clean = git_target_clean(self.vault, target, self._git_env)
            commit_id = git_head_id(self.vault, self._git_env)
        except Exception:
            return MutationResult(state=APPLIED_UNCOMMITTED, slug=slug)
        if not target_clean:
            return MutationResult(state=APPLIED_UNCOMMITTED, slug=slug)
        return MutationResult(
            state=APPLIED_AND_COMMITTED,
            slug=slug,
            commit_id=commit_id,
        )

    def _recover_from_disk_failure(
        self,
        profile: TaskNotesProfile,
        slug: str,
        gbrain_slug: str,
    ) -> MutationResult:
        """Run immediate locked full sync and verify recovery."""
        try:
            gbrain_sync_full(self.gbrain_bin, self._gbrain_env, self.vault, profile.source_id)  # type: ignore[arg-type]
        except Exception:
            self._write_recovery_marker()
            return MutationResult(state=RECOVERY_REQUIRED, slug=slug)
        try:
            self._verify_readback(profile, slug, gbrain_slug)
        except Exception:
            self._write_recovery_marker()
            return MutationResult(state=RECOVERY_REQUIRED, slug=slug)
        return MutationResult(state=DB_UPDATED_DISK_FAILED, slug=slug)

    # -- public operations ------------------------------------------------

    def create(
        self,
        slug: Optional[str],
        title: str,
        *,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        due: Optional[str] = None,
        scheduled: Optional[str] = None,
        projects: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        body: str = "",
        custom_fields: Optional[Dict[str, Any]] = None,
        recurrence: Optional[str] = None,
    ) -> MutationResult:
        """Create a new task. Rejects completed status and archive tag.

        When ``slug`` is ``None``, a slug is auto-generated from the title
        and current timestamp (``YYYY-MM-DD-HHmmss-slugified-title``).
        """
        # Input validation BEFORE preflight (no side effects).
        if slug is None:
            slug = generate_slug(title, tz=self.tz)
        validate_slug(slug)
        title = validate_title(title)
        body = validate_body(body)
        recurrence_v = validate_recurrence(recurrence) if recurrence is not None else None
        with Lock(self.lock_path, timeout=self.lock_timeout):
            self._check_recovery_marker()
            profile = self.load_profile()
            st = status if status is not None else profile.default_status
            if st == profile.completed_status:
                raise ValidationError("create rejects completed status")
            st = validate_status_value(st, profile)
            pr = priority if priority is not None else profile.default_priority
            pr = validate_priority_value(pr, profile)
            due_v = validate_optional_date(due, "due")
            scheduled_v = validate_optional_date(scheduled, "scheduled")
            projects_v = validate_projects(projects) if projects is not None else None
            tags_v = validate_tags(tags, profile, allow_archive=False) if tags is not None else None
            custom_fields_v = validate_custom_fields(custom_fields, profile)
            # Verify gbrain source under lock.
            profile = self._verify_source(profile)
            # Preflight.
            original_hash = profile.profile_hash
            self._preflight(profile)
            # Re-check profile stability.
            profile = self._verify_profile_stable(original_hash)
            profile = self._verify_source(profile)
            # Pre-capture guard: target absent + DB page_not_found.
            self._create_pre_capture_guard(profile, slug)
            profile = self._verify_profile_stable(original_hash)
            profile = self._verify_source(profile)
            # Build markdown.
            markdown = build_create_markdown(
                profile, title, st, pr, due_v, scheduled_v, projects_v, tags_v, body,
                custom_fields_v, recurrence_v,
            )
            _validate_markdown_bound(markdown)
            expected_document = semantic_from_markdown(markdown, profile)
            gbrain_slug = resolve_gbrain_slug(profile, slug)
            # CAPTURE-STARTED BOUNDARY: from here on, always return a MutationResult.
            try:
                capture_result = gbrain_capture(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id, markdown)  # type: ignore[arg-type]
            except Exception as exc:
                # Capture invocation failed after starting; reconcile.
                return self._reconcile_after_capture_failure(
                    profile, slug, gbrain_slug, exc, expected_document
                )
            return self._handle_post_capture(
                profile,
                slug,
                gbrain_slug,
                capture_result,
                expected_document=expected_document,
            )

    def get(self, slug: str) -> Dict[str, Any]:
        """Return one task by slug. Takes the shared lock (invokes gbrain)."""
        validate_slug(slug)
        with Lock(self.lock_path, timeout=self.lock_timeout):
            profile = self.load_profile()
            profile = self._verify_source(profile)
            gbrain_slug = resolve_gbrain_slug(profile, slug)
            page = gbrain_get_page(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id)  # type: ignore[arg-type]
            decoded = decode_page(page)
            modeled = _extract_modeled_fields(decoded["frontmatter"], profile)
            modeled["slug"] = slug
            modeled["type"] = decoded["type"]
            modeled["title"] = decoded["title"]
            modeled["tags"] = list(decoded["tags"])
            modeled["body"] = decoded["compiled_truth"]
            modeled["timeline"] = decoded["timeline"]
            return modeled

    def list(
        self,
        *,
        max_results: int = LIST_MAX_RESULTS,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        tag: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Read-only structured listing of the tasks folder (lock-free)."""
        profile = self.load_profile()
        return list_tasks(
            self.vault,
            profile,
            max_results=max_results,
            status=status,
            priority=priority,
            tag=tag,
            archived=archived,
        )

    def update(
        self,
        slug: str,
        *,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        due: Optional[str] = None,
        scheduled: Optional[str] = None,
        projects: Optional[List[str]] = None,
        clear_due: bool = False,
        clear_scheduled: bool = False,
        clear_projects: bool = False,
        custom_fields: Optional[Dict[str, Any]] = None,
        body: Optional[str] = None,
    ) -> MutationResult:
        """Update status/priority/dates/projects/custom fields and optionally the body.

        ``body`` semantics: ``None`` (default) leaves the body unchanged;
        ``""`` clears the body; a non-empty string replaces the body
        content. Body edits do not affect the title (title edits remain
        unsupported). No completion transition.
        """
        validate_slug(slug)
        # Input validation BEFORE preflight.
        body_v = validate_body(body) if body is not None else None
        if status is not None:
            status_v: Optional[str] = status
        else:
            status_v = None
        if priority is not None:
            priority_v: Optional[str] = priority
        else:
            priority_v = None
        due_v = validate_optional_date(due, "due") if due is not None else None
        scheduled_v = validate_optional_date(scheduled, "scheduled") if scheduled is not None else None
        projects_v = validate_projects(projects) if projects is not None else None
        for name, value in (
            ("clear_due", clear_due),
            ("clear_scheduled", clear_scheduled),
            ("clear_projects", clear_projects),
        ):
            if not isinstance(value, bool):
                raise ValidationError(f"{name} must be a boolean")
        if due is not None and clear_due:
            raise ValidationError("due and clear_due are mutually exclusive")
        if scheduled is not None and clear_scheduled:
            raise ValidationError("scheduled and clear_scheduled are mutually exclusive")
        if projects is not None and clear_projects:
            raise ValidationError("projects and clear_projects are mutually exclusive")
        with Lock(self.lock_path, timeout=self.lock_timeout):
            self._check_recovery_marker()
            profile = self.load_profile()
            # Validate status if provided.
            if status_v is not None:
                if status_v == profile.completed_status:
                    raise ValidationError("update rejects completed status; use complete")
                status_v = validate_status_value(status_v, profile)
            if priority_v is not None:
                priority_v = validate_priority_value(priority_v, profile)
            custom_fields_v = validate_custom_fields(custom_fields, profile)
            # Verify gbrain source under lock.
            profile = self._verify_source(profile)
            # Preflight.
            original_hash = profile.profile_hash
            self._preflight(profile)
            # Re-check profile stability.
            profile = self._verify_profile_stable(original_hash)
            profile = self._verify_source(profile)
            gbrain_slug = resolve_gbrain_slug(profile, slug)
            # Get current state.
            page = gbrain_get_page(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id)  # type: ignore[arg-type]
            decoded = decode_page(page)
            current_fm = decoded["frontmatter"]
            # Reject if current status is completed.
            current_status = current_fm.get(profile.mappings["status"])
            if current_status == profile.completed_status:
                raise ValidationError("cannot update a completed task")
            # Build updates (only modeled fields, no tags/title/completedDate).
            updates: Dict[str, Any] = {}
            if status_v is not None:
                updates["status"] = status_v
            if priority_v is not None:
                updates["priority"] = priority_v
            if clear_due:
                updates["due"] = None
            elif due_v is not None:
                updates["due"] = due_v
            if clear_scheduled:
                updates["scheduled"] = None
            elif scheduled_v is not None:
                updates["scheduled"] = scheduled_v
            if clear_projects:
                updates["projects"] = None
            elif projects_v is not None:
                updates["projects"] = list(projects_v)
            # Custom field updates (None means clear).
            for key, value in custom_fields_v.items():
                updates[key] = value
            if not updates and body_v is None:
                return MutationResult(state=NOT_APPLIED, slug=slug)
            markdown = reconstruct_markdown(
                page, profile, updates, body_override=body_v
            )
            _validate_markdown_bound(markdown)
            expected_document = semantic_from_markdown(markdown, profile)
            # Pre-capture guard: target exists + Git-clean + profile hash re-read.
            self._mutation_pre_capture_guard(profile, slug)
            # Test seam: race hook.
            if self._pre_capture_hook is not None:
                self._pre_capture_hook(self, slug, profile)
                # Re-verify after hook.
                self._mutation_pre_capture_guard(profile, slug)
            # CAPTURE-STARTED BOUNDARY.
            try:
                capture_result = gbrain_capture(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id, markdown)  # type: ignore[arg-type]
            except Exception as exc:
                return self._reconcile_after_capture_failure(
                    profile, slug, gbrain_slug, exc, expected_document
                )
            return self._handle_post_capture(
                profile,
                slug,
                gbrain_slug,
                capture_result,
                expected_document=expected_document,
            )

    def complete(
        self,
        slug: str,
        *,
        completion_date: Optional[str] = None,
    ) -> MutationResult:
        """Set the completed status and completion date. Idempotent.

        Explicit ``completion_date`` must be valid ``YYYY-MM-DD``. Omitted
        date generates today's date in the configured TZ. Already-completed
        tasks preserve the existing completion date (idempotent).
        """
        validate_slug(slug)
        # Input validation BEFORE preflight.
        date_v = validate_date(completion_date, "completion_date") if completion_date is not None else None
        with Lock(self.lock_path, timeout=self.lock_timeout):
            self._check_recovery_marker()
            profile = self.load_profile()
            profile = self._verify_source(profile)
            original_hash = profile.profile_hash
            self._preflight(profile)
            profile = self._verify_profile_stable(original_hash)
            profile = self._verify_source(profile)
            gbrain_slug = resolve_gbrain_slug(profile, slug)
            page = gbrain_get_page(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id)  # type: ignore[arg-type]
            decoded = decode_page(page)
            current_fm = decoded["frontmatter"]
            current_status = current_fm.get(profile.mappings["status"])
            # Idempotent: already completed.
            if current_status == profile.completed_status:
                return MutationResult(state=NOT_APPLIED, slug=slug)
            # Determine completion date.
            if date_v is not None:
                final_date = date_v
            else:
                final_date = today_in_tz(self.tz)
            updates: Dict[str, Any] = {
                "status": profile.completed_status,
                "completedDate": final_date,
            }
            markdown = reconstruct_markdown(page, profile, updates)
            _validate_markdown_bound(markdown)
            expected_document = semantic_from_markdown(markdown, profile)
            # Pre-capture guard.
            self._mutation_pre_capture_guard(profile, slug)
            if self._pre_capture_hook is not None:
                self._pre_capture_hook(self, slug, profile)
                self._mutation_pre_capture_guard(profile, slug)
            # CAPTURE-STARTED BOUNDARY.
            try:
                capture_result = gbrain_capture(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id, markdown)  # type: ignore[arg-type]
            except Exception as exc:
                return self._reconcile_after_capture_failure(
                    profile, slug, gbrain_slug, exc, expected_document
                )
            return self._handle_post_capture(
                profile,
                slug,
                gbrain_slug,
                capture_result,
                expected_document=expected_document,
            )

    def archive(self, slug: str) -> MutationResult:
        """Add the archive tag idempotently."""
        validate_slug(slug)
        with Lock(self.lock_path, timeout=self.lock_timeout):
            self._check_recovery_marker()
            profile = self.load_profile()
            profile = self._verify_source(profile)
            original_hash = profile.profile_hash
            self._preflight(profile)
            profile = self._verify_profile_stable(original_hash)
            profile = self._verify_source(profile)
            gbrain_slug = resolve_gbrain_slug(profile, slug)
            page = gbrain_get_page(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id)  # type: ignore[arg-type]
            decoded = decode_page(page)
            current_tags = list(decoded["tags"])
            if profile.archive_tag in current_tags:
                return MutationResult(state=NOT_APPLIED, slug=slug)
            new_tags = sorted(set(current_tags) | {profile.archive_tag})
            updates: Dict[str, Any] = {"tags": new_tags}
            markdown = reconstruct_markdown(page, profile, updates)
            _validate_markdown_bound(markdown)
            expected_document = semantic_from_markdown(markdown, profile)
            # Pre-capture guard.
            self._mutation_pre_capture_guard(profile, slug)
            if self._pre_capture_hook is not None:
                self._pre_capture_hook(self, slug, profile)
                self._mutation_pre_capture_guard(profile, slug)
            # CAPTURE-STARTED BOUNDARY.
            try:
                capture_result = gbrain_capture(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id, markdown)  # type: ignore[arg-type]
            except Exception as exc:
                return self._reconcile_after_capture_failure(
                    profile, slug, gbrain_slug, exc, expected_document
                )
            return self._handle_post_capture(
                profile,
                slug,
                gbrain_slug,
                capture_result,
                expected_document=expected_document,
            )

    def delete(self, slug: str) -> MutationResult:
        """Delete a task: gbrain soft-delete, git rm the file, git commit.

        This is the only mutation that removes a file from disk instead of
        writing through ``gbrain capture``. The gbrain delete is a soft-delete
        confirmation gate; once confirmed, the file is removed via ``git
        rm`` and the deletion committed. Idempotent: returns NOT_APPLIED if
        the task is already absent from both gbrain and disk.

        Target cleanliness is checked BEFORE preflight so manual edits are
        not silently committed before a destructive operation.
        """
        validate_slug(slug)
        with Lock(self.lock_path, timeout=self.lock_timeout):
            self._check_recovery_marker()
            profile = self.load_profile()
            profile = self._verify_source(profile)
            original_hash = profile.profile_hash

            # Idempotency and pre-guard BEFORE preflight so dirty edits
            # are not silently committed before deletion.
            gbrain_slug = resolve_gbrain_slug(profile, slug)
            target = resolve_task_path_any(self.vault, profile, slug)
            if target is None:
                try:
                    gbrain_get_page(
                        self.gbrain_bin, self._gbrain_env,
                        gbrain_slug, profile.source_id,  # type: ignore[arg-type]
                    )
                except GbrainPageNotFound:
                    return MutationResult(
                        state=NOT_APPLIED, slug=slug,
                        detail="task already deleted",
                    )
                raise CoreError(
                    "task exists in gbrain but file is missing on disk"
                )

            # Target must be git-clean before we touch anything.
            if not git_target_clean(self.vault, target, self._git_env):
                raise ValidationError(
                    f"target has uncommitted changes: {target}"
                )

            # Now safe: preflight commits pending unrelated edits and syncs.
            self._preflight(profile)
            profile = self._verify_profile_stable(original_hash)
            profile = self._verify_source(profile)
            # Re-resolve in case preflight changed things.
            gbrain_slug = resolve_gbrain_slug(profile, slug)

            # Gbrain soft-delete: confirmation gate.
            try:
                gbrain_delete(
                    self.gbrain_bin, self._gbrain_env,
                    gbrain_slug, profile.source_id,  # type: ignore[arg-type]
                )
            except GbrainError:
                raise

            # Git rm: remove file from disk + stage.
            try:
                git_rm_and_commit(self.vault, target, self._git_env)
            except GitError:
                # Gbrain soft-deleted, but file removal failed. The file
                # remains on disk — next sync will re-import it, undoing
                # the soft-delete. Conservative: mark recovery required.
                return MutationResult(
                    state=RECOVERY_REQUIRED, slug=slug,
                    detail="gbrain deleted but git rm failed",
                )
            # Recreate the parent directory if git rm cleaned it up
            # (happens when this was the last file in the directory).
            target.parent.mkdir(exist_ok=True)

            try:
                commit_id = git_head_id(self.vault, self._git_env)
            except Exception:
                return MutationResult(
                    state=APPLIED_UNCOMMITTED, slug=slug,
                )

            # Verify: gbrain get_page must now report page_not_found.
            try:
                gbrain_get_page(
                    self.gbrain_bin, self._gbrain_env,
                    gbrain_slug, profile.source_id,  # type: ignore[arg-type]
                )
                return MutationResult(
                    state=RECOVERY_REQUIRED, slug=slug,
                    detail="gbrain delete did not take effect",
                )
            except GbrainPageNotFound:
                pass

            return MutationResult(
                state=APPLIED_AND_COMMITTED,
                slug=slug,
                commit_id=commit_id,
            )

    def _validate_tag_value(self, tag: Any) -> str:
        """Validate a single tag string against format and length bounds."""
        if not isinstance(tag, str) or not tag:
            raise ValidationError("tag must be a non-empty string")
        if len(tag) > MAX_TAG_LEN:
            raise ValidationError("tag exceeds length bound")
        if not _TAG_RE.match(tag):
            raise ValidationError("tag contains whitespace or control characters")
        return tag

    def add_tag(self, slug: str, tag: str) -> MutationResult:
        """Add a custom tag idempotently. Rejects the task and archive tags."""
        validate_slug(slug)
        tag = self._validate_tag_value(tag)
        with Lock(self.lock_path, timeout=self.lock_timeout):
            self._check_recovery_marker()
            profile = self.load_profile()
            if tag == profile.task_tag:
                raise ValidationError(
                    "cannot add the task-identification tag; it is always present"
                )
            if tag == profile.archive_tag:
                raise ValidationError(
                    "cannot add the archive tag; use task_archive"
                )
            profile = self._verify_source(profile)
            original_hash = profile.profile_hash
            self._preflight(profile)
            profile = self._verify_profile_stable(original_hash)
            profile = self._verify_source(profile)
            gbrain_slug = resolve_gbrain_slug(profile, slug)
            page = gbrain_get_page(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id)  # type: ignore[arg-type]
            decoded = decode_page(page)
            current_tags = list(decoded["tags"])
            if tag in current_tags:
                return MutationResult(state=NOT_APPLIED, slug=slug)
            if len(current_tags) + 1 > MAX_TAGS_COUNT:
                raise ValidationError("adding the tag would exceed the tag count bound")
            new_tags = sorted(set(current_tags) | {tag})
            updates: Dict[str, Any] = {"tags": new_tags}
            markdown = reconstruct_markdown(page, profile, updates)
            _validate_markdown_bound(markdown)
            expected_document = semantic_from_markdown(markdown, profile)
            self._mutation_pre_capture_guard(profile, slug)
            if self._pre_capture_hook is not None:
                self._pre_capture_hook(self, slug, profile)
                self._mutation_pre_capture_guard(profile, slug)
            try:
                capture_result = gbrain_capture(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id, markdown)  # type: ignore[arg-type]
            except Exception as exc:
                return self._reconcile_after_capture_failure(
                    profile, slug, gbrain_slug, exc, expected_document
                )
            return self._handle_post_capture(
                profile,
                slug,
                gbrain_slug,
                capture_result,
                expected_document=expected_document,
            )

    def remove_tag(self, slug: str, tag: str) -> MutationResult:
        """Remove a custom tag idempotently. Rejects the task and archive tags."""
        validate_slug(slug)
        tag = self._validate_tag_value(tag)
        with Lock(self.lock_path, timeout=self.lock_timeout):
            self._check_recovery_marker()
            profile = self.load_profile()
            if tag == profile.task_tag:
                raise ValidationError(
                    "cannot remove the task-identification tag"
                )
            if tag == profile.archive_tag:
                raise ValidationError(
                    "cannot remove the archive tag; use a future unarchive tool"
                )
            profile = self._verify_source(profile)
            original_hash = profile.profile_hash
            self._preflight(profile)
            profile = self._verify_profile_stable(original_hash)
            profile = self._verify_source(profile)
            gbrain_slug = resolve_gbrain_slug(profile, slug)
            page = gbrain_get_page(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id)  # type: ignore[arg-type]
            decoded = decode_page(page)
            current_tags = list(decoded["tags"])
            if tag not in current_tags:
                return MutationResult(state=NOT_APPLIED, slug=slug)
            new_tags = sorted(set(current_tags) - {tag})
            updates: Dict[str, Any] = {"tags": new_tags}
            markdown = reconstruct_markdown(page, profile, updates)
            _validate_markdown_bound(markdown)
            expected_document = semantic_from_markdown(markdown, profile)
            self._mutation_pre_capture_guard(profile, slug)
            if self._pre_capture_hook is not None:
                self._pre_capture_hook(self, slug, profile)
                self._mutation_pre_capture_guard(profile, slug)
            # Gbrain's write-through reconciles tags additively: it re-adds
            # all DB tags to the file even if the new frontmatter omits them.
            # We must untag from the DB FIRST, then capture the new markdown
            # so the write-through picks up the updated DB tag set.
            try:
                gbrain_untag(self.gbrain_bin, self._gbrain_env, gbrain_slug, tag, profile.source_id)  # type: ignore[arg-type]
            except GbrainError:
                pass  # Best-effort; continue with capture.
            try:
                capture_result = gbrain_capture(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id, markdown)  # type: ignore[arg-type]
            except Exception as exc:
                return self._reconcile_after_capture_failure(
                    profile, slug, gbrain_slug, exc, expected_document
                )
            return self._handle_post_capture(
                profile,
                slug,
                gbrain_slug,
                capture_result,
                expected_document=expected_document,
            )

    def _reconcile_after_capture_failure(
        self,
        profile: TaskNotesProfile,
        slug: str,
        gbrain_slug: str,
        exc: Exception,
        expected_document: SemanticDocument,
    ) -> MutationResult:
        """Reconcile after a capture invocation failed (nonzero/timeout/invalid JSON).

        Attempts a read-back; if the DB was updated and disk matches,
        treat as applied_uncommitted (commit not attempted). Otherwise
        write the recovery marker and return recovery_required.
        """
        try:
            page = gbrain_get_page(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id)  # type: ignore[arg-type]
            gbrain_doc = semantic_from_gbrain(page, profile)
            disk_doc = semantic_from_disk(self.vault, profile, slug)
            if semantic_documents_agree(gbrain_doc, disk_doc, profile) and semantic_documents_agree(
                gbrain_doc, expected_document, profile
            ):
                return MutationResult(state=APPLIED_UNCOMMITTED, slug=slug)
        except Exception:
            pass
        self._write_recovery_marker()
        return MutationResult(state=RECOVERY_REQUIRED, slug=slug, detail="capture invocation failed")
