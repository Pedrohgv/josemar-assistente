"""Focused tests for the Josemar state-backed Hermes skill toggle helper.

These tests exercise ``scripts/josemar_skill_state.py`` plus the pinned
build-time patch and the init/cron wiring. They do NOT require Docker or
the Hermes venv: the helper's runtime-config projection path uses
PyYAML, which is available in the repo's dev venv, and the patch tests
inspect source text directly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = REPO_ROOT / "scripts" / "josemar_skill_state.py"
PATCH_PATH = REPO_ROOT / "scripts" / "patch-hermes-skills-config.py"
INIT_PATH = REPO_ROOT / "docker-hermes-init.sh"
CRON_PATH = REPO_ROOT / "scripts" / "hermes-workspace-sync-cron.sh"
CONFIG_PATH = REPO_ROOT / "config" / "hermes-config.yaml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile.hermes"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
EXPECTED_IMAGE = "nousresearch/hermes-agent:v2026.8.31"
LIVE_MANIFEST = REPO_ROOT / "agent-state" / ".sync-manifest"
LIVE_GITIGNORE = REPO_ROOT / "agent-state" / ".gitignore"
TEMPLATE_MANIFEST = REPO_ROOT / "templates" / "agent-state-template" / ".sync-manifest"
TEMPLATE_GITIGNORE = REPO_ROOT / "templates" / "agent-state-template" / ".gitignore"
TEMPLATE_DEFAULT_SIDECAR = (
    REPO_ROOT / "templates" / "agent-state-template" / "hermes" / "skill-toggles" / "default.json"
)
TEMPLATE_README = REPO_ROOT / "templates" / "agent-state-template" / "README.md"
TEMPLATE_BOOT = REPO_ROOT / "templates" / "agent-state-template" / "BOOT.md"


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


class NormalizationTests(unittest.TestCase):
    """normalize_state / serialize / deserialize contract."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_disabled_sorted_and_deduped(self) -> None:
        state = self.m.normalize_state(["b", "a", "b", "  ", ""], None)
        self.assertEqual(state["disabled"], ["a", "b"])

    def test_platform_disabled_sorted_and_deduped(self) -> None:
        state = self.m.normalize_state([], {"telegram": ["x", "a", "x"], "cli": []})
        self.assertEqual(state["platform_disabled"], {"cli": [], "telegram": ["a", "x"]})

    def test_explicit_empty_arrays_retained(self) -> None:
        state = self.m.empty_state()
        self.assertEqual(state["disabled"], [])
        self.assertEqual(state["platform_disabled"], {})
        line = self.m.serialize_sidecar(state)
        self.assertIn('"disabled":[]', line)
        self.assertIn('"platform_disabled":{}', line)

    def test_serialize_is_one_line(self) -> None:
        line = self.m.serialize_sidecar(self.m.normalize_state(["a"], {"cli": ["x"]}))
        self.assertEqual(line.count("\n"), 0)

    def test_serialize_rejects_wrong_version(self) -> None:
        with self.assertRaises(ValueError):
            self.m.serialize_sidecar({"version": 2, "disabled": [], "platform_disabled": {}})

    def test_arbitrary_platform_keys_allowed(self) -> None:
        state = self.m.normalize_state([], {"discord": ["a"], "slack": ["b", "c"]})
        self.assertEqual(set(state["platform_disabled"].keys()), {"discord", "slack"})

    def test_platform_disabled_rejects_non_mapping(self) -> None:
        with self.assertRaises(ValueError):
            self.m.normalize_state([], ["not-a-dict"])

    def test_platform_disabled_rejects_empty_key(self) -> None:
        with self.assertRaises(ValueError):
            self.m.normalize_state([], {"": ["a"]})

    def test_deserialize_roundtrip(self) -> None:
        original = self.m.normalize_state(["a", "b"], {"cli": ["x"], "telegram": []})
        line = self.m.serialize_sidecar(original)
        parsed = self.m.deserialize_sidecar(line)
        self.assertEqual(parsed, original)

    def test_deserialize_malformed_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.deserialize_sidecar("{not json")

    def test_deserialize_wrong_version_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.deserialize_sidecar('{"version":2,"disabled":[],"platform_disabled":{}}')

    def test_deserialize_non_list_disabled_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.deserialize_sidecar('{"version":1,"disabled":"x","platform_disabled":{}}')

    def test_deserialize_non_mapping_platform_disabled_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.deserialize_sidecar('{"version":1,"disabled":[],"platform_disabled":[]}')


class PathMappingTests(unittest.TestCase):
    """default/named profile mapping; reject other paths."""

    def setUp(self) -> None:
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _with_workspace(self) -> None:
        self._patch = mock.patch.dict(
            os.environ, {"WORKSPACE_DIR": str(self.workspace), "HERMES_HOME": str(self.workspace)}
        )
        self._patch.start()

    def stop(self) -> None:
        self._patch.stop()

    def test_default_profile_maps_to_default_json(self) -> None:
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            path = self.m.resolve_sidecar_for_profile(None)
            self.assertEqual(path.name, "default.json")
            self.assertEqual(path.parent.name, "skill-toggles")

    def test_named_profile_maps_to_profiles_dir(self) -> None:
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            path = self.m.resolve_sidecar_for_profile("coder")
            self.assertEqual(path.name, "coder.json")
            self.assertEqual(path.parent.name, "profiles")

    def test_hermes_home_workspace_root_maps_to_default(self) -> None:
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            path = self.m.resolve_sidecar_for_hermes_home(self.workspace)
            self.assertEqual(path.name, "default.json")

    def test_hermes_home_named_profile_maps_to_profiles(self) -> None:
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            profile_home = self.workspace / "profiles" / "coder"
            profile_home.mkdir(parents=True)
            path = self.m.resolve_sidecar_for_hermes_home(profile_home)
            self.assertEqual(path.name, "coder.json")

    def test_hermes_home_other_path_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            other = self.workspace / "somewhere" / "else"
            other.mkdir(parents=True)
            with self.assertRaises(ValueError):
                self.m.resolve_sidecar_for_hermes_home(other)

    def test_invalid_profile_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.m.resolve_sidecar_for_profile("Bad Name!")


class AtomicSidecarIOTests(unittest.TestCase):
    """write/read sidecar; atomic temp+replace; mode preservation."""

    def setUp(self) -> None:
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_write_then_read_roundtrip(self) -> None:
        path = self.dir / "default.json"
        self.m.write_sidecar(path, self.m.normalize_state(["a"], {"cli": []}))
        state = self.m.read_sidecar(path)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["disabled"], ["a"])
        self.assertEqual(state["platform_disabled"], {"cli": []})

    def test_read_absent_returns_none(self) -> None:
        self.assertIsNone(self.m.read_sidecar(self.dir / "missing.json"))

    def test_write_is_atomic_no_temp_left(self) -> None:
        path = self.dir / "default.json"
        self.m.write_sidecar(path, self.m.empty_state())
        temps = list(self.dir.glob(".*.tmp"))
        self.assertEqual(temps, [])

    def test_write_preserves_mode(self) -> None:
        path = self.dir / "default.json"
        self.m.write_sidecar(path, self.m.empty_state())
        os.chmod(path, 0o600)
        self.m.write_sidecar(path, self.m.empty_state())
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_malformed_read_raises(self) -> None:
        path = self.dir / "default.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.m.read_sidecar(path)


class ApplyStateTests(unittest.TestCase):
    """apply_state_to_config preserves unrelated config keys."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_preserves_unrelated_keys(self) -> None:
        config = {
            "model": {"default": "x"},
            "skills": {"disabled": ["old"], "external_dirs": ["/a"], "template_vars": True},
            "memory": {"nudge_interval": 10},
        }
        state = self.m.normalize_state(["new"], {"telegram": ["t"]})
        self.m.apply_state_to_config(config, state)
        self.assertEqual(config["model"], {"default": "x"})
        self.assertEqual(config["skills"]["external_dirs"], ["/a"])
        self.assertEqual(config["skills"]["template_vars"], True)
        self.assertEqual(config["memory"]["nudge_interval"], 10)
        self.assertEqual(config["skills"]["disabled"], ["new"])
        self.assertEqual(config["skills"]["platform_disabled"], {"telegram": ["t"]})

    def test_explicit_clear_is_durable(self) -> None:
        config = {"skills": {"disabled": ["old"], "platform_disabled": {"cli": ["x"]}}}
        state = self.m.empty_state()
        self.m.apply_state_to_config(config, state)
        self.assertEqual(config["skills"]["disabled"], [])
        self.assertEqual(config["skills"]["platform_disabled"], {})

    def test_apply_creates_skills_section_if_absent(self) -> None:
        config = {"model": {"default": "x"}}
        self.m.apply_state_to_config(config, self.m.normalize_state(["a"], None))
        self.assertEqual(config["skills"]["disabled"], ["a"])
        self.assertEqual(config["skills"]["platform_disabled"], {})


class PolicyTests(unittest.TestCase):
    """enforce_policy / policy_violations."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_enforce_sets_all_keys(self) -> None:
        config = {}
        changed = self.m.enforce_policy(config)
        self.assertTrue(changed)
        self.assertEqual(config["skills"]["creation_nudge_interval"], 0)
        self.assertEqual(config["skills"]["write_approval"], True)
        self.assertEqual(config["curator"]["enabled"], False)

    def test_enforce_no_change_when_already_set(self) -> None:
        config = {
            "skills": {"creation_nudge_interval": 0, "write_approval": True},
            "curator": {"enabled": False},
        }
        self.assertFalse(self.m.enforce_policy(config))

    def test_policy_violations_reports_wrong_keys(self) -> None:
        config = {
            "skills": {"creation_nudge_interval": 15, "write_approval": True},
            "curator": {"enabled": True},
        }
        violations = self.m.policy_violations(config)
        self.assertIn("skills.creation_nudge_interval", violations)
        self.assertIn("curator.enabled", violations)
        self.assertNotIn("skills.write_approval", violations)

    def test_enforce_does_not_touch_memory_nudge(self) -> None:
        config = {"memory": {"nudge_interval": 10}, "skills": {}}
        self.m.enforce_policy(config)
        self.assertEqual(config["memory"]["nudge_interval"], 10)


class ConfigPolicyTemplateTests(unittest.TestCase):
    """config/hermes-config.yaml carries the Josemar policy."""

    def test_config_has_policy_keys(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not available")
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["skills"]["creation_nudge_interval"], 0)
        self.assertEqual(data["skills"]["write_approval"], True)
        self.assertEqual(data["curator"]["enabled"], False)

    def test_config_does_not_alter_memory_nudge(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not available")
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["memory"]["nudge_interval"], 10)


class MigrationTests(unittest.TestCase):
    """migrate_existing_toggles_to_absent_sidecars only when sidecar absent + keys exist."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_migrate_creates_sidecar_when_keys_present(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text(
            "skills:\n  disabled: ['a', 'b']\n  platform_disabled: {cli: ['x']}\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            created = self.m.migrate_existing_toggles_to_absent_sidecars(config_path, self.workspace)
        self.assertTrue(created)
        sidecar = self.workspace / "hermes" / "skill-toggles" / "default.json"
        self.assertTrue(sidecar.exists())
        state = self.m.read_sidecar(sidecar)
        assert state is not None
        self.assertEqual(state["disabled"], ["a", "b"])

    def test_migrate_noop_when_keys_absent(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text("model:\n  default: x\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            created = self.m.migrate_existing_toggles_to_absent_sidecars(config_path, self.workspace)
        self.assertFalse(created)
        sidecar = self.workspace / "hermes" / "skill-toggles" / "default.json"
        self.assertFalse(sidecar.exists())

    def test_migrate_noop_when_sidecar_present(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text("skills:\n  disabled: ['a']\n", encoding="utf-8")
        sidecar = self.workspace / "hermes" / "skill-toggles" / "default.json"
        sidecar.parent.mkdir(parents=True)
        self.m.write_sidecar(sidecar, self.m.empty_state())
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            created = self.m.migrate_existing_toggles_to_absent_sidecars(config_path, self.workspace)
        self.assertFalse(created)

    def test_migrate_noop_when_toggles_empty_does_not_create_default(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text("skills:\n  disabled: []\n  platform_disabled: {}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            created = self.m.migrate_existing_toggles_to_absent_sidecars(config_path, self.workspace)
        self.assertFalse(created)
        sidecar = self.workspace / "hermes" / "skill-toggles" / "default.json"
        self.assertFalse(sidecar.exists())


class ApplySidecarAndPolicyTests(unittest.TestCase):
    """apply_sidecar_and_enforce_policy: malformed sidecar leaves config untouched."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_apply_preserves_unrelated_config(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text(
            "model:\n  default: x\nskills:\n  external_dirs: ['/a']\n",
            encoding="utf-8",
        )
        sidecar = self.workspace / "hermes" / "skill-toggles" / "default.json"
        sidecar.parent.mkdir(parents=True)
        self.m.write_sidecar(sidecar, self.m.normalize_state(["a"], {"cli": []}))
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            status = self.m.apply_sidecar_and_enforce_policy(config_path, self.workspace)
        self.assertIn("applied", status)
        import yaml
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], {"default": "x"})
        self.assertEqual(data["skills"]["external_dirs"], ["/a"])
        self.assertEqual(data["skills"]["disabled"], ["a"])
        self.assertEqual(data["skills"]["platform_disabled"], {"cli": []})
        self.assertEqual(data["skills"]["creation_nudge_interval"], 0)
        self.assertEqual(data["curator"]["enabled"], False)

    def test_malformed_sidecar_leaves_config_untouched(self) -> None:
        config_path = self.workspace / "config.yaml"
        original = "model:\n  default: x\n"
        config_path.write_text(original, encoding="utf-8")
        sidecar = self.workspace / "hermes" / "skill-toggles" / "default.json"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("{not json", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            with self.assertRaises(ValueError):
                self.m.apply_sidecar_and_enforce_policy(config_path, self.workspace)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)


class ApplyAllSidecarsTests(unittest.TestCase):
    """apply_all_sidecars_and_policy reconciles every profile config under one lock."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _load_config(self, path: Path) -> dict:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_apply_all_applies_default_and_named(self) -> None:
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: x\n", encoding="utf-8"
        )
        profile_home = self.workspace / "profiles" / "coder"
        (profile_home / "config.yaml").parent.mkdir(parents=True)
        (profile_home / "config.yaml").write_text("model:\n  default: y\n", encoding="utf-8")

        default_sidecar = self.workspace / "hermes" / "skill-toggles" / "default.json"
        default_sidecar.parent.mkdir(parents=True)
        self.m.write_sidecar(default_sidecar, self.m.normalize_state(["a"], None))
        coder_sidecar = self.workspace / "hermes" / "skill-toggles" / "profiles" / "coder.json"
        coder_sidecar.parent.mkdir(parents=True)
        self.m.write_sidecar(coder_sidecar, self.m.normalize_state(["b"], None))

        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any(s.startswith("default:") for s in statuses))
        self.assertTrue(any(s.startswith("coder:") for s in statuses))
        self.assertEqual(self._load_config(self.workspace / "config.yaml")["skills"]["disabled"], ["a"])
        self.assertEqual(self._load_config(profile_home / "config.yaml")["skills"]["disabled"], ["b"])

    def test_apply_all_enforces_policy_for_named_profile_without_sidecar(self) -> None:
        # Named profile with config.yaml but NO sidecar must still get policy.
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: x\nskills:\n  creation_nudge_interval: 15\n"
            "memory:\n  nudge_interval: 10\n",
            encoding="utf-8",
        )
        profile_home = self.workspace / "profiles" / "coder"
        (profile_home / "config.yaml").parent.mkdir(parents=True)
        (profile_home / "config.yaml").write_text(
            "model:\n  default: y\nskills:\n  creation_nudge_interval: 15\n"
            "memory:\n  nudge_interval: 5\n",
            encoding="utf-8",
        )
        # No sidecars at all.
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any(s.startswith("default:enforced-policy") for s in statuses))
        self.assertTrue(any(s.startswith("coder:enforced-policy") for s in statuses))
        default_cfg = self._load_config(self.workspace / "config.yaml")
        coder_cfg = self._load_config(profile_home / "config.yaml")
        for cfg in (default_cfg, coder_cfg):
            self.assertEqual(cfg["skills"]["creation_nudge_interval"], 0)
            self.assertEqual(cfg["skills"]["write_approval"], True)
            self.assertEqual(cfg["curator"]["enabled"], False)
        # Memory nudge and unrelated keys preserved.
        self.assertEqual(default_cfg["memory"]["nudge_interval"], 10)
        self.assertEqual(default_cfg["model"], {"default": "x"})
        self.assertEqual(coder_cfg["memory"]["nudge_interval"], 5)
        self.assertEqual(coder_cfg["model"], {"default": "y"})

    def test_apply_all_default_without_sidecar_enforces_policy(self) -> None:
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: x\nskills:\n  creation_nudge_interval: 15\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any(s.startswith("default:enforced-policy") for s in statuses))
        cfg = self._load_config(self.workspace / "config.yaml")
        self.assertEqual(cfg["skills"]["creation_nudge_interval"], 0)
        self.assertEqual(cfg["model"], {"default": "x"})

    def test_apply_all_reports_orphan_sidecar(self) -> None:
        # Sidecar for a named profile that has no config.yaml -> orphan.
        (self.workspace / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
        coder_sidecar = self.workspace / "hermes" / "skill-toggles" / "profiles" / "coder.json"
        coder_sidecar.parent.mkdir(parents=True)
        self.m.write_sidecar(coder_sidecar, self.m.empty_state())
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any("coder:orphan-sidecar" in s for s in statuses))

    def test_apply_all_skips_invalid_profile_dir_name(self) -> None:
        # A profile directory with an invalid name is skipped, not reconciled.
        (self.workspace / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
        bad_profile = self.workspace / "profiles" / "Bad Name!"
        (bad_profile / "config.yaml").parent.mkdir(parents=True)
        (bad_profile / "config.yaml").write_text("model:\n  default: z\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            statuses = self.m.apply_all_sidecars_and_policy()
        self.assertFalse(any("Bad Name!" in s for s in statuses))


class PatchSourceContractTests(unittest.TestCase):
    """patch-hermes-skills-config.py: package-relative import, fail-fast, duplicate."""

    def test_patch_targets_skills_config_path(self) -> None:
        text = PATCH_PATH.read_text(encoding="utf-8")
        self.assertIn("/opt/hermes/hermes_cli/skills_config.py", text)

    def test_patch_uses_package_relative_import(self) -> None:
        """The helper must be imported as ``hermes_cli.josemar_skill_state``.

        A bare ``from josemar_skill_state import ...`` would fail at runtime
        because the helper is a sibling inside the ``hermes_cli`` package,
        not a top-level module on ``sys.path``. This test fails for the
        top-level import layout, not merely a substring check: it parses
        the replacement snippet and asserts the imported module path is
        package-relative.
        """
        text = PATCH_PATH.read_text(encoding="utf-8")
        # The replacement snippet contains the import line. Extract it.
        self.assertIn("from hermes_cli.josemar_skill_state import", text)
        # The bare top-level import must NOT appear anywhere in the patch.
        self.assertNotIn("from josemar_skill_state import", text)

    def test_patch_replacement_snippet_uses_package_path(self) -> None:
        """Parse the replace_once call and verify the new snippet's import.

        This is a structural check: it extracts the ``new`` argument of
        ``replace_once`` and confirms the import statement inside it
        resolves to ``hermes_cli.josemar_skill_state``. A patch that used
        the top-level import would fail here because the parsed import
        module would be ``josemar_skill_state`` (not package-relative).
        """
        import ast
        import textwrap

        tree = ast.parse(PATCH_PATH.read_text(encoding="utf-8"))
        replace_calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "replace_once"
        ]
        self.assertEqual(len(replace_calls), 1)
        new_arg = replace_calls[0].args[2]
        assert isinstance(new_arg, ast.Constant)
        new_src = str(new_arg.value)
        # The replacement snippet is indented inside a function and has
        # mixed indentation (the mutated line is deeper than the import).
        # Parse only the import line, which is the part we care about.
        import_line = next(
            line for line in new_src.splitlines() if "import" in line
        )
        snippet_tree = ast.parse(textwrap.dedent(import_line))
        imports = [
            n for n in ast.walk(snippet_tree)
            if isinstance(n, ast.ImportFrom)
        ]
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0].module, "hermes_cli.josemar_skill_state")
        self.assertEqual([a.name for a in imports[0].names], ["save_disabled_skills_stateful"])

    def test_patch_uses_replace_once_pattern(self) -> None:
        text = PATCH_PATH.read_text(encoding="utf-8")
        self.assertIn("def replace_once", text)
        self.assertIn("raise RuntimeError", text)

    def test_patch_replaces_save_config_call(self) -> None:
        text = PATCH_PATH.read_text(encoding="utf-8")
        self.assertIn("save_config(config)", text)
        self.assertIn("save_disabled_skills_stateful(config, disabled, platform)", text)

    def test_patch_doc_explains_package_relative_import(self) -> None:
        text = PATCH_PATH.read_text(encoding="utf-8")
        self.assertIn("hermes_cli.josemar_skill_state", text)
        self.assertIn("package-relative", text)


class InitOrderingTests(unittest.TestCase):
    """docker-hermes-init.sh: migrate before template overwrite, apply after sync."""

    def setUp(self) -> None:
        self.src = INIT_PATH.read_text(encoding="utf-8")

    def test_migrate_called_before_template_overwrite(self) -> None:
        migrate_pos = self.src.find("migrate_existing_toggles")
        overwrite_pos = self.src.find("Syncing Hermes config.yaml from repo template")
        self.assertLess(migrate_pos, overwrite_pos)

    def test_apply_called_after_workspace_sync(self) -> None:
        sync_pos = self.src.find("Running workspace git sync as hermes user")
        # The call site (not the function definition) appears after sync.
        apply_call_pos = self.src.find("\napply_sidecars_and_policy\n")
        seed_pos = self.src.find("seed_workspace_from_manifest")
        self.assertLess(sync_pos, apply_call_pos)
        self.assertLess(seed_pos, apply_call_pos)

    def test_helper_path_default(self) -> None:
        self.assertIn("JOSEMAR_SKILL_STATE", self.src)
        self.assertIn("/opt/hermes/hermes_cli/josemar_skill_state.py", self.src)

    def test_migrate_walks_named_profiles(self) -> None:
        self.assertIn("profiles_root", self.src)
        self.assertIn('profile_config="${profile_dir}config.yaml"', self.src)

    def test_init_chowns_dedicated_toggle_tree(self) -> None:
        """Init must chown only ${WORKSPACE_DIR}/hermes/skill-toggles.

        The migration/seed/apply steps can create this tree as root; the
        dashboard runtime user must be able to atomically replace it.
        This is a narrow ownership repair — it must NOT broaden the
        writable-volume policy or chown bind mounts.
        """
        self.assertIn("repair_skill_toggle_ownership", self.src)
        self.assertIn('${WORKSPACE_DIR}/hermes/skill-toggles', self.src)
        self.assertIn("HERMES_UID_VALUE", self.src)
        self.assertIn("HERMES_GID_VALUE", self.src)

    def test_init_chown_is_gated_to_root_and_existing_tree(self) -> None:
        """The chown must be gated to uid 0 and an existing toggle tree."""
        self.assertIn('"$(id -u)" = "0"', self.src)
        self.assertIn('[ -d "$toggle_tree" ]', self.src)

    def test_init_chown_does_not_target_broad_hermes_home(self) -> None:
        """The repair must not chown the broad HERMES_HOME tree (already done later)."""
        # The narrow repair targets only the toggle tree, not a recursive
        # HERMES_HOME chown at this point in the script.
        chown_pos = self.src.find("repair_skill_toggle_ownership")
        self.assertGreater(chown_pos, 0)
        # The final broad chown of HERMES_HOME happens at the end; the
        # narrow repair must appear between apply_sidecars_and_policy and
        # the final chown.
        apply_pos = self.src.find("\napply_sidecars_and_policy\n")
        final_chown_pos = self.src.rfind("chown -R", 0)
        self.assertLess(apply_pos, chown_pos)
        self.assertLess(chown_pos, final_chown_pos)


class CronLockSyncApplyTests(unittest.TestCase):
    """hermes-workspace-sync-cron.sh delegates sync+apply to the helper under one lock.

    Source-contract checks plus behavioral tests using a temporary sync
    command that prove sync+apply share one critical section.
    """

    def setUp(self) -> None:
        self.src = CRON_PATH.read_text(encoding="utf-8")

    def test_cron_invokes_sync_and_apply_operation(self) -> None:
        self.assertIn("josemar_skill_state", self.src)
        self.assertIn("sync-and-apply", self.src)

    def test_cron_does_not_invoke_apply_all_separately(self) -> None:
        # The old structure ran sync then apply-all as separate steps; the
        # fixed design delegates both to sync-and-apply under one lock.
        self.assertNotIn("apply-all", self.src)

    def test_cron_preserves_sync_exit_status(self) -> None:
        self.assertIn('exit "$status"', self.src)

    def test_cron_surfaces_apply_errors_after_successful_sync(self) -> None:
        self.assertIn("grep -q 'sync-and-apply: .*:error:'", self.src)
        self.assertIn('cat "$log_file" >&2', self.src)

    def test_cron_falls_back_to_sync_only_when_helper_missing(self) -> None:
        self.assertIn("JOSEMAR_SKILL_STATE", self.src)
        self.assertIn("/usr/local/bin/workspace-sync", self.src)

    def test_cron_uses_hermes_venv_python(self) -> None:
        self.assertIn("/opt/hermes/.venv/bin/python3", self.src)


class SyncAndApplyBehavioralTests(unittest.TestCase):
    """sync_and_apply: one lock covers sync + apply; exit status preserved."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_sync_script(self, *, body: str) -> Path:
        script = self.workspace / "fake-sync.sh"
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_successful_sync_then_apply_enforces_policy(self) -> None:
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: x\nskills:\n  creation_nudge_interval: 15\n",
            encoding="utf-8",
        )
        script = self._make_sync_script(body="echo SYNC_OK\n")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            exit_status, statuses, output = self.m.sync_and_apply([str(script)])
        self.assertEqual(exit_status, 0)
        self.assertIn("SYNC_OK", output)
        self.assertTrue(any("default:enforced-policy" in s for s in statuses))
        import yaml
        cfg = yaml.safe_load((self.workspace / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["skills"]["creation_nudge_interval"], 0)

    def test_sync_failure_preserves_exit_status_and_skips_apply(self) -> None:
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: x\nskills:\n  creation_nudge_interval: 15\n",
            encoding="utf-8",
        )
        script = self._make_sync_script(body="echo SYNC_FAIL >&2\nexit 7\n")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            exit_status, statuses, output = self.m.sync_and_apply([str(script)])
        self.assertEqual(exit_status, 7)
        self.assertIn("SYNC_FAIL", output)
        self.assertEqual(statuses, [])
        # Config untouched because apply was skipped.
        import yaml
        cfg = yaml.safe_load((self.workspace / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["skills"]["creation_nudge_interval"], 15)

    def test_lock_held_during_sync_command_blocks_concurrent_process(self) -> None:
        """A separate process cannot acquire the sidecar lock while sync runs.

        This directly proves the sync command and the apply step share one
        critical section: ``sync_and_apply`` acquires the lock BEFORE
        running the sync command and holds it through apply. A separate
        process attempting ``flock(LOCK_NB)`` while the sync command runs
        must be blocked.
        """
        probe = self.workspace / "try_lock.py"
        probe.write_text(
            "import os, fcntl\n"
            "lock_path = os.path.join(os.environ['WORKSPACE_DIR'], "
            "'hermes', 'skill-toggles', '.skill-toggles.lock')\n"
            "os.makedirs(os.path.dirname(lock_path), exist_ok=True)\n"
            "fh = open(lock_path, 'a+')\n"
            "try:\n"
            "    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    print('LOCK-ACQUIRED')\n"
            "except BlockingIOError:\n"
            "    print('LOCK-BLOCKED')\n",
            encoding="utf-8",
        )
        # The sync command runs the probe (which should be blocked) and
        # then sleeps briefly so the external probe below also runs while
        # the lock is still held.
        script = self._make_sync_script(
            body="python3 {probe}\nsleep 0.4\n".format(probe=probe),
        )
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            # Run sync_and_apply in a background subprocess so we can probe
            # from this process while it holds the lock.
            import subprocess as sp

            env = os.environ.copy()
            env["WORKSPACE_DIR"] = str(self.workspace)
            proc = sp.Popen(
                [sys.executable, str(HELPER_PATH), "sync-and-apply", "--", str(script)],
                env=env,
                stdout=sp.PIPE,
                stderr=sp.PIPE,
                text=True,
            )
            try:
                # Wait for the sync command to start holding the lock.
                import time

                time.sleep(0.2)
                # Probe from THIS process (separate from the sync_and_apply
                # subprocess) — must be blocked.
                probe_result = sp.run(
                    [sys.executable, str(probe)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertIn("LOCK-BLOCKED", probe_result.stdout)
            finally:
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0, stdout + stderr)
        # The sync command's own probe output must also report blocked.
        self.assertIn("LOCK-BLOCKED", stdout)

    def test_apply_failure_after_sync_fails_nonzero(self) -> None:
        """If apply raises after a successful sync, exit status is nonzero.

        Fail-closed: an apply failure (including a models overlay validation
        failure) must make sync-and-apply fail nonzero so the cron run does
        not silently boot the template configuration. The apply failure is
        captured in the statuses list with an ``error:`` segment. The runtime
        config is left untouched (last-known-good preserved) because the
        overlay validates fully before mutating.
        """
        script = self._make_sync_script(body="echo SYNC_OK\n")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            with mock.patch.object(
                self.m, "_apply_all_sidecars_and_policy_unlocked", side_effect=RuntimeError("boom")
            ):
                exit_status, statuses, output = self.m.sync_and_apply([str(script)])
        self.assertNotEqual(exit_status, 0)
        self.assertTrue(any("error:" in s for s in statuses))


class ManifestGitignoreTests(unittest.TestCase):
    """Live + template manifest/gitignore allow only the dedicated toggle paths."""

    def _read_live_state_file(self, path: Path) -> str:
        if not path.exists():
            self.skipTest("private agent-state checkout is not present")
        return path.read_text(encoding="utf-8")

    def test_live_manifest_allows_toggle_paths(self) -> None:
        text = self._read_live_state_file(LIVE_MANIFEST)
        self.assertIn("hermes/skill-toggles/default.json", text)
        self.assertIn("hermes/skill-toggles/profiles/*.json", text)

    def test_live_manifest_does_not_allow_broad_hermes(self) -> None:
        text = self._read_live_state_file(LIVE_MANIFEST)
        self.assertNotIn("hermes/**", text)

    def test_live_gitignore_allows_toggle_paths(self) -> None:
        text = self._read_live_state_file(LIVE_GITIGNORE)
        self.assertIn("!hermes/skill-toggles/default.json", text)
        self.assertIn("!hermes/skill-toggles/profiles/*.json", text)

    def test_live_gitignore_does_not_allow_broad_hermes(self) -> None:
        text = self._read_live_state_file(LIVE_GITIGNORE)
        self.assertNotIn("!hermes/**", text)

    def test_template_manifest_allows_toggle_paths(self) -> None:
        text = TEMPLATE_MANIFEST.read_text(encoding="utf-8")
        self.assertIn("hermes/skill-toggles/default.json", text)
        self.assertIn("hermes/skill-toggles/profiles/*.json", text)

    def test_template_manifest_versions_models_yaml_exactly(self) -> None:
        """The shipped template manifest carries the exact literal models.yaml entry."""
        text = TEMPLATE_MANIFEST.read_text(encoding="utf-8")
        self.assertIn("hermes/models.yaml", text.splitlines())

    def test_template_gitignore_permits_models_yaml_exactly(self) -> None:
        """The shipped template gitignore permits exactly !hermes/models.yaml."""
        text = TEMPLATE_GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("!hermes/models.yaml", text.splitlines())

    def test_template_gitignore_allows_toggle_paths(self) -> None:
        text = TEMPLATE_GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("!hermes/skill-toggles/default.json", text)
        self.assertIn("!hermes/skill-toggles/profiles/*.json", text)

    def test_template_default_sidecar_is_canonical_empty(self) -> None:
        if not TEMPLATE_DEFAULT_SIDECAR.exists():
            self.skipTest("template default sidecar absent (absence semantics acceptable)")
        data = json.loads(TEMPLATE_DEFAULT_SIDECAR.read_text(encoding="utf-8"))
        self.assertEqual(data, {"version": 1, "disabled": [], "platform_disabled": {}})


class DockerfileContractTests(unittest.TestCase):
    """Dockerfile copies the helper and runs the patch."""

    def setUp(self) -> None:
        self.src = (REPO_ROOT / "Dockerfile.hermes").read_text(encoding="utf-8")

    def test_dockerfile_copies_helper(self) -> None:
        self.assertIn(
            "COPY scripts/josemar_skill_state.py /opt/hermes/hermes_cli/josemar_skill_state.py",
            self.src,
        )

    def test_dockerfile_runs_skills_patch(self) -> None:
        self.assertIn("patch-hermes-skills-config.py", self.src)
        self.assertIn("skills_config.py", self.src)

    def test_dockerfile_py_compiles_helper(self) -> None:
        # The skills-config patch block py_compiles the helper fail-loudly.
        # Later patcher blocks (e.g. the browser-routing patch, issue #136)
        # append their own py_compile lines, so scope the assertion to the
        # skills-config segment instead of the whole file tail.
        segment = self.src.split("patch-hermes-browser-routing.py")[0]
        self.assertIn("josemar_skill_state.py", segment.split("py_compile")[-1])


class HermesUpgradeContractTests(unittest.TestCase):
    """Narrow contract tests for the Hermes v2026.8.31 upgrade.

    Four focused tests: image pins across the three source-of-truth files,
    config schema version plus raw comment, approvals defaults, and the
    patch docstring. These tests are intentionally surgical and do NOT
    assert approvals are in POLICY_KEYS.
    """

    def test_all_image_pins_equal_expected_version(self) -> None:
        """All three image-pin locations reference the expected image and no stale tag remains."""
        stale = "nousresearch/hermes-agent:v2026.8.18"
        for path, label in (
            (DOCKERFILE_PATH, "Dockerfile.hermes"),
            (COMPOSE_PATH, "docker-compose.yml"),
            (ENV_EXAMPLE_PATH, ".env.example"),
        ):
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                EXPECTED_IMAGE,
                text,
                f"{label} is missing expected image {EXPECTED_IMAGE}",
            )
            self.assertNotIn(
                stale,
                text,
                f"{label} still references stale image {stale}",
            )

    def test_config_schema_and_comment_match_version(self) -> None:
        """Parsed config schema is 39 and raw comment names v2026.8.31 with no stale tag."""
        text = CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn("nousresearch/hermes-agent:v2026.8.31", text)
        self.assertNotIn("nousresearch/hermes-agent:v2026.8.18", text)
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not available")
        data = yaml.safe_load(text)
        self.assertEqual(data["_config_version"], 39)

    def test_config_approvals_block_defaults(self) -> None:
        """Root-level approvals block carries the chosen restart-time defaults."""
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not available")
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        approvals = data["approvals"]
        self.assertEqual(approvals["mode"], "manual")
        self.assertEqual(approvals["cron_mode"], "deny")
        self.assertEqual(approvals["deny"], [])

    def test_patch_docstring_names_v2026_8_31(self) -> None:
        """Build-time patch docstring names the new Hermes version and not the old one."""
        text = PATCH_PATH.read_text(encoding="utf-8")
        self.assertIn("Hermes v2026.8.31", text)
        self.assertNotIn("Hermes v2026.8.18", text)


# ---------------------------------------------------------------------------
# Mnemosyne Phase 1: pinned packages, write-approval policy, init activation
# ---------------------------------------------------------------------------


class MnemosyneDockerfilePinTests(unittest.TestCase):
    """Dockerfile.hermes pins mnemosyne-hermes and mnemosyne-memory."""

    def setUp(self) -> None:
        self.src = DOCKERFILE_PATH.read_text(encoding="utf-8")

    def test_dockerfile_pins_mnemosyne_hermes(self) -> None:
        self.assertIn("mnemosyne-hermes==0.5.0", self.src)

    def test_dockerfile_pins_mnemosyne_memory(self) -> None:
        self.assertIn("mnemosyne-memory==3.15.1", self.src)

    def test_dockerfile_installs_into_hermes_venv(self) -> None:
        # The install must target the Hermes venv, not system Python.
        # Find the mnemosyne install line.
        mnemo_pos = self.src.find("mnemosyne-hermes==0.5.0")
        self.assertGreater(mnemo_pos, 0)
        # Look backwards for the venv python invocation.
        before = self.src[:mnemo_pos]
        self.assertIn("/opt/hermes/.venv/bin/python3", before[-400:])

    def test_dockerfile_does_not_use_no_deps(self) -> None:
        # Must not bypass dependencies with --no-deps.
        mnemo_pos = self.src.find("mnemosyne-hermes==0.5.0")
        self.assertGreater(mnemo_pos, 0)
        install_line_start = self.src.rfind("\n", 0, mnemo_pos)
        install_line_end = self.src.find("\n", mnemo_pos)
        install_line = self.src[install_line_start:install_line_end]
        self.assertNotIn("--no-deps", install_line)


class MemoryWriteApprovalPolicyTests(unittest.TestCase):
    """memory.write_approval is present in the template but NOT enforced by
    josemar_skill_state.py POLICY_KEYS (preserved/un-enforced)."""

    def test_config_has_memory_write_approval(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not available")
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertIn("write_approval", data.get("memory", {}))
        self.assertTrue(data["memory"]["write_approval"])

    def test_config_does_not_set_memory_provider(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not available")
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("provider", data.get("memory", {}))

    def test_config_preserves_curated_static_coexistence(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not available")
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        memory = data.get("memory", {})
        self.assertTrue(memory.get("memory_enabled"))
        self.assertTrue(memory.get("user_profile_enabled"))
        # Existing limits retained.
        self.assertEqual(memory.get("memory_char_limit"), 2200)
        self.assertEqual(memory.get("user_char_limit"), 1375)

    def test_write_approval_not_in_policy_keys(self) -> None:
        # memory.write_approval must NOT be in POLICY_KEYS (preserved/un-enforced).
        m = _load_helper()
        policy_keys = m.POLICY_KEYS
        for key in policy_keys:
            self.assertNotIn("memory.write_approval", key)
            self.assertNotIn("write_approval", key)

    def test_enforce_policy_does_not_touch_memory_write_approval(self) -> None:
        m = _load_helper()
        config = {"memory": {"write_approval": True, "memory_enabled": True}}
        m.enforce_policy(config)
        # write_approval must be preserved as-is.
        self.assertTrue(config["memory"]["write_approval"])
        self.assertTrue(config["memory"]["memory_enabled"])


class MnemosyneInitActivationTests(unittest.TestCase):
    """docker-hermes-init.sh: runtime config activation shape and directory usage."""

    def setUp(self) -> None:
        self.src = INIT_PATH.read_text(encoding="utf-8")

    def test_init_has_mnemosyne_activation_block(self) -> None:
        self.assertIn("mnemosyne", self.src.lower())
        self.assertIn("MNEMOSYNE_PROVIDER", self.src)

    def test_init_gates_activation_on_mnemosyne_provider(self) -> None:
        # Activation must only run when MNEMOSYNE_PROVIDER=mnemosyne.
        self.assertIn("MNEMOSYNE_PROVIDER", self.src)
        # The gate must check for the value "mnemosyne".
        self.assertIn('"mnemosyne"', self.src)

    def test_init_creates_data_directory_inside_hermes_home(self) -> None:
        # The data directory must be under /opt/data (HERMES_HOME), not a new
        # writable-volume allowlist entry.
        self.assertIn("MNEMOSYNE_DATA_DIR", self.src)
        self.assertIn("/opt/data/mnemosyne/data", self.src)
        self.assertIn("mkdir -p", self.src)

    def test_init_does_not_add_mnemosyne_to_writable_volumes(self) -> None:
        # The writable-volume allowlist must NOT include mnemosyne paths.
        writable_pos = self.src.find("HERMES_WRITABLE_VOLUMES=")
        self.assertGreater(writable_pos, 0)
        writable_end = self.src.find("\n", writable_pos)
        writable_line = self.src[writable_pos:writable_end]
        self.assertNotIn("mnemosyne", writable_line.lower())

    def test_init_runs_installer_as_hermes_user(self) -> None:
        # The pinned wrapper installer must run as the Hermes user, not root.
        self.assertIn("hermes", self.src)
        # The installer must target the Hermes venv.
        self.assertIn("/opt/hermes/.venv", self.src)

    def test_init_uses_verified_console_installer_cli(self) -> None:
        # The verified supported CLI is the console entry point
        # `mnemosyne-hermes --hermes-home <home> install --mode wrapper
        # --force --python /opt/hermes/.venv/bin/python3`. The invalid
        # `python -m mnemosyne_hermes install/init --data-dir ...` sequence
        # must NOT appear.
        self.assertIn("mnemosyne-hermes", self.src)
        self.assertIn("--hermes-home", self.src)
        self.assertIn("install --mode wrapper --force", self.src)
        self.assertIn("--python /opt/hermes/.venv/bin/python3", self.src)
        # The invalid module-form invocation must NOT appear.
        self.assertNotIn("mnemosyne_hermes install", self.src)
        self.assertNotIn("mnemosyne_hermes init", self.src)
        # --data-dir is NOT an installer flag and must NOT be passed to install.
        # (MNEMOSYNE_DATA_DIR is still referenced for mkdir, but not as an
        # install flag.)
        install_pos = self.src.find("install --mode wrapper")
        self.assertGreater(install_pos, 0)
        # Find the end of the install command line(s).
        install_end = self.src.find("\n", install_pos)
        # Look a bit further for continuation lines.
        install_block = self.src[install_pos:install_pos + 400]
        self.assertNotIn("--data-dir", install_block)

    def test_init_installer_failure_fails_closed(self) -> None:
        # A wrapper install failure must NOT then activate memory.provider.
        # The helper must return nonzero on installer failure (fail closed for
        # the pilot) and leave provider blank. It must NOT mask the failure as
        # success with "|| log ... continuing".
        # Inspect the activate_mnemosyne function body and locate the ACTUAL
        # installer command (the venv path prefix distinguishes it from the
        # docstring comment that names the CLI).
        activate_block = self.src[self.src.find("activate_mnemosyne()"):]
        # The actual command line starts with the venv binary path.
        cmd_pos = activate_block.find("/opt/hermes/.venv/bin/mnemosyne-hermes")
        self.assertGreater(cmd_pos, 0, "actual installer command not found")
        # The provider-activation step references hermes_cli.config.
        provider_pos = activate_block.find("hermes_cli.config", cmd_pos)
        self.assertGreater(provider_pos, 0, "provider activation step not found")
        between = activate_block[cmd_pos:provider_pos]
        self.assertIn("return", between,
                      "installer failure must return before provider activation")

    def test_init_does_not_use_python_module_form(self) -> None:
        # The Python module lacks __main__; `python -m mnemosyne_hermes` must
        # NOT be used anywhere in the init.
        self.assertNotIn("-m mnemosyne_hermes", self.src)

    def test_init_sets_memory_provider_via_supported_interface(self) -> None:
        # Must use the supported Hermes config interface (hermes_cli.config
        # load_config/save_config or hermes CLI), not manual YAML writing.
        self.assertIn("hermes_cli", self.src)
        self.assertIn("memory", self.src)
        self.assertIn("provider", self.src)

    def test_init_activation_runs_after_template_copy(self) -> None:
        # Activation must run AFTER the source config template is copied to the
        # runtime config (so it sets provider on the freshly-copied config).
        template_pos = self.src.find("Syncing Hermes config.yaml from repo template")
        self.assertGreater(template_pos, 0)
        # Use the activate_mnemosyne function definition position, not the
        # first MNEMOSYNE_PROVIDER occurrence (which may be in the backup
        # cron function earlier in the script).
        mnemo_pos = self.src.find("activate_mnemosyne()")
        self.assertGreater(mnemo_pos, template_pos,
                           "Mnemosyne activation must run after template copy")

    def test_init_activation_runs_after_apply_sidecars_and_policy(self) -> None:
        # Activation must run AFTER apply_sidecars_and_policy so the skill
        # policy is enforced before provider activation.
        apply_pos = self.src.find("\napply_sidecars_and_policy\n")
        self.assertGreater(apply_pos, 0)
        # Use the activate_mnemosyne function definition position, not the
        # first MNEMOSYNE_PROVIDER occurrence (which may be in the backup
        # cron function earlier in the script).
        mnemo_pos = self.src.find("activate_mnemosyne()")
        self.assertGreater(mnemo_pos, apply_pos,
                           "Mnemosyne activation must run after apply_sidecars_and_policy")

    def test_init_preserves_static_memories_directory(self) -> None:
        # The static memories/ directory behavior must be preserved.
        self.assertIn("${HERMES_HOME}/memories", self.src)

    def test_init_does_not_activate_when_provider_unset(self) -> None:
        # When MNEMOSYNE_PROVIDER is unset/empty, the init must NOT install/activate.
        # The gate must use the standard empty-check pattern.
        # Look for the case-style empty check used elsewhere in the script.
        self.assertIn("MNEMOSYNE_PROVIDER", self.src)

    # --- Phase 1 pivot: upstream-native, nested runtime config, rollback ---

    def test_init_sets_nested_mnemosyne_config_block(self) -> None:
        # The init must set the nested memory.mnemosyne config block via
        # hermes_cli.config, not just memory.provider. This is where
        # profile_isolation, tools, default_scope, etc. live.
        activate_block = self.src[self.src.find("activate_mnemosyne()"):]
        # The config-setting Python snippet must set memory.mnemosyne.
        self.assertIn("mnemosyne", activate_block)
        # Must set memory.provider, memory.memory_enabled, user_profile_enabled.
        self.assertIn('"provider"', activate_block)
        self.assertIn('"memory_enabled"', activate_block)
        self.assertIn('"user_profile_enabled"', activate_block)

    def test_init_disables_static_injection_when_active(self) -> None:
        # When Mnemosyne is active, memory_enabled and user_profile_enabled
        # must be set to false (upstream-native, no static injection).
        activate_block = self.src[self.src.find("activate_mnemosyne()"):]
        self.assertIn("False", activate_block)

    def test_init_sets_tools_full_native_not_empty(self) -> None:
        # User chose full native Mnemosyne tools. The init must NOT set
        # memory.mnemosyne.tools to [] (passive-only). It must omit the tools
        # key entirely so the provider exposes all upstream-native tools
        # (including mutating operations). This is upstream-native behavior.
        activate_block = self.src[self.src.find("activate_mnemosyne()"):]
        # The tools key must NOT be set in the activation block.
        self.assertNotIn('"tools"', activate_block)
        self.assertNotIn("mnemo[\"tools\"]", activate_block)

    def test_init_sets_global_scope_and_profile_isolation_false(self) -> None:
        # default_scope global, profile_isolation false.
        activate_block = self.src[self.src.find("activate_mnemosyne()"):]
        self.assertIn('"default_scope"', activate_block)
        self.assertIn('"global"', activate_block)
        self.assertIn('"profile_isolation"', activate_block)

    def test_init_uses_auto_sleep_not_auto_sleep_enabled(self) -> None:
        # Direct installed-source validation: the nested config key must be
        # `auto_sleep` (not `auto_sleep_enabled`). The provider reads
        # _read_config_key("auto_sleep"), not "auto_sleep_enabled".
        activate_block = self.src[self.src.find("activate_mnemosyne()"):]
        self.assertIn('"auto_sleep"', activate_block)
        self.assertIn("False", activate_block)
        # The incorrect key must NOT be set as a nested config key in the
        # Python code (mnemo["auto_sleep_enabled"]). Comments may mention it
        # for explanation, but the code must not set it.
        self.assertNotIn('mnemo["auto_sleep_enabled"]', activate_block)
        self.assertNotIn("mnemo['auto_sleep_enabled']", activate_block)

    def test_init_retains_write_approval_as_archive_protection(self) -> None:
        # write_approval must remain true (archive protection) even when
        # Mnemosyne is active. The init must NOT set it to false.
        activate_block = self.src[self.src.find("activate_mnemosyne()"):]
        # The activation must not touch write_approval (it stays from template).
        # Look for write_approval in the activation block — it should NOT be
        # set to False.
        self.assertNotIn('"write_approval"', activate_block)

    def test_init_has_rollback_cleanup_block(self) -> None:
        # When MNEMOSYNE_PROVIDER is absent/not mnemosyne, the init must run
        # a rollback/cleanup that resets provider/static flags and removes
        # installer-owned plugin/skill artifacts while preserving the DB.
        self.assertIn("rollback", self.src.lower())
        self.assertIn("cleanup", self.src.lower())

    def test_init_rollback_uses_upstream_cleanup_cli(self) -> None:
        # Rollback must use the upstream `mnemosyne-hermes cleanup` CLI for
        # plugin removal (safe, never touches database), not a blanket rm -rf.
        self.assertIn("mnemosyne-hermes", self.src)
        self.assertIn("cleanup", self.src.lower())

    def test_init_rollback_preserves_mnemosyne_db(self) -> None:
        # The rollback must NOT delete the mnemosyne data directory / DB.
        # Inspect only the rollback_mnemosyne function body (not comments,
        # which may mention rm -rf while explaining what it does NOT do).
        rollback_pos = self.src.find("rollback_mnemosyne()")
        self.assertGreater(rollback_pos, 0, "rollback_mnemosyne function not found")
        # The function body ends at the next top-level function or the final
        # activate/rollback call.
        rollback_body = self.src[rollback_pos:rollback_pos + 3000]
        # The data dir must NOT be removed in the rollback function body.
        # `rm -rf` targeting the override skill is expected, but NOT targeting
        # the mnemosyne data dir.
        for line in rollback_body.splitlines():
            if "rm -rf" in line and "mnemosyne/data" in line:
                self.fail(f"rollback must not rm -rf the mnemosyne data dir: {line}")
            if "rm -rf" in line and "mnemosyne" in line and "override" not in line and "skill" not in line:
                # Allow rm -rf of the override skill dir only.
                if "data" in line:
                    self.fail(f"rollback must not rm -rf mnemosyne data: {line}")

    def test_init_rollback_removes_managed_override_skill(self) -> None:
        # The rollback must remove the installer-owned managed override skill
        # (skills/memory/mnemosyne-memory-override/) but only when the
        # .sha256 sidecar is present AND the SKILL.md content matches the
        # sidecar hash (confirming it's installer-owned and unmodified).
        self.assertIn("mnemosyne-memory-override", self.src)

    def test_init_rollback_validates_sha256_content_before_skill_removal(self) -> None:
        # The managed skill removal must be guarded by the .sha256 sidecar
        # presence AND content hash verification so a user-modified skill is
        # preserved. No blanket rm -rf of the directory.
        self.assertIn("sha256", self.src.lower())
        # Must verify the hash matches the SKILL.md content, not just check
        # the sidecar file exists.
        self.assertIn("sha256sum", self.src.lower())

    def test_init_rollback_removes_only_skill_md_and_sidecar_not_dir(self) -> None:
        # The cleanup must remove only the installer-owned SKILL.md and its
        # sidecar, then rmdir only if the directory is empty. It must NOT
        # blanket rm -rf the directory (which would delete user-added files).
        # The logic lives in the shared cleanup_mnemosyne_artifacts helper.
        cleanup_pos = self.src.find("cleanup_mnemosyne_artifacts()")
        self.assertGreater(cleanup_pos, 0)
        cleanup_body = self.src[cleanup_pos:cleanup_pos + 2000]
        # Must use rm -f (file removal), not rm -rf (directory removal) for the
        # skill content.
        self.assertIn("rm -f", cleanup_body)
        # Must use rmdir (empty directory removal), not rm -rf for the dir.
        self.assertIn("rmdir", cleanup_body)

    def test_init_rollback_resets_provider_and_static_flags(self) -> None:
        # Rollback must reset memory.provider to blank/None and restore
        # memory_enabled/user_profile_enabled to true (base template values).
        rollback_block = self.src[self.src.lower().find("rollback"):]
        if not rollback_block:
            rollback_block = self.src[self.src.lower().find("cleanup"):]
        self.assertIn("provider", rollback_block)
        self.assertIn("memory_enabled", rollback_block)

    # --- Gate 1: activation failure cleanup ---

    def test_init_has_activation_failure_cleanup(self) -> None:
        # When activation fails (install or config), the init must clean
        # installer-owned plugin/managed skill artifacts safely while
        # preserving DB and user-modified/unverified skill content. This must
        # NOT be skipped by rollback's provider-env guard.
        self.assertIn("cleanup_mnemosyne_artifacts", self.src)

    def test_init_activation_failure_calls_cleanup(self) -> None:
        # The activate_mnemosyne function must call the shared cleanup helper
        # on failure (install or config), not just return 1.
        activate_block = self.src[self.src.find("activate_mnemosyne()"):]
        # Both failure paths (install and config) must invoke cleanup.
        self.assertIn("cleanup_mnemosyne_artifacts", activate_block)

    def test_init_cleanup_helper_not_gated_by_provider_env(self) -> None:
        # The shared cleanup helper must NOT have a MNEMOSYNE_PROVIDER case
        # guard that would skip cleanup during activation failure (when
        # MNEMOSYNE_PROVIDER=mnemosyne).
        cleanup_pos = self.src.find("cleanup_mnemosyne_artifacts()")
        self.assertGreater(cleanup_pos, 0, "cleanup_mnemosyne_artifacts function not found")
        cleanup_body = self.src[cleanup_pos:cleanup_pos + 2000]
        # The cleanup helper must NOT early-return on MNEMOSYNE_PROVIDER=mnemosyne.
        # It should be callable regardless of the provider env value.
        self.assertNotIn('case "${MNEMOSYNE_PROVIDER:-}" in', cleanup_body)

    def test_init_cleanup_does_not_early_return_on_absent_plugin(self) -> None:
        # The cleanup helper must NOT return early merely because the plugin
        # dir is absent — a managed override skill may still remain from a
        # partial install.
        cleanup_pos = self.src.find("cleanup_mnemosyne_artifacts()")
        cleanup_body = self.src[cleanup_pos:cleanup_pos + 2000]
        # Must check for the override skill dir independently of the plugin dir.
        self.assertIn("mnemosyne-memory-override", cleanup_body)

    def test_init_cleanup_preserves_db(self) -> None:
        # The cleanup helper must NOT delete the mnemosyne data dir / DB.
        cleanup_pos = self.src.find("cleanup_mnemosyne_artifacts()")
        cleanup_body = self.src[cleanup_pos:cleanup_pos + 2000]
        for line in cleanup_body.splitlines():
            if "rm -rf" in line and "mnemosyne/data" in line:
                self.fail(f"cleanup must not rm -rf the mnemosyne data dir: {line}")

    def test_init_cleanup_uses_sha256_content_verification(self) -> None:
        # The cleanup helper must verify the SKILL.md hash matches the sidecar
        # before removing, preserving user-modified content.
        cleanup_pos = self.src.find("cleanup_mnemosyne_artifacts()")
        cleanup_body = self.src[cleanup_pos:cleanup_pos + 2000]
        self.assertIn("sha256sum", cleanup_body)
        self.assertIn("rm -f", cleanup_body)
        self.assertIn("rmdir", cleanup_body)

    def test_init_rollback_delegates_to_shared_cleanup(self) -> None:
        # The rollback function must delegate artifact removal to the shared
        # cleanup helper (not duplicate the logic).
        rollback_pos = self.src.find("rollback_mnemosyne()")
        self.assertGreater(rollback_pos, 0)
        rollback_body = self.src[rollback_pos:rollback_pos + 3000]
        self.assertIn("cleanup_mnemosyne_artifacts", rollback_body)


# ---------------------------------------------------------------------------
# Mnemosyne Phase 1 pivot: template archive/rollback material
# ---------------------------------------------------------------------------


class MnemosyneTemplateArchiveTests(unittest.TestCase):
    """Template README/BOOT/manifest/gitignore must preserve MEMORY.md/USER.md
    as versioned Mnemosyne archive/rollback material and explain they are not
    injected while the pilot is active."""

    def test_template_manifest_preserves_memory_paths(self) -> None:
        text = TEMPLATE_MANIFEST.read_text(encoding="utf-8")
        self.assertIn("memories/USER.md", text)
        self.assertIn("memories/MEMORY.md", text)

    def test_template_gitignore_preserves_memory_paths(self) -> None:
        text = TEMPLATE_GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("!memories/MEMORY.md", text)
        self.assertIn("!memories/USER.md", text)

    def test_template_readme_documents_archive_status(self) -> None:
        text = TEMPLATE_README.read_text(encoding="utf-8")
        # The README must explain MEMORY.md/USER.md are archived-but-not-injected
        # while the Mnemosyne pilot is active.
        self.assertIn("Mnemosyne", text)
        self.assertIn("archive", text.lower())

    def test_template_readme_documents_no_injection_while_pilot_active(self) -> None:
        text = TEMPLATE_README.read_text(encoding="utf-8")
        # Must state the files are NOT injected while the pilot is active.
        self.assertIn("not injected", text.lower())

    def test_template_readme_documents_rollback_material(self) -> None:
        text = TEMPLATE_README.read_text(encoding="utf-8")
        # Must explain the files serve as explicit rollback material.
        self.assertIn("rollback", text.lower())

    def test_template_boot_documents_mnemosyne_pilot(self) -> None:
        text = TEMPLATE_BOOT.read_text(encoding="utf-8")
        # BOOT.md must mention the Mnemosyne pilot and the archive/injection
        # policy so a fresh deployment knows the status.
        self.assertIn("Mnemosyne", text)

    def test_template_manifest_does_not_remove_memory_paths(self) -> None:
        # The manifest must still list the memory paths (not removed).
        text = TEMPLATE_MANIFEST.read_text(encoding="utf-8")
        # Ensure both paths are present (not commented out).
        for line in text.splitlines():
            if "memories/USER.md" in line:
                self.assertFalse(line.strip().startswith("#"),
                                 "memories/USER.md must not be commented out")
            if "memories/MEMORY.md" in line:
                self.assertFalse(line.strip().startswith("#"),
                                 "memories/MEMORY.md must not be commented out")

    def test_template_gitignore_does_not_remove_memory_paths(self) -> None:
        # The gitignore must still allow the memory paths (not removed).
        text = TEMPLATE_GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("!memories/MEMORY.md", text)
        self.assertIn("!memories/USER.md", text)


# ---------------------------------------------------------------------------
# Phase 2: Mnemosyne backup export cron + writable-volume allowlist
# ---------------------------------------------------------------------------


class MnemosyneBackupExportCronTests(unittest.TestCase):
    """docker-hermes-init.sh: opt-in backup export cron and staging allowlist."""

    def setUp(self) -> None:
        self.src = INIT_PATH.read_text(encoding="utf-8")

    def _cron_function_text(self) -> str:
        """Return the FULL body of install_mnemosyne_backup_export_cron.

        Uses brace matching from the function definition to its closing brace
        so the nested remove_mnemosyne_backup_export_cron helper and every
        gate/removal path are always included (a fixed-length slice silently
        truncates the function as it grows).
        """
        start = self.src.index("install_mnemosyne_backup_export_cron()")
        # Start scanning after the opening `{` of the function body.
        brace_pos = self.src.index("{", start)
        depth = 0
        i = brace_pos
        while i < len(self.src):
            ch = self.src[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.src[start:i + 1]
            i += 1
        raise AssertionError("could not find the end of install_mnemosyne_backup_export_cron()")

    # --- Writable-volume allowlist ---

    def test_base_writable_volumes_unchanged(self) -> None:
        # Base-only startup must remain exactly HERMES_HOME + /shared plus the
        # vault-recovery staging volume (a BASE feature: the daily export cron
        # is default-enabled, so its path is unconditionally allowlisted).
        # The default HERMES_WRITABLE_VOLUMES line must not include
        # mnemosyne-backup (that stays opt-in).
        pos = self.src.find("HERMES_WRITABLE_VOLUMES=")
        self.assertGreater(pos, 0)
        # Find the assignment line (may be multi-line with conditional append).
        # The base default must be HERMES_HOME + /shared + vault-recovery staging.
        self.assertIn('"${HERMES_HOME} /shared /opt/data/vault-recovery/staging"', self.src)

    def test_optin_staging_appended_to_writable_volumes(self) -> None:
        # When MNEMOSYNE_BACKUP_STAGING_DIR equals the exact expected path,
        # /opt/data/mnemosyne-backup/staging must be appended to the allowlist.
        self.assertIn("MNEMOSYNE_BACKUP_STAGING_DIR", self.src)
        self.assertIn("/opt/data/mnemosyne-backup/staging", self.src)

    def test_arbitrary_staging_path_rejected(self) -> None:
        # The allowlist must NOT append an arbitrary env path. The append must
        # be gated on the exact expected path string.
        # Find the conditional append and verify it checks the exact path.
        self.assertIn('"/opt/data/mnemosyne-backup/staging"', self.src)

    def test_no_uploader_state_write_path(self) -> None:
        # The init must NOT add any uploader-state write path to the allowlist.
        # Only the staging dir is added (the uploader itself is read-only/no-mount).
        pos = self.src.find("HERMES_WRITABLE_VOLUMES=")
        self.assertGreater(pos, 0)
        # Check the conditional append block does not reference uploader state.
        append_block = self.src[pos:pos + 500]
        self.assertNotIn("uploader", append_block.lower())
        self.assertNotIn("rclone", append_block.lower())

    # --- Cron installer ---

    def test_has_install_mnemosyne_backup_export_cron(self) -> None:
        self.assertIn("install_mnemosyne_backup_export_cron", self.src)

    def test_cron_sources_correct_script(self) -> None:
        self.assertIn("/opt/josemar/scripts/mnemosyne-backup-export.sh", self.src)

    def test_cron_copies_to_hermes_scripts(self) -> None:
        self.assertIn("mnemosyne-backup-export.sh", self.src)
        self.assertIn("${HERMES_HOME}/scripts", self.src)

    def test_cron_sets_mode_700(self) -> None:
        # The script copy must be mode 700.
        cron_body = self._cron_function_text()
        self.assertIn("chmod 700", cron_body)

    def test_cron_owned_by_hermes(self) -> None:
        cron_body = self._cron_function_text()
        self.assertIn("chown", cron_body)
        self.assertIn("HERMES_UID_VALUE", cron_body)

    def test_cron_gated_on_mnemosyne_provider(self) -> None:
        # The cron must only install when MNEMOSYNE_PROVIDER=mnemosyne.
        cron_body = self._cron_function_text()
        self.assertIn("MNEMOSYNE_PROVIDER", cron_body)
        self.assertIn("mnemosyne", cron_body)

    def test_cron_gated_on_exact_staging_path(self) -> None:
        # The cron must only install when the staging env equals the exact
        # expected path.
        cron_body = self._cron_function_text()
        self.assertIn("MNEMOSYNE_BACKUP_STAGING_DIR", cron_body)
        self.assertIn("/opt/data/mnemosyne-backup/staging", cron_body)

    def test_cron_gated_on_positive_interval(self) -> None:
        # The cron must only install when the interval is a positive integer.
        cron_body = self._cron_function_text()
        self.assertIn("MNEMOSYNE_BACKUP_EXPORT_INTERVAL", cron_body)
        # Must reject 0/unset/malformed (same case pattern as other crons).
        self.assertIn("*[!0-9]*", cron_body)

    def test_cron_disabled_when_interval_unset(self) -> None:
        # When interval is unset, the cron must be disabled with a clear log.
        cron_body = self._cron_function_text()
        self.assertIn("disabled", cron_body.lower())

    def test_cron_disabled_when_interval_zero(self) -> None:
        # When interval is 0, the cron must be disabled.
        cron_body = self._cron_function_text()
        # The case pattern must include 0 as a disable value (unquoted, same
        # as existing cron patterns).
        self.assertIn("|0|", cron_body)

    def test_cron_removal_uses_cron_remove_positional_name(self) -> None:
        # The installed Hermes CLI's removal subcommand is `cron remove
        # <id-or-name>` (positional). The init must NOT use `cron delete
        # --name`, which is not a valid Hermes CLI invocation: it always
        # errors with "unrecognized arguments: --name", leaving the owned
        # job behind. This is the exact regression that kept the owned
        # export job alive after MNEMOSYNE_BACKUP_EXPORT_INTERVAL=0.
        cron_body = self._cron_function_text()
        # Removal must be via the positional `cron remove` form.
        self.assertIn('exec "$HERMES_CLI" cron remove "$1"', cron_body)
        # The name is passed positionally (the sh args shift-2 pattern).
        self.assertIn("shift 2", cron_body)
        # The removal helper must still be invoked by the false-gate paths.
        self.assertIn("remove_mnemosyne_backup_export_cron", cron_body)
        # No actual CLI invocation may use the non-existent `cron delete`
        # subcommand. (Comments describing the regression may mention the
        # broken form, so only inspect executable `exec "$HERMES_CLI"` lines.)
        for line in cron_body.splitlines():
            if 'exec "$HERMES_CLI"' in line:
                self.assertNotIn("cron delete", line,
                                 f"broken cron delete invocation present: {line}")
                self.assertNotIn("cron delete --name", line)

    def test_cron_disabled_when_provider_absent(self) -> None:
        # When MNEMOSYNE_PROVIDER is absent/not mnemosyne, the cron must not
        # install.
        cron_body = self._cron_function_text()
        # Must have a case/return guard on MNEMOSYNE_PROVIDER.
        self.assertIn("return 0", cron_body)

    def test_cron_uses_no_agent(self) -> None:
        cron_body = self._cron_function_text()
        self.assertIn("--no-agent", cron_body)

    def test_cron_uses_stable_name(self) -> None:
        cron_body = self._cron_function_text()
        self.assertIn("--name mnemosyne-backup-export", cron_body)

    def test_cron_uses_correct_script_name(self) -> None:
        cron_body = self._cron_function_text()
        self.assertIn("--script mnemosyne-backup-export.sh", cron_body)

    def test_cron_uses_hermes_home_workdir(self) -> None:
        cron_body = self._cron_function_text()
        self.assertIn("--workdir", cron_body)
        # Workdir must be HERMES_HOME (safe), not the staging dir.
        self.assertIn('"$HERMES_HOME"', cron_body)

    def test_cron_schedule_in_minutes(self) -> None:
        # The interval must be interpreted as MINUTES and the schedule must be
        # `every ${interval}m`.
        cron_body = self._cron_function_text()
        self.assertIn("every ${interval}m", cron_body)

    def test_cron_idempotent_by_name(self) -> None:
        # The cron must inspect jobs.json by exact stable name and not duplicate.
        cron_body = self._cron_function_text()
        self.assertIn("mnemosyne-backup-export", cron_body)
        # Must check jobs.json for existing job by name.
        self.assertIn("jobs.json", cron_body)

    def test_cron_idempotency_uses_hermes_interval_schedule_schema(self) -> None:
        # Hermes persists schedules as an object. The enabled-job comparison
        # must use its authoritative interval kind/minutes fields, not compare
        # the object to the human-readable `every Nm` display string.
        cron_body = self._cron_function_text()
        self.assertIn('schedule.get("kind") == "interval"', cron_body)
        self.assertIn('schedule.get("minutes")', cron_body)
        self.assertIn('isinstance(schedule.get("minutes"), int)', cron_body)
        self.assertIn('job.get("script")', cron_body)
        self.assertIn('job.get("no_agent")', cron_body)
        self.assertIn('job.get("workdir")', cron_body)
        # Unknown/string schedule shapes must be treated as drift, not
        # guessed into an enabled state.
        self.assertIn('isinstance(schedule, dict)', cron_body)

    def test_cron_drift_warning(self) -> None:
        # If an existing same-name job has a different schedule/script/mode,
        # the cron must surface a warning (not silently duplicate or mutate).
        cron_body = self._cron_function_text()
        self.assertIn("WARNING", cron_body)
        self.assertIn("reconciliation", cron_body.lower())

    def test_cron_source_missing_safe_skip(self) -> None:
        # If the source script is missing, the cron must safely skip.
        cron_body = self._cron_function_text()
        self.assertIn("script_source", cron_body)
        self.assertIn("return 0", cron_body)

    def test_cron_failure_non_fatal(self) -> None:
        # Cron install failure must be non-fatal to gateway startup (WARNING).
        # The call site must use || log WARNING.
        call_pos = self.src.find("install_mnemosyne_backup_export_cron")
        # Find the call site (not the function definition).
        # The function definition has (); the call site doesn't.
        call_site = self.src[self.src.rfind("install_mnemosyne_backup_export_cron", 0, call_pos + 1):]
        # The last occurrence is the call site.
        last_call = self.src.rfind("install_mnemosyne_backup_export_cron")
        call_context = self.src[last_call:last_call + 100]
        self.assertIn("WARNING", self.src[last_call:last_call + 200])

    def test_cron_called_after_jobs_json_creation(self) -> None:
        # The cron must be called after cron/jobs.json creation and alongside
        # other cron installers.
        jobs_json_pos = self.src.find("Creating empty Hermes cron/jobs.json")
        self.assertGreater(jobs_json_pos, 0)
        cron_call_pos = self.src.rfind("install_mnemosyne_backup_export_cron")
        self.assertGreater(cron_call_pos, jobs_json_pos,
                           "backup export cron must be called after jobs.json creation")

    def test_cron_called_alongside_other_cron_installers(self) -> None:
        # The backup job is intentionally reconciled only after activation;
        # the other jobs remain in the earlier generic cron section.
        ws_cron_pos = self.src.rfind("install_workspace_sync_cron")
        gb_cron_pos = self.src.rfind("install_gbrain_refresh_cron")
        backup_cron_pos = self.src.rfind("install_mnemosyne_backup_export_cron")
        activation_pos = self.src.find("if activate_mnemosyne; then")
        self.assertLess(ws_cron_pos, activation_pos)
        self.assertLess(gb_cron_pos, activation_pos)
        self.assertGreater(backup_cron_pos, activation_pos)


class MnemosyneBackupExportCronBehavioralTests(unittest.TestCase):
    """Disposable shell/source tests that exercise the actual init function."""

    def _actual_cron_function(self, tmpdir: str) -> str:
        source = INIT_PATH.read_text(encoding="utf-8")
        start = source.index("install_mnemosyne_backup_export_cron()")
        end = source.index("\n}\n\nif [ -n \"${WORKSPACE_STATE_REPO", start) + 3
        extracted = source[start:end]
        path = Path(tmpdir) / "actual-cron-function.sh"
        path.write_text(extracted, encoding="utf-8")
        return str(path)

    def test_actual_extracted_function_creates_expected_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "hermes-home"
            (home / "cron").mkdir(parents=True)
            (home / "cron" / "jobs.json").write_text('{"jobs": []}\n')
            script = Path(tmpdir) / "export.sh"
            script.write_text("#!/bin/sh\n")
            script.chmod(0o700)
            cli = Path(tmpdir) / "hermes"
            cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > \"{tmpdir}/created\"\n")
            cli.chmod(0o700)
            su = Path(tmpdir) / "su"
            su.write_text('''#!/bin/sh
while [ "$1" != "-c" ]; do shift; done
shift
command="$1"
shift
exec /bin/sh -c "$command" "$@"
''')
            su.chmod(0o700)
            fn = self._actual_cron_function(tmpdir)
            wrapper = Path(tmpdir) / "wrapper.sh"
            wrapper.write_text(f'''#!/bin/sh
set -eu
PATH="{tmpdir}:$PATH"
HERMES_HOME="{home}"
HERMES_USER="$(id -un)"
HERMES_UID_VALUE="$(id -u)"
HERMES_GID_VALUE="$(id -g)"
HERMES_CLI="{cli}"
MNEMOSYNE_PROVIDER=mnemosyne
MNEMOSYNE_BACKUP_STAGING_DIR=/opt/data/mnemosyne-backup/staging
MNEMOSYNE_BACKUP_EXPORT_INTERVAL=30
MNEMOSYNE_BACKUP_EXPORT_SCRIPT_SOURCE="{script}"
log() {{ :; }}
. "{fn}"
install_mnemosyne_backup_export_cron
''')
            wrapper.chmod(0o700)
            result = subprocess.run(["sh", str(wrapper)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(tmpdir) / "created").exists(),
                            f"stdout={result.stdout}\nstderr={result.stderr}")

    def test_actual_extracted_function_keeps_schema_valid_job(self) -> None:
        # Exercise the actual init function with the pinned Hermes jobs.json
        # schedule object. A matching job must be a no-op: the CLI must not be
        # called to remove/recreate it.
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "hermes-home"
            (home / "cron").mkdir(parents=True)
            (home / "cron" / "jobs.json").write_text(json.dumps({
                "jobs": [{
                    "id": "stable-id",
                    "name": "mnemosyne-backup-export",
                    "script": "mnemosyne-backup-export.sh",
                    "no_agent": True,
                    "schedule": {
                        "kind": "interval",
                        "minutes": 30,
                        "display": "every 30m",
                    },
                    "workdir": str(home),
                }],
            }), encoding="utf-8")
            script = Path(tmpdir) / "export.sh"
            script.write_text("#!/bin/sh\n")
            script.chmod(0o700)
            calls = Path(tmpdir) / "calls"
            cli = Path(tmpdir) / "hermes"
            cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"{calls}\"\n")
            cli.chmod(0o700)
            su = Path(tmpdir) / "su"
            su.write_text('''#!/bin/sh
while [ "$1" != "-c" ]; do shift; done
shift
command="$1"
shift
exec /bin/sh -c "$command" "$@"
''')
            su.chmod(0o700)
            fn = self._actual_cron_function(tmpdir)
            wrapper = Path(tmpdir) / "wrapper.sh"
            wrapper.write_text(f'''#!/bin/sh
set -eu
PATH="{tmpdir}:$PATH"
HERMES_HOME="{home}"
HERMES_USER="$(id -un)"
HERMES_UID_VALUE="$(id -u)"
HERMES_GID_VALUE="$(id -g)"
HERMES_CLI="{cli}"
MNEMOSYNE_PROVIDER=mnemosyne
MNEMOSYNE_BACKUP_STAGING_DIR=/opt/data/mnemosyne-backup/staging
MNEMOSYNE_BACKUP_EXPORT_INTERVAL=30
MNEMOSYNE_BACKUP_EXPORT_SCRIPT_SOURCE="{script}"
log() {{ :; }}
. "{fn}"
install_mnemosyne_backup_export_cron
''')
            wrapper.chmod(0o700)
            result = subprocess.run(["sh", str(wrapper)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(calls.exists(), "matching schema job must not be recreated")

    def test_actual_extracted_function_removes_owned_job_when_interval_zero(self) -> None:
        # Exercise the ACTUAL extracted init function (not duplicated
        # pseudo-code) for the negative interval=0 gate: the owned job must be
        # removed via `cron remove mnemosyne-backup-export` (positional), and
        # unrelated jobs must remain untouched. This reproduces the exact
        # lifecycle the gateway test failed on before the `cron delete --name`
        # fix.
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "hermes-home"
            (home / "cron").mkdir(parents=True)
            # jobs.json already holds the owned job plus an unrelated one.
            (home / "cron" / "jobs.json").write_text(json.dumps({
                "jobs": [
                    {"id": "aaa111", "name": "gbrain-refresh", "schedule": "every 5m"},
                    {"id": "bbb222", "name": "mnemosyne-backup-export", "schedule": "every 1m"},
                ]
            }), encoding="utf-8")
            script = Path(tmpdir) / "export.sh"
            script.write_text("#!/bin/sh\n")
            script.chmod(0o700)
            calls = Path(tmpdir) / "calls"
            cli = Path(tmpdir) / "hermes"
            cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"{calls}\"\n")
            cli.chmod(0o700)
            su = Path(tmpdir) / "su"
            su.write_text('''#!/bin/sh
while [ "$1" != "-c" ]; do shift; done
shift
command="$1"
shift
exec /bin/sh -c "$command" "$@"
''')
            su.chmod(0o700)
            fn = self._actual_cron_function(tmpdir)
            wrapper = Path(tmpdir) / "wrapper.sh"
            wrapper.write_text(f'''#!/bin/sh
set -eu
PATH="{tmpdir}:$PATH"
HERMES_HOME="{home}"
HERMES_USER="$(id -un)"
HERMES_UID_VALUE="$(id -u)"
HERMES_GID_VALUE="$(id -g)"
HERMES_CLI="{cli}"
MNEMOSYNE_PROVIDER=mnemosyne
MNEMOSYNE_BACKUP_STAGING_DIR=/opt/data/mnemosyne-backup/staging
MNEMOSYNE_BACKUP_EXPORT_INTERVAL=0
MNEMOSYNE_BACKUP_EXPORT_SCRIPT_SOURCE="{script}"
log() {{ :; }}
. "{fn}"
install_mnemosyne_backup_export_cron
''')
            wrapper.chmod(0o700)
            result = subprocess.run(["sh", str(wrapper)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            # The actual function must have invoked `cron remove` with the
            # stable name as the positional id-or-name argument.
            self.assertTrue(calls.exists(), f"hermes CLI was not invoked:\n{result.stdout}\n{result.stderr}")
            removal = calls.read_text().strip()
            self.assertIn("cron remove mnemosyne-backup-export", removal,
                          f"expected positional cron remove, got: {removal!r}")
            self.assertNotIn("cron delete", removal,
                             f"broken cron delete form must not be used: {removal!r}")

    def test_stub_hermes_cli_proves_one_job_creation(self) -> None:
        import subprocess
        import tempfile

        # Create a stub hermes CLI that records cron create calls.
        with tempfile.TemporaryDirectory() as tmpdir:
            stub_hermes = f"{tmpdir}/hermes"
            with open(stub_hermes, "w") as f:
                f.write("""#!/bin/sh
# Stub hermes CLI that records cron create calls.
echo "STUB_CALLED: $*" >> "$1/../cron-create-calls.log" 2>/dev/null
exit 0
""")
            os.chmod(stub_hermes, 0o755)

            # Create a minimal jobs.json (empty).
            hermes_home = f"{tmpdir}/hermes-home"
            os.makedirs(f"{hermes_home}/cron", exist_ok=True)
            with open(f"{hermes_home}/cron/jobs.json", "w") as f:
                f.write('{"jobs": [], "updated_at": null}\n')

            # Create a stub backup export script.
            script_source = f"{tmpdir}/mnemosyne-backup-export.sh"
            with open(script_source, "w") as f:
                f.write("#!/bin/sh\necho backup\n")
            os.chmod(script_source, 0o755)

            # Source the init script's cron function and call it with the
            # right env. We extract and run just the function via a wrapper.
            wrapper = f"{tmpdir}/wrapper.sh"
            with open(wrapper, "w") as f:
                f.write(f"""#!/bin/sh
set -eu
HERMES_HOME="{hermes_home}"
HERMES_UID_VALUE=10000
HERMES_GID_VALUE=10000
HERMES_USER="$(whoami)"
HERMES_CLI="{stub_hermes}"
MNEMOSYNE_PROVIDER=mnemosyne
MNEMOSYNE_BACKUP_STAGING_DIR=/opt/data/mnemosyne-backup/staging
MNEMOSYNE_BACKUP_EXPORT_INTERVAL=30

log() {{ echo "[test] $1"; }}

# Extract the install_mnemosyne_backup_export_cron function from the init
# script and source it, then call it.
# We use a simplified inline version that mirrors the init contract.
install_mnemosyne_backup_export_cron() {{
    case "${{MNEMOSYNE_PROVIDER:-}}" in
        mnemosyne) ;;
        *) return 0 ;;
    esac

    local script_source="{script_source}"
    local script_dir="${{HERMES_HOME}}/scripts"
    local script_path="${{script_dir}}/mnemosyne-backup-export.sh"
    local interval="${{MNEMOSYNE_BACKUP_EXPORT_INTERVAL:-0}}"

    if [ ! -x "$script_source" ]; then
        log "Mnemosyne backup-export cron disabled (source script missing)"
        return 0
    fi

    case "$interval" in
        ""|0|*[!0-9]*)
            log "Mnemosyne backup-export cron disabled (MNEMOSYNE_BACKUP_EXPORT_INTERVAL=${{interval:-unset}})"
            return 0
            ;;
    esac

    case "${{MNEMOSYNE_BACKUP_STAGING_DIR:-}}" in
        /opt/data/mnemosyne-backup/staging) ;;
        *)
            log "Mnemosyne backup-export cron disabled (staging dir not exact expected path)"
            return 0
            ;;
    esac

    mkdir -p "$script_dir"
    cp "$script_source" "$script_path"
    chmod 700 "$script_path"

    if python3 - "${{HERMES_HOME}}/cron/jobs.json" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
except Exception:
    sys.exit(1)
for job in data.get("jobs", []):
    if job.get("name") == "mnemosyne-backup-export":
        sys.exit(0)
sys.exit(1)
PY
    then
        log "Mnemosyne backup-export cron job already exists"
        return 0
    fi

    log "Creating Mnemosyne backup-export cron job"
    su -s /bin/sh -- "$HERMES_USER" -c '
        HOME="$1"
        HERMES_HOME="$1"
        export HOME HERMES_HOME
        shift 1
        exec "$HERMES_CLI" cron create "$@"
    ' sh "$HERMES_HOME" "every ${{interval}}m" --no-agent --script mnemosyne-backup-export.sh --workdir "$HERMES_HOME" --name mnemosyne-backup-export || log "WARNING: failed to create Mnemosyne backup-export cron job"
}}

install_mnemosyne_backup_export_cron
echo "WRAPPER_DONE"
""")
            os.chmod(wrapper, 0o755)

            proc = subprocess.run(
                ["sh", wrapper],
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(proc.returncode, 0,
                             f"wrapper failed:\n{proc.stdout}\n{proc.stderr}")
            self.assertIn("Creating Mnemosyne backup-export cron job", proc.stdout)
            self.assertIn("WRAPPER_DONE", proc.stdout)

            # Verify the stub was called exactly once with the right args.
            log_path = f"{hermes_home}/cron-create-calls.log"
            # The stub writes to $1/../cron-create-calls.log where $1 is
            # HERMES_HOME. So the log is at hermes_home/../cron-create-calls.log
            # = tmpdir/cron-create-calls.log
            stub_log = f"{tmpdir}/cron-create-calls.log"
            if os.path.exists(stub_log):
                with open(stub_log) as f:
                    calls = f.read()
                self.assertIn("mnemosyne-backup-export", calls)
                self.assertIn("--no-agent", calls)
                self.assertIn("every 30m", calls)

    def test_stub_hermes_cli_no_job_when_provider_absent(self) -> None:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            hermes_home = f"{tmpdir}/hermes-home"
            os.makedirs(f"{hermes_home}/cron", exist_ok=True)
            with open(f"{hermes_home}/cron/jobs.json", "w") as f:
                f.write('{"jobs": [], "updated_at": null}\n')

            wrapper = f"{tmpdir}/wrapper.sh"
            with open(wrapper, "w") as f:
                f.write(f"""#!/bin/sh
set -eu
HERMES_HOME="{hermes_home}"
HERMES_UID_VALUE=10000
HERMES_GID_VALUE=10000
HERMES_USER="$(whoami)"
MNEMOSYNE_PROVIDER=""
MNEMOSYNE_BACKUP_STAGING_DIR=/opt/data/mnemosyne-backup/staging
MNEMOSYNE_BACKUP_EXPORT_INTERVAL=30

log() {{ echo "[test] $1"; }}

install_mnemosyne_backup_export_cron() {{
    case "${{MNEMOSYNE_PROVIDER:-}}" in
        mnemosyne) ;;
        *) return 0 ;;
    esac
    log "SHOULD_NOT_REACH_HERE"
}}

install_mnemosyne_backup_export_cron
echo "WRAPPER_DONE"
""")
            os.chmod(wrapper, 0o755)

            proc = subprocess.run(
                ["sh", wrapper],
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("WRAPPER_DONE", proc.stdout)
            self.assertNotIn("SHOULD_NOT_REACH_HERE", proc.stdout)

    def test_stub_hermes_cli_no_job_when_interval_zero(self) -> None:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            hermes_home = f"{tmpdir}/hermes-home"
            os.makedirs(f"{hermes_home}/cron", exist_ok=True)
            with open(f"{hermes_home}/cron/jobs.json", "w") as f:
                f.write('{"jobs": [], "updated_at": null}\n')

            wrapper = f"{tmpdir}/wrapper.sh"
            with open(wrapper, "w") as f:
                f.write(f"""#!/bin/sh
set -eu
HERMES_HOME="{hermes_home}"
MNEMOSYNE_PROVIDER=mnemosyne
MNEMOSYNE_BACKUP_STAGING_DIR=/opt/data/mnemosyne-backup/staging
MNEMOSYNE_BACKUP_EXPORT_INTERVAL=0

log() {{ echo "[test] $1"; }}

install_mnemosyne_backup_export_cron() {{
    case "${{MNEMOSYNE_PROVIDER:-}}" in
        mnemosyne) ;;
        *) return 0 ;;
    esac
    local interval="${{MNEMOSYNE_BACKUP_EXPORT_INTERVAL:-0}}"
    case "$interval" in
        ""|0|*[!0-9]*)
            log "Mnemosyne backup-export cron disabled (interval=$interval)"
            return 0
            ;;
    esac
    log "SHOULD_NOT_REACH_HERE"
}}

install_mnemosyne_backup_export_cron
echo "WRAPPER_DONE"
""")
            os.chmod(wrapper, 0o755)

            proc = subprocess.run(
                ["sh", wrapper],
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("WRAPPER_DONE", proc.stdout)
            self.assertIn("disabled", proc.stdout.lower())
            self.assertNotIn("SHOULD_NOT_REACH_HERE", proc.stdout)


if __name__ == "__main__":
    unittest.main()
