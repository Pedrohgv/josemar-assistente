#!/usr/bin/env python3
"""Disposable built-image TaskNotes MCP lifecycle smoke test."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
    ],
}


# The public `gbrain` on PATH is the issue #110 adapter (which rejects admin
# commands), so this built-image smoke harness calls the private native CLI
# directly — the same fixed path TaskNotes uses in production. An explicit
# override is allowed only for local development against a scratch install.
GBRAIN_NATIVE = os.environ.get("REAL_GBRAIN_E2E_NATIVE_BIN", "/opt/josemar/libexec/gbrain-native")


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


def prepare(root: Path) -> tuple[Path, dict[str, str]]:
    vault = root / "vault"
    state = root / "state"
    home = root / "home"
    plugin = vault / ".obsidian" / "plugins" / "tasknotes"
    tasks = vault / "tasks"
    for directory in (plugin, tasks, state, home):
        directory.mkdir(parents=True, exist_ok=True)
    (plugin / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (plugin / "data.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    (vault / ".placeholder").write_text("disposable vault\n", encoding="utf-8")

    env = {
        "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "GBRAIN_HOME": str(state),
        "GBRAIN_BRAIN_REPO": str(vault),
        "GBRAIN_SKIP_STARTUP_HOOKS": "1",
        "TASKNOTES_LOCK_DIR": str(state / ".locks"),
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


async def call(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    if result.isError:
        rendered = " ".join(getattr(item, "text", repr(item)) for item in result.content)
        raise AssertionError(f"{name} returned MCP error: {rendered}")
    assert result.structuredContent is not None, name
    return result.structuredContent


async def lifecycle(vault: Path, env: dict[str, str]) -> None:
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

    clean = run(["git", "status", "--porcelain"], env=env, cwd=vault)
    assert clean.strip() == "", clean
    log = run(["git", "log", "--oneline"], env=env, cwd=vault)
    assert log.count("tasknotes-mcp: task update") == 6, log
    task_text = (vault / "tasks" / "20260719t120000.md").read_text(encoding="utf-8")
    assert "archived" in task_text
    assert "pipeline_stage" in task_text
    assert "Disposable body preserved by gbrain." in task_text


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="tasknotes-real-e2e-"))
    try:
        vault, env = prepare(root)
        asyncio.run(lifecycle(vault, env))
        print("real-gbrain MCP lifecycle: PASS")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
