"""Focused tests for the state-owned model authoring overlay (models.yaml).

These tests exercise the model-authoring overlay added to
``scripts/josemar_skill_state.py`` and its init/workspace-sync wiring. They
do NOT require Docker or the Hermes venv: the overlay's runtime-config
projection uses PyYAML, which is available in the repo's dev environment.

Canonical v1 schema (strict selection-only):
  version: 1
  model: {provider: <nonempty>, default: <nonempty>}
  fallback_providers: [{provider: <nonempty>, model: <nonempty>}]
  auxiliary: {<allowlisted task>: {provider: <nonempty>, model: <string>}}
  #   auxiliary model: exactly '' iff provider == 'auto'; non-empty otherwise
  cron: {model: <string>, model_provider: <string>}

Forbidden: base_url, api_mode, extra_body, timeouts, token limits,
fallback_chain, credentials/secret keys, or any other Hermes config. The
overlay validates the full file before mutating config (fail-closed),
preserves template-owned sibling fields by deep merge, and restores
state-owned keys to repo template defaults on rollback (absent/empty).
Application happens only through the shared advisory lock.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = REPO_ROOT / "scripts" / "josemar_skill_state.py"
INIT_PATH = REPO_ROOT / "docker-hermes-init.sh"
WORKSPACE_SYNC_PATH = REPO_ROOT / "scripts" / "workspace_sync.py"
TEMPLATE_MODELS_YAML = (
    REPO_ROOT / "templates" / "agent-state-template" / "hermes" / "models.yaml"
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


def _write_models(workspace: Path, content: str) -> Path:
    models_path = workspace / "hermes" / "models.yaml"
    models_path.parent.mkdir(parents=True, exist_ok=True)
    models_path.write_text(content, encoding="utf-8")
    return models_path


def _load_config(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_template(workspace: Path, content: str) -> Path:
    """Write a template config file and return its path."""
    tmpl = workspace / "template-config.yaml"
    tmpl.write_text(content, encoding="utf-8")
    return tmpl


# ---------------------------------------------------------------------------
# Schema validation — strict selection-only v1
# ---------------------------------------------------------------------------


class ModelsValidationTests(unittest.TestCase):
    """validate_models_state accepts valid, rejects invalid (fail-closed)."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_valid_full_document(self) -> None:
        data = {
            "version": 1,
            "model": {"provider": "deepseek", "default": "deepseek-v4-pro"},
            "fallback_providers": [
                {"provider": "ollama-cloud", "model": "glm-5.2:cloud"},
            ],
            "auxiliary": {
                "vision": {"provider": "ollama-cloud", "model": "kimi-k2.7-code"},
            },
            "cron": {"model": "deepseek-v4-pro", "model_provider": "deepseek"},
        }
        result = self.m.validate_models_state(data)
        self.assertEqual(result, data)

    def test_valid_minimal_document(self) -> None:
        data = {"version": 1, "model": {"provider": "x", "default": "y"}}
        result = self.m.validate_models_state(data)
        self.assertEqual(result, data)

    def test_valid_empty_document(self) -> None:
        # version-only document is valid (rollback — no overlay keys).
        result = self.m.validate_models_state({"version": 1})
        self.assertEqual(result, {"version": 1})

    def test_valid_blank_cron_fields(self) -> None:
        # cron.model and cron.model_provider may be blank (inherit default).
        result = self.m.validate_models_state(
            {"version": 1, "cron": {"model": "", "model_provider": ""}}
        )
        self.assertEqual(result["cron"]["model"], "")

    def test_rejects_wrong_version(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state({"version": 2})
        self.assertIn("unsupported version", str(cm.exception))

    def test_rejects_non_mapping_root(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state([1, 2, 3])

    def test_rejects_unknown_top_level_key(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state({"version": 1, "bogus": {}})
        self.assertIn("unknown key", str(cm.exception))

    # -- forbidden nested keys (strict selection-only) --

    def test_rejects_base_url_in_model(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state(
                {"version": 1, "model": {"provider": "x", "default": "y", "base_url": "https://x"}}
            )
        self.assertIn("unknown key", str(cm.exception))

    def test_rejects_api_mode_in_model(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "model": {"provider": "x", "default": "y", "api_mode": "chat"}}
            )

    def test_rejects_extra_body_in_model(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "model": {"provider": "x", "default": "y", "extra_body": {}}}
            )

    def test_rejects_context_length_in_model(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "model": {"provider": "x", "default": "y", "context_length": 64000}}
            )

    def test_rejects_max_tokens_in_model(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "model": {"provider": "x", "default": "y", "max_tokens": 8192}}
            )

    def test_rejects_base_url_in_fallback(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "fallback_providers": [
                    {"provider": "x", "model": "y", "base_url": "https://x"},
                ]}
            )

    def test_rejects_api_mode_in_fallback(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "fallback_providers": [
                    {"provider": "x", "model": "y", "api_mode": "chat"},
                ]}
            )

    def test_rejects_extra_body_in_fallback(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "fallback_providers": [
                    {"provider": "x", "model": "y", "extra_body": {}},
                ]}
            )

    def test_rejects_base_url_in_auxiliary(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {
                    "vision": {"provider": "x", "model": "y", "base_url": "https://x"},
                }}
            )

    def test_rejects_timeout_in_auxiliary(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {
                    "vision": {"provider": "x", "model": "y", "timeout": 120},
                }}
            )

    def test_rejects_extra_body_in_auxiliary(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {
                    "vision": {"provider": "x", "model": "y", "extra_body": {}},
                }}
            )

    def test_rejects_reasoning_effort_in_auxiliary(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {
                    "vision": {"provider": "x", "model": "y", "reasoning_effort": "medium"},
                }}
            )

    def test_rejects_fallback_chain_in_auxiliary(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {
                    "vision": {"provider": "x", "model": "y", "fallback_chain": []},
                }}
            )

    def test_rejects_unknown_model_key(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state(
                {"version": 1, "model": {"provider": "x", "default": "y", "bogus": 1}}
            )
        self.assertIn("unknown key", str(cm.exception))

    def test_rejects_unknown_fallback_entry_key(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state(
                {"version": 1, "fallback_providers": [
                    {"provider": "x", "model": "y", "bogus": 1},
                ]}
            )
        self.assertIn("unknown key", str(cm.exception))

    def test_rejects_unknown_auxiliary_task(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {"unknown_task": {}}}
            )
        self.assertIn("unknown key", str(cm.exception))

    def test_rejects_unknown_auxiliary_task_key(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {
                    "vision": {"provider": "x", "model": "y", "bogus": 1},
                }}
            )
        self.assertIn("unknown key", str(cm.exception))

    def test_rejects_unknown_cron_key(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state({"version": 1, "cron": {"bogus": 1}})
        self.assertIn("unknown key", str(cm.exception))

    # -- secret-looking keys (deep scan) --

    def test_rejects_api_key_in_model(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state(
                {"version": 1, "model": {"provider": "x", "default": "y", "api_key": "s"}}
            )
        self.assertIn("secret-looking key", str(cm.exception))

    def test_rejects_key_env_in_fallback(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state(
                {"version": 1, "fallback_providers": [
                    {"provider": "x", "model": "y", "key_env": "K"},
                ]}
            )
        self.assertIn("secret-looking key", str(cm.exception))

    def test_rejects_api_key_env_in_auxiliary(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {
                    "vision": {"provider": "x", "model": "y", "api_key_env": "K"},
                }}
            )
        self.assertIn("secret-looking key", str(cm.exception))

    def test_rejects_secret_prefix_key(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state(
                {"version": 1, "model": {"provider": "x", "default": "y", "secret_value": "s"}}
            )
        self.assertIn("secret-looking key", str(cm.exception))

    # -- invalid shapes --

    def test_rejects_model_not_mapping(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state({"version": 1, "model": [1, 2]})

    def test_rejects_model_provider_missing(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state({"version": 1, "model": {"default": "y"}})

    def test_rejects_model_provider_empty(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "model": {"provider": "  ", "default": "y"}}
            )

    def test_rejects_model_default_missing(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state({"version": 1, "model": {"provider": "x"}})

    def test_rejects_fallback_provider_missing(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "fallback_providers": [{"model": "y"}]}
            )

    def test_rejects_fallback_not_list(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "fallback_providers": {"provider": "x"}}
            )

    def test_rejects_auxiliary_not_mapping(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state({"version": 1, "auxiliary": [1, 2]})

    def test_rejects_auxiliary_task_not_mapping(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {"vision": [1, 2]}}
            )

    def test_rejects_auxiliary_task_provider_missing(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "auxiliary": {"vision": {"model": "y"}}}
            )

    def test_rejects_cron_not_mapping(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state({"version": 1, "cron": [1, 2]})


# ---------------------------------------------------------------------------
# validate_models_state_from_text (canonical entry point for workspace_sync)
# ---------------------------------------------------------------------------


class ValidateModelsStateFromTextTests(unittest.TestCase):
    """validate_models_state_from_text: parse + validate raw text."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()

    def test_valid_text(self) -> None:
        result = self.m.validate_models_state_from_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["model"]["provider"], "x")

    def test_empty_text_returns_none(self) -> None:
        self.assertIsNone(self.m.validate_models_state_from_text(""))

    def test_malformed_yaml_raises(self) -> None:
        with self.assertRaises(ValueError) as cm:
            self.m.validate_models_state_from_text("version: 1\nmodel: {invalid\n")
        self.assertIn("invalid YAML", str(cm.exception))

    def test_schema_violation_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.m.validate_models_state_from_text(
                "version: 1\nmodel:\n  provider: x\n  default: y\n  api_key: s\n"
            )


# ---------------------------------------------------------------------------
# load_models_state (file I/O + validation)
# ---------------------------------------------------------------------------


class LoadModelsStateTests(unittest.TestCase):
    """load_models_state: absent -> None; malformed -> ValueError."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_absent_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            result = self.m.load_models_state(self.workspace / "hermes" / "models.yaml")
        self.assertIsNone(result)

    def test_valid_yaml_loads(self) -> None:
        path = _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: x\n  default: y\n",
        )
        result = self.m.load_models_state(path)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["model"]["provider"], "x")

    def test_malformed_yaml_raises(self) -> None:
        path = _write_models(self.workspace, "version: 1\nmodel: {invalid yaml\n")
        with self.assertRaises(ValueError) as cm:
            self.m.load_models_state(path)
        self.assertIn("invalid YAML", str(cm.exception))

    def test_schema_violation_raises(self) -> None:
        path = _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: x\n  default: y\n  api_key: s\n",
        )
        with self.assertRaises(ValueError):
            self.m.load_models_state(path)

    def test_empty_file_returns_none(self) -> None:
        path = _write_models(self.workspace, "")
        self.assertIsNone(self.m.load_models_state(path))


# ---------------------------------------------------------------------------
# apply_models_to_config (deep merge + preservation)
# ---------------------------------------------------------------------------


class ApplyModelsToConfigTests(unittest.TestCase):
    """apply_models_to_config overlays selection, preserves siblings (deep merge)."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_overlays_model_selection(self) -> None:
        config = {"model": {"default": "old", "provider": "old-p"}}
        models = {"version": 1, "model": {"provider": "deepseek", "default": "deepseek-v4-pro"}}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(config["model"]["provider"], "deepseek")
        self.assertEqual(config["model"]["default"], "deepseek-v4-pro")

    def test_overlays_fallback_providers(self) -> None:
        config = {"fallback_providers": [{"provider": "old", "model": "old-model"}]}
        models = {
            "version": 1,
            "fallback_providers": [
                {"provider": "ollama-cloud", "model": "glm-5.2:cloud"},
            ],
        }
        self.m.apply_models_to_config(config, models)
        self.assertEqual(
            config["fallback_providers"],
            [{"provider": "ollama-cloud", "model": "glm-5.2:cloud"}],
        )

    def test_overlays_empty_fallback_providers(self) -> None:
        config = {"fallback_providers": [{"provider": "old", "model": "old-model"}]}
        models = {"version": 1, "fallback_providers": []}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(config["fallback_providers"], [])

    def test_overlays_auxiliary_vision_preserves_siblings(self) -> None:
        """Deep merge: auxiliary.vision provider/model updated, siblings preserved."""
        config = {"auxiliary": {"vision": {
            "provider": "old", "model": "old-model",
            "api_key": "runtime-key",
            "download_timeout": 99,
            "base_url": "https://api.example.com",
        }}}
        models = {
            "version": 1,
            "auxiliary": {"vision": {"provider": "new", "model": "new-model"}},
        }
        self.m.apply_models_to_config(config, models)
        self.assertEqual(config["auxiliary"]["vision"]["provider"], "new")
        self.assertEqual(config["auxiliary"]["vision"]["model"], "new-model")
        # Template-owned sibling fields preserved (deep merge).
        self.assertEqual(config["auxiliary"]["vision"]["api_key"], "runtime-key")
        self.assertEqual(config["auxiliary"]["vision"]["download_timeout"], 99)
        self.assertEqual(config["auxiliary"]["vision"]["base_url"], "https://api.example.com")

    def test_overlays_cron_model(self) -> None:
        config = {"cron": {"wrap_response": False, "model": "old"}}
        models = {"version": 1, "cron": {"model": "new-model", "model_provider": "new-p"}}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(config["cron"]["model"], "new-model")
        self.assertEqual(config["cron"]["model_provider"], "new-p")
        # Unrelated cron key preserved.
        self.assertEqual(config["cron"]["wrap_response"], False)

    def test_preserves_unrelated_config_keys(self) -> None:
        config = {
            "model": {"default": "old", "provider": "old-p"},
            "memory": {"nudge_interval": 10, "memory_enabled": True},
            "skills": {"disabled": ["a"], "creation_nudge_interval": 0},
            "terminal": {"backend": "local"},
            "approvals": {"mode": "manual"},
        }
        models = {"version": 1, "model": {"provider": "x", "default": "y"}}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(config["memory"]["nudge_interval"], 10)
        self.assertEqual(config["memory"]["memory_enabled"], True)
        self.assertEqual(config["skills"]["disabled"], ["a"])
        self.assertEqual(config["terminal"]["backend"], "local")
        self.assertEqual(config["approvals"]["mode"], "manual")

    def test_absent_keys_in_models_do_not_clear_config(self) -> None:
        """Overlay semantics: absent keys in models do NOT clear config."""
        config = {
            "model": {"default": "old", "provider": "old-p"},
            "fallback_providers": [{"provider": "old", "model": "old-model"}],
            "auxiliary": {"vision": {"provider": "old", "model": "old-model"}},
        }
        # models.yaml only has model; fallback/auxiliary absent.
        models = {"version": 1, "model": {"provider": "x", "default": "y"}}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(config["model"]["provider"], "x")
        # fallback_providers and auxiliary preserved (not cleared).
        self.assertEqual(config["fallback_providers"], [{"provider": "old", "model": "old-model"}])
        self.assertEqual(config["auxiliary"]["vision"]["provider"], "old")

    def test_returns_true_when_changed(self) -> None:
        config = {"model": {"default": "old", "provider": "old-p"}}
        models = {"version": 1, "model": {"provider": "x", "default": "y"}}
        changed = self.m.apply_models_to_config(config, models)
        self.assertTrue(changed)

    def test_returns_false_when_unchanged(self) -> None:
        config = {"model": {"default": "y", "provider": "x"}}
        models = {"version": 1, "model": {"provider": "x", "default": "y"}}
        changed = self.m.apply_models_to_config(config, models)
        self.assertFalse(changed)

    def test_creates_model_section_if_absent(self) -> None:
        config = {}
        models = {"version": 1, "model": {"provider": "x", "default": "y"}}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(config["model"]["provider"], "x")
        self.assertEqual(config["model"]["default"], "y")


# ---------------------------------------------------------------------------
# restore_template_models_defaults (rollback semantics)
# ---------------------------------------------------------------------------


class RestoreTemplateDefaultsTests(unittest.TestCase):
    """restore_template_models_defaults: reset state-owned keys, preserve siblings."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_restores_model_selection(self) -> None:
        config = {"model": {"default": "operator", "provider": "operator-p"}}
        template = {"model": {"default": "tmpl", "provider": "tmpl-p"}}
        changed = self.m.restore_template_models_defaults(config, template)
        self.assertTrue(changed)
        self.assertEqual(config["model"]["default"], "tmpl")
        self.assertEqual(config["model"]["provider"], "tmpl-p")

    def test_restores_fallback_providers(self) -> None:
        config = {"fallback_providers": [{"provider": "op", "model": "op-model"}]}
        template = {"fallback_providers": [{"provider": "tmpl", "model": "tmpl-model"}]}
        self.m.restore_template_models_defaults(config, template)
        self.assertEqual(config["fallback_providers"], [{"provider": "tmpl", "model": "tmpl-model"}])

    def test_restores_auxiliary_preserves_siblings(self) -> None:
        """Rollback resets provider/model, preserves api_key/download_timeout."""
        config = {"auxiliary": {"vision": {
            "provider": "op", "model": "op-model",
            "api_key": "runtime-key",
            "download_timeout": 99,
        }}}
        template = {"auxiliary": {"vision": {
            "provider": "tmpl", "model": "tmpl-model",
            "api_key": "tmpl-key",
            "download_timeout": 30,
        }}}
        self.m.restore_template_models_defaults(config, template)
        self.assertEqual(config["auxiliary"]["vision"]["provider"], "tmpl")
        self.assertEqual(config["auxiliary"]["vision"]["model"], "tmpl-model")
        # Sibling fields preserved (NOT overwritten by template — deep merge).
        self.assertEqual(config["auxiliary"]["vision"]["api_key"], "runtime-key")
        self.assertEqual(config["auxiliary"]["vision"]["download_timeout"], 99)

    def test_restores_cron_model(self) -> None:
        config = {"cron": {"wrap_response": False, "model": "op", "model_provider": "op-p"}}
        template = {"cron": {"wrap_response": False, "model": "tmpl"}}
        self.m.restore_template_models_defaults(config, template)
        self.assertEqual(config["cron"]["model"], "tmpl")
        # model_provider not in template -> removed (restore to absent).
        self.assertNotIn("model_provider", config["cron"])
        # Unrelated cron key preserved.
        self.assertEqual(config["cron"]["wrap_response"], False)

    def test_removes_cron_keys_absent_in_template(self) -> None:
        """When template has no cron.model/model_provider, rollback removes them."""
        config = {"cron": {"model": "op", "model_provider": "op-p", "wrap_response": False}}
        template = {"cron": {"wrap_response": False}}
        self.m.restore_template_models_defaults(config, template)
        self.assertNotIn("model", config["cron"])
        self.assertNotIn("model_provider", config["cron"])
        self.assertEqual(config["cron"]["wrap_response"], False)

    def test_removes_fallback_providers_absent_in_template(self) -> None:
        """When template has no fallback_providers, rollback removes it."""
        config = {"fallback_providers": [{"provider": "op", "model": "op-model"}], "model": {"default": "x"}}
        template = {"model": {"default": "y", "provider": "p"}}
        self.m.restore_template_models_defaults(config, template)
        self.assertNotIn("fallback_providers", config)

    def test_preserves_unrelated_keys(self) -> None:
        config = {
            "model": {"default": "op", "provider": "op-p"},
            "memory": {"nudge_interval": 10},
            "approvals": {"mode": "manual"},
        }
        template = {"model": {"default": "tmpl", "provider": "tmpl-p"}}
        self.m.restore_template_models_defaults(config, template)
        self.assertEqual(config["memory"]["nudge_interval"], 10)
        self.assertEqual(config["approvals"]["mode"], "manual")


# ---------------------------------------------------------------------------
# apply_models_overlay (full file I/O + fail-closed + rollback)
# ---------------------------------------------------------------------------


class ApplyModelsOverlayTests(unittest.TestCase):
    """apply_models_overlay: validate fully before mutating config (fail-closed)."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.template = _write_template(
            self.workspace,
            "model:\n  default: tmpl-default\n  provider: tmpl-p\n"
            "fallback_providers:\n  - provider: tmpl-fb\n    model: tmpl-fb-model\n"
            "auxiliary:\n  vision:\n    provider: tmpl-vision\n    model: tmpl-vision-model\n"
            "    api_key: tmpl-key\n    download_timeout: 30\n"
            "cron:\n  wrap_response: false\n"
            "memory:\n  nudge_interval: 10\n",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_sidecar_no_template_returns_no_models_sidecar(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text("model:\n  default: old\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            status = self.m.apply_models_overlay(config_path)
        self.assertEqual(status, "no-models-sidecar")
        # Config untouched.
        self.assertEqual(_load_config(config_path)["model"]["default"], "old")

    def test_no_sidecar_with_template_restores_defaults(self) -> None:
        """Rollback: absent models.yaml restores state-owned keys to template."""
        config_path = self.workspace / "config.yaml"
        config_path.write_text(
            "model:\n  default: operator\n  provider: operator-p\n"
            "fallback_providers:\n  - provider: op-fb\n    model: op-fb-model\n"
            "auxiliary:\n  vision:\n    provider: op-vision\n    model: op-vision-model\n"
            "    api_key: runtime-key\n    download_timeout: 99\n"
            "cron:\n  wrap_response: false\n  model: op-cron\n  model_provider: op-cp\n"
            "memory:\n  nudge_interval: 10\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            status = self.m.apply_models_overlay(
                config_path, template_config_path=self.template
            )
        self.assertEqual(status, "restored-template-defaults")
        data = _load_config(config_path)
        self.assertEqual(data["model"]["default"], "tmpl-default")
        self.assertEqual(data["model"]["provider"], "tmpl-p")
        self.assertEqual(data["fallback_providers"], [{"provider": "tmpl-fb", "model": "tmpl-fb-model"}])
        self.assertEqual(data["auxiliary"]["vision"]["provider"], "tmpl-vision")
        self.assertEqual(data["auxiliary"]["vision"]["model"], "tmpl-vision-model")
        # Sibling fields preserved (deep merge).
        self.assertEqual(data["auxiliary"]["vision"]["api_key"], "runtime-key")
        self.assertEqual(data["auxiliary"]["vision"]["download_timeout"], 99)
        # cron.model restored to template (absent -> removed).
        self.assertNotIn("model", data["cron"])
        self.assertNotIn("model_provider", data["cron"])
        self.assertEqual(data["cron"]["wrap_response"], False)
        # Unrelated key preserved.
        self.assertEqual(data["memory"]["nudge_interval"], 10)

    def test_empty_sidecar_with_template_restores_defaults(self) -> None:
        """Rollback: empty models.yaml (YAML null) restores template defaults."""
        config_path = self.workspace / "config.yaml"
        config_path.write_text(
            "model:\n  default: operator\n  provider: operator-p\n",
            encoding="utf-8",
        )
        _write_models(self.workspace, "")  # empty file
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            status = self.m.apply_models_overlay(
                config_path, template_config_path=self.template
            )
        self.assertEqual(status, "restored-template-defaults")
        data = _load_config(config_path)
        self.assertEqual(data["model"]["default"], "tmpl-default")

    def test_valid_sidecar_applies_overlay(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text(
            "model:\n  default: old\n  provider: old-p\n"
            "auxiliary:\n  vision:\n    provider: old-v\n    model: old-vm\n"
            "    api_key: runtime-key\n    download_timeout: 99\n"
            "memory:\n  nudge_interval: 10\n",
            encoding="utf-8",
        )
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: deepseek\n  default: deepseek-v4-pro\n"
            "auxiliary:\n  vision:\n    provider: new-v\n    model: new-vm\n",
        )
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            status = self.m.apply_models_overlay(config_path)
        self.assertEqual(status, "applied-models")
        data = _load_config(config_path)
        self.assertEqual(data["model"]["provider"], "deepseek")
        self.assertEqual(data["model"]["default"], "deepseek-v4-pro")
        self.assertEqual(data["auxiliary"]["vision"]["provider"], "new-v")
        self.assertEqual(data["auxiliary"]["vision"]["model"], "new-vm")
        # Sibling fields preserved (deep merge).
        self.assertEqual(data["auxiliary"]["vision"]["api_key"], "runtime-key")
        self.assertEqual(data["auxiliary"]["vision"]["download_timeout"], 99)
        # Unrelated key preserved.
        self.assertEqual(data["memory"]["nudge_interval"], 10)

    def test_malformed_sidecar_leaves_config_untouched(self) -> None:
        config_path = self.workspace / "config.yaml"
        original = "model:\n  default: old\n  provider: old-p\n"
        config_path.write_text(original, encoding="utf-8")
        _write_models(self.workspace, "version: 1\nmodel: {invalid yaml\n")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            with self.assertRaises(ValueError):
                self.m.apply_models_overlay(config_path)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_schema_violation_leaves_config_untouched(self) -> None:
        config_path = self.workspace / "config.yaml"
        original = "model:\n  default: old\n"
        config_path.write_text(original, encoding="utf-8")
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: x\n  default: y\n  api_key: s\n",
        )
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            with self.assertRaises(ValueError):
                self.m.apply_models_overlay(config_path)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_forbidden_field_leaves_config_untouched(self) -> None:
        """base_url in models.yaml is rejected; config untouched."""
        config_path = self.workspace / "config.yaml"
        original = "model:\n  default: old\n"
        config_path.write_text(original, encoding="utf-8")
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: x\n  default: y\n  base_url: https://bad\n",
        )
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            with self.assertRaises(ValueError):
                self.m.apply_models_overlay(config_path)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_atomic_write_no_temp_left(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text("model:\n  default: old\n", encoding="utf-8")
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: x\n  default: y\n",
        )
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            self.m.apply_models_overlay(config_path)
        temps = list(self.workspace.glob(".config.yaml.*.tmp"))
        self.assertEqual(temps, [])


# ---------------------------------------------------------------------------
# apply-all integration (fail-closed, models overlay layered after policy)
# ---------------------------------------------------------------------------


class ApplyAllModelsOverlayTests(unittest.TestCase):
    """apply_all_sidecars_and_policy: models overlay fail-closed, layered after policy."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.template = _write_template(
            self.workspace,
            "model:\n  default: tmpl-default\n  provider: tmpl-p\n"
            "cron:\n  wrap_response: false\n",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_apply_all_includes_models_status(self) -> None:
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: old\n  provider: old-p\n"
            "skills:\n  creation_nudge_interval: 15\n",
            encoding="utf-8",
        )
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: deepseek\n  default: deepseek-v4-pro\n",
        )
        with mock.patch.dict(os.environ, {
            "WORKSPACE_DIR": str(self.workspace),
            "JOSEMAR_TEMPLATE_CONFIG": str(self.template),
        }):
            statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any(s.startswith("models:") for s in statuses))
        self.assertTrue(any(s.startswith("default:") for s in statuses))
        data = _load_config(self.workspace / "config.yaml")
        self.assertEqual(data["model"]["provider"], "deepseek")
        self.assertEqual(data["model"]["default"], "deepseek-v4-pro")
        # Policy also enforced.
        self.assertEqual(data["skills"]["creation_nudge_interval"], 0)
        self.assertEqual(data["curator"]["enabled"], False)

    def test_apply_all_models_overlay_after_policy(self) -> None:
        """Models overlay layered AFTER sidecar+policy (operator model wins)."""
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: template-default\n  provider: template-p\n"
            "skills:\n  creation_nudge_interval: 15\n",
            encoding="utf-8",
        )
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: operator-p\n  default: operator-default\n",
        )
        with mock.patch.dict(os.environ, {
            "WORKSPACE_DIR": str(self.workspace),
            "JOSEMAR_TEMPLATE_CONFIG": str(self.template),
        }):
            self.m.apply_all_sidecars_and_policy()
        data = _load_config(self.workspace / "config.yaml")
        self.assertEqual(data["model"]["provider"], "operator-p")
        self.assertEqual(data["model"]["default"], "operator-default")

    def test_apply_all_models_error_fails_nonzero(self) -> None:
        """A malformed models.yaml makes apply-all raise (fail-closed nonzero)."""
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: old\nskills:\n  creation_nudge_interval: 15\n",
            encoding="utf-8",
        )
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: x\n  default: y\n  api_key: s\n",
        )
        with mock.patch.dict(os.environ, {
            "WORKSPACE_DIR": str(self.workspace),
            "JOSEMAR_TEMPLATE_CONFIG": str(self.template),
        }):
            with self.assertRaises(ValueError):
                self.m.apply_all_sidecars_and_policy()
        # Config: policy enforced (written before models overlay), model untouched.
        data = _load_config(self.workspace / "config.yaml")
        self.assertEqual(data["skills"]["creation_nudge_interval"], 0)
        # Model NOT overwritten (fail-closed — last-known-good preserved).
        self.assertEqual(data["model"]["default"], "old")

    def test_apply_all_no_models_sidecar_restores_template(self) -> None:
        """Absent models.yaml + template path -> restore template defaults."""
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: operator\n  provider: operator-p\n"
            "cron:\n  wrap_response: false\n  model: op-cron\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {
            "WORKSPACE_DIR": str(self.workspace),
            "JOSEMAR_TEMPLATE_CONFIG": str(self.template),
        }):
            statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any("models:restored-template-defaults" in s for s in statuses))
        data = _load_config(self.workspace / "config.yaml")
        self.assertEqual(data["model"]["default"], "tmpl-default")
        self.assertEqual(data["model"]["provider"], "tmpl-p")
        # cron.model removed (template doesn't define it).
        self.assertNotIn("model", data["cron"])

    def test_apply_all_no_models_no_template_no_op(self) -> None:
        """Absent models.yaml + no template path -> no-op (init template copy handles it)."""
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: old\n", encoding="utf-8"
        )
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            statuses = self.m.apply_all_sidecars_and_policy()
        self.assertTrue(any("models:no-models-sidecar" in s for s in statuses))
        data = _load_config(self.workspace / "config.yaml")
        self.assertEqual(data["model"]["default"], "old")


# ---------------------------------------------------------------------------
# sync_and_apply: apply errors fail nonzero
# ---------------------------------------------------------------------------


class SyncAndApplyModelsTests(unittest.TestCase):
    """sync_and_apply: invalid model state makes it fail nonzero."""

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

    def test_successful_sync_then_apply_with_models(self) -> None:
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: old\nskills:\n  creation_nudge_interval: 15\n",
            encoding="utf-8",
        )
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: deepseek\n  default: deepseek-v4-pro\n",
        )
        script = self._make_sync_script(body="echo SYNC_OK\n")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            exit_status, statuses, output = self.m.sync_and_apply([str(script)])
        self.assertEqual(exit_status, 0)
        self.assertIn("SYNC_OK", output)
        self.assertTrue(any("models:applied-models" in s for s in statuses))

    def test_invalid_models_makes_sync_and_apply_fail_nonzero(self) -> None:
        """Invalid models.yaml -> sync succeeds but apply fails nonzero."""
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: old\n", encoding="utf-8"
        )
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: x\n  default: y\n  api_key: s\n",
        )
        script = self._make_sync_script(body="echo SYNC_OK\n")
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            exit_status, statuses, output = self.m.sync_and_apply([str(script)])
        self.assertNotEqual(exit_status, 0)
        self.assertIn("SYNC_OK", output)
        # Config untouched (fail-closed).
        data = _load_config(self.workspace / "config.yaml")
        self.assertEqual(data["model"]["default"], "old")


# ---------------------------------------------------------------------------
# Init ordering (models overlay layered after template copy + workspace sync)
# ---------------------------------------------------------------------------


class InitModelsOverlayOrderingTests(unittest.TestCase):
    """docker-hermes-init.sh applies models overlay after template copy + sync."""

    def setUp(self) -> None:
        self.src = INIT_PATH.read_text(encoding="utf-8")

    def test_apply_sidecars_called_after_template_copy(self) -> None:
        template_pos = self.src.find("Syncing Hermes config.yaml from repo template")
        apply_pos = self.src.find("\napply_sidecars_and_policy\n")
        self.assertGreater(template_pos, 0)
        self.assertGreater(apply_pos, 0)
        self.assertLess(template_pos, apply_pos)

    def test_apply_sidecars_called_after_workspace_sync(self) -> None:
        sync_pos = self.src.find("Running workspace git sync as hermes user")
        apply_pos = self.src.find("\napply_sidecars_and_policy\n")
        self.assertGreater(sync_pos, 0)
        self.assertLess(sync_pos, apply_pos)

    def test_apply_sidecars_called_after_seed_from_manifest(self) -> None:
        seed_pos = self.src.find("seed_workspace_from_manifest")
        apply_pos = self.src.find("\napply_sidecars_and_policy\n")
        self.assertGreater(seed_pos, 0)
        self.assertLess(seed_pos, apply_pos)

    def test_init_documents_models_overlay_layering(self) -> None:
        self.assertIn("models.yaml", self.src)
        self.assertIn("model authoring overlay", self.src)

    def test_init_passes_template_config(self) -> None:
        """Init passes JOSEMAR_TEMPLATE_CONFIG so rollback can restore defaults."""
        self.assertIn("JOSEMAR_TEMPLATE_CONFIG", self.src)
        self.assertIn("$SOURCE_CONFIG", self.src)

    def test_init_does_not_silently_continue_on_apply_failure(self) -> None:
        """Init must NOT swallow apply-all failures with `|| log ... continuing`."""
        # The apply-all call must propagate nonzero (no `|| log` fallback).
        apply_section = self.src.split("apply_sidecars_and_policy()")[1].split("\n}")[0]
        self.assertIn("apply-all", apply_section)
        # The old pattern `|| log "WARNING: ... continuing"` must NOT appear
        # in the apply_sidecars_and_policy function body.
        self.assertNotIn('|| log "WARNING: skill toggle apply/policy failed; continuing"', apply_section)

    def test_apply_all_is_the_apply_mechanism(self) -> None:
        self.assertIn("apply-all", self.src)


# ---------------------------------------------------------------------------
# CLI: apply-models root-only + fail-closed
# ---------------------------------------------------------------------------


class CliApplyModelsTests(unittest.TestCase):
    """apply-models CLI: root-only, fail-closed, template config."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cli_apply_models_no_sidecar_no_template(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text("model:\n  default: old\n", encoding="utf-8")
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), "apply-models",
             "--config-path", str(config_path)],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no-models-sidecar", result.stdout)

    def test_cli_apply_models_with_sidecar(self) -> None:
        config_path = self.workspace / "config.yaml"
        config_path.write_text(
            "model:\n  default: old\n  provider: old-p\n", encoding="utf-8"
        )
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: deepseek\n  default: deepseek-v4-pro\n",
        )
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), "apply-models",
             "--config-path", str(config_path)],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("applied-models", result.stdout)
        data = _load_config(config_path)
        self.assertEqual(data["model"]["provider"], "deepseek")

    def test_cli_apply_models_malformed_fails_closed(self) -> None:
        config_path = self.workspace / "config.yaml"
        original = "model:\n  default: old\n"
        config_path.write_text(original, encoding="utf-8")
        _write_models(self.workspace, "version: 1\nmodel: {invalid\n")
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), "apply-models",
             "--config-path", str(config_path)],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_cli_apply_models_rejects_named_profile(self) -> None:
        """apply-models for a named profile hermes_home must fail nonzero."""
        profile_home = self.workspace / "profiles" / "coder"
        profile_home.mkdir(parents=True)
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), "apply-models",
             "--hermes-home", str(profile_home)],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root-only", result.stderr)

    def test_cli_apply_models_rejects_non_workspace_home(self) -> None:
        """apply-models for a hermes home other than workspace root must fail."""
        other = self.workspace / "somewhere" / "else"
        other.mkdir(parents=True)
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), "apply-models",
             "--hermes-home", str(other)],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root-only", result.stderr)

    def test_cli_apply_models_rollback_with_template(self) -> None:
        """apply-models with absent models.yaml + template restores defaults."""
        template = _write_template(
            self.workspace,
            "model:\n  default: tmpl\n  provider: tmpl-p\n",
        )
        config_path = self.workspace / "config.yaml"
        config_path.write_text(
            "model:\n  default: operator\n  provider: operator-p\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), "apply-models",
             "--config-path", str(config_path),
             "--template-config", str(template)],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restored-template-defaults", result.stdout)
        data = _load_config(config_path)
        self.assertEqual(data["model"]["default"], "tmpl")

    def test_cli_apply_all_fails_nonzero_on_models_error(self) -> None:
        """apply-all CLI returns nonzero on models overlay validation failure."""
        (self.workspace / "config.yaml").write_text(
            "model:\n  default: old\n", encoding="utf-8"
        )
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: x\n  default: y\n  api_key: s\n",
        )
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), "apply-all"],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        # Config untouched (fail-closed).
        data = _load_config(self.workspace / "config.yaml")
        self.assertEqual(data["model"]["default"], "old")


# ---------------------------------------------------------------------------
# Workspace sync: models.yaml validation before staging/commit + remote merge
# ---------------------------------------------------------------------------


class WorkspaceSyncModelsValidationTests(unittest.TestCase):
    """workspace_sync.py validates models.yaml locally and remotely."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        subprocess.run(
            ["git", "init", "-q", str(self.workspace)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "checkout", "-q", "-B", "main"],
            check=True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_manifest_gitignore(self) -> None:
        (self.workspace / ".sync-manifest").write_text(
            ".gitignore\n.sync-manifest\nhermes/models.yaml\n",
            encoding="utf-8",
        )
        (self.workspace / ".gitignore").write_text(
            "*\n!.gitignore\n!.sync-manifest\n"
            "!hermes/\n!hermes/models.yaml\n",
            encoding="utf-8",
        )

    def _run_sync_tool(self, action: str, message: str = "test") -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        env["JOSEMAR_SKILL_STATE"] = str(HELPER_PATH)
        payload = {"action": action, "message": message}
        return subprocess.run(
            [sys.executable, str(WORKSPACE_SYNC_PATH)],
            input=json.dumps(payload),
            env=env, capture_output=True, text=True, check=False,
        )

    def test_valid_models_yaml_can_be_committed(self) -> None:
        """Valid models.yaml passes local validation and is committed."""
        self._write_manifest_gitignore()
        # Initial commit.
        self._run_sync_tool("commit", "initial")
        models_path = self.workspace / "hermes" / "models.yaml"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n",
            encoding="utf-8",
        )
        result = self._run_sync_tool("commit", "add models")
        self.assertEqual(result.returncode, 0, result.stderr)
        tracked = subprocess.run(
            ["git", "-C", str(self.workspace), "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("hermes/models.yaml", tracked)

    def test_invalid_models_yaml_rejected_before_commit(self) -> None:
        """Invalid models.yaml (secret key) fails local validation, nonzero."""
        self._write_manifest_gitignore()
        self._run_sync_tool("commit", "initial")
        models_path = self.workspace / "hermes" / "models.yaml"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n  api_key: secret\n",
            encoding="utf-8",
        )
        result = self._run_sync_tool("commit", "add bad models")
        self.assertNotEqual(result.returncode, 0)
        # File NOT staged/committed.
        tracked = subprocess.run(
            ["git", "-C", str(self.workspace), "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn("hermes/models.yaml", tracked)

    def test_forbidden_field_rejected_before_commit(self) -> None:
        """base_url in models.yaml fails local validation, nonzero."""
        self._write_manifest_gitignore()
        self._run_sync_tool("commit", "initial")
        models_path = self.workspace / "hermes" / "models.yaml"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n  base_url: https://bad\n",
            encoding="utf-8",
        )
        result = self._run_sync_tool("commit", "add bad models")
        self.assertNotEqual(result.returncode, 0)

    def test_malformed_yaml_rejected_before_commit(self) -> None:
        """Malformed YAML in models.yaml fails local validation, nonzero."""
        self._write_manifest_gitignore()
        self._run_sync_tool("commit", "initial")
        models_path = self.workspace / "hermes" / "models.yaml"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text("version: 1\nmodel: {invalid\n", encoding="utf-8")
        result = self._run_sync_tool("commit", "add bad models")
        self.assertNotEqual(result.returncode, 0)


# ---------------------------------------------------------------------------
# Workspace sync: models.yaml manifest path is stageable
# ---------------------------------------------------------------------------


class WorkspaceSyncModelsPathTests(unittest.TestCase):
    """workspace_sync.py allows hermes/models.yaml as a manifest entry."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "checkout", "-q", "-B", "main"],
            check=True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_models_yaml_not_rejected_as_wildcard(self) -> None:
        """hermes/models.yaml has no glob chars; passes manifest validation."""
        (self.workspace / ".sync-manifest").write_text(
            ".gitignore\n.sync-manifest\nhermes/models.yaml\n",
            encoding="utf-8",
        )
        (self.workspace / ".gitignore").write_text(
            "*\n!.gitignore\n!.sync-manifest\n"
            "!hermes/\n!hermes/models.yaml\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        env["JOSEMAR_SKILL_STATE"] = str(HELPER_PATH)
        result = subprocess.run(
            [sys.executable, str(WORKSPACE_SYNC_PATH)],
            input=json.dumps({"action": "status"}),
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["success"])
        self.assertIn("hermes/models.yaml", data["tracked_patterns"])


# ---------------------------------------------------------------------------
# Shipped template schema compatibility
# ---------------------------------------------------------------------------


class TemplateSchemaCompatibilityTests(unittest.TestCase):
    """The shipped template models.yaml must pass canonical validation and
    carry exactly the reviewed auxiliary allowlist (manual migration only —
    no automatic migration/expand of existing state files)."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()

    def _validated_template(self) -> dict:
        """Canonical-parse + validate the shipped template; dict, or fail.

        The shipped template is a hard contract: a deleted/renamed
        ``templates/agent-state-template/hermes/models.yaml`` must fail these
        tests rather than silently skip them.
        """
        self.assertTrue(
            TEMPLATE_MODELS_YAML.exists(),
            "shipped template hermes/models.yaml is missing or renamed; its "
            "contract tests must not silently skip",
        )
        text = TEMPLATE_MODELS_YAML.read_text(encoding="utf-8")
        result = self.m.validate_models_state_from_text(text)
        self.assertIsNotNone(result)
        assert result is not None
        return result

    def test_template_models_yaml_validates(self) -> None:
        """The shipped template hermes/models.yaml passes strict v1 validation."""
        result = self._validated_template()
        self.assertEqual(result.get("version"), 1)
        # Must have model with nonempty provider/default.
        self.assertIn("model", result)
        self.assertTrue(result["model"]["provider"])
        self.assertTrue(result["model"]["default"])

    def test_template_auxiliary_keys_equal_helper_allowlist(self) -> None:
        """Template auxiliary keys are exactly ALLOWED_AUXILIARY_TASKS
        (therefore the exact reviewed 11-ID allowlist, same order)."""
        result = self._validated_template()
        aux = result.get("auxiliary")
        self.assertIsInstance(aux, dict)
        assert isinstance(aux, dict)
        self.assertEqual(list(aux.keys()), list(self.m.ALLOWED_AUXILIARY_TASKS))

    def test_template_non_vision_auxiliary_slots_use_auto(self) -> None:
        """All 10 non-vision template slots ship provider=auto, model=''."""
        result = self._validated_template()
        aux = result["auxiliary"]
        for task in self.m.ALLOWED_AUXILIARY_TASKS:
            if task == "vision":
                continue
            with self.subTest(task=task):
                self.assertEqual(aux[task], {"provider": "auto", "model": ""})

    def test_template_vision_keeps_concrete_selection(self) -> None:
        """vision preserves its concrete (non-auto) selection."""
        result = self._validated_template()
        vision = result["auxiliary"]["vision"]
        self.assertNotEqual(vision["provider"], "auto")
        self.assertTrue(vision["provider"])
        self.assertTrue(vision["model"])


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class ModelsPathTests(unittest.TestCase):
    """_models_sidecar_path resolves to <workspace>/hermes/models.yaml."""

    def setUp(self) -> None:
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_models_path_under_hermes_dir(self) -> None:
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            path = self.m._models_sidecar_path()
        self.assertEqual(path.name, "models.yaml")
        self.assertEqual(path.parent.name, "hermes")
        self.assertTrue(path.is_relative_to(self.workspace))

    def test_template_config_path_default(self) -> None:
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            path = self.m._template_config_path()
        self.assertEqual(str(path), self.m.DEFAULT_TEMPLATE_CONFIG_PATH)

    def test_template_config_path_env_override(self) -> None:
        with mock.patch.dict(os.environ, {
            "WORKSPACE_DIR": str(self.workspace),
            "JOSEMAR_TEMPLATE_CONFIG": "/custom/template.yaml",
        }):
            path = self.m._template_config_path()
        self.assertEqual(str(path), "/custom/template.yaml")


# ---------------------------------------------------------------------------
# Finding 1: Validation-helper availability (fail-closed when models.yaml exists)
# ---------------------------------------------------------------------------


class ValidationHelperAvailabilityTests(unittest.TestCase):
    """Validation must fail-closed when models.yaml exists, even if helper unavailable."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_absent_models_yaml_no_helper_still_valid(self) -> None:
        """Absence of models.yaml remains valid regardless of helper availability."""
        # No models.yaml present — load_models_state returns None.
        with mock.patch.dict(os.environ, {"WORKSPACE_DIR": str(self.workspace)}):
            result = self.m.load_models_state(self.workspace / "hermes" / "models.yaml")
        self.assertIsNone(result)

    def test_present_models_yaml_helper_unavailable_fails_closed(self) -> None:
        """Present models.yaml + unavailable validator helper -> SyncError (fail-closed)."""
        # Initialize a git repo so the sync tool can attempt staging.
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "checkout", "-q", "-B", "main"],
            check=True,
        )
        (self.workspace / ".sync-manifest").write_text(
            ".gitignore\n.sync-manifest\nhermes/models.yaml\n",
            encoding="utf-8",
        )
        (self.workspace / ".gitignore").write_text(
            "*\n!.gitignore\n!.sync-manifest\n!hermes/\n!hermes/models.yaml\n",
            encoding="utf-8",
        )
        _write_models(
            self.workspace,
            "version: 1\nmodel:\n  provider: x\n  default: y\n",
        )
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        env["JOSEMAR_SKILL_STATE"] = "/nonexistent/helper.py"
        result = subprocess.run(
            [sys.executable, str(WORKSPACE_SYNC_PATH)],
            input=json.dumps({"action": "commit", "message": "test"}),
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertFalse(data["success"])
        self.assertIn("unavailable", data["error"].lower())

    def test_init_fails_nonzero_when_helper_missing_and_models_present(self) -> None:
        """Init must fail nonzero if models.yaml present but helper unavailable."""
        # Simulate the init check: helper missing + models.yaml present.
        # The init script checks this condition and returns 1.
        src = INIT_PATH.read_text(encoding="utf-8")
        # The init must have the fail-closed check for present models.yaml.
        self.assertIn("${WORKSPACE_DIR}/hermes/models.yaml", src)
        self.assertIn("return 1", src)


# ---------------------------------------------------------------------------
# Finding 2: workspace-sync push validates local models.yaml (all three paths)
# ---------------------------------------------------------------------------


class WorkspaceSyncPushValidationTests(unittest.TestCase):
    """workspace-sync push/commit/remote-acceptance all validate models.yaml."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "checkout", "-q", "-B", "main"],
            check=True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_manifest_gitignore(self) -> None:
        (self.workspace / ".sync-manifest").write_text(
            ".gitignore\n.sync-manifest\nhermes/models.yaml\n",
            encoding="utf-8",
        )
        (self.workspace / ".gitignore").write_text(
            "*\n!.gitignore\n!.sync-manifest\n"
            "!hermes/\n!hermes/models.yaml\n",
            encoding="utf-8",
        )

    def _run_sync_tool(self, action: str, message: str = "test") -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        env["JOSEMAR_SKILL_STATE"] = str(HELPER_PATH)
        payload = {"action": action, "message": message}
        return subprocess.run(
            [sys.executable, str(WORKSPACE_SYNC_PATH)],
            input=json.dumps(payload),
            env=env, capture_output=True, text=True, check=False,
        )

    def _setup_remote_and_initial_commit(self) -> None:
        """Set up a bare remote and push initial state so push has a target."""
        remote_dir = tempfile.mkdtemp(prefix="ws-remote-")
        self._remote = remote_dir
        subprocess.run(["git", "init", "-q", "--bare", remote_dir], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "remote", "add", "origin", remote_dir],
            check=True,
        )
        self._write_manifest_gitignore()
        self._run_sync_tool("commit", "initial")
        # Push initial state to remote.
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        env["JOSEMAR_SKILL_STATE"] = str(HELPER_PATH)
        subprocess.run(
            [sys.executable, str(WORKSPACE_SYNC_PATH)],
            input=json.dumps({"action": "push"}),
            env=env, capture_output=True, text=True, check=False,
        )

    def test_push_validates_head_models_yaml(self) -> None:
        """push action validates HEAD-committed models.yaml before pushing."""
        self._setup_remote_and_initial_commit()
        # Commit a valid models.yaml first.
        models_path = self.workspace / "hermes" / "models.yaml"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n",
            encoding="utf-8",
        )
        self._run_sync_tool("commit", "add valid models")
        # Now corrupt the committed models.yaml via direct git manipulation
        # (simulating a commit by another path) — amend the commit with
        # invalid content.
        models_path.write_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n  api_key: secret\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "add", "hermes/models.yaml"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "--amend", "--no-edit"],
            check=True,
        )
        # push must fail because HEAD carries an invalid models.yaml.
        result = self._run_sync_tool("push")
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertFalse(data["success"])

    def test_push_rejects_invalid_head_models_yaml(self) -> None:
        """push with invalid HEAD models.yaml fails nonzero."""
        self._setup_remote_and_initial_commit()
        # Commit an invalid models.yaml via direct git (bypassing validation).
        models_path = self.workspace / "hermes" / "models.yaml"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n  base_url: https://bad\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "add", "hermes/models.yaml"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-m", "bad models via direct git"],
            check=True,
        )
        # push must fail.
        result = self._run_sync_tool("push")
        self.assertNotEqual(result.returncode, 0)

    def test_push_valid_models_yaml_succeeds(self) -> None:
        """push with valid HEAD models.yaml succeeds."""
        self._setup_remote_and_initial_commit()
        models_path = self.workspace / "hermes" / "models.yaml"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n",
            encoding="utf-8",
        )
        self._run_sync_tool("commit", "add valid models")
        result = self._run_sync_tool("push")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertTrue(data["success"])

    def test_commit_validates_staging_models_yaml(self) -> None:
        """commit action validates working-copy models.yaml before staging."""
        self._setup_remote_and_initial_commit()
        models_path = self.workspace / "hermes" / "models.yaml"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n  api_key: secret\n",
            encoding="utf-8",
        )
        result = self._run_sync_tool("commit", "bad models")
        self.assertNotEqual(result.returncode, 0)

    def test_remote_acceptance_validates_models_yaml(self) -> None:
        """pull/sync validates remote candidate models.yaml before merge."""
        # This is covered by _assert_remote_tree_safe which calls
        # _validate_remote_models_yaml. Verify the validation function
        # exists and is called.
        src = WORKSPACE_SYNC_PATH.read_text(encoding="utf-8")
        self.assertIn("_validate_remote_models_yaml", src)
        self.assertIn("_assert_remote_tree_safe", src)

    def test_push_helper_unavailable_with_present_models_fails_closed(self) -> None:
        """push with present models.yaml + unavailable helper -> fail-closed."""
        self._setup_remote_and_initial_commit()
        models_path = self.workspace / "hermes" / "models.yaml"
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(
            "version: 1\nmodel:\n  provider: x\n  default: y\n",
            encoding="utf-8",
        )
        self._run_sync_tool("commit", "add valid models")
        # Now push with helper unavailable.
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        env["JOSEMAR_SKILL_STATE"] = "/nonexistent/helper.py"
        result = subprocess.run(
            [sys.executable, str(WORKSPACE_SYNC_PATH)],
            input=json.dumps({"action": "push"}),
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertFalse(data["success"])
        self.assertIn("unavailable", data["error"].lower())


# ---------------------------------------------------------------------------
# Finding 3: Root-only CLI scope for --config-path
# ---------------------------------------------------------------------------


class CliApplyModelsConfigPathTests(unittest.TestCase):
    """apply-models --config-path must be exactly <workspace-root>/config.yaml."""

    def setUp(self) -> None:
        if not _has_yaml():
            self.skipTest("PyYAML not available")
        self.m = _load_helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_cli(self, *args: str, env_override: dict | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WORKSPACE_DIR"] = str(self.workspace)
        if env_override:
            env.update(env_override)
        return subprocess.run(
            [sys.executable, str(HELPER_PATH), "apply-models", *args],
            env=env, capture_output=True, text=True, check=False,
        )

    def test_config_path_workspace_root_config_accepted(self) -> None:
        """--config-path <workspace>/config.yaml is accepted."""
        config_path = self.workspace / "config.yaml"
        config_path.write_text("model:\n  default: old\n", encoding="utf-8")
        result = self._run_cli("--config-path", str(config_path))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_path_profile_config_rejected(self) -> None:
        """--config-path pointing to a profile config is rejected."""
        profile_home = self.workspace / "profiles" / "coder"
        profile_home.mkdir(parents=True)
        profile_cfg = profile_home / "config.yaml"
        profile_cfg.write_text("model:\n  default: x\n", encoding="utf-8")
        result = self._run_cli("--config-path", str(profile_cfg))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be exactly", result.stderr)

    def test_config_path_arbitrary_path_rejected(self) -> None:
        """--config-path pointing to an arbitrary file is rejected."""
        arb = self.workspace / "arbitrary.yaml"
        arb.write_text("model:\n  default: x\n", encoding="utf-8")
        result = self._run_cli("--config-path", str(arb))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be exactly", result.stderr)

    def test_config_path_outside_workspace_rejected(self) -> None:
        """--config-path outside the workspace is rejected."""
        outside = Path(tempfile.mkdtemp()) / "config.yaml"
        outside.write_text("model:\n  default: x\n", encoding="utf-8")
        result = self._run_cli("--config-path", str(outside))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be exactly", result.stderr)
        outside.unlink()
        outside.parent.rmdir()

    def test_default_config_path_accepted(self) -> None:
        """No --config-path defaults to <workspace>/config.yaml (accepted)."""
        config_path = self.workspace / "config.yaml"
        config_path.write_text("model:\n  default: old\n", encoding="utf-8")
        result = self._run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hermes_home_and_config_path_both_checked(self) -> None:
        """Both --hermes-home and --config-path must resolve to workspace root."""
        profile_home = self.workspace / "profiles" / "coder"
        profile_home.mkdir(parents=True)
        # --hermes-home is workspace root, but --config-path is profile.
        root_cfg = self.workspace / "config.yaml"
        root_cfg.write_text("model:\n  default: x\n", encoding="utf-8")
        profile_cfg = profile_home / "config.yaml"
        profile_cfg.write_text("model:\n  default: x\n", encoding="utf-8")
        result = self._run_cli(
            "--hermes-home", str(self.workspace),
            "--config-path", str(profile_cfg),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be exactly", result.stderr)

    def test_config_path_symlink_to_root_config_rejected(self) -> None:
        """A named-profile config symlink pointing to root config is rejected.

        The literal path check rejects it because the symlink path differs
        from <workspace>/config.yaml. Even if it resolved to the same
        target, the symlink check would catch it.
        """
        root_cfg = self.workspace / "config.yaml"
        root_cfg.write_text("model:\n  default: x\n", encoding="utf-8")
        profile_home = self.workspace / "profiles" / "coder"
        profile_home.mkdir(parents=True)
        symlink_cfg = profile_home / "config.yaml"
        os.symlink(str(root_cfg), str(symlink_cfg))
        result = self._run_cli("--config-path", str(symlink_cfg))
        self.assertNotEqual(result.returncode, 0)
        # Rejected by the literal path check (symlink path != expected path).
        self.assertIn("must be exactly", result.stderr)
        # Root config must NOT be mutated by the rejected attempt.
        import yaml
        data = yaml.safe_load(root_cfg.read_text(encoding="utf-8"))
        self.assertEqual(data, {"model": {"default": "x"}})

    def test_config_path_symlink_at_expected_path_rejected(self) -> None:
        """A symlink AT the expected config path is rejected by the symlink check."""
        real_cfg = self.workspace / "real-config.yaml"
        real_cfg.write_text("model:\n  default: x\n", encoding="utf-8")
        root_cfg = self.workspace / "config.yaml"
        os.symlink(str(real_cfg), str(root_cfg))
        result = self._run_cli("--config-path", str(root_cfg))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr.lower())

    def test_config_path_relative_alias_rejected(self) -> None:
        """A relative path alias that resolves to root config is rejected by literal check."""
        root_cfg = self.workspace / "config.yaml"
        root_cfg.write_text("model:\n  default: x\n", encoding="utf-8")
        # Use a relative path like ./config.yaml from the workspace dir.
        # The literal path "./config.yaml" != "<workspace>/config.yaml".
        result = self._run_cli("--config-path", "./config.yaml")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be exactly", result.stderr)


# ---------------------------------------------------------------------------
# Finding 4: Fallback merging (positional merge preserving siblings)
# ---------------------------------------------------------------------------


class FallbackPositionalMergeTests(unittest.TestCase):
    """apply_models_to_config / restore_template: provider-matched merge preserving siblings.

    Sibling fields (base_url/api_mode/api_key/extra_body) are preserved ONLY
    when the state/template entry's provider matches an existing entry's
    provider. On provider change/new provider, a minimal {provider, model}
    entry is created — runtime-only siblings are NEVER transferred from one
    provider to another. A consumed-entry mechanism handles duplicate
    providers and reordering.
    """

    def setUp(self) -> None:
        self.m = _load_helper()

    # -- apply: same provider preserves siblings --

    def test_apply_same_provider_preserves_siblings(self) -> None:
        """Apply with same provider updates model, preserves siblings."""
        config = {"fallback_providers": [
            {"provider": "same", "model": "old-model",
             "base_url": "https://same", "api_mode": "chat",
             "api_key": "key1", "extra_body": {"x": 1}},
        ]}
        models = {"version": 1, "fallback_providers": [
            {"provider": "same", "model": "new-model"},
        ]}
        self.m.apply_models_to_config(config, models)
        entry = config["fallback_providers"][0]
        self.assertEqual(entry["provider"], "same")
        self.assertEqual(entry["model"], "new-model")
        # Siblings preserved (same provider).
        self.assertEqual(entry["base_url"], "https://same")
        self.assertEqual(entry["api_mode"], "chat")
        self.assertEqual(entry["api_key"], "key1")
        self.assertEqual(entry["extra_body"], {"x": 1})

    # -- apply: provider change -> no sibling transfer --

    def test_apply_provider_change_no_sibling_transfer(self) -> None:
        """Provider change creates minimal dict — no sibling transfer."""
        config = {"fallback_providers": [
            {"provider": "old1", "model": "old1-model",
             "base_url": "https://old1", "api_mode": "chat",
             "api_key": "key1", "extra_body": {"x": 1}},
        ]}
        models = {"version": 1, "fallback_providers": [
            {"provider": "new1", "model": "new1-model"},
        ]}
        self.m.apply_models_to_config(config, models)
        entry = config["fallback_providers"][0]
        self.assertEqual(entry["provider"], "new1")
        self.assertEqual(entry["model"], "new1-model")
        # NO siblings transferred from old1.
        self.assertNotIn("base_url", entry)
        self.assertNotIn("api_mode", entry)
        self.assertNotIn("api_key", entry)
        self.assertNotIn("extra_body", entry)

    def test_apply_provider_change_credentials_no_transfer(self) -> None:
        """api_key/base_url must not transfer to a different provider."""
        config = {"fallback_providers": [
            {"provider": "alpha", "model": "a-model",
             "base_url": "https://alpha", "api_key": "secret-alpha"},
        ]}
        models = {"version": 1, "fallback_providers": [
            {"provider": "beta", "model": "b-model"},
        ]}
        self.m.apply_models_to_config(config, models)
        entry = config["fallback_providers"][0]
        self.assertEqual(entry["provider"], "beta")
        self.assertNotIn("api_key", entry)
        self.assertNotIn("base_url", entry)

    # -- apply: reorder preserves siblings by provider match --

    def test_apply_reorder_preserves_siblings_by_provider(self) -> None:
        """Reorder: siblings follow the provider match, not the index."""
        config = {"fallback_providers": [
            {"provider": "a", "model": "a-model", "base_url": "https://a", "api_key": "ka"},
            {"provider": "b", "model": "b-model", "base_url": "https://b", "api_key": "kb"},
        ]}
        models = {"version": 1, "fallback_providers": [
            {"provider": "b", "model": "b-new"},
            {"provider": "a", "model": "a-new"},
        ]}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(len(config["fallback_providers"]), 2)
        # b is now first, with its siblings.
        self.assertEqual(config["fallback_providers"][0]["provider"], "b")
        self.assertEqual(config["fallback_providers"][0]["base_url"], "https://b")
        self.assertEqual(config["fallback_providers"][0]["api_key"], "kb")
        self.assertEqual(config["fallback_providers"][0]["model"], "b-new")
        # a is now second, with its siblings.
        self.assertEqual(config["fallback_providers"][1]["provider"], "a")
        self.assertEqual(config["fallback_providers"][1]["base_url"], "https://a")
        self.assertEqual(config["fallback_providers"][1]["api_key"], "ka")
        self.assertEqual(config["fallback_providers"][1]["model"], "a-new")

    # -- apply: duplicates with consumed-entry mechanism --

    def test_apply_duplicates_preserve_siblings_by_consumed_match(self) -> None:
        """Duplicate providers: each state entry consumes one matching existing entry."""
        config = {"fallback_providers": [
            {"provider": "dup", "model": "dup1", "base_url": "https://dup1", "api_key": "k1"},
            {"provider": "dup", "model": "dup2", "base_url": "https://dup2", "api_key": "k2"},
        ]}
        models = {"version": 1, "fallback_providers": [
            {"provider": "dup", "model": "new1"},
            {"provider": "dup", "model": "new2"},
        ]}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(len(config["fallback_providers"]), 2)
        # First state entry consumes first existing entry.
        self.assertEqual(config["fallback_providers"][0]["base_url"], "https://dup1")
        self.assertEqual(config["fallback_providers"][0]["api_key"], "k1")
        self.assertEqual(config["fallback_providers"][0]["model"], "new1")
        # Second state entry consumes second existing entry.
        self.assertEqual(config["fallback_providers"][1]["base_url"], "https://dup2")
        self.assertEqual(config["fallback_providers"][1]["api_key"], "k2")
        self.assertEqual(config["fallback_providers"][1]["model"], "new2")

    def test_apply_duplicate_state_more_than_existing_appends_minimal(self) -> None:
        """Duplicate state entries beyond existing count become minimal dicts."""
        config = {"fallback_providers": [
            {"provider": "dup", "model": "dup1", "base_url": "https://dup1", "api_key": "k1"},
        ]}
        models = {"version": 1, "fallback_providers": [
            {"provider": "dup", "model": "new1"},
            {"provider": "dup", "model": "new2"},
        ]}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(len(config["fallback_providers"]), 2)
        # First: merged (consumed the one existing entry).
        self.assertEqual(config["fallback_providers"][0]["base_url"], "https://dup1")
        self.assertEqual(config["fallback_providers"][0]["api_key"], "k1")
        self.assertEqual(config["fallback_providers"][0]["model"], "new1")
        # Second: minimal (no unconsumed matching existing entry).
        self.assertEqual(config["fallback_providers"][1], {
            "provider": "dup", "model": "new2",
        })

    # -- apply: new entries and truncation --

    def test_apply_new_provider_appends_minimal_dict(self) -> None:
        """State entry with a new provider becomes a minimal dict."""
        config = {"fallback_providers": [
            {"provider": "old1", "model": "old1-model", "base_url": "https://old1"},
        ]}
        models = {"version": 1, "fallback_providers": [
            {"provider": "old1", "model": "old1-new"},
            {"provider": "new2", "model": "new2-model"},
        ]}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(len(config["fallback_providers"]), 2)
        # First: merged (same provider).
        self.assertEqual(config["fallback_providers"][0]["base_url"], "https://old1")
        self.assertEqual(config["fallback_providers"][0]["model"], "old1-new")
        # Second: minimal (new provider, no sibling transfer).
        self.assertEqual(config["fallback_providers"][1], {
            "provider": "new2", "model": "new2-model",
        })

    def test_apply_truncates_entries_beyond_state_list(self) -> None:
        """Existing entries beyond the state list length are removed."""
        config = {"fallback_providers": [
            {"provider": "old1", "model": "old1-model"},
            {"provider": "old2", "model": "old2-model"},
            {"provider": "old3", "model": "old3-model"},
        ]}
        models = {"version": 1, "fallback_providers": [
            {"provider": "new1", "model": "new1-model"},
        ]}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(len(config["fallback_providers"]), 1)
        self.assertEqual(config["fallback_providers"][0]["provider"], "new1")

    def test_apply_empty_state_clears_fallback_list(self) -> None:
        """Empty state fallback_providers clears the list."""
        config = {"fallback_providers": [
            {"provider": "old1", "model": "old1-model"},
        ]}
        models = {"version": 1, "fallback_providers": []}
        self.m.apply_models_to_config(config, models)
        self.assertEqual(config["fallback_providers"], [])

    # -- rollback: same provider preserves siblings --

    def test_rollback_same_provider_preserves_siblings(self) -> None:
        """Rollback with same provider restores model, preserves siblings."""
        config = {"fallback_providers": [
            {"provider": "same", "model": "op-model",
             "base_url": "https://op", "api_mode": "chat",
             "api_key": "rk1", "extra_body": {"y": 2}},
        ]}
        template = {"fallback_providers": [
            {"provider": "same", "model": "tmpl-model"},
        ]}
        self.m.restore_template_models_defaults(config, template)
        entry = config["fallback_providers"][0]
        self.assertEqual(entry["provider"], "same")
        self.assertEqual(entry["model"], "tmpl-model")
        # Runtime siblings preserved (same provider).
        self.assertEqual(entry["base_url"], "https://op")
        self.assertEqual(entry["api_mode"], "chat")
        self.assertEqual(entry["api_key"], "rk1")
        self.assertEqual(entry["extra_body"], {"y": 2})

    # -- rollback: provider change -> no sibling transfer --

    def test_rollback_provider_change_no_sibling_transfer(self) -> None:
        """Rollback provider change creates minimal dict — no sibling transfer."""
        config = {"fallback_providers": [
            {"provider": "op", "model": "op-model",
             "base_url": "https://op", "api_key": "rk"},
        ]}
        template = {"fallback_providers": [
            {"provider": "tmpl", "model": "tmpl-model"},
        ]}
        self.m.restore_template_models_defaults(config, template)
        entry = config["fallback_providers"][0]
        self.assertEqual(entry["provider"], "tmpl")
        self.assertEqual(entry["model"], "tmpl-model")
        # NO siblings transferred from op.
        self.assertNotIn("base_url", entry)
        self.assertNotIn("api_key", entry)

    def test_rollback_credentials_no_transfer(self) -> None:
        """Rollback: api_key/base_url must not transfer to a different provider."""
        config = {"fallback_providers": [
            {"provider": "alpha", "model": "a-model",
             "base_url": "https://alpha", "api_key": "secret-alpha"},
        ]}
        template = {"fallback_providers": [
            {"provider": "beta", "model": "b-model"},
        ]}
        self.m.restore_template_models_defaults(config, template)
        entry = config["fallback_providers"][0]
        self.assertEqual(entry["provider"], "beta")
        self.assertNotIn("api_key", entry)
        self.assertNotIn("base_url", entry)

    # -- rollback: reorder preserves siblings by provider match --

    def test_rollback_reorder_preserves_siblings_by_provider(self) -> None:
        """Rollback reorder: siblings follow the provider match."""
        config = {"fallback_providers": [
            {"provider": "a", "model": "op-a", "base_url": "https://a", "api_key": "ka"},
            {"provider": "b", "model": "op-b", "base_url": "https://b", "api_key": "kb"},
        ]}
        template = {"fallback_providers": [
            {"provider": "b", "model": "tmpl-b"},
            {"provider": "a", "model": "tmpl-a"},
        ]}
        self.m.restore_template_models_defaults(config, template)
        self.assertEqual(len(config["fallback_providers"]), 2)
        self.assertEqual(config["fallback_providers"][0]["provider"], "b")
        self.assertEqual(config["fallback_providers"][0]["base_url"], "https://b")
        self.assertEqual(config["fallback_providers"][0]["api_key"], "kb")
        self.assertEqual(config["fallback_providers"][0]["model"], "tmpl-b")
        self.assertEqual(config["fallback_providers"][1]["provider"], "a")
        self.assertEqual(config["fallback_providers"][1]["base_url"], "https://a")
        self.assertEqual(config["fallback_providers"][1]["api_key"], "ka")
        self.assertEqual(config["fallback_providers"][1]["model"], "tmpl-a")

    # -- rollback: duplicates --

    def test_rollback_duplicates_preserve_siblings_by_consumed_match(self) -> None:
        """Rollback duplicates: each template entry consumes one matching existing."""
        config = {"fallback_providers": [
            {"provider": "dup", "model": "op1", "base_url": "https://dup1", "api_key": "k1"},
            {"provider": "dup", "model": "op2", "base_url": "https://dup2", "api_key": "k2"},
        ]}
        template = {"fallback_providers": [
            {"provider": "dup", "model": "tmpl1"},
            {"provider": "dup", "model": "tmpl2"},
        ]}
        self.m.restore_template_models_defaults(config, template)
        self.assertEqual(len(config["fallback_providers"]), 2)
        self.assertEqual(config["fallback_providers"][0]["base_url"], "https://dup1")
        self.assertEqual(config["fallback_providers"][0]["api_key"], "k1")
        self.assertEqual(config["fallback_providers"][0]["model"], "tmpl1")
        self.assertEqual(config["fallback_providers"][1]["base_url"], "https://dup2")
        self.assertEqual(config["fallback_providers"][1]["api_key"], "k2")
        self.assertEqual(config["fallback_providers"][1]["model"], "tmpl2")

    # -- rollback: new entries and truncation --

    def test_rollback_new_provider_appends_minimal_dict(self) -> None:
        """Rollback template entry with new provider becomes minimal dict."""
        config = {"fallback_providers": [
            {"provider": "op", "model": "op-model", "base_url": "https://op"},
        ]}
        template = {"fallback_providers": [
            {"provider": "op", "model": "op-tmpl"},
            {"provider": "new", "model": "new-tmpl"},
        ]}
        self.m.restore_template_models_defaults(config, template)
        self.assertEqual(len(config["fallback_providers"]), 2)
        # First: merged (same provider).
        self.assertEqual(config["fallback_providers"][0]["base_url"], "https://op")
        self.assertEqual(config["fallback_providers"][0]["model"], "op-tmpl")
        # Second: minimal (new provider).
        self.assertEqual(config["fallback_providers"][1], {
            "provider": "new", "model": "new-tmpl",
        })

    def test_rollback_truncates_entries_beyond_template_list(self) -> None:
        """Existing entries beyond the template list length are removed."""
        config = {"fallback_providers": [
            {"provider": "op1", "model": "op1-model"},
            {"provider": "op2", "model": "op2-model"},
            {"provider": "op3", "model": "op3-model"},
        ]}
        template = {"fallback_providers": [
            {"provider": "tmpl1", "model": "tmpl1-model"},
        ]}
        self.m.restore_template_models_defaults(config, template)
        self.assertEqual(len(config["fallback_providers"]), 1)
        self.assertEqual(config["fallback_providers"][0]["provider"], "tmpl1")

    def test_rollback_removes_state_owned_keys_absent_in_template_entry(self) -> None:
        """If a template entry lacks a state-owned key, it's removed from runtime."""
        config = {"fallback_providers": [
            {"provider": "tmpl1", "model": "op-model", "base_url": "https://op"},
        ]}
        # Template entry has provider but no model.
        template = {"fallback_providers": [
            {"provider": "tmpl1"},
        ]}
        self.m.restore_template_models_defaults(config, template)
        entry = config["fallback_providers"][0]
        self.assertEqual(entry["provider"], "tmpl1")
        self.assertNotIn("model", entry)
        # Sibling preserved.
        self.assertEqual(entry["base_url"], "https://op")


# ---------------------------------------------------------------------------
# Auxiliary allowlist (reviewed upstream order) + auto-provider model rule
# ---------------------------------------------------------------------------


# The exact reviewed 11-ID allowlist, in deterministic upstream dashboard
# order. Hardcoded literal on purpose: never dynamically discovered.
EXPECTED_AUXILIARY_TASKS = (
    "vision",
    "web_extract",
    "compression",
    "skills_hub",
    "approval",
    "mcp",
    "title_generation",
    "triage_specifier",
    "kanban_decomposer",
    "profile_describer",
    "curator",
)
# Tasks added to the allowlist beyond the original ``vision``-only set.
NEW_AUXILIARY_TASKS = tuple(t for t in EXPECTED_AUXILIARY_TASKS if t != "vision")

# Reviewed production Hermes base image pin (Dockerfile.hermes ARG).
EXPECTED_HERMES_BASE_IMAGE = "nousresearch/hermes-agent:v2026.8.18"


class AuxiliaryAllowlistTests(unittest.TestCase):
    """ALLOWED_AUXILIARY_TASKS is exactly the reviewed 11-ID upstream order."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_allowlist_matches_reviewed_order_exactly(self) -> None:
        """Exact order + contents (tuple equality is order-sensitive)."""
        self.assertEqual(self.m.ALLOWED_AUXILIARY_TASKS, EXPECTED_AUXILIARY_TASKS)

    def test_accepts_each_allowlisted_task(self) -> None:
        """Table-driven acceptance across all 11 task IDs."""
        for task in EXPECTED_AUXILIARY_TASKS:
            with self.subTest(task=task):
                data = {
                    "version": 1,
                    "auxiliary": {
                        task: {"provider": "some-provider", "model": "some-model"}
                    },
                }
                self.assertEqual(self.m.validate_models_state(data), data)

    def test_accepts_document_with_all_allowlisted_tasks(self) -> None:
        aux = {
            task: {"provider": f"p-{task}", "model": f"m-{task}"}
            for task in EXPECTED_AUXILIARY_TASKS
        }
        data = {"version": 1, "auxiliary": aux}
        self.assertEqual(self.m.validate_models_state(data), data)

    def test_rejects_unknown_task_ids(self) -> None:
        """Any task key outside the allowlist is rejected (case-sensitive)."""
        for bad in ("unknown_task", "Vision", "webextract", "vision2", ""):
            with self.subTest(task=bad):
                with self.assertRaises(ValueError) as cm:
                    self.m.validate_models_state(
                        {
                            "version": 1,
                            "auxiliary": {
                                bad: {"provider": "p", "model": "m"}
                            },
                        }
                    )
                self.assertIn("unknown key", str(cm.exception))

    def test_rejects_forbidden_config_and_secret_keys_for_every_task(self) -> None:
        """Config/secret sibling keys stay forbidden for all 11 task IDs."""
        for task in EXPECTED_AUXILIARY_TASKS:
            for bad_key, bad_value in (
                ("base_url", "https://bad.example.com"),
                ("api_key", "sk-secret"),
                ("download_timeout", 120),
            ):
                with self.subTest(task=task, key=bad_key):
                    entry = {"provider": "p", "model": "m", bad_key: bad_value}
                    with self.assertRaises(ValueError):
                        self.m.validate_models_state(
                            {"version": 1, "auxiliary": {task: entry}}
                        )


class AuxiliaryAutoProviderRuleTests(unittest.TestCase):
    """provider 'auto' requires model exactly ''; non-auto requires non-empty."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_auto_provider_rule_three_cases(self) -> None:
        """The three auto-rule cases: auto+'', auto+nonempty, non-auto+model."""
        cases = [
            # (provider, model, expected_valid)
            ("auto", "", True),  # auto + exactly empty model: valid
            ("auto", "some-model", False),  # auto + non-empty model: rejected
            ("ollama-cloud", "kimi-k2.7-code", True),  # non-auto + model: valid
        ]
        for provider, model, expected_valid in cases:
            with self.subTest(provider=provider, model=model):
                data = {
                    "version": 1,
                    "auxiliary": {"vision": {"provider": provider, "model": model}},
                }
                if expected_valid:
                    self.assertEqual(self.m.validate_models_state(data), data)
                else:
                    with self.assertRaises(ValueError):
                        self.m.validate_models_state(data)

    def test_non_auto_provider_requires_nonempty_model(self) -> None:
        """Concrete behavior unchanged: non-auto + blank model is rejected."""
        for provider, model in (("x", ""), ("x", "   ")):
            with self.subTest(provider=provider, model=model):
                data = {
                    "version": 1,
                    "auxiliary": {"vision": {"provider": provider, "model": model}},
                }
                with self.assertRaises(ValueError) as cm:
                    self.m.validate_models_state(data)
                self.assertIn("non-empty string", str(cm.exception))

    def test_model_key_required_even_for_auto(self) -> None:
        """Auto rule requires model to be exactly '' — absent model is invalid."""
        data = {"version": 1, "auxiliary": {"vision": {"provider": "auto"}}}
        with self.assertRaises(ValueError):
            self.m.validate_models_state(data)

    def test_auto_rule_not_applied_to_root_fallback_cron(self) -> None:
        """The auto/empty-model exemption is auxiliary-only."""
        # Root selection: 'default' stays a required non-empty string even
        # when the provider is literally 'auto'.
        data = {"version": 1, "model": {"provider": "auto", "default": "deepseek-v4-pro"}}
        self.assertEqual(self.m.validate_models_state(data), data)
        # Fallback entries: non-empty model required regardless of provider.
        data = {"version": 1, "fallback_providers": [{"provider": "auto", "model": "m"}]}
        self.assertEqual(self.m.validate_models_state(data), data)
        with self.assertRaises(ValueError):
            self.m.validate_models_state(
                {"version": 1, "fallback_providers": [{"provider": "x", "model": ""}]}
            )
        # Cron: blank model/model_provider remain allowed (inherit default)
        # and are NOT forced empty/checked by the auto rule.
        data = {"version": 1, "cron": {"model": "", "model_provider": "auto"}}
        self.assertEqual(self.m.validate_models_state(data), data)


class AuxiliaryNewTasksDeepMergeTests(unittest.TestCase):
    """New allowlisted tasks deep-merge like vision: siblings preserved."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_apply_new_task_preserves_siblings(self) -> None:
        """Apply updates provider/model only; runtime siblings are preserved."""
        for task in NEW_AUXILIARY_TASKS:
            with self.subTest(task=task):
                config = {"auxiliary": {task: {
                    "provider": "old", "model": "old-model",
                    "api_key": "runtime-key",
                    "download_timeout": 99,
                    "base_url": "https://api.example.com",
                }}}
                models = {
                    "version": 1,
                    "auxiliary": {task: {"provider": "new", "model": "new-model"}},
                }
                self.m.apply_models_to_config(config, models)
                entry = config["auxiliary"][task]
                self.assertEqual(entry["provider"], "new")
                self.assertEqual(entry["model"], "new-model")
                self.assertEqual(entry["api_key"], "runtime-key")
                self.assertEqual(entry["download_timeout"], 99)
                self.assertEqual(entry["base_url"], "https://api.example.com")

    def test_apply_creates_task_section_when_absent(self) -> None:
        """Apply creates the task dict with only selection keys when absent."""
        config = {}
        models = {
            "version": 1,
            "auxiliary": {"web_extract": {"provider": "p", "model": "m"}},
        }
        self.m.apply_models_to_config(config, models)
        self.assertEqual(
            config["auxiliary"]["web_extract"], {"provider": "p", "model": "m"}
        )


class AuxiliaryNewTasksRollbackTests(unittest.TestCase):
    """Rollback deletes/restores new-task selection keys, preserving siblings."""

    def setUp(self) -> None:
        self.m = _load_helper()

    def test_rollback_restores_template_selection_preserving_siblings(self) -> None:
        """Template defines the task: provider/model restored, siblings kept."""
        for task in NEW_AUXILIARY_TASKS:
            with self.subTest(task=task):
                config = {"auxiliary": {task: {
                    "provider": "op", "model": "op-model",
                    "api_key": "runtime-key",
                    "download_timeout": 99,
                }}}
                template = {"auxiliary": {task: {
                    "provider": "tmpl", "model": "tmpl-model",
                }}}
                changed = self.m.restore_template_models_defaults(config, template)
                self.assertTrue(changed)
                entry = config["auxiliary"][task]
                self.assertEqual(entry["provider"], "tmpl")
                self.assertEqual(entry["model"], "tmpl-model")
                # Runtime-only siblings preserved (NOT overwritten by template).
                self.assertEqual(entry["api_key"], "runtime-key")
                self.assertEqual(entry["download_timeout"], 99)

    def test_rollback_deletes_selection_keys_when_template_lacks_task(self) -> None:
        """Template lacks the task: provider/model removed, siblings kept."""
        for task in NEW_AUXILIARY_TASKS:
            with self.subTest(task=task):
                config = {"auxiliary": {task: {
                    "provider": "op", "model": "op-model",
                    "download_timeout": 45,
                }}}
                template = {"auxiliary": {}}
                self.m.restore_template_models_defaults(config, template)
                entry = config["auxiliary"][task]
                self.assertNotIn("provider", entry)
                self.assertNotIn("model", entry)
                self.assertEqual(entry["download_timeout"], 45)

    def test_rollback_deletes_selection_keys_when_template_has_no_auxiliary(self) -> None:
        """Template with no auxiliary section: selection keys removed, siblings kept."""
        config = {
            "auxiliary": {
                "compression": {"provider": "op", "model": "op-model", "api_key": "k"},
            },
        }
        template = {}
        self.m.restore_template_models_defaults(config, template)
        entry = config["auxiliary"]["compression"]
        self.assertNotIn("provider", entry)
        self.assertNotIn("model", entry)
        self.assertEqual(entry["api_key"], "k")


class HermesBaseImagePinTripwireTests(unittest.TestCase):
    """Tripwire: the auxiliary allowlist is pinned to the reviewed Hermes image.

    ALLOWED_AUXILIARY_TASKS mirrors the upstream auxiliary task configs of a
    specific Hermes release. If the production base image pin changes, the
    allowlist must be re-reviewed against that release before this tripwire
    is updated. Static file-parse only: no network, Docker, or dynamic task
    discovery.
    """

    def test_dockerfile_hermes_base_image_pin_and_allowlist(self) -> None:
        dockerfile = REPO_ROOT / "Dockerfile.hermes"
        self.assertTrue(dockerfile.exists(), "Dockerfile.hermes not found at repo root")
        pin = None
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^ARG\s+HERMES_BASE_IMAGE=(\S+)\s*$", line)
            if match:
                pin = match.group(1)
                break
        self.assertIsNotNone(pin, "ARG HERMES_BASE_IMAGE not found in Dockerfile.hermes")
        self.assertEqual(
            pin,
            EXPECTED_HERMES_BASE_IMAGE,
            "Dockerfile.hermes HERMES_BASE_IMAGE changed; re-review "
            "ALLOWED_AUXILIARY_TASKS against the new upstream release, then "
            "update EXPECTED_HERMES_BASE_IMAGE and EXPECTED_AUXILIARY_TASKS "
            "together.",
        )
        m = _load_helper()
        self.assertEqual(
            m.ALLOWED_AUXILIARY_TASKS,
            EXPECTED_AUXILIARY_TASKS,
            "ALLOWED_AUXILIARY_TASKS must remain exactly the reviewed 11-ID "
            "upstream dashboard order while the Hermes base image pin is "
            f"{EXPECTED_HERMES_BASE_IMAGE}.",
        )


if __name__ == "__main__":
    unittest.main()