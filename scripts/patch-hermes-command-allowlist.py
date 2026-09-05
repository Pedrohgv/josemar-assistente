#!/usr/bin/env python3
"""Patch pinned Hermes v2026.8.31 to route the runtime command allowlist
through the Josemar stateful sidecar helpers (issue #151, W2).

Pinned source identity: nousresearch/hermes-agent:v2026.8.31, commit
``29112bef099274229cadff79cdff7bf7b99c4b77``. This matches the pinned
source used by the existing Josemar patchers (patch-hermes-skills-config.py,
patch-hermes-browser-routing.py). If this commit string no longer matches the
pinned upstream source, the pin-identity tripwire assertion in
tests/skill_state/test_command_allowlist_patch.py and the fail-loud anchors
below will both fail the build.

Hermes v2026.8.31 persists the runtime command allowlist directly to
``config.yaml``. Josemar tracks only canonical JSON sidecars under
``/opt/data/hermes/command-allowlist/`` (the full ``config.yaml`` is
sensitive and deliberately untracked), so the CLI ``hermes config set/unset
command_allowlist`` and the permanent-approval ("always") save must route
through the Josemar helpers that atomically write the sidecar first and only
then the native runtime config under one advisory lock.

This build-time patch makes three exact-key/function routings:

  1. ``tools.approval.save_permanent_allowlist`` (permanent "always"
     approval save): the upstream body that wrote ``config["command_allowlist"]
     = list(patterns)`` then ``save_config(config)`` inside a swallowing
     ``try/except`` is replaced by a call to
     ``hermes_cli.josemar_skill_state.save_command_allowlist_stateful(config,
     list(patterns))``. The swallowing ``try/except`` is REMOVED so a
     state-write failure PROPAGATES (fail-loud): a user-approved permanent
     save must never silently diverge from the tracked state. ``list(patterns)``
     is passed explicitly because the helper strictly requires a REAL list.

  2. ``hermes_cli.config.set_config_value`` — ONLY for the exact key
     ``command_allowlist``, after upstream parsing/validation: the raw
     ``atomic_yaml_write`` persistence block is replaced by a branch that
     routes the (already YAML-coerced) list value to
     ``save_command_allowlist_stateful``, preserving the normal
     ``atomic_yaml_write`` path for every other key and preserving the
     upstream output/return flow.

  3. ``hermes_cli.config.unset_config_value`` — ONLY for the exact key
     ``command_allowlist``, after upstream removal validation: the persistence
     block is replaced by a branch that routes to
     ``clear_command_allowlist_stateful``, preserving the normal
     ``atomic_yaml_write`` path and the ``✓ Unset`` output for every other key.

The helper is copied next to the ``hermes_cli`` package by the Dockerfile
(``/opt/hermes/hermes_cli/josemar_skill_state.py``) and imported as
``hermes_cli.josemar_skill_state`` — the same package-relative import style
every ``hermes_cli`` sibling module uses (and that ``tools/approval.py``
already uses via ``from hermes_cli.config import cfg_get``). A bare
``from josemar_skill_state import ...`` would fail at runtime.

Deliberately NOT patched (hardline/policy logic preserved):

  - No generic wrap of ``save_config`` / ``atomic_yaml_write``: unrelated
    keys, nested keys, ``model`` shorthand redirection, managed-scope guards,
    env-shaped key routing, unknown-key notices, display.skin touches, and all
    other upstream behavior are byte-for-byte untouched.
  - ``set_config_value`` / ``unset_config_value`` intercept ONLY the exact
    root key ``command_allowlist`` (no dotted/sub-key match).
  - Upstream approval hardline/policy logic (``_command_matches_permanent_
    allowlist``, dangerous-command heuristics, tirith gating) is untouched.

Fail-fast contract (mirrors the other Josemar patchers):

  - Each ``replace_once`` raises if the expected upstream snippet is missing
    (source shape changed upstream).
  - A duplicate application raises because the first anchor is already
    replaced.
"""

from __future__ import annotations

import sys
from pathlib import Path

APPROVAL_PATH = Path("/opt/hermes/tools/approval.py")
CONFIG_PATH = Path("/opt/hermes/hermes_cli/config.py")

# Pinned upstream commit. Kept here as a named constant so the contract tests
# can assert it against the patcher (docstring pin-drift tripwire).
PINNED_UPSTREAM_SHA = "29112bef099274229cadff79cdff7bf7b99c4b77"

# The root-level exact key this patch intercepts (never a dotted/sub-key path).
ALLOWLIST_KEY = "command_allowlist"


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Expected snippet not found in {path}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def apply_patches(approval_path: Path, config_path: Path) -> None:
    # 1. tools/approval.py -> save_permanent_allowlist routes through the
    #    stateful SET helper. The upstream swallowing try/except is removed so
    #    a state-write failure PROPAGATES (fail-loud) rather than being logged
    #    away — a user-approved permanent save must not silently diverge from
    #    the tracked sidecar. list(patterns) is passed explicitly (the helper
    #    strictly requires a real list).
    replace_once(
        approval_path,
        '    try:\n'
        '        from hermes_cli.config import load_config, save_config\n'
        '        config = load_config()\n'
        '        config["command_allowlist"] = list(patterns)\n'
        '        save_config(config)\n'
        '    except Exception as e:\n'
        '        logger.warning("Could not save allowlist: %s", e)\n',
        '    from hermes_cli.config import load_config\n'
        '    from hermes_cli.josemar_skill_state import save_command_allowlist_stateful\n'
        '    config = load_config()\n'
        '    # W2 (issue #151): route through the stateful helper so the canonical\n'
        '    # JSON sidecar is written before the native runtime config. A\n'
        '    # state-write failure PROPAGATES (fail-loud) instead of being swallowed,\n'
        '    # so a user-approved permanent save never silently diverges from the\n'
        '    # tracked state.\n'
        '    save_command_allowlist_stateful(config, list(patterns))\n',
    )

    # 2. hermes_cli/config.py -> set_config_value. Intercept ONLY the exact
    #    root key ``command_allowlist`` after upstream parsing/validation, at
    #    the persistence block. The raw atomic_yaml_write becomes the else-branch
    #    so every non-allowlist key keeps the identical write path.
    replace_once(
        config_path,
        '    # Write only user config back (not the full merged defaults)\n'
        '    ensure_hermes_home()\n'
        '    from utils import atomic_yaml_write\n'
        '    atomic_yaml_write(config_path, user_config, sort_keys=False)\n',
        '    # Write only user config back (not the full merged defaults)\n'
        '    ensure_hermes_home()\n'
        '    if key == "command_allowlist":\n'
        '        from hermes_cli.josemar_skill_state import save_command_allowlist_stateful\n'
        '        save_command_allowlist_stateful(user_config, value)\n'
        '    else:\n'
        '        from utils import atomic_yaml_write\n'
        '        atomic_yaml_write(config_path, user_config, sort_keys=False)\n',
    )

    # 3. hermes_cli/config.py -> unset_config_value. Intercept ONLY the exact
    #    root key ``command_allowlist`` after upstream removal validation, at
    #    the persistence block. The raw atomic_yaml_write becomes the else-branch;
    #    the ``✓ Unset`` output is preserved for every key (incl. allowlist).
    replace_once(
        config_path,
        '    ensure_hermes_home()\n'
        '    from utils import atomic_yaml_write\n'
        '    atomic_yaml_write(config_path, user_config, sort_keys=False)\n'
        '    print(f"✓ Unset {key} from {config_path}")\n',
        '    ensure_hermes_home()\n'
        '    if key == "command_allowlist":\n'
        '        from hermes_cli.josemar_skill_state import clear_command_allowlist_stateful\n'
        '        clear_command_allowlist_stateful(user_config)\n'
        '    else:\n'
        '        from utils import atomic_yaml_write\n'
        '        atomic_yaml_write(config_path, user_config, sort_keys=False)\n'
        '    print(f"✓ Unset {key} from {config_path}")\n',
    )


def main() -> None:
    approval_path = Path(sys.argv[1]) if len(sys.argv) > 1 else APPROVAL_PATH
    config_path = Path(sys.argv[2]) if len(sys.argv) > 2 else CONFIG_PATH
    apply_patches(approval_path, config_path)
    print(
        "Patched Hermes command-allowlist runtime wiring (issue #151 W2): "
        f"{approval_path}, {config_path}"
    )


if __name__ == "__main__":
    main()
