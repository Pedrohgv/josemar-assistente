"""Contract tests for the bounded FastMCP Josemar Knowledge MCP surface.

These tests mirror the tasknotes_mcp test_server.py pattern: they stub the
``mcp`` package with a FakeFastMCP, load the server module from
``scripts/josemar_knowledge_mcp.py``, and assert the exact tool surface,
validation bounds, gbrain allowlisting, lock coordination, and josemar_chat
auth/request behavior (mocked). No real gbrain or HTTP calls are made.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = REPO_ROOT / "scripts" / "josemar_knowledge_mcp.py"
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
    spec = importlib.util.spec_from_file_location("josemar_knowledge_mcp", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    old_path = list(sys.path)
    try:
        if SCRIPTS_PATH not in sys.path:
            sys.path.insert(0, SCRIPTS_PATH)
        with mock.patch.dict(sys.modules, modules):
            sys.modules["josemar_knowledge_mcp"] = module
            spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path
    return module


class ServerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _load_server()

    def test_exposes_exact_bounded_tool_surface(self) -> None:
        self.assertEqual(
            set(self.server.mcp.tools),
            {"vault_search", "vault_get", "project_context", "josemar_chat"},
        )
        for _function, options in self.server.mcp.tools.values():
            self.assertIs(options.get("structured_output"), True)

    def test_main_uses_stdio_transport(self) -> None:
        self.server.main()
        self.assertEqual(self.server.mcp.run_calls, [{"transport": "stdio"}])

    def test_log_level_is_warning(self) -> None:
        self.assertEqual(self.server.mcp.kwargs.get("log_level"), "WARNING")

    # --- vault_search validation ---

    def test_search_rejects_non_integer_max_results(self) -> None:
        for value in (True, "10", 1.5):
            with self.subTest(value=value):
                with self.assertRaises(FakeToolError):
                    self.server.vault_search("q", value)

    def test_search_rejects_out_of_bounds_max_results(self) -> None:
        for value in (0, self.server.SEARCH_MAX_RESULTS + 1, -1):
            with self.subTest(value=value):
                with self.assertRaises(FakeToolError):
                    self.server.vault_search("q", value)

    def test_search_rejects_empty_query(self) -> None:
        for value in ("", "   ", "\n\t"):
            with self.subTest(value=value):
                with self.assertRaises(FakeToolError):
                    self.server.vault_search(value)

    def test_search_rejects_oversized_query(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.vault_search("x" * 2001)

    def test_search_rejects_control_chars_in_query(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.vault_search("q\x00")

    def test_search_returns_normalized_results(self) -> None:
        with mock.patch.object(self.server, "_run_gbrain") as run:
            run.return_value = json.dumps(
                {"results": [{"slug": "a", "title": "A", "type": "note", "score": 0.9}]}
            )
            with mock.patch.object(self.server, "_GbrainLock") as lock:
                result = self.server.vault_search("query", 5)
        self.assertEqual(
            result,
            [
                {
                    "slug": "a",
                    "title": "A",
                    "type": "note",
                    "score": 0.9,
                    "snippet": "",
                }
            ],
        )
        run.assert_called_once()

    def test_search_handles_list_output(self) -> None:
        with mock.patch.object(self.server, "_run_gbrain") as run:
            run.return_value = json.dumps([{"slug": "a", "title": "A"}])
            with mock.patch.object(self.server, "_GbrainLock") as lock:
                result = self.server.vault_search("query", 10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["slug"], "a")

    def test_search_handles_non_json_output(self) -> None:
        with mock.patch.object(self.server, "_run_gbrain") as run:
            run.return_value = "not json"
            with mock.patch.object(self.server, "_GbrainLock") as lock:
                result = self.server.vault_search("query", 10)
        self.assertEqual(result, [])

    # --- vault_get validation ---

    def test_get_rejects_absolute_path(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.vault_get("/etc/passwd")

    def test_get_rejects_home_path(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.vault_get("~/secret")

    def test_get_rejects_parent_reference(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.vault_get("../escape")

    def test_get_rejects_uppercase(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.vault_get("Bad/Slug")

    def test_get_rejects_empty(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.vault_get("")

    def test_get_rejects_control_chars(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.vault_get("a\x00b")

    def test_get_returns_normalized_page(self) -> None:
        with mock.patch.object(self.server, "_run_gbrain") as run:
            run.return_value = json.dumps(
                {
                    "slug": "inbox/note",
                    "title": "Note",
                    "type": "note",
                    "tags": ["x"],
                    "frontmatter": {"k": "v"},
                    "compiled_truth": "body",
                }
            )
            with mock.patch.object(self.server, "_GbrainLock") as lock:
                result = self.server.vault_get("inbox/note")
        self.assertEqual(result["slug"], "inbox/note")
        self.assertEqual(result["content"], "body")
        self.assertEqual(result["tags"], ["x"])

    def test_get_page_not_found_raises_tool_error(self) -> None:
        with mock.patch.object(self.server, "_run_gbrain") as run:
            run.return_value = json.dumps({"error": "page_not_found"})
            with mock.patch.object(self.server, "_GbrainLock") as lock:
                with self.assertRaisesRegex(FakeToolError, "page not found"):
                    self.server.vault_get("missing")

    # --- project_context ---

    def test_project_context_reads_allowlisted_slugs(self) -> None:
        slugs = list(self.server.PROJECT_CONTEXT_SLUGS)
        self.assertTrue(slugs)
        with mock.patch.object(self.server, "_run_gbrain") as run:
            run.return_value = json.dumps(
                {"slug": "x", "title": "T", "compiled_truth": "c"}
            )
            with mock.patch.object(self.server, "_GbrainLock") as lock:
                result = self.server.project_context()
        self.assertEqual(set(result.keys()), set(slugs))
        for slug in slugs:
            self.assertIsNotNone(result[slug])
        self.assertEqual(run.call_count, len(slugs))

    def test_project_context_missing_slug_is_none(self) -> None:
        with mock.patch.object(self.server, "_run_gbrain") as run:
            run.return_value = json.dumps({"error": "page_not_found"})
            with mock.patch.object(self.server, "_GbrainLock") as lock:
                result = self.server.project_context()
        for slug in self.server.PROJECT_CONTEXT_SLUGS:
            self.assertIsNone(result[slug])

    # --- josemar_chat validation and auth ---

    def test_chat_rejects_empty_prompt(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.josemar_chat("")

    def test_chat_rejects_oversized_prompt(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.josemar_chat("x" * (self.server.CHAT_MAX_PROMPT_CHARS + 1))

    def test_chat_rejects_non_integer_max_tokens(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server.josemar_chat("hi", True)

    def test_chat_rejects_out_of_bounds_max_tokens(self) -> None:
        for value in (0, self.server.CHAT_MAX_TOKENS + 1, -1):
            with self.subTest(value=value):
                with self.assertRaises(FakeToolError):
                    self.server.josemar_chat("hi", value)

    def test_chat_requires_api_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(FakeToolError):
                self.server.josemar_chat("hi")

    def test_chat_posts_bearer_auth_and_returns_content(self) -> None:
        captured: dict = {}

        class FakeResp:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def __enter__(self) -> "FakeResp":
                return self

            def __exit__(self, *args) -> None:
                pass

            def read(self) -> bytes:
                return self._body

        class FakeReq:
            def __init__(self, url, data, headers, method):
                self._url = url
                self._data = data
                self._headers = headers
                self._method = method

            def full_url(self) -> str:
                return self._url

            def get_method(self) -> str:
                return self._method

            @property
            def data(self) -> bytes:
                return self._data

            def header_items(self):
                return list(self._headers.items())

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url()
            captured["headers"] = dict(req.header_items())
            captured["method"] = req.get_method()
            captured["data"] = json.loads(req.data.decode("utf-8"))
            return FakeResp(
                json.dumps(
                    {
                        "model": "Josemar",
                        "choices": [
                            {"message": {"role": "assistant", "content": "hello"}}
                        ],
                        "usage": {"total_tokens": 5},
                    }
                ).encode("utf-8")
            )

        env = {"API_SERVER_KEY": "secret-key", "JOSEMAR_MCP_CHAT_HOST": "localhost"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.server, "urllib_request") as ureq_mod:
                ureq_mod.Request.side_effect = FakeReq
                ureq_mod.urlopen.side_effect = fake_urlopen
                result = self.server.josemar_chat("hi", 100)
        self.assertEqual(result["content"], "hello")
        self.assertEqual(result["model"], "Josemar")
        self.assertEqual(captured["url"], "http://localhost:8642/v1/chat/completions")
        self.assertEqual(captured["method"], "POST")
        # Bearer token sent.
        auth_header = captured["headers"].get("Authorization")
        self.assertEqual(auth_header, "Bearer secret-key")
        # Payload shape.
        self.assertEqual(captured["data"]["model"], "Josemar")
        self.assertEqual(captured["data"]["messages"][0]["content"], "hi")
        self.assertEqual(captured["data"]["max_tokens"], 100)
        self.assertIs(captured["data"]["stream"], False)

    def test_chat_http_error_is_generic_no_key(self) -> None:
        from urllib import error as urllib_error

        env = {"API_SERVER_KEY": "secret-key"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.server, "urllib_request") as ureq:
                ureq.urlopen.side_effect = urllib_error.HTTPError(
                    "url", 401, "Unauthorized", {}, None  # type: ignore[arg-type]
                )
                with self.assertRaisesRegex(FakeToolError, "HTTP 401"):
                    self.server.josemar_chat("hi")
        # The error message must not contain the key.
        try:
            self.server.josemar_chat("hi")
        except FakeToolError as exc:
            self.assertNotIn("secret-key", str(exc))

    def test_chat_url_error_is_generic(self) -> None:
        from urllib import error as urllib_error

        env = {"API_SERVER_KEY": "secret-key"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.server, "urllib_request") as ureq:
                ureq.urlopen.side_effect = urllib_error.URLError("conn refused")
                with self.assertRaisesRegex(FakeToolError, "unreachable"):
                    self.server.josemar_chat("hi")

    # --- gbrain allowlisting and subprocess ---

    def test_run_gbrain_rejects_disallowed_subcommand(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server._run_gbrain(["gbrain", "delete", "x"])

    def test_run_gbrain_rejects_no_subcommand(self) -> None:
        with self.assertRaises(FakeToolError):
            self.server._run_gbrain(["gbrain"])

    def test_run_gbrain_allows_search_and_get(self) -> None:
        allowed = self.server.ALLOWED_GBRAIN_SUBCOMMANDS
        self.assertEqual(allowed, frozenset({"search", "get"}))

    def test_run_gbrain_timeout_is_generic(self) -> None:
        import subprocess

        with mock.patch.object(self.server.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="gbrain", timeout=1)
            with self.assertRaisesRegex(FakeToolError, "timed out"):
                self.server._run_gbrain(["gbrain", "search", "q", "--json"])

    def test_run_gbrain_nonzero_return_sanitized_stderr(self) -> None:
        with mock.patch.object(self.server.subprocess, "run") as run:
            run.return_value = mock.Mock(
                returncode=1,
                stdout=b"",
                stderr=b"some diagnostic\nwith newlines",
            )
            with self.assertRaisesRegex(FakeToolError, "rc=1"):
                self.server._run_gbrain(["gbrain", "search", "q", "--json"])

    def test_gbrain_env_is_minimal_no_credentials(self) -> None:
        env = self.server._gbrain_env()
        for forbidden in ("API_SERVER_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY"):
            self.assertNotIn(forbidden, env)
        self.assertEqual(env["GBRAIN_SKIP_STARTUP_HOOKS"], "1")
        self.assertIn("GBRAIN_HOME", env)
        self.assertIn("GBRAIN_BRAIN_REPO", env)

    # --- lock coordination (fail-closed, context manager) ---

    def test_search_acquires_shared_lock_via_context_manager(self) -> None:
        with mock.patch.object(self.server, "_GbrainLock") as lock_cls:
            ctx = mock.MagicMock()
            lock_cls.return_value = ctx
            with mock.patch.object(self.server, "_run_gbrain") as run:
                run.return_value = "[]"
                self.server.vault_search("q", 5)
        lock_cls.assert_called_once_with()
        ctx.__enter__.assert_called_once()
        ctx.__exit__.assert_called_once()

    def test_get_acquires_shared_lock_via_context_manager(self) -> None:
        with mock.patch.object(self.server, "_GbrainLock") as lock_cls:
            ctx = mock.MagicMock()
            lock_cls.return_value = ctx
            with mock.patch.object(self.server, "_run_gbrain") as run:
                run.return_value = json.dumps({"slug": "a", "title": "A"})
                self.server.vault_get("a")
        lock_cls.assert_called_once_with()
        ctx.__enter__.assert_called_once()
        ctx.__exit__.assert_called_once()

    def test_lock_failure_fails_closed_with_busy_error(self) -> None:
        # When the shared lock cannot be acquired, the tool must raise a
        # generic "vault busy" ToolError and NOT proceed with the read.
        with mock.patch.object(self.server, "_GbrainLock") as lock_cls:
            ctx = mock.MagicMock()
            ctx.__enter__.side_effect = FakeToolError("vault busy: lock timed out")
            lock_cls.return_value = ctx
            with mock.patch.object(self.server, "_run_gbrain") as run:
                with self.assertRaisesRegex(FakeToolError, "vault busy"):
                    self.server.vault_search("q", 5)
        run.assert_not_called()

    def test_lock_open_failure_fails_closed(self) -> None:
        with mock.patch.object(self.server, "_GbrainLock") as lock_cls:
            ctx = mock.MagicMock()
            ctx.__enter__.side_effect = FakeToolError("vault busy: lock unavailable")
            lock_cls.return_value = ctx
            with mock.patch.object(self.server, "_run_gbrain") as run:
                with self.assertRaisesRegex(FakeToolError, "vault busy"):
                    self.server.vault_get("a")
        run.assert_not_called()

    def test_chat_does_not_acquire_vault_lock(self) -> None:
        env = {"API_SERVER_KEY": "k"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.server, "_post_chat") as post:
                post.return_value = {"content": "x"}
                with mock.patch.object(self.server, "_GbrainLock") as lock_cls:
                    self.server.josemar_chat("hi")
        lock_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()