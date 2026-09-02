"""Focused tests for the state-owned command-allowlist sidecars (issue #151 W1).

These tests exercise the command-allowlist state family added to
``scripts/josemar_skill_state.py`` plus the init ownership repair. They do
NOT require Docker or the Hermes venv: the runtime-config projection path
uses PyYAML, which is available in the repo's dev venv.

Contract under test:
- Profile-aware sidecars exactly ``hermes/command-allowlist/default.json``
  and ``hermes/command-allowlist/profiles/<canonical>.json``.
- Strict v1 schema exactly ``{"version": 1, "command_allowlist": [...]}``:
  BOTH keys required; wrong version, unknown keys, wrong types, missing
  keys, and empty/non-string entries are rejected. Canonical
  serialization is sorted and deduped.
- Runtime projection is the ROOT-LEVEL ``config["command_allowlist"]``
  key (pinned Hermes v2026.8.18), never ``skills.command_allowlist``.
- Presence semantics: an explicit ``[]`` is authoritative (runtime
  ``command_allowlist`` stays durably empty) while an ABSENT sidecar
  removes the runtime key.
- No allowlist contents ever appear in errors or statuses.
- Migration (before the template overwrite): non-empty runtime value
  only, absent sidecar only, never overwrites.
- Stateful helpers (W2 wiring surface): ``save_command_allowlist_stateful``
  writes the sidecar first, then the raw runtime config, under the shared
  lock; ``clear_command_allowlist_stateful`` deletes the sidecar first,
  then removes only the root key and persists natively. State failures
  fail the save; runtime failures afterward propagate for the next
  reconcile repair.
- Reconciliation prevalidates EVERY present allowlist sidecar before any
  runtime config write in an apply cycle (fail-closed, no partial apply)
  and preserves unrelated top-level and ``skills`` fields plus existing
  toggle/policy/models behavior.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = REPO_ROOT / "scripts" / "josemar_skill_state.py"
INIT_PATH = REPO_ROOT / "docker-hermes-init.sh"


def _load_helper():
    spec = importlib.util.spec_from_file_location("josemar_skill_state", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_yaml() -> bool:
    try:
        import yaml  # noqa: F401

        return True
    except ImportError:
        return False


_requires_yaml = unittest.skipUnless(_has_yaml(), "PyYAML not available")


class WorkspaceTestCase(unittest.TestCase):
    """Base: isolated temporary workspace with env wired."""

    def setUp(self) -> None:
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"WORKSPACE_DIR": str(self.workspace)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- convenience builders ------------------------------------------------

    def _allowlist_sidecar(self, profile: str | None = None) -> Path:
        if profile in (None, "default"):
            return self.workspace / "hermes" / "command-allowlist" / "default.json"
        return (
            self.workspace
            / "hermes"
            / "command-allowlist"
            / "profiles"
            / f"{profile}.json"
        )

    def _write_allowlist(self, items: list, profile: str | None = None) -> Path:
        path = self._allowlist_sidecar(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.m.write_command_allowlist_sidecar(
            path, {"version": 1, "command_allowlist": items}
        )
        return path

    def _write_config(self, rel: str, content: str) -> Path:
        path = self.workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _load_config(self, path: Path) -> dict:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Strict v1 schema validation
# ---------------------------------------------------------------------------


class AllowlistSchemaValidationTests(unittest.TestCase):
    """validate_command_allowlist_state accepts valid, rejects invalid."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_valid_minimal_document(self) -> None:
        result = self.m.validate_command_allowlist_state(
            {"version": 1, "command_allowlist": ["a"]}
        )
        self.assertEqual(result, {"version": 1, "command_allowlist": ["a"]})

    def test_valid_explicit_empty_list(self) -> None:
        result = self.m.validate_command_allowlist_state(
            {"version": 1, "command_allowlist": []}
        )
        self.assertEqual(result, {"version": 1, "command_allowlist": []})

    def test_rejects_missing_command_allowlist_key(self) -> None:
        # The exact v1 schema requires BOTH keys: a missing allowlist key
        # is malformed, not an implicit empty allowlist.
        with self.assertRaises(ValueError) as cm:
            self.m.validate_command_allowlist_state({"version": 1})
        self.assertIn("command_allowlist", str(cm.exception))

    def test_rejects_null_command_allowlist(self) -> None:
        # Explicit JSON null is a wrong type, not an implicit empty list.
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state(
                {"version": 1, "command_allowlist": None}
            )

    def test_rejects_missing_version(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state({"command_allowlist": []})

    def test_rejects_wrong_version(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_command_allowlist_state(
                {"version": 2, "command_allowlist": []}
            )
        self.assertIn("unsupported", str(cm.exception))

    def test_rejects_string_version(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state(
                {"version": "1", "command_allowlist": []}
            )

    def test_rejects_bool_version(self) -> None:
        # bool is an int subclass; strict typing must reject it.
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state(
                {"version": True, "command_allowlist": []}
            )

    def test_rejects_non_mapping_root(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state(["a", "b"])

    def test_rejects_unknown_top_level_key(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_command_allowlist_state(
                {"version": 1, "command_allowlist": [], "bogus": True}
            )
        self.assertIn("unknown key", str(cm.exception))

    def test_rejects_toggle_schema_keys(self) -> None:
        # The skill-toggle keys are unknown keys here (no schema mixing).
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state(
                {"version": 1, "command_allowlist": [], "disabled": ["x"]}
            )

    def test_rejects_non_list_allowlist(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state(
                {"version": 1, "command_allowlist": "a"}
            )

    def test_rejects_non_string_entry(self) -> None:
        for bad in (5, None, True, {"cmd": 1}):
            with self.assertRaises(ValueError):
                self.m.validate_command_allowlist_state(
                    {"version": 1, "command_allowlist": ["ok", bad]}
                )

    def test_rejects_empty_string_entry(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state(
                {"version": 1, "command_allowlist": ["ok", ""]}
            )

    def test_rejects_whitespace_only_entry(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state(
                {"version": 1, "command_allowlist": ["  "]}
            )

    def test_error_messages_do_not_contain_values(self) -> None:
        secret = "TOPSECRET-command-value"
        with self.assertRaises(ValueError) as cm:
            self.m.validate_command_allowlist_state(
                {"version": 1, "command_allowlist": [secret, ""]}
            )
        self.assertNotIn(secret, str(cm.exception))


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------


class AllowlistCanonicalSerializationTests(unittest.TestCase):
    """Sorted/deduped canonical single-line JSON."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_sorted_output(self) -> None:
        result = self.m.validate_command_allowlist_state(
            {"version": 1, "command_allowlist": ["b", "a"]}
        )
        self.assertEqual(result["command_allowlist"], ["a", "b"])

    def test_deduped_output(self) -> None:
        result = self.m.validate_command_allowlist_state(
            {"version": 1, "command_allowlist": ["a", "a", "b", "a"]}
        )
        self.assertEqual(result["command_allowlist"], ["a", "b"])

    def test_canonical_line_is_exact(self) -> None:
        line = self.m.serialize_command_allowlist_state(
            {"version": 1, "command_allowlist": ["b", "a"]}
        )
        self.assertEqual(line, '{"version":1,"command_allowlist":["a","b"]}')

    def test_serialize_is_one_line(self) -> None:
        line = self.m.serialize_command_allowlist_state(
            {"version": 1, "command_allowlist": ["a"]}
        )
        self.assertEqual(line.count("\n"), 0)

    def test_serialize_rejects_wrong_version(self) -> None:
        with self.assertRaises(ValueError):
            self.m.serialize_command_allowlist_state(
                {"version": 2, "command_allowlist": []}
            )

    def test_serialize_rejects_missing_allowlist_key(self) -> None:
        with self.assertRaises(ValueError):
            self.m.serialize_command_allowlist_state({"version": 1})

    def test_serialize_rejects_unknown_key(self) -> None:
        with self.assertRaises(ValueError):
            self.m.serialize_command_allowlist_state(
                {"version": 1, "command_allowlist": [], "extra": 1}
            )

    def test_serialize_rejects_non_string_entry(self) -> None:
        with self.assertRaises(ValueError):
            self.m.serialize_command_allowlist_state(
                {"version": 1, "command_allowlist": [7]}
            )

    def test_roundtrip_serialize_then_text_validate(self) -> None:
        state = {"version": 1, "command_allowlist": ["b", "a", "b"]}
        line = self.m.serialize_command_allowlist_state(state)
        parsed = self.m.validate_command_allowlist_state_from_text(line)
        self.assertEqual(parsed, {"version": 1, "command_allowlist": ["a", "b"]})


# ---------------------------------------------------------------------------
# Canonical text validator (workspace_sync reuse surface)
# ---------------------------------------------------------------------------


class AllowlistTextValidatorTests(unittest.TestCase):
    """validate_command_allowlist_state_from_text is the single text entry."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_valid_text(self) -> None:
        result = self.m.validate_command_allowlist_state_from_text(
            '{"version":1,"command_allowlist":["a"]}'
        )
        self.assertEqual(result, {"version": 1, "command_allowlist": ["a"]})

    def test_empty_text_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state_from_text("")

    def test_whitespace_text_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state_from_text("  \n ")

    def test_malformed_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state_from_text("{not json")

    def test_missing_allowlist_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state_from_text('{"version":1}')

    def test_wrong_version_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state_from_text(
                '{"version":2,"command_allowlist":[]}'
            )

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state_from_text(
                '{"version":1,"command_allowlist":[],"nope":1}'
            )

    def test_non_string_entry_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_command_allowlist_state_from_text(
                '{"version":1,"command_allowlist":[3]}'
            )

    def test_read_sidecar_uses_same_rules(self) -> None:
        # The read path must not duplicate or weaken the schema rules.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "default.json"
            path.write_text('{"version":1,"command_allowlist":[],"bogus":1}', encoding="utf-8")
            with self.assertRaises(ValueError):
                self.m.read_command_allowlist_sidecar(path)


# ---------------------------------------------------------------------------
# Hermes-home profile resolution
# ---------------------------------------------------------------------------


class AllowlistPathMappingTests(WorkspaceTestCase):
    """Exact sidecar paths and profile isolation."""

    def test_default_profile_maps_to_command_allowlist_default_json(self) -> None:
        path = self.m.resolve_allowlist_sidecar_for_profile(None)
        self.assertEqual(
            path,
            self.workspace / "hermes" / "command-allowlist" / "default.json",
        )

    def test_named_profile_maps_to_profiles_dir(self) -> None:
        path = self.m.resolve_allowlist_sidecar_for_profile("coder")
        self.assertEqual(
            path,
            self.workspace
            / "hermes"
            / "command-allowlist"
            / "profiles"
            / "coder.json",
        )

    def test_hermes_home_workspace_root_maps_to_default(self) -> None:
        path = self.m.resolve_allowlist_sidecar_for_hermes_home(self.workspace)
        self.assertEqual(path.name, "default.json")
        self.assertEqual(path.parent.name, "command-allowlist")

    def test_hermes_home_named_profile_maps_to_profiles(self) -> None:
        profile_home = self.workspace / "profiles" / "coder"
        profile_home.mkdir(parents=True)
        path = self.m.resolve_allowlist_sidecar_for_hermes_home(profile_home)
        self.assertEqual(path.name, "coder.json")
        self.assertEqual(path.parent.name, "profiles")

    def test_hermes_home_other_path_rejected(self) -> None:
        other = self.workspace / "somewhere" / "else"
        other.mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.m.resolve_allowlist_sidecar_for_hermes_home(other)

    def test_invalid_profile_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.m.resolve_allowlist_sidecar_for_profile("Bad Name!")

    def test_profile_isolation_distinct_paths(self) -> None:
        default = self.m.resolve_allowlist_sidecar_for_profile(None)
        coder = self.m.resolve_allowlist_sidecar_for_profile("coder")
        ops = self.m.resolve_allowlist_sidecar_for_profile("ops")
        self.assertNotEqual(default, coder)
        self.assertNotEqual(coder, ops)
        # Toggle and allowlist families are distinct trees.
        toggle_default = self.m.resolve_sidecar_for_profile(None)
        self.assertNotEqual(default, toggle_default)
        self.assertIn("command-allowlist", str(default))
        self.assertIn("skill-toggles", str(toggle_default))


# ---------------------------------------------------------------------------
# Runtime config projection (ROOT-LEVEL key)
# ---------------------------------------------------------------------------


class AllowlistConfigProjectionTests(unittest.TestCase):
    """extract/apply/remove on the ROOT key, preserving unrelated fields."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_extract_canonicalizes(self) -> None:
        config = {"command_allowlist": ["b", "a", "b"]}
        self.assertEqual(
            self.m.extract_command_allowlist_from_config(config), ["a", "b"]
        )

    def test_extract_absent_returns_empty(self) -> None:
        self.assertEqual(self.m.extract_command_allowlist_from_config({}), [])
        # A skills section must never be consulted for the allowlist.
        self.assertEqual(
            self.m.extract_command_allowlist_from_config(
                {"skills": {"command_allowlist": ["wrong-spot"]}}
            ),
            [],
        )

    def test_extract_non_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.extract_command_allowlist_from_config({"command_allowlist": "x"})

    def test_extract_non_string_entry_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.extract_command_allowlist_from_config(
                {"command_allowlist": ["a", 4]}
            )

    def test_has_command_allowlist(self) -> None:
        self.assertTrue(
            self.m.config_has_command_allowlist({"command_allowlist": []})
        )
        self.assertFalse(
            self.m.config_has_command_allowlist(
                {"skills": {"command_allowlist": []}}
            )
        )

    def test_apply_sets_root_key_preserving_unrelated(self) -> None:
        config = {
            "model": {"default": "x"},
            "command_allowlist": ["old"],
            "skills": {"external_dirs": ["/a"], "disabled": ["d"]},
            "memory": {"nudge_interval": 10},
        }
        self.m.apply_command_allowlist_to_config(config, ["new"])
        self.assertEqual(config["command_allowlist"], ["new"])
        # Unrelated top-level and skills fields survive untouched.
        self.assertEqual(config["model"], {"default": "x"})
        self.assertEqual(config["skills"], {"external_dirs": ["/a"], "disabled": ["d"]})
        self.assertEqual(config["memory"], {"nudge_interval": 10})

    def test_apply_explicit_empty_keeps_empty_list(self) -> None:
        config = {"command_allowlist": ["old"]}
        self.m.apply_command_allowlist_to_config(config, [])
        self.assertEqual(config["command_allowlist"], [])

    def test_apply_on_minimal_config_sets_root_key(self) -> None:
        config = {"model": {"default": "x"}}
        self.m.apply_command_allowlist_to_config(config, ["a"])
        self.assertEqual(config["command_allowlist"], ["a"])
        self.assertNotIn("skills", config)
        self.assertEqual(config["model"], {"default": "x"})

    def test_remove_deletes_root_key_returns_true(self) -> None:
        config = {
            "command_allowlist": ["a"],
            "model": {"default": "x"},
            "skills": {"disabled": ["d"]},
        }
        self.assertTrue(self.m.remove_command_allowlist_from_config(config))
        self.assertNotIn("command_allowlist", config)
        self.assertEqual(config["model"], {"default": "x"})
        self.assertEqual(config["skills"], {"disabled": ["d"]})

    def test_remove_absent_key_returns_false(self) -> None:
        config = {"skills": {"disabled": ["d"]}}
        self.assertFalse(self.m.remove_command_allowlist_from_config(config))
        self.assertEqual(config, {"skills": {"disabled": ["d"]}})

    def test_remove_on_minimal_config_returns_false(self) -> None:
        config = {"model": {"default": "x"}}
        self.assertFalse(self.m.remove_command_allowlist_from_config(config))
        self.assertEqual(config, {"model": {"default": "x"}})


# ---------------------------------------------------------------------------
# Sidecar atomic I/O
# ---------------------------------------------------------------------------


class AllowlistSidecarIOTests(WorkspaceTestCase):
    """write/read roundtrip through the shared atomic writer."""

    def test_write_then_read_roundtrip(self) -> None:
        path = self._write_allowlist(["b", "a"])
        state = self.m.read_command_allowlist_sidecar(path)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state, {"version": 1, "command_allowlist": ["a", "b"]})

    def test_read_absent_returns_none(self) -> None:
        self.assertIsNone(self.m.read_command_allowlist_sidecar(self._allowlist_sidecar()))

    def test_file_content_is_canonical_single_line(self) -> None:
        path = self._write_allowlist(["b", "a", "a"])
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text, '{"version":1,"command_allowlist":["a","b"]}\n')

    def test_write_is_atomic_no_temp_left(self) -> None:
        path = self._write_allowlist(["a"])
        temps = list(path.parent.glob(".*.tmp"))
        self.assertEqual(temps, [])

    def test_write_preserves_mode(self) -> None:
        path = self._write_allowlist(["a"])
        os.chmod(path, 0o600)
        self.m.write_command_allowlist_sidecar(
            path, {"version": 1, "command_allowlist": ["a"]}
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_malformed_read_raises(self) -> None:
        path = self._allowlist_sidecar()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.m.read_command_allowlist_sidecar(path)


# ---------------------------------------------------------------------------
# Migration (pre template-overwrite; absent sidecar only; never overwrites)
# ---------------------------------------------------------------------------


@_requires_yaml
class AllowlistMigrationTests(WorkspaceTestCase):
    """migrate_existing_command_allowlist_to_absent_sidecar contract."""

    def _write_runtime_config(self, content: str) -> Path:
        return self._write_config("config.yaml", content)

    def test_migrate_creates_sidecar_when_runtime_non_empty(self) -> None:
        config_path = self._write_runtime_config(
            "command_allowlist: ['b', 'a', 'a']\n"
        )
        created = self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            config_path, self.workspace
        )
        self.assertTrue(created)
        state = self.m.read_command_allowlist_sidecar(self._allowlist_sidecar())
        assert state is not None
        self.assertEqual(state["command_allowlist"], ["a", "b"])

    def test_migrate_noop_when_runtime_absent(self) -> None:
        # A skills-section key is unrelated state and must not migrate.
        config_path = self._write_runtime_config(
            "skills:\n  command_allowlist: ['wrong-spot']\n"
        )
        created = self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            config_path, self.workspace
        )
        self.assertFalse(created)
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_migrate_noop_when_runtime_empty(self) -> None:
        # Non-empty runtime value only: an empty runtime allowlist must not
        # create an authoritative-empty sidecar.
        config_path = self._write_runtime_config("command_allowlist: []\n")
        created = self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            config_path, self.workspace
        )
        self.assertFalse(created)
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_migrate_never_overwrites_existing_sidecar(self) -> None:
        self._write_allowlist(["kept"])
        config_path = self._write_runtime_config(
            "command_allowlist: ['runtime-value']\n"
        )
        created = self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            config_path, self.workspace
        )
        self.assertFalse(created)
        state = self.m.read_command_allowlist_sidecar(self._allowlist_sidecar())
        assert state is not None
        self.assertEqual(state["command_allowlist"], ["kept"])

    def test_migrate_noop_when_config_missing(self) -> None:
        created = self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            self.workspace / "config.yaml", self.workspace
        )
        self.assertFalse(created)
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_migrate_preserves_profile_isolation(self) -> None:
        profile_home = self.workspace / "profiles" / "coder"
        profile_config = profile_home / "config.yaml"
        profile_home.mkdir(parents=True)
        profile_config.write_text("command_allowlist: ['p1']\n", encoding="utf-8")
        created = self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            profile_config, profile_home
        )
        self.assertTrue(created)
        self.assertFalse(self._allowlist_sidecar().exists())
        coder_state = self.m.read_command_allowlist_sidecar(
            self._allowlist_sidecar("coder")
        )
        assert coder_state is not None
        self.assertEqual(coder_state["command_allowlist"], ["p1"])

    def test_migrate_malformed_runtime_value_fails_closed(self) -> None:
        config_path = self._write_runtime_config("command_allowlist: ['a', 5]\n")
        with self.assertRaises(ValueError):
            self.m.migrate_existing_command_allowlist_to_absent_sidecar(
                config_path, self.workspace
            )
        self.assertFalse(self._allowlist_sidecar().exists())


@_requires_yaml
class AllowlistMigrateCliTests(WorkspaceTestCase):
    """The init-invoked `migrate` CLI now covers both state families."""

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        return subprocess.run(
            [sys.executable, str(HELPER_PATH), "migrate", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_migrate_creates_allowlist_sidecar(self) -> None:
        self._write_config("config.yaml", "command_allowlist: ['x', 'w']\n")
        proc = self._run_cli("--hermes-home", str(self.workspace))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("created", proc.stdout)
        state = self.m.read_command_allowlist_sidecar(self._allowlist_sidecar())
        assert state is not None
        self.assertEqual(state["command_allowlist"], ["w", "x"])

    def test_cli_migrate_covers_both_families_independently(self) -> None:
        # Only a root allowlist present (no toggle keys): allowlist migrates.
        self._write_config("config.yaml", "command_allowlist: ['x']\n")
        proc = self._run_cli("--hermes-home", str(self.workspace))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._allowlist_sidecar().exists())
        self.assertFalse(
            (self.workspace / "hermes" / "skill-toggles" / "default.json").exists()
        )

    def test_cli_migrate_never_overwrites(self) -> None:
        self._write_allowlist(["kept"])
        self._write_config("config.yaml", "command_allowlist: ['runtime']\n")
        proc = self._run_cli("--hermes-home", str(self.workspace))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.m.read_command_allowlist_sidecar(self._allowlist_sidecar())
        assert state is not None
        self.assertEqual(state["command_allowlist"], ["kept"])

    def test_cli_migrate_allowlist_failure_exits_nonzero(self) -> None:
        # Fail-closed: a malformed runtime allowlist must make the CLI
        # exit nonzero (init aborts before the template overwrite) and
        # the error must not contain allowlist contents.
        self._write_config("config.yaml", "command_allowlist: ['SECRET-cmd', 5]\n")
        proc = self._run_cli("--hermes-home", str(self.workspace))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("command-allowlist migration failed", proc.stderr)
        self.assertNotIn("SECRET-cmd", proc.stderr)
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_cli_migrate_toggle_failure_alone_still_exits_zero(self) -> None:
        # Historical toggle behavior preserved: a toggle migration failure
        # (non-mapping skills section) warns and continues (exit 0).
        self._write_config("config.yaml", "skills: ['not', 'a', 'mapping']\n")
        proc = self._run_cli("--hermes-home", str(self.workspace))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("warning: toggle migration failed", proc.stderr)
        self.assertFalse(self._allowlist_sidecar().exists())


# ---------------------------------------------------------------------------
# One-time migration marker (strict v1, value-free, monotonic)
# ---------------------------------------------------------------------------


class MigrationMarkerSchemaTests(unittest.TestCase):
    """validate_migration_marker: exact schema, value-free errors."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_valid_marker(self) -> None:
        result = self.m.validate_migration_marker(
            {"version": 1, "legacy_runtime_import_complete": True}
        )
        self.assertEqual(
            result, {"version": 1, "legacy_runtime_import_complete": True}
        )

    def test_rejects_missing_version(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_migration_marker({"legacy_runtime_import_complete": True})

    def test_rejects_wrong_version(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_migration_marker(
                {"version": 2, "legacy_runtime_import_complete": True}
            )
        self.assertIn("unsupported", str(cm.exception))

    def test_rejects_bool_version(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_migration_marker(
                {"version": True, "legacy_runtime_import_complete": True}
            )

    def test_rejects_string_version(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_migration_marker(
                {"version": "1", "legacy_runtime_import_complete": True}
            )

    def test_rejects_missing_complete_key(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_migration_marker({"version": 1})

    def test_rejects_complete_not_true(self) -> None:
        for bad in (False, 1, "yes", None, []):
            with self.assertRaises(ValueError):
                self.m.validate_migration_marker(
                    {"version": 1, "legacy_runtime_import_complete": bad}
                )

    def test_rejects_unknown_keys(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_migration_marker(
                {
                    "version": 1,
                    "legacy_runtime_import_complete": True,
                    "bogus": 1,
                }
            )

    def test_rejects_non_mapping_root(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_migration_marker([1])

    def test_rejects_command_values_present(self) -> None:
        # The marker must be value-free: an attempt to carry command values
        # is a schema violation (unknown keys).
        with self.assertRaises(ValueError):
            self.m.validate_migration_marker(
                {
                    "version": 1,
                    "legacy_runtime_import_complete": True,
                    "command_allowlist": ["a"],
                }
            )

    def test_wrong_version_error_never_contains_marker_value(self) -> None:
        # Secret-like (and other malformed) wrong versions must be rejected
        # fail-closed WITHOUT ever being echoed into the error text.
        secret = "TOPSECRET-marker-version"
        bad_versions: list = [
            secret,
            999,
            "2",
            True,
            1.5,
            ["2"],
            {"v": 2},
            None,  # missing version key
        ]
        for bad in bad_versions:
            with self.subTest(bad_type=type(bad).__name__):
                doc: dict = {"legacy_runtime_import_complete": True}
                if bad is not None:
                    doc["version"] = bad
                with self.assertRaises(ValueError) as cm:
                    self.m.validate_migration_marker(doc)
                message = str(cm.exception)
                self.assertNotIn(secret, message)
                # Structural diagnostic retained (fixed schema key name).
                self.assertIn("version", message)

    def test_unknown_key_error_never_contains_key_name(self) -> None:
        # Unknown marker keys are counted, never named: a secret-like key
        # name (or value) must be rejected fail-closed without appearing
        # in the error.
        secret_key = "TOPSECRET-key-name"
        with self.assertRaises(ValueError) as cm:
            self.m.validate_migration_marker(
                {
                    "version": 1,
                    "legacy_runtime_import_complete": True,
                    secret_key: "TOPSECRET-value",
                }
            )
        message = str(cm.exception)
        # Structural diagnostic retained, but the raw key is never named.
        self.assertIn("unknown key", message)
        self.assertNotIn(secret_key, message)
        self.assertNotIn("TOPSECRET-value", message)

    def test_combined_malformed_marker_is_value_free(self) -> None:
        # Wrong version AND an unknown secret key: still fail-closed, and
        # neither marker-controlled string appears in the error.
        secret_version = "TOPSECRET-version-9"
        secret_key = "TOPSECRET-key-name"
        with self.assertRaises(ValueError) as cm:
            self.m.validate_migration_marker(
                {
                    "version": secret_version,
                    "legacy_runtime_import_complete": True,
                    secret_key: 1,
                }
            )
        message = str(cm.exception)
        self.assertNotIn(secret_version, message)
        self.assertNotIn(secret_key, message)

    def test_text_validator_error_is_value_free(self) -> None:
        # Sync-facing text entry point (W2 import surface): well-formed
        # JSON with a secret-like wrong version must be rejected without
        # leaking the value.
        secret = "TOPSECRET-text-version"
        text = json.dumps(
            {"version": secret, "legacy_runtime_import_complete": True}
        )
        with self.assertRaises(ValueError) as cm:
            self.m.validate_migration_marker_from_text(text)
        message = str(cm.exception)
        self.assertNotIn(secret, message)
        # Structural diagnostic retained (fixed schema key name).
        self.assertIn("version", message)

    def test_canonical_line_is_exact(self) -> None:
        line = self.m.serialize_migration_marker()
        self.assertEqual(
            line, '{"version":1,"legacy_runtime_import_complete":true}'
        )
        self.assertEqual(line.count("\n"), 0)


class MigrationMarkerIOTests(WorkspaceTestCase):
    """read/finalize marker behavior."""

    def _marker(self) -> Path:
        return self.workspace / "hermes" / "command-allowlist" / "migration-v1.json"

    def test_read_absent_returns_none(self) -> None:
        self.assertIsNone(self.m.read_migration_marker(self._marker()))

    def test_read_valid(self) -> None:
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text(
            '{"version":1,"legacy_runtime_import_complete":true}\n', encoding="utf-8"
        )
        state = self.m.read_migration_marker(self._marker())
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(
            state, {"version": 1, "legacy_runtime_import_complete": True}
        )

    def test_read_malformed_raises(self) -> None:
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.m.read_migration_marker(self._marker())

    def test_read_unreadable_raises(self) -> None:
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text('{"version":1,"legacy_runtime_import_complete":true}\n', encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=OSError("unreadable")):
            with self.assertRaises(ValueError):
                self.m.read_migration_marker(self._marker())

    def test_finalize_writes_canonical_marker(self) -> None:
        self.m.finalize_migration_marker()
        self.assertEqual(
            self._marker().read_text(encoding="utf-8"),
            '{"version":1,"legacy_runtime_import_complete":true}\n',
        )

    def test_finalize_uses_shared_lock(self) -> None:
        self.m.finalize_migration_marker()
        lock_file = (
            self.workspace / "hermes" / "skill-toggles" / ".skill-toggles.lock"
        )
        self.assertTrue(lock_file.exists())

    def test_finalize_validates_present_marker(self) -> None:
        # A malformed present marker is fatal, not silently overwritten.
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.m.finalize_migration_marker()
        # Original (malformed) content untouched by the failed finalize.
        self.assertEqual(self._marker().read_text(encoding="utf-8"), "{broken")

    def test_finalize_write_failure_raises(self) -> None:
        with mock.patch.object(
            self.m, "_atomic_write", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.m.finalize_migration_marker()
        self.assertFalse(self._marker().exists())


class MigrationMarkerCliTests(WorkspaceTestCase):
    """marker-present / finalize-migration-marker CLI subcommands."""

    def _marker(self) -> Path:
        return self.workspace / "hermes" / "command-allowlist" / "migration-v1.json"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        return subprocess.run(
            [sys.executable, str(HELPER_PATH), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_marker_present_exit_one_when_absent(self) -> None:
        proc = self._run("marker-present")
        self.assertEqual(proc.returncode, 1)

    def test_marker_present_exit_zero_when_valid(self) -> None:
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text(
            '{"version":1,"legacy_runtime_import_complete":true}\n', encoding="utf-8"
        )
        proc = self._run("marker-present")
        self.assertEqual(proc.returncode, 0)

    def test_marker_present_malformed_exit_two(self) -> None:
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text("{bad", encoding="utf-8")
        proc = self._run("marker-present")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)

    def test_marker_present_wrong_version_value_free_stderr(self) -> None:
        # Fail-closed CLI path: a well-formed marker with a secret-like
        # wrong version exits 2 and the value never reaches stderr/stdout.
        secret = "TOPSECRET-cli-version"
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text(
            json.dumps(
                {"version": secret, "legacy_runtime_import_complete": True}
            ),
            encoding="utf-8",
        )
        proc = self._run("marker-present")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)
        self.assertNotIn(secret, proc.stderr)
        self.assertNotIn(secret, proc.stdout)

    def test_marker_present_unknown_key_value_free_stderr(self) -> None:
        # Unknown marker keys are rejected without naming them on stderr.
        secret_key = "TOPSECRET-cli-key"
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text(
            json.dumps(
                {
                    "version": 1,
                    "legacy_runtime_import_complete": True,
                    secret_key: 1,
                }
            ),
            encoding="utf-8",
        )
        proc = self._run("marker-present")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)
        self.assertNotIn(secret_key, proc.stderr)
        self.assertNotIn(secret_key, proc.stdout)

    def test_finalize_malformed_marker_value_free_stderr(self) -> None:
        # finalize-migration-marker on a schema-violating present marker
        # exits 2 (fatal before template overwrite) and never echoes
        # marker-controlled data.
        secret = "TOPSECRET-finalize-version"
        secret_key = "TOPSECRET-finalize-key"
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text(
            json.dumps(
                {
                    "version": secret,
                    "legacy_runtime_import_complete": True,
                    secret_key: 1,
                }
            ),
            encoding="utf-8",
        )
        proc = self._run("finalize-migration-marker")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)
        self.assertNotIn(secret, proc.stderr)
        self.assertNotIn(secret_key, proc.stderr)

    def test_finalize_creates_marker_exit_zero(self) -> None:
        proc = self._run("finalize-migration-marker")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._marker().exists())

    def test_finalize_malformed_present_marker_exit_two(self) -> None:
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text("{bad", encoding="utf-8")
        proc = self._run("finalize-migration-marker")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)

    def test_finalize_write_failure_exit_two_clean_fatal(self) -> None:
        # A marker WRITE failure (OSError) is a clean fatal exit 2 (no
        # traceback), so the init caller aborts BEFORE the template
        # overwrite, and no marker is left behind.
        with mock.patch.object(
            self.m, "_atomic_write", side_effect=OSError("disk full")
        ):
            rc = self.m.main(["finalize-migration-marker"])
        self.assertEqual(rc, 2)
        self.assertFalse(self._marker().exists())


# ---------------------------------------------------------------------------
# Marker lifecycle: first upgrade migration + marker gating (W1)
# ---------------------------------------------------------------------------


@_requires_yaml
class MarkerLifecycleTests(WorkspaceTestCase):
    """First-upgrade migration and marker-gated skip semantics."""

    def _marker(self) -> Path:
        return self.workspace / "hermes" / "command-allowlist" / "migration-v1.json"

    def test_first_upgrade_migrates_and_creates_marker(self) -> None:
        # Default + named profile both carry a non-empty runtime allowlist.
        self._write_config("config.yaml", "command_allowlist: ['d1', 'd2']\n")
        profile_home = self.workspace / "profiles" / "coder"
        (profile_home / "config.yaml").parent.mkdir(parents=True)
        (profile_home / "config.yaml").write_text(
            "command_allowlist: ['c1']\n", encoding="utf-8"
        )
        # Simulate the init flow: migrate default, migrate profile, finalize.
        self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            self.workspace / "config.yaml", self.workspace
        )
        self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            profile_home / "config.yaml", profile_home
        )
        # Marker is finalized ONLY after all profiles are migrated.
        self.assertFalse(self._marker().exists())
        self.m.finalize_migration_marker()
        self.assertTrue(self._marker().exists())
        default_state = self.m.read_command_allowlist_sidecar(
            self._allowlist_sidecar()
        )
        assert default_state is not None
        self.assertEqual(default_state["command_allowlist"], ["d1", "d2"])
        coder_state = self.m.read_command_allowlist_sidecar(
            self._allowlist_sidecar("coder")
        )
        assert coder_state is not None
        self.assertEqual(coder_state["command_allowlist"], ["c1"])

    def test_no_legacy_values_still_finalizes_marker(self) -> None:
        self._write_config("config.yaml", "model:\n  default: x\n")
        self.m.finalize_migration_marker()
        self.assertTrue(self._marker().exists())
        # No sidecars were invented.
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_partial_first_migration_retry_is_idempotent(self) -> None:
        # Default migrates successfully; profile finalize not yet reached.
        self._write_config("config.yaml", "command_allowlist: ['d1']\n")
        profile_home = self.workspace / "profiles" / "coder"
        (profile_home / "config.yaml").parent.mkdir(parents=True)
        (profile_home / "config.yaml").write_text(
            "command_allowlist: ['c1']\n", encoding="utf-8"
        )
        # First attempt: default migrated, marker NOT finalized (e.g. crash).
        self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            self.workspace / "config.yaml", self.workspace
        )
        # Retry: existing sidecars are never overwritten.
        created_default = (
            self.m.migrate_existing_command_allowlist_to_absent_sidecar(
                self.workspace / "config.yaml", self.workspace
            )
        )
        self.assertFalse(created_default)
        state = self.m.read_command_allowlist_sidecar(self._allowlist_sidecar())
        assert state is not None
        self.assertEqual(state["command_allowlist"], ["d1"])
        # Now complete the profile + finalize.
        self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            profile_home / "config.yaml", profile_home
        )
        self.m.finalize_migration_marker()
        self.assertTrue(self._marker().exists())

    def test_marker_present_skips_runtime_import(self) -> None:
        # Stale runtime values that were deliberately cleared must not be
        # re-imported once the marker is present (R1 resurrection guard).
        self._write_config("config.yaml", "command_allowlist: ['stale']\n")
        self.m.finalize_migration_marker()
        created = self.m.migrate_existing_command_allowlist_to_absent_sidecar(
            self.workspace / "config.yaml", self.workspace
        )
        self.assertFalse(created)
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_malformed_present_marker_is_fatal(self) -> None:
        # A malformed marker must abort migration before overwrite.
        self._marker().parent.mkdir(parents=True, exist_ok=True)
        self._marker().write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.m.finalize_migration_marker()

    def test_marker_never_removed_by_clear(self) -> None:
        # clear_command_allowlist_stateful must never delete the marker.
        self._write_allowlist(["legacy"])
        self.m.finalize_migration_marker()
        config: dict = {"command_allowlist": ["legacy"]}
        with mock.patch.object(self.m, "_native_save_config"):
            self.m.clear_command_allowlist_stateful(config)
        self.assertTrue(self._marker().exists())
        self.assertFalse(self._allowlist_sidecar().exists())


# ---------------------------------------------------------------------------
# R1 exact regression: clear -> native save failure -> restart -> reconcile
# ---------------------------------------------------------------------------


@_requires_yaml
class R1RestartRegressionTests(WorkspaceTestCase):
    """Exact-order R1 sequence with the marker present."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["HERMES_HOME"] = str(self.workspace)
        # Capture stderr so value-free assertions can inspect CLI output.
        self._stderr = io.StringIO()
        self._orig_stderr = sys.stderr
        sys.stderr = self._stderr
        self.addCleanup(self._restore_stderr)

    def _restore_stderr(self) -> None:
        sys.stderr = self._orig_stderr

    @property
    def m_capture(self) -> str:
        return self._stderr.getvalue()

    def _marker(self) -> Path:
        return self.workspace / "hermes" / "command-allowlist" / "migration-v1.json"

    def test_marker_prevents_resurrection_across_restart(self) -> None:
        # Initial durable state: sidecar present with a value, marker present
        # (post-feature history). The runtime config holds a matching value.
        self._write_allowlist(["approved-cmd"])
        self._write_config(
            "config.yaml", "command_allowlist: ['approved-cmd']\n"
        )
        self.m.finalize_migration_marker()

        # 1) User clears via W2: sidecar deleted first.
        config = self._load_config(self.workspace / "config.yaml")
        # 2) Native runtime save FAILS after the sidecar deletion.
        def failing_native_save(cfg: dict, **kwargs) -> None:
            raise RuntimeError("native save failed")

        with mock.patch.object(
            self.m, "_native_save_config", side_effect=failing_native_save
        ):
            with self.assertRaises(RuntimeError):
                self.m.clear_command_allowlist_stateful(config)
        # Sidecar already deleted, but the stale runtime key remains in the
        # persisted config (the failed native save left the file untouched).
        self.assertFalse(self._allowlist_sidecar().exists())
        stale_cfg = self._load_config(self.workspace / "config.yaml")
        self.assertIn("command_allowlist", stale_cfg)

        # 3) Simulated immediate restart: legacy migration runs. The marker
        #    is present, so runtime import is SKIPPED — the stale key must
        #    never resurrect the deleted sidecar.
        rc = self.m.main(["migrate", "--hermes-home", str(self.workspace)])
        self.assertEqual(rc, 0)
        self.assertFalse(self._allowlist_sidecar().exists())

        # 4) Template/apply reconciliation removes the stale runtime key
        #    (absent-sidecar rule) and the deleted sidecar stays absent.
        self._write_config(
            "config.yaml", "command_allowlist: ['stale']\nmodel:\n  default: x\n"
        )
        statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any(s.startswith("default:") for s in statuses))
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertNotIn("command_allowlist", cfg)
        self.assertFalse(self._allowlist_sidecar().exists())
        self.assertTrue(self._marker().exists())

    def test_remote_sidecar_deletion_stays_absent_across_restart(self) -> None:
        # A remotely deleted sidecar (absent here) plus a stale runtime key
        # with the marker present must not be re-imported on restart.
        self.m.finalize_migration_marker()
        self._write_config(
            "config.yaml", "command_allowlist: ['stale-remote']\n"
        )
        rc = self.m.main(["migrate", "--hermes-home", str(self.workspace)])
        self.assertEqual(rc, 0)
        self.assertFalse(self._allowlist_sidecar().exists())
        # Reconcile removes the stale runtime key.
        self.m.apply_all_sidecars_and_policy()
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertNotIn("command_allowlist", cfg)
        self.assertTrue(self._marker().exists())


# ---------------------------------------------------------------------------
# R2: toggle failures warn-and-continue; command migration independently fatal
# ---------------------------------------------------------------------------


@_requires_yaml
class R2ToggleCommandMigrationTests(WorkspaceTestCase):
    """Toggle OSError warns + continues; command migration failures fatal."""

    def setUp(self) -> None:
        super().setUp()
        # Capture stderr so value-free assertions can inspect CLI output.
        self._stderr = io.StringIO()
        self._orig_stderr = sys.stderr
        sys.stderr = self._stderr
        self.addCleanup(self._restore_stderr)

    def _restore_stderr(self) -> None:
        sys.stderr = self._orig_stderr

    @property
    def m_capture(self) -> str:
        return self._stderr.getvalue()

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        return subprocess.run(
            [sys.executable, str(HELPER_PATH), "migrate", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_toggle_sidecar_oserror_warns_and_exit_zero(self) -> None:
        # A valid config with NO command allowlist and a toggle write OSError
        # must warn and proceed (exit 0): startup stays available.
        self._write_config("config.yaml", "skills:\n  disabled: ['d']\n")
        with mock.patch.object(
            self.m, "write_sidecar", side_effect=OSError("permission denied")
        ):
            proc = self.m.main(
                ["migrate", "--hermes-home", str(self.workspace)]
            )
        self.assertEqual(proc, 0)
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_toggle_sidecar_oserror_no_allowlist_exit_zero_subprocess(self) -> None:
        # End-to-end: a config with toggle keys but NO command allowlist
        # migrates the toggle sidecar and exits 0; no allowlist sidecar is
        # ever created.
        self._write_config("config.yaml", "skills:\n  disabled: ['d']\n")
        proc = self._run_cli("--hermes-home", str(self.workspace))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self._allowlist_sidecar().exists())
        self.assertTrue(
            (self.workspace / "hermes" / "skill-toggles" / "default.json").exists()
        )

    def test_command_migration_unreadable_config_is_fatal_value_free(self) -> None:
        self._write_config("config.yaml", "command_allowlist: ['SECRET']\n")
        with mock.patch.object(
            self.m,
            "migrate_existing_command_allowlist_to_absent_sidecar",
            side_effect=OSError("unreadable config"),
        ):
            proc = self.m.main(
                ["migrate", "--hermes-home", str(self.workspace)]
            )
        self.assertEqual(proc, 1)
        # Value-free: the secret command must not leak into stderr.
        self.assertNotIn("SECRET", self.m_capture)

    def test_command_migration_malformed_fatal(self) -> None:
        self._write_config("config.yaml", "command_allowlist: ['a', 5]\n")
        proc = self._run_cli("--hermes-home", str(self.workspace))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("command-allowlist migration failed", proc.stderr)
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_command_migration_oserror_fatal(self) -> None:
        # An ordinary non-ValueError command failure must be fatal and
        # sanitized (type-only, value-free).
        self._write_config("config.yaml", "command_allowlist: ['TOPSECRET-cmd']\n")
        with mock.patch.object(
            self.m,
            "migrate_existing_command_allowlist_to_absent_sidecar",
            side_effect=OSError("disk failure"),
        ):
            proc = self.m.main(
                ["migrate", "--hermes-home", str(self.workspace)]
            )
        self.assertEqual(proc, 1)
        self.assertIn("command-allowlist migration failed", self.m_capture)
        self.assertNotIn("TOPSECRET-cmd", self.m_capture)

    def test_toggle_failure_does_not_mask_command_failure(self) -> None:
        # A toggle failure alone must not make a command-safe config fatal,
        # and a command failure must be fatal even if toggle also fails.
        self._write_config("config.yaml", "command_allowlist: ['a']\n")
        with mock.patch.object(
            self.m,
            "migrate_existing_toggles_to_absent_sidecars",
            side_effect=OSError("toggle write failed"),
        ), mock.patch.object(
            self.m,
            "migrate_existing_command_allowlist_to_absent_sidecar",
            side_effect=OSError("command write failed"),
        ):
            proc = self.m.main(
                ["migrate", "--hermes-home", str(self.workspace)]
            )
        self.assertEqual(proc, 1)
        self.assertIn("command-allowlist migration failed", self.m_capture)


# ---------------------------------------------------------------------------
# W2 stateful helper surface (save + clear)
# ---------------------------------------------------------------------------


@_requires_yaml
class AllowlistStatefulHelperTests(WorkspaceTestCase):
    """save_command_allowlist_stateful: sidecar first, then raw runtime config."""

    def setUp(self) -> None:
        super().setUp()
        # The helper resolves the active HERMES_HOME for the sidecar; the
        # tests pin it to the isolated workspace.
        os.environ["HERMES_HOME"] = str(self.workspace)

    def test_writes_sidecar_first_then_runtime_config(self) -> None:
        config: dict = {}
        observed: dict = {}

        def fake_native_save(cfg: dict, **kwargs) -> None:
            observed["sidecar_existed"] = self._allowlist_sidecar().exists()
            observed["runtime_key"] = cfg["command_allowlist"]
            observed["config_is_same_object"] = cfg is config

        with mock.patch.object(self.m, "_native_save_config", side_effect=fake_native_save):
            self.m.save_command_allowlist_stateful(config, ["b", "a", "a"])

        self.assertTrue(observed["sidecar_existed"])
        self.assertEqual(observed["runtime_key"], ["a", "b"])
        self.assertTrue(observed["config_is_same_object"])
        state = self.m.read_command_allowlist_sidecar(self._allowlist_sidecar())
        assert state is not None
        self.assertEqual(state["command_allowlist"], ["a", "b"])

    def test_uses_shared_skill_state_lock(self) -> None:
        config: dict = {}
        with mock.patch.object(self.m, "_native_save_config"):
            self.m.save_command_allowlist_stateful(config, ["a"])
        lock_file = (
            self.workspace / "hermes" / "skill-toggles" / ".skill-toggles.lock"
        )
        self.assertTrue(lock_file.exists())

    def test_state_write_failure_fails_save_before_runtime_write(self) -> None:
        config: dict = {}
        native_calls: list = []

        def failing_write(path: Path, state: dict) -> None:
            raise ValueError("disk error")

        with mock.patch.object(
            self.m, "write_command_allowlist_sidecar", side_effect=failing_write
        ):
            with mock.patch.object(
                self.m,
                "_native_save_config",
                side_effect=lambda cfg, **kwargs: native_calls.append(cfg),
            ):
                with self.assertRaises(ValueError):
                    self.m.save_command_allowlist_stateful(config, ["a"])
        self.assertEqual(native_calls, [])
        # Neither the sidecar nor any runtime config write happened: the
        # save failed at the state-write step (file-level guarantee, same
        # as the toggle save helper).
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_runtime_failure_propagates_and_next_reconcile_repairs(self) -> None:
        config: dict = {}

        def failing_native_save(cfg: dict, **kwargs) -> None:
            raise RuntimeError("native save failed")

        with mock.patch.object(
            self.m, "_native_save_config", side_effect=failing_native_save
        ):
            with self.assertRaises(RuntimeError):
                self.m.save_command_allowlist_stateful(config, ["repair-me"])
        # Sidecar is durable despite the runtime failure...
        state = self.m.read_command_allowlist_sidecar(self._allowlist_sidecar())
        assert state is not None
        self.assertEqual(state["command_allowlist"], ["repair-me"])
        # ...and the next reconcile cycle repairs the runtime config.
        self._write_config(
            "config.yaml",
            "command_allowlist: ['stale']\nskills:\n  external_dirs: ['/a']\n"
            "memory:\n  nudge_interval: 10\n",
        )
        statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any(s.startswith("default:") for s in statuses))
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertEqual(cfg["command_allowlist"], ["repair-me"])
        self.assertEqual(cfg["skills"]["external_dirs"], ["/a"])
        self.assertEqual(cfg["memory"]["nudge_interval"], 10)

    def test_invalid_commands_fail_before_any_write(self) -> None:
        config: dict = {}
        native_calls: list = []
        with mock.patch.object(
            self.m,
            "_native_save_config",
            side_effect=lambda cfg, **kwargs: native_calls.append(cfg),
        ):
            with self.assertRaises(ValueError):
                self.m.save_command_allowlist_stateful(config, ["ok", ""])
        self.assertEqual(native_calls, [])
        self.assertFalse(self._allowlist_sidecar().exists())
        self.assertNotIn("command_allowlist", config)

    def test_set_rejects_non_list_types_before_any_write(self) -> None:
        # Strict schema input: only a REAL list is accepted. str/tuple/
        # set/generator (and other non-lists) are rejected before any
        # sidecar or runtime write; the W2 permanent approval path
        # explicitly converts its set to list.
        bad_inputs = [
            "cmd-string",
            ("t1", "t2"),
            {"s1", "s2"},
            (c for c in ["generator-item"]),
            7,
            None,
        ]
        for bad in bad_inputs:
            with self.subTest(bad_type=type(bad).__name__):
                config: dict = {}
                native_calls: list = []
                with mock.patch.object(
                    self.m,
                    "_native_save_config",
                    side_effect=lambda cfg, **kwargs: native_calls.append(cfg),
                ):
                    with self.assertRaises(ValueError):
                        self.m.save_command_allowlist_stateful(config, bad)
                self.assertEqual(native_calls, [])
                self.assertFalse(self._allowlist_sidecar().exists())
                self.assertNotIn("command_allowlist", config)

    def test_set_accepts_real_list(self) -> None:
        config: dict = {}
        with mock.patch.object(self.m, "_native_save_config"):
            self.m.save_command_allowlist_stateful(config, ["b", "a"])
        self.assertEqual(config["command_allowlist"], ["a", "b"])
        state = self.m.read_command_allowlist_sidecar(self._allowlist_sidecar())
        assert state is not None
        self.assertEqual(state["command_allowlist"], ["a", "b"])

    def test_set_explicit_empty_preserved_from_absent_runtime_key(self) -> None:
        # Initially absent runtime key -> explicit empty through the native
        # SET persistence path: the sidecar is written first (authoritative
        # []), then the native save is invoked WITH the pinned upstream
        # preserve_keys argument (exact set of path tuples) so upstream
        # keeps the root key at [].
        config: dict = {"model": {"default": "x"}}
        captured: dict = {}

        def fake_native_save(cfg: dict, **kwargs) -> None:
            captured["kwargs"] = kwargs
            captured["sidecar_existed"] = self._allowlist_sidecar().exists()
            captured["cfg"] = cfg

        with mock.patch.object(
            self.m, "_native_save_config", side_effect=fake_native_save
        ):
            self.m.save_command_allowlist_stateful(config, [])

        self.assertEqual(
            captured["kwargs"], {"preserve_keys": {("command_allowlist",)}}
        )
        self.assertTrue(captured["sidecar_existed"])
        self.assertEqual(captured["cfg"]["command_allowlist"], [])
        self.assertEqual(config["command_allowlist"], [])
        self.assertEqual(config["model"], {"default": "x"})
        state = self.m.read_command_allowlist_sidecar(self._allowlist_sidecar())
        assert state is not None
        self.assertEqual(state["command_allowlist"], [])


@_requires_yaml
class ClearCommandAllowlistStatefulTests(WorkspaceTestCase):
    """clear_command_allowlist_stateful: delete sidecar, then exact-key unset."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["HERMES_HOME"] = str(self.workspace)

    def test_deletes_sidecar_first_then_saves_runtime(self) -> None:
        self._write_allowlist(["legacy"])
        config: dict = {
            "command_allowlist": ["legacy"],
            "model": {"default": "x"},
            "skills": {"disabled": ["d"]},
        }
        observed: dict = {}

        def fake_native_save(cfg: dict, **kwargs) -> None:
            observed["sidecar_existed"] = self._allowlist_sidecar().exists()
            observed["key_removed"] = "command_allowlist" not in cfg
            observed["config_is_same_object"] = cfg is config

        with mock.patch.object(self.m, "_native_save_config", side_effect=fake_native_save):
            self.m.clear_command_allowlist_stateful(config)

        # Sidecar deleted BEFORE the runtime config was persisted.
        self.assertFalse(observed["sidecar_existed"])
        self.assertTrue(observed["key_removed"])
        self.assertTrue(observed["config_is_same_object"])
        self.assertFalse(self._allowlist_sidecar().exists())

    def test_removes_only_root_key_unrelated_fields_survive(self) -> None:
        self._write_allowlist(["legacy"])
        config: dict = {
            "command_allowlist": ["legacy"],
            "model": {"default": "x"},
            "skills": {"disabled": ["d"], "external_dirs": ["/a"]},
            "memory": {"nudge_interval": 10},
        }
        captured: dict = {}

        with mock.patch.object(
            self.m, "_native_save_config", side_effect=lambda cfg, **kwargs: captured.update(cfg=cfg)
        ):
            self.m.clear_command_allowlist_stateful(config)

        cfg = captured["cfg"]
        self.assertNotIn("command_allowlist", cfg)
        self.assertEqual(cfg["model"], {"default": "x"})
        self.assertEqual(
            cfg["skills"], {"disabled": ["d"], "external_dirs": ["/a"]}
        )
        self.assertEqual(cfg["memory"], {"nudge_interval": 10})

    def test_absent_sidecar_is_noop_success(self) -> None:
        config: dict = {"command_allowlist": ["stale"]}
        native_calls: list = []
        with mock.patch.object(
            self.m,
            "_native_save_config",
            side_effect=lambda cfg, **kwargs: native_calls.append(dict(cfg)),
        ):
            self.m.clear_command_allowlist_stateful(config)
        self.assertEqual(len(native_calls), 1)
        self.assertNotIn("command_allowlist", native_calls[0])

    def test_clear_never_requests_preservation(self) -> None:
        # UNSET must never pass default-key preservation: the key has to be
        # dropped from the persisted config.
        self._write_allowlist(["legacy"])
        config: dict = {"command_allowlist": ["legacy"]}
        captured: dict = {}

        def fake_native_save(cfg: dict, **kwargs) -> None:
            captured["kwargs"] = kwargs

        with mock.patch.object(
            self.m, "_native_save_config", side_effect=fake_native_save
        ):
            self.m.clear_command_allowlist_stateful(config)
        self.assertEqual(captured["kwargs"], {})

    def test_state_deletion_failure_propagates_before_runtime_save(self) -> None:
        self._write_allowlist(["legacy"])
        config: dict = {"command_allowlist": ["legacy"]}
        native_calls: list = []

        with mock.patch.object(Path, "unlink", side_effect=OSError("delete failed")):
            with mock.patch.object(
                self.m,
                "_native_save_config",
                side_effect=lambda cfg, **kwargs: native_calls.append(cfg),
            ):
                with self.assertRaises(OSError):
                    self.m.clear_command_allowlist_stateful(config)
        self.assertEqual(native_calls, [])
        # State was NOT deleted and the runtime key was not removed.
        self.assertTrue(self._allowlist_sidecar().exists())
        self.assertIn("command_allowlist", config)

    def test_config_save_failure_propagates_and_reconcile_repairs(self) -> None:
        self._write_allowlist(["legacy"])
        config: dict = {"command_allowlist": ["legacy"]}

        def failing_native_save(cfg: dict, **kwargs) -> None:
            raise RuntimeError("native save failed")

        with mock.patch.object(
            self.m, "_native_save_config", side_effect=failing_native_save
        ):
            with self.assertRaises(RuntimeError):
                self.m.clear_command_allowlist_stateful(config)
        # The sidecar deletion already happened (delete-first ordering)...
        self.assertFalse(self._allowlist_sidecar().exists())
        # ...so the next reconcile cycle removes the stale runtime key
        # via the absent-sidecar rule, preserving unrelated fields.
        self._write_config(
            "config.yaml",
            "command_allowlist: ['stale']\nmodel:\n  default: x\n"
            "skills:\n  external_dirs: ['/a']\n",
        )
        statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any(s.startswith("default:") for s in statuses))
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertNotIn("command_allowlist", cfg)
        self.assertEqual(cfg["model"], {"default": "x"})
        self.assertEqual(cfg["skills"]["external_dirs"], ["/a"])


# ---------------------------------------------------------------------------
# Native-save wrapper contract (pinned v2026.8.18 preserve_keys pass-through)
# ---------------------------------------------------------------------------


class NativeSaveWrapperContractTests(unittest.TestCase):
    """_native_save_config forwards preserve_keys to the pinned upstream.

    Pinned upstream signature (Hermes v2026.8.18)::

        save_config(config, *, strip_defaults=True,
                    preserve_keys: Optional[Set[Tuple[str, ...]]] = None,
                    merge_existing=False)

    The fake below uses EXACTLY that signature (not arbitrary **kwargs),
    so the tests prove the precise set-of-path-tuples argument upstream
    receives.
    """

    def setUp(self) -> None:
        self.m = _load_helper()

    def _install_fake_upstream(self):
        import types

        module = types.ModuleType("hermes_cli.config")
        package = types.ModuleType("hermes_cli")
        calls: list = []

        def fake_save_config(
            config,
            *,
            strip_defaults=True,
            preserve_keys=None,
            merge_existing=False,
        ):
            calls.append(
                (
                    config,
                    {
                        "strip_defaults": strip_defaults,
                        "preserve_keys": preserve_keys,
                        "merge_existing": merge_existing,
                    },
                )
            )

        module.save_config = fake_save_config  # type: ignore[attr-defined]
        return module, package, calls

    def test_preserve_keys_set_of_path_tuples_reaches_upstream(self) -> None:
        module, package, calls = self._install_fake_upstream()
        with mock.patch.dict(
            sys.modules, {"hermes_cli": package, "hermes_cli.config": module}
        ):
            self.m._native_save_config(
                {"command_allowlist": []},
                preserve_keys={("command_allowlist",)},
            )
        self.assertEqual(
            calls,
            [
                (
                    {"command_allowlist": []},
                    {
                        "strip_defaults": True,
                        "preserve_keys": {("command_allowlist",)},
                        "merge_existing": False,
                    },
                )
            ],
        )

    def test_no_preserve_keys_by_default(self) -> None:
        # Toggle saves and the UNSET path call the wrapper bare: upstream
        # gets preserve_keys=None (its own default), never a set.
        module, package, calls = self._install_fake_upstream()
        with mock.patch.dict(
            sys.modules, {"hermes_cli": package, "hermes_cli.config": module}
        ):
            self.m._native_save_config({"a": 1})
        self.assertEqual(
            calls,
            [
                (
                    {"a": 1},
                    {
                        "strip_defaults": True,
                        "preserve_keys": None,
                        "merge_existing": False,
                    },
                )
            ],
        )


# ---------------------------------------------------------------------------
# Reconciliation integration (apply-all)
# ---------------------------------------------------------------------------


@_requires_yaml
class AllowlistReconcileTests(WorkspaceTestCase):
    """apply-all: prevalidation before any write; per-profile application."""

    def test_malformed_orphan_allowlist_sidecar_fails_cycle_before_any_write(
        self,
    ) -> None:
        # A canonical-named ORPHAN profile sidecar (no matching runtime
        # profile config) is still "present" state: a malformed one must
        # fail the whole apply cycle BEFORE any runtime config mutation,
        # including the default profile's otherwise-mandatory policy write.
        default_cfg = self._write_config(
            "config.yaml",
            "skills:\n  creation_nudge_interval: 15\nmemory:\n  nudge_interval: 10\n",
        )
        other_home = self.workspace / "profiles" / "coder"
        other_home.mkdir(parents=True)
        other_cfg = other_home / "config.yaml"
        other_cfg.write_text("model:\n  default: y\n", encoding="utf-8")
        default_original = default_cfg.read_text(encoding="utf-8")
        other_original = other_cfg.read_text(encoding="utf-8")
        orphan = self._allowlist_sidecar("ghost")
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text(
            '{"version":1,"command_allowlist":["MARKER-cmd", 3]}',
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as cm:
            self.m.apply_all_sidecars_and_policy()

        # Value-free error (sidecar contents never surfaced).
        self.assertNotIn("MARKER-cmd", str(cm.exception))
        # NO runtime config was mutated: default got no policy write and
        # the other profile was left untouched.
        self.assertEqual(default_cfg.read_text(encoding="utf-8"), default_original)
        self.assertEqual(other_cfg.read_text(encoding="utf-8"), other_original)

    def test_apply_all_prevalidates_before_any_config_write(self) -> None:
        # A malformed allowlist sidecar on ANY profile must fail the whole
        # cycle BEFORE the default config gets its (otherwise mandatory)
        # policy write — no partial apply.
        self._write_config(
            "config.yaml",
            "skills:\n  creation_nudge_interval: 15\nmemory:\n  nudge_interval: 10\n",
        )
        profile_home = self.workspace / "profiles" / "coder"
        (profile_home / "config.yaml").parent.mkdir(parents=True)
        (profile_home / "config.yaml").write_text(
            "model:\n  default: y\n", encoding="utf-8"
        )
        default_config = self.workspace / "config.yaml"
        original_default = default_config.read_text(encoding="utf-8")
        original_coder = (profile_home / "config.yaml").read_text(encoding="utf-8")
        bad = self._allowlist_sidecar("coder")
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{not json", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.m.apply_all_sidecars_and_policy()

        self.assertEqual(
            default_config.read_text(encoding="utf-8"), original_default
        )
        self.assertEqual(
            (profile_home / "config.yaml").read_text(encoding="utf-8"),
            original_coder,
        )

    def test_apply_all_validates_present_marker_before_any_config_write(self) -> None:
        # The migration marker is part of the fail-closed prevalidation
        # pass: a malformed present marker must fail the whole apply cycle
        # BEFORE any runtime config mutation, even when every sidecar is
        # absent (the marker is validated by itself).
        default_cfg = self._write_config(
            "config.yaml",
            "command_allowlist: ['stale']\n"
            "skills:\n  creation_nudge_interval: 15\nmemory:\n  nudge_interval: 10\n",
        )
        original = default_cfg.read_text(encoding="utf-8")
        marker = (
            self.workspace / "hermes" / "command-allowlist" / "migration-v1.json"
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{broken", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.m.apply_all_sidecars_and_policy()

        # No runtime config mutation (the stale key would otherwise be
        # removed by the absent-sidecar rule).
        self.assertEqual(default_cfg.read_text(encoding="utf-8"), original)
        # The marker itself is never repaired or rewritten by apply-all.
        self.assertEqual(marker.read_text(encoding="utf-8"), "{broken")

    def test_apply_all_malformed_marker_error_is_value_free(self) -> None:
        # The prevalidation pass validates the marker BEFORE any config
        # write; a marker with secret-like data fails the cycle fail-closed
        # and value-free.
        secret_version = "TOPSECRET-sync-version"
        secret_key = "TOPSECRET-sync-key"
        default_cfg = self._write_config(
            "config.yaml", "command_allowlist: ['stale']\n"
        )
        marker = (
            self.workspace / "hermes" / "command-allowlist" / "migration-v1.json"
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "version": secret_version,
                    "legacy_runtime_import_complete": True,
                    secret_key: 1,
                }
            ),
            encoding="utf-8",
        )
        original = default_cfg.read_text(encoding="utf-8")

        with self.assertRaises(ValueError) as cm:
            self.m.apply_all_sidecars_and_policy()
        message = str(cm.exception)
        self.assertNotIn(secret_version, message)
        self.assertNotIn(secret_key, message)
        # Fail-closed: no runtime config mutation happened.
        self.assertEqual(default_cfg.read_text(encoding="utf-8"), original)

    def test_sync_and_apply_malformed_marker_error_is_value_free(self) -> None:
        # End-to-end cron surface: the apply-failure status derived from
        # the marker exception must not carry marker-controlled data.
        secret = "TOPSECRET-cron-version"
        self._write_config("config.yaml", "command_allowlist: ['stale']\n")
        marker = (
            self.workspace / "hermes" / "command-allowlist" / "migration-v1.json"
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {"version": secret, "legacy_runtime_import_complete": True}
            ),
            encoding="utf-8",
        )
        script = self.workspace / "fake-sync.sh"
        script.write_text("#!/bin/sh\necho SYNC_OK\n", encoding="utf-8")
        script.chmod(0o755)
        exit_status, statuses, output = self.m.sync_and_apply([str(script)])
        self.assertNotEqual(exit_status, 0)
        self.assertTrue(any("apply:error:" in s for s in statuses))
        joined = " ".join(statuses) + output
        self.assertNotIn(secret, joined)
        # Fail-closed: the stale runtime key was NOT removed (no partial
        # apply happened).
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertIn("command_allowlist", cfg)

    def test_apply_all_applies_allowlist_state(self) -> None:
        self._write_config(
            "config.yaml",
            "command_allowlist: ['stale']\nmodel:\n  default: x\n"
            "skills:\n  external_dirs: ['/a']\nmemory:\n  nudge_interval: 10\n",
        )
        self._write_allowlist(["b", "a"])
        statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any(s.startswith("default:") for s in statuses))
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertEqual(cfg["command_allowlist"], ["a", "b"])
        # Unrelated top-level/skills fields preserved; policy and models
        # behavior intact.
        self.assertEqual(cfg["skills"]["external_dirs"], ["/a"])
        self.assertEqual(cfg["model"], {"default": "x"})
        self.assertEqual(cfg["memory"]["nudge_interval"], 10)
        self.assertEqual(cfg["skills"]["creation_nudge_interval"], 0)
        self.assertEqual(cfg["skills"]["write_approval"], True)
        self.assertEqual(cfg["curator"]["enabled"], False)
        self.assertTrue(any(s.startswith("models:") for s in statuses))

    def test_apply_all_absent_sidecar_removes_runtime_key(self) -> None:
        self._write_config(
            "config.yaml",
            "command_allowlist: ['legacy']\nskills:\n  disabled: ['d']\n",
        )
        # No allowlist sidecar at all.
        statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any(s.startswith("default:") for s in statuses))
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertNotIn("command_allowlist", cfg)
        # Toggle behavior preserved.
        self.assertEqual(cfg["skills"]["disabled"], ["d"])

    def test_apply_all_explicit_empty_sidecar_is_authoritative(self) -> None:
        self._write_config("config.yaml", "command_allowlist: ['legacy']\n")
        self._write_allowlist([])
        self.m.apply_all_sidecars_and_policy()
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertEqual(cfg["command_allowlist"], [])

    def test_apply_all_profile_isolation(self) -> None:
        self._write_config("config.yaml", "model:\n  default: x\n")
        profile_home = self.workspace / "profiles" / "coder"
        (profile_home / "config.yaml").parent.mkdir(parents=True)
        (profile_home / "config.yaml").write_text(
            "model:\n  default: y\n", encoding="utf-8"
        )
        self._write_allowlist(["default-cmd"])
        self._write_allowlist(["coder-cmd"], profile="coder")
        self.m.apply_all_sidecars_and_policy()
        default_cfg = self._load_config(self.workspace / "config.yaml")
        coder_cfg = self._load_config(profile_home / "config.yaml")
        self.assertEqual(default_cfg["command_allowlist"], ["default-cmd"])
        self.assertEqual(coder_cfg["command_allowlist"], ["coder-cmd"])

    def test_apply_all_statuses_never_contain_allowlist_contents(self) -> None:
        secret = "TOPSECRET-reconcile-command"
        self._write_config("config.yaml", "model:\n  default: x\n")
        self._write_allowlist([secret])
        statuses = self.m.apply_all_sidecars_and_policy()
        for status in statuses:
            self.assertNotIn(secret, status)

    def test_apply_all_preserves_toggles_and_policy_behavior(self) -> None:
        self._write_config(
            "config.yaml",
            "skills:\n  disabled: ['old']\n  creation_nudge_interval: 15\n",
        )
        toggle_sidecar = (
            self.workspace / "hermes" / "skill-toggles" / "default.json"
        )
        toggle_sidecar.parent.mkdir(parents=True, exist_ok=True)
        self.m.write_sidecar(
            toggle_sidecar, self.m.normalize_state(["new"], {"cli": ["x"]})
        )
        self._write_allowlist(["cmd"])
        self.m.apply_all_sidecars_and_policy()
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertEqual(cfg["skills"]["disabled"], ["new"])
        self.assertEqual(cfg["skills"]["platform_disabled"], {"cli": ["x"]})
        self.assertEqual(cfg["skills"]["creation_nudge_interval"], 0)
        self.assertEqual(cfg["skills"]["write_approval"], True)
        self.assertEqual(cfg["curator"]["enabled"], False)
        self.assertEqual(cfg["command_allowlist"], ["cmd"])

    def test_apply_all_malformed_error_has_no_contents_via_sync_and_apply(self) -> None:
        secret = "TOPSECRET-sync-command"
        self._write_config("config.yaml", "model:\n  default: x\n")
        bad = self._allowlist_sidecar()
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(
            json.dumps({"version": 1, "command_allowlist": [secret, 5]}),
            encoding="utf-8",
        )
        script = self.workspace / "fake-sync.sh"
        script.write_text("#!/bin/sh\necho SYNC_OK\n", encoding="utf-8")
        script.chmod(0o755)
        exit_status, statuses, output = self.m.sync_and_apply([str(script)])
        self.assertNotEqual(exit_status, 0)
        self.assertTrue(any("error:" in s for s in statuses))
        # Fail-closed: no allowlist contents may leak into statuses/output.
        for status in statuses:
            self.assertNotIn(secret, status)
        self.assertNotIn(secret, output)
        # Config untouched (no partial apply).
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertNotIn("command_allowlist", cfg)


@_requires_yaml
class AllowlistApplySingleProfileTests(WorkspaceTestCase):
    """apply_sidecar_and_enforce_policy with allowlist state."""

    def _config_path(self) -> Path:
        return self.workspace / "config.yaml"

    def test_applies_allowlist_and_reports_status(self) -> None:
        self._write_config("config.yaml", "command_allowlist: ['old']\n")
        self._write_allowlist(["new"])
        status = self.m.apply_sidecar_and_enforce_policy(
            self._config_path(), self.workspace
        )
        self.assertIn("command-allowlist", status)
        cfg = self._load_config(self._config_path())
        self.assertEqual(cfg["command_allowlist"], ["new"])

    def test_absent_sidecar_removes_key_and_reports_status(self) -> None:
        self._write_config("config.yaml", "command_allowlist: ['legacy']\n")
        status = self.m.apply_sidecar_and_enforce_policy(
            self._config_path(), self.workspace
        )
        self.assertIn("command-allowlist-removed", status)
        cfg = self._load_config(self._config_path())
        self.assertNotIn("command_allowlist", cfg)

    def test_malformed_allowlist_leaves_config_untouched(self) -> None:
        original = "model:\n  default: x\n"
        self._write_config("config.yaml", original)
        bad = self._allowlist_sidecar()
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text('{"version":1,"command_allowlist":[3]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.m.apply_sidecar_and_enforce_policy(
                self._config_path(), self.workspace
            )
        self.assertEqual(self._config_path().read_text(encoding="utf-8"), original)

    def test_matching_state_is_a_write_free_noop(self) -> None:
        self._write_config(
            "config.yaml",
            "command_allowlist: ['a']\nskills:\n  creation_nudge_interval: 0\n"
            "  write_approval: true\ncurator:\n  enabled: false\n",
        )
        self._write_allowlist(["a"])
        config_path = self._config_path()
        before = config_path.read_text(encoding="utf-8")
        status = self.m.apply_sidecar_and_enforce_policy(config_path, self.workspace)
        self.assertEqual(status, "no-sidecar-no-policy-change")
        self.assertEqual(config_path.read_text(encoding="utf-8"), before)


# ---------------------------------------------------------------------------
# Init ownership repair (narrow, tested)
# ---------------------------------------------------------------------------


class AllowlistInitOwnershipTests(unittest.TestCase):
    """docker-hermes-init.sh ownership repair covers the new dedicated tree."""

    def setUp(self) -> None:
        self.src = INIT_PATH.read_text(encoding="utf-8")

    def _repair_function_text(self) -> str:
        start = self.src.index("repair_skill_toggle_ownership() {")
        end = self.src.index("\n}\n", start) + 3
        return self.src[start:end]

    def test_repair_covers_command_allowlist_tree(self) -> None:
        body = self._repair_function_text()
        self.assertIn("${WORKSPACE_DIR}/hermes/command-allowlist", body)

    def test_repair_still_covers_toggle_tree(self) -> None:
        body = self._repair_function_text()
        self.assertIn("${WORKSPACE_DIR}/hermes/skill-toggles", body)

    def test_repair_is_gated_to_root_and_existing_tree(self) -> None:
        body = self._repair_function_text()
        self.assertIn('"$(id -u)" = "0"', body)
        self.assertIn('[ -d "$toggle_tree" ]', body)

    def test_repair_is_narrow(self) -> None:
        # The repair loop targets ONLY the two dedicated state trees.
        body = self._repair_function_text()
        self.assertEqual(body.count("WORKSPACE_DIR}/hermes/"), 2)
        self.assertNotIn('chown -R "${HERMES_UID_VALUE}:${HERMES_GID_VALUE}" "${HERMES_HOME}"', body)


@unittest.skipIf(os.geteuid() == 0, "gating-to-non-root assertion requires non-root")
class AllowlistInitOwnershipBehavioralTests(unittest.TestCase):
    """Exercise the ACTUAL extracted repair function with stubbed id/chown."""

    def _extract_function(self, tmpdir: str) -> str:
        source = INIT_PATH.read_text(encoding="utf-8")
        start = source.index("repair_skill_toggle_ownership() {")
        end = source.index("\n}\n", start) + 3
        path = Path(tmpdir) / "repair-function.sh"
        path.write_text(source[start:end], encoding="utf-8")
        return str(path)

    def _make_stub(self, directory: Path, name: str, body: str) -> None:
        stub = directory / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)

    def _run_repair(self, workspace: Path, tmpdir: Path, *, stub_id: bool) -> subprocess.CompletedProcess:
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir(exist_ok=True)
        self._make_stub(
            bin_dir,
            "chown",
            f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{tmpdir}/chown-calls"\nexit 0\n',
        )
        if stub_id:
            self._make_stub(bin_dir, "id", "#!/bin/sh\necho 0\n")
        fn = self._extract_function(str(tmpdir))
        wrapper = tmpdir / "wrapper.sh"
        wrapper.write_text(
            f'#!/bin/sh\nset -eu\nPATH="{bin_dir}:$PATH"\n'
            f'WORKSPACE_DIR="{workspace}"\n'
            "HERMES_UID_VALUE=10000\nHERMES_GID_VALUE=10000\n"
            f'. "{fn}"\nrepair_skill_toggle_ownership\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return subprocess.run(
            ["sh", str(wrapper)], capture_output=True, text=True, check=False
        )

    def test_repairs_both_existing_trees_with_hermes_uid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            workspace = tmpdir / "ws"
            for rel in ("hermes/skill-toggles", "hermes/command-allowlist"):
                (workspace / rel).mkdir(parents=True)
            proc = self._run_repair(workspace, tmpdir, stub_id=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            calls = (tmpdir / "chown-calls").read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertIn(
                f"-R 10000:10000 {workspace / 'hermes' / 'skill-toggles'}", calls
            )
            self.assertIn(
                f"-R 10000:10000 {workspace / 'hermes' / 'command-allowlist'}", calls
            )

    def test_skips_absent_tree_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            workspace = tmpdir / "ws"
            (workspace / "hermes" / "skill-toggles").mkdir(parents=True)
            proc = self._run_repair(workspace, tmpdir, stub_id=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            calls = (tmpdir / "chown-calls").read_text().splitlines()
            self.assertEqual(len(calls), 1)
            self.assertIn("skill-toggles", calls[0])
            self.assertNotIn("command-allowlist", calls[0])
            self.assertFalse((workspace / "hermes" / "command-allowlist").exists())

    def test_non_root_runner_never_chowns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            workspace = tmpdir / "ws"
            for rel in ("hermes/skill-toggles", "hermes/command-allowlist"):
                (workspace / rel).mkdir(parents=True)
            proc = self._run_repair(workspace, tmpdir, stub_id=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((tmpdir / "chown-calls").exists())


# ---------------------------------------------------------------------------
# CLI one-profile reconcile under the shared lock
# ---------------------------------------------------------------------------


class CliApplyLockTests(WorkspaceTestCase):
    """_cli_apply holds the shared SkillStateLock around its apply call."""

    def test_cli_apply_runs_apply_between_lock_acquire_and_release(self) -> None:
        self._write_config("config.yaml", "model:\n  default: x\n")
        self._write_allowlist(["a"])
        events: list = []
        real_lock = self.m.SkillStateLock

        class SpyLock(real_lock):
            def __enter__(self):
                events.append("lock-acquired")
                return super().__enter__()

            def __exit__(self, exc_type, exc, tb):
                events.append("lock-released")
                return super().__exit__(exc_type, exc, tb)

        with mock.patch.object(self.m, "SkillStateLock", SpyLock), mock.patch.object(
            self.m,
            "apply_sidecar_and_enforce_policy",
            side_effect=lambda *a, **k: events.append("apply") or "applied-sidecar",
        ):
            rc = self.m.main(["apply", "--hermes-home", str(self.workspace)])
        self.assertEqual(rc, 0)
        # The apply call happens strictly inside the shared critical
        # section, exercised through the real (flock-backed) lock class.
        self.assertEqual(events, ["lock-acquired", "apply", "lock-released"])
        # The shared default lock file is the one used.
        self.assertTrue(
            (
                self.workspace
                / "hermes"
                / "skill-toggles"
                / ".skill-toggles.lock"
            ).exists()
        )

    def test_cli_apply_failing_allowlist_sidecar_leaves_config_unchanged(self) -> None:
        # Fail-closed: a malformed allowlist sidecar makes the CLI exit
        # nonzero (error handling preserved) and the runtime config is
        # untouched; the error is value-free.
        original = "model:\n  default: x\nmemory:\n  nudge_interval: 10\n"
        self._write_config("config.yaml", original)
        bad = self._allowlist_sidecar()
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(
            '{"version":1,"command_allowlist":["MARKER-cmd", 4]}',
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        proc = subprocess.run(
            [
                sys.executable,
                str(HELPER_PATH),
                "apply",
                "--hermes-home",
                str(self.workspace),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)
        self.assertNotIn("MARKER-cmd", proc.stderr)
        self.assertEqual(
            (self.workspace / "config.yaml").read_text(encoding="utf-8"), original
        )


# ---------------------------------------------------------------------------
# Init fail-closed: allowlist migration + helper absence (source contract)
# ---------------------------------------------------------------------------


class AllowlistInitFailClosedTests(unittest.TestCase):
    """docker-hermes-init.sh fails closed around allowlist migration/absence."""

    def setUp(self) -> None:
        self.src = INIT_PATH.read_text(encoding="utf-8")

    def _function_body(self, name: str) -> str:
        start = self.src.index(f"{name}() {{")
        end = self.src.index("\n}\n", start)
        return self.src[start:end]

    def test_migrate_call_site_fails_init_before_template_overwrite(self) -> None:
        call = "migrate_existing_toggles || exit 1"
        call_pos = self.src.find(call)
        overwrite_pos = self.src.find("Syncing Hermes config.yaml from repo template")
        self.assertGreater(call_pos, 0)
        self.assertGreater(overwrite_pos, 0)
        self.assertLess(call_pos, overwrite_pos)

    def test_migrate_helper_missing_fails_closed_when_state_present(self) -> None:
        body = self._function_body("migrate_existing_toggles")
        self.assertIn("command_allowlist_state_present", body)
        self.assertIn("ERROR: command-allowlist state present", body)

    def test_migrate_default_profile_failure_is_fatal(self) -> None:
        body = self._function_body("migrate_existing_toggles")
        self.assertIn("if ! WORKSPACE_DIR", body)
        self.assertIn("refusing to overwrite", body)
        # No warn-and-continue fallback for the default profile migration.
        self.assertNotIn("migration failed; continuing", body)

    def test_migrate_named_profile_failure_is_fatal(self) -> None:
        body = self._function_body("migrate_existing_toggles")
        self.assertIn('profile_config="${profile_dir}config.yaml"', body)
        loop_pos = body.find("for profile_dir")
        fatal_pos = body.find("refusing to overwrite", loop_pos)
        self.assertGreater(fatal_pos, 0)
        # The named-profile failure path returns nonzero from the function.
        return_pos = body.find("return 1", fatal_pos)
        self.assertGreater(return_pos, 0)

    def test_apply_helper_missing_fails_closed_when_state_present(self) -> None:
        body = self._function_body("apply_sidecars_and_policy")
        self.assertIn("command_allowlist_state_present", body)
        self.assertIn("command-allowlist state present but helper unavailable", body)

    def test_state_detection_is_narrow(self) -> None:
        body = self._function_body("command_allowlist_state_present")
        # Only the dedicated allowlist tree is inspected: the base path is
        # WORKSPACE_DIR/hermes/command-allowlist with default.json plus the
        # profiles/*.json globs; never a broad HERMES_HOME scan.
        self.assertIn("${WORKSPACE_DIR}/hermes/command-allowlist", body)
        self.assertIn("default.json", body)
        self.assertIn("profiles", body)
        self.assertNotIn("${HERMES_HOME}", body)


# ---------------------------------------------------------------------------
# Init fail-closed: behavioral tests on the ACTUAL extracted functions
# ---------------------------------------------------------------------------


class AllowlistInitFailClosedBehavioralTests(unittest.TestCase):
    """Run the actual init migrate/apply flow in a sandbox wrapper."""

    def _extract_functions(self, names, tmpdir: str) -> str:
        source = INIT_PATH.read_text(encoding="utf-8")
        chunks = []
        for name in names:
            start = source.index(f"{name}() {{")
            end = source.index("\n}\n", start) + 3
            chunks.append(source[start:end])
        path = Path(tmpdir) / "init-functions.sh"
        path.write_text("\n".join(chunks), encoding="utf-8")
        return str(path)

    def _write_migrate_wrapper(
        self,
        tmpdir: str,
        *,
        workspace: Path,
        helper_path: str,
        source_config: Path,
        state_python: str | None = None,
    ) -> Path:
        fn = self._extract_functions(
            ["command_allowlist_state_present", "migrate_existing_toggles"], tmpdir
        )
        wrapper = Path(tmpdir) / "migrate-wrapper.sh"
        wrapper.write_text(
            f'''#!/bin/sh
set -eu
WORKSPACE_DIR="{workspace}"
HERMES_HOME="{workspace}"
RUNTIME_CONFIG="{workspace}/config.yaml"
SOURCE_CONFIG="{source_config}"
JOSEMAR_SKILL_STATE="{helper_path}"
JOSEMAR_STATE_PYTHON="{state_python if state_python is not None else sys.executable}"
log() {{ echo "[init-test] $1"; }}
. "{fn}"
migrate_existing_toggles || exit 1
cp "$SOURCE_CONFIG" "$RUNTIME_CONFIG"
echo "TEMPLATE_COPIED"
''',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return wrapper

    def test_migration_failure_prevents_template_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            runtime = workspace / "config.yaml"
            original = "command_allowlist: ['a', 5]\n"  # malformed -> migration fails
            runtime.write_text(original, encoding="utf-8")
            template = Path(tmp) / "template.yaml"
            template.write_text("model:\n  default: template\n", encoding="utf-8")
            wrapper = self._write_migrate_wrapper(
                tmp, workspace=workspace, helper_path=str(HELPER_PATH),
                source_config=template,
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("TEMPLATE_COPIED", proc.stdout)
            # The pre-feature runtime allowlist was NOT erased.
            self.assertEqual(runtime.read_text(encoding="utf-8"), original)
            self.assertFalse(
                (workspace / "hermes" / "command-allowlist" / "default.json").exists()
            )
            self.assertIn("ERROR", proc.stdout)

    def test_migration_success_continues_to_template_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "config.yaml").write_text(
                "command_allowlist: ['b', 'a']\n", encoding="utf-8"
            )
            template = Path(tmp) / "template.yaml"
            template.write_text("model:\n  default: template\n", encoding="utf-8")
            wrapper = self._write_migrate_wrapper(
                tmp, workspace=workspace, helper_path=str(HELPER_PATH),
                source_config=template,
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("TEMPLATE_COPIED", proc.stdout)
            # The pre-feature allowlist survived in an absent-... present sidecar.
            sidecar = (
                workspace / "hermes" / "command-allowlist" / "default.json"
            )
            self.assertEqual(
                sidecar.read_text(encoding="utf-8"),
                '{"version":1,"command_allowlist":["a","b"]}\n',
            )

    def test_named_profile_migration_failure_prevents_template_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            profile_home = workspace / "profiles" / "coder"
            profile_home.mkdir(parents=True)
            (workspace / "config.yaml").write_text(
                "model:\n  default: ok\n", encoding="utf-8"
            )
            profile_config = profile_home / "config.yaml"
            original = "command_allowlist: ['a', 5]\n"  # malformed -> fails
            profile_config.write_text(original, encoding="utf-8")
            template = Path(tmp) / "template.yaml"
            template.write_text("model:\n  default: template\n", encoding="utf-8")
            wrapper = self._write_migrate_wrapper(
                tmp, workspace=workspace, helper_path=str(HELPER_PATH),
                source_config=template,
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("TEMPLATE_COPIED", proc.stdout)
            self.assertEqual(profile_config.read_text(encoding="utf-8"), original)

    def _write_gate_wrapper(
        self,
        tmpdir: str,
        *,
        workspace: Path,
        helper_path: str,
        state_python: str | None = None,
    ) -> Path:
        fn = self._extract_functions(
            ["command_allowlist_state_present", "migrate_existing_toggles"],
            tmpdir,
        )
        wrapper = Path(tmpdir) / "gate-wrapper.sh"
        wrapper.write_text(
            f'''#!/bin/sh
set -eu
WORKSPACE_DIR="{workspace}"
HERMES_HOME="{workspace}"
RUNTIME_CONFIG="{workspace}/config.yaml"
JOSEMAR_SKILL_STATE="{helper_path}"
JOSEMAR_STATE_PYTHON="{state_python if state_python is not None else sys.executable}"
log() {{ echo "[init-test] $1"; }}
. "{fn}"
migrate_existing_toggles || exit 1
echo "MIGRATE_CONTINUED"
''',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return wrapper

    def test_helper_missing_with_default_state_present_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            state = workspace / "hermes" / "command-allowlist" / "default.json"
            state.parent.mkdir(parents=True)
            state.write_text("{not json", encoding="utf-8")  # malformed counts
            (workspace / "config.yaml").write_text(
                "model:\n  default: x\n", encoding="utf-8"
            )
            wrapper = self._write_gate_wrapper(
                tmp, workspace=workspace, helper_path=str(Path(tmp) / "no-such-helper.py")
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("MIGRATE_CONTINUED", proc.stdout)
            self.assertIn("ERROR", proc.stdout)

    def test_helper_missing_with_profile_state_present_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            state = (
                workspace / "hermes" / "command-allowlist" / "profiles" / "ops.json"
            )
            state.parent.mkdir(parents=True)
            state.write_text("{}", encoding="utf-8")
            (workspace / "config.yaml").write_text(
                "model:\n  default: x\n", encoding="utf-8"
            )
            wrapper = self._write_gate_wrapper(
                tmp, workspace=workspace, helper_path=str(Path(tmp) / "no-such-helper.py")
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("MIGRATE_CONTINUED", proc.stdout)

    def test_helper_missing_without_state_continues(self) -> None:
        # Historical skip behavior is preserved when NO allowlist state
        # exists anywhere.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "config.yaml").write_text(
                "model:\n  default: x\n", encoding="utf-8"
            )
            wrapper = self._write_gate_wrapper(
                tmp, workspace=workspace, helper_path=str(Path(tmp) / "no-such-helper.py")
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("MIGRATE_CONTINUED", proc.stdout)
            self.assertIn("skipping toggle migration", proc.stdout)

    def test_marker_present_rc2_is_fatal_in_shell_gate(self) -> None:
        # Regression: marker-present exit 2 (malformed/unreadable marker)
        # must be treated as FATAL by the shell gate itself. "$?" inside
        # "if ! cmd; then" is always 0, so the gate must capture the status
        # via "|| marker_rc=$?". A stub helper whose marker-present exits 2
        # must abort BEFORE the template overwrite even though the runtime
        # config would migrate cleanly.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "config.yaml").write_text(
                "command_allowlist: ['a']\n", encoding="utf-8"
            )
            stub_python = Path(tmp) / "stub-marker-fatal-python.sh"
            stub_python.write_text(
                '#!/bin/sh\n'
                '# Mimics: python <helper> <subcommand> ... ($1=helper, $2=subcommand)\n'
                'if [ "$2" = "marker-present" ]; then exit 2; fi\n'
                "exit 0\n",
                encoding="utf-8",
            )
            stub_python.chmod(0o755)
            wrapper = self._write_gate_wrapper(
                tmp,
                workspace=workspace,
                helper_path=str(HELPER_PATH),
                state_python=str(stub_python),
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("MIGRATE_CONTINUED", proc.stdout)
            self.assertIn("malformed/unreadable", proc.stdout)

    def test_apply_helper_missing_with_state_fails_closed(self) -> None:
        fn = None
        with tempfile.TemporaryDirectory() as tmp:
            fn = self._extract_functions(
                ["command_allowlist_state_present", "apply_sidecars_and_policy"], tmp
            )
            workspace = Path(tmp) / "ws"
            state = workspace / "hermes" / "command-allowlist" / "default.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"version":1,"command_allowlist":["x"]}', encoding="utf-8")
            wrapper = Path(tmp) / "apply-wrapper.sh"
            wrapper.write_text(
                f'''#!/bin/sh
set -eu
WORKSPACE_DIR="{workspace}"
JOSEMAR_SKILL_STATE="{Path(tmp) / 'no-such-helper.py'}"
log() {{ echo "[init-test] $1"; }}
. "{fn}"
apply_sidecars_and_policy || exit 3
echo "APPLY_CONTINUED"
''',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 3)
            self.assertNotIn("APPLY_CONTINUED", proc.stdout)
            self.assertIn("command-allowlist state present but helper unavailable", proc.stdout)

    def test_apply_helper_missing_without_state_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fn = self._extract_functions(
                ["command_allowlist_state_present", "apply_sidecars_and_policy"], tmp
            )
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            wrapper = Path(tmp) / "apply-wrapper.sh"
            wrapper.write_text(
                f'''#!/bin/sh
set -eu
WORKSPACE_DIR="{workspace}"
JOSEMAR_SKILL_STATE="{Path(tmp) / 'no-such-helper.py'}"
log() {{ echo "[init-test] $1"; }}
. "{fn}"
apply_sidecars_and_policy || exit 3
echo "APPLY_CONTINUED"
''',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("APPLY_CONTINUED", proc.stdout)

    def test_marker_finalized_before_template_overwrite(self) -> None:
        # First-upgrade: non-empty default allowlist migrates, then the
        # marker is finalized BEFORE the template is copied over the runtime.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "config.yaml").write_text(
                "command_allowlist: ['b', 'a']\n", encoding="utf-8"
            )
            template = Path(tmp) / "template.yaml"
            template.write_text("model:\n  default: template\n", encoding="utf-8")
            wrapper = self._write_migrate_wrapper(
                tmp, workspace=workspace, helper_path=str(HELPER_PATH),
                source_config=template,
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("TEMPLATE_COPIED", proc.stdout)
            marker = (
                workspace / "hermes" / "command-allowlist" / "migration-v1.json"
            )
            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                '{"version":1,"legacy_runtime_import_complete":true}\n',
            )

    def test_marker_present_skips_runtime_import_all_profiles(self) -> None:
        # Post-feature restart with a present marker: stale runtime values
        # must NOT create sidecars; migration proceeds straight to template.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            # A deliberately cleared allowlist leaves a stale runtime key.
            (workspace / "config.yaml").write_text(
                "command_allowlist: ['stale']\n", encoding="utf-8"
            )
            marker_dir = workspace / "hermes" / "command-allowlist"
            marker_dir.mkdir(parents=True)
            (marker_dir / "migration-v1.json").write_text(
                '{"version":1,"legacy_runtime_import_complete":true}\n',
                encoding="utf-8",
            )
            template = Path(tmp) / "template.yaml"
            template.write_text("model:\n  default: template\n", encoding="utf-8")
            wrapper = self._write_migrate_wrapper(
                tmp, workspace=workspace, helper_path=str(HELPER_PATH),
                source_config=template,
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("TEMPLATE_COPIED", proc.stdout)
            # No sidecar was resurrected from the stale runtime value.
            self.assertFalse(
                (marker_dir / "default.json").exists()
            )
            # The marker is still present (validated, not rewritten).
            self.assertTrue((marker_dir / "migration-v1.json").exists())

    def test_malformed_marker_is_fatal_before_template_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "config.yaml").write_text(
                "model:\n  default: x\n", encoding="utf-8"
            )
            marker_dir = workspace / "hermes" / "command-allowlist"
            marker_dir.mkdir(parents=True)
            (marker_dir / "migration-v1.json").write_text(
                "{broken", encoding="utf-8"
            )
            template = Path(tmp) / "template.yaml"
            template.write_text("model:\n  default: template\n", encoding="utf-8")
            wrapper = self._write_migrate_wrapper(
                tmp, workspace=workspace, helper_path=str(HELPER_PATH),
                source_config=template,
            )
            proc = subprocess.run(
                ["sh", str(wrapper)], capture_output=True, text=True, check=False
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("TEMPLATE_COPIED", proc.stdout)
            self.assertIn("ERROR", proc.stdout)


if __name__ == "__main__":
    unittest.main()
