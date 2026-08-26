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

The script never deletes anything under /opt/data: the outer host test
fixture owns the fresh temporary directory and removes it after the
container exits.
"""

from __future__ import annotations

import asyncio
import json
import os
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
    asyncio.run(lifecycle(vault, env))
    _assert_fixed_contract_paths_used()
    print("real-gbrain MCP lifecycle: PASS")


if __name__ == "__main__":
    main()
