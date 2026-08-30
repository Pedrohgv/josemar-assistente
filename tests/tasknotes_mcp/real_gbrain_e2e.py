#!/usr/bin/env python3
"""Disposable built-image TaskNotes MCP lifecycle smoke test.

This script may ONLY run inside the opt-in Docker harness
(tests/tasknotes_mcp/test_docker_runtime.py). It proves the harness
container before touching anything: the fixed image interpreter, the
read-only harness mount path of this script, Docker-native evidence
(/.dockerenv or container cgroup), the bind-mounted disposable /opt/data, a
fresh (empty) mount, a read-only script mount, and an exact match of the
runtime identity with the validated Hermes UID/GID. There is deliberately NO
host-execution, environment, or executable-override escape hatch: host runs
and caller-provided bypasses are refused.

Two lifecycle phases run against the same disposable vault (issue #139 W4):

1. Disabled mode (explicit ``TASKNOTES_DAILY_LINKS_ENABLED=false``): the
   plain task lifecycle runs BEFORE any Daily Notes configuration exists,
   proving no configuration prerequisite and no ``daily_link_*`` result
   fields. The master flag is passed explicitly as ``false`` because the
   runtime treats a missing flag as enabled; omitting it would silently run
   this phase in enabled mode.
2. Daily-links mode (``TASKNOTES_DAILY_LINKS_ENABLED=true``): after a
   fixture (numeric-subfolder ``daily-notes.json``, a template with empty
   ``date``/``title`` identity, and one preexisting Daily Note) the real
   MCP projects scheduled creates, D1->D2 reschedules, week/backlog
   cleanups, delete cleanup, and completion/archive link retention into the
   Daily Notes, commits them as content-free Git commits, syncs them under
   the shared lock, and makes them visible to the real gbrain index.
3. External-edit refresh reconciliation (issue #139 revision 3 W3): an
   established scheduled task with its bare canonical Daily Note link is
   rescheduled externally (direct task-file edit + commit, no MCP mutation);
   ``josemar-gbrain refresh`` then runs the approved W3 lane under the
   runtime lock (reconcile CLI prepare/apply/targeted commit, native
   committed incremental sync, then finalize). The old link is gone, the
   new link appears exactly once, the task stays gbrain-visible through the
   required committed incremental sync, and the cursor advances to the new
   HEAD with no pending sibling.

The script never deletes anything under /opt/data: the outer host test
fixture owns the fresh temporary directory and removes it after the
container exits; the Daily Notes fixture is only ever written, never
removed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn


# Fixed deployment contract (issue #110): the vault, gbrain state, and the
# shared lock are pinned constants in the MCP — never environment-driven.
# The Docker harness provisions a fresh disposable mount at exactly these
# paths, so a path-contract drift in the built image fails this test instead
# of silently running against a different location.
VAULT = Path("/opt/data/obsidian")
GBRAIN_HOME = Path("/opt/data")
LOCK_DIR = Path("/opt/data/.locks")

# The harness container is the only allowed execution environment: this
# script runs from the read-only harness mount under the image's own venv
# interpreter. Neither may be overridden.
HARNESS_SCRIPT_MOUNT = Path("/tmp/real_gbrain_e2e.py")
HARNESS_INTERPRETER = "/opt/hermes/.venv/bin/python3"

# TaskNotes invokes the private native gbrain CLI at a fixed path; the
# harness pins the same fixed constant — no executable override.
GBRAIN_NATIVE = "/opt/josemar/libexec/gbrain-native"

# Issue #139 revision 3 W3: the approved refresh/reconciliation path is the
# fixed-purpose reconcile CLI installed into the built image and invoked
# through the ``josemar-gbrain refresh`` wrapper under the runtime lock. The
# wrapper's production constants all resolve to the harness mount at
# /opt/data, so invoking the real wrapper here IS the production path (the
# image ships the W3 CLI next to the TaskNotes core; its absence fails the
# refresh, which is exactly the Docker contract we are proving).
GBRAIN_WRAPPER = "/usr/local/bin/josemar-gbrain"

# Fixed reconcile cursor/pending state under /opt/data/.gbrain (structural
# metadata only; never vault content). These alias the core's W2a fixed
# paths so the harness proves the exact finalize state the engine leaves.
RECONCILE_CURSOR_PATH = Path(
    "/opt/data/.gbrain/josemar-tasknotes-daily-links-reconcile.json"
)
RECONCILE_PENDING_PATH = Path(
    "/opt/data/.gbrain/josemar-tasknotes-daily-links-reconcile-pending.json"
)

# Hard upper bound for uid/gid sanity validation (2**32 - 2); 0 (root) is
# always rejected because the MCP and the harness refuse root execution.
MAX_RUNTIME_ID = (1 << 32) - 2

MANIFEST = {
    "id": "tasknotes",
    "name": "TaskNotes",
    "version": "4.11.1",
    "minAppVersion": "1.12.2",
    "description": "Note-based task management.",
    "author": "Example",
    "isDesktopOnly": False,
}

PROFILE = {
    "tasksFolder": "tasks",
    "moveArchivedTasks": False,
    "archiveFolder": "tasks/archive",
    "taskTag": "task",
    "taskIdentificationMethod": "tag",
    "defaultTaskPriority": "normal",
    "defaultTaskStatus": "open",
    "taskFilenameFormat": "zettel",
    "storeTitleInFilename": False,
    "customFilenameTemplate": "{{title}}",
    "fieldMapping": {
        "title": "title",
        "status": "status",
        "priority": "priority",
        "due": "due",
        "scheduled": "scheduled",
        "projects": "projects",
        "completedDate": "completedDate",
        "archiveTag": "archived",
    },
    "customStatuses": [
        {"id": "open", "value": "open", "isCompleted": False},
        {"id": "in-progress", "value": "in-progress", "isCompleted": False},
        {"id": "done", "value": "done", "isCompleted": True},
    ],
    "customPriorities": [
        {"id": "low", "value": "low"},
        {"id": "normal", "value": "normal"},
        {"id": "high", "value": "high"},
    ],
    "userFields": [
        {"id": "pipeline_stage", "key": "pipeline_stage", "type": "text", "label": "Pipeline Stage"},
        {"id": "planned_week", "key": "planned_week", "type": "date", "label": "Planned week"},
    ],
}

# ---------------------------------------------------------------------------
# Daily Notes fixture (issue #139 W4): custom numeric-subfolder configuration,
# a template with normal headings and empty date/title identity, and one
# preexisting Daily Note. Written once between the two lifecycle phases;
# never removed (the e2e never deletes anything under /opt/data).
# ---------------------------------------------------------------------------

DAILY_NOTES_CONFIG_NAME = "daily-notes.json"

# Custom folder plus a numeric hierarchy format: notes resolve to
# ``daily/YYYY/MM/<formatted date>.md``, exercising multi-level numeric
# subfolder creation instead of the flat default.
DAILY_FOLDER = "daily"
DAILY_FORMAT = "YYYY/MM/DD"
DAILY_TEMPLATE_REL = "templates/daily-template.md"

DAILY_NOTES_CONFIG = {
    "folder": DAILY_FOLDER,
    "format": DAILY_FORMAT,
    "template": DAILY_TEMPLATE_REL,
}

# Valid template: normal headings, exactly one ``## Tasks`` section, and
# empty (null) top-level ``date``/``title`` identity that the projection
# must fill (scheduled date / filename stem).
DAILY_TEMPLATE_TEXT = """---
date:
title:
---
# {{date}}

Plan for the day.

## Tasks

## Notes

- Template rendered on {{date}} for {{title}}.
"""

# Preexisting human-authored Daily Note for 2026-07-19 with non-empty
# date/title identity (must be preserved verbatim), an unrelated preexisting
# link (must survive projections), and exactly one ``## Tasks`` section.
PREEXISTING_DAILY_NOTE_DATE = "2026-07-19"
PREEXISTING_DAILY_NOTE = """---
date: 2026-07-19
title: July 19 planning
---
# 2026-07-19

Morning plan.

## Tasks

- [[20260718t090000|Preexisting unrelated link]]

## Notes

Human-authored content that must survive projections.
"""


def _refuse(reason: str) -> NoReturn:
    raise RuntimeError(f"tasknotes runtime harness refuses to run: {reason}")


def _validated_id(name: str) -> int:
    """Only a validated positive non-root uid/gid is accepted as the
    expected identity; anything else (missing, garbage, 0, out of range) is
    refused."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        _refuse(f"missing required environment variable {name}")
    try:
        value = int(raw)
    except ValueError as exc:
        _refuse(f"{name} must be an integer, got {raw!r}")
    if not 1 <= value <= MAX_RUNTIME_ID:
        _refuse(f"{name} must be a valid non-root uid/gid (1..{MAX_RUNTIME_ID}), got {value}")
    return value


def _is_disposable_mount() -> bool:
    try:
        return os.path.ismount(GBRAIN_HOME)
    except OSError:
        return False


def _has_docker_native_evidence() -> bool:
    """Docker-native container evidence, never env-driven: the daemon places
    /.dockerenv in the container root, or the cgroup path names the docker
    container (cgroup v1)."""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "docker" in cgroup


def _harness_script_is_readonly() -> bool:
    """True when the harness script mount is read-only. The /proc/self/mounts
    entry must carry the ro option; otherwise an effective write attempt
    (open-for-append without writing, which cannot alter the source) must
    fail. Environment variables are never consulted."""
    try:
        mounts = Path("/proc/self/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        mounts = ""
    target = str(HARNESS_SCRIPT_MOUNT)
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == target:
            return "ro" in fields[3].split(",")
    try:
        with open(HARNESS_SCRIPT_MOUNT, "a"):
            pass
    except OSError:
        return True
    return False


def _prove_docker_harness(expected_uid: int, expected_gid: int) -> None:
    """Refuse unless every signal of the disposable Docker harness holds:
    harness interpreter, harness script mount, Docker-native evidence, a
    bind-mounted /opt/data, a fresh (empty) mount, a read-only script mount,
    and an exact match of the runtime identity with the validated Hermes
    UID/GID. No signal is caller-controllable to a degree that would permit
    a host run."""
    if sys.executable != HARNESS_INTERPRETER:
        _refuse(
            f"not running under the harness interpreter {HARNESS_INTERPRETER} "
            f"(got {sys.executable!r})"
        )
    script_path = Path(__file__).resolve()
    if script_path != HARNESS_SCRIPT_MOUNT:
        _refuse(
            f"not running from the harness mount {HARNESS_SCRIPT_MOUNT} "
            f"(got {script_path!r})"
        )
    if not _has_docker_native_evidence():
        _refuse(
            "no Docker-native evidence (/.dockerenv or container cgroup); "
            "this script only runs inside the Docker harness"
        )
    if not _is_disposable_mount():
        _refuse(f"{GBRAIN_HOME} is not a mountpoint; refusing to touch a host directory")
    try:
        entries = list(GBRAIN_HOME.iterdir())
    except OSError as exc:
        _refuse(f"cannot inspect {GBRAIN_HOME}: {exc}")
    if entries:
        _refuse(
            f"{GBRAIN_HOME} is not a fresh disposable mount "
            f"({len(entries)} entries present)"
        )
    if not _harness_script_is_readonly():
        _refuse(f"harness script mount {HARNESS_SCRIPT_MOUNT} is not read-only")
    if os.geteuid() != expected_uid or os.getegid() != expected_gid:
        _refuse(
            f"identity mismatch: running as {os.geteuid()}:{os.getegid()}, "
            f"expected Hermes runtime user {expected_uid}:{expected_gid}"
        )


def _recheck_mount_safety() -> None:
    """Explicit recheck immediately before the harness starts mutating
    /opt/data: the disposable mount must still be in place."""
    if not _is_disposable_mount():
        _refuse(f"{GBRAIN_HOME} is no longer a mountpoint; aborting before any mutation")


def run(command: list[str], *, env: dict[str, str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {command!r}\n"
            f"stdout={completed.stdout[-2000:]}\nstderr={completed.stderr[-2000:]}"
        )
    return completed.stdout


def prepare() -> tuple[Path, dict[str, str]]:
    _recheck_mount_safety()
    vault = VAULT
    plugin = vault / ".obsidian" / "plugins" / "tasknotes"
    tasks = vault / "tasks"
    for directory in (plugin, tasks):
        directory.mkdir(parents=True, exist_ok=True)
    (plugin / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (plugin / "data.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    (vault / ".placeholder").write_text("disposable vault\n", encoding="utf-8")

    env = {
        "HOME": str(GBRAIN_HOME),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "GBRAIN_HOME": str(GBRAIN_HOME),
        "GBRAIN_BRAIN_REPO": str(vault),
        "GBRAIN_SKIP_STARTUP_HOOKS": "1",
        "TELEGRAM_BOT_TOKEN": "",
        "PRIMARY_TELEGRAM_ID": "",
        "TELEGRAM_ALLOWED_USERS": "",
        "TELEGRAM_HOME_CHANNEL": "",
        "GATEWAY_ALLOWED_USERS": "",
        "ZAI_API_KEY": "",
        "GLM_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "OLLAMA_API_KEY": "",
        "TAVILY_API_KEY": "",
    }

    run(["git", "init", "-q", "-b", "main"], env=env, cwd=vault)
    run(["git", "config", "user.name", "Disposable Test"], env=env, cwd=vault)
    run(["git", "config", "user.email", "test" + "@example.invalid"], env=env, cwd=vault)
    run(["git", "add", "-A"], env=env, cwd=vault)
    run(["git", "commit", "-q", "-m", "Initialize disposable vault"], env=env, cwd=vault)

    run([GBRAIN_NATIVE, "init", "--pglite", "--no-embedding"], env=env)
    run([GBRAIN_NATIVE, "config", "set", "sync.repo_path", str(vault)], env=env)
    run(
        [
            GBRAIN_NATIVE,
            "sync",
            "--full",
            "--no-embed",
            "--yes",
            "--no-pull",
            "--json",
            "--repo",
            str(vault),
        ],
        env=env,
    )
    sources = json.loads(run([GBRAIN_NATIVE, "sources", "list", "--json"], env=env))
    matching = [source for source in sources["sources"] if source.get("local_path") == str(vault)]
    if len(matching) != 1:
        raise AssertionError(f"expected one matching source, got {matching!r}")
    # Test-level limitation: source routing is pinned to the single default
    # source id here. Asserting a non-default source would require a second
    # isolated gbrain source/vault, which this disposable harness does not
    # provision. The exact argv source-routing contract (``--source <id>``)
    # is instead pinned by the focused unit test
    # ``test_capture_routes_with_source`` in test_core.py.
    return vault, env


def _result_is_error(result: Any) -> bool:
    """Narrow MCP SDK compatibility accessor for the error flag.

    The pinned runtime SDK (mcp==2.0.0, mirroring the Hermes base image)
    names the field ``is_error``; older SDK generations used ``isError``.
    The installed (current) field wins when present; the legacy field is
    honored as a fallback so the harness survives a base-image pin bump in
    either direction. This only reads the result envelope — task semantics
    are untouched.
    """
    if hasattr(result, "is_error"):
        return bool(result.is_error)
    if hasattr(result, "isError"):
        return bool(result.isError)
    raise AttributeError(
        f"CallToolResult exposes neither is_error nor isError "
        f"(got {type(result).__name__})"
    )


def _result_structured_content(result: Any) -> dict | None:
    """Narrow MCP SDK compatibility accessor for the structured content.

    Mirrors ``_result_is_error``: mcp==2.0.0 names the field
    ``structured_content``; older SDK generations used ``structuredContent``.
    """
    if hasattr(result, "structured_content"):
        return result.structured_content
    if hasattr(result, "structuredContent"):
        return result.structuredContent
    raise AttributeError(
        f"CallToolResult exposes neither structured_content nor "
        f"structuredContent (got {type(result).__name__})"
    )


async def call(session: Any, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    if _result_is_error(result):
        rendered = " ".join(getattr(item, "text", repr(item)) for item in result.content)
        raise AssertionError(f"{name} returned MCP error: {rendered}")
    structured = _result_structured_content(result)
    assert structured is not None, name
    return structured


async def lifecycle(vault: Path, env: dict[str, str]) -> None:
    # Disabled-mode subcase (issue #139): the master flag is passed
    # explicitly as "false" — the runtime treats a MISSING flag as enabled,
    # so omission would silently run this phase in enabled mode — and no
    # Daily Notes configuration exists yet: the plain lifecycle must work
    # with no configuration prerequisite and no daily_link_* result fields.
    assert not (vault / ".obsidian" / DAILY_NOTES_CONFIG_NAME).exists()
    assert env.get("TASKNOTES_DAILY_LINKS_ENABLED") == "false", env

    # The MCP client lives in the image venv; it is imported lazily so the
    # harness-proof guards above can run even on hosts without the package.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command="/opt/hermes/.venv/bin/python3",
        args=["/opt/josemar/scripts/tasknotes_mcp.py"],
        env=env,
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = [tool.name for tool in listed.tools]
            assert names == [
                "task_create",
                "task_get",
                "task_list",
                "task_update",
                "task_complete",
                "task_archive",
                "task_delete",
                "task_add_tag",
                "task_remove_tag",
            ], names

            created = await call(
                session,
                "task_create",
                {
                    "slug": "20260719t120000",
                    "title": "Disposable lifecycle task",
                    "status": "open",
                    "priority": "high",
                    "due": "2026-07-20",
                    "projects": ["validation"],
                    "tags": ["smoke"],
                    "body": "Disposable body preserved by gbrain.",
                    "custom_fields": {"pipeline_stage": "discovered"},
                },
            )
            assert created["state"] == "applied_and_committed", created
            # Disabled mode reports no daily link bookkeeping at all.
            for daily_key in (
                "daily_link_state",
                "daily_link_detail",
                "daily_link_dates",
            ):
                assert daily_key not in created, (daily_key, created)

            fetched = await call(session, "task_get", {"slug": "20260719t120000"})
            assert fetched["title"] == "Disposable lifecycle task", fetched
            assert fetched["status"] == "open", fetched
            assert fetched["priority"] == "high", fetched
            # Body preservation is asserted immediately after create, not
            # only at the end of the lifecycle, so a capture write-through
            # regression that drops the body is caught at the earliest point.
            assert fetched["body"].strip() == "Disposable body preserved by gbrain.", fetched

            tasks = await call(session, "task_list", {"max_results": 10})
            assert [item["slug"] for item in tasks["result"]] == ["20260719t120000"], tasks

            tagged = await call(
                session,
                "task_add_tag",
                {"slug": "20260719t120000", "tag": "followup"},
            )
            assert tagged["state"] == "applied_and_committed", tagged

            updated = await call(
                session,
                "task_update",
                {
                    "slug": "20260719t120000",
                    "status": "in-progress",
                    "priority": "normal",
                    "scheduled": "2026-07-19",
                    "projects": ["validation", "mcp"],
                    "clear_due": True,
                    "custom_fields": {"pipeline_stage": "negotiating"},
                },
            )
            assert updated["state"] == "applied_and_committed", updated
            for daily_key in (
                "daily_link_state",
                "daily_link_detail",
                "daily_link_dates",
            ):
                assert daily_key not in updated, (daily_key, updated)

            untagged = await call(
                session,
                "task_remove_tag",
                {"slug": "20260719t120000", "tag": "followup"},
            )
            assert untagged["state"] == "applied_and_committed", untagged

            completed = await call(
                session,
                "task_complete",
                {"slug": "20260719t120000", "completion_date": "2026-07-19"},
            )
            assert completed["state"] == "applied_and_committed", completed

            archived = await call(session, "task_archive", {"slug": "20260719t120000"})
            assert archived["state"] == "applied_and_committed", archived

            final = await call(session, "task_get", {"slug": "20260719t120000"})
            assert final["status"] == "done", final
            assert "archived" in final["tags"], final
            assert "followup" not in final["tags"], final
            assert final["body"].strip() == "Disposable body preserved by gbrain.", final

            # Week-planning lifecycle (issue #128): bounded real-MCP proof of
            # the three planning states and the scheduled/planned_week
            # invariant on a second disposable task.
            monday = "2026-07-20"  # verified Monday (ISO week start)
            week_created = await call(
                session,
                "task_create",
                {
                    "slug": "20260720t090000",
                    "title": "Disposable week-planned task",
                    "planned_week": monday,
                    "body": "Week-only plan.",
                },
            )
            assert week_created["state"] == "applied_and_committed", week_created
            for daily_key in (
                "daily_link_state",
                "daily_link_detail",
                "daily_link_dates",
            ):
                assert daily_key not in week_created, (daily_key, week_created)

            week_fetched = await call(session, "task_get", {"slug": "20260720t090000"})
            assert str(week_fetched.get("planned_week"))[:10] == monday, week_fetched
            assert "scheduled" not in week_fetched, week_fetched

            listed = await call(session, "task_list", {"max_results": 10})
            by_slug = {item["slug"]: item for item in listed["result"]}
            assert set(by_slug) == {"20260719t120000", "20260720t090000"}, listed
            assert str(by_slug["20260720t090000"].get("planned_week")) == monday, listed

            # Invariant: setting a day schedule clears the week plan.
            rescheduled = await call(
                session,
                "task_update",
                {"slug": "20260720t090000", "scheduled": "2026-07-21"},
            )
            assert rescheduled["state"] == "applied_and_committed", rescheduled
            refetched = await call(session, "task_get", {"slug": "20260720t090000"})
            assert str(refetched["scheduled"])[:10] == "2026-07-21", refetched
            assert "planned_week" not in refetched, refetched

    clean = run(["git", "status", "--porcelain"], env=env, cwd=vault)
    assert clean.strip() == "", clean
    log = run(["git", "log", "--oneline"], env=env, cwd=vault)
    # 6 mutations for the first task (create, add/remove tag, update,
    # complete, archive) plus the week-planning create and reschedule.
    assert log.count("tasknotes-mcp: task update") == 8, log
    task_text = (vault / "tasks" / "20260719t120000.md").read_text(encoding="utf-8")
    assert "archived" in task_text
    assert "pipeline_stage" in task_text
    assert "Disposable body preserved by gbrain." in task_text
    week_text = (vault / "tasks" / "20260720t090000.md").read_text(encoding="utf-8")
    assert "scheduled" in week_text
    assert "planned_week" not in week_text


def install_daily_notes_fixture(vault: Path, env: dict[str, str]) -> None:
    """Write the Daily Notes fixture and commit it (writes only, no deletion).

    Installs the custom numeric-subfolder ``daily-notes.json``, the template
    with normal headings and empty ``date``/``title`` identity, and one
    preexisting human-authored Daily Note. Committing the fixture keeps the
    daily-links phase starting from a clean tree so the phase's Git
    accounting below stays deterministic (no preflight commits).
    """
    (vault / ".obsidian" / DAILY_NOTES_CONFIG_NAME).write_text(
        json.dumps(DAILY_NOTES_CONFIG), encoding="utf-8"
    )
    template_path = vault / DAILY_TEMPLATE_REL
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(DAILY_TEMPLATE_TEXT, encoding="utf-8")
    preexisting_path = _daily_note_path(PREEXISTING_DAILY_NOTE_DATE)
    preexisting_path.parent.mkdir(parents=True, exist_ok=True)
    preexisting_path.write_text(PREEXISTING_DAILY_NOTE, encoding="utf-8")
    run(["git", "add", "-A"], env=env, cwd=vault)
    run(["git", "commit", "-q", "-m", "Add Daily Notes fixture"], env=env, cwd=vault)


def _daily_note_path(date: str) -> Path:
    """Resolve a daily note path for the fixture's numeric-subfolder format.

    ``YYYY/MM/DD`` renders the whole date as the relative path, so
    2026-07-19 resolves to ``daily/2026/07/19.md`` (DD is zero-padded).
    """
    year, month, day = date.split("-")
    return VAULT / DAILY_FOLDER / year / month / f"{int(day):02d}.md"


def _read_daily_note(date: str) -> str:
    return _daily_note_path(date).read_text(encoding="utf-8")


def _frontmatter_block(text: str) -> str:
    """Return the raw YAML frontmatter block (without fences) of a note."""
    if not text.startswith("---\n"):
        return ""
    end = text.index("\n---\n", 4)
    return text[4:end]


def _assert_no_daily_link(note_text: str, slug: str) -> None:
    assert f"[[{slug}" not in note_text, note_text


async def daily_links_lifecycle(vault: Path, env: dict[str, str]) -> None:
    """Real-MCP daily-links phase (issue #139): enabled mode end to end."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command="/opt/hermes/.venv/bin/python3",
        args=["/opt/josemar/scripts/tasknotes_mcp.py"],
        env=env,
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            # -- alpha: scheduled create onto the PREEXISTING note --------
            alpha = "20260721t100000"
            alpha_title = "Disposable daily-link task alpha"
            alpha_created = await call(
                session,
                "task_create",
                {
                    "slug": alpha,
                    "title": alpha_title,
                    "scheduled": PREEXISTING_DAILY_NOTE_DATE,
                    "body": "Alpha body.",
                },
            )
            assert alpha_created["state"] == "applied_and_committed", alpha_created
            assert (
                alpha_created["daily_link_state"] == "applied_and_committed"
            ), alpha_created
            assert alpha_created["daily_link_dates"] == [
                PREEXISTING_DAILY_NOTE_DATE
            ], alpha_created
            assert "daily_link_detail" not in alpha_created, alpha_created

            # Idempotent unchanged reschedule: link already canonical ->
            # not_applied, no duplicate, no projection commit.
            alpha_kept = await call(
                session,
                "task_update",
                {"slug": alpha, "scheduled": PREEXISTING_DAILY_NOTE_DATE},
            )
            assert alpha_kept["state"] == "applied_and_committed", alpha_kept
            assert alpha_kept["daily_link_state"] == "not_applied", alpha_kept
            assert alpha_kept["daily_link_dates"] == [
                PREEXISTING_DAILY_NOTE_DATE
            ], alpha_kept

            # Completion/archive retain the schedule link (no daily fields).
            alpha_done = await call(
                session,
                "task_complete",
                {"slug": alpha, "completion_date": "2026-07-21"},
            )
            assert alpha_done["state"] == "applied_and_committed", alpha_done
            assert "daily_link_state" not in alpha_done, alpha_done
            alpha_archived = await call(session, "task_archive", {"slug": alpha})
            assert alpha_archived["state"] == "applied_and_committed", alpha_archived
            assert "daily_link_state" not in alpha_archived, alpha_archived

            note_19 = _read_daily_note(PREEXISTING_DAILY_NOTE_DATE)
            assert f"- [[{alpha}]]" in note_19, note_19
            # Non-empty identity and unrelated content preserved verbatim.
            assert "date: 2026-07-19" in note_19, note_19
            assert "title: July 19 planning" in note_19, note_19
            assert "- [[20260718t090000|Preexisting unrelated link]]" in note_19, note_19
            assert (
                "Human-authored content that must survive projections." in note_19
            ), note_19

            # -- beta: template-created note, D1->D2 reschedule, week plan --
            beta = "20260722t110000"
            beta_title = "Disposable daily-link task beta"
            beta_created = await call(
                session,
                "task_create",
                {
                    "slug": beta,
                    "title": beta_title,
                    "scheduled": "2026-07-20",
                },
            )
            assert beta_created["state"] == "applied_and_committed", beta_created
            assert (
                beta_created["daily_link_state"] == "applied_and_committed"
            ), beta_created
            assert beta_created["daily_link_dates"] == ["2026-07-20"], beta_created

            note_20 = _read_daily_note("2026-07-20")
            # Template-rendered headings and filled empty date/title identity.
            # The title identity is the filename stem ("20" under the
            # numeric YYYY/MM/DD format), not the full date.
            assert "# 2026-07-20" in note_20, note_20
            assert "## Tasks" in note_20 and "## Notes" in note_20, note_20
            fm_20 = _frontmatter_block(note_20)
            stem_20 = re.escape(_daily_note_path("2026-07-20").stem)
            assert re.search(rf"(?m)^date: '?2026-07-20'?$", fm_20), fm_20
            assert re.search(rf"(?m)^title: '?{stem_20}'?$", fm_20), fm_20
            assert f"- [[{beta}]]" in note_20, note_20

            # D1 -> D2 reschedule: the new date's link is ensured before the
            # old one is removed (dates report the plan order).
            rescheduled = await call(
                session,
                "task_update",
                {"slug": beta, "scheduled": "2026-07-21"},
            )
            assert rescheduled["state"] == "applied_and_committed", rescheduled
            assert (
                rescheduled["daily_link_state"] == "applied_and_committed"
            ), rescheduled
            assert rescheduled["daily_link_dates"] == ["2026-07-21", "2026-07-20"], (
                rescheduled
            )
            _assert_no_daily_link(_read_daily_note("2026-07-20"), beta)
            note_21 = _read_daily_note("2026-07-21")
            assert f"- [[{beta}]]" in note_21, note_21

            # D2 -> planned_week cleanup: link removed, note retained.
            monday = "2026-07-27"  # verified Monday (ISO week start)
            week_planned = await call(
                session,
                "task_update",
                {"slug": beta, "planned_week": monday},
            )
            assert week_planned["state"] == "applied_and_committed", week_planned
            assert (
                week_planned["daily_link_state"] == "applied_and_committed"
            ), week_planned
            assert week_planned["daily_link_dates"] == ["2026-07-21"], week_planned
            beta_after = await call(session, "task_get", {"slug": beta})
            assert str(beta_after.get("planned_week"))[:10] == monday, beta_after
            assert "scheduled" not in beta_after, beta_after
            _assert_no_daily_link(_read_daily_note("2026-07-21"), beta)
            assert _daily_note_path("2026-07-21").is_file()

            # -- gamma: backlog cleanup and delete cleanup ------------------
            gamma = "20260723t120000"
            gamma_title = "Disposable daily-link task gamma"
            gamma_created = await call(
                session,
                "task_create",
                {
                    "slug": gamma,
                    "title": gamma_title,
                    "scheduled": "2026-07-22",
                },
            )
            assert gamma_created["state"] == "applied_and_committed", gamma_created
            assert gamma_created["daily_link_dates"] == ["2026-07-22"], gamma_created

            # scheduled -> Backlog cleanup.
            backlogged = await call(
                session,
                "task_update",
                {"slug": gamma, "clear_scheduled": True},
            )
            assert backlogged["state"] == "applied_and_committed", backlogged
            assert (
                backlogged["daily_link_state"] == "applied_and_committed"
            ), backlogged
            assert backlogged["daily_link_dates"] == ["2026-07-22"], backlogged
            _assert_no_daily_link(_read_daily_note("2026-07-22"), gamma)

            # Backlog -> scheduled again, then delete cleanup.
            rescheduled_gamma = await call(
                session,
                "task_update",
                {"slug": gamma, "scheduled": "2026-07-23"},
            )
            assert (
                rescheduled_gamma["state"] == "applied_and_committed"
            ), rescheduled_gamma
            assert rescheduled_gamma["daily_link_dates"] == ["2026-07-23"], (
                rescheduled_gamma
            )
            note_23 = _read_daily_note("2026-07-23")
            assert f"- [[{gamma}]]" in note_23, note_23

            deleted = await call(session, "task_delete", {"slug": gamma})
            assert deleted["state"] == "applied_and_committed", deleted
            assert deleted["daily_link_state"] == "applied_and_committed", deleted
            assert deleted["daily_link_dates"] == ["2026-07-23"], deleted
            assert deleted["commit_id"], deleted
            assert not (vault / "tasks" / f"{gamma}.md").exists()
            _assert_no_daily_link(_read_daily_note("2026-07-23"), gamma)
            assert _daily_note_path("2026-07-23").is_file()

            # -- Issue #139 revision 3: special-character task titles ------
            # TaskNotes accepts task titles containing '[', ']', '|'. The
            # daily link is the bare canonical wikilink: only the exact
            # slug is serialized and the title is never written into the
            # Daily Note — it stays authoritative in the task's
            # frontmatter:
            #   "Review [draft]"  -> "- [[<slug>]]"
            #   "Compare A | B"   -> "- [[<slug>]]"
            delta = "20260724t130000"
            delta_title = "Review [draft]"
            delta_link = f"- [[{delta}]]"
            delta_created = await call(
                session,
                "task_create",
                {
                    "slug": delta,
                    "title": delta_title,
                    "scheduled": "2026-07-24",
                },
            )
            assert delta_created["state"] == "applied_and_committed", delta_created
            assert (
                delta_created["daily_link_state"] == "applied_and_committed"
            ), delta_created
            assert delta_created["daily_link_dates"] == ["2026-07-24"], delta_created
            # The task mutation itself succeeds with the raw title intact.
            delta_fetched = await call(session, "task_get", {"slug": delta})
            assert delta_fetched["title"] == delta_title, delta_fetched
            # The generated line is the bare canonical link; the title is
            # not serialized into the Daily Note.
            note_24 = _read_daily_note("2026-07-24")
            assert delta_link in note_24, note_24
            assert "Review [draft]" not in note_24, note_24

            # Idempotent by exact slug: an unchanged reschedule reports
            # not_applied and never duplicates the bare link.
            delta_kept = await call(
                session,
                "task_update",
                {"slug": delta, "scheduled": "2026-07-24"},
            )
            assert delta_kept["state"] == "applied_and_committed", delta_kept
            assert delta_kept["daily_link_state"] == "not_applied", delta_kept
            assert _read_daily_note("2026-07-24").count(delta_link) == 1

            # A second special-character title shares the same date: the
            # existing exact-slug link is untouched and the new bare link
            # lands alongside it.
            delta2 = "20260724t133000"
            delta2_title = "Compare A | B"
            delta2_link = f"- [[{delta2}]]"
            delta2_created = await call(
                session,
                "task_create",
                {
                    "slug": delta2,
                    "title": delta2_title,
                    "scheduled": "2026-07-24",
                },
            )
            assert delta2_created["state"] == "applied_and_committed", delta2_created
            assert delta2_created["daily_link_dates"] == ["2026-07-24"], delta2_created
            note_24 = _read_daily_note("2026-07-24")
            assert delta_link in note_24, note_24
            assert delta2_link in note_24, note_24

            # Reschedule the first task: the D1 removal must preserve the
            # unrelated second link (and every other byte) while the D2
            # note gains the bare link.
            delta_moved = await call(
                session,
                "task_update",
                {"slug": delta, "scheduled": "2026-07-25"},
            )
            assert delta_moved["state"] == "applied_and_committed", delta_moved
            assert delta_moved["daily_link_dates"] == ["2026-07-25", "2026-07-24"], (
                delta_moved
            )
            note_24_after = _read_daily_note("2026-07-24")
            assert f"[[{delta}" not in note_24_after, note_24_after
            assert delta2_link in note_24_after, note_24_after
            note_25 = _read_daily_note("2026-07-25")
            assert delta_link in note_25, note_25

            # Delete cleanup: exact-slug removal only; the note remains.
            delta_deleted = await call(session, "task_delete", {"slug": delta})
            assert delta_deleted["state"] == "applied_and_committed", delta_deleted
            assert delta_deleted["daily_link_state"] == "applied_and_committed", (
                delta_deleted
            )
            assert delta_deleted["daily_link_dates"] == ["2026-07-25"], delta_deleted
            assert not (vault / "tasks" / f"{delta}.md").exists()
            note_25_after = _read_daily_note("2026-07-25")
            assert f"[[{delta}" not in note_25_after, note_25_after
            assert _daily_note_path("2026-07-25").is_file()

            # Final listing: both phases' tasks remain; gamma is gone.
            listed = await call(session, "task_list", {"max_results": 10})
            assert {item["slug"] for item in listed["result"]} == {
                "20260719t120000",
                "20260720t090000",
                alpha,
                beta,
                delta2,
            }, listed


def _assert_daily_evidence(vault: Path, env: dict[str, str]) -> None:
    """Git + native gbrain evidence for the daily-links phase.

    Proves the deterministic Git projection commit accounting (projection
    commits stage only Daily Note paths, never the whole vault) and that
    the required incremental projection sync made the projected notes
    visible to the real gbrain index.
    """
    clean = run(["git", "status", "--porcelain"], env=env, cwd=vault)
    assert clean.strip() == "", clean
    log = run(["git", "log", "--oneline"], env=env, cwd=vault)
    # 8 task updates from the disabled-mode phase plus 14 daily-phase task
    # updates: alpha create/unchanged-reschedule/complete/archive, beta
    # create/reschedule/week, gamma create/backlog/reschedule, and the
    # delta create/unchanged-reschedule/reschedule plus delta2 create
    # (special-character titles; bare canonical links).
    assert log.count("tasknotes-mcp: task update") == 22, log
    assert log.count("tasknotes-mcp: task delete") == 2, log
    # One projection commit per mutation that CHANGED a daily target:
    # alpha create; beta create/reschedule (two targets, one commit)/week
    # cleanup; gamma create/backlog/reschedule/delete; delta
    # create/reschedule (two targets, one commit)/delete and delta2 create.
    # The idempotent unchanged reschedules (alpha and delta) commit nothing.
    assert log.count("tasknotes-mcp: daily note projection") == 12, log
    # The fixture was committed between phases; nothing is ever pending.
    assert log.count("tasknotes-mcp: preflight sync") == 0, log
    projection_ids = [
        line.split(" ", 1)[0]
        for line in log.splitlines()
        if line.endswith("tasknotes-mcp: daily note projection")
    ]
    assert len(projection_ids) == 12, projection_ids
    for commit_id in projection_ids:
        names = run(
            ["git", "show", "--name-only", "--pretty=format:", commit_id],
            env=env,
            cwd=vault,
        )
        paths = [line for line in names.splitlines() if line.strip()]
        assert paths and all(path.startswith(f"{DAILY_FOLDER}/") for path in paths), (
            commit_id,
            paths,
        )

    # Native gbrain source visibility after the engine's required
    # incremental projection sync (run under the shared lock). Slugs are
    # the vault-relative note paths under the numeric-subfolder format.
    sources = json.loads(run([GBRAIN_NATIVE, "sources", "list", "--json"], env=env))
    matching = [
        source for source in sources["sources"] if source.get("local_path") == str(vault)
    ]
    assert len(matching) == 1, sources
    source_id = matching[0]["id"]
    preexisting_slug = (
        f"{DAILY_FOLDER}/2026/07/{PREEXISTING_DAILY_NOTE_DATE[-2:]}"
    )
    page_19 = json.loads(
        run(
            [
                GBRAIN_NATIVE,
                "call",
                "--source",
                source_id,
                "get_page",
                json.dumps({"slug": preexisting_slug}),
            ],
            env=env,
        )
    )
    body_19 = page_19.get("compiled_truth", "")
    assert "[[20260721t100000]]" in body_19, body_19
    assert "[[20260718t090000|Preexisting unrelated link]]" in body_19, body_19
    page_20 = json.loads(
        run(
            [
                GBRAIN_NATIVE,
                "call",
                "--source",
                source_id,
                "get_page",
                json.dumps({"slug": f"{DAILY_FOLDER}/2026/07/20"}),
            ],
            env=env,
        )
    )
    body_20 = page_20.get("compiled_truth", "")
    assert "## Tasks" in body_20 and "Plan for the day." in body_20, body_20
    assert "[[20260722t110000" not in body_20, body_20
    # Bare-link visibility: the special-character-title task's bare
    # canonical link survives the projection sync into the real gbrain
    # index; the deleted task's link is gone.
    page_24 = json.loads(
        run(
            [
                GBRAIN_NATIVE,
                "call",
                "--source",
                source_id,
                "get_page",
                json.dumps({"slug": f"{DAILY_FOLDER}/2026/07/24"}),
            ],
            env=env,
        )
    )
    body_24 = page_24.get("compiled_truth", "")
    assert "[[20260724t133000]]" in body_24, body_24
    assert "[[20260724t130000" not in body_24, body_24


def _reconcile_refresh_source_id(env: dict[str, str]) -> str:
    """Return the single vault source id for source-routed native reads.

    Same single-default-source limitation as the projection evidence: the
    disposable harness provisions exactly one source. The exact
    ``--source <id>`` argv routing contract is additionally pinned by the
    focused unit test ``test_capture_routes_with_source``.
    """
    sources = json.loads(run([GBRAIN_NATIVE, "sources", "list", "--json"], env=env))
    matching = [
        source for source in sources["sources"] if source.get("local_path") == str(VAULT)
    ]
    assert len(matching) == 1, sources
    return matching[0]["id"]


def _gbrain_task_visible(source_id: str, slug: str, env: dict[str, str]) -> str:
    """Read a task's compiled body from the real gbrain index (read-only).

    TaskNotes resolves a task's gbrain slug to ``tasks/<slug>``. A missing
    page is reported as ``{"error": "page_not_found"}``; a present page
    returns a page object. Asserting the page is present (no error) proves
    the task is still visible through the required committed incremental
    sync that the approved refresh runs. The compiled body may be empty for
    a body-less task, so presence is the visibility signal, not content.
    """
    page = json.loads(
        run(
            [
                GBRAIN_NATIVE,
                "call",
                "--source",
                source_id,
                "get_page",
                json.dumps({"slug": f"tasks/{slug}"}),
            ],
            env=env,
        )
    )
    assert "error" not in page, page
    return page.get("compiled_truth", "")


def _external_reconcile_phase(vault: Path, env: dict[str, str]) -> None:
    """Issue #139 revision 3 W3: prove the refresh lane reconciles an
    external (non-MCP) manual task reschedule against the Daily Notes.

    Every task mutation here goes DIRECTLY to the task file and git — never
    through the MCP. Scenario:

      1. ``old_task`` is committed at ``old_date`` and its bare canonical
         Daily Note link is already committed and synced into gbrain (the
         W4 projection phase left both the link and the reconcile cursor at
         the then-HEAD).
      2. The task is edited externally to a NEW scheduled date and the
         change is committed (a manual Obsidian edit the next refresh must
         pick up).
      3. ``josemar-gbrain refresh`` runs the approved W3 lane under the
         runtime lock: the fixed reconcile CLI prepare/apply + one targeted
         commit, the wrapper's native committed incremental sync/extract,
         then finalize.
      4. We assert the old date's link is gone, the new date's link is
         exactly once, the task remains gbrain-visible through that
         committed incremental sync, and the cursor/pending reflect the
         advanced reconciled HEAD with no pending sibling.
    """
    old_task = "20260724t133000"  # delta2, still scheduled from the W4 phase
    old_date = "2026-07-24"
    new_date = "2026-07-26"

    # The bare link is live from the projection phase (exactly once).
    note_old = _read_daily_note(old_date)
    assert note_old.count(f"- [[{old_task}]]") == 1, note_old
    # The new date's note does not exist yet (no link to remove).
    assert not _daily_note_path(new_date).is_file(), _daily_note_path(new_date)

    # External manual edit (never through the MCP): rewrite the task's
    # scheduled date and commit it. The worktree then matches HEAD, so the
    # reconcile's head reader vs. worktree snapshot sees the reschedule.
    task_path = vault / "tasks" / f"{old_task}.md"
    assert task_path.is_file(), task_path
    text = task_path.read_text(encoding="utf-8")
    assert f"scheduled: '{old_date}'" in text, text
    task_path.write_text(
        text.replace(f"scheduled: '{old_date}'", f"scheduled: '{new_date}'"),
        encoding="utf-8",
    )
    run(["git", "add", "-A"], env=env, cwd=vault)
    run(["git", "commit", "-q", "-m", "external manual reschedule"], env=env, cwd=vault)
    head_before = run(["git", "rev-parse", "HEAD"], env=env, cwd=vault).strip()

    # Approved W3 refresh lane under the runtime lock: the real wrapper
    # (self-acquires the shared lock through the lock-runner chain and
    # invokes the real fixed reconcile CLI inside it) reconciles, syncs,
    # then finalizes.
    refresh = run(
        [GBRAIN_WRAPPER, "refresh"],
        env=dict(
            env,
            TASKNOTES_DAILY_LINKS_ENABLED="true",
            TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED="true",
        ),
    )
    assert '"success": true' in refresh, refresh

    # Old link gone; new link exactly once.
    note_old_after = _read_daily_note(old_date)
    assert f"- [[{old_task}]]" not in note_old_after, note_old_after
    note_new = _read_daily_note(new_date)
    assert note_new.count(f"- [[{old_task}]]") == 1, note_new

    # Task remains gbrain-visible through the refresh's committed
    # incremental sync (the page is present, not page_not_found).
    _gbrain_task_visible(_reconcile_refresh_source_id(env), old_task, env)

    # Git accounting: the W3 refresh lane created exactly one targeted
    # reconcile commit AFTER the external edit (the W4 phase's pre-mutation
    # reconcile commits precede it); the tree is clean (nothing pending
    # after the sync).
    log = run(["git", "log", "--oneline"], env=env, cwd=vault)
    lines = log.splitlines()
    external_idx = next(
        i for i, line in enumerate(lines) if line.endswith("external manual reschedule")
    )
    reconcile_after = [
        line
        for line in lines[:external_idx]
        if line.endswith("tasknotes-mcp: daily links reconcile")
    ]
    assert len(reconcile_after) == 1, reconcile_after
    clean = run(["git", "status", "--porcelain"], env=env, cwd=vault)
    assert clean.strip() == "", clean

    # Cursor/pending final state: cursor advanced to the new HEAD, pending
    # sibling cleared (finalize succeeded only after the committed sync).
    head_after = run(["git", "rev-parse", "HEAD"], env=env, cwd=vault).strip()
    assert head_after != head_before, head_after
    assert RECONCILE_CURSOR_PATH.is_file(), RECONCILE_CURSOR_PATH
    cursor = json.loads(RECONCILE_CURSOR_PATH.read_text(encoding="utf-8"))
    assert cursor["reconciled_head"] == head_after, cursor
    assert cursor["daily_folder"] == DAILY_FOLDER, cursor
    assert cursor["daily_format"] == DAILY_FORMAT, cursor
    assert not RECONCILE_PENDING_PATH.exists(), RECONCILE_PENDING_PATH


def _assert_fixed_contract_paths_used() -> None:
    """Runtime proof that the built-image MCP honored the fixed-path
    contract: gbrain state landed in /opt/data/.gbrain and the shared lock
    file exists at /opt/data/.locks/tasknotes.lock (created by the engine
    during the mutations). A silent path drift would fail here."""
    if not (GBRAIN_HOME / ".gbrain").is_dir():
        raise AssertionError("gbrain state missing at fixed path /opt/data/.gbrain")
    lock_file = LOCK_DIR / "tasknotes.lock"
    if not lock_file.is_file():
        raise AssertionError(f"lock file missing at fixed path {lock_file}")


def main() -> None:
    expected_uid = _validated_id("TASKNOTES_E2E_UID")
    expected_gid = _validated_id("TASKNOTES_E2E_GID")
    _prove_docker_harness(expected_uid, expected_gid)
    vault, env = prepare()
    # Phase 1 — disabled mode (issue #139): the runtime treats a MISSING
    # master flag as enabled, so the disabled subcase pins its env with an
    # explicit TASKNOTES_DAILY_LINKS_ENABLED="false" (the slave reconcile
    # flag is never consulted while the master is off). The plain lifecycle
    # runs before any Daily Notes configuration exists, proving no
    # configuration prerequisite and no daily_link_* result fields.
    asyncio.run(
        lifecycle(vault, dict(env, TASKNOTES_DAILY_LINKS_ENABLED="false"))
    )
    print("real-gbrain disabled-mode lifecycle: PASS")
    # Phase 2 — daily-links mode: install the fixture (writes only), then
    # run the enabled-mode lifecycle against the real MCP.
    install_daily_notes_fixture(vault, env)
    asyncio.run(
        daily_links_lifecycle(vault, dict(env, TASKNOTES_DAILY_LINKS_ENABLED="true"))
    )
    _assert_daily_evidence(vault, env)
    _assert_fixed_contract_paths_used()
    print("real-gbrain daily-links MCP lifecycle: PASS")
    # Phase 3 — issue #139 revision 3 W3: the approved refresh lane
    # reconciles an external (non-MCP) manual task reschedule under the
    # runtime lock and finalizes the cursor/pending state.
    _external_reconcile_phase(vault, env)
    _assert_fixed_contract_paths_used()
    print("real-gbrain external-edit refresh reconciliation: PASS")
    print("real-gbrain MCP lifecycle: PASS")


if __name__ == "__main__":
    main()
