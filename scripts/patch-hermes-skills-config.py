#!/usr/bin/env python3
"""Patch Hermes ``skills_config.save_disabled_skills`` for Josemar deployments.

Hermes v2026.8.31 persists skill toggles directly to ``config.yaml`` via
``save_config``. Josemar tracks only canonical JSON sidecars under
``/opt/data/hermes/skill-toggles/`` (the full ``config.yaml`` is sensitive
and deliberately untracked), so the dashboard ``PUT /api/skills/toggle``
and the CLI ``hermes skills`` flow must route through a Josemar helper
that atomically writes the sidecar first and then invokes native
``save_config`` under one advisory lock.

This build-time patch replaces the final ``save_config(config)`` call in
``save_disabled_skills`` with a call to
``hermes_cli.josemar_skill_state.save_disabled_skills_stateful``. The
helper is copied next to ``skills_config.py`` by the Dockerfile
(``/opt/hermes/hermes_cli/josemar_skill_state.py``) and is imported as a
sibling of the ``hermes_cli`` package — the same package-relative import
style every other ``hermes_cli`` module uses (e.g.
``from hermes_cli.config import cfg_get, load_config, save_config`` at
the top of ``skills_config.py``). A bare ``from josemar_skill_state
import ...`` would fail at runtime because ``josemar_skill_state`` is
not a top-level module on ``sys.path``; only the ``hermes_cli`` package
is.

Fail-fast contract (mirrors ``patch-hermes-dashboard-profile-name.py``):

  - Each ``replace_once`` raises if the expected snippet is missing
    (source shape changed upstream).
  - A duplicate application raises because the second call finds the
    original snippet already replaced.
"""

from __future__ import annotations

from pathlib import Path


SKILLS_CONFIG_PATH = Path("/opt/hermes/hermes_cli/skills_config.py")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Expected snippet not found in {path}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# Replace the final ``save_config(config)`` call inside
# ``save_disabled_skills`` with the Josemar stateful helper. The upstream
# function ends with exactly this line after mutating
# ``config["skills"]["disabled"]`` / ``platform_disabled[platform]``.
# The helper is imported as ``hermes_cli.josemar_skill_state`` because
# ``skills_config.py`` is part of the ``hermes_cli`` package and its
# siblings are imported package-relative (e.g.
# ``from hermes_cli.config import save_config``).
replace_once(
    SKILLS_CONFIG_PATH,
    '        config["skills"]["platform_disabled"][platform] = sorted(disabled)\n'
    '    save_config(config)\n',
    '        config["skills"]["platform_disabled"][platform] = sorted(disabled)\n'
    '    from hermes_cli.josemar_skill_state import save_disabled_skills_stateful\n'
    '    save_disabled_skills_stateful(config, disabled, platform)\n',
)

print("Patched Hermes skills_config.save_disabled_skills to use Josemar stateful helper")