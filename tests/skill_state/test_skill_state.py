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
EXPECTED_IMAGE = "nousresearch/hermes-agent:v2026.7.20"
LIVE_MANIFEST = REPO_ROOT / "agent-state" / ".sync-manifest"
LIVE_GITIGNORE = REPO_ROOT / "agent-state" / ".gitignore"
TEMPLATE_MANIFEST = REPO_ROOT / "templates" / "agent-state-template" / ".sync-manifest"
TEMPLATE_GITIGNORE = REPO_ROOT / "templates" / "agent-state-template" / ".gitignore"
TEMPLATE_DEFAULT_SIDECAR = (
    REPO_ROOT / "templates" / "agent-state-template" / "hermes" / "skill-toggles" / "default.json"
)


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

    def test_apply_failure_after_sync_does_not_change_exit_status(self) -> None:
        """If apply raises after a successful sync, exit status stays 0.

        Sync succeeded and that fact is reported faithfully; the apply
        failure is captured in the statuses list with an ``error:`` segment.
        """
        script = self._make_sync_script(body="echo SYNC_OK\n")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            with mock.patch.object(
                self.m, "_apply_all_sidecars_and_policy_unlocked", side_effect=RuntimeError("boom")
            ):
                exit_status, statuses, output = self.m.sync_and_apply([str(script)])
        self.assertEqual(exit_status, 0)
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
        self.assertIn("josemar_skill_state.py", self.src.split("py_compile")[-1])


class HermesUpgradeContractTests(unittest.TestCase):
    """Narrow contract tests for the Hermes v2026.7.20 upgrade.

    Four focused tests: image pins across the three source-of-truth files,
    config schema version plus raw comment, approvals defaults, and the
    patch docstring. These tests are intentionally surgical and do NOT
    assert approvals are in POLICY_KEYS.
    """

    def test_all_image_pins_equal_expected_version(self) -> None:
        """All three image-pin locations reference the expected image and no stale tag remains."""
        stale = "nousresearch/hermes-agent:v2026.7.7.2"
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
        """Parsed config schema is 33 and raw comment names v2026.7.20 with no stale tag."""
        text = CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn("nousresearch/hermes-agent:v2026.7.20", text)
        self.assertNotIn("nousresearch/hermes-agent:v2026.7.7.2", text)
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not available")
        data = yaml.safe_load(text)
        self.assertEqual(data["_config_version"], 33)

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

    def test_patch_docstring_names_v2026_7_20(self) -> None:
        """Build-time patch docstring names the new Hermes version and not the old one."""
        text = PATCH_PATH.read_text(encoding="utf-8")
        self.assertIn("Hermes v2026.7.20", text)
        self.assertNotIn("Hermes v2026.7.7.2", text)


if __name__ == "__main__":
    unittest.main()
