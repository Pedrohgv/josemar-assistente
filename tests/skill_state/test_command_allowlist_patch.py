"""Contract tests for the issue #151 W2 command-allowlist runtime patch.

This suite tests ``scripts/patch-hermes-command-allowlist.py``, which routes
the pinned Hermes v2026.8.18 runtime command allowlist through the W1
stateful sidecar helpers:

  1. ``tools.approval.save_permanent_allowlist`` -> the W1 SET helper
     (``save_command_allowlist_stateful``), passing ``list(patterns)``
     explicitly and PROPAGATING a state-write failure (fail-loud) instead of
     the upstream swallowing ``try/except``.
  2. ``hermes_cli.config.set_config_value`` -> ONLY for the exact root key
     ``command_allowlist``, after upstream parsing/validation; non-allowlist
     writes/output/returns are preserved.
  3. ``hermes_cli.config.unset_config_value`` -> ONLY for the exact root key
     ``command_allowlist``, after removal validation, routed to the W1 clear
     helper.

The suite runs on ordinary ``make test`` (no Docker, no Hermes venv): it
imports the patcher in-process, applies it to anchor/mechanics fixtures that
reproduce the EXACT pinned upstream shapes (the load-bearing anchors below),
and py_compiles the patched fixtures. The real Docker image build (Dockerfile
applies the same patch to the real pinned source) is the authoritative drift
proof. The W1 helpers and their sidecar semantics are already covered by
``test_command_allowlist_state.py``.

Synthetic allowlist patterns ONLY are used (never real command content).

Transient artifacts are written only under ``dump_folder/`` and removed on
teardown.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH_SCRIPT = REPO_ROOT / "scripts" / "patch-hermes-command-allowlist.py"
DOCKERFILE = REPO_ROOT / "Dockerfile.hermes"
DUMP_DIR = REPO_ROOT / "dump_folder" / "command-allowlist-patch-contract"

# Pinned upstream commit (must match the patcher's named constant + docstring).
PINNED_SHA = "e624e9fde561e1add9388384012b295fde669ade"


def load_patch_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "patch_hermes_command_allowlist", PATCH_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Anchor/mechanics fixtures reproducing the EXACT pinned upstream shapes.
# These are MECHANICS fixtures, NOT claims of complete upstream fidelity —
# each contains exactly the replace_once anchors the patch expects (in
# upstream order) plus the minimal surrounding code to stay valid Python for
# py_compile. The Docker image build applies the same patch to the real
# pinned source and is the authoritative drift proof.
# ---------------------------------------------------------------------------

# tools/approval.py: exactly the save_permanent_allowlist anchor (with the
# surrounding load_permanent_allowlist so the module is coherent and the
# ``_permanent_approved``/``logger`` names referenced exist).
APPROVAL_SKELETON_SOURCE = (
    "import logging\n"
    "from typing import Optional\n"
    "\n"
    "logger = logging.getLogger(\"skeleton\")\n"
    "\n"
    "_permanent_approved: set = set()\n"
    "\n"
    "def approve_permanent(pattern: str) -> None:\n"
    "    _permanent_approved.add(pattern)\n"
    "\n"
    "\n"
    "def load_permanent_allowlist() -> set:\n"
    "    try:\n"
    "        from hermes_cli.config import load_config_readonly\n"
    "        config = load_config_readonly()\n"
    "        patterns = set(config.get(\"command_allowlist\", []) or [])\n"
    "        if patterns:\n"
    "            load_permanent(patterns)\n"
    "        return patterns\n"
    "    except Exception as e:\n"
    "        logger.warning(\"Failed to load permanent allowlist: %s\", e)\n"
    "        return set()\n"
    "\n"
    "def load_permanent(patterns) -> None:\n"
    "    _permanent_approved.update(patterns)\n"
    "\n"
    "\n"
    "def save_permanent_allowlist(patterns: set):\n"
    '    """Save permanently allowed command patterns to config."""\n'
    "    try:\n"
    "        from hermes_cli.config import load_config, save_config\n"
    "        config = load_config()\n"
    '        config["command_allowlist"] = list(patterns)\n'
    "        save_config(config)\n"
    "    except Exception as e:\n"
    '        logger.warning("Could not save allowlist: %s", e)\n'
)


# hermes_cli/config.py: exactly the set_config_value persistence anchor (the
# ``atomic_yaml_write`` block preceded by the "Write only user config back"
# comment) and the unset_config_value persistence anchor (the block followed
# by the ``✓ Unset`` print). Surrounding function defs keep it coherent.
CONFIG_SKELETON_SOURCE = (
    "from typing import Optional\n"
    "\n"
    "def _set_nested(user_config, key, value) -> None:\n"
    "    user_config[key] = value\n"
    "\n"
    "\n"
    "def _unset_nested(user_config, key) -> bool:\n"
    "    return user_config.pop(key, None) is not None\n"
    "\n"
    "\n"
    "def set_config_value(key: str, value: str, force: bool = False):\n"
    "    config_path = \"/tmp/config.yaml\"\n"
    "    ensure_hermes_home()\n"
    "    user_config = {}\n"
    "    _set_nested(user_config, key, value)\n"
    "    # Write only user config back (not the full merged defaults)\n"
    "    ensure_hermes_home()\n"
    "    from utils import atomic_yaml_write\n"
    "    atomic_yaml_write(config_path, user_config, sort_keys=False)\n"
    '    print(f"✓ Set {key} = {value}")\n'
    "\n"
    "\n"
    "def unset_config_value(key: str):\n"
    "    config_path = \"/tmp/config.yaml\"\n"
    "    ensure_hermes_home()\n"
    "    user_config = {\"command_allowlist\": [\"stale\"]}\n"
    "    removed = _unset_nested(user_config, key)\n"
    "    if not removed:\n"
    '        print(f"Config key not set: {key}")\n'
    "        return\n"
    "    ensure_hermes_home()\n"
    "    from utils import atomic_yaml_write\n"
    "    atomic_yaml_write(config_path, user_config, sort_keys=False)\n"
    '    print(f"✓ Unset {key} from {config_path}")\n'
    "\n"
    "\n"
    "def ensure_hermes_home() -> None:\n"
    "    return None\n"
)


# Minimal utils stub (the config skeleton imports atomic_yaml_write).
UTILS_SOURCE = (
    "def atomic_yaml_write(path, data, sort_keys=True) -> None:\n"
    "    return None\n"
)


class PatchSourceContractTests(unittest.TestCase):
    """scripts/patch-hermes-command-allowlist.py source shape."""

    def setUp(self) -> None:
        self.text = PATCH_SCRIPT.read_text(encoding="utf-8")

    def test_patch_importable_and_exposes_apply_patches(self) -> None:
        module = load_patch_module()
        self.assertTrue(callable(module.apply_patches))
        self.assertTrue(callable(module.replace_once))
        self.assertEqual(module.APPROVAL_PATH.name, "approval.py")
        self.assertEqual(module.CONFIG_PATH.name, "config.py")
        self.assertEqual(module.PINNED_UPSTREAM_SHA, PINNED_SHA)
        self.assertEqual(module.ALLOWLIST_KEY, "command_allowlist")

    def test_patch_uses_fail_loud_replace_once(self) -> None:
        self.assertIn("def replace_once", self.text)
        self.assertIn("raise RuntimeError", self.text)
        self.assertIn("Expected snippet not found", self.text)

    def test_patch_docstring_pins_upstream_sha(self) -> None:
        self.assertIn(PINNED_SHA, self.text)
        self.assertIn("e624e9fde561e1add9388384012b295fde669ade", self.text)

    def test_patch_uses_package_relative_import(self) -> None:
        # The helper must be imported as ``hermes_cli.josemar_skill_state``.
        self.assertIn("from hermes_cli.josemar_skill_state import", self.text)
        # A bare top-level import must NOT appear in the code (the docstring
        # legitimately narrates why the bare form would fail; scope the absence
        # assertion to the code after the docstring, like the browser test).
        code = self.text.split('"""', 2)[2]
        self.assertNotIn("from josemar_skill_state import", code)

    def test_patch_routes_through_w1_helpers(self) -> None:
        self.assertIn("save_command_allowlist_stateful(config, list(patterns))", self.text)
        self.assertIn("save_command_allowlist_stateful(user_config, value)", self.text)
        self.assertIn("clear_command_allowlist_stateful(user_config)", self.text)

    def test_patch_uses_exact_root_key_only(self) -> None:
        # set/unset route ONLY on the exact root key ``command_allowlist``,
        # never a dotted/sub-key path.
        self.assertIn('if key == "command_allowlist":', self.text)
        # No generic save_config wrapper anywhere in the patch.
        self.assertNotIn("def _wrap_save_config", self.text)

    def test_patch_swallowing_try_except_removed(self) -> None:
        # save_permanent_allowlist must NOT keep the upstream swallowing
        # try/except (a state-write failure must PROPAGATE, fail-loud).
        self.assertIn("save_command_allowlist_stateful(config, list(patterns))", self.text)
        self.assertIn("PROPAGATES (fail-loud)", self.text)

    def test_patch_preserves_non_allowlist_write(self) -> None:
        # The else-branch keeps the exact upstream atomic_yaml_write for every
        # non-allowlist key in BOTH set and unset.
        self.assertIn(
            "atomic_yaml_write(config_path, user_config, sort_keys=False)",
            self.text,
        )


class PatchApplyFunctionalTests(unittest.TestCase):
    """Apply the real patch to the anchor fixtures and prove the fail-loud
    contract and exact-key routing."""

    @classmethod
    def setUpClass(cls) -> None:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="allowlist-", dir=DUMP_DIR))
        cls.module = load_patch_module()
        cls.approval_path = cls.tmpdir / "approval.py"
        cls.config_path = cls.tmpdir / "config.py"
        (cls.tmpdir / "utils.py").write_text(UTILS_SOURCE, encoding="utf-8")
        cls.approval_path.write_text(APPROVAL_SKELETON_SOURCE, encoding="utf-8")
        cls.config_path.write_text(CONFIG_SKELETON_SOURCE, encoding="utf-8")
        cls.module.apply_patches(cls.approval_path, cls.config_path)
        cls.patched_approval = cls.approval_path.read_text(encoding="utf-8")
        cls.patched_config = cls.config_path.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(DUMP_DIR, ignore_errors=True)

    def test_patch_applies_to_pinned_shape(self) -> None:
        self.assertNotEqual(self.patched_approval, APPROVAL_SKELETON_SOURCE)
        self.assertNotEqual(self.patched_config, CONFIG_SKELETON_SOURCE)

    def test_approval_routed_through_stateful_helper(self) -> None:
        self.assertIn(
            "from hermes_cli.josemar_skill_state import save_command_allowlist_stateful",
            self.patched_approval,
        )
        self.assertIn(
            "save_command_allowlist_stateful(config, list(patterns))",
            self.patched_approval,
        )
        # The swallowing try/except is gone: a state-write failure propagates.
        self.assertNotIn('logger.warning("Could not save allowlist: %s", e)', self.patched_approval)

    def test_set_config_routed_exact_key_only(self) -> None:
        self.assertIn(
            'if key == "command_allowlist":',
            self.patched_config,
        )
        self.assertIn(
            "from hermes_cli.josemar_skill_state import save_command_allowlist_stateful",
            self.patched_config,
        )
        self.assertIn(
            "save_command_allowlist_stateful(user_config, value)",
            self.patched_config,
        )
        # Non-allowlist keys keep the raw atomic write.
        self.assertIn(
            "atomic_yaml_write(config_path, user_config, sort_keys=False)",
            self.patched_config,
        )

    def test_unset_config_routed_exact_key_only(self) -> None:
        self.assertIn(
            "from hermes_cli.josemar_skill_state import clear_command_allowlist_stateful",
            self.patched_config,
        )
        self.assertIn(
            "clear_command_allowlist_stateful(user_config)",
            self.patched_config,
        )
        # The ✓ Unset output is preserved.
        self.assertIn('print(f"✓ Unset {key} from {config_path}")', self.patched_config)

    def test_patched_fixtures_compile(self) -> None:
        py_compile.compile(str(self.approval_path), doraise=True)
        py_compile.compile(str(self.config_path), doraise=True)
        py_compile.compile(str(self.tmpdir / "utils.py"), doraise=True)

    def test_second_apply_fails_loudly(self) -> None:
        with self.assertRaises(RuntimeError):
            self.module.apply_patches(self.approval_path, self.config_path)

    def test_missing_anchor_fails_loudly(self) -> None:
        broken = self.tmpdir / "broken_approval.py"
        broken.write_text(
            APPROVAL_SKELETON_SOURCE.replace(
                'config["command_allowlist"] = list(patterns)',
                'config["command_allowlist_x"] = list(patterns)',
            ),
            encoding="utf-8",
        )
        # Apply on a fresh config skeleton (anchors intact there).
        fresh_config = self.tmpdir / "fresh_config.py"
        fresh_config.write_text(CONFIG_SKELETON_SOURCE, encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            self.module.apply_patches(broken, fresh_config)
        self.assertIn("Expected snippet not found", str(ctx.exception))

    def test_no_generic_hooks(self) -> None:
        # The patcher must never generically wrap save_config or atomic_yaml_write.
        # The only atomic_yaml_write calls are inside the else-branch (exact-key).
        text = self.patched_config
        # The helper import must be the package-relative hermes_cli path.
        self.assertNotIn("from josemar_skill_state import", text)
        self.assertIn("hermes_cli.josemar_skill_state", text)


class DockerfileContractTests(unittest.TestCase):
    """Dockerfile.hermes: helper baked before patch, fail-loud py_compile block."""

    def setUp(self) -> None:
        self.src = DOCKERFILE.read_text(encoding="utf-8")

    def test_helper_baked_before_patch(self) -> None:
        helper_pos = self.src.find(
            "COPY scripts/josemar_skill_state.py /opt/hermes/hermes_cli/josemar_skill_state.py"
        )
        patch_pos = self.src.find("COPY scripts/patch-hermes-command-allowlist.py")
        self.assertNotEqual(helper_pos, -1, "helper COPY missing")
        self.assertNotEqual(patch_pos, -1, "W2 patch COPY missing")
        self.assertLess(helper_pos, patch_pos, "the helper must be baked before the W2 patch")

    def test_patch_block_fail_loud_py_compiles_three_modules(self) -> None:
        self.assertIn(
            "COPY scripts/patch-hermes-command-allowlist.py /tmp/patch-hermes-command-allowlist.py",
            self.src,
        )
        self.assertIn(
            "RUN python3 /tmp/patch-hermes-command-allowlist.py \\\n"
            "    && /opt/hermes/.venv/bin/python3 -m py_compile \\\n"
            "        /opt/hermes/tools/approval.py \\\n"
            "        /opt/hermes/hermes_cli/config.py \\\n"
            "        /opt/hermes/hermes_cli/josemar_skill_state.py \\\n"
            "    && rm /tmp/patch-hermes-command-allowlist.py",
            self.src,
        )

    def test_existing_patch_blocks_not_disturbed(self) -> None:
        # The W2 block must not remove or disturb the existing skills-config
        # and browser-routing patch blocks.
        self.assertIn("patch-hermes-skills-config.py", self.src)
        self.assertIn("patch-hermes-browser-routing.py", self.src)


class PiiAllowlistContractTests(unittest.TestCase):
    """The pinned SHA numeric run in the new patcher is file-scoped in
    .pii-allowlist (provenance, not a phone number)."""

    def test_pii_allowlist_scopes_new_patcher(self) -> None:
        allowlist = (REPO_ROOT / ".pii-allowlist").read_text(encoding="utf-8")
        self.assertIn("scripts/patch-hermes-command-allowlist", allowlist)
        self.assertIn("e624e9fde561e1add9388[3]84012b295fde669ade", allowlist)


if __name__ == "__main__":
    unittest.main()
