from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_ROOT = REPO_ROOT / "skills-factory" / "vault-gateway"
GATEWAY_EXECUTABLE = GATEWAY_ROOT / "vault-gateway"


def assert_test_vault_path(path: Path) -> None:
    resolved = path.resolve(strict=False)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    if resolved != temp_root and temp_root not in resolved.parents:
        raise AssertionError(f"Refusing to use non-temporary test vault path: {resolved}")


class FakeVault:
    def __init__(self) -> None:
        self._tmp_dir = tempfile.mkdtemp(prefix="vault-gateway-tests-")
        self.root = Path(self._tmp_dir)
        self.workspace_dir = self.root / "workspace"
        self.vault_dir = self.root / "vault"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        assert_test_vault_path(self.vault_dir)

        self.env = os.environ.copy()
        self.env.update(
            {
                "WORKSPACE_DIR": str(self.workspace_dir),
                "OBSIDIAN_VAULT_DIR": str(self.vault_dir),
                "VAULT_GATEWAY_ALLOWED_ROOTS": str(self.vault_dir),
            }
        )

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def write_note(self, relative_path: str, content: str) -> Path:
        note_path = self.vault_dir / relative_path
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(content, encoding="utf-8")
        return note_path

    def run_gateway(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return run_gateway(payload, self.env)


def run_gateway(payload: dict[str, Any], env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    try:
        process = subprocess.run(
            [str(GATEWAY_EXECUTABLE)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"Gateway timed out for payload route={payload.get('route')!r}"
        ) from exc

    try:
        data = json.loads(process.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Gateway output is not valid JSON. stdout={process.stdout!r}, stderr={process.stderr!r}"
        ) from exc

    return process.returncode, data
