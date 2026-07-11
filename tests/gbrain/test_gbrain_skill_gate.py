"""Contract tests for the gbrain skill chat-facing gate and action surface.

These tests run the skill module in a subprocess with a fake josemar-gbrain
wrapper to verify:
  - allowed native actions: status, search, get, capture, put, link, backlinks
  - rejection of old note.* route names (dotted and underscored) and query
  - rejection of reindex from chat
  - action-specific input validation and bounds
  - gate passthrough (skill delegates to wrapper; wrapper enforces gate)
  - JSON payload passing (no sparse positional arguments)
  - capture with only type works (not treated as slug)
  - link with only link_source works (not treated as link_type/context)
  - write-through failure detection for capture/put
  - strengthened slug validation (backslash, URL-encoded, control/bidi, long)
  - output caps and JSON envelopes
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills-factory" / "gbrain" / "gbrain"


def load_skill_module():
    loader = importlib.machinery.SourceFileLoader("gbrain_skill_under_test", str(SKILL_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Could not load gbrain skill module")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def make_fake_wrapper(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    record_path: Path | None = None,
) -> Path:
    """Create a fake josemar-gbrain wrapper script that prints fixed output."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, encoding="utf-8")
    try:
        tmp.write("#!/bin/sh\n")
        if record_path is not None:
            tmp.write(f'for a in "$@"; do printf "%s\\n" "$a" >> {record_path}; done\n')
        if stdout:
            tmp.write(f"printf '%s\\n' {json.dumps(stdout)}\n")
        if stderr:
            tmp.write(f"printf '%s\\n' {json.dumps(stderr)} >&2\n")
        tmp.write(f"exit {exit_code}\n")
    finally:
        tmp.close()
    path = Path(tmp.name)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def run_skill(module, payload: dict | str, *, env: dict | None = None) -> tuple[int, str]:
    """Run the skill's main() in a subprocess with controlled stdin/env."""
    if isinstance(payload, dict):
        stdin_text = json.dumps(payload)
    else:
        stdin_text = payload

    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = str(REPO_ROOT)
    if env:
        full_env.update(env)

    driver = (
        "import importlib.util, importlib.machinery, sys\n"
        "loader = importlib.machinery.SourceFileLoader('gbrain_skill_under_test', sys.argv[1])\n"
        "spec = importlib.util.spec_from_loader(loader.name, loader)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "loader.exec_module(mod)\n"
        "sys.argv = ['gbrain']\n"
        "sys.exit(mod.main())\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", driver, str(SKILL_PATH)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    return proc.returncode, proc.stdout


def run_skill_cli(
    cli_args: list[str],
    *,
    env: dict | None = None,
    stdin: str = "",
) -> tuple[int, str]:
    """Run the skill's main() in a subprocess with CLI argv (not JSON stdin)."""
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = str(REPO_ROOT)
    if env:
        full_env.update(env)

    driver = (
        "import importlib.util, importlib.machinery, sys\n"
        "loader = importlib.machinery.SourceFileLoader('gbrain_skill_under_test', sys.argv[1])\n"
        "spec = importlib.util.spec_from_loader(loader.name, loader)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "loader.exec_module(mod)\n"
        "sys.argv = ['gbrain'] + sys.argv[2:]\n"
        "sys.exit(mod.main())\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", driver, str(SKILL_PATH), *cli_args],
        input=stdin,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    return proc.returncode, proc.stdout


def parse_json(stdout: str) -> dict:
    return json.loads(stdout)


class GbrainSkillGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempfiles: list[Path] = []
        self.module = load_skill_module()

    def tearDown(self) -> None:
        for p in self._tempfiles:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def _fake_wrapper(
        self,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        record: bool = False,
    ) -> Path:
        record_path = None
        if record:
            tf = tempfile.NamedTemporaryFile(suffix=".calls", delete=False)
            tf.close()
            record_path = Path(tf.name)
            self._tempfiles.append(record_path)
        return make_fake_wrapper(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            record_path=record_path,
        )

    def _ok_wrapper(self, action: str, **extra) -> Path:
        return self._fake_wrapper(stdout=json.dumps({"success": True, "action": action, **extra}))

    def _record_wrapper(self, action: str, **extra) -> tuple[Path, Path]:
        record = tempfile.NamedTemporaryFile(suffix=".calls", delete=False)
        record.close()
        record_path = Path(record.name)
        self._tempfiles.append(record_path)
        wrapper = make_fake_wrapper(
            stdout=json.dumps({"success": True, "action": action, **extra}),
            record_path=record_path,
        )
        return wrapper, record_path

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def test_status_returns_wrapper_envelope(self) -> None:
        wrapper = self._fake_wrapper(
            stdout=json.dumps({"success": True, "action": "status", "gate_open": False, "enabled": True})
        )
        rc, out = run_skill(self.module, {"action": "status"}, env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("status", data["action"])

    def test_status_when_wrapper_missing_returns_error(self) -> None:
        rc, out = run_skill(
            self.module,
            {"action": "status"},
            env={"JOSEMAR_GBRAIN_WRAPPER": "/nonexistent/path/does-not-exist"},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("wrapper_missing", data["error"])

    def test_status_when_wrapper_errors_returns_error_envelope(self) -> None:
        wrapper = self._fake_wrapper(exit_code=2, stdout="not json at all")
        rc, out = run_skill(self.module, {"action": "status"}, env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("wrapper_unparseable", data["error"])

    # ------------------------------------------------------------------
    # schema_status
    # ------------------------------------------------------------------

    def test_schema_status_returns_wrapper_envelope(self) -> None:
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "schema_status",
                "selected_pack": "josemar-user",
                "is_bundled": False,
                "source_exists": True,
                "source_sha256": "abc123",
                "installed_exists": True,
                "installed_sha256": "abc123",
                "pack_matches": True,
                "source_hash_matches": True,
                "installed_hash_matches": True,
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "schema_status"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("schema_status", data["action"])
        self.assertEqual("josemar-user", data["selected_pack"])

    def test_schema_status_passes_through_degraded(self) -> None:
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": False,
                "error": "schema_source_missing",
                "message": "Custom pack source not found.",
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "schema_status"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("schema_source_missing", data["error"])

    def test_schema_status_uses_argv_array(self) -> None:
        record = tempfile.NamedTemporaryFile(suffix=".calls", delete=False)
        record.close()
        record_path = Path(record.name)
        self._tempfiles.append(record_path)
        wrapper = make_fake_wrapper(
            stdout=json.dumps({"success": True, "action": "schema_status"}),
            record_path=record_path,
        )
        rc, out = run_skill(
            self.module,
            {"action": "schema_status"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        self.assertIn("schema-status", calls)

    def test_schema_mutation_action_rejected(self) -> None:
        """Generic schema mutation verbs must not be exposed."""
        wrapper = self._ok_wrapper("schema_status")
        for action in ["schema_use", "schema_sync", "schema_edit", "schema_init", "schema_fork"]:
            rc, out = run_skill(
                self.module,
                {"action": action},
                env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
            )
            self.assertNotEqual(0, rc, f"action {action} should be rejected")
            data = parse_json(out)
            self.assertFalse(data["success"])
            self.assertEqual("unknown_action", data["error"])

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def test_search_success(self) -> None:
        wrapper = self._ok_wrapper("search", result="[0.99] note-a -- snippet")
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": "obsidian sync", "limit": 5},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("search", data["action"])

    def test_search_rejects_missing_query(self) -> None:
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill(self.module, {"action": "search"}, env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("missing_query", data["error"])

    def test_search_rejects_empty_query(self) -> None:
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": "   "},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("empty_query", data["error"])

    def test_search_rejects_too_long_query(self) -> None:
        wrapper = self._ok_wrapper("search")
        long_query = "x" * 3000
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": long_query},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper), "GBRAIN_QUERY_MAX_INPUT_CHARS": "2000"},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("query_too_long", data["error"])

    def test_search_rejects_invalid_limit_type(self) -> None:
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": "test", "limit": "ten"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_limit", data["error"])

    def test_search_rejects_limit_out_of_range(self) -> None:
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": "test", "limit": 999},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper), "GBRAIN_QUERY_MAX_LIMIT": "20"},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("limit_out_of_range", data["error"])

    def test_search_rejects_invalid_offset(self) -> None:
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": "test", "offset": -1},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_offset", data["error"])

    def test_search_passes_json_payload(self) -> None:
        wrapper, record_path = self._record_wrapper("search", result="ok")
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": "obsidian sync", "limit": 7, "offset": 2},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        self.assertIn("search", calls)
        # The second arg must be a JSON payload containing query, limit, offset.
        payload_str = calls[1]
        payload = json.loads(payload_str)
        self.assertEqual(payload["query"], "obsidian sync")
        self.assertEqual(payload["limit"], 7)
        self.assertEqual(payload["offset"], 2)

    def test_search_passes_through_disabled_error(self) -> None:
        wrapper = self._fake_wrapper(
            stdout=json.dumps({"success": False, "error": "gbrain_disabled", "message": "GBRAIN_ENABLED is not true."})
        )
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("gbrain_disabled", data["error"])

    def test_search_passes_through_marker_error(self) -> None:
        wrapper = self._fake_wrapper(
            stdout=json.dumps({"success": False, "error": "marker_missing_or_invalid", "message": "Run reindex."})
        )
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("marker_missing_or_invalid", data["error"])

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    def test_get_success(self) -> None:
        wrapper = self._ok_wrapper("get", result="page content")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "inbox/my-note"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("get", data["action"])

    def test_get_rejects_missing_slug(self) -> None:
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(self.module, {"action": "get"}, env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_get_rejects_traversal_slug(self) -> None:
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "../../../etc/passwd"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_get_passes_json_payload(self) -> None:
        wrapper, record_path = self._record_wrapper("get", result="ok")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "inbox/test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        self.assertIn("get", calls)
        payload = json.loads(calls[1])
        self.assertEqual(payload["slug"], "inbox/test")

    # ------------------------------------------------------------------
    # capture
    # ------------------------------------------------------------------

    def test_capture_success(self) -> None:
        wrapper = self._ok_wrapper("capture", result="captured")
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "remember this"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("capture", data["action"])

    def test_capture_rejects_missing_content(self) -> None:
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill(self.module, {"action": "capture"}, env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("missing_content", data["error"])

    def test_capture_rejects_empty_content(self) -> None:
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "   "},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("empty_content", data["error"])

    def test_capture_rejects_too_long_content(self) -> None:
        wrapper = self._ok_wrapper("capture")
        long_content = "x" * 60000
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": long_content},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper), "GBRAIN_CONTENT_MAX_CHARS": "50000"},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("content_too_long", data["error"])

    def test_capture_with_slug_and_type(self) -> None:
        wrapper, record_path = self._record_wrapper("capture", result="ok")
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test content", "slug": "inbox/custom", "type": "note"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        self.assertIn("capture", calls)
        payload = json.loads(calls[1])
        self.assertEqual(payload["content"], "test content")
        self.assertEqual(payload["slug"], "inbox/custom")
        self.assertEqual(payload["type"], "note")

    def test_capture_with_only_type_not_treated_as_slug(self) -> None:
        """Blocker 1: capture with only type must not be treated as slug."""
        wrapper, record_path = self._record_wrapper("capture", result="ok")
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test content", "type": "diary"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["content"], "test content")
        self.assertEqual(payload["type"], "diary")
        # slug must NOT be present (or must be empty) — type must not be
        # misinterpreted as slug.
        self.assertNotIn("slug", payload)

    def test_capture_rejects_invalid_type(self) -> None:
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test", "type": "Invalid Type!"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_type", data["error"])

    def test_capture_rejects_traversal_slug(self) -> None:
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test", "slug": "../../etc/passwd"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_capture_write_through_failure_surfaces(self) -> None:
        """Blocker 2: write-through failure must not be unconditional success."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "capture",
                "slug": "inbox/test",
                "write_through": {"written": False, "error": "disk full"},
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("write_through_degraded", data["error"])

    def test_capture_write_through_written_false_surfaces(self) -> None:
        """Blocker 2: top-level written=false must surface."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "capture",
                "slug": "inbox/test",
                "written": False,
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("write_through_degraded", data["error"])

    def test_capture_write_through_skipped_surfaces(self) -> None:
        """Blocker 2: write_through.skipped must surface."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "capture",
                "slug": "inbox/test",
                "write_through": {"written": True, "skipped": True},
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("write_through_degraded", data["error"])

    def test_capture_write_through_ok_succeeds(self) -> None:
        """Blocker 2: successful write-through must still succeed."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "capture",
                "slug": "inbox/test",
                "write_through": {"written": True},
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)

    def test_capture_write_through_failure_with_stderr_noise(self) -> None:
        """Blocker fix: write_through.written=false must still be detected
        even when the wrapper output includes stderr noise."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "capture",
                "slug": "inbox/test",
                "write_through": {"written": False, "error": "disk full"},
                "stderr": "warning: some gbrain warning noise",
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("write_through_degraded", data["error"])

    def test_capture_written_false_with_stderr_noise(self) -> None:
        """Blocker fix: top-level written=false must still be detected
        even when the wrapper output includes stderr noise."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "capture",
                "slug": "inbox/test",
                "written": False,
                "stderr": "warning: some gbrain warning noise",
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("write_through_degraded", data["error"])

    # ------------------------------------------------------------------
    # put
    # ------------------------------------------------------------------

    def test_put_success(self) -> None:
        wrapper = self._ok_wrapper("put", result="written")
        rc, out = run_skill(
            self.module,
            {"action": "put", "slug": "inbox/my-note", "content": "---\ntitle: Test\n---\nBody"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("put", data["action"])

    def test_put_rejects_missing_slug(self) -> None:
        wrapper = self._ok_wrapper("put")
        rc, out = run_skill(
            self.module,
            {"action": "put", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_put_rejects_missing_content(self) -> None:
        wrapper = self._ok_wrapper("put")
        rc, out = run_skill(
            self.module,
            {"action": "put", "slug": "inbox/test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("missing_content", data["error"])

    def test_put_rejects_too_long_content(self) -> None:
        wrapper = self._ok_wrapper("put")
        long_content = "x" * 60000
        rc, out = run_skill(
            self.module,
            {"action": "put", "slug": "inbox/test", "content": long_content},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper), "GBRAIN_CONTENT_MAX_CHARS": "50000"},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("content_too_long", data["error"])

    def test_put_passes_json_payload(self) -> None:
        wrapper, record_path = self._record_wrapper("put", result="ok")
        rc, out = run_skill(
            self.module,
            {"action": "put", "slug": "inbox/test", "content": "body content"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        self.assertIn("put", calls)
        payload = json.loads(calls[1])
        self.assertEqual(payload["slug"], "inbox/test")
        self.assertEqual(payload["content"], "body content")

    def test_put_write_through_failure_surfaces(self) -> None:
        """Blocker 2: put write-through failure must not be unconditional success."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "put",
                "slug": "inbox/test",
                "write_through": {"written": False, "error": "permission denied"},
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "put", "slug": "inbox/test", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("write_through_degraded", data["error"])

    def test_put_write_through_ok_succeeds(self) -> None:
        """Blocker 2: successful write-through must still succeed."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "put",
                "slug": "inbox/test",
                "write_through": {"written": True},
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "put", "slug": "inbox/test", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)

    def test_put_write_through_failure_with_stderr_noise(self) -> None:
        """Blocker fix: write_through.written=false must still be detected
        even when the wrapper output includes stderr noise."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "put",
                "slug": "inbox/test",
                "write_through": {"written": False, "error": "permission denied"},
                "stderr": "warning: some gbrain warning noise",
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "put", "slug": "inbox/test", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("write_through_degraded", data["error"])

    def test_put_written_false_with_stderr_noise(self) -> None:
        """Blocker fix: top-level written=false must still be detected
        even when the wrapper output includes stderr noise."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({
                "success": True,
                "action": "put",
                "slug": "inbox/test",
                "written": False,
                "stderr": "warning: some gbrain warning noise",
            })
        )
        rc, out = run_skill(
            self.module,
            {"action": "put", "slug": "inbox/test", "content": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("write_through_degraded", data["error"])

    # ------------------------------------------------------------------
    # link
    # ------------------------------------------------------------------

    def test_link_success(self) -> None:
        wrapper = self._ok_wrapper("link", result="ok")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "inbox/a", "to": "people/b"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("link", data["action"])

    def test_link_rejects_missing_from(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "to": "people/b"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_from_slug", data["error"])

    def test_link_rejects_missing_to(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "inbox/a"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_to_slug", data["error"])

    def test_link_rejects_managed_source_markdown(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "b", "link_source": "markdown"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("managed_link_source", data["error"])

    def test_link_rejects_managed_source_frontmatter(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "b", "link_source": "frontmatter"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("managed_link_source", data["error"])

    def test_link_rejects_managed_source_mentions(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "b", "link_source": "mentions"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("managed_link_source", data["error"])

    def test_link_rejects_managed_source_wikilink_resolved(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "b", "link_source": "wikilink-resolved"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("managed_link_source", data["error"])

    def test_link_accepts_manual_source(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "b", "link_source": "manual"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)

    def test_link_accepts_custom_source(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "b", "link_source": "citation-graph"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)

    def test_link_rejects_invalid_link_type(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "b", "link_type": "Invalid Type!"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_link_type", data["error"])

    def test_link_passes_json_payload(self) -> None:
        wrapper, record_path = self._record_wrapper("link", result="ok")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "b", "link_type": "mentions", "context": "ctx", "link_source": "manual"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        self.assertIn("link", calls)
        payload = json.loads(calls[1])
        self.assertEqual(payload["from"], "a")
        self.assertEqual(payload["to"], "b")
        self.assertEqual(payload["link_type"], "mentions")
        self.assertEqual(payload["context"], "ctx")
        self.assertEqual(payload["link_source"], "manual")

    def test_link_with_only_link_source_not_treated_as_type(self) -> None:
        """Blocker 1: link with only link_source must not be treated as link_type."""
        wrapper, record_path = self._record_wrapper("link", result="ok")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "b", "link_source": "manual"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["from"], "a")
        self.assertEqual(payload["to"], "b")
        self.assertEqual(payload["link_source"], "manual")
        # link_type and context must NOT be present.
        self.assertNotIn("link_type", payload)
        self.assertNotIn("context", payload)

    # ------------------------------------------------------------------
    # backlinks
    # ------------------------------------------------------------------

    def test_backlinks_success(self) -> None:
        wrapper = self._ok_wrapper("backlinks", result=[])
        rc, out = run_skill(
            self.module,
            {"action": "backlinks", "slug": "people/b"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("backlinks", data["action"])

    def test_backlinks_rejects_missing_slug(self) -> None:
        wrapper = self._ok_wrapper("backlinks")
        rc, out = run_skill(self.module, {"action": "backlinks"}, env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    # ------------------------------------------------------------------
    # reindex is not exposed from chat
    # ------------------------------------------------------------------

    def test_reindex_action_is_not_exposed(self) -> None:
        wrapper = self._ok_wrapper("reindex")
        rc, out = run_skill(
            self.module,
            {"action": "reindex"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("unknown_action", data["error"])
        valid = data.get("valid_actions", [])
        self.assertIn("status", valid)
        self.assertIn("schema_status", valid)
        self.assertIn("search", valid)
        self.assertIn("get", valid)
        self.assertIn("capture", valid)
        self.assertIn("put", valid)
        self.assertIn("link", valid)
        self.assertIn("backlinks", valid)
        self.assertNotIn("reindex", valid)

    # ------------------------------------------------------------------
    # old note.* routes are rejected (both dotted and underscored)
    # ------------------------------------------------------------------

    def test_note_capture_underscored_rejected(self) -> None:
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill(
            self.module,
            {"action": "note_capture", "title": "test", "text": "body"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_capture_dotted_rejected(self) -> None:
        """Blocker 4: dotted note.capture must be rejected."""
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill(
            self.module,
            {"action": "note.capture", "title": "test", "text": "body"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_read_dotted_rejected(self) -> None:
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "note.read", "path": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_write_dotted_rejected(self) -> None:
        wrapper = self._ok_wrapper("put")
        rc, out = run_skill(
            self.module,
            {"action": "note.write", "path": "test", "content": "body"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_link_dotted_rejected(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "note.link", "from": "a", "to": "b"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_search_dotted_rejected(self) -> None:
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill(
            self.module,
            {"action": "note.search", "query": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_create_dotted_rejected(self) -> None:
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill(
            self.module,
            {"action": "note.create", "title": "test", "text": "body"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_update_dotted_rejected(self) -> None:
        wrapper = self._ok_wrapper("put")
        rc, out = run_skill(
            self.module,
            {"action": "note.update", "path": "test", "content": "body"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_rename_dotted_rejected(self) -> None:
        wrapper = self._ok_wrapper("put")
        rc, out = run_skill(
            self.module,
            {"action": "note.rename", "path": "test", "new_title": "new"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_file_dotted_rejected(self) -> None:
        wrapper = self._ok_wrapper("put")
        rc, out = run_skill(
            self.module,
            {"action": "note.file", "path": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_read_underscored_rejected(self) -> None:
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "note_read", "path": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_write_underscored_rejected(self) -> None:
        wrapper = self._ok_wrapper("put")
        rc, out = run_skill(
            self.module,
            {"action": "note_write", "path": "test", "content": "body"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_link_underscored_rejected(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "note_link", "from": "a", "to": "b"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_note_search_underscored_rejected(self) -> None:
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill(
            self.module,
            {"action": "note_search", "query": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_query_action_rejected(self) -> None:
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill(
            self.module,
            {"action": "query", "query": "test"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    # ------------------------------------------------------------------
    # strengthened slug validation
    # ------------------------------------------------------------------

    def test_slug_rejects_backslash(self) -> None:
        """Blocker 5: backslash must be rejected."""
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "foo\\bar"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_slug_rejects_url_encoded_dot(self) -> None:
        """Blocker 5: %2e (URL-encoded ..) must be rejected."""
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "foo%2e%2ebar"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_slug_rejects_url_encoded_slash(self) -> None:
        """Blocker 5: %2f (URL-encoded /) must be rejected."""
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "foo%2fbar"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_slug_rejects_url_encoded_backslash(self) -> None:
        """Blocker 5: %5c (URL-encoded \\) must be rejected."""
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "foo%5cbar"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_slug_rejects_control_char(self) -> None:
        """Blocker 5: control characters must be rejected."""
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "foo\x00bar"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_slug_rejects_bidi_override(self) -> None:
        """Blocker 5: bidi/RTL override characters must be rejected."""
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "foo\u202ebar"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_slug_rejects_too_long(self) -> None:
        """Blocker 5: overly long slugs must be rejected."""
        wrapper = self._ok_wrapper("get")
        rc, out = run_skill(
            self.module,
            {"action": "get", "slug": "x" * 600},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    def test_link_slug_rejects_backslash(self) -> None:
        """Blocker 5: link slugs get same validation."""
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "foo\\bar", "to": "b"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_from_slug", data["error"])

    def test_link_slug_rejects_url_encoded(self) -> None:
        """Blocker 5: link slugs reject URL-encoded traversal."""
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill(
            self.module,
            {"action": "link", "from": "a", "to": "foo%2e%2ebar"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_to_slug", data["error"])

    def test_capture_slug_rejects_backslash(self) -> None:
        """Blocker 5: capture slugs get same validation."""
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill(
            self.module,
            {"action": "capture", "content": "test", "slug": "foo\\bar"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_slug", data["error"])

    # ------------------------------------------------------------------
    # unknown action
    # ------------------------------------------------------------------

    def test_unknown_action_returns_error(self) -> None:
        wrapper = self._ok_wrapper("status")
        rc, out = run_skill(
            self.module,
            {"action": "frobnicate"},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("unknown_action", data["error"])

    def test_no_input_returns_usage(self) -> None:
        wrapper = self._ok_wrapper("status")
        rc, out = run_skill(self.module, "", env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("no_input", data["error"])

    def test_invalid_json_returns_error(self) -> None:
        wrapper = self._ok_wrapper("status")
        rc, out = run_skill(
            self.module,
            "not json at all",
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_json", data["error"])

    def test_valid_actions_listed_in_no_input(self) -> None:
        wrapper = self._ok_wrapper("status")
        rc, out = run_skill(self.module, "", env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        data = parse_json(out)
        actions = data.get("usage", {}).get("actions", [])
        self.assertIn("status", actions)
        self.assertIn("schema_status", actions)
        self.assertIn("search", actions)
        self.assertIn("get", actions)
        self.assertIn("capture", actions)
        self.assertIn("put", actions)
        self.assertIn("link", actions)
        self.assertIn("backlinks", actions)


class GbrainSkillCLITests(unittest.TestCase):
    """Tests for CLI argv mode (alternative to JSON stdin)."""

    def setUp(self) -> None:
        self._tempfiles: list[Path] = []
        self.module = load_skill_module()

    def tearDown(self) -> None:
        for p in self._tempfiles:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def _fake_wrapper(
        self,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        record: bool = False,
    ) -> Path:
        record_path = None
        if record:
            tf = tempfile.NamedTemporaryFile(suffix=".calls", delete=False)
            tf.close()
            record_path = Path(tf.name)
            self._tempfiles.append(record_path)
        return make_fake_wrapper(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            record_path=record_path,
        )

    def _ok_wrapper(self, action: str, **extra) -> Path:
        return self._fake_wrapper(stdout=json.dumps({"success": True, "action": action, **extra}))

    def _record_wrapper(self, action: str, **extra) -> tuple[Path, Path]:
        record = tempfile.NamedTemporaryFile(suffix=".calls", delete=False)
        record.close()
        record_path = Path(record.name)
        self._tempfiles.append(record_path)
        wrapper = make_fake_wrapper(
            stdout=json.dumps({"success": True, "action": action, **extra}),
            record_path=record_path,
        )
        return wrapper, record_path

    # ------------------------------------------------------------------
    # CLI mode: basic actions
    # ------------------------------------------------------------------

    def test_cli_status(self) -> None:
        wrapper = self._fake_wrapper(
            stdout=json.dumps({"success": True, "action": "status", "gate_open": True})
        )
        rc, out = run_skill_cli(["status"], env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("status", data["action"])

    def test_cli_schema_status_hyphen_mapping(self) -> None:
        """schema-status (hyphen) must map to schema_status (underscore)."""
        wrapper = self._fake_wrapper(
            stdout=json.dumps({"success": True, "action": "schema_status", "selected_pack": "josemar-user"})
        )
        rc, out = run_skill_cli(["schema-status"], env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)})
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("schema_status", data["action"])

    def test_cli_search(self) -> None:
        wrapper, record_path = self._record_wrapper("search", result="ok")
        rc, out = run_skill_cli(
            ["search", "rodrigo green wall", "--limit", "3"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("search", data["action"])
        # Verify the wrapper received the JSON payload with correct fields.
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["query"], "rodrigo green wall")
        self.assertEqual(payload["limit"], 3)

    def test_cli_search_with_offset(self) -> None:
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill_cli(
            ["search", "test query", "--limit", "5", "--offset", "10"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)

    def test_cli_get(self) -> None:
        wrapper, record_path = self._record_wrapper("get", result="page content")
        rc, out = run_skill_cli(
            ["get", "people/rodrigo-green-wall-cerrado"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["slug"], "people/rodrigo-green-wall-cerrado")

    def test_cli_capture(self) -> None:
        wrapper, record_path = self._record_wrapper("capture", result="ok")
        rc, out = run_skill_cli(
            ["capture", "--slug", "inbox/my-note", "--type", "note", "--content", "remember to follow up"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["content"], "remember to follow up")
        self.assertEqual(payload["slug"], "inbox/my-note")
        self.assertEqual(payload["type"], "note")

    def test_cli_capture_content_via_stdin(self) -> None:
        """capture can read content from stdin when --content is not provided."""
        wrapper, record_path = self._record_wrapper("capture", result="ok")
        rc, out = run_skill_cli(
            ["capture", "--slug", "inbox/my-note"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
            stdin="content from stdin",
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["content"], "content from stdin")

    def test_cli_capture_prefers_content_flag_over_stdin(self) -> None:
        """If both --content and stdin are present, --content wins."""
        wrapper, record_path = self._record_wrapper("capture", result="ok")
        rc, out = run_skill_cli(
            ["capture", "--content", "from flag"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
            stdin="from stdin",
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["content"], "from flag")

    def test_cli_capture_missing_content_error(self) -> None:
        """capture without --content or stdin must return invalid_args."""
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill_cli(
            ["capture", "--slug", "inbox/test"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_args", data["error"])

    def test_cli_put(self) -> None:
        wrapper, record_path = self._record_wrapper("put", result="ok")
        rc, out = run_skill_cli(
            ["put", "people/some-person", "--content", "full markdown here"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["slug"], "people/some-person")
        self.assertEqual(payload["content"], "full markdown here")

    def test_cli_put_content_via_stdin(self) -> None:
        """put can read content from stdin when --content is not provided."""
        wrapper, record_path = self._record_wrapper("put", result="ok")
        rc, out = run_skill_cli(
            ["put", "people/some-person"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
            stdin="content via stdin",
        )
        self.assertEqual(0, rc, out)
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["content"], "content via stdin")

    def test_cli_put_missing_slug_error(self) -> None:
        wrapper = self._ok_wrapper("put")
        rc, out = run_skill_cli(
            ["put", "--content", "test"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_args", data["error"])

    def test_cli_link(self) -> None:
        wrapper, record_path = self._record_wrapper("link", result="ok")
        rc, out = run_skill_cli(
            ["link", "people/a", "people/b", "--link-type", "mentions", "--context", "meeting notes"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["from"], "people/a")
        self.assertEqual(payload["to"], "people/b")
        self.assertEqual(payload["link_type"], "mentions")
        self.assertEqual(payload["context"], "meeting notes")

    def test_cli_link_with_source(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill_cli(
            ["link", "a", "b", "--link-source", "manual"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)

    def test_cli_link_missing_args_error(self) -> None:
        wrapper = self._ok_wrapper("link")
        rc, out = run_skill_cli(
            ["link", "a"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_args", data["error"])

    def test_cli_backlinks(self) -> None:
        wrapper, record_path = self._record_wrapper("backlinks", result=[])
        rc, out = run_skill_cli(
            ["backlinks", "people/elton-bora"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        calls = record_path.read_text(encoding="utf-8").splitlines()
        payload = json.loads(calls[1])
        self.assertEqual(payload["slug"], "people/elton-bora")

    # ------------------------------------------------------------------
    # CLI mode: error handling
    # ------------------------------------------------------------------

    def test_cli_unknown_action_error(self) -> None:
        wrapper = self._ok_wrapper("status")
        rc, out = run_skill_cli(
            ["frobnicate"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        # Unknown action from CLI goes through _dispatch which returns unknown_action.
        self.assertEqual("unknown_action", data["error"])

    def test_cli_invalid_args_envelope(self) -> None:
        """Missing required positional must return invalid_args error."""
        wrapper = self._ok_wrapper("search")
        rc, out = run_skill_cli(
            ["search"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("invalid_args", data["error"])

    def test_cli_rejected_action(self) -> None:
        """Old note.* route names must be rejected in CLI mode too."""
        wrapper = self._ok_wrapper("capture")
        rc, out = run_skill_cli(
            ["note.capture", "--content", "test"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("rejected_action", data["error"])

    def test_cli_reindex_not_exposed(self) -> None:
        """reindex must not be exposed from CLI mode."""
        wrapper = self._ok_wrapper("reindex")
        rc, out = run_skill_cli(
            ["reindex"],
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("unknown_action", data["error"])

    # ------------------------------------------------------------------
    # JSON stdin mode regression
    # ------------------------------------------------------------------

    def test_json_stdin_mode_still_works(self) -> None:
        """JSON stdin mode must remain 100% backward-compatible."""
        wrapper = self._ok_wrapper("search", result="ok")
        rc, out = run_skill(
            self.module,
            {"action": "search", "query": "test query", "limit": 5},
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertEqual(0, rc, out)
        data = parse_json(out)
        self.assertTrue(data["success"])
        self.assertEqual("search", data["action"])

    def test_json_stdin_no_input_shows_usage(self) -> None:
        """No argv + no stdin must show usage (existing behavior)."""
        wrapper = self._ok_wrapper("status")
        rc, out = run_skill(
            self.module,
            "",
            env={"JOSEMAR_GBRAIN_WRAPPER": str(wrapper)},
        )
        self.assertNotEqual(0, rc)
        data = parse_json(out)
        self.assertFalse(data["success"])
        self.assertEqual("no_input", data["error"])


if __name__ == "__main__":
    unittest.main()