"""Contract tests for the bounded MCPServer TaskNotes surface."""

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
    mcpserver_module = types.ModuleType("mcp.server.mcpserver")
    exceptions_module = types.ModuleType("mcp.server.mcpserver.exceptions")
    setattr(mcpserver_module, "MCPServer", FakeFastMCP)
    setattr(exceptions_module, "ToolError", FakeToolError)
    modules = {
        "mcp": mcp_module,
        "mcp.server": server_module,
        "mcp.server.mcpserver": mcpserver_module,
        "mcp.server.mcpserver.exceptions": exceptions_module,
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
            planned_week=None,
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
            planned_week=None,
        )

    def test_create_forwards_explicit_planned_week(self) -> None:
        """The semantic week-planning argument is forwarded exactly; the
        Monday/Mutual-exclusion validation lives in the engine."""
        self.engine.create.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        result = self.server.task_create("Task", slug="t1", planned_week="2026-08-24")
        self.assertEqual(result["state"], "applied_and_committed")
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
            custom_fields=None,
            recurrence=None,
            planned_week="2026-08-24",
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
            body=None,
            planned_week=None,
            clear_planned_week=False,
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
            planned_week=None,
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
            body=None,
            planned_week=None,
            clear_planned_week=False,
        )

    def test_update_forwards_explicit_planned_week(self) -> None:
        """Setting the week-planning target is forwarded exactly; the engine
        owns the transition (clears native scheduled)."""
        self.engine.update.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        result = self.server.task_update("t1", planned_week="2026-08-31")
        self.assertEqual(result["state"], "applied_and_committed")
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
            custom_fields=None,
            body=None,
            planned_week="2026-08-31",
            clear_planned_week=False,
        )

    def test_update_forwards_clear_planned_week(self) -> None:
        """The week-plan clear flag is forwarded exactly; the engine owns the
        clearing semantics (removes only the week-only plan)."""
        self.engine.update.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        result = self.server.task_update("t1", clear_planned_week=True)
        self.assertEqual(result["state"], "applied_and_committed")
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
            custom_fields=None,
            body=None,
            planned_week=None,
            clear_planned_week=True,
        )

    def test_update_forwards_body(self) -> None:
        self.engine.update.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        self.server.task_update("t1", body="new body")
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
            custom_fields=None,
            body="new body",
            planned_week=None,
            clear_planned_week=False,
        )

    def test_update_forwards_empty_body(self) -> None:
        self.engine.update.return_value = self.server.MutationResult(
            state="applied_and_committed", slug="t1", commit_id="abc"
        )
        self.server.task_update("t1", body="")
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
            custom_fields=None,
            body="",
            planned_week=None,
            clear_planned_week=False,
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
        """Locations are fixed constants; only the non-location operational
        settings (lock timeout, TZ, daily-links switch) come from the
        environment. An empty daily-links value disables (default off)."""
        setattr(self.server, "_ENGINE", None)
        env = {
            "TASKNOTES_LOCK_TIMEOUT": "3.5",
            "TZ": "America/Sao_Paulo",
            "TASKNOTES_DAILY_LINKS_ENABLED": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.server, "TaskNotesEngine") as engine_class:
                instance = self.server._get_engine()
        self.assertIs(instance, engine_class.return_value)
        engine_class.assert_called_once_with(
            vault=Path("/opt/data/obsidian"),
            gbrain_bin="/opt/josemar/libexec/gbrain-native",
            gbrain_home=Path("/opt/data"),
            lock_dir=Path("/opt/data/.locks"),
            lock_timeout=3.5,
            tz="America/Sao_Paulo",
            daily_links_enabled=False,
        )

    def test_engine_fixed_locations_ignore_forged_env(self) -> None:
        """Forged GBRAIN_BRAIN_REPO / GBRAIN_HOME / TASKNOTES_LOCK_DIR env
        values must not redirect the vault, state, or shared lock."""
        setattr(self.server, "_ENGINE", None)
        env = {
            "GBRAIN_BRAIN_REPO": "/forged/vault",
            "GBRAIN_HOME": "/forged/state",
            "TASKNOTES_GBRAIN_BIN": "/bin/forged-gbrain",
            "TASKNOTES_LOCK_DIR": "/forged/locks",
            "TASKNOTES_LOCK_TIMEOUT": "3.5",
            "TZ": "America/Sao_Paulo",
            "TASKNOTES_DAILY_LINKS_ENABLED": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.server, "TaskNotesEngine") as engine_class:
                self.server._get_engine()
        engine_class.assert_called_once_with(
            vault=Path("/opt/data/obsidian"),
            gbrain_bin="/opt/josemar/libexec/gbrain-native",
            gbrain_home=Path("/opt/data"),
            lock_dir=Path("/opt/data/.locks"),
            lock_timeout=3.5,
            tz="America/Sao_Paulo",
            daily_links_enabled=False,
        )

    def test_engine_refuses_to_run_as_root(self) -> None:
        """The MCP must enforce the hermes runtime identity before any native
        gbrain work: root execution is refused (fail-closed)."""
        setattr(self.server, "_ENGINE", None)
        with mock.patch.object(self.server.os, "geteuid", return_value=0):
            with self.assertRaises(RuntimeError):
                self.server._get_engine()
        self.assertIsNone(self.server._ENGINE)

    def test_engine_accepts_non_root_identity(self) -> None:
        setattr(self.server, "_ENGINE", None)
        with mock.patch.object(self.server.os, "geteuid", return_value=10000):
            with mock.patch.object(self.server, "TaskNotesEngine") as engine_class:
                instance = self.server._get_engine()
        self.assertIs(instance, engine_class.return_value)

    def test_invalid_timeout_becomes_tool_error(self) -> None:
        setattr(self.server, "_ENGINE", None)
        with mock.patch.dict(
            os.environ, {"TASKNOTES_LOCK_TIMEOUT": "invalid"}, clear=False
        ):
            with self.assertRaisesRegex(FakeToolError, "must be a number"):
                self.server.task_get("t1")

    def test_lock_timeout_rejects_non_finite_values(self) -> None:
        """nan/inf/-inf must be rejected before the engine/Lock is
        constructed: they would make the lock wait unbounded or nonsensical.
        The existing ValidationError->tool-error surface is preserved."""
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(bad=bad):
                setattr(self.server, "_ENGINE", None)
                with mock.patch.dict(
                    os.environ, {"TASKNOTES_LOCK_TIMEOUT": bad}, clear=False
                ):
                    with self.assertRaisesRegex(
                        FakeToolError, "must be a finite number"
                    ):
                        self.server.task_get("t1")
                self.assertIsNone(self.server._ENGINE)

    def test_lock_timeout_accepts_finite_positive_values(self) -> None:
        """A valid finite positive timeout reaches the engine unchanged; the
        engine/Lock is constructed with it."""
        setattr(self.server, "_ENGINE", None)
        with mock.patch.object(self.server.os, "geteuid", return_value=10000):
            with mock.patch.dict(
                os.environ, {"TASKNOTES_LOCK_TIMEOUT": "3.5"}, clear=False
            ):
                with mock.patch.object(self.server, "TaskNotesEngine") as engine_class:
                    self.server._get_engine()
        self.assertEqual(engine_class.call_args.kwargs["lock_timeout"], 3.5)

    # --- TASKNOTES_DAILY_LINKS_ENABLED strict switch (issue #139) ---

    def _get_engine_with_bool_env(self, raw: str | None) -> mock.Mock:
        """Construct the engine (TaskNotesEngine mocked) with the daily-links
        switch pinned to ``raw`` (None = key absent) in a deterministic
        clear environment, returning the mocked engine class."""
        setattr(self.server, "_ENGINE", None)
        env = {"TASKNOTES_LOCK_TIMEOUT": "10", "TZ": "UTC"}
        if raw is not None:
            env["TASKNOTES_DAILY_LINKS_ENABLED"] = raw
        with mock.patch.object(self.server.os, "geteuid", return_value=10000):
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(
                    self.server, "TaskNotesEngine"
                ) as engine_class:
                    self.server._get_engine()
        return engine_class

    def test_daily_links_env_forwards_both_valid_values_case_insensitively(
        self,
    ) -> None:
        for raw, expected in (
            ("true", True),
            ("TRUE", True),
            ("True", True),
            ("false", False),
            ("FALSE", False),
            ("False", False),
        ):
            with self.subTest(raw=raw):
                engine_class = self._get_engine_with_bool_env(raw)
                self.assertIs(
                    engine_class.call_args.kwargs["daily_links_enabled"], expected
                )

    def test_daily_links_env_missing_or_empty_disables(self) -> None:
        """Missing/empty MUST be disabled: the engine is constructed with the
        default-off value, never an ambient truthy value."""
        for raw in (None, "", "   "):
            with self.subTest(raw=repr(raw)):
                engine_class = self._get_engine_with_bool_env(raw)
                self.assertIs(
                    engine_class.call_args.kwargs["daily_links_enabled"], False
                )

    def test_daily_links_env_invalid_rejected_at_engine_init(self) -> None:
        """Any nonempty value other than case-insensitive true/false is
        rejected at engine init (fail-closed, no coercion); the engine is
        never constructed and the invalid value never reaches it. Through a
        tool call the same rejection surfaces as a tool error."""
        for bad in ("yes", "1", "0", "on", "enabled", " true extra"):
            with self.subTest(bad=bad):
                setattr(self.server, "_ENGINE", None)
                with mock.patch.object(
                    self.server.os, "geteuid", return_value=10000
                ):
                    with mock.patch.dict(
                        os.environ,
                        {"TASKNOTES_DAILY_LINKS_ENABLED": bad},
                        clear=False,
                    ):
                        with mock.patch.object(
                            self.server, "TaskNotesEngine"
                        ) as engine_class:
                            with self.assertRaisesRegex(
                                self.server.ValidationError,
                                "TASKNOTES_DAILY_LINKS_ENABLED must be "
                                "'true' or 'false'",
                            ):
                                self.server._get_engine()
                            with self.assertRaisesRegex(
                                FakeToolError,
                                "TASKNOTES_DAILY_LINKS_ENABLED must be "
                                "'true' or 'false'",
                            ):
                                self.server.task_get("t1")
                self.assertIsNone(self.server._ENGINE)
                engine_class.assert_not_called()

    def test_main_uses_stdio_transport(self) -> None:
        self.server.main()
        self.assertEqual(self.server.mcp.run_calls, [{"transport": "stdio"}])


if __name__ == "__main__":
    unittest.main()
