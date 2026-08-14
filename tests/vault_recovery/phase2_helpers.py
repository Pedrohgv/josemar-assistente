"""Shared fixtures for vault-recovery Phase-2 unit tests (no Docker).

Reuses the real exporter core (scripts/vault_recovery_core.py) to produce
genuine READY generations with the Phase-2 entries index, plus a fake rclone
on PATH for the shell uploader/recover scripts.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
UPLOADER_SCRIPT = SCRIPTS_DIR / "vault-recovery-uploader.sh"
RECOVER_SCRIPT = SCRIPTS_DIR / "vault-recovery-recover.sh"
FAKE_RCLONE_MODULE = Path(__file__).resolve().parent / "fake_rclone.py"


def import_core() -> Any:
    core_path = SCRIPTS_DIR / "vault_recovery_core.py"
    spec = importlib.util.spec_from_file_location("vault_recovery_core", str(core_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def import_restore_core() -> Any:
    """Load the restore core with the exporter core registered under its
    canonical module name (the restore core imports it)."""
    core = import_core()
    sys.modules["vault_recovery_core"] = core
    restore_path = SCRIPTS_DIR / "vault_recovery_restore_core.py"
    spec = importlib.util.spec_from_file_location(
        "vault_recovery_restore_core", str(restore_path)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_tree(root: Path, spec: dict) -> None:
    """Build a tree from a {relpath: content|None(dir)|("mode", content)} spec."""
    for rel, value in spec.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if value is None:
            path.mkdir(parents=True, exist_ok=True)
            continue
        mode, content = value if isinstance(value, tuple) else (0o644, value)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)


def fake_gbrain_bin(doctor_json: dict, exit_code: int = 0) -> str:
    """Executable fake gbrain printing ``doctor_json``; also dumps its whole
    env to ``<bin>.env.json`` (plus ``__CWD__`` = the working directory it
    ran in, and ``__CWD_REAL__`` = its realpath) so tests can assert
    GBRAIN_HOME/GBRAIN_BRAIN_REPO/HOME/cwd all point at disposable paths
    only."""
    script = (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open(sys.argv[0] + '.env.json', 'w') as fh:\n"
        f"    dump = dict(os.environ)\n"
        f"    dump['__CWD__'] = os.getcwd()\n"
        f"    dump['__CWD_REAL__'] = os.path.realpath(os.getcwd())\n"
        f"    json.dump(dump, fh, sort_keys=True)\n"
        f"print(json.dumps({json.dumps(doctor_json)}))\n"
        f"sys.exit({exit_code})\n"
    )
    path = Path(tempfile.mkdtemp(prefix="vr-fake-gbrain-")) / "gbrain-native"
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)
    return str(path)


def doctor_ok(**overrides) -> dict:
    report = {
        "schema_version": 2,
        "status": "healthy",
        "checks": [
            {"name": "connection", "status": "ok", "message": "Connected"},
            {"name": "jsonb_integrity", "status": "ok", "message": "ok"},
            {"name": "schema_version", "status": "ok", "message": "ok"},
            {"name": "pgvector", "status": "ok", "message": "Extension installed"},
        ],
    }
    report.update(overrides)
    return report


class LockContext:
    """Hold a real exclusive flock on a temp lock file and export the fd."""

    def __init__(self, lock_path: Path, shared: bool = False) -> None:
        self.lock_path = lock_path
        self.shared = shared
        self.fd = None
        self._old_env = None

    def __enter__(self) -> "LockContext":
        self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX)
        self._old_env = os.environ.get("TASKNOTES_LOCK_FD")
        os.environ["TASKNOTES_LOCK_FD"] = str(self.fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._old_env is None:
            os.environ.pop("TASKNOTES_LOCK_FD", None)
        else:
            os.environ["TASKNOTES_LOCK_FD"] = self._old_env
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


def make_generation(tmp: Path) -> tuple[str, Path]:
    """Run the REAL exporter (lock held, fake doctor) and return (gen_id,
    staging_dir). The generation carries the Phase-2 entries index."""
    core = import_core()
    gbrain_dir = tmp / "source" / ".gbrain"
    vault_dir = tmp / "source" / "obsidian"
    write_tree(
        gbrain_dir,
        {
            "config.json": (0o600, '{"search": {"mcp_keyword_only": true}}\n'),
            "base/PG_VERSION": (0o644, "16\n"),
            "empty-dir": None,
        },
    )
    write_tree(
        vault_dir,
        {
            "notes/hello.md": (0o644, "# Hello\nmarker-1\n"),
            "notes/deep/nested.md": (0o600, "nested\n"),
            "empty/": None,
        },
    )
    staging = tmp / "staging"
    lock_path = tmp / "tasknotes.lock"
    with LockContext(lock_path):
        manifest = core.export_generation(
            gbrain_dir, vault_dir, staging,
            gbrain_bin=fake_gbrain_bin(doctor_ok()), lock_path=str(lock_path),
        )
    return manifest["generation_id"], staging


class FakeRcloneFixture:
    """A fake `rclone` on PATH backed by a local directory "remote"."""

    def __init__(self, tmp: Path, config: Optional[dict] = None) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        link = self.bin / "rclone"
        link.symlink_to(FAKE_RCLONE_MODULE)
        os.chmod(FAKE_RCLONE_MODULE, 0o755)
        self.base = tmp / "remote-base"
        self.log = tmp / "rclone.log"
        self.config_file = tmp / "rclone.conf"
        self._write_config(config or {
            "vault-crypt": {
                "type": "crypt",
                "remote": "local:/underlying",
                "password": "obfuscated",
            }
        })
        self.extra_env: Dict[str, str] = {}

    def _write_config(self, config: dict) -> None:
        lines = []
        for name, kv in config.items():
            lines.append(f"[{name}]")
            for key, value in kv.items():
                lines.append(f"{key} = {value}")
            lines.append("")
        self.config_file.write_text("\n".join(lines), encoding="utf-8")

    def env(self, **overrides) -> Dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        env["RCLONE_CONFIG"] = str(self.config_file)
        env["FAKE_RCLONE_BASE"] = str(self.base)
        env["FAKE_RCLONE_LOG"] = str(self.log)
        env["FAKE_RCLONE_CONFIG"] = str(self.config_file)
        env.update(self.extra_env)
        env.update(overrides)
        return env

    def run(self, script: Path, args: Optional[List[str]] = None, **env_overrides) -> subprocess.CompletedProcess[str]:
        self.log.unlink(missing_ok=True)
        return subprocess.run(
            ["/bin/sh", str(script), *(args or [])],
            env=self.env(**env_overrides),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def log_entries(self) -> List[Dict[str, Any]]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text("utf-8").splitlines()
            if line.strip()
        ]

    def log_commands(self) -> List[str]:
        return [e["cmd"] for e in self.log_entries()]

    def remote_dir(self, *parts: str) -> Path:
        return self.base.joinpath("vault-crypt", *parts)


def uploader_env_for(fixture: FakeRcloneFixture, staging: Path, state: Path, **over) -> Dict[str, str]:
    defaults = {
        "VAULT_RECOVERY_UPLOADER_STAGING_DIR": str(staging),
        "VAULT_RECOVERY_UPLOADER_STATE_DIR": str(state),
        "VAULT_RECOVERY_RCLONE_REMOTE": "vault-crypt",
        "VAULT_RECOVERY_RCLONE_PATH": "Josemar/vault-recovery",
        "VAULT_RECOVERY_RETENTION": "14",
        "VAULT_RECOVERY_ONCE": "true",
    }
    defaults.update(over)  # allow tests to override (e.g. blank the remote)
    return fixture.env(**defaults)


def recover_env_for(fixture: FakeRcloneFixture, recovery: Path, **over) -> Dict[str, str]:
    defaults = {
        "VAULT_RECOVERY_RECOVERY_DIR": str(recovery),
        "VAULT_RECOVERY_RCLONE_REMOTE": "vault-crypt",
        "VAULT_RECOVERY_RCLONE_PATH": "Josemar/vault-recovery",
    }
    defaults.update(over)
    return fixture.env(**defaults)


def seed_remote_committed(
    fixture: FakeRcloneFixture, gen_id: str, staging: Path
) -> Path:
    """Copy a staged generation into the fake remote's COMMITTED namespace
    (what the uploader would have produced), returning the remote dir."""
    committed = fixture.remote_dir("Josemar", "vault-recovery", "committed")
    target = committed / gen_id
    shutil.copytree(staging / gen_id, target)
    return target


def seed_remote_committed_id(
    fixture: FakeRcloneFixture, staging: Path, src_gen_id: str, new_gen_id: str
) -> Path:
    """Copy a staged generation into the fake remote's COMMITTED namespace
    under a NEW generation id, rewriting READY content and the manifest
    generation_id so the READY marker binds to the new name (the READY
    protocol requires marker == dir name == manifest generation_id)."""
    committed = fixture.remote_dir("Josemar", "vault-recovery", "committed")
    target = committed / new_gen_id
    shutil.copytree(staging / src_gen_id, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["generation_id"] = new_gen_id
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (target / "READY").write_text(f"{new_gen_id}\n", encoding="utf-8")
    return target


def make_recovery_handoff(tmp: Path, staging: Path, gen_id: str) -> tuple[Path, str]:
    """Build a RECOVERY_READY handoff from a staged generation: copy the
    bundle into ``tmp/recovery/<gen_id>`` and write the two-line sentinel
    (generation id + manifest sha256) exactly like the recover step does."""
    recovery = tmp / "recovery"
    bundle = recovery / gen_id
    shutil.copytree(staging / gen_id, bundle)
    manifest_sha = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
    (recovery / "RECOVERY_READY").write_text(f"{gen_id}\n{manifest_sha}\n", encoding="utf-8")
    return recovery, manifest_sha
