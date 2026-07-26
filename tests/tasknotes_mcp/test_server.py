"""Contract tests for the bounded FastMCP TaskNotes surface."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = REPO_ROOT / "scripts" / "tasknotes_mcp.py"
SCRIPTS_PATH = str(REPO_ROOT / "scripts")


class FakeToolError(Exception):
    pass


class FakeFastMCP:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.tools = {}
        self.run_calls = []

    def tool(self, **kwargs):
        def decorate(function):
            self.tools[function.__name__] = (function, kwargs)
            return function

        return decorate

    def run(self, **kwargs):
        self.run_calls.append(kwargs)


def _load_server():
    mcp_module = types.ModuleType("mcp")
    server_module = types.ModuleType("mcp.server")
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    exceptions_module = types.ModuleType("mcp.server.fastmcp.exceptions")
    setattr(fastmcp_module, "FastMCP", FakeFastMCP)
    setattr(exceptions_module, "ToolError", FakeToolError)
    modules = {
        "mcp": mcp_module,
        "mcp.server": server_module,
        "mcp.server.fastmcp": fastmcp_module,
        "mcp.server.fastmcp.exceptions": exceptions_module,
    }
    spec = importlib.util.spec_from_file_location("tasknotes_mcp", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    old_path = list(sys.path)
    try:
        if SCRIPTS_PATH not in sys.path:
            sys.path.insert(0, SCRIPTS_PATH)
        with mock.patch.dict(sys.modules, modules):
            sys.modules["tasknotes_mcp"] = module
            spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path
    return module


class ServerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _load_server()
        self.engine = mock.Mock()
        setattr(self.server, "_ENGINE", self.engine)

    def test_exposes_exact_bounded_tool_surface(self) -> None:
        self.assertEqual(
            set(self.server.mcp.tools),
            {
                "task_create",
                "task_get",
                "task_list",
                "task_update",
                "task_complete",
                "task_archive",
                "task_delete",
                "task_add_tag",
                "task_remove_tag",
            },
        )
        for _function, options in self.server.mcp.tools.values():
            self.assertIs(options.get("structured_output"), True)

    def test_create_forwards_typed_fields_and_returns_structured_result(self) -> None:
        self.engine.create.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        result = self.server.task_create(
            "Task",
            status="open",
            priority="normal",
            due="2026-07-20",
            projects=["home"],
            tags=["next"],
            body="Details",
            slug="t1",
        )
        self.assertEqual(
            result,
            {"state": "applied_and_committed", "slug": "t1", "commit_id": "abc"},
        )
        self.engine.create.assert_called_once_with(
            "t1",
            "Task",
            status="open",
            priority="normal",
            due="2026-07-20",
            scheduled=None,
            projects=["home"],
            tags=["next"],
            body="Details",
            custom_fields=None,
            recurrence=None,
        )

    def test_create_auto_generates_slug_when_omitted(self) -> None:
        self.engine.create.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="auto-slug", commit_id="abc"
        )
        result = self.server.task_create(
            "Buy Groceries",
            status="open",
        )
        self.assertEqual(result["state"], "applied_and_committed")
        # Engine receives None as slug (auto-generation happens in engine).
        self.engine.create.assert_called_once_with(
            None,
            "Buy Groceries",
            status="open",
            priority=None,
            due=None,
            scheduled=None,
            projects=None,
            tags=None,
            body="",
            custom_fields=None,
            recurrence=None,
        )

    def test_update_forwards_only_supported_fields(self) -> None:
        self.engine.update.return_value = self.server.MutationResult(
            state="not_applied", slug="t1"
        )
        result = self.server.task_update("t1", clear_due=True)
        self.assertEqual(result, {"state": "not_applied", "slug": "t1"})
        self.engine.update.assert_called_once_with(
            "t1",
            status=None,
            priority=None,
            due=None,
            scheduled=None,
            projects=None,
            clear_due=True,
            clear_scheduled=False,
            clear_projects=False,
            custom_fields=None,
        )

    def test_create_forwards_custom_fields(self) -> None:
        self.engine.create.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        self.server.task_create(
            "Task",
            slug="t1",
            custom_fields={"pipeline_stage": "drafting"},
        )
        self.engine.create.assert_called_once_with(
            "t1",
            "Task",
            status=None,
            priority=None,
            due=None,
            scheduled=None,
            projects=None,
            tags=None,
            body="",
            custom_fields={"pipeline_stage": "drafting"},
            recurrence=None,
        )

    def test_update_forwards_custom_fields(self) -> None:
        self.engine.update.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        self.server.task_update("t1", custom_fields={"pipeline_stage": None})
        self.engine.update.assert_called_once_with(
            "t1",
            status=None,
            priority=None,
            due=None,
            scheduled=None,
            projects=None,
            clear_due=False,
            clear_scheduled=False,
            clear_projects=False,
            custom_fields={"pipeline_stage": None},
        )

    def test_add_tag_forwards_to_engine(self) -> None:
        self.engine.add_tag.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        result = self.server.task_add_tag("t1", "urgent")
        self.assertEqual(
            result,
            {"state": "applied_and_committed", "slug": "t1", "commit_id": "abc"},
        )
        self.engine.add_tag.assert_called_once_with("t1", "urgent")

    def test_remove_tag_forwards_to_engine(self) -> None:
        self.engine.remove_tag.return_value = self.server.MutationResult(
            state="not_applied", slug="t1"
        )
        result = self.server.task_remove_tag("t1", "urgent")
        self.assertEqual(result, {"state": "not_applied", "slug": "t1"})
        self.engine.remove_tag.assert_called_once_with("t1", "urgent")

    def test_delete_forwards_to_engine(self) -> None:
        self.engine.delete.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        result = self.server.task_delete("t1")
        self.assertEqual(
            result,
            {"state": "applied_and_committed", "slug": "t1", "commit_id": "abc"},
        )
        self.engine.delete.assert_called_once_with("t1")

    def test_expected_core_error_becomes_tool_error(self) -> None:
        self.engine.get.side_effect = self.server.ValidationError("invalid slug")
        with self.assertRaisesRegex(FakeToolError, "invalid slug"):
            self.server.task_get("BAD")

    def test_unexpected_error_is_generic_and_logs_no_arguments(self) -> None:
        self.engine.get.side_effect = RuntimeError("private task content")
        with mock.patch.object(self.server.LOGGER, "error") as log:
            with self.assertRaisesRegex(FakeToolError, "task_get failed unexpectedly"):
                self.server.task_get("secret-slug")
        log.assert_called_once_with("unexpected failure in %s", "task_get")

    def test_list_bounds_result_count_before_engine_call(self) -> None:
        for value in (0, self.server.LIST_MAX_RESULTS + 1, True):
            with self.subTest(value=value):
                with self.assertRaises(FakeToolError):
                    self.server.task_list(value)
        self.engine.list.assert_not_called()

    def test_list_returns_engine_result(self) -> None:
        self.engine.list.return_value = [{"slug": "t1", "title": "Task"}]
        self.assertEqual(
            self.server.task_list(25), [{"slug": "t1", "title": "Task"}]
        )
        self.engine.list.assert_called_once_with(
            max_results=25,
            status=None,
            priority=None,
            tag=None,
            archived=None,
        )

    def test_engine_uses_runtime_environment(self) -> None:
        setattr(self.server, "_ENGINE", None)
        env = {
            "GBRAIN_BRAIN_REPO": "/vault",
            "GBRAIN_HOME": "/state",
            "TASKNOTES_GBRAIN_BIN": "/bin/gbrain-test",
            "TASKNOTES_LOCK_DIR": "/locks",
            "TASKNOTES_LOCK_TIMEOUT": "3.5",
            "TZ": "America/Sao_Paulo",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.server, "TaskNotesEngine") as engine_class:
                instance = self.server._get_engine()
        self.assertIs(instance, engine_class.return_value)
        engine_class.assert_called_once_with(
            vault=Path("/vault"),
            gbrain_bin="/bin/gbrain-test",
            gbrain_home=Path("/state"),
            lock_dir=Path("/locks"),
            lock_timeout=3.5,
            tz="America/Sao_Paulo",
        )

    def test_invalid_timeout_becomes_tool_error(self) -> None:
        setattr(self.server, "_ENGINE", None)
        with mock.patch.dict(
            os.environ, {"TASKNOTES_LOCK_TIMEOUT": "invalid"}, clear=False
        ):
            with self.assertRaisesRegex(FakeToolError, "must be a number"):
                self.server.task_get("t1")

    def test_main_uses_stdio_transport(self) -> None:
        self.server.main()
        self.assertEqual(self.server.mcp.run_calls, [{"transport": "stdio"}])


if __name__ == "__main__":
    unittest.main()
