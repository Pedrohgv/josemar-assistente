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
   - Week planning (issue #128) is the semantic ``planned_week`` argument:
     a valid ``YYYY-MM-DD`` Monday stored under the raw ``planned_week``
     key, mutually exclusive with ``scheduled`` on MCP writes, reserved
     from generic custom_fields, and requiring a profile ``userFields``
     entry of type ``date`` to set. Rewrites normalize a manually
     inconsistent pair to scheduled-only; reads never mutate.
   - Daily Notes projection primitives (issue #139 W1b) are internal-only:
     they prepare transformed Daily Note bytes and apply them with a
     no-follow optimistic atomic writer. They only accept a validated
     ``DailyNotesConfig`` plus a resolved Daily Note target/operation,
     never write task files, and never invoke gbrain/PGLite.
   - Daily-links reconciliation (issue #139 W2a/W2b) is internal-only
     and never writes task Markdown, never invokes gbrain/PGLite, and
     never uses the recovery marker. W2a added read-only foundations: a
     structural cursor/pending pair at fixed runtime paths, bounded
     no-follow atomic file primitives, a fixed-argv streamed Git object
     reader, and bounded candidate enumeration/snapshots. W2b adds the
     prepare/apply/targeted-commit/finalize lifecycle on those
     foundations; only finalize advances the cursor, and only after the
     caller confirms native sync success.
   - Missing-cursor bootstrap reconciliation (issue #144 W1) is the
     only path allowed past the composed-transition bound: it composes
     through an internal bootstrap-only seam and applies deterministic
     batches of at most ``MAX_DAILY_PROJECTION_TARGETS`` distinct Daily
     Note targets, one targeted commit per batch (no config rider).
     Established reconciliation keeps the >16 fail-closed policy. W2
     adds in-memory-only full candidate evidence on the plan and
     full-candidate + locally-evolving expected-HEAD rechecks before
     the first batch, between bootstrap batches, and immediately
     before pending publication; external drift fails closed with no
     pending/cursor completion and no rollback.
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
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

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

# Semantic week-planning field (issue #128). Stored under the raw
# ``planned_week`` frontmatter key as the ISO ``YYYY-MM-DD`` date of that
# week's Monday. It is a first-class MCP argument, NOT a fieldMapping
# entry: setting it requires a profile ``userFields`` entry with this key
# and type ``date``, and the key is reserved from generic custom_fields.
# Native ``scheduled`` wins: an MCP rewrite never persists both keys.
PLANNED_WEEK_KEY = "planned_week"

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
# Pinned gbrain normalizes bare TaskNotes dates to midnight UTC on read.
# This is the exact normalized form of a bare ``YYYY-MM-DD`` date as
# returned by ``gbrain get_page`` frontmatter. The write path collapses
# it back to the plain bare-date form so disk frontmatter stays canonical.
_NORMALIZED_BARE_DATE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T00:00:00\.000Z$"
)

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


def _require_planned_week_user_field(profile: TaskNotesProfile) -> Dict[str, Any]:
    """Require a ``planned_week`` user field of type ``date`` in the profile.

    Week planning (issue #128) is stored under the raw ``planned_week``
    frontmatter key, which must be declared as a TaskNotes user field of
    type ``date`` for correct plugin/UI behavior. Setting the field fails
    explicitly (before any side effect) when the definition is missing or
    has an incompatible type. Normalization of already-stored values does
    NOT require this definition.
    """
    for uf in profile.user_fields:
        if uf["key"] == PLANNED_WEEK_KEY:
            if uf["type"] != "date":
                raise ValidationError(
                    f"profile user field {PLANNED_WEEK_KEY!r} must have type "
                    f"'date', found {uf['type']!r}"
                )
            return uf
    raise ValidationError(
        f"profile must define a {PLANNED_WEEK_KEY!r} user field of type "
        "'date' to set week planning"
    )


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

    # Planning-state invariant (issue #128): native ``scheduled`` wins over
    # semantic week planning. Whenever a rewrite leaves both keys present
    # (only possible via manual Obsidian edits), drop the stale
    # ``planned_week`` so no MCP write persists the inconsistent pair.
    # Centralized here so every non-delete rewrite (update, complete,
    # archive, tag paths) normalizes; reads never mutate and delete is
    # untouched. Works regardless of profile userFields configuration.
    if PLANNED_WEEK_KEY in fm and profile.mappings["scheduled"] in fm:
        fm.pop(PLANNED_WEEK_KEY, None)

    # Rehydrate structural fields that gbrain returns only at the page level.
    fm["type"] = decoded["type"]
    fm["title"] = decoded["title"]
    if profile.mappings["title"] != "title":
        fm.setdefault(profile.mappings["title"], decoded["title"])
    if "tags" not in updates:
        fm["tags"] = list(decoded["tags"])

    # Collapse gbrain-normalized bare dates back to plain ``YYYY-MM-DD``.
    # gbrain returns bare TaskNotes dates as ``YYYY-MM-DDT00:00:00.000Z``;
    # the write path serializes them as plain dates so disk frontmatter
    # stays canonical. Caller-supplied dates are already plain
    # ``YYYY-MM-DD`` (validated) and pass through unchanged; true
    # datetimes (any other ISO form) are preserved verbatim.
    date_keys = _date_valued_frontmatter_keys(profile)
    for key in date_keys:
        if key in fm:
            fm[key] = _denormalize_bare_date(fm[key])

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
    planned_week: Optional[str] = None,
) -> str:
    """Build markdown for a new task (no existing page).

    ``planned_week`` is the semantic week-planning target (issue #128):
    mutually exclusive with ``scheduled`` and written under the raw
    ``planned_week`` key, which must be declared as a profile user field
    of type ``date``.
    """
    if scheduled is not None and planned_week is not None:
        raise ValidationError(
            "scheduled and planned_week are mutually exclusive; "
            "choose one planning target"
        )
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
    if planned_week is not None:
        _require_planned_week_user_field(profile)
        fm[PLANNED_WEEK_KEY] = planned_week
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
    # Pinned gbrain normalizes bare TaskNotes dates to midnight UTC strings:
    # the modeled date mappings, the semantic ``planned_week`` key
    # (regardless of profile userFields configuration), and custom user
    # fields declared with type ``date``.
    date_keys = [profile.mappings[logical] for logical in ("due", "scheduled", "completedDate")]
    date_keys.append(PLANNED_WEEK_KEY)
    date_keys.extend(uf["key"] for uf in profile.user_fields if uf["type"] == "date")
    for key in date_keys:
        value = normalized.get(key)
        if isinstance(value, str) and _DATE_RE.fullmatch(value):
            normalized[key] = value + "T00:00:00.000Z"
    return normalized


def _date_valued_frontmatter_keys(profile: TaskNotesProfile) -> set:
    """Return the set of frontmatter keys that hold bare-date values.

    Includes the modeled date mappings (``due``, ``scheduled``,
    ``completedDate``), the semantic ``planned_week`` key (always, even
    when the profile lacks its userFields definition), and any custom
    user fields declared with ``type: date``. These are the keys whose
    values gbrain normalizes to ``YYYY-MM-DDT00:00:00.000Z`` on read and
    that the write path must collapse back to plain ``YYYY-MM-DD``.
    """
    keys = {profile.mappings[logical] for logical in ("due", "scheduled", "completedDate")}
    keys.add(PLANNED_WEEK_KEY)
    for uf in profile.user_fields:
        if uf["type"] == "date":
            keys.add(uf["key"])
    return keys


def _denormalize_bare_date(value: Any) -> Any:
    """Collapse a gbrain-normalized bare date back to ``YYYY-MM-DD``.

    gbrain returns bare TaskNotes dates as ``YYYY-MM-DDT00:00:00.000Z``.
    The write path serializes them as plain ``YYYY-MM-DD`` so disk
    frontmatter stays canonical. True datetimes (any other ISO form) and
    non-string values are returned unchanged.
    """
    if isinstance(value, str):
        m = _NORMALIZED_BARE_DATE_RE.match(value)
        if m is not None:
            return m.group(1)
    return value


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
    """Extract modeled fields from frontmatter using profile mappings.

    Also promotes the semantic ``planned_week`` key when present
    (issue #128) so structured get/list output distinguishes Backlog,
    week-planned, and day-scheduled tasks. Unrelated user fields stay
    unexposed.
    """
    out: Dict[str, Any] = {}
    m = profile.mappings
    for logical in REQUIRED_MAPPINGS:
        key = m[logical]
        if key in frontmatter:
            out[logical] = frontmatter[key]
    if PLANNED_WEEK_KEY in frontmatter:
        out[PLANNED_WEEK_KEY] = frontmatter[PLANNED_WEEK_KEY]
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


def validate_planned_week(value: Any) -> str:
    """Validate a semantic week-planning value (issue #128).

    Must be a valid ``YYYY-MM-DD`` calendar date AND a Monday (the ISO
    week start). No silent rounding: any non-Monday date is rejected.
    """
    value = validate_date(value, "planned_week")
    if datetime.date.fromisoformat(value).weekday() != 0:
        raise ValidationError("planned_week must be a Monday (ISO week start)")
    return value


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
        if key == PLANNED_WEEK_KEY:
            # Reserved semantic key (issue #128): week planning must go
            # through the dedicated planned_week argument so transition
            # and invariant logic cannot be bypassed. Rejected even when
            # the value is None (clearing uses clear_planned_week).
            raise ValidationError(
                f"custom field {key!r} is reserved; use the dedicated "
                "planned_week argument"
            )
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
# Daily Notes primitives (issue #139, W1a: pure/internal only)
# ---------------------------------------------------------------------------
#
# Strict parsing/transformation primitives for Obsidian Daily Notes
# integration. These are pure or internal-only helpers: they never write
# task files, never invoke gbrain/PGLite, and are NOT wired into the
# engine lifecycle (no engine construction requirement, no lifecycle
# integration). Error messages are content-free.

# Obsidian core Daily Notes plugin config (relative to the vault root).
DAILY_NOTES_OBSIDIAN_DIR = ".obsidian"
DAILY_NOTES_CONFIG_NAME = "daily-notes.json"
DAILY_NOTES_DEFAULT_FORMAT = "YYYY-MM-DD"
DAILY_NOTES_MAX_FILE_SIZE = 1024 * 1024  # 1 MB config/template read bound

# Exact H2 heading that scopes the daily task list inside a daily note body.
DAILY_NOTE_TASKS_HEADING = "## Tasks"

# Top-level frontmatter keys normalized for daily note pages.
DAILY_NOTE_DATE_KEY = "date"
DAILY_NOTE_TITLE_KEY = "title"

# Supported date tokens (strict subset of the Obsidian/moment syntax) and
# the only literal characters allowed between tokens (safe numeric-path
# separators). Every maximal alphabetic run must be exactly one supported
# token; any other alphabetic syntax is rejected.
_DAILY_FORMAT_TOKENS: Tuple[str, ...] = ("YYYY", "YY", "MM", "M", "DD", "D")
_DAILY_FORMAT_ALPHA_RUN_RE = re.compile(r"[A-Za-z]+")
_DAILY_FORMAT_SAFE_LITERAL_RE = re.compile(r"[-._/]")
_DAILY_FORMAT_MAX_LEN = 64

# Bullet list item that is exactly one wikilink (optional indent, optional
# display text). Anything else in the section is preserved as prose.
_DAILY_BULLET_WIKILINK_RE = re.compile(
    r"^([ \t]*)[-*+][ \t]+\[\[([^\[\]]+)\]\][ \t]*$"
)

# Template expression (no nesting, no braces inside).
_DAILY_TEMPLATE_EXPR_RE = re.compile(r"\{\{([^{}]*)\}\}")


@dataclass(frozen=True)
class DailyNotesConfig:
    """Validated Daily Notes configuration (strict subset, lazy defaults).

    ``folder`` is the relative daily-notes folder (``""`` = vault root),
    ``format`` the note filename date format (default ``YYYY-MM-DD``), and
    ``template`` an optional canonical vault-relative Markdown physical
    path (``None`` = unset), normalized exactly once at load/validation.
    """

    folder: str = ""
    format: str = DAILY_NOTES_DEFAULT_FORMAT
    template: Optional[str] = None


def validate_daily_note_format(fmt: str) -> str:
    """Validate a Daily Notes date format string (strict token subset).

    Every maximal alphabetic run must be exactly one of ``YYYY``, ``YY``,
    ``MM``, ``M``, ``DD``, ``D``; literal characters between runs must be
    safe numeric-path separators (``-``, ``.``, ``_``, ``/``). Any other
    alphabetic or unsafe literal syntax is rejected. Returns the format
    unchanged.
    """
    if not isinstance(fmt, str) or not fmt:
        raise ValidationError("daily note format must be a non-empty string")
    if len(fmt) > _DAILY_FORMAT_MAX_LEN:
        raise ValidationError("daily note format exceeds length bound")
    has_token = False
    pos = 0
    for run_match in _DAILY_FORMAT_ALPHA_RUN_RE.finditer(fmt):
        for ch in fmt[pos:run_match.start()]:
            if _DAILY_FORMAT_SAFE_LITERAL_RE.match(ch) is None:
                raise ValidationError(
                    "daily note format literal is not a safe path separator"
                )
        if run_match.group(0) not in _DAILY_FORMAT_TOKENS:
            raise ValidationError("daily note format uses unsupported syntax")
        has_token = True
        pos = run_match.end()
    for ch in fmt[pos:]:
        if _DAILY_FORMAT_SAFE_LITERAL_RE.match(ch) is None:
            raise ValidationError(
                "daily note format literal is not a safe path separator"
            )
    if not has_token:
        raise ValidationError("daily note format must contain a date token")
    return fmt


def _format_daily_token(token: str, parsed: datetime.date) -> str:
    """Render one validated date token deterministically."""
    if token == "YYYY":
        return f"{parsed.year:04d}"
    if token == "YY":
        return f"{parsed.year % 100:02d}"
    if token == "MM":
        return f"{parsed.month:02d}"
    if token == "M":
        return str(parsed.month)
    if token == "DD":
        return f"{parsed.day:02d}"
    return str(parsed.day)  # "D"


def format_daily_note_date(date: str, fmt: str) -> str:
    """Deterministically format a ``YYYY-MM-DD`` date with a validated format.

    Token semantics: ``YYYY`` (4-digit year), ``YY`` (2-digit year),
    ``MM``/``M`` (zero-padded/plain month), ``DD``/``D`` (zero-padded/
    plain day). Literals must be safe numeric-path separators.
    """
    validate_date(date, "date")
    validate_daily_note_format(fmt)
    parsed = datetime.date.fromisoformat(date)
    out: List[str] = []
    pos = 0
    for run_match in _DAILY_FORMAT_ALPHA_RUN_RE.finditer(fmt):
        out.append(fmt[pos:run_match.start()])
        out.append(_format_daily_token(run_match.group(0), parsed))
        pos = run_match.end()
    out.append(fmt[pos:])
    return "".join(out)


def _validate_relative_note_path(relative: str) -> str:
    """Validate a vault-relative note path: safe segments only, no traversal."""
    if not isinstance(relative, str) or not relative:
        raise PathError("relative path must be a non-empty string")
    if relative.startswith("/"):
        raise PathError("relative path must not be absolute")
    if "\\" in relative:
        raise PathError("relative path must not contain backslash")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in relative):
        raise PathError("relative path must not contain control characters")
    for part in relative.split("/"):
        if part in ("", ".", ".."):
            raise PathError("relative path must not contain traversal segments")
    return relative


def _check_existing_dir_components_no_follow(vault: Path, relative_dir: str) -> None:
    """Reject existing symlink components in a vault-relative directory path.

    Missing components are tolerated (the Daily Notes plugin creates
    folders lazily). Once a component is missing, nothing deeper can
    exist, so the walk stops. Existing non-directory components are also
    rejected.
    """
    if not relative_dir:
        return
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    current_fd = _open_directory_no_follow(vault)
    try:
        for part in relative_dir.split("/"):
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return
                if exc.errno == errno.ELOOP:
                    raise PathError("path component is a symlink") from exc
                raise PathError("cannot verify path component") from exc
            os.close(current_fd)
            current_fd = next_fd
    finally:
        os.close(current_fd)


def _check_existing_regular_no_follow(path: Path) -> None:
    """If ``path`` exists, require a regular file (no symlink). Tolerate absence."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PathError("cannot inspect configured path") from exc
    if not stat_is_regular_mode(st.st_mode):
        raise PathError("configured path must be a regular file when present")


def _validate_daily_notes_folder(folder: str, vault: Path) -> str:
    """Validate the configured daily-notes folder path (may not exist yet)."""
    if not folder:
        return ""
    _validate_relative_note_path(folder)
    _check_existing_dir_components_no_follow(vault, folder)
    return folder


def _normalize_daily_template_reference(reference: str) -> str:
    """Append ``.md`` unless the complete reference already ends in ``.md``.

    The decision uses the complete configured reference string only (never
    ``Path.suffix``), so ``templates/daily-note`` and ``templates/daily.v2``
    both gain ``.md`` while ``templates/daily-note.md`` is unchanged.
    Callers must shape-validate the raw reference first so an appended
    suffix can never mask an unsafe raw segment.
    """
    if reference.endswith(".md"):
        return reference
    return f"{reference}.md"


def _validate_daily_notes_template(template: str, vault: Path) -> str:
    """Validate and normalize the configured daily-notes template reference.

    The raw reference is first validated for path shape (relative, no
    backslash/control characters, no traversal/empty segments) BEFORE
    normalization, so appending ``.md`` can never mask an unsafe raw
    segment (``.`` → ``..md``, ``..`` → ``...md``, ``templates/`` →
    ``templates/.md``); this is a pure shape check with no filesystem
    probing — the raw extensionless reference is never probed on disk.
    The reference is then normalized exactly once at load/validation into
    the canonical vault-relative Markdown physical path (issue #141):
    ``.md`` is appended unless the complete reference already ends in
    ``.md``. All remaining checks apply to the canonical target only.
    """
    _validate_relative_note_path(template)
    canonical = _normalize_daily_template_reference(template)
    _validate_relative_note_path(canonical)
    parts = canonical.split("/")
    dir_rel = "/".join(parts[:-1])
    _check_existing_dir_components_no_follow(vault, dir_rel)
    _check_existing_regular_no_follow(vault / canonical)
    return canonical


def load_daily_notes_config(vault: Path) -> DailyNotesConfig:
    """Strict reader for ``<vault>/.obsidian/daily-notes.json`` (fail closed).

    A validated active Obsidian core Daily Notes config is required: a
    missing file (or missing ``.obsidian``) raises ``CoreError`` rather
    than inferring defaults. Inside a present config, missing/empty/null
    ``folder``/``format`` values retain their defaults (``""`` and
    ``YYYY-MM-DD``) and ``template`` stays unset. When present, the file
    must be a JSON object; only ``folder``, ``format``, and ``template``
    are interpreted (unknown keys are ignored for Obsidian compatibility).
    Path-shaped values are strictly validated: relative, no backslash/
    control characters, no traversal or unsafe segments, and no symlink
    components where they exist. A non-empty ``template`` reference is
    normalized exactly once into the canonical vault-relative Markdown
    physical path (``.md`` appended unless the complete reference already
    ends in ``.md``); all template checks apply to the canonical target
    only, and the raw extensionless reference is never probed on disk.
    A missing canonical template file is tolerated at load (lazy allowance).
    Raises ``CoreError`` (``ValidationError``
    /``PathError`` subclasses) on missing, malformed, or unsafe config.
    """
    config_path = vault / DAILY_NOTES_OBSIDIAN_DIR / DAILY_NOTES_CONFIG_NAME
    if not target_exists_no_follow(config_path):
        raise CoreError("daily notes config is required but missing")
    try:
        obsidian_fd = _open_relative_directory_no_follow(
            vault, DAILY_NOTES_OBSIDIAN_DIR
        )
    except PathError:
        raise
    try:
        text = _read_directory_entry_no_follow(
            obsidian_fd,
            DAILY_NOTES_CONFIG_NAME,
            max_size=DAILY_NOTES_MAX_FILE_SIZE,
        )
    finally:
        os.close(obsidian_fd)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("daily notes config is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValidationError("daily notes config root must be an object")

    folder = data.get("folder")
    if folder is None:
        folder = ""
    if not isinstance(folder, str):
        raise ValidationError("daily notes folder must be a string")
    folder = _validate_daily_notes_folder(folder, vault)

    fmt = data.get("format")
    if fmt is None:
        fmt = ""
    if not isinstance(fmt, str):
        raise ValidationError("daily notes format must be a string")
    if fmt:
        validate_daily_note_format(fmt)
    else:
        fmt = DAILY_NOTES_DEFAULT_FORMAT

    template = data.get("template")
    if template is None:
        template = ""
    if not isinstance(template, str):
        raise ValidationError("daily notes template must be a string")
    if template:
        template = _validate_daily_notes_template(template, vault)
    else:
        template = None

    return DailyNotesConfig(folder=folder, format=fmt, template=template)


def read_daily_note_template(vault: Path, config: DailyNotesConfig) -> Optional[str]:
    """Read the configured daily-notes template no-follow and bounded.

    Returns ``None`` when no template is configured. ``config.template``
    is the canonical Markdown physical path (normalized at load), and no
    extensionless variant is ever probed. A configured template must exist
    as a regular, non-symlink, bounded file; anything else is a strict
    ``PathError``.
    """
    if not config.template:
        return None
    parts = config.template.split("/")
    dir_rel = "/".join(parts[:-1])
    name = parts[-1]
    if dir_rel:
        directory_fd = _open_relative_directory_no_follow(vault, dir_rel)
    else:
        directory_fd = _open_directory_no_follow(vault)
    try:
        return _read_directory_entry_no_follow(
            directory_fd, name, max_size=DAILY_NOTES_MAX_FILE_SIZE
        )
    finally:
        os.close(directory_fd)


def resolve_daily_note_path(vault: Path, config: DailyNotesConfig, date: str) -> Path:
    """Resolve ``<folder>/<formatted date>.md`` as a vault-confined path.

    Pure computation over an already-validated config: the date must be a
    valid ``YYYY-MM-DD``, the formatted filename must pass strict relative
    path validation, and the result is joined under ``vault`` (vault-
    confined by construction). No filesystem access.
    """
    validate_date(date, "date")
    formatted = format_daily_note_date(date, config.format)
    filename = f"{formatted}.md"
    if config.folder:
        relative = f"{config.folder}/{filename}"
    else:
        relative = filename
    _validate_relative_note_path(relative)
    return vault / relative


def render_daily_note_template(template: str, *, date: str, title: str) -> str:
    """Render a Daily Notes template body (no code execution).

    Supported expressions only: ``{{date}}``, ``{{title}}``, and
    ``{{date:FORMAT}}`` (``FORMAT`` validated by
    :func:`validate_daily_note_format`). Any other expression is rejected;
    nothing is ever evaluated.
    """
    if not isinstance(template, str):
        raise ValidationError("daily note template must be a string")
    if len(template) > MAX_BODY_LEN:
        raise ValidationError("daily note template exceeds length bound")
    validate_date(date, "date")
    if not isinstance(title, str) or len(title) > MAX_TITLE_LEN:
        raise ValidationError("daily note title must be a bounded string")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in title):
        raise ValidationError("daily note title must not contain control characters")

    def _replace(match: "re.Match[str]") -> str:
        expr = match.group(1).strip()
        if expr == "date":
            return date
        if expr == "title":
            return title
        if expr.startswith("date:"):
            return format_daily_note_date(date, expr[len("date:"):])
        raise ValidationError(
            "daily note template contains an unsupported expression"
        )

    return _DAILY_TEMPLATE_EXPR_RE.sub(_replace, template)


def build_default_daily_note_body(date: str) -> str:
    """Deterministic default body for a missing daily note.

    Includes the ``# <date>`` H1 and the exact ``## Tasks`` H2 that the
    structural transformer scopes to.
    """
    validate_date(date, "date")
    return f"# {date}\n\n{DAILY_NOTE_TASKS_HEADING}\n"


def normalize_daily_note_frontmatter(
    frontmatter: Mapping[str, Any], *, date: str, title_stem: str
) -> Dict[str, Any]:
    """Fill empty/null top-level daily-note frontmatter date/title.

    A top-level ``date`` value that is null or an empty/blank string
    becomes the scheduled ISO date; a top-level ``title`` value that is
    null or empty/blank becomes the filename stem. Non-empty values (any
    type) and absent keys are returned unchanged.
    """
    validate_date(date, "date")
    if not isinstance(frontmatter, Mapping):
        raise ValidationError("daily note frontmatter must be a mapping")
    if not isinstance(title_stem, str) or not title_stem:
        raise ValidationError("daily note title stem must be a non-empty string")
    out = dict(frontmatter)
    for key, fallback in (
        (DAILY_NOTE_DATE_KEY, date),
        (DAILY_NOTE_TITLE_KEY, title_stem),
    ):
        if key not in out:
            continue
        current = out[key]
        if current is None or (isinstance(current, str) and not current.strip()):
            out[key] = fallback
    return out


def _is_h2_heading(line: str) -> bool:
    """Return True for exactly-level-2 headings (``##``/``## text``), not H3+."""
    stripped = line.rstrip(" \t")
    return stripped == "##" or stripped.startswith("## ")


def _is_tasks_heading(line: str) -> bool:
    """Return True for the exact ``## Tasks`` H2 (trailing whitespace tolerated)."""
    return line.rstrip(" \t") == DAILY_NOTE_TASKS_HEADING


def find_tasks_section(body: str) -> Tuple[int, int]:
    """Locate the single ``## Tasks`` H2 section in a daily note body.

    Returns ``(start, end)`` character offsets: ``start`` is the offset of
    the heading line, ``end`` the offset of the next H2 heading line or
    ``len(body)``. Only exact-text level-2 headings match; H1/H3 lines do
    not end the section. Raises ``ValidationError`` unless the body
    contains exactly one ``## Tasks`` heading.
    """
    if not isinstance(body, str):
        raise ValidationError("daily note body must be a string")
    if len(body) > MAX_BODY_LEN:
        raise ValidationError("daily note body exceeds length bound")
    lines = body.split("\n")
    heading_offsets: List[int] = []
    line_offsets: List[int] = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        if _is_tasks_heading(line):
            heading_offsets.append(offset)
        offset += len(line) + 1
    if len(heading_offsets) != 1:
        raise ValidationError(
            "daily note body must contain exactly one '## Tasks' section"
        )
    start = heading_offsets[0]
    end = len(body)
    for line, off in zip(lines, line_offsets):
        if off > start and _is_h2_heading(line):
            end = off
            break
    return start, end


def _bullet_wikilink_matches_slug(line: str, slug: str) -> bool:
    """Return True if the line is a bullet whose wikilink target is exactly ``slug``."""
    matched = _DAILY_BULLET_WIKILINK_RE.match(line)
    if matched is None:
        return False
    target = matched.group(2).split("|", 1)[0]
    return target == slug


def _daily_link_line(slug: str, indent: str = "") -> str:
    """The exact canonical task line: a bullet-only bare wikilink."""
    return f"{indent}- [[{slug}]]"


def _section_bullet_wikilink_spans(
    body: str, start: int, end: int, slug: str
) -> List[Tuple[int, int]]:
    """Return ``(offset, length)`` content spans of exact-slug bullet lines."""
    spans: List[Tuple[int, int]] = []
    offset = start
    for line in body[start:end].split("\n"):
        if _bullet_wikilink_matches_slug(line, slug):
            spans.append((offset, len(line)))
        offset += len(line) + 1
    return spans


def _line_removal_span(body: str, line_start: int, line_len: int) -> Tuple[int, int]:
    """Extend a line content span to swallow exactly one adjacent newline."""
    line_end = line_start + line_len
    if line_end < len(body) and body[line_end] == "\n":
        return line_start, line_end + 1
    if line_start > 0 and body[line_start - 1] == "\n":
        return line_start - 1, line_end
    return line_start, line_end


def _merge_removal_spans(
    spans: List[Tuple[int, int]]
) -> List[Tuple[int, int]]:
    """Merge overlapping/adjacent removal spans (newline-swallow chains)."""
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _apply_edits(body: str, edits: List[Tuple[int, int, str]]) -> str:
    """Apply non-overlapping ``(start, end, replacement)`` edits in order."""
    ordered = sorted(edits, key=lambda edit: (edit[0], edit[1]))
    parts: List[str] = []
    cursor = 0
    for start, end, replacement in ordered:
        parts.append(body[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(body[cursor:])
    return "".join(parts)


def add_daily_note_task_link(body: str, *, slug: str) -> Tuple[str, bool]:
    """Add or normalize the task's bare bullet wikilink in ``## Tasks``.

    The canonical task line is exactly ``- [[<slug>]]``. Any bullet-only
    wikilink whose target is exactly ``slug`` — bare (``- [[slug]]``) or
    carrying a prior display alias (``- [[slug|alias]]``) — inside the
    exactly-one ``## Tasks`` section is owned: all such lines are
    deduped, the first occurrence is normalized in place to the bare
    canonical form (leading indentation preserved), and the duplicates
    are removed. When no exact-slug bullet exists, the bare canonical
    line is appended at the end of the section. Bytes outside the
    section — and similar-but-not-exact slugs or prose — are preserved
    verbatim. Returns ``(new_body, changed)``.
    """
    slug = validate_slug(slug)
    start, end = find_tasks_section(body)
    spans = _section_bullet_wikilink_spans(body, start, end, slug)
    if not spans:
        canonical = _daily_link_line(slug)
        prefix = ""
        if end == len(body) and body and not body.endswith("\n"):
            prefix = "\n"
        new_body = body[:end] + prefix + canonical + "\n" + body[end:]
        return new_body, True
    first_start, first_len = spans[0]
    matched = _DAILY_BULLET_WIKILINK_RE.match(body[first_start:first_start + first_len])
    assert matched is not None  # matched in _section_bullet_wikilink_spans
    canonical = _daily_link_line(slug, indent=matched.group(1))
    edits: List[Tuple[int, int, str]] = [
        (first_start, first_start + first_len, canonical)
    ]
    removal_spans = [
        _line_removal_span(body, span_start, span_len)
        for span_start, span_len in spans[1:]
    ]
    for rm_start, rm_end in _merge_removal_spans(removal_spans):
        edits.append((rm_start, rm_end, ""))
    new_body = _apply_edits(body, edits)
    return new_body, new_body != body


def remove_daily_note_task_link(body: str, *, slug: str) -> Tuple[str, bool]:
    """Remove every exact-slug bullet wikilink from ``## Tasks``.

    Every bullet-only line in the section whose wikilink targets exactly
    ``slug`` — bare (``- [[slug]]``) or carrying a display alias
    (``- [[slug|alias]]``) — is removed together with one adjacent
    newline. Similar slugs, prose mentions, and bytes outside the
    section are preserved verbatim. Returns ``(new_body, changed)``;
    unchanged bodies return ``(body, False)``.
    """
    slug = validate_slug(slug)
    start, end = find_tasks_section(body)
    spans = _section_bullet_wikilink_spans(body, start, end, slug)
    if not spans:
        return body, False
    removal_spans = [
        _line_removal_span(body, span_start, span_len)
        for span_start, span_len in spans
    ]
    new_body = _apply_edits(
        body, [(s, e, "") for s, e in _merge_removal_spans(removal_spans)]
    )
    return new_body, True


# ---------------------------------------------------------------------------
# Daily Notes projection preparation/persistence (issue #139, W1b: internal)
# ---------------------------------------------------------------------------
#
# Internal primitives that turn a planned task-link ensure/remove into
# pre-computed Daily Note bytes and apply them with a no-follow optimistic
# atomic writer. Built strictly on top of the W1a primitives (validated
# DailyNotesConfig, template renderer, ``## Tasks`` section transformer).
# These helpers are NOT wired into the engine lifecycle, never write task
# files, never invoke gbrain/PGLite, and are NOT a generic writer/tool:
# every API only accepts a validated DailyNotesConfig plus a resolved
# Daily Note target/operation. Error messages are content-free (no note
# contents, no absolute/private target paths, no temp names).

# Projection operations.
DAILY_PROJECTION_OP_ENSURE = "ensure"
DAILY_PROJECTION_OP_REMOVE = "remove"

# Projection kinds (what the writer must do with the target note).
DAILY_NOTE_PROJECTION_CREATE = "create"    # note missing at prepare: create it
DAILY_NOTE_PROJECTION_REPLACE = "replace"  # note exists: atomic replacement
DAILY_NOTE_PROJECTION_NONE = "none"        # nothing to write (idempotent)

# Projection outcome states.
DAILY_PROJECTION_APPLIED = "applied"
DAILY_PROJECTION_NOT_APPLIED = "not_applied"
DAILY_PROJECTION_CONFLICT = "projection_conflict"

# Modes for newly created daily-note folders/files (umask applies on create).
DAILY_NOTE_CREATE_DIR_MODE = 0o755
DAILY_NOTE_CREATE_FILE_MODE = 0o644

# Hard bounds.
MAX_DAILY_PROJECTION_TARGETS = 16
DAILY_PROJECTION_MAX_ATTEMPTS = 2  # exactly two total attempts per projection

# Generic content-free projection commit message.
DAILY_PROJECTION_COMMIT_MSG = "tasknotes-mcp: daily note projection"


@dataclass(frozen=True)
class _DailySourceFingerprint:
    """Identity of the exact Daily Note source a transformation was based on.

    Combines stable identity/ freshness metadata (device, inode, size,
    ``mtime_ns``) with a SHA-256 over the bytes actually read so the
    writer can require "same source" immediately before replacement.
    """

    dev: int
    ino: int
    size: int
    mtime_ns: int
    mode: int
    sha256: str


@dataclass(frozen=True)
class DailyNoteProjection:
    """A prepared Daily Note projection ready for atomic application.

    Produced by :func:`prepare_daily_note_projection` before any task
    side effect. ``kind`` selects the writer behavior (create / replace /
    none); ``content`` holds the transformed bytes (``None`` for the
    ``none`` kind); ``fingerprint`` is the source identity the
    transformation was computed against (``None`` when the note was
    missing). Consumed only by :func:`apply_daily_note_projection`.
    """

    operation: str
    date: str
    slug: str
    target_relative: str
    kind: str
    content: Optional[bytes]
    fingerprint: Optional[_DailySourceFingerprint]


@dataclass(frozen=True)
class DailyNoteProjectionOutcome:
    """Structured outcome of an applied Daily Note projection.

    ``state`` is one of ``applied``, ``not_applied`` (idempotent no-op),
    or ``projection_conflict`` (persistent race; nothing was written).
    ``attempts`` reports the write attempts consumed (1 or 2).
    """

    state: str
    attempts: int = 1
    created: bool = False
    changed: bool = False
    detail: Optional[str] = None


def _read_fd_bytes_bounded(fd: int, max_size: int) -> bytes:
    """Read at most ``max_size + 1`` bytes from ``fd``; close the fd.

    Raises ``CoreError`` if the source exceeds ``max_size``.
    """
    data = b""
    try:
        while len(data) <= max_size:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        if len(data) > max_size:
            raise CoreError("daily note exceeds size bound")
    finally:
        os.close(fd)
    return data


def _read_daily_note_source(
    vault: Path, relative: str
) -> Optional[Tuple[str, _DailySourceFingerprint]]:
    """Read a Daily Note no-follow and bounded; return text + fingerprint.

    Every path component is opened with ``O_NOFOLLOW`` (symlinked
    components raise ``PathError``); the note must be a regular file
    within the size bound. Returns ``None`` when the note — or any
    folder along the configured path — is absent.
    """
    parts = relative.split("/")
    dir_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        dir_flags |= os.O_DIRECTORY
    parent_fd = _open_directory_no_follow(vault)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, dir_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PathError(
                        "daily note folder component is a symlink"
                    ) from exc
                raise PathError("cannot open daily note folder component") from exc
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PathError("daily note is a symlink") from exc
            raise PathError("cannot open daily note") from exc
        try:
            st = os.fstat(fd)
        except OSError as exc:
            os.close(fd)
            raise PathError("cannot inspect daily note") from exc
        if not stat_is_regular_mode(st.st_mode):
            os.close(fd)
            raise PathError("daily note is not a regular file")
        try:
            data = _read_fd_bytes_bounded(fd, DAILY_NOTES_MAX_FILE_SIZE)
        except CoreError:
            raise
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoreError("daily note is not valid UTF-8") from exc
        fingerprint = _DailySourceFingerprint(
            dev=st.st_dev,
            ino=st.st_ino,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            mode=st.st_mode & 0o7777,
            sha256=hashlib.sha256(data).hexdigest(),
        )
        return text, fingerprint
    finally:
        os.close(parent_fd)


def _transform_daily_note_text(
    text: str,
    *,
    operation: str,
    slug: str,
    date: str,
    stem: str,
    normalize_frontmatter: bool = False,
) -> Tuple[str, bool]:
    """Apply the section transformer (and, only on creation, normalization).

    The ``## Tasks`` requirement is enforced by the W1a transformer
    (exactly one section). Existing-note frontmatter is never normalized
    or reserialized: every byte outside the ``## Tasks`` section is
    preserved verbatim unless ``normalize_frontmatter`` is explicitly
    requested — which happens only for missing-note/template creation,
    where null/empty top-level ``date``/``title`` values are filled
    before the note first exists. Returns ``(new_text, changed)``.
    """
    fm, raw_body = _parse_frontmatter(text)
    body_offset = len(text) - len(raw_body)
    if operation == DAILY_PROJECTION_OP_ENSURE:
        new_body, body_changed = add_daily_note_task_link(raw_body, slug=slug)
    else:
        new_body, body_changed = remove_daily_note_task_link(raw_body, slug=slug)
    if not normalize_frontmatter:
        return text[:body_offset] + new_body, body_changed
    normalized = normalize_daily_note_frontmatter(fm, date=date, title_stem=stem)
    fm_changed = normalized != fm
    if fm_changed:
        new_text = _serialize_frontmatter(normalized) + new_body
    else:
        new_text = text[:body_offset] + new_body
    return new_text, body_changed or fm_changed


def _validate_daily_note_content(text: str) -> bytes:
    """Bound-check and encode transformed note content before any write."""
    encoded = text.encode("utf-8")
    if len(encoded) > DAILY_NOTES_MAX_FILE_SIZE:
        raise ValidationError("daily note content exceeds size bound")
    return encoded


def prepare_daily_note_projection(
    vault: Path,
    config: DailyNotesConfig,
    operation: str,
    date: str,
    *,
    slug: str,
) -> DailyNoteProjection:
    """Pre-read the target and compute the transformed bytes (no side effects).

    For a planned ensure/remove, reads the existing target no-follow and
    bounded, requires exactly one ``## Tasks`` section, and applies the
    W1a section transformer. Existing notes are never reserialized:
    bytes outside the ``## Tasks`` section (including all frontmatter)
    are preserved verbatim. Missing notes: ensure prepares a create from
    the valid configured template (rendered, with null/empty top-level
    ``date``/``title`` normalized) or the deterministic default body —
    date/title normalization applies only during this creation; remove
    is an idempotent no-op (no note is created just to remove it).
    Raises typed core errors on invalid input, unreadable/symlinked/
    oversized sources, or a missing/duplicated ``## Tasks`` section.
    """
    if not isinstance(config, DailyNotesConfig):
        raise ValidationError(
            "daily note projection requires a validated DailyNotesConfig"
        )
    if operation not in (DAILY_PROJECTION_OP_ENSURE, DAILY_PROJECTION_OP_REMOVE):
        raise ValidationError(
            "daily note operation must be 'ensure' or 'remove'"
        )
    validate_slug(slug)
    target = resolve_daily_note_path(vault, config, date)
    relative = target.relative_to(vault).as_posix()
    stem = target.stem
    source = _read_daily_note_source(vault, relative)
    if source is None:
        if operation == DAILY_PROJECTION_OP_REMOVE:
            return DailyNoteProjection(
                operation=operation,
                date=date,
                slug=slug,
                target_relative=relative,
                kind=DAILY_NOTE_PROJECTION_NONE,
                content=None,
                fingerprint=None,
            )
        template_text = read_daily_note_template(vault, config)
        if template_text is None:
            base_text = build_default_daily_note_body(date)
        else:
            base_text = render_daily_note_template(
                template_text, date=date, title=stem
            )
        new_text, _changed = _transform_daily_note_text(
            base_text,
            operation=operation,
            slug=slug,
            date=date,
            stem=stem,
            # Date/title normalization applies only during missing-note
            # creation (template rendering); existing notes are never
            # normalized or reserialized.
            normalize_frontmatter=True,
        )
        return DailyNoteProjection(
            operation=operation,
            date=date,
            slug=slug,
            target_relative=relative,
            kind=DAILY_NOTE_PROJECTION_CREATE,
            content=_validate_daily_note_content(new_text),
            fingerprint=None,
        )
    text, fingerprint = source
    new_text, changed = _transform_daily_note_text(
        text,
        operation=operation,
        slug=slug,
        date=date,
        stem=stem,
    )
    if not changed:
        return DailyNoteProjection(
            operation=operation,
            date=date,
            slug=slug,
            target_relative=relative,
            kind=DAILY_NOTE_PROJECTION_NONE,
            content=None,
            fingerprint=fingerprint,
        )
    return DailyNoteProjection(
        operation=operation,
        date=date,
        slug=slug,
        target_relative=relative,
        kind=DAILY_NOTE_PROJECTION_REPLACE,
        content=_validate_daily_note_content(new_text),
        fingerprint=fingerprint,
    )


def _open_daily_parent_dir(
    vault: Path, dir_rel: str, *, create: bool
) -> Optional[int]:
    """Open the note's parent directory no-follow; optionally mkdir missing parts.

    Missing components are created only when ``create`` is true, along
    the already-validated configured path, and each created (or racing)
    component is immediately reopened with ``O_NOFOLLOW|O_DIRECTORY`` so
    a symlink swap is rejected. Returns ``None`` when a component is
    missing and creation was not requested.
    """
    if not dir_rel:
        return _open_directory_no_follow(vault)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    current_fd = _open_directory_no_follow(vault)
    try:
        for part in dir_rel.split("/"):
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    os.close(current_fd)
                    return None
                try:
                    os.mkdir(part, DAILY_NOTE_CREATE_DIR_MODE, dir_fd=current_fd)
                except FileExistsError:
                    pass  # racing creator; re-checked by the open below
                except OSError as exc:
                    raise PathError("cannot create daily note folder") from exc
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise PathError(
                            "daily note folder component is a symlink"
                        ) from exc
                    raise PathError(
                        "cannot open daily note folder component"
                    ) from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PathError(
                        "daily note folder component is a symlink"
                    ) from exc
                raise PathError("cannot open daily note folder component") from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _daily_entry_exists(parent_fd: int, name: str) -> bool:
    """No-follow existence check for one directory entry."""
    try:
        os.lstat(name, dir_fd=parent_fd)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PathError("cannot inspect daily note path") from exc


def _daily_fingerprint_matches(
    parent_fd: int, name: str, fingerprint: _DailySourceFingerprint
) -> bool:
    """Reopen/restat/rehash the source; require the identical fingerprint."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathError("daily note is a symlink") from exc
        raise PathError("cannot open daily note") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise PathError("cannot inspect daily note") from exc
    if (
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mtime_ns,
    ) != (
        fingerprint.dev,
        fingerprint.ino,
        fingerprint.size,
        fingerprint.mtime_ns,
    ):
        os.close(fd)
        return False
    try:
        data = _read_fd_bytes_bounded(fd, DAILY_NOTES_MAX_FILE_SIZE)
    except (CoreError, OSError):
        # Unreadable or overgrown source counts as a mismatch (race);
        # the recompute path re-reads strictly.
        return False
    return hashlib.sha256(data).hexdigest() == fingerprint.sha256


def _create_daily_temp_file(parent_fd: int, target_name: str) -> Tuple[str, int]:
    """Create a sibling temp file with O_EXCL|O_NOFOLLOW; return (name, fd)."""
    for _ in range(8):
        candidate = f".{target_name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
        try:
            fd = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PathError("daily note temp path is a symlink") from exc
            raise CoreError("cannot create daily note temp file") from exc
        return candidate, fd
    raise CoreError("cannot create daily note temp file")


def _apply_daily_projection_attempt(
    vault: Path,
    projection: DailyNoteProjection,
    *,
    hook: Optional[Callable[[], None]],
) -> Optional[str]:
    """Apply one projection attempt; return the outcome state or None on race.

    Creates/opens the parent directory no-follow, writes a sibling temp
    file (O_EXCL|O_NOFOLLOW), fsyncs the content, runs the final-check
    test seam, re-verifies the source fingerprint (or continued absence),
    publishes, fsyncs the parent when supported, and always cleans the
    temp file on failure. Never overwrites a source that changed since
    prepare. Publication is atomic and kind-aware: a replace projection
    publishes with ``os.replace`` after fingerprint re-verification; a
    create projection publishes with an atomic no-clobber ``os.link`` so
    a target materialized at the actual publication boundary (EEXIST) is
    never overwritten and becomes a retryable race instead. OSError from
    the write/publish syscalls is mapped to a typed content-free
    ``CoreError`` so it degrades into the projection write-failure
    result instead of escaping after the task has been committed.
    """
    parts = projection.target_relative.split("/")
    name = parts[-1]
    dir_rel = "/".join(parts[:-1])
    kind = projection.kind
    if kind == DAILY_NOTE_PROJECTION_NONE:
        parent_fd = _open_daily_parent_dir(vault, dir_rel, create=False)
        try:
            if parent_fd is None:
                # The note (and its folder) cannot exist now.
                if projection.fingerprint is None:
                    return DAILY_PROJECTION_NOT_APPLIED
                return None  # source vanished since prepare: race
            if hook is not None:
                hook()
            if projection.fingerprint is None:
                if _daily_entry_exists(parent_fd, name):
                    return None  # note appeared since prepare: race
                return DAILY_PROJECTION_NOT_APPLIED
            if not _daily_fingerprint_matches(
                parent_fd, name, projection.fingerprint
            ):
                return None  # changed since prepare: race
            return DAILY_PROJECTION_NOT_APPLIED
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
    if kind not in (DAILY_NOTE_PROJECTION_CREATE, DAILY_NOTE_PROJECTION_REPLACE):
        raise ValidationError("invalid daily note projection kind")
    if not isinstance(projection.content, (bytes, bytearray)):
        raise ValidationError("daily note projection content must be bytes")
    fingerprint = projection.fingerprint
    if kind == DAILY_NOTE_PROJECTION_REPLACE and fingerprint is None:
        raise ValidationError("replace projection requires a source fingerprint")
    content = bytes(projection.content)
    parent_fd = _open_daily_parent_dir(
        vault, dir_rel, create=(kind == DAILY_NOTE_PROJECTION_CREATE)
    )
    if parent_fd is None:
        return None  # parent vanished since prepare: race
    mode = (
        fingerprint.mode
        if fingerprint is not None
        else DAILY_NOTE_CREATE_FILE_MODE
    )
    temp_name: Optional[str] = None
    published = False
    try:
        temp_name, temp_fd = _create_daily_temp_file(parent_fd, name)
        try:
            try:
                view = memoryview(content)
                while view:
                    written = os.write(temp_fd, view)
                    view = view[written:]
                os.fsync(temp_fd)
                os.fchmod(temp_fd, mode)
            except OSError as exc:
                # Write-stage failures must never escape after the task
                # has been committed: map them into the typed projection
                # write-failure path (degraded daily result, temp cleaned
                # by the finally below, never a recovery marker).
                raise CoreError(
                    "daily note projection write failed"
                ) from exc
        finally:
            os.close(temp_fd)
        # Final-check test seam: runs immediately before the source
        # re-verification so tests can inject concurrent modifications.
        if hook is not None:
            hook()
        if kind == DAILY_NOTE_PROJECTION_CREATE:
            if _daily_entry_exists(parent_fd, name):
                return None  # note appeared since prepare: race
        else:
            if fingerprint is None or not _daily_fingerprint_matches(
                parent_fd, name, fingerprint
            ):
                return None  # source changed since prepare: race
        if kind == DAILY_NOTE_PROJECTION_CREATE:
            # Atomic no-clobber publication: the absence check above is
            # only advisory, so the target is created by hard-linking the
            # fsynced sibling temp file. If a competing creator
            # materializes the target at the actual publication boundary,
            # ``os.link`` fails with EEXIST, the racing bytes are never
            # overwritten, and the caller takes the bounded
            # re-read/recompute/retry path.
            try:
                os.link(
                    temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
                )
            except FileExistsError:
                return None  # created at the publication boundary: race
            except OSError as exc:
                raise CoreError(
                    "daily note projection write failed"
                ) from exc
            # The temp name still exists (second hard link to the same
            # inode); ``published`` stays False so the finally below
            # removes it while the target keeps the published content.
        else:
            try:
                os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except OSError as exc:
                # Publish-stage failures map to the same typed write-failure
                # path; the temp file is cleaned up below and the target is
                # never partially written.
                raise CoreError(
                    "daily note projection write failed"
                ) from exc
            published = True  # os.replace consumed the temp name
        try:
            os.fsync(parent_fd)
        except OSError:
            pass  # parent fsync is best-effort (not supported everywhere)
        return DAILY_PROJECTION_APPLIED
    finally:
        if temp_name is not None and not published:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def apply_daily_note_projection(
    vault: Path,
    config: DailyNotesConfig,
    projection: DailyNoteProjection,
    *,
    _final_check_hook: Optional[Callable[[], None]] = None,
) -> DailyNoteProjectionOutcome:
    """Optimistically apply a prepared projection (exactly two attempts).

    The first attempt applies the prepared bytes; on a detected race the
    transformation is recomputed (fresh strict read + transform) and the
    second — final — attempt is made. A persistent race returns the
    typed generic ``projection_conflict`` outcome and never overwrites
    the racing content. Creation is no-clobber at the actual publication
    boundary: a target created concurrently there is never overwritten
    (the EEXIST becomes a retryable race). OSError from the
    write/publish syscalls is mapped to a typed content-free ``CoreError``
    (write-failure path). ``_final_check_hook`` is an internal test seam
    invoked immediately before each final check.
    """
    if not isinstance(config, DailyNotesConfig):
        raise ValidationError(
            "daily note projection requires a validated DailyNotesConfig"
        )
    if not isinstance(projection, DailyNoteProjection):
        raise ValidationError("daily note projection requires a prepared projection")
    _validate_relative_note_path(projection.target_relative)
    attempts = 0
    current = projection
    while attempts < DAILY_PROJECTION_MAX_ATTEMPTS:
        attempts += 1
        state = _apply_daily_projection_attempt(
            vault, current, hook=_final_check_hook
        )
        if state is not None:
            return DailyNoteProjectionOutcome(
                state=state,
                attempts=attempts,
                created=(
                    state == DAILY_PROJECTION_APPLIED
                    and current.kind == DAILY_NOTE_PROJECTION_CREATE
                ),
                changed=(
                    state == DAILY_PROJECTION_APPLIED
                    and current.kind == DAILY_NOTE_PROJECTION_REPLACE
                ),
            )
        if attempts < DAILY_PROJECTION_MAX_ATTEMPTS:
            current = prepare_daily_note_projection(
                vault,
                config,
                current.operation,
                current.date,
                slug=current.slug,
            )
    return DailyNoteProjectionOutcome(
        state=DAILY_PROJECTION_CONFLICT,
        attempts=attempts,
        detail="daily note changed during projection; not applied",
    )


def git_commit_daily_projection_targets(
    vault: Path,
    targets: List[Path],
    git_env: Optional[Dict[str, str]] = None,
) -> bool:
    """Stage only the provided Daily Note paths and commit iff staged.

    Bounded explicit multi-target companion to the task Git helpers: it
    stages exactly the given (already vault-confined) changed Daily Note
    paths — never ``git add -A`` — and creates one generic content-free
    projection commit. Targets are lexically validated to stay inside
    the vault (relative, no traversal, no backslash). Hooks, signing,
    gc, and maintenance are disabled command-locally. Never runs
    checkout/reset/clean/merge/pull/push. Returns True if a commit was
    created, False if nothing was staged.
    """
    if git_env is None:
        git_env = _build_git_env()
    if len(targets) > MAX_DAILY_PROJECTION_TARGETS:
        raise ValidationError("daily note projection targets exceed count bound")
    rels: List[str] = []
    seen: set = set()
    for target in targets:
        path = Path(target)
        if path.is_absolute():
            try:
                rel = path.relative_to(vault)
            except ValueError as exc:
                raise PathError("daily note projection target is not in the vault") from exc
        else:
            rel = path
        rel_str = rel.as_posix()
        _validate_relative_note_path(rel_str)
        if rel_str in seen:
            continue
        seen.add(rel_str)
        rels.append(rel_str)
    if not rels:
        return False
    r = _run_git(vault, git_env, ["add", "--"] + rels)
    if r.returncode != 0:
        raise GitError(f"git add daily projection targets failed: {_redact(r.stderr)[:200]}")
    r = _run_git(vault, git_env, ["diff", "--cached", "--quiet"])
    if r.returncode == 0:
        return False
    r = _run_git(vault, git_env, ["commit", "-m", DAILY_PROJECTION_COMMIT_MSG, "--"] + rels)
    if r.returncode != 0:
        raise GitError(f"git daily projection commit failed: {_redact(r.stderr)[:200]}")
    return True


# ---------------------------------------------------------------------------
# Daily Notes link integration (issue #139, W2: engine lifecycle)
# ---------------------------------------------------------------------------
#
# Scheduled-driven Daily Notes projection for create/update/delete. The
# final ACTUAL scheduling state (never caller intent) drives the link
# transitions; plans are composed by resolved target path (a transition
# collapsing onto one daily target emits exactly one ensure); resolved
# targets inside the task/archive folders are rejected before any task
# side effect. The Daily Notes config is loaded and validated at most
# once per operation, before the task side effects, and the same
# immutable snapshot is carried through the post-task apply/commit/sync
# (no engine-lifetime caching). The W1b prepare/apply primitives and the
# multi-target Git helper do all the work. Only ``applied_and_committed``
# task outcomes are projected; projection-only failures degrade the
# optional daily result fields and never change the authoritative task
# state, never write task files, and never use the recovery marker.

# Daily link projection result states (content-free).
DAILY_LINK_APPLIED = "applied_and_committed"
DAILY_LINK_SYNC_FAILED = "committed_sync_failed"
DAILY_LINK_CONFLICT = "conflict"
DAILY_LINK_WRITE_FAILED = "write_failed"
DAILY_LINK_COMMIT_FAILED = "commit_failed"
DAILY_LINK_NOT_APPLICABLE = "not_applicable"
DAILY_LINK_NOT_APPLIED = "not_applied"

# Generic content-free details for degraded daily outcomes.
_DAILY_LINK_FAILURE_DETAIL = {
    DAILY_LINK_CONFLICT: "daily note projection conflict",
    DAILY_LINK_WRITE_FAILED: "daily note projection write failed",
    DAILY_LINK_COMMIT_FAILED: "daily note projection commit failed",
    DAILY_LINK_SYNC_FAILED: "daily note projection committed but sync failed",
}


def _daily_scheduled_date(value: Any) -> Optional[str]:
    """Extract a plain ``YYYY-MM-DD`` scheduled value; None when unusable.

    Collapses the gbrain-normalized bare-date form back to ``YYYY-MM-DD``
    and validates. Non-strings and invalid dates yield ``None`` (only a
    valid scheduled date drives a daily link; scheduled is the sole
    source).
    """
    if not isinstance(value, str):
        return None
    value = _denormalize_bare_date(value)
    try:
        return validate_date(value, "scheduled")
    except ValidationError:
        return None


def _daily_link_plan(
    old: Optional[str], new: Optional[str]
) -> Optional[List[Tuple[str, str]]]:
    """Compute ensure/remove steps from the actual scheduling transition.

    ``old``/``new`` are plain ``YYYY-MM-DD`` scheduled dates or None
    (backlog/week planning). Order matters: the ensure of the new date
    always precedes the removal of the old date so a partial failure
    never loses the link.
    """
    if new is not None:
        steps: List[Tuple[str, str]] = [(DAILY_PROJECTION_OP_ENSURE, new)]
        if old is not None and old != new:
            steps.append((DAILY_PROJECTION_OP_REMOVE, old))
        return steps
    if old is not None:
        return [(DAILY_PROJECTION_OP_REMOVE, old)]
    return None


def _compose_daily_link_plan_by_target(
    vault: Path,
    config: DailyNotesConfig,
    steps: List[Tuple[str, str]],
) -> List[Tuple[str, str]]:
    """Compose planned steps by resolved target path, not merely date.

    ``steps`` are date-level ensure/remove steps in plan order (ensure
    before remove). Steps whose dates resolve to the same Daily Note
    target (e.g. a ``YYYY-MM`` format where D1 and D2 share one monthly
    note) collapse to the first step: a D1 -> D2 transition resolving to
    a single target emits exactly one ensure and never an ensure
    followed by a remove of the same link.
    """
    composed: List[Tuple[str, str]] = []
    seen: set = set()
    for operation, date in steps:
        target = resolve_daily_note_path(vault, config, date)
        relative = target.relative_to(vault).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        composed.append((operation, date))
    return composed


def _reject_daily_projection_collision(
    profile: TaskNotesProfile, target_relative: str
) -> None:
    """Reject a projection target inside task/archive Markdown folders.

    Task Markdown is gbrain-only; the Daily Notes direct writer must
    never mutate TaskNotes-managed files. Any resolved daily-note target
    inside the configured ``tasksFolder`` — or inside the active archive
    folder (relevant only when ``moveArchivedTasks`` is true and an
    archive folder is configured) — is rejected deterministically
    before any side effect.
    """
    protected = [profile.tasks_folder]
    if profile.move_archived_tasks and profile.archive_folder:
        protected.append(profile.archive_folder)
    for folder in protected:
        if target_relative == folder or target_relative.startswith(folder + "/"):
            raise ValidationError(
                "daily note projection target collides with the task or "
                "archive folder"
            )


# ---------------------------------------------------------------------------
# Daily-links reconciliation foundations (issue #139, W2a: read-only)
# ---------------------------------------------------------------------------
#
# Private, read-only foundations for the future Daily Notes link
# backfill/reconciliation (prepare/finalize lands in later phases on
# these stable names). Nothing here advances the cursor, writes task
# files, writes projections, invokes gbrain/PGLite, uses the public
# gbrain wrapper, or touches the recovery marker.
#
# State lives in two fixed runtime files under ``/opt/data/.gbrain``
# (never under the vault, never in the gbrain DB): the reconcile cursor
# and its fixed pending sibling. Both hold only structural metadata —
# schema id/version, a reconciled HEAD SHA, the prior Daily Notes
# folder/format, and the projection format version — never titles, note
# bodies, or any content. A missing cursor is the bootstrap signal; any
# present-but-invalid document (malformed, oversized, schema- or
# SHA-invalid) fails closed. File primitives are bounded, no-follow,
# atomic (temp + fsync + replace), restrictive-mode, and content-free
# on error. Git task objects are read via fixed-argv, no-shell,
# streamed ``git show`` with a kill/reap hard cap aligned to
# ``LIST_MAX_FILE_SIZE`` (not ``MAX_OUTPUT``); there is no custom Git
# object/pack parser.

# Fixed runtime paths (never vault-relative, never gbrain DB).
DAILY_LINKS_RECONCILE_CURSOR_PATH = Path(
    "/opt/data/.gbrain/josemar-tasknotes-daily-links-reconcile.json"
)
DAILY_LINKS_RECONCILE_PENDING_PATH = Path(
    "/opt/data/.gbrain/josemar-tasknotes-daily-links-reconcile-pending.json"
)

# Cursor/pending schema identity. A version bump is a deliberate
# migration event; readers reject every other version (fail closed).
# The pending document gained its applied-routing pin while the format
# was still unwired (no released writer exists), so the shared version
# remains 1.
DAILY_LINKS_RECONCILE_SCHEMA_ID = "josemar-tasknotes-daily-links-reconcile"
DAILY_LINKS_RECONCILE_CURSOR_VERSION = 1

# The projection link format version recorded by the cursor. A mismatch
# means the cursor was produced for a different link format and later
# reconciliation must not assume compatibility (the loader rejects it).
DAILY_LINKS_PROJECTION_FORMAT_VERSION = 1

# Cursor/pending documents are tiny structural JSON; this bound is a
# corruption guard, not a storage budget.
RECONCILE_CURSOR_MAX_FILE_SIZE = 64 * 1024
RECONCILE_CURSOR_FILE_MODE = 0o600

# Candidate classification constants (pure, deterministic).
RECONCILE_CLASS_CANDIDATE = "candidate"
RECONCILE_CLASS_NON_TASK = "non_task"
RECONCILE_CLASS_MALFORMED = "malformed"

# Candidate locations inside the snapshot.
RECONCILE_LOCATION_TASKS = "tasks"
RECONCILE_LOCATION_ARCHIVE = "archive"

# Git object read states. ``missing`` is the typed no-before-state
# result: the task path simply does not exist at the requested commit.
GIT_OBJECT_PRESENT = "present"
GIT_OBJECT_MISSING = "missing"

# Exact key sets of the two reconcile documents (fail closed on both
# missing and extra keys; the format is private and versioned).
_RECONCILE_CURSOR_JSON_KEYS = frozenset(
    {"schema", "version", "reconciled_head", "daily_folder", "daily_format",
     "projection_format"}
)
_RECONCILE_PENDING_JSON_KEYS = frozenset(
    {"schema", "version", "from_head", "to_head", "started_at",
     "daily_folder", "daily_format"}
)

_GIT_SHA_HEX = frozenset("abcdef") | frozenset(str(value) for value in range(10))


@dataclass(frozen=True)
class DailyLinksReconcileCursor:
    """Strict reconcile cursor (structural metadata only, never content).

    ``reconciled_head`` is the full vault HEAD SHA through which all
    Daily Notes links are considered reconciled. ``daily_folder``/``daily_format``
    record the prior Daily Notes configuration so later phases can resolve
    old targets after config changes. ``projection_format`` is the link
    format version the reconciliation was performed with.
    """

    reconciled_head: str
    daily_folder: str
    daily_format: str
    projection_format: int
    version: int = DAILY_LINKS_RECONCILE_CURSOR_VERSION


@dataclass(frozen=True)
class DailyLinksReconcilePending:
    """Strict in-flight reconciliation marker (fixed pending sibling).

    ``daily_folder``/``daily_format`` structurally pin the applied Daily
    Notes routing snapshot: the exact configuration representation the
    applied cycle used, consumed by finalize identity verification and
    by old-routing lookup when a later prepare replays an
    applied-but-unfinalized cycle. Non-sensitive commit/routing
    metadata only.
    """

    from_head: str
    to_head: str
    started_at: int
    daily_folder: str
    daily_format: str
    version: int = DAILY_LINKS_RECONCILE_CURSOR_VERSION


def is_valid_git_commit_sha(value: Any) -> bool:
    """True for a full lowercase hexadecimal Git object id (SHA-1/SHA-256)."""
    if not isinstance(value, str) or len(value) not in (40, 64):
        return False
    return all(ch in _GIT_SHA_HEX for ch in value)


def validate_git_commit_sha(value: Any) -> str:
    """Require a full lowercase hexadecimal Git object id (fail closed)."""
    if not is_valid_git_commit_sha(value):
        raise ValidationError(
            "value must be a full lowercase hexadecimal Git object id"
        )
    return value


def validate_git_task_object_path(value: Any) -> str:
    """Validate a vault-relative task Markdown path for ``<sha>:<path>`` revs."""
    _validate_relative_note_path(value)
    if not value.endswith(".md"):
        raise PathError("git object path must be a Markdown (.md) file")
    return value


def _require_reconcile_document_header(
    data: Any, keys: frozenset
) -> None:
    """Strict common document checks: object root, exact keys, schema, version."""
    if not isinstance(data, Mapping):
        raise ValidationError("reconcile state document root must be an object")
    if set(data.keys()) != keys:
        raise ValidationError("reconcile state document schema is invalid")
    if data["schema"] != DAILY_LINKS_RECONCILE_SCHEMA_ID:
        raise ValidationError("reconcile state document schema id mismatch")
    version = data["version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != DAILY_LINKS_RECONCILE_CURSOR_VERSION
    ):
        raise ValidationError("reconcile state document version mismatch")


def parse_daily_links_reconcile_cursor(
    data: Mapping[str, Any]
) -> DailyLinksReconcileCursor:
    """Strictly validate a parsed cursor document (pure, fail closed).

    Rejects missing/extra keys, schema id/version mismatch, SHA-invalid
    heads, unsafe folders, invalid formats, and projection format
    mismatch. Holds no titles or note bodies by construction.
    """
    _require_reconcile_document_header(data, _RECONCILE_CURSOR_JSON_KEYS)
    head = validate_git_commit_sha(data["reconciled_head"])
    folder = data["daily_folder"]
    if not isinstance(folder, str):
        raise ValidationError("reconcile cursor daily folder must be a string")
    if folder:
        _validate_relative_note_path(folder)
    fmt = data["daily_format"]
    if not isinstance(fmt, str):
        raise ValidationError("reconcile cursor daily format must be a string")
    validate_daily_note_format(fmt)
    projection_format = data["projection_format"]
    if isinstance(projection_format, bool) or not isinstance(projection_format, int):
        raise ValidationError(
            "reconcile cursor projection format must be an integer"
        )
    if projection_format != DAILY_LINKS_PROJECTION_FORMAT_VERSION:
        raise ValidationError("reconcile cursor projection format mismatch")
    return DailyLinksReconcileCursor(
        reconciled_head=head,
        daily_folder=folder,
        daily_format=fmt,
        projection_format=projection_format,
        version=data["version"],
    )


def parse_daily_links_reconcile_pending(
    data: Mapping[str, Any]
) -> DailyLinksReconcilePending:
    """Strictly validate a parsed pending document (pure, fail closed).

    The applied-routing pin (``daily_folder``/``daily_format``) is
    validated with the same strictness as the cursor's routing fields.
    """
    _require_reconcile_document_header(data, _RECONCILE_PENDING_JSON_KEYS)
    from_head = validate_git_commit_sha(data["from_head"])
    to_head = validate_git_commit_sha(data["to_head"])
    started_at = data["started_at"]
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, int)
        or started_at <= 0
    ):
        raise ValidationError(
            "reconcile pending started_at must be a positive integer"
        )
    folder = data["daily_folder"]
    if not isinstance(folder, str):
        raise ValidationError("reconcile pending daily folder must be a string")
    if folder:
        _validate_relative_note_path(folder)
    fmt = data["daily_format"]
    if not isinstance(fmt, str):
        raise ValidationError("reconcile pending daily format must be a string")
    validate_daily_note_format(fmt)
    return DailyLinksReconcilePending(
        from_head=from_head,
        to_head=to_head,
        started_at=started_at,
        daily_folder=folder,
        daily_format=fmt,
        version=data["version"],
    )


def _daily_links_reconcile_cursor_payload(
    cursor: DailyLinksReconcileCursor
) -> Dict[str, Any]:
    """Canonical (sorted-keys on dump) structural cursor payload."""
    return {
        "schema": DAILY_LINKS_RECONCILE_SCHEMA_ID,
        "version": cursor.version,
        "reconciled_head": cursor.reconciled_head,
        "daily_folder": cursor.daily_folder,
        "daily_format": cursor.daily_format,
        "projection_format": cursor.projection_format,
    }


def _daily_links_reconcile_pending_payload(
    pending: DailyLinksReconcilePending
) -> Dict[str, Any]:
    """Canonical (sorted-keys on dump) structural pending payload."""
    return {
        "schema": DAILY_LINKS_RECONCILE_SCHEMA_ID,
        "version": pending.version,
        "from_head": pending.from_head,
        "to_head": pending.to_head,
        "started_at": pending.started_at,
        "daily_folder": pending.daily_folder,
        "daily_format": pending.daily_format,
    }


def _read_reconcile_json_document(
    path: Path, *, max_size: int
) -> Optional[Mapping[str, Any]]:
    """Read a bounded runtime JSON object no-follow; ``None`` when absent.

    Any present-but-unreadable, symlinked, non-regular, oversized, or
    non-JSON document fails closed with typed, content-free errors.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathError("reconcile state file is a symlink") from exc
        raise PathError("cannot open reconcile state file") from exc
    closed = False
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise PathError("cannot inspect reconcile state file") from exc
        if not stat_is_regular_mode(st.st_mode):
            raise PathError("reconcile state file is not a regular file")
        try:
            data = _read_fd_bytes_bounded(fd, max_size)  # closes fd itself
            closed = True
        except CoreError:
            raise CoreError("reconcile state file exceeds size bound") from None
    finally:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("reconcile state file is not valid UTF-8") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("reconcile state file is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("reconcile state file root must be an object")
    return parsed


def _write_reconcile_json_document(
    path: Path, payload: Mapping[str, Any]
) -> None:
    """Atomically publish a bounded runtime JSON document.

    Creates the parent directory (runtime state area, never the vault),
    writes a restrictive-mode ``O_EXCL|O_NOFOLLOW`` temp sibling, fsyncs
    it, publishes with ``os.replace`` (no-follow by construction), and
    best-effort fsyncs the parent. The temp file is always cleaned up on
    failure. Errors are typed and content-free.
    """
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(encoded) > RECONCILE_CURSOR_MAX_FILE_SIZE:
        raise ValidationError("reconcile state payload exceeds size bound")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathError("cannot prepare reconcile state directory") from exc
    parent_fd = _open_directory_no_follow(path.parent)
    name = path.name
    temp_name: Optional[str] = None
    temp_fd: Optional[int] = None
    published = False
    try:
        for _ in range(8):
            candidate = f".{name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
            try:
                temp_fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PathError(
                        "reconcile state temp path is a symlink"
                    ) from exc
                raise CoreError(
                    "cannot create reconcile state temp file"
                ) from exc
            temp_name = candidate
            break
        if temp_name is None or temp_fd is None:
            raise CoreError("cannot create reconcile state temp file")
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(temp_fd, view)
                view = view[written:]
            os.fsync(temp_fd)
            os.fchmod(temp_fd, RECONCILE_CURSOR_FILE_MODE)
        except OSError as exc:
            raise CoreError("cannot write reconcile state file") from exc
        finally:
            os.close(temp_fd)
            temp_fd = None
        try:
            os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            published = True
        except OSError as exc:
            raise CoreError("cannot publish reconcile state file") from exc
        try:
            os.fsync(parent_fd)
        except OSError:
            pass  # parent fsync is best-effort (not supported everywhere)
    finally:
        if temp_name is not None and not published:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        os.close(parent_fd)


def load_daily_links_reconcile_cursor(
    path: Optional[Path] = None,
) -> Optional[DailyLinksReconcileCursor]:
    """Load the reconcile cursor; ``None`` means bootstrap (no cursor yet).

    A missing cursor is the bootstrap signal. Any present-but-invalid
    document fails closed (typed ``CoreError`` subclasses).
    """
    cursor_path = DAILY_LINKS_RECONCILE_CURSOR_PATH if path is None else path
    data = _read_reconcile_json_document(
        cursor_path, max_size=RECONCILE_CURSOR_MAX_FILE_SIZE
    )
    if data is None:
        return None
    return parse_daily_links_reconcile_cursor(data)


def write_daily_links_reconcile_cursor(
    cursor: DailyLinksReconcileCursor,
    path: Optional[Path] = None,
) -> None:
    """Validate and atomically publish the reconcile cursor."""
    if not isinstance(cursor, DailyLinksReconcileCursor):
        raise ValidationError(
            "reconcile cursor write requires a DailyLinksReconcileCursor"
        )
    payload = _daily_links_reconcile_cursor_payload(cursor)
    # Fail closed before touching the filesystem if the payload would
    # not round-trip through the strict parser.
    parse_daily_links_reconcile_cursor(payload)
    cursor_path = DAILY_LINKS_RECONCILE_CURSOR_PATH if path is None else path
    _write_reconcile_json_document(cursor_path, payload)


def load_daily_links_reconcile_pending(
    path: Optional[Path] = None,
) -> Optional[DailyLinksReconcilePending]:
    """Load the pending sibling; ``None`` when no reconciliation is in flight.

    Any present-but-invalid document fails closed (typed ``CoreError``
    subclasses); policy for stale pending state belongs to later phases.
    """
    pending_path = DAILY_LINKS_RECONCILE_PENDING_PATH if path is None else path
    data = _read_reconcile_json_document(
        pending_path, max_size=RECONCILE_CURSOR_MAX_FILE_SIZE
    )
    if data is None:
        return None
    return parse_daily_links_reconcile_pending(data)


def write_daily_links_reconcile_pending(
    pending: DailyLinksReconcilePending,
    path: Optional[Path] = None,
) -> None:
    """Validate and atomically publish the pending sibling."""
    if not isinstance(pending, DailyLinksReconcilePending):
        raise ValidationError(
            "reconcile pending write requires a DailyLinksReconcilePending"
        )
    payload = _daily_links_reconcile_pending_payload(pending)
    parse_daily_links_reconcile_pending(payload)
    pending_path = DAILY_LINKS_RECONCILE_PENDING_PATH if path is None else path
    _write_reconcile_json_document(pending_path, payload)


def clear_daily_links_reconcile_pending(path: Optional[Path] = None) -> bool:
    """Unlink the pending sibling no-follow; True when it existed."""
    pending_path = DAILY_LINKS_RECONCILE_PENDING_PATH if path is None else path
    try:
        os.unlink(str(pending_path))
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PathError("cannot clear reconcile pending file") from exc


@dataclass(frozen=True)
class GitTaskObjectRead:
    """Typed result of a bounded ``git show <sha>:<path>`` object read.

    ``state`` is ``present`` (``text`` holds the strict-UTF-8 object
    body) or ``missing`` — the typed no-before-state result for a task
    path that does not exist at the requested commit.
    """

    state: str
    text: Optional[str] = None


def _run_git_show_bounded(
    vault: Path,
    git_env: Dict[str, str],
    rev: str,
    *,
    max_size: int,
    timeout: float,
) -> bytes:
    """Stream ``git show <rev>`` with a kill/reap hard size cap.

    Mirrors the bounded subprocess runner: disk-backed capture files, a
    polled size cap on both streams (``max_size`` for the object body —
    deliberately ``LIST_MAX_FILE_SIZE``-aligned, not ``MAX_OUTPUT`` —
    and ``MAX_OUTPUT`` for diagnostics), and process-group SIGKILL plus
    reap on cap or timeout. Returns raw object bytes (never decoded
    here).
    """
    argv = ["git"] + _GIT_BASE_ARGS + ["show", rev]
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        try:
            proc = subprocess.Popen(
                argv,
                stdout=stdout_file,
                stderr=stderr_file,
                env=git_env,
                cwd=str(vault),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SubprocessError("executable not found: git") from exc
        except OSError as exc:
            raise SubprocessError("cannot start subprocess") from exc
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if (
                os.fstat(stdout_file.fileno()).st_size > max_size
                or os.fstat(stderr_file.fileno()).st_size > MAX_OUTPUT
            ):
                _kill_reap(proc)
                raise SubprocessError("git object exceeds size bound")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_reap(proc)
                raise SubprocessError("git object read timed out")
            try:
                proc.wait(timeout=min(0.01, remaining))
            except subprocess.TimeoutExpired:
                pass
        if (
            os.fstat(stdout_file.fileno()).st_size > max_size
            or os.fstat(stderr_file.fileno()).st_size > MAX_OUTPUT
        ):
            raise SubprocessError("git object exceeds size bound")
        if proc.returncode != 0:
            stderr_file.seek(0)
            err = stderr_file.read(MAX_OUTPUT + 1).decode("utf-8", errors="replace")
            raise GitError(f"git show failed: {_redact(err)[:200]}")
        stdout_file.seek(0)
        data = stdout_file.read(max_size + 1)
        if len(data) > max_size:
            raise SubprocessError("git object exceeds size bound")
        return data


def read_git_task_object(
    vault: Path,
    sha: str,
    rel_path: str,
    git_env: Optional[Dict[str, str]] = None,
    *,
    max_size: int = LIST_MAX_FILE_SIZE,
    timeout: float = GIT_TIMEOUT,
) -> GitTaskObjectRead:
    """Read ``git show <sha>:<vault-relative task path>`` bounded and no-shell.

    Fixed argv only (the SHA and path are strictly validated before
    interpolation), no shell, a minimal no-credential env, streamed
    output with a kill/reap hard cap aligned to ``max_size`` (default
    ``LIST_MAX_FILE_SIZE``, not ``MAX_OUTPUT``), and strict UTF-8
    decoding. A task path absent at the commit is the typed ``missing``
    state; an unknown commit, an unreadable repo, an oversized object,
    or a non-UTF-8 object raise typed content-free errors. No custom
    Git object/pack parsing: object membership is resolved through the
    ``rev-parse``/``ls-tree`` plumbing and the body through ``git show``.
    """
    sha = validate_git_commit_sha(sha)
    rel_path = validate_git_task_object_path(rel_path)
    if git_env is None:
        git_env = _build_git_env()
    rev = f"{sha}:{rel_path}"
    # The commit itself must exist: an unknown SHA is corruption from the
    # reconciler's perspective (fail closed), never a "missing" state.
    r = _run_git(
        vault, git_env,
        ["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
        timeout=timeout,
    )
    if r.returncode == 1:
        raise GitError("git object reader: commit not found")
    if r.returncode != 0:
        raise GitError(f"git object reader failed: {_redact(r.stderr)[:200]}")
    # Exact path membership in the commit tree (exit-code semantics only).
    r = _run_git(vault, git_env, ["ls-tree", sha, "--", rel_path], timeout=timeout)
    if r.returncode != 0:
        raise GitError(f"git object reader failed: {_redact(r.stderr)[:200]}")
    listing = r.stdout.strip()
    if not listing:
        return GitTaskObjectRead(state=GIT_OBJECT_MISSING, text=None)
    fields = listing.splitlines()[0].split("\t", 1)[0].split()
    if (
        len(fields) < 3
        or fields[1] != "blob"
        or fields[0] not in ("100644", "100755")
    ):
        # Git reports committed symlinks as blobs (mode 120000 whose
        # content is the link target): require an allowed regular-file
        # mode so a symlink can never masquerade as task content.
        raise CoreError("git object reader: path is not a regular blob")
    data = _run_git_show_bounded(
        vault, git_env, rev, max_size=max_size, timeout=timeout
    )
    try:
        text = data.decode("utf-8")  # strict
    except UnicodeDecodeError as exc:
        raise CoreError("git object reader: object is not valid UTF-8") from exc
    return GitTaskObjectRead(state=GIT_OBJECT_PRESENT, text=text)


@dataclass(frozen=True)
class ReconcileClassification:
    """Pure classification of one candidate file's frontmatter.

    ``candidate`` carries the task tag and a usable scheduled value;
    ``non_task`` is confidently not a task; ``malformed`` is task-scope
    ambiguity (unparsable tags, or a task with an unusable scheduled
    value).
    """

    cls: str
    scheduled: Optional[str] = None
    archived: bool = False


@dataclass(frozen=True)
class ReconcileTaskCandidate:
    """One valid task candidate in the reconciliation snapshot."""

    slug: str
    location: str
    scheduled: Optional[str]
    archived: bool = False


@dataclass(frozen=True)
class ReconcileSnapshot:
    """Deterministic, read-only reconciliation input snapshot."""

    head: str
    candidates: Tuple[ReconcileTaskCandidate, ...]


def reconcile_slug_from_filename(name: str) -> Optional[str]:
    """``<slug>.md`` -> validated slug, else ``None`` (not a candidate name)."""
    if not name.endswith(".md"):
        return None
    try:
        return validate_slug(name[:-3])
    except PathError:
        return None


def classify_reconcile_frontmatter(
    frontmatter: Mapping[str, Any],
    profile: TaskNotesProfile,
) -> ReconcileClassification:
    """Pure, deterministic classification of task-file frontmatter.

    Built on the existing semantic parsing rules: only the configured
    task tag makes a file a task; the scheduled value must be absent
    (backlog), a plain ``YYYY-MM-DD`` date, or the gbrain-normalized
    bare-date form. Anything ambiguous in task scope is ``malformed``.
    """
    if not isinstance(frontmatter, Mapping):
        raise ValidationError("frontmatter must be a mapping")
    tags = frontmatter.get("tags")
    if tags is None:
        tags = []  # absent or null tags: confidently not a task
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return ReconcileClassification(cls=RECONCILE_CLASS_MALFORMED)
    tags_t = tuple(tags)
    archived = profile.archive_tag in tags_t
    if profile.task_tag not in tags_t:
        return ReconcileClassification(
            cls=RECONCILE_CLASS_NON_TASK, archived=archived
        )
    raw_scheduled = frontmatter.get(profile.mappings["scheduled"])
    if raw_scheduled is None:
        return ReconcileClassification(
            cls=RECONCILE_CLASS_CANDIDATE, scheduled=None, archived=archived
        )
    scheduled = _daily_scheduled_date(raw_scheduled)
    if scheduled is None:
        return ReconcileClassification(cls=RECONCILE_CLASS_MALFORMED, archived=archived)
    return ReconcileClassification(
        cls=RECONCILE_CLASS_CANDIDATE, scheduled=scheduled, archived=archived
    )


def enumerate_reconcile_candidates(
    vault: Path,
    profile: TaskNotesProfile,
    *,
    max_files: int = LIST_MAX_FILES,
    max_size: int = LIST_MAX_FILE_SIZE,
) -> Tuple[ReconcileTaskCandidate, ...]:
    """Enumerate valid task/archive candidates read-only and bounded.

    Top-level ``.md`` entries of the tasks folder plus — only when the
    archive is active (``moveArchivedTasks`` with a configured folder) —
    the archive folder. No recursion and no symlink following: symlinked
    or oversized task-scope entries fail closed, as does exceeding the
    file bound. A slug present in both folders is an ambiguous pair and
    fails closed. Malformed and non-task entries are classified and
    excluded. Nothing is ever written and no gbrain/PGLite call is made.
    """
    folders: List[Tuple[str, str]] = [(RECONCILE_LOCATION_TASKS, profile.tasks_folder)]
    if (
        profile.move_archived_tasks
        and profile.archive_folder
        and profile.archive_folder != profile.tasks_folder
    ):
        folders.append((RECONCILE_LOCATION_ARCHIVE, profile.archive_folder))
    seen: Dict[str, ReconcileTaskCandidate] = {}
    scanned = 0
    for location, folder in folders:
        if location == RECONCILE_LOCATION_ARCHIVE and not target_exists_no_follow(
            vault / folder
        ):
            continue  # the plugin creates the archive folder lazily
        directory_fd = _open_relative_directory_no_follow(vault, folder)
        try:
            for name in sorted(os.listdir(directory_fd)):
                if not name.endswith(".md"):
                    continue
                scanned += 1
                if scanned > max_files:
                    raise CoreError(
                        "reconcile candidate enumeration exceeds file bound"
                    )
                try:
                    text = _read_directory_entry_no_follow(
                        directory_fd, name, max_size=max_size
                    )
                except (PathError, CoreError):
                    # Symlinked/unreadable/oversized task-scope entries are
                    # ambiguity, not absence: fail closed.
                    raise
                slug = reconcile_slug_from_filename(name)
                if slug is None:
                    continue  # non-slug .md name: not a candidate
                try:
                    fm, _body = _parse_frontmatter(text)
                except Exception:
                    # Unparsable frontmatter (invalid YAML etc.):
                    # malformed, excluded.
                    continue
                classification = classify_reconcile_frontmatter(fm, profile)
                if classification.cls != RECONCILE_CLASS_CANDIDATE:
                    continue
                candidate = ReconcileTaskCandidate(
                    slug=slug,
                    location=location,
                    scheduled=classification.scheduled,
                    archived=classification.archived,
                )
                if slug in seen:
                    raise CoreError(
                        "ambiguous task candidate in both task and archive folders"
                    )
                seen[slug] = candidate
        finally:
            os.close(directory_fd)
    return tuple(sorted(seen.values(), key=lambda c: (c.slug, c.location)))


def build_reconcile_snapshot(
    vault: Path,
    profile: TaskNotesProfile,
    git_env: Optional[Dict[str, str]] = None,
    *,
    max_files: int = LIST_MAX_FILES,
    max_size: int = LIST_MAX_FILE_SIZE,
) -> ReconcileSnapshot:
    """Build a deterministic read-only snapshot for later reconciliation.

    Combines the validated current HEAD SHA with the bounded candidate
    enumeration. Pure data in, pure data out: no cursor access, no
    writes, no gbrain. Fails closed when HEAD is missing or invalid.
    """
    head = git_head_id(vault, git_env)
    if head is None:
        raise CoreError("reconcile snapshot requires a vault HEAD")
    head = validate_git_commit_sha(head)
    candidates = enumerate_reconcile_candidates(
        vault, profile, max_files=max_files, max_size=max_size
    )
    return ReconcileSnapshot(head=head, candidates=candidates)


# ---------------------------------------------------------------------------
# Daily-links reconciliation lifecycle (issue #139, W2b: prepare/apply/
# targeted-commit/finalize) — internal only
# ---------------------------------------------------------------------------
#
# Stable core API called by later phases under their existing lock. The
# lifecycle plans link transitions from the net external task changes
# between the cursor's ``reconciled_head`` (read through the W2a
# fixed-argv Git object reader) and the current committed HEAD +
# worktree (W2a bounded snapshot), applies them with the existing safe
# W1b Daily Note primitives (R1-R4), commits ONLY the actually changed
# daily notes (plus the tracked-and-daily-notes config when a routing
# change needs it) with explicit targeted staging — never ``git add -A``
# — and finally advances the cursor.
#
# Hard rules (do not weaken):
#   - Task Markdown is never written, staged, or rewritten by
#     reconciliation; gbrain/PGLite/the public wrapper are never
#     invoked; the recovery marker is never touched.
#   - A missing cursor bootstraps (ensure-only for currently scheduled
#     tasks; the only path allowed past the composed-transition bound,
#     via the internal bootstrap-only composition seam and deterministic
#     batching by distinct daily target — issue #144 W1); a corrupt
#     established cursor fails closed; pending/replay is idempotent.
#   - Transitions compose by ``(resolved daily target, slug)``: the
#     final ensure wins (a coarse target collapse is one ensure), the
#     destination is ensured before the old link is removed, old dates
#     route through the cursor's prior folder/format and current dates
#     through the current config, and every old/new target passes the
#     R5 collision fence. A config folder/format routing change re-homes
#     all currently scheduled tasks; a template-only change does not.
#   - All bounded candidates are prepared before anything is applied;
#     candidate/target overflow, source churn between prepare and
#     apply, projection conflicts, and commit failures fail closed with
#     no cursor advancement and no partial cursor. The targeted commit
#     accepts at most ``MAX_DAILY_PROJECTION_TARGETS`` changed daily
#     notes plus the single ``RECONCILE_CONFIG_RELPATH`` rider.
#     Established reconciliation keeps the total composed-transition
#     bound; only the bootstrap apply batches deterministically (max
#     distinct targets per batch, one targeted commit per batch, no
#     rider — bootstrap never re-homes routing).
#   - Bootstrap apply rechecks are full-candidate (issue #144 W2): a
#     bounded re-enumeration must exactly match the plan's in-memory
#     candidate evidence and HEAD must equal the locally expected head
#     before the first batch, before each later batch, and immediately
#     before publishing the pending. The expected head evolves only to
#     a proven direct child of the retained pre-commit head (exactly one
#     parent equal to it — an external commit before or reordered with
#     the targeted reconciliation commit fails closed), and immediately
#     before pending construction the current HEAD must still equal it;
#     external drift fails closed with no pending publication and no
#     rollback (partial batch commits stay so an unchanged retry
#     converges).
#   - The pending sibling structurally pins the applied Daily Notes
#     routing (folder/format). ``finalize_*`` requires the caller's
#     native-sync-success signal, verifies pending/head identity and
#     the pinned routing against its supplied config, then atomically
#     advances the cursor and clears the pending sibling; failures
#     leave both in place for replay. One crash window is idempotent:
#     a cursor already advanced to the pending's head just clears the
#     stale pending. Prepare resolves old-link routing through the
#     pending's pinned routing when one is present (it supersedes the
#     cursor's prior routing for an applied-but-unfinalized cycle).

# Reconciliation modes.
RECONCILE_MODE_BOOTSTRAP = "bootstrap"    # no cursor yet: ensure-only
RECONCILE_MODE_RECONCILE = "reconcile"    # established cursor: net diff

# Routing selection for a composed step.
RECONCILE_ROUTING_CURRENT = "current"     # resolve via the current config
RECONCILE_ROUTING_PRIOR = "prior"         # resolve via the cursor's prior routing

# Net external change classes (per slug).
RECONCILE_NET_ADDED = "added"             # not at reconciled_head, task now
RECONCILE_NET_REMOVED = "removed"         # at reconciled_head, gone now
RECONCILE_NET_TAG_LOSS = "tag_loss"       # file remains, task tag gone
RECONCILE_NET_RESCHEDULED = "rescheduled"  # scheduled changed (either way)
RECONCILE_NET_UNSCHEDULED = "unscheduled"  # scheduled cleared to backlog
RECONCILE_NET_ARCHIVE_MOVE = "archive_move"  # same slug/date, folder moved
RECONCILE_NET_UNCHANGED = "unchanged"     # incl. title/body-only edits

# Generic content-free reconciliation commit message.
RECONCILE_COMMIT_MSG = "tasknotes-mcp: daily links reconcile"

# Vault-relative Daily Notes config path staged with a routing change.
RECONCILE_CONFIG_RELPATH = f"{DAILY_NOTES_OBSIDIAN_DIR}/{DAILY_NOTES_CONFIG_NAME}"


@dataclass(frozen=True)
class ReconcileTaskState:
    """One slug's task state on one side of the comparison.

    ``present`` False models an absent file (all other fields then
    meaningless); ``is_task`` False models a present file that is not a
    managed task (e.g. task-tag loss).
    """

    slug: str
    present: bool
    is_task: bool = False
    location: Optional[str] = None
    scheduled: Optional[str] = None
    archived: bool = False


@dataclass(frozen=True)
class ReconcileNetChange:
    """Pure net external change for one slug, with planned link dates.

    ``ensure_date``/``remove_date`` are link-level dates (prior routing
    for the remove, current routing for the ensure — resolved later);
    ``current_scheduled`` carries the task's current scheduled date so a
    routing change can re-home otherwise-unchanged tasks.
    """

    cls: str
    current_scheduled: Optional[str] = None
    ensure_date: Optional[str] = None
    remove_date: Optional[str] = None


@dataclass(frozen=True)
class ReconcileComposedStep:
    """One composed transition, resolved to its daily-note target."""

    operation: str
    slug: str
    date: str
    routing: str
    target_relative: str


@dataclass(frozen=True)
class ReconcileTransition:
    """A composed step with its pre-prepared Daily Note projection."""

    operation: str
    slug: str
    date: str
    routing: str
    target_relative: str
    projection: DailyNoteProjection


@dataclass(frozen=True)
class ReconcilePlan:
    """Everything needed to apply one reconciliation pass (no side effects yet).

    ``from_head`` is the head the comparison was made against (the
    cursor's ``reconciled_head``, or the current head for bootstrap);
    ``to_head`` is the current head at prepare time. Bootstrap plans
    (missing cursor) may carry more than ``MAX_DAILY_PROJECTION_TARGETS``
    transitions (issue #144 W1); apply batches them deterministically
    by distinct target.

    ``expected_candidates`` is IN-MEMORY-ONLY expected full candidate
    identity/state evidence (issue #144 W2): every bounded candidate of
    the prepared snapshot — bootstrap captures all of them, including
    unscheduled ones — and the current-side state of every slug in an
    established plan's old/current union. Each entry is structural only
    (slug, presence, task-ness, location, scheduled, archived; never
    titles, bodies, or any note content) and is never persisted; apply
    re-proves an exact set/state match against a fresh bounded
    enumeration.
    """

    mode: str
    cursor: Optional[DailyLinksReconcileCursor]
    prior: Optional[DailyNotesConfig]
    from_head: str
    to_head: str
    routing_changed: bool
    transitions: Tuple[ReconcileTransition, ...]
    net_classes: Tuple[Tuple[str, str], ...]
    expected_candidates: Tuple[ReconcileTaskState, ...]
    config_commit_needed: bool


@dataclass(frozen=True)
class ReconcileApplyOutcome:
    """Structured outcome of an applied reconciliation plan."""

    applied: int
    changed_targets: Tuple[str, ...]
    commit_created: bool
    commit_id: Optional[str]
    pending: DailyLinksReconcilePending


def classify_reconcile_net_change(
    old_state: Optional[ReconcileTaskState],
    new_state: Optional[ReconcileTaskState],
) -> ReconcileNetChange:
    """Pure, deterministic classification of one slug's net external change.

    ``old_state`` is the task state at the cursor's ``reconciled_head``
    (``None`` = absent there), ``new_state`` the current worktree state.
    A slug rename manifests naturally as ``removed``(old) +
    ``added``(new), which composes to the correct remove+ensure pair.
    Title/body-only edits and same-slug archive moves never change the
    link, so they classify as ``unchanged``/``archive_move`` with no
    transitions; titles are never projected.
    """
    for state in (old_state, new_state):
        if state is not None and not isinstance(state, ReconcileTaskState):
            raise ValidationError(
                "reconcile net change requires ReconcileTaskState inputs"
            )
    # Normalize None to an absent state so the branch logic below is total.
    old = old_state if old_state is not None else ReconcileTaskState(
        slug="", present=False
    )
    new = new_state if new_state is not None else ReconcileTaskState(
        slug="", present=False
    )
    if not old.present and not new.present:
        return ReconcileNetChange(cls=RECONCILE_NET_UNCHANGED)
    if not old.present:
        if new.is_task:
            return ReconcileNetChange(
                cls=RECONCILE_NET_ADDED,
                current_scheduled=new.scheduled,
                ensure_date=new.scheduled,
            )
        return ReconcileNetChange(cls=RECONCILE_NET_UNCHANGED)
    if not new.present:
        return ReconcileNetChange(
            cls=RECONCILE_NET_REMOVED,
            remove_date=old.scheduled if old.is_task else None,
        )
    if old.is_task and not new.is_task:
        return ReconcileNetChange(
            cls=RECONCILE_NET_TAG_LOSS,
            remove_date=old.scheduled,
        )
    if not old.is_task and new.is_task:
        return ReconcileNetChange(
            cls=RECONCILE_NET_ADDED,
            current_scheduled=new.scheduled,
            ensure_date=new.scheduled,
        )
    if not old.is_task and not new.is_task:
        return ReconcileNetChange(cls=RECONCILE_NET_UNCHANGED)
    # Both sides are managed tasks.
    if old.scheduled != new.scheduled:
        if old.scheduled is None:
            return ReconcileNetChange(
                cls=RECONCILE_NET_RESCHEDULED,
                current_scheduled=new.scheduled,
                ensure_date=new.scheduled,
            )
        if new.scheduled is None:
            return ReconcileNetChange(
                cls=RECONCILE_NET_UNSCHEDULED,
                remove_date=old.scheduled,
            )
        return ReconcileNetChange(
            cls=RECONCILE_NET_RESCHEDULED,
            current_scheduled=new.scheduled,
            ensure_date=new.scheduled,
            remove_date=old.scheduled,
        )
    if old.location != new.location:
        return ReconcileNetChange(
            cls=RECONCILE_NET_ARCHIVE_MOVE,
            current_scheduled=new.scheduled,
        )
    return ReconcileNetChange(
        cls=RECONCILE_NET_UNCHANGED,
        current_scheduled=new.scheduled,
    )


def compose_reconcile_steps(
    vault: Path,
    profile: TaskNotesProfile,
    config: DailyNotesConfig,
    prior: Optional[DailyNotesConfig],
    nets: Mapping[str, ReconcileNetChange],
) -> Tuple[ReconcileComposedStep, ...]:
    """Compose net changes into ordered, target-resolved steps (pure).

    Steps compose by ``(resolved daily target, slug)``: the final ensure
    wins over a remove on the same physical target (a coarse target
    collapse is one ensure), ensures are ordered before removes so a
    partial failure never loses a link, and each group is sorted by
    ``(target, slug)`` for determinism. Ensure dates resolve through the
    current config; remove dates through the cursor's prior routing.
    Every old/new target passes the R5 collision fence. Exceeding the
    target bound fails closed. This bound is the established-cursor
    policy; a missing-cursor bootstrap must compose through the internal
    :func:`_compose_reconcile_steps_bootstrap` seam instead (issue #144).
    """
    return _compose_reconcile_steps_impl(
        vault, profile, config, prior, nets, enforce_target_bound=True
    )


def _compose_reconcile_steps_impl(
    vault: Path,
    profile: TaskNotesProfile,
    config: DailyNotesConfig,
    prior: Optional[DailyNotesConfig],
    nets: Mapping[str, ReconcileNetChange],
    *,
    enforce_target_bound: bool,
) -> Tuple[ReconcileComposedStep, ...]:
    """Shared composition body; ``enforce_target_bound`` selects whether
    the total composed-transition bound is enforced (established-cursor
    policy) or lifted (bootstrap-only seam, issue #144 W1). Internal."""
    if not isinstance(config, DailyNotesConfig):
        raise ValidationError(
            "reconcile composition requires a validated DailyNotesConfig"
        )
    if prior is not None and not isinstance(prior, DailyNotesConfig):
        raise ValidationError(
            "reconcile prior routing must be a DailyNotesConfig"
        )
    routing_changed = prior is not None and (
        prior.folder,
        prior.format,
    ) != (config.folder, config.format)
    raw: List[Tuple[str, str, str, str]] = []
    for slug in sorted(nets):
        net = nets[slug]
        if not isinstance(net, ReconcileNetChange):
            raise ValidationError(
                "reconcile composition requires ReconcileNetChange values"
            )
        if net.ensure_date:
            raw.append(
                (DAILY_PROJECTION_OP_ENSURE, slug, net.ensure_date,
                 RECONCILE_ROUTING_CURRENT)
            )
        if net.remove_date:
            raw.append(
                (DAILY_PROJECTION_OP_REMOVE, slug, net.remove_date,
                 RECONCILE_ROUTING_PRIOR)
            )
        if (
            routing_changed
            and net.cls in (RECONCILE_NET_UNCHANGED, RECONCILE_NET_ARCHIVE_MOVE)
            and net.current_scheduled
        ):
            # Config routing change: re-home this unchanged task directly.
            raw.append(
                (DAILY_PROJECTION_OP_REMOVE, slug, net.current_scheduled,
                 RECONCILE_ROUTING_PRIOR)
            )
            raw.append(
                (DAILY_PROJECTION_OP_ENSURE, slug, net.current_scheduled,
                 RECONCILE_ROUTING_CURRENT)
            )
    ensures: Dict[Tuple[str, str], ReconcileComposedStep] = {}
    removes: Dict[Tuple[str, str], ReconcileComposedStep] = {}
    for operation, slug, date, routing in raw:
        cfg = config if routing == RECONCILE_ROUTING_CURRENT else prior
        if cfg is None:
            raise ValidationError(
                "reconcile remove requires a prior routing"
            )
        target = resolve_daily_note_path(vault, cfg, date)
        target_relative = target.relative_to(vault).as_posix()
        _reject_daily_projection_collision(profile, target_relative)
        step = ReconcileComposedStep(
            operation=operation,
            slug=slug,
            date=date,
            routing=routing,
            target_relative=target_relative,
        )
        key = (target_relative, slug)
        if operation == DAILY_PROJECTION_OP_ENSURE:
            ensures[key] = step
        else:
            removes[key] = step
    ensure_steps = sorted(
        ensures.values(), key=lambda s: (s.target_relative, s.slug)
    )
    remove_steps = sorted(
        (s for k, s in removes.items() if k not in ensures),
        key=lambda s: (s.target_relative, s.slug),
    )
    composed = ensure_steps + remove_steps
    if enforce_target_bound and len(composed) > MAX_DAILY_PROJECTION_TARGETS:
        raise CoreError("reconcile transitions exceed target bound")
    return tuple(composed)


def _compose_reconcile_steps_bootstrap(
    vault: Path,
    profile: TaskNotesProfile,
    config: DailyNotesConfig,
    nets: Mapping[str, ReconcileNetChange],
) -> Tuple[ReconcileComposedStep, ...]:
    """Bootstrap-only composition seam: no total composed-transition bound.

    Internal-only (issue #144 W1); never caller-selectable. Exclusively
    for a missing-cursor bootstrap, which is ensure-only for currently
    scheduled tasks (no prior routing, no removes), so the shared
    composition semantics — same ``(target, slug)`` collapse, ordering,
    determinism, and the R5 collision fence — still apply unchanged;
    only the total bound is lifted. Apply batches the result by distinct
    target via :func:`_batch_reconcile_transitions`. Established
    reconciliation must keep using :func:`compose_reconcile_steps`
    (bound enforced, fail closed before any side effect).
    """
    return _compose_reconcile_steps_impl(
        vault, profile, config, None, nets, enforce_target_bound=False
    )


_ReconcileTransitionT = TypeVar(
    "_ReconcileTransitionT", ReconcileComposedStep, ReconcileTransition
)


def _batch_reconcile_transitions(
    transitions: Tuple[_ReconcileTransitionT, ...],
) -> Tuple[Tuple[_ReconcileTransitionT, ...], ...]:
    """Deterministically split composed transitions into target batches.

    Issue #144 W1 bootstrap-only helper (internal; never caller
    selectable). Membership is keyed by distinct ``target_relative``:
    every transition resolving to the same Daily Note target stays in
    exactly one batch (a target is never split across batches), each
    batch holds at most ``MAX_DAILY_PROJECTION_TARGETS`` distinct
    targets, and both membership and order derive solely from the
    deterministic composed order (targets batch in first-appearance
    order; within-batch order is the input order). Pure: no side
    effects. An empty input yields no batches.
    """
    batches: List[List[Any]] = []
    targets_per_batch: List[int] = []
    batch_of_target: Dict[str, int] = {}
    for transition in transitions:
        target = transition.target_relative
        index = batch_of_target.get(target)
        if index is None:
            if batches and targets_per_batch[-1] < MAX_DAILY_PROJECTION_TARGETS:
                index = len(batches) - 1
            else:
                batches.append([])
                targets_per_batch.append(0)
                index = len(batches) - 1
            batch_of_target[target] = index
            targets_per_batch[index] += 1
        batches[index].append(transition)
    return tuple(tuple(batch) for batch in batches)


def list_git_tree_task_slugs(
    vault: Path,
    sha: str,
    dir_rel: str,
    git_env: Optional[Dict[str, str]] = None,
    *,
    timeout: float = GIT_TIMEOUT,
    max_files: int = LIST_MAX_FILES,
) -> Tuple[str, ...]:
    """Enumerate top-level candidate slugs of one directory at a commit.

    Read-only plumbing on ``git ls-tree <sha> -- <dir>/`` (non-recursive,
    exit-code semantics only — no custom object/pack parsing). Missing
    directories enumerate empty; symlinked or non-regular ``.md`` entries
    and bound overruns fail closed with content-free errors.
    """
    sha = validate_git_commit_sha(sha)
    _validate_relative_note_path(dir_rel)
    if git_env is None:
        git_env = _build_git_env()
    r = _run_git(
        vault, git_env, ["ls-tree", sha, "--", f"{dir_rel}/"], timeout=timeout
    )
    if r.returncode != 0:
        raise GitError(f"git object reader failed: {_redact(r.stderr)[:200]}")
    slugs: List[str] = []
    count = 0
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        meta, tab, path = line.partition("\t")
        fields = meta.split()
        if not tab or len(fields) < 3:
            raise CoreError("reconcile head enumeration: unexpected entry")
        name = path.rsplit("/", 1)[-1]
        if not name.endswith(".md"):
            continue
        count += 1
        if count > max_files:
            raise CoreError("reconcile head enumeration exceeds file bound")
        if fields[0] not in ("100644", "100755"):
            raise CoreError(
                "reconcile head enumeration: entry is not a regular file"
            )
        slug = reconcile_slug_from_filename(name)
        if slug is not None:
            slugs.append(slug)
    return tuple(sorted(set(slugs)))


def _reconcile_probe_locations(profile: TaskNotesProfile) -> List[Tuple[str, str]]:
    """Task/archive probe locations (location label, vault-relative path)."""
    locations = [
        (RECONCILE_LOCATION_TASKS, f"{profile.tasks_folder}/{{slug}}.md")
    ]
    if (
        profile.move_archived_tasks
        and profile.archive_folder
        and profile.archive_folder != profile.tasks_folder
    ):
        locations.append(
            (RECONCILE_LOCATION_ARCHIVE, f"{profile.archive_folder}/{{slug}}.md")
        )
    return locations


def _classify_reconcile_probe(
    slug: str,
    location: str,
    text: str,
    profile: TaskNotesProfile,
    *,
    where: str,
) -> ReconcileTaskState:
    """Classify one probed task-file body; any ambiguity fails closed."""
    try:
        fm, _body = _parse_frontmatter(text)
    except Exception as exc:
        raise CoreError(f"reconcile probe: ambiguous task state ({where})") from exc
    classification = classify_reconcile_frontmatter(fm, profile)
    if classification.cls == RECONCILE_CLASS_MALFORMED:
        raise CoreError(f"reconcile probe: ambiguous task state ({where})")
    if classification.cls == RECONCILE_CLASS_NON_TASK:
        return ReconcileTaskState(
            slug=slug, present=True, is_task=False, location=location
        )
    return ReconcileTaskState(
        slug=slug,
        present=True,
        is_task=True,
        location=location,
        scheduled=classification.scheduled,
        archived=classification.archived,
    )


def _head_reconcile_task_state(
    vault: Path,
    profile: TaskNotesProfile,
    sha: str,
    slug: str,
    git_env: Dict[str, str],
    *,
    max_size: int,
) -> ReconcileTaskState:
    """Read one slug's task state at a commit through the W2a Git reader.

    Checks the tasks folder then the active archive folder; present in
    both is an ambiguous pair (fail closed). Unparsable or malformed old
    content is ambiguity (fail closed); an absent file is the absent
    state.
    """
    found: List[Tuple[str, str]] = []
    for location, template in _reconcile_probe_locations(profile):
        rel = template.format(slug=slug)
        result = read_git_task_object(
            vault, sha, rel, git_env, max_size=max_size
        )
        if result.state == GIT_OBJECT_PRESENT:
            found.append((location, result.text or ""))
    if len(found) > 1:
        raise CoreError("reconcile probe: ambiguous task state (head)")
    if not found:
        return ReconcileTaskState(slug=slug, present=False)
    location, text = found[0]
    return _classify_reconcile_probe(
        slug, location, text, profile, where="head"
    )


def _current_reconcile_task_state(
    vault: Path,
    profile: TaskNotesProfile,
    slug: str,
    snapshot_map: Mapping[str, ReconcileTaskCandidate],
    *,
    max_size: int,
) -> ReconcileTaskState:
    """Read one slug's current task state from the bounded worktree view.

    Snapshot candidates are used directly. A slug missing from the
    snapshot is probed no-follow to distinguish an absent file from a
    present non-task (task-tag loss); present in both task and archive
    folders, unparsable, or malformed content is ambiguity (fail
    closed).
    """
    candidate = snapshot_map.get(slug)
    if candidate is not None:
        return ReconcileTaskState(
            slug=slug,
            present=True,
            is_task=True,
            location=candidate.location,
            scheduled=candidate.scheduled,
            archived=candidate.archived,
        )
    found: List[Tuple[str, str]] = []
    for location, template in _reconcile_probe_locations(profile):
        rel = template.format(slug=slug)
        if target_exists_no_follow(vault / rel):
            found.append((location, rel))
    if len(found) > 1:
        raise CoreError("reconcile probe: ambiguous task state (worktree)")
    if not found:
        return ReconcileTaskState(slug=slug, present=False)
    location, rel = found[0]
    text = read_file_no_follow(vault / rel, max_size=max_size)
    return _classify_reconcile_probe(
        slug, location, text, profile, where="worktree"
    )


def _config_tracked_and_dirty(
    vault: Path, git_env: Dict[str, str]
) -> bool:
    """True when the Daily Notes config is tracked AND has changes."""
    r = _run_git(vault, git_env, ["ls-files", "--", RECONCILE_CONFIG_RELPATH])
    if r.returncode != 0:
        raise GitError(f"git ls-files failed: {_redact(r.stderr)[:200]}")
    if not r.stdout.strip():
        return False
    r = _run_git(
        vault, git_env, ["status", "--porcelain", "--", RECONCILE_CONFIG_RELPATH]
    )
    if r.returncode != 0:
        raise GitError(f"git status failed: {_redact(r.stderr)[:200]}")
    return r.stdout.strip() != ""


def git_commit_reconcile_targets(
    vault: Path,
    targets: List[Path],
    git_env: Optional[Dict[str, str]] = None,
) -> bool:
    """Stage only the provided reconciliation paths and commit iff staged.

    Explicit targeted companion to the task Git helpers: it stages
    exactly the given (already vault-confined) changed paths — never
    ``git add -A`` — and creates one generic content-free reconciliation
    commit. The allowed set is at most ``MAX_DAILY_PROJECTION_TARGETS``
    daily-note (``.md``) targets plus the single validated
    ``RECONCILE_CONFIG_RELPATH`` rider when a routing change needs it;
    any other or excess path fails closed (a legal full bound of
    ``MAX`` changed notes plus the config rider commits as one
    17-path-max set). Targets are lexically validated to stay inside
    the vault (relative, no traversal, no backslash). Hooks, signing,
    gc, and maintenance are disabled command-locally. Never runs
    checkout/reset/clean/merge/pull/push. Returns True if a commit was
    created, False if nothing was staged. Unrelated dirty or staged
    paths are untouched (the pathspec commit excludes them).
    """
    if git_env is None:
        git_env = _build_git_env()
    rels: List[str] = []
    seen: set = set()
    for target in targets:
        path = Path(target)
        if path.is_absolute():
            try:
                rel = path.relative_to(vault)
            except ValueError as exc:
                raise PathError(
                    "reconcile commit target is not in the vault"
                ) from exc
        else:
            rel = path
        rel_str = rel.as_posix()
        _validate_relative_note_path(rel_str)
        if rel_str in seen:
            continue
        seen.add(rel_str)
        rels.append(rel_str)
    if not rels:
        return False
    note_targets = [r for r in rels if r != RECONCILE_CONFIG_RELPATH]
    for rel in note_targets:
        if not rel.endswith(".md"):
            raise ValidationError("reconcile commit target is not a daily note")
    if len(note_targets) > MAX_DAILY_PROJECTION_TARGETS:
        raise ValidationError("reconcile commit targets exceed count bound")
    r = _run_git(vault, git_env, ["add", "--"] + rels)
    if r.returncode != 0:
        raise GitError(f"git add reconcile targets failed: {_redact(r.stderr)[:200]}")
    r = _run_git(vault, git_env, ["diff", "--cached", "--quiet"])
    if r.returncode == 0:
        return False
    r = _run_git(
        vault, git_env, ["commit", "-m", RECONCILE_COMMIT_MSG, "--"] + rels
    )
    if r.returncode != 0:
        raise GitError(f"git reconcile commit failed: {_redact(r.stderr)[:200]}")
    return True


def prepare_daily_links_reconciliation(
    vault: Path,
    profile: TaskNotesProfile,
    config: DailyNotesConfig,
    git_env: Optional[Dict[str, str]] = None,
    *,
    cursor: Optional[DailyLinksReconcileCursor] = None,
    cursor_path: Optional[Path] = None,
    pending_path: Optional[Path] = None,
    max_files: int = LIST_MAX_FILES,
    max_size: int = LIST_MAX_FILE_SIZE,
) -> ReconcilePlan:
    """Plan one reconciliation pass (read-only, no side effects).

    With a missing cursor (bootstrap), the plan is ensure-only for the
    currently scheduled tasks, composed through the internal
    bootstrap-only seam so more than ``MAX_DAILY_PROJECTION_TARGETS``
    composed transitions are allowed (issue #144 W1); apply batches
    them deterministically by distinct target. With an established
    cursor, each slug's state at ``reconciled_head`` is read through
    the W2a Git object reader and compared to the current committed
    HEAD + worktree (W2a bounded snapshot plus targeted no-follow
    probes), producing per-slug net classifications and composed
    transitions. A corrupt established cursor fails closed (the loader
    raises). All bounded candidates are prepared before anything is
    applied; an established-cursor overflow fails closed. Task Markdown
    is never written and no gbrain/PGLite call is made.

    Old-routing lookup uses the applied routing pinned by an existing
    pending sibling when one is present (an applied-but-unfinalized
    cycle committed its links at that routing, superseding the
    cursor's prior routing); a pending that matches neither the cursor
    head nor the cursor's base fails closed as foreign state. The
    effective prior routing is carried on the plan so apply re-uses
    the identical routing for race recomputation.
    """
    if not isinstance(config, DailyNotesConfig):
        raise ValidationError(
            "reconcile prepare requires a validated DailyNotesConfig"
        )
    if git_env is None:
        git_env = _build_git_env()
    if cursor is None:
        cursor = load_daily_links_reconcile_cursor(cursor_path)
    pending = load_daily_links_reconcile_pending(pending_path)
    snapshot = build_reconcile_snapshot(
        vault, profile, git_env, max_files=max_files, max_size=max_size
    )
    head = snapshot.head
    current_map = {c.slug: c for c in snapshot.candidates}
    new_states: Dict[str, ReconcileTaskState] = {}
    nets: Dict[str, ReconcileNetChange] = {}
    prior: Optional[DailyNotesConfig] = None
    if cursor is None:
        mode = RECONCILE_MODE_BOOTSTRAP
        from_head = head
        for slug in sorted(current_map):
            new_states[slug] = _current_reconcile_task_state(
                vault, profile, slug, current_map, max_size=max_size
            )
            nets[slug] = classify_reconcile_net_change(None, new_states[slug])
    else:
        mode = RECONCILE_MODE_RECONCILE
        from_head = cursor.reconciled_head
        prior = DailyNotesConfig(
            folder=cursor.daily_folder, format=cursor.daily_format
        )
        if pending is not None:
            if (
                pending.from_head != cursor.reconciled_head
                and pending.to_head != cursor.reconciled_head
            ):
                raise CoreError(
                    "reconcile pending does not match the established cursor"
                )
            # The applied-but-unfinalized cycle committed its links at
            # the pending's pinned routing: it supersedes the cursor's
            # prior routing for old-link lookup (identical when the
            # cursor already advanced to the pending's head).
            prior = DailyNotesConfig(
                folder=pending.daily_folder, format=pending.daily_format
            )
        old_tasks = set(
            list_git_tree_task_slugs(
                vault, cursor.reconciled_head, profile.tasks_folder,
                git_env, max_files=max_files,
            )
        )
        old_archive: set = set()
        if (
            profile.move_archived_tasks
            and profile.archive_folder
            and profile.archive_folder != profile.tasks_folder
        ):
            old_archive = set(
                list_git_tree_task_slugs(
                    vault, cursor.reconciled_head, profile.archive_folder,
                    git_env, max_files=max_files,
                )
            )
        if old_tasks & old_archive:
            raise CoreError("reconcile probe: ambiguous task state (head)")
        for slug in sorted(old_tasks | old_archive | set(current_map)):
            old_state = _head_reconcile_task_state(
                vault, profile, cursor.reconciled_head, slug, git_env,
                max_size=max_size,
            )
            new_state = _current_reconcile_task_state(
                vault, profile, slug, current_map, max_size=max_size
            )
            new_states[slug] = new_state
            nets[slug] = classify_reconcile_net_change(old_state, new_state)
    routing_changed = prior is not None and (
        prior.folder,
        prior.format,
    ) != (config.folder, config.format)
    if mode == RECONCILE_MODE_BOOTSTRAP:
        # Issue #144 W1: bootstrap-only seam — a missing-cursor plan may
        # carry more than MAX composed ensure transitions; apply batches
        # them by distinct target. Established reconciliation keeps the
        # bound (fail closed before any side effect).
        steps = _compose_reconcile_steps_bootstrap(vault, profile, config, nets)
    else:
        steps = compose_reconcile_steps(vault, profile, config, prior, nets)
    transitions: List[ReconcileTransition] = []
    for step in steps:
        cfg = config if step.routing == RECONCILE_ROUTING_CURRENT else prior
        if cfg is None:
            raise ValidationError("reconcile remove requires a prior routing")
        projection = prepare_daily_note_projection(
            vault, cfg, step.operation, step.date, slug=step.slug
        )
        transitions.append(
            ReconcileTransition(
                operation=step.operation,
                slug=step.slug,
                date=step.date,
                routing=step.routing,
                target_relative=step.target_relative,
                projection=projection,
            )
        )
    # Issue #144 W2: IN-MEMORY-ONLY full candidate identity/state
    # evidence — every bounded candidate of the prepared snapshot
    # (bootstrap: all of them, unscheduled included) and the
    # current-side state of every slug in an established plan's
    # old/current union. Structural only; never persisted.
    expected_candidates = tuple(sorted(new_states.values(), key=lambda s: s.slug))
    config_commit_needed = False
    if mode == RECONCILE_MODE_RECONCILE and routing_changed:
        config_commit_needed = _config_tracked_and_dirty(vault, git_env)
    return ReconcilePlan(
        mode=mode,
        cursor=cursor,
        prior=prior,
        from_head=from_head,
        to_head=head,
        routing_changed=routing_changed,
        transitions=tuple(transitions),
        net_classes=tuple(sorted((s, n.cls) for s, n in nets.items())),
        expected_candidates=expected_candidates,
        config_commit_needed=config_commit_needed,
    )


def _git_commit_parent_ids(
    vault: Path,
    git_env: Dict[str, str],
    head: str,
) -> Tuple[str, ...]:
    """Resolve the parent SHAs of one commit via fixed-argv plumbing.

    Read-only: ``git rev-list --parents -n 1 <head>`` through the same
    sanitized, streamed, bounded ``_run_git`` plumbing as every other
    core Git call. Raises ``GitError`` on failure and ``CoreError`` if
    the output does not resolve exactly ``head``.
    """
    r = _run_git(vault, git_env, ["rev-list", "--parents", "-n", "1", head])
    if r.returncode != 0:
        raise GitError(f"git rev-list failed: {_redact(r.stderr)[:200]}")
    fields = r.stdout.split()
    if len(fields) < 1 or fields[0] != head:
        raise CoreError("reconcile commit head could not be resolved")
    return tuple(fields[1:])


def _require_reconcile_commit_provenance(
    vault: Path,
    git_env: Dict[str, str],
    head: str,
    expected_parent: str,
) -> None:
    """Fail closed unless ``head`` is a direct child of ``expected_parent``.

    Exactly one parent (non-merge) equal to the retained pre-commit
    expected head. An external commit that arrived before the targeted
    reconciliation commit, or was reordered with it, makes ``head``'s
    parent something else and fails closed (issue #144 Gate 2).
    """
    parents = _git_commit_parent_ids(vault, git_env, head)
    if parents != (expected_parent,):
        raise CoreError(
            "reconcile commit is not the direct child of the expected head"
        )


def _recheck_reconcile_sources(
    vault: Path,
    profile: TaskNotesProfile,
    plan: ReconcilePlan,
    git_env: Dict[str, str],
    *,
    expected_head: str,
    max_files: int,
    max_size: int,
) -> None:
    """Fail closed when the prepared candidate evidence or HEAD drifted.

    Issue #144 W2: replaces the fixed-HEAD/transition-only recheck with
    a full bounded candidate re-enumeration proved against the plan's
    in-memory evidence — an exact set and state match (newly appearing
    candidates, add/remove/tag/schedule/location changes on known ones
    all fail) — plus an expected-HEAD check. The expected head is
    ``plan.to_head`` for the first check and afterwards evolves only to
    the directly resulting current HEAD of this reconciler's own batch
    commits, so any external commit movement fails closed.
    """
    head = git_head_id(vault, git_env)
    if head is None or not is_valid_git_commit_sha(head) or head != expected_head:
        raise CoreError("reconcile source inputs changed before apply")
    current_map = {
        c.slug: c
        for c in enumerate_reconcile_candidates(
            vault, profile, max_files=max_files, max_size=max_size
        )
    }
    evidence = {s.slug: s for s in plan.expected_candidates}
    for slug in current_map:
        if slug not in evidence:
            raise CoreError("reconcile source inputs changed before apply")
    for expected in plan.expected_candidates:
        now = _current_reconcile_task_state(
            vault, profile, expected.slug, current_map, max_size=max_size
        )
        if now != expected:
            raise CoreError("reconcile source inputs changed before apply")


def apply_daily_links_reconciliation(
    vault: Path,
    profile: TaskNotesProfile,
    config: DailyNotesConfig,
    plan: ReconcilePlan,
    git_env: Optional[Dict[str, str]] = None,
    *,
    pending_path: Optional[Path] = None,
    max_files: int = LIST_MAX_FILES,
    max_size: int = LIST_MAX_FILE_SIZE,
    _final_check_hook: Optional[Callable[[], None]] = None,
) -> ReconcileApplyOutcome:
    """Apply a prepared plan: projections, targeted commit, pending record.

    Rechecks task source inputs (and HEAD) first — any churn fails
    closed before a side effect. Applies every pre-prepared projection
    with the existing safe W1b primitives (ensure projections route
    through the current config, removes through the plan's prior
    routing); a persistent projection conflict fails closed. Stages
    ONLY the actually changed daily notes (plus the tracked-and-dirty
    Daily Notes config when the plan needs it) and creates one targeted
    commit — never ``git add -A``; unrelated dirty/staged paths are
    untouched. On success writes the validated pending sibling after
    the commit, structurally pinning the applied Daily Notes routing
    (heads, timestamp, folder, format). The cursor is never advanced
    here.

    Bootstrap plans (missing cursor) may exceed
    ``MAX_DAILY_PROJECTION_TARGETS`` composed ensure transitions (issue
    #144 W1): they are processed in deterministic batches keyed by
    distinct Daily Note target (at most MAX distinct targets per batch,
    same-target transitions never split), each batch applied only via
    :func:`apply_daily_note_projection` and committed on its own with
    only that batch's actually changed targets and no config rider
    (bootstrap never re-homes routing). The outcome aggregates the
    union across batches (changed targets, applied sum, any-commit
    flag, final commit head). Established reconciliation keeps the
    single-commit path with the config rider when the plan needs it.

    Source rechecks (issue #144 W2) are full-candidate: a bounded
    re-enumeration must prove an exact set/state match against the
    plan's in-memory candidate evidence (unscheduled candidates
    included) and HEAD must equal the locally expected head —
    ``plan.to_head`` before the first batch, re-proved before each
    later bootstrap batch, and immediately before publishing the
    single pending. After a batch commit created by this reconciler,
    the expected head evolves only to a proven direct child of the
    retained pre-commit head (exactly one parent equal to it), so an
    external commit arriving before or reordered with the targeted
    reconciliation commit fails closed; and immediately before
    pending construction the current HEAD must still equal the
    locally expected head, which alone is adopted. Any external task
    add/remove/schedule change or unexpected HEAD movement fails
    closed with no pending publication and no cursor completion; batch
    commits already created are kept (no automatic rollback, no
    recovery marker) so an unchanged retry converges.
    """
    if not isinstance(config, DailyNotesConfig):
        raise ValidationError(
            "reconcile apply requires a validated DailyNotesConfig"
        )
    if not isinstance(plan, ReconcilePlan):
        raise ValidationError("reconcile apply requires a prepared plan")
    if git_env is None:
        git_env = _build_git_env()
    _recheck_reconcile_sources(
        vault, profile, plan, git_env,
        expected_head=plan.to_head,
        max_files=max_files, max_size=max_size,
    )
    prior = plan.prior
    changed: List[str] = []
    applied = 0
    commit_created = False
    if plan.mode == RECONCILE_MODE_BOOTSTRAP:
        # Issue #144 W1: bootstrap-only batched apply. Batches are
        # deterministic (see _batch_reconcile_transitions); each batch
        # stays within the per-commit daily-note target bound. Issue
        # #144 W2: every later batch re-proves the full candidate
        # snapshot against a locally evolving expected HEAD, and the
        # single pending is published only after all batches succeeded
        # plus a final recheck.
        expected_head = plan.to_head
        for batch_index, batch in enumerate(
            _batch_reconcile_transitions(plan.transitions)
        ):
            if batch_index > 0:
                _recheck_reconcile_sources(
                    vault, profile, plan, git_env,
                    expected_head=expected_head,
                    max_files=max_files, max_size=max_size,
                )
            batch_changed: List[str] = []
            for transition in batch:
                cfg = (
                    config
                    if transition.routing == RECONCILE_ROUTING_CURRENT
                    else prior
                )
                if cfg is None:
                    raise ValidationError(
                        "reconcile remove requires a prior routing"
                    )
                outcome = apply_daily_note_projection(
                    vault, cfg, transition.projection,
                    _final_check_hook=_final_check_hook,
                )
                if outcome.state == DAILY_PROJECTION_CONFLICT:
                    raise CoreError(
                        "daily note projection conflict during reconcile apply"
                    )
                if outcome.state == DAILY_PROJECTION_APPLIED:
                    applied += 1
                    batch_changed.append(transition.target_relative)
            stage = sorted(set(batch_changed))
            if stage and git_commit_reconcile_targets(
                vault, [vault / rel for rel in stage], git_env
            ):
                commit_created = True
                # Issue #144 Gate 2: the expected head evolves only to
                # the directly resulting current HEAD of this
                # reconciler's own commit — proven, not adopted. The
                # resolved head must be exactly one commit whose single
                # parent is the retained pre-commit expected head; an
                # external commit arriving before (or reordered with)
                # the targeted reconciliation commit fails closed
                # before pending.
                evolved = git_head_id(vault, git_env)
                if evolved is None or not is_valid_git_commit_sha(evolved):
                    raise CoreError("reconcile apply requires a vault HEAD")
                evolved = validate_git_commit_sha(evolved)
                _require_reconcile_commit_provenance(
                    vault, git_env, evolved, expected_head
                )
                expected_head = evolved
            changed.extend(stage)
        # Issue #144 W2: final full-candidate + expected-HEAD recheck
        # immediately before publishing the single pending record.
        _recheck_reconcile_sources(
            vault, profile, plan, git_env,
            expected_head=expected_head,
            max_files=max_files, max_size=max_size,
        )
        # Issue #144 Gate 2: narrow final race gate immediately before
        # pending construction — the current HEAD must still equal the
        # locally expected head, and only that expected head (never a
        # reread arbitrary value) is adopted for the pending.
        head_now = git_head_id(vault, git_env)
        if (
            head_now is None
            or not is_valid_git_commit_sha(head_now)
            or head_now != expected_head
        ):
            raise CoreError("reconcile source inputs changed before apply")
        head_now = expected_head
    else:
        for transition in plan.transitions:
            cfg = (
                config
                if transition.routing == RECONCILE_ROUTING_CURRENT
                else prior
            )
            if cfg is None:
                raise ValidationError("reconcile remove requires a prior routing")
            outcome = apply_daily_note_projection(
                vault, cfg, transition.projection,
                _final_check_hook=_final_check_hook,
            )
            if outcome.state == DAILY_PROJECTION_CONFLICT:
                raise CoreError(
                    "daily note projection conflict during reconcile apply"
                )
            if outcome.state == DAILY_PROJECTION_APPLIED:
                applied += 1
                changed.append(transition.target_relative)
        stage = sorted(set(changed))
        if plan.config_commit_needed and _config_tracked_and_dirty(vault, git_env):
            stage.append(RECONCILE_CONFIG_RELPATH)
            stage.sort()
        if stage:
            commit_created = git_commit_reconcile_targets(
                vault, [vault / rel for rel in stage], git_env
            )
        head_now = git_head_id(vault, git_env)
        if head_now is None:
            raise CoreError("reconcile apply requires a vault HEAD")
        head_now = validate_git_commit_sha(head_now)
    pending = DailyLinksReconcilePending(
        from_head=plan.from_head,
        to_head=head_now,
        started_at=int(time.time()),
        daily_folder=config.folder,
        daily_format=config.format,
    )
    write_daily_links_reconcile_pending(pending, pending_path)
    return ReconcileApplyOutcome(
        applied=applied,
        changed_targets=tuple(sorted(set(changed))),
        commit_created=commit_created,
        commit_id=head_now if commit_created else None,
        pending=pending,
    )


def finalize_daily_links_reconciliation(
    vault: Path,
    config: DailyNotesConfig,
    *,
    sync_succeeded: bool,
    git_env: Optional[Dict[str, str]] = None,
    cursor: Optional[DailyLinksReconcileCursor] = None,
    pending: Optional[DailyLinksReconcilePending] = None,
    cursor_path: Optional[Path] = None,
    pending_path: Optional[Path] = None,
) -> DailyLinksReconcileCursor:
    """Advance the cursor after the caller confirms native sync success.

    Requires the caller's ``sync_succeeded`` signal and verifies the
    pending/head identity (``to_head`` equals the current HEAD, and
    ``from_head`` matches the established cursor when present) and the
    pending's pinned applied routing (folder/format must equal the
    supplied config) before atomically writing the advanced cursor
    (current routing recorded) and clearing the pending sibling. A
    config edit between apply and finalize therefore fails closed with
    the old cursor and pending preserved for replay. One crash window
    is idempotent: when the cursor was already advanced to the
    pending's ``to_head`` (cursor-written, pending-not-cleared) and the
    current HEAD matches, the stale pending is safely cleared and the
    existing cursor returned. All other mismatches fail closed. Never
    touches the recovery marker.
    """
    if not isinstance(sync_succeeded, bool):
        raise ValidationError("sync_succeeded must be a boolean")
    if not sync_succeeded:
        raise CoreError(
            "reconcile finalize requires a successful native sync"
        )
    if not isinstance(config, DailyNotesConfig):
        raise ValidationError(
            "reconcile finalize requires a validated DailyNotesConfig"
        )
    if git_env is None:
        git_env = _build_git_env()
    if cursor is None:
        cursor = load_daily_links_reconcile_cursor(cursor_path)
    if pending is None:
        pending = load_daily_links_reconcile_pending(pending_path)
    if pending is None:
        raise CoreError("reconcile finalize requires a pending record")
    if not isinstance(pending, DailyLinksReconcilePending):
        raise ValidationError(
            "reconcile finalize requires a pending record"
        )
    current = git_head_id(vault, git_env)
    if current is None:
        raise CoreError("reconcile finalize requires a vault HEAD")
    current = validate_git_commit_sha(current)
    if current != pending.to_head:
        raise CoreError("reconcile pending head mismatch")
    if cursor is not None and cursor.reconciled_head == pending.to_head:
        # Crash window: the cursor was already advanced to this
        # pending's head but the pending sibling was not cleared
        # (identity verified above). Clear the stale pending and
        # return the already-advanced cursor.
        clear_daily_links_reconcile_pending(pending_path)
        return cursor
    if cursor is not None and pending.from_head != cursor.reconciled_head:
        raise CoreError("reconcile pending base mismatch")
    if (pending.daily_folder, pending.daily_format) != (
        config.folder,
        config.format,
    ):
        raise CoreError("reconcile pending routing mismatch")
    advanced = DailyLinksReconcileCursor(
        reconciled_head=pending.to_head,
        daily_folder=config.folder,
        daily_format=config.format,
        projection_format=DAILY_LINKS_PROJECTION_FORMAT_VERSION,
    )
    write_daily_links_reconcile_cursor(advanced, cursor_path)
    clear_daily_links_reconcile_pending(pending_path)
    return advanced



# ---------------------------------------------------------------------------
# Mutation result
# ---------------------------------------------------------------------------


@dataclass
class MutationResult:
    """Structured outcome of a mutation operation.

    The ``daily_link_*`` fields (issue #139) are optional Daily Notes
    link bookkeeping. They are populated only for ``create``/``update``/
    ``delete`` operations with the daily-link feature enabled and an
    ``applied_and_committed`` task outcome: ``daily_link_state`` reports
    the projection outcome (``applied_and_committed``, or
    ``not_applicable`` when no transition was required, or ``not_applied``
    when nothing needed to change, or a degraded ``conflict``/
    ``write_failed``/``commit_failed``/``committed_sync_failed``),
    ``daily_link_detail`` carries a generic (content-free) degradation
    detail, and ``daily_link_dates`` lists the affected ``YYYY-MM-DD``
    daily note dates (multiple when a reschedule touches D1 and D2).
    They default to ``None`` and stay ``None`` otherwise: disabled mode,
    operations that never project (complete/archive/add_tag/remove_tag),
    and task outcomes that did not apply and commit.
    """

    state: str
    slug: str
    commit_id: Optional[str] = None
    detail: Optional[str] = None
    daily_link_state: Optional[str] = None
    daily_link_detail: Optional[str] = None
    daily_link_dates: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TaskNotesEngine:
    """Core engine for the seven TaskNotes operations.

    All operations that invoke gbrain (including get) take the shared
    lock. List is lock-free because it never invokes gbrain. Mutations
    verify the profile, verify the gbrain source under the lock, run
    Daily-links reconciliation when enabled (issue #139 W4a, before
    preflight; delete runs its Git-clean target guard first), run
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
        daily_links_enabled: bool = False,
        reconcile_enabled: bool = False,
        reconcile_cursor_path: Optional[Path] = None,
        reconcile_pending_path: Optional[Path] = None,
    ) -> None:
        if not isinstance(daily_links_enabled, bool):
            raise ValidationError("daily_links_enabled must be a boolean")
        if not isinstance(reconcile_enabled, bool):
            raise ValidationError("reconcile_enabled must be a boolean")
        self.vault = Path(vault)
        self.gbrain_bin = gbrain_bin
        self.gbrain_home = Path(gbrain_home)
        self.lock_dir = Path(lock_dir) if lock_dir is not None else DEFAULT_LOCK_DIR
        self.lock_timeout = lock_timeout
        self.lock_path = self.lock_dir / DEFAULT_LOCK_NAME
        self.recovery_marker = self.lock_dir / RECOVERY_MARKER_NAME
        self.tz = tz
        self.daily_links_enabled = daily_links_enabled
        # Daily-links reconciliation (issue #139 W4a) is gated by BOTH
        # the daily-links master switch and the reconciliation switch:
        # with the master disabled it is fully inert (no cursor/pending
        # I/O, no calls). The cursor/pending locations default to the
        # fixed W2a runtime paths; tests inject temporary paths.
        self.reconcile_enabled = reconcile_enabled
        self._reconcile_active = daily_links_enabled and reconcile_enabled
        self.reconcile_cursor_path = (
            Path(reconcile_cursor_path)
            if reconcile_cursor_path is not None
            else DAILY_LINKS_RECONCILE_CURSOR_PATH
        )
        self.reconcile_pending_path = (
            Path(reconcile_pending_path)
            if reconcile_pending_path is not None
            else DAILY_LINKS_RECONCILE_PENDING_PATH
        )
        # Daily Notes config is loaded and validated at most once per
        # projection-bearing operation, before any task side effect, and
        # the immutable snapshot is carried through the post-task
        # apply/commit/sync (no engine-lifetime caching). Never touched
        # while daily_links_enabled is False.
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

    # -- daily link projection (issue #139, W2) ---------------------------

    def _load_daily_config(self) -> DailyNotesConfig:
        """Load and validate the Daily Notes config (no engine caching).

        Called at most once per projection-bearing operation, before any
        task side effect; the returned immutable snapshot is carried
        through the post-task daily apply/commit/sync. Disabled mode
        never calls this. A config edit therefore takes effect on the
        next projection-bearing operation.
        """
        return load_daily_notes_config(self.vault)

    # -- daily-links reconciliation (issue #139, W4a: engine wiring) ------

    def _run_reconciliation(self, profile: TaskNotesProfile) -> DailyNotesConfig:
        """Run the W2 reconciliation lifecycle (shared lock already held).

        prepare -> apply (targeted commit) -> required native gbrain
        sync (source-routed internal CLI path; never the public gbrain
        wrapper, no nested lock, no PGLite access) -> finalize. One
        validated ``DailyNotesConfig`` snapshot is loaded here and that
        same object passes through prepare, apply, and finalize. Any
        failure raises a typed core error that skips all later
        preflight/projection/mutation; cursor/pending semantics are
        preserved for replay and the recovery marker is never touched.
        """
        config = self._load_daily_config()
        plan = prepare_daily_links_reconciliation(
            self.vault, profile, config, self._git_env,
            cursor_path=self.reconcile_cursor_path,
            pending_path=self.reconcile_pending_path,
        )
        apply_daily_links_reconciliation(
            self.vault, profile, config, plan, self._git_env,
            pending_path=self.reconcile_pending_path,
        )
        # Required native sync: finalize may only run after a verified
        # sync, so a sync failure leaves cursor+pending intact for
        # replay and skips the rest of the operation.
        gbrain_sync_incremental(
            self.gbrain_bin, self._gbrain_env, self.vault, profile.source_id  # type: ignore[arg-type]
        )
        finalize_daily_links_reconciliation(
            self.vault, config,
            sync_succeeded=True,
            git_env=self._git_env,
            cursor_path=self.reconcile_cursor_path,
            pending_path=self.reconcile_pending_path,
        )
        return config

    def _maybe_reconcile(self, profile: TaskNotesProfile) -> None:
        """Reconcile Daily Notes links before preflight (lock held).

        Inert unless the daily-links master AND the reconciliation
        switch are both enabled: disabled mode performs no cursor or
        pending I/O and makes no reconciliation calls. When active this
        runs BEFORE the existing Git preflight and before any mutation
        side effect; any failure propagates and skips everything that
        follows.
        """
        if not self._reconcile_active:
            return
        self._run_reconciliation(profile)

    def _prepare_daily_link_projections(
        self,
        profile: TaskNotesProfile,
        config: DailyNotesConfig,
        steps: List[Tuple[str, str]],
        slug: str,
    ) -> List[DailyNoteProjection]:
        """Pre-compute every needed projection target (no side effects).

        Every resolved target is first rejected when it falls inside the
        configured task folder or the active archive folder, so the
        direct writer can never mutate task/archive Markdown. Runs the
        W1b preparation (strict no-follow reads, structural ``## Tasks``
        validation, template rendering for missing notes only) against
        the operation's single validated ``config`` snapshot so a
        deterministic failure raises BEFORE any task or gbrain side
        effect.
        """
        if not isinstance(config, DailyNotesConfig):
            raise ValidationError(
                "daily note projection requires a validated DailyNotesConfig"
            )
        projections: List[DailyNoteProjection] = []
        for operation, date in steps:
            target = resolve_daily_note_path(self.vault, config, date)
            _reject_daily_projection_collision(
                profile, target.relative_to(self.vault).as_posix()
            )
            projections.append(
                prepare_daily_note_projection(
                    self.vault, config, operation, date, slug=slug
                )
            )
        return projections

    def _run_daily_link_projection(
        self,
        profile: TaskNotesProfile,
        config: DailyNotesConfig,
        projections: List[DailyNoteProjection],
    ) -> Tuple[str, Optional[str], Optional[List[str]]]:
        """Apply prepared projections, commit changed targets, sync (lock held).

        Uses the same validated ``config`` snapshot that prevalidation
        loaded before the task side effects. Steps run in plan order
        (ensure before remove); the first failure stops later steps so a
        failed ensure never loses the link. Every projection target is
        re-checked against the task/archive folders immediately before
        the direct write. Changed targets are committed with the W1
        multi-target helper and synced with the native source-scoped
        incremental sync. All failures are content-free and never touch
        the recovery marker. Returns
        ``(daily_link_state, daily_link_detail, daily_link_dates)``.
        """
        dates: List[str] = [projection.date for projection in projections]
        changed: List[Path] = []
        failure: Optional[str] = None
        for projection in projections:
            if failure is not None:
                break
            try:
                # Last-gate collision check: the direct writer must never
                # mutate task/archive Markdown (belt-and-braces behind the
                # prevalidation rejection).
                _reject_daily_projection_collision(
                    profile, projection.target_relative
                )
                outcome = apply_daily_note_projection(
                    self.vault, config, projection
                )
            except CoreError:
                failure = DAILY_LINK_WRITE_FAILED
                continue
            if outcome.state == DAILY_PROJECTION_APPLIED:
                changed.append(self.vault / projection.target_relative)
            elif outcome.state == DAILY_PROJECTION_CONFLICT:
                failure = DAILY_LINK_CONFLICT
            # DAILY_PROJECTION_NOT_APPLIED: nothing to commit for this target.
        if not changed:
            if failure is not None:
                return failure, _DAILY_LINK_FAILURE_DETAIL[failure], dates
            return DAILY_LINK_NOT_APPLIED, None, (dates or None)
        try:
            git_commit_daily_projection_targets(
                self.vault, changed, self._git_env
            )
        except CoreError:
            if failure is not None:
                return (
                    DAILY_LINK_COMMIT_FAILED,
                    "daily note projection partially applied; commit failed",
                    dates,
                )
            return (
                DAILY_LINK_COMMIT_FAILED,
                _DAILY_LINK_FAILURE_DETAIL[DAILY_LINK_COMMIT_FAILED],
                dates,
            )
        try:
            gbrain_sync_incremental(
                self.gbrain_bin, self._gbrain_env, self.vault, profile.source_id  # type: ignore[arg-type]
            )
        except CoreError:
            if failure is not None:
                return (
                    DAILY_LINK_SYNC_FAILED,
                    "daily note projection partially applied; sync failed",
                    dates,
                )
            return (
                DAILY_LINK_SYNC_FAILED,
                _DAILY_LINK_FAILURE_DETAIL[DAILY_LINK_SYNC_FAILED],
                dates,
            )
        if failure is not None:
            return (
                failure,
                "daily note projection partially applied; applied targets committed and synced",
                dates,
            )
        return DAILY_LINK_APPLIED, None, dates

    def _finish_with_daily_links(
        self,
        profile: TaskNotesProfile,
        result: MutationResult,
        projections: Optional[List[DailyNoteProjection]],
        config: Optional[DailyNotesConfig],
    ) -> MutationResult:
        """Attach Daily Notes projection outcomes to a finished mutation.

        Disabled mode returns the task result untouched (no daily fields).
        Projections run ONLY for ``applied_and_committed`` task outcomes;
        any other outcome keeps the task result authoritative and untouched.
        When enabled and no transition is required, reports
        ``not_applicable``. ``config`` is the immutable snapshot loaded
        once per operation before the task side effects; it is carried
        unchanged into the post-task apply/commit/sync.
        """
        if not self.daily_links_enabled:
            return result
        if result.state != APPLIED_AND_COMMITTED:
            return result
        if projections is None:
            result.daily_link_state = DAILY_LINK_NOT_APPLICABLE
            return result
        if config is None:
            raise CoreError(
                "daily link projection config snapshot missing"
            )
        state, detail, dates = self._run_daily_link_projection(
            profile, config, projections
        )
        result.daily_link_state = state
        result.daily_link_detail = detail
        result.daily_link_dates = dates
        return result

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
        planned_week: Optional[str] = None,
    ) -> MutationResult:
        """Create a new task. Rejects completed status and archive tag.

        When ``slug`` is ``None``, a slug is auto-generated from the title
        and current timestamp (``YYYY-MM-DD-HHmmss-slugified-title``).

        Planning target (issue #128): at most one of ``scheduled`` and
        ``planned_week`` may be supplied. ``planned_week`` must be a valid
        ``YYYY-MM-DD`` Monday and requires a profile ``userFields`` entry
        of type ``date``; supplying neither creates a Backlog task.
        """
        # Input validation BEFORE preflight (no side effects).
        if slug is None:
            slug = generate_slug(title, tz=self.tz)
        validate_slug(slug)
        title = validate_title(title)
        body = validate_body(body)
        recurrence_v = validate_recurrence(recurrence) if recurrence is not None else None
        planned_week_v = (
            validate_planned_week(planned_week) if planned_week is not None else None
        )
        if scheduled is not None and planned_week_v is not None:
            raise ValidationError(
                "scheduled and planned_week are mutually exclusive; "
                "choose one planning target"
            )
        with Lock(self.lock_path, timeout=self.lock_timeout):
            self._check_recovery_marker()
            profile = self.load_profile()
            if planned_week_v is not None:
                # Explicit profile prerequisite check before any side effect.
                _require_planned_week_user_field(profile)
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
            # W4a: reconcile Daily Notes links before preflight (lock
            # already held); failure skips all later preflight/mutation.
            self._maybe_reconcile(profile)
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
                custom_fields_v, recurrence_v, planned_week_v,
            )
            _validate_markdown_bound(markdown)
            expected_document = semantic_from_markdown(markdown, profile)
            gbrain_slug = resolve_gbrain_slug(profile, slug)
            # Daily link prevalidation (issue #139 W2): enabled mode with a
            # scheduled create loads and validates the Daily Notes config
            # exactly once, resolves the ensure target, and pre-computes
            # the projection BEFORE any task side effect; a deterministic
            # failure raises here. The snapshot is carried through the
            # post-task apply/commit/sync.
            daily_projections: Optional[List[DailyNoteProjection]] = None
            daily_config: Optional[DailyNotesConfig] = None
            if self.daily_links_enabled and scheduled_v is not None:
                daily_config = self._load_daily_config()
                date_steps = _daily_link_plan(None, scheduled_v)
                steps = _compose_daily_link_plan_by_target(
                    self.vault, daily_config, date_steps or []
                )
                daily_projections = self._prepare_daily_link_projections(
                    profile, daily_config, steps, slug
                )
            # CAPTURE-STARTED BOUNDARY: from here on, always return a MutationResult.
            try:
                capture_result = gbrain_capture(self.gbrain_bin, self._gbrain_env, gbrain_slug, profile.source_id, markdown)  # type: ignore[arg-type]
            except Exception as exc:
                # Capture invocation failed after starting; reconcile.
                return self._reconcile_after_capture_failure(
                    profile, slug, gbrain_slug, exc, expected_document
                )
            result = self._handle_post_capture(
                profile,
                slug,
                gbrain_slug,
                capture_result,
                expected_document=expected_document,
            )
            return self._finish_with_daily_links(
                profile, result, daily_projections, daily_config
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
        planned_week: Optional[str] = None,
        clear_planned_week: bool = False,
    ) -> MutationResult:
        """Update status/priority/dates/projects/custom fields and optionally the body.

        ``body`` semantics: ``None`` (default) leaves the body unchanged;
        ``""`` clears the body; a non-empty string replaces the body
        content. Body edits do not affect the title (title edits remain
        unsupported). No completion transition.

        Planning transitions (issue #128): callers express exactly one
        desired target — ``scheduled=<date>`` sets day scheduling and
        removes ``planned_week``; ``planned_week=<Monday>`` sets week
        planning and removes ``scheduled``; ``clear_scheduled`` with no
        new target removes both (Backlog); ``clear_planned_week`` with no
        new target removes only ``planned_week`` (a manual ``scheduled``
        remains authoritative). Contradictory or redundant combinations
        that compose a set with a clear of the same planning transition
        are rejected before any side effect.
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
        planned_week_v = (
            validate_planned_week(planned_week) if planned_week is not None else None
        )
        projects_v = validate_projects(projects) if projects is not None else None
        for name, value in (
            ("clear_due", clear_due),
            ("clear_scheduled", clear_scheduled),
            ("clear_projects", clear_projects),
            ("clear_planned_week", clear_planned_week),
        ):
            if not isinstance(value, bool):
                raise ValidationError(f"{name} must be a boolean")
        if due is not None and clear_due:
            raise ValidationError("due and clear_due are mutually exclusive")
        if scheduled is not None and clear_scheduled:
            raise ValidationError("scheduled and clear_scheduled are mutually exclusive")
        if projects is not None and clear_projects:
            raise ValidationError("projects and clear_projects are mutually exclusive")
        if planned_week is not None and clear_planned_week:
            raise ValidationError(
                "planned_week and clear_planned_week are mutually exclusive"
            )
        if scheduled is not None and planned_week is not None:
            raise ValidationError(
                "scheduled and planned_week are mutually exclusive; "
                "set one planning target"
            )
        if clear_scheduled and planned_week is not None:
            raise ValidationError(
                "clear_scheduled cannot be combined with planned_week; "
                "set the desired target only"
            )
        if clear_planned_week and scheduled is not None:
            raise ValidationError(
                "clear_planned_week cannot be combined with scheduled; "
                "set the desired target only"
            )
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
            if planned_week_v is not None:
                # Explicit profile prerequisite check before any side effect.
                _require_planned_week_user_field(profile)
            custom_fields_v = validate_custom_fields(custom_fields, profile)
            # Verify gbrain source under lock.
            profile = self._verify_source(profile)
            # Preflight.
            original_hash = profile.profile_hash
            # W4a: reconcile Daily Notes links before preflight (lock
            # already held); failure skips all later preflight/mutation.
            self._maybe_reconcile(profile)
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
            # Actual current scheduling state (denormalized, validated);
            # the sole driver of daily link transitions (issue #139 W2).
            current_scheduled = _daily_scheduled_date(
                current_fm.get(profile.mappings["scheduled"])
            )
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
            # Planning-state transitions (issue #128): setting one target
            # clears the other; explicit clears yield Backlog unless a
            # manual ``scheduled`` exists (scheduled wins). The raw
            # ``planned_week`` key is applied as a custom update below.
            if scheduled_v is not None:
                updates["scheduled"] = scheduled_v
                updates[PLANNED_WEEK_KEY] = None
            elif planned_week_v is not None:
                updates[PLANNED_WEEK_KEY] = planned_week_v
                updates["scheduled"] = None
            elif clear_scheduled:
                updates["scheduled"] = None
                updates[PLANNED_WEEK_KEY] = None
            elif clear_planned_week:
                updates[PLANNED_WEEK_KEY] = None
            if clear_projects:
                updates["projects"] = None
            elif projects_v is not None:
                updates["projects"] = list(projects_v)
            # Custom field updates (None means clear).
            for key, value in custom_fields_v.items():
                updates[key] = value
            if not updates and body_v is None:
                return MutationResult(state=NOT_APPLIED, slug=slug)
            # Daily link plan (issue #139 W2) from the FINAL actual
            # scheduling state (current page state + applied updates,
            # never caller intent). Only updates that actually touch the
            # scheduled field drive a projection; a non-scheduling update
            # projects nothing. The Daily Notes config is loaded and
            # validated exactly once, BEFORE any task side effect, and
            # the plan is composed by resolved target path (R4) so a
            # transition collapsing onto one daily target emits exactly
            # one ensure. Pre-computed before any task side effect; a
            # deterministic failure raises here.
            daily_projections_update: Optional[List[DailyNoteProjection]] = None
            daily_config_update: Optional[DailyNotesConfig] = None
            if self.daily_links_enabled and "scheduled" in updates:
                final_scheduled = _daily_scheduled_date(updates["scheduled"])
                date_steps = _daily_link_plan(current_scheduled, final_scheduled)
                if date_steps is not None:
                    daily_config_update = self._load_daily_config()
                    steps = _compose_daily_link_plan_by_target(
                        self.vault, daily_config_update, date_steps
                    )
                    daily_projections_update = self._prepare_daily_link_projections(
                        profile, daily_config_update, steps, slug
                    )
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
            result = self._handle_post_capture(
                profile,
                slug,
                gbrain_slug,
                capture_result,
                expected_document=expected_document,
            )
            return self._finish_with_daily_links(
                profile, result, daily_projections_update, daily_config_update
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
            # W4a: reconcile Daily Notes links before preflight (lock
            # already held); failure skips all later preflight/mutation.
            self._maybe_reconcile(profile)
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
            # W4a: reconcile Daily Notes links before preflight (lock
            # already held); failure skips all later preflight/mutation.
            self._maybe_reconcile(profile)
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

            # W4a delete exception: the Git-clean guard above runs BEFORE
            # any reconciliation side effect, so a dirty target yields
            # zero reconciliation I/O and calls. Once clean, reconcile
            # other pending changes, then the preflight/delete lifecycle.
            self._maybe_reconcile(profile)

            # Now safe: preflight commits pending unrelated edits and syncs.
            self._preflight(profile)
            profile = self._verify_profile_stable(original_hash)
            profile = self._verify_source(profile)
            # Re-resolve in case preflight changed things.
            gbrain_slug = resolve_gbrain_slug(profile, slug)

            # Daily link prevalidation (issue #139 W2, D13 ordering): read
            # and decode the CURRENT page to retain the actual scheduled
            # state, then load the Daily Notes config exactly once and
            # pre-compute the removal projection — BEFORE the soft-delete
            # gate. A deterministic failure raises here: the task and its
            # daily link stay untouched.
            daily_projections_delete: Optional[List[DailyNoteProjection]] = None
            daily_config_delete: Optional[DailyNotesConfig] = None
            if self.daily_links_enabled:
                page = gbrain_get_page(
                    self.gbrain_bin, self._gbrain_env,
                    gbrain_slug, profile.source_id,  # type: ignore[arg-type]
                )
                decoded_delete = decode_page(page)
                old_scheduled = _daily_scheduled_date(
                    decoded_delete["frontmatter"].get(profile.mappings["scheduled"])
                )
                if old_scheduled is not None:
                    daily_config_delete = self._load_daily_config()
                    date_steps = _daily_link_plan(old_scheduled, None)
                    steps = _compose_daily_link_plan_by_target(
                        self.vault, daily_config_delete, date_steps or []
                    )
                    daily_projections_delete = self._prepare_daily_link_projections(
                        profile,
                        daily_config_delete,
                        steps,
                        slug,
                    )

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

            result = MutationResult(
                state=APPLIED_AND_COMMITTED,
                slug=slug,
                commit_id=commit_id,
            )
            # Daily link removal (issue #139 W2, D13): runs ONLY after the
            # task deletion is verified (page_not_found above). A
            # projection failure degrades the daily result fields while
            # the task outcome stays authoritative; the returned
            # commit_id remains the TASK deletion commit.
            return self._finish_with_daily_links(
                profile, result, daily_projections_delete, daily_config_delete
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
            # W4a: reconcile Daily Notes links before preflight (lock
            # already held); failure skips all later preflight/mutation.
            self._maybe_reconcile(profile)
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
            # W4a: reconcile Daily Notes links before preflight (lock
            # already held); failure skips all later preflight/mutation.
            self._maybe_reconcile(profile)
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
