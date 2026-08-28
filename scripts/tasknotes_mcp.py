#!/usr/bin/env python3
"""Bounded stdio MCP surface for gbrain-backed TaskNotes tasks."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from tasknotes_mcp_core import (
    LIST_MAX_RESULTS,
    CoreError,
    MutationResult,
    TaskNotesEngine,
    ValidationError,
)


LOGGER = logging.getLogger("tasknotes-mcp")

# TaskNotes is a trusted internal native gbrain user: it invokes the private
# non-PATH native CLI directly (transaction-level lock ownership), never the
# public adapter. The path is a fixed constant — no environment escape hatch;
# tests inject a fake binary through the TaskNotesEngine constructor.
TASKNOTES_GBRAIN_NATIVE = "/opt/josemar/libexec/gbrain-native"

# Fixed deployment locations (issue #110): the vault, gbrain state, and the
# shared lock never come from the caller's environment. Non-location
# operational settings (TASKNOTES_LOCK_TIMEOUT, TZ,
# TASKNOTES_DAILY_LINKS_ENABLED) remain env-driven.
TASKNOTES_VAULT = "/opt/data/obsidian"
TASKNOTES_GBRAIN_HOME = "/opt/data"
TASKNOTES_LOCK_DIR = "/opt/data/.locks"

mcp = MCPServer(
    "tasknotes",
    instructions=(
        "Manage TaskNotes tasks through the bounded gbrain-backed lifecycle API. "
        "Task slugs are immutable lowercase identifiers."
    ),
    log_level="WARNING",
)

_ENGINE: Optional[TaskNotesEngine] = None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        # nan/inf/-inf would make the lock wait unbounded or nonsensical.
        raise ValidationError(f"{name} must be a finite number")
    if value <= 0:
        raise ValidationError(f"{name} must be greater than zero")
    return value


def _env_bool(name: str, default: bool) -> bool:
    """Strict boolean env parsing (issue #139): missing or empty disables
    (default); a nonempty value must be case-insensitive 'true'/'false' and
    anything else is rejected at engine init instead of being coerced."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValidationError(f"{name} must be 'true' or 'false'")


def _assert_runtime_identity() -> None:
    """The MCP must run as the hermes runtime user before any native gbrain
    work: the global lock and PGLite must never be touched as root. Refuses
    root execution outright (fail-closed, no env escape hatch); the check is
    a direct effective-UID probe, compatible with the container runtime and
    custom HERMES_UID deployments (any non-root uid is accepted)."""
    if os.geteuid() == 0:
        raise RuntimeError(
            "tasknotes MCP refuses to run as root; start as the hermes runtime user"
        )


def _get_engine() -> TaskNotesEngine:
    global _ENGINE
    if _ENGINE is None:
        _assert_runtime_identity()
        _ENGINE = TaskNotesEngine(
            vault=Path(TASKNOTES_VAULT),
            gbrain_bin=TASKNOTES_GBRAIN_NATIVE,
            gbrain_home=Path(TASKNOTES_GBRAIN_HOME),
            lock_dir=Path(TASKNOTES_LOCK_DIR),
            lock_timeout=_env_float("TASKNOTES_LOCK_TIMEOUT", 10.0),
            tz=os.environ.get("TZ", "UTC"),
            daily_links_enabled=_env_bool("TASKNOTES_DAILY_LINKS_ENABLED", False),
        )
    return _ENGINE


def _mutation_dict(result: MutationResult) -> dict[str, Any]:
    return {key: value for key, value in asdict(result).items() if value is not None}


def _tool_call(operation: str, function: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except CoreError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        # Never include task arguments or content in logs or error responses.
        LOGGER.error("unexpected failure in %s", operation)
        raise ToolError(f"{operation} failed unexpectedly") from exc


def _engine_call(operation: str, method: str, *args: Any, **kwargs: Any) -> Any:
    return _tool_call(
        operation,
        lambda: getattr(_get_engine(), method)(*args, **kwargs),
    )


@mcp.tool(structured_output=True)
def task_create(
    title: str,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    due: Optional[str] = None,
    scheduled: Optional[str] = None,
    projects: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    body: str = "",
    slug: Optional[str] = None,
    custom_fields: Optional[dict[str, Any]] = None,
    recurrence: Optional[str] = None,
    planned_week: Optional[str] = None,
) -> dict[str, Any]:
    """Create one task. When slug is omitted, a timestamp-prefixed slug is auto-generated from the title (e.g. 2026-07-18-143000-buy-groceries). Dates use YYYY-MM-DD. ``recurrence`` is an optional RFC 5545 RRULE string (e.g. FREQ=WEEKLY;BYDAY=MO,WE,FR). ``planned_week`` is optional week-only planning: a YYYY-MM-DD date that must be a Monday. It and ``scheduled`` are mutually exclusive planning targets — supply at most one; neither creates a Backlog task."""
    result = _engine_call(
        "task_create",
        "create",
        slug,
        title,
        status=status,
        priority=priority,
        due=due,
        scheduled=scheduled,
        projects=projects,
        tags=tags,
        body=body,
        custom_fields=custom_fields,
        recurrence=recurrence,
        planned_week=planned_week,
    )
    return _mutation_dict(result)


@mcp.tool(structured_output=True)
def task_get(slug: str) -> dict[str, Any]:
    """Get one task by its immutable lowercase slug."""
    return _engine_call("task_get", "get", slug)


@mcp.tool(structured_output=True)
def task_list(
    max_results: int = 100,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    tag: Optional[str] = None,
    archived: Optional[bool] = None,
) -> list[dict[str, Any]]:
    """List bounded structured task metadata from TaskNotes files. Optional filters (combined with AND logic): ``status`` and ``priority`` match mapped frontmatter values, ``tag`` keeps tasks carrying the tag, ``archived`` (True/False) filters by archive state."""
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ToolError("max_results must be an integer")
    if max_results < 1 or max_results > LIST_MAX_RESULTS:
        raise ToolError(f"max_results must be between 1 and {LIST_MAX_RESULTS}")
    return _engine_call(
        "task_list",
        "list",
        max_results=max_results,
        status=status,
        priority=priority,
        tag=tag,
        archived=archived,
    )


@mcp.tool(structured_output=True)
def task_update(
    slug: str,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    due: Optional[str] = None,
    scheduled: Optional[str] = None,
    projects: Optional[list[str]] = None,
    clear_due: bool = False,
    clear_scheduled: bool = False,
    clear_projects: bool = False,
    custom_fields: Optional[dict[str, Any]] = None,
    body: Optional[str] = None,
    planned_week: Optional[str] = None,
    clear_planned_week: bool = False,
) -> dict[str, Any]:
    """Update status, priority, dates, projects, or body; cannot complete a task. ``body=None`` leaves the body unchanged, ``body=""`` clears it, and a string replaces the body content. Title edits are not supported. Planning targets: ``scheduled`` (YYYY-MM-DD) and ``planned_week`` (YYYY-MM-DD Monday, week-only planning) are mutually exclusive — set exactly one to select that state; setting either clears the other. ``clear_scheduled`` removes both planning fields (Backlog); ``clear_planned_week`` removes only the week-only plan."""
    result = _engine_call(
        "task_update",
        "update",
        slug,
        status=status,
        priority=priority,
        due=due,
        scheduled=scheduled,
        projects=projects,
        clear_due=clear_due,
        clear_scheduled=clear_scheduled,
        clear_projects=clear_projects,
        custom_fields=custom_fields,
        body=body,
        planned_week=planned_week,
        clear_planned_week=clear_planned_week,
    )
    return _mutation_dict(result)


@mcp.tool(structured_output=True)
def task_complete(
    slug: str,
    completion_date: Optional[str] = None,
) -> dict[str, Any]:
    """Complete one task, optionally with an explicit YYYY-MM-DD date."""
    result = _engine_call(
        "task_complete",
        "complete",
        slug,
        completion_date=completion_date,
    )
    return _mutation_dict(result)


@mcp.tool(structured_output=True)
def task_archive(slug: str) -> dict[str, Any]:
    """Add the configured archive tag to one task idempotently."""
    result = _engine_call("task_archive", "archive", slug)
    return _mutation_dict(result)


@mcp.tool(structured_output=True)
def task_delete(slug: str) -> dict[str, Any]:
    """Delete one task: gbrain soft-delete confirmation gate, git rm to remove the file from disk, git commit."""
    result = _engine_call("task_delete", "delete", slug)
    return _mutation_dict(result)


@mcp.tool(structured_output=True)
def task_add_tag(slug: str, tag: str) -> dict[str, Any]:
    """Add a custom tag to one task idempotently. Rejects the task-identification and archive tags."""
    result = _engine_call("task_add_tag", "add_tag", slug, tag)
    return _mutation_dict(result)


@mcp.tool(structured_output=True)
def task_remove_tag(slug: str, tag: str) -> dict[str, Any]:
    """Remove a custom tag from one task idempotently. Rejects the task-identification and archive tags."""
    result = _engine_call("task_remove_tag", "remove_tag", slug, tag)
    return _mutation_dict(result)


def main() -> None:
    """Run the server over stdio. Stdout is reserved for MCP traffic."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
