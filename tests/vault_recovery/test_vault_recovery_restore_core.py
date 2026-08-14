"""Unit/contract tests for the Phase-2 restore core
(scripts/vault_recovery_restore_core.py): verify (disposable doctor),
journaled two-tree install (atomic + per-entry mount fallback), automatic
rollback on failure, and operator rollback.

Uses the REAL exporter to build a genuine staged generation, then builds a
RECOVERY_READY handoff exactly like the recover step, runs the REAL
verify_recovery against a fake pinned doctor binary, and installs/rolls back
into disposable live trees. The EBUSY mount-root case (production vault at
/opt/data/obsidian) is simulated by intercepting rename(2) for the live
vault root - the per-entry journaled swap must take over and still be fully
reversible.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from .phase2_helpers import (
        LockContext,
        doctor_ok,
        fake_gbrain_bin,
        import_core,
        import_restore_core,
        make_generation,
        make_recovery_handoff,
        write_tree,
    )
except ImportError:  # discover -s tests/vault_recovery imports top-level
    from phase2_helpers import (  # type: ignore
        LockContext,
        doctor_ok,
        fake_gbrain_bin,
        import_core,
        import_restore_core,
        make_generation,
        make_recovery_handoff,
        write_tree,
    )


class RestoreCoreBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vr-restore-core-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gen_id, self.staging = make_generation(self.tmp)
        self.core = import_restore_core()
        self.fake_bin = fake_gbrain_bin(doctor_ok())
        # Live trees: deliberately different content from the bundle, so any
        # successful install/rollback is observable.
        self.live_vault = self.tmp / "live" / "obsidian"
        self.live_gbrain = self.tmp / "live" / ".gbrain"
        self.live_vault.mkdir(parents=True)
        self.live_gbrain.mkdir(parents=True)
        # Overlapping entries: the bundle also carries notes/hello.md and
        # .gbrain/config.json, so the per-entry swap must move live entries
        # aside and staged twins in (not just add/remove).
        (self.live_vault / "notes").mkdir()
        (self.live_vault / "notes" / "hello.md").write_text(
            "OLD hello\n", encoding="utf-8"
        )
        (self.live_vault / "old-note.md").write_text("old vault content\n", encoding="utf-8")
        (self.live_gbrain / "config.json").write_text('{"old": true}\n', encoding="utf-8")
        (self.live_gbrain / "old-state").write_text("old gbrain content\n", encoding="utf-8")
        self.journal_root = self.tmp / "install-journal"
        self.lock_path = self.tmp / "locks" / "tasknotes.lock"
        self.lock_path.parent.mkdir(parents=True)
        self.bundle = self.staging / self.gen_id

    def _handoff(self) -> tuple[Path, str]:
        return make_recovery_handoff(self.tmp, self.staging, self.gen_id)

    def _handoff_gen(self, recovery: Path) -> str:
        """The generation id a RECOVERY_READY handoff carries (first line)."""
        return (recovery / "RECOVERY_READY").read_text("utf-8").splitlines()[0].strip()

    def _verified_handoff(self) -> Path:
        """Run the REAL verify step (fake pinned doctor) to produce
        VERIFIED_READY, mirroring the operator flow."""
        recovery, _ = self._handoff()
        self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertTrue((recovery / "VERIFIED_READY").exists())
        return recovery

    def _install(self, recovery: Path, **over) -> dict:
        return self.core.install_generation(
            recovery,
            self.live_vault,
            self.live_gbrain,
            confirm=True,
            journal_root=self.journal_root,
            lock_path=str(self.lock_path),
            **over,
        )

    def _live_files(self, root: Path) -> dict:
        return {
            rel: (root / rel).read_text("utf-8")
            for rel in sorted(
                p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
            )
        }


class VerifyRecoveryTests(RestoreCoreBase):
    def test_verify_missing_handoff_refused(self) -> None:
        with self.assertRaises(self.core.HandoffError):
            self.core.verify_recovery(self.tmp / "nope", lock_path=str(self.lock_path))

    def test_verify_tampered_bundle_refused(self) -> None:
        recovery, _ = self._handoff()
        (recovery / self.gen_id / "vault" / "notes" / "hello.md").write_text(
            "TAMPERED\n", encoding="utf-8"
        )
        with self.assertRaises(self.core.ValidationError):
            self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_verify_doctor_failure_refused(self) -> None:
        recovery, _ = self._handoff()
        failing = fake_gbrain_bin(doctor_ok(checks=[]), exit_code=0)
        with self.assertRaises(self.core.core.DoctorPreflightError):
            self.core.verify_recovery(recovery, gbrain_bin=failing, lock_path=str(self.lock_path))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_verify_writes_verified_ready_and_doctor_runs_on_disposable_copy(self) -> None:
        recovery, manifest_sha = self._handoff()
        result = self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertEqual(result["generation_id"], self.gen_id)
        self.assertEqual(result["manifest_sha256"], manifest_sha)
        self.assertEqual(result["trees"][".gbrain"]["exact_match"], True)
        self.assertEqual(result["trees"]["vault"]["exact_match"], True)
        verified = (recovery / "VERIFIED_READY").read_text("utf-8").splitlines()
        self.assertEqual(verified[0], self.gen_id)
        self.assertEqual(verified[1], manifest_sha)
        # The fake doctor dumped its whole env; it must point ONLY at
        # disposable paths (never the live trees).
        env = json.loads(Path(self.fake_bin + ".env.json").read_text("utf-8"))
        self.assertEqual(env["GBRAIN_HOME"], str(recovery / f".verify-{self.gen_id}"))
        self.assertEqual(env["GBRAIN_BRAIN_REPO"], str(recovery / self.gen_id / "vault"))
        self.assertNotIn("live", env["GBRAIN_HOME"])
        self.assertNotIn("live", env["GBRAIN_BRAIN_REPO"])

    def test_verify_containment_rewrites_absolute_paths_in_disposable_config(self) -> None:
        """Regression (DR drill): the exported config.json carries the LIVE
        absolute database_path/sync.repo_path. The pinned doctor merges the
        config FILE into the engine config, so without containment it would
        open (and re-create, if destroyed) the LIVE PGLite instead of the
        disposable copy. verify_recovery must rewrite the disposable config
        so both paths resolve inside the handoff."""
        core = import_core()
        gbrain_dir = self.tmp / "src3" / ".gbrain"
        vault_dir = self.tmp / "src3" / "obsidian"
        live_database_path = "/opt/data/.gbrain/brain.pglite"
        live_repo_path = "/opt/data/obsidian"
        write_tree(
            gbrain_dir,
            {
                "config.json": (
                    0o600,
                    json.dumps(
                        {
                            "engine": "pglite",
                            "database_path": live_database_path,
                            "sync": {"repo_path": live_repo_path},
                            "search": {"mcp_keyword_only": True},
                        }
                    )
                    + "\n",
                ),
                "brain.pglite/PG_VERSION": (0o644, "16\n"),
            },
        )
        write_tree(vault_dir, {"note.md": (0o644, "# Vault note\nMARKER\n")})
        staging = self.tmp / "staging3"
        lock_path = self.tmp / "locks3" / "tasknotes.lock"
        lock_path.parent.mkdir(parents=True)
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_gbrain_bin(doctor_ok()), lock_path=str(lock_path),
            )
        gen_id = manifest["generation_id"]
        recovery, _ = make_recovery_handoff(self.tmp, staging, gen_id)
        # Capture the disposable config AT doctor-run time (the disposable
        # copy is removed when verify_recovery finishes).
        seen: dict = {}
        real_doctor = self.core._run_doctor_at

        def capturing_doctor(gbrain_bin, home_root, brain_repo, schema_pack, timeout):
            cfg_path = Path(home_root) / ".gbrain" / "config.json"
            if cfg_path.exists():
                seen["config"] = json.loads(cfg_path.read_text("utf-8"))
            return real_doctor(gbrain_bin, home_root, brain_repo, schema_pack, timeout)

        with mock.patch.object(self.core, "_run_doctor_at", side_effect=capturing_doctor):
            self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertTrue((recovery / "VERIFIED_READY").exists())
        sanitized = seen["config"]
        self.assertEqual(
            sanitized["database_path"],
            str(recovery / f".verify-{gen_id}" / ".gbrain" / "brain.pglite"),
        )
        self.assertEqual(
            sanitized["sync"]["repo_path"],
            str(recovery / gen_id / "vault"),
        )
        raw = json.dumps(sanitized)
        self.assertNotIn(live_database_path, raw)
        self.assertNotIn(live_repo_path, raw)
        # The BUNDLE is untouched (the VERIFIED_READY manifest sha binding
        # stays valid): the original absolute paths are still in the bundle.
        bundle_config = json.loads(
            (recovery / gen_id / ".gbrain" / "config.json").read_text("utf-8")
        )
        self.assertEqual(bundle_config["database_path"], live_database_path)
        self.assertEqual(bundle_config["sync"]["repo_path"], live_repo_path)


    def test_verify_containment_rewrites_every_live_absolute_key(self) -> None:
        """The containment scan covers the WHOLE disposable config, not just
        database_path/sync.repo_path: any absolute path with the LIVE
        gbrain/vault prefix in ANY (nested) key is rewritten into the
        disposable layout, so the doctor can never resolve a live path from
        an unexpected config key."""
        core = import_core()
        gbrain_dir = self.tmp / "src4" / ".gbrain"
        vault_dir = self.tmp / "src4" / "obsidian"
        write_tree(
            gbrain_dir,
            {
                "config.json": (
                    0o600,
                    json.dumps(
                        {
                            "engine": "pglite",
                            "database_path": "/opt/data/.gbrain/base",
                            "sync": {"repo_path": "/opt/data/obsidian"},
                            "embedding": {
                                "model_cache_dir": "/opt/data/.gbrain/cache/models",
                                "snippet_dir": "/opt/data/obsidian/.snippets",
                            },
                        }
                    )
                    + "\n",
                ),
                "base/PG_VERSION": (0o644, "16\n"),
            },
        )
        write_tree(vault_dir, {"note.md": (0o644, "# Vault note\nMARKER\n")})
        staging = self.tmp / "staging4"
        lock_path = self.tmp / "locks4" / "tasknotes.lock"
        lock_path.parent.mkdir(parents=True)
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_gbrain_bin(doctor_ok()), lock_path=str(lock_path),
            )
        recovery, _ = make_recovery_handoff(self.tmp, staging, manifest["generation_id"])
        seen: dict = {}
        real_doctor = self.core._run_doctor_at

        def capturing_doctor(gbrain_bin, home_root, brain_repo, schema_pack, timeout):
            cfg_path = Path(home_root) / ".gbrain" / "config.json"
            if cfg_path.exists():
                seen["config"] = json.loads(cfg_path.read_text("utf-8"))
            return real_doctor(gbrain_bin, home_root, brain_repo, schema_pack, timeout)

        with mock.patch.object(self.core, "_run_doctor_at", side_effect=capturing_doctor):
            self.core.verify_recovery(
                recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path)
            )
        sanitized = seen["config"]
        disposable_gbrain = recovery / f".verify-{manifest['generation_id']}" / ".gbrain"
        self.assertEqual(
            sanitized["database_path"],
            str(disposable_gbrain / "base"),
        )
        self.assertEqual(
            sanitized["embedding"]["model_cache_dir"],
            str(disposable_gbrain / "cache" / "models"),
        )
        self.assertEqual(
            sanitized["embedding"]["snippet_dir"],
            str(recovery / manifest["generation_id"] / "vault" / ".snippets"),
        )
        # No live absolute path survives anywhere in the disposable config.
        self.assertNotIn("/opt/data", json.dumps(sanitized))

    def test_verify_containment_unconfinable_path_fails_closed(self) -> None:
        """A config carrying an absolute path that resolves outside the
        disposable layout AND outside the known live prefixes refuses the
        verification: the doctor never runs on a config that could escape
        the disposable copy (fail closed)."""
        core = import_core()
        gbrain_dir = self.tmp / "src5" / ".gbrain"
        vault_dir = self.tmp / "src5" / "obsidian"
        write_tree(
            gbrain_dir,
            {
                "config.json": (
                    0o600,
                    json.dumps(
                        {
                            "database_path": "/opt/data/.gbrain/base",
                            "sync": {"repo_path": "/opt/data/obsidian"},
                            "unexpected": {"cache": "/var/lib/other-cache"},
                        }
                    )
                    + "\n",
                ),
                "base/PG_VERSION": (0o644, "16\n"),
            },
        )
        write_tree(vault_dir, {"note.md": (0o644, "# Vault note\nMARKER\n")})
        staging = self.tmp / "staging5"
        lock_path = self.tmp / "locks5" / "tasknotes.lock"
        lock_path.parent.mkdir(parents=True)
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_gbrain_bin(doctor_ok()), lock_path=str(lock_path),
            )
        recovery, _ = make_recovery_handoff(self.tmp, staging, manifest["generation_id"])
        with self.assertRaises(self.core.ValidationError) as cm:
            self.core.verify_recovery(
                recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path)
            )
        self.assertIn("unconfinable absolute path", str(cm.exception))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    # ------------------------------------------------------------------
    # Normalized/resolved containment (council fix: `..` bypass)
    # ------------------------------------------------------------------

    def _contain(self, config: dict, root: Path) -> dict:
        """Run the containment scan over a config rooted at
        ``root/.verify-x/.gbrain`` (disposable) with a bundle vault at
        ``root/<gen>/vault``."""
        disposable = root / ".verify-x" / ".gbrain"
        bundle_vault = root / "gen" / "vault"
        return self.core._contain_absolute_paths(config, disposable, bundle_vault)

    def test_containment_rejects_live_prefix_dotdot_rewrite_escape(self) -> None:
        """The `..` bypass on the live-gbrain prefix: a path like
        /opt/data/.gbrain/../../x passes the raw string-prefix test (it
        literally starts with the live gbrain dir) but its normalized form
        resolves OUTSIDE the live dir. The containment must compare
        NORMALIZED forms and refuse the whole config instead of rewriting
        the escape into the disposable copy (fail closed)."""
        with self.assertRaises(self.core.ValidationError) as cm:
            self._contain({"cache": "/opt/data/.gbrain/../../x"}, self.tmp)
        self.assertIn("unconfinable absolute path", str(cm.exception))
        with self.assertRaises(self.core.ValidationError):
            self._contain({"cache": "/opt/data/.gbrain/../../../etc/passwd"}, self.tmp)

    def test_containment_rejects_live_vault_dotdot_rewrite_escape(self) -> None:
        """Same `..` bypass on the live-vault prefix: /opt/data/obsidian/
        ../../y normalizes OUTSIDE the live vault and must be refused,
        never rewritten into an escaping path."""
        with self.assertRaises(self.core.ValidationError) as cm:
            self._contain({"sync": {"repo_path": "/opt/data/obsidian/../../y"}}, self.tmp)
        self.assertIn("unconfinable absolute path", str(cm.exception))

    def test_containment_rejects_dotdot_escape_in_disposable_root(self) -> None:
        """A path that is string-prefixed inside the disposable root but
        whose `..` components escape it (e.g. <disposable>/../escape) is
        NOT contained: the normalized form must stay under the resolved
        disposable root."""
        disposable = self.tmp / ".verify-x" / ".gbrain"
        with self.assertRaises(self.core.ValidationError) as cm:
            self._contain({"db": str(disposable) + "/../../escape"}, self.tmp)
        self.assertIn("unconfinable absolute path", str(cm.exception))

    def test_containment_normalizes_dotdot_inside_root(self) -> None:
        """A `..` path whose normalized form STAYS inside the disposable
        root is kept (normalized), never passed through raw: the doctor
        only ever sees canonical, contained paths."""
        disposable = self.tmp / ".verify-x" / ".gbrain"
        result = self._contain(
            {"db": str(disposable.parent) + "/a/../b"}, self.tmp
        )
        expected = os.path.normpath(str(disposable.parent) + "/b")
        self.assertEqual(result["db"], expected)

    def test_containment_rewrite_is_normalized_and_contained(self) -> None:
        """The live-prefix rewrite collapses `..` inside the remainder and
        re-checks containment on the NORMALIZED rewritten value: a
        /opt/data/.gbrain/../.gbrain/base path rewrites to the disposable
        .gbrain/base (normalized), never to an escaping path."""
        disposable = self.tmp / ".verify-x" / ".gbrain"
        result = self._contain(
            {"db": "/opt/data/.gbrain/../.gbrain/base"}, self.tmp
        )
        expected = os.path.normpath(
            os.path.join(os.path.realpath(str(disposable)), "base")
        )
        self.assertEqual(result["db"], expected)

    def test_verify_containment_rejects_dotdot_database_path(self) -> None:
        """Malicious path end to end (council fix): a bundle whose
        config.json carries a live-prefixed database_path with a `..`
        remainder that escapes the disposable root (e.g.
        /opt/data/.gbrain/../../../../etc/passwd) REFUSES the verification
        — the old raw-prefix check would have rewritten it into
        <disposable>/../../../../etc/passwd, which passes the string
        prefix test yet resolves to /etc/passwd. No VERIFIED_READY is
        written."""
        core = import_core()
        gbrain_dir = self.tmp / "src-dotdot" / ".gbrain"
        vault_dir = self.tmp / "src-dotdot" / "obsidian"
        write_tree(
            gbrain_dir,
            {
                "config.json": (
                    0o600,
                    json.dumps(
                        {
                            "database_path": "/opt/data/.gbrain/../../../../etc/passwd",
                            "sync": {"repo_path": "/opt/data/obsidian"},
                        }
                    )
                    + "\n",
                ),
                "base/PG_VERSION": (0o644, "16\n"),
            },
        )
        write_tree(vault_dir, {"note.md": (0o644, "# Vault note\nMARKER\n")})
        staging = self.tmp / "staging-dotdot"
        lock_path = self.tmp / "locks-dotdot" / "tasknotes.lock"
        lock_path.parent.mkdir(parents=True)
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_gbrain_bin(doctor_ok()), lock_path=str(lock_path),
            )
        recovery, _ = make_recovery_handoff(self.tmp, staging, manifest["generation_id"])
        with self.assertRaises(self.core.ValidationError) as cm:
            self.core.verify_recovery(
                recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path)
            )
        self.assertIn("unconfinable absolute path", str(cm.exception))
        self.assertFalse((recovery / "VERIFIED_READY").exists())
        # The disposable doctor copy is cleaned up even on refusal.
        self.assertFalse((recovery / f".verify-{manifest['generation_id']}").exists())

    # ------------------------------------------------------------------
    # Relative-path verifier containment (council fix: the verifier must
    # constrain the doctor working directory/environment AND refuse
    # relative config paths that could resolve into production)
    # ------------------------------------------------------------------

    def _relative_bundle(self, config: dict, label: str) -> Path:
        """Export a bundle whose .gbrain/config.json carries ``config`` and
        return its recovery handoff dir (the bundle is untouched by the
        verifier, so the relative paths survive into the disposable copy)."""
        core = import_core()
        gbrain_dir = self.tmp / f"src-rel-{label}" / ".gbrain"
        vault_dir = self.tmp / f"src-rel-{label}" / "obsidian"
        write_tree(
            gbrain_dir,
            {
                "config.json": (0o600, json.dumps(config) + "\n"),
                "base/PG_VERSION": (0o644, "16\n"),
            },
        )
        write_tree(vault_dir, {"note.md": (0o644, "# Vault note\nMARKER\n")})
        staging = self.tmp / f"staging-rel-{label}"
        lock_path = self.tmp / f"locks-rel-{label}" / "tasknotes.lock"
        lock_path.parent.mkdir(parents=True)
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_gbrain_bin(doctor_ok()), lock_path=str(lock_path),
            )
        recovery, _ = make_recovery_handoff(self.tmp, staging, manifest["generation_id"])
        return recovery

    def _rel_to(self, target: str, start: Path) -> str:
        """A relative path from a START OF THE SAME DEPTH as the real
        containment base (the disposable .gbrain dir or the bundle vault)
        to ``target``. Only the depth below the filesystem root matters for
        the resolution, so the placeholder names are irrelevant."""
        return os.path.relpath(target, start=start)

    def test_verify_containment_rejects_relative_dotdot_database_path(self) -> None:
        """A RELATIVE database_path with `..` components must never resolve
        into production: with the doctor cwd pinned inside the disposable
        root, a relative path walking out of the disposable copy is either
        refused (when it resolves outside the disposable layout and the
        known live prefixes — this bundle resolves to /etc/passwd) or
        rewritten into the disposable layout (when it resolves onto a live
        path). It is NEVER left relative for the doctor to resolve."""
        start = self.tmp / "recovery" / ".verify-GEN" / ".gbrain"
        rel_etc = self._rel_to("/etc/passwd", start)
        self.assertTrue(rel_etc.startswith("../"), rel_etc)
        recovery = self._relative_bundle(
            {"database_path": rel_etc, "sync": {"repo_path": "notes"}},
            "db-escape",
        )
        with self.assertRaises(self.core.ValidationError) as cm:
            self.core.verify_recovery(
                recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path)
            )
        self.assertIn("refusing to run the doctor", str(cm.exception))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_verify_containment_rejects_relative_dotdot_repo_path(self) -> None:
        """Same fail-closed rule for a RELATIVE sync.repo_path: a value that
        walks out of the bundle vault (resolving to /etc/passwd here)
        refuses the verification instead of resolving into the live vault
        or anywhere outside the bundle."""
        start = self.tmp / "recovery" / "GEN" / "vault"
        rel_etc = self._rel_to("/etc/passwd", start)
        recovery = self._relative_bundle(
            {"database_path": "base", "sync": {"repo_path": rel_etc}},
            "repo-escape",
        )
        with self.assertRaises(self.core.ValidationError) as cm:
            self.core.verify_recovery(
                recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path)
            )
        self.assertIn("refusing to run the doctor", str(cm.exception))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_verify_containment_rewrites_relative_live_resolution_into_disposable(self) -> None:
        """A relative database_path whose `..` components resolve exactly
        onto the LIVE gbrain path is neutralized by the live-prefix
        rewrite: the doctor gets a path INSIDE the disposable copy, never
        the live tree (containment by rewrite, same as the absolute
        case)."""
        start = self.tmp / "recovery" / ".verify-GEN" / ".gbrain"
        rel_live = self._rel_to("/opt/data/.gbrain", start)
        recovery = self._relative_bundle(
            {"database_path": rel_live, "sync": {"repo_path": "notes"}},
            "db-live-resolve",
        )
        seen: dict = {}
        real_doctor = self.core._run_doctor_at

        def capturing_doctor(gbrain_bin, home_root, brain_repo, schema_pack, timeout):
            cfg_path = Path(home_root) / ".gbrain" / "config.json"
            if cfg_path.exists():
                seen["config"] = json.loads(cfg_path.read_text("utf-8"))
            return real_doctor(gbrain_bin, home_root, brain_repo, schema_pack, timeout)

        with mock.patch.object(self.core, "_run_doctor_at", side_effect=capturing_doctor):
            self.core.verify_recovery(
                recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path)
            )
        self.assertTrue((recovery / "VERIFIED_READY").exists())
        gen_id = self._handoff_gen(recovery)
        disposable_gbrain = recovery / f".verify-{gen_id}" / ".gbrain"
        self.assertEqual(seen["config"]["database_path"], str(disposable_gbrain))
        self.assertNotIn("/opt/data/.gbrain", json.dumps(seen["config"]))

    def test_verify_containment_resolves_relative_database_path_into_disposable(self) -> None:
        """A BENIGN relative database_path (e.g. base/1234, the PGLite
        layout relative to the .gbrain dir) is resolved against the
        disposable .gbrain copy — the doctor opens the COPY, never the live
        tree, and verification succeeds."""
        recovery = self._relative_bundle(
            {"database_path": "base/1234", "sync": {"repo_path": "obsidian"}},
            "db-rel",
        )
        seen: dict = {}
        real_doctor = self.core._run_doctor_at

        def capturing_doctor(gbrain_bin, home_root, brain_repo, schema_pack, timeout):
            cfg_path = Path(home_root) / ".gbrain" / "config.json"
            if cfg_path.exists():
                seen["config"] = json.loads(cfg_path.read_text("utf-8"))
            return real_doctor(gbrain_bin, home_root, brain_repo, schema_pack, timeout)

        with mock.patch.object(self.core, "_run_doctor_at", side_effect=capturing_doctor):
            self.core.verify_recovery(
                recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path)
            )
        self.assertTrue((recovery / "VERIFIED_READY").exists())
        sanitized = seen["config"]
        gen_id = self._handoff_gen(recovery)
        disposable_gbrain = recovery / f".verify-{gen_id}" / ".gbrain"
        # The relative value was resolved INSIDE the disposable copy...
        self.assertEqual(
            sanitized["database_path"],
            str(disposable_gbrain / "base" / "1234"),
        )
        # ...and never reaches a live path (absolute or relative).
        raw = json.dumps(sanitized)
        self.assertNotIn("/opt/data", raw)
        self.assertNotIn("../", raw)

    def test_containment_refuses_relative_dotdot_in_any_key(self) -> None:
        """The relative rule applies to EVERY string value, not just
        database_path/repo_path: a relative value whose normalized form
        escapes the constrained cwd (../..) refuses the config (fail
        closed), while a benign relative value (sub/dir or a plain token)
        is left untouched."""
        with self.assertRaises(self.core.ValidationError) as cm:
            self._contain({"cache": "../.."}, self.tmp)
        self.assertIn("relative path that escapes", str(cm.exception))
        with self.assertRaises(self.core.ValidationError):
            self._contain({"cache": "sub/../../.."}, self.tmp)
        result = self._contain(
            {"cache": "sub/dir", "tz": "America/Sao_Paulo", "plain": "token"},
            self.tmp,
        )
        self.assertEqual(result["cache"], "sub/dir")
        self.assertEqual(result["tz"], "America/Sao_Paulo")
        self.assertEqual(result["plain"], "token")

    def test_verify_doctor_cwd_and_home_pinned_inside_disposable_root(self) -> None:
        """The doctor subprocess runs with cwd AND HOME inside the
        disposable root (council fix: relative verifier containment): even
        a relative path the config could smuggle resolves inside the
        disposable layout, never into the live tree."""
        recovery, _ = self._handoff()
        self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        env = json.loads(Path(self.fake_bin + ".env.json").read_text("utf-8"))
        disposable_root = os.path.realpath(str(recovery / f".verify-{self.gen_id}"))
        self.assertEqual(env["__CWD_REAL__"], disposable_root)
        self.assertEqual(os.path.realpath(env["HOME"]), disposable_root)
        self.assertNotIn("live", env["__CWD__"])
        self.assertNotIn("live", env["HOME"])

    def test_verify_refuses_when_shared_lock_held(self) -> None:
        """The verifier holds the same exclusive nonblocking shared lock as
        the install (fix 5): a concurrent install (or any gbrain user)
        refuses the verification instead of racing the handoff."""
        recovery, _ = self._handoff()
        with LockContext(self.lock_path):
            with self.assertRaises(self.core.LockError):
                self.core.verify_recovery(
                    recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path)
                )
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_verify_live_path_directed_config_never_touches_live_trees(self) -> None:
        """The LIVE trees stay byte-identical when the exported config
        carries paths directed at them: after verify, the live trees contain
        exactly their original files (the disposable doctor only ever sees
        the disposable copy)."""
        core = import_core()
        gbrain_dir = self.tmp / "src6" / ".gbrain"
        vault_dir = self.tmp / "src6" / "obsidian"
        write_tree(
            gbrain_dir,
            {
                "config.json": (
                    0o600,
                    json.dumps(
                        {
                            "database_path": str(self.live_gbrain / "base"),
                            "sync": {"repo_path": str(self.live_vault)},
                        }
                    )
                    + "\n",
                ),
                "base/PG_VERSION": (0o644, "16\n"),
            },
        )
        write_tree(vault_dir, {"note.md": (0o644, "# Vault note\nMARKER\n")})
        staging = self.tmp / "staging6"
        lock_path = self.tmp / "locks6" / "tasknotes.lock"
        lock_path.parent.mkdir(parents=True)
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_gbrain_bin(doctor_ok()), lock_path=str(lock_path),
            )
        recovery, _ = make_recovery_handoff(self.tmp, staging, manifest["generation_id"])
        live_before = self._live_files(self.live_gbrain)
        self.core.verify_recovery(
            recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path)
        )
        # The live trees are untouched: same files, same content.
        self.assertEqual(self._live_files(self.live_gbrain), live_before)
        self.assertEqual(
            (self.live_vault / "notes" / "hello.md").read_text("utf-8"),
            "OLD hello\n",
        )


class InstallRefusalTests(RestoreCoreBase):
    def test_install_requires_explicit_confirm(self) -> None:
        recovery = self._verified_handoff()
        with self.assertRaises(self.core.InstallError):
            self.core.install_generation(
                recovery, self.live_vault, self.live_gbrain, confirm=False,
                journal_root=self.journal_root,
            )

    def test_install_requires_verified_handoff(self) -> None:
        recovery, _ = self._handoff()  # RECOVERY_READY only, no VERIFIED_READY
        with self.assertRaises(self.core.HandoffError):
            self._install(recovery)

    def test_install_requires_recovery_handoff(self) -> None:
        recovery = self.tmp / "recovery"
        recovery.mkdir()
        with self.assertRaises(self.core.HandoffError):
            self._install(recovery)

    def test_install_refuses_mismatched_sentinels(self) -> None:
        recovery, _ = self._handoff()
        self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        (recovery / "VERIFIED_READY").write_text(
            "20260101T000000000000Z-ffffffff\nother-sha\n{}\n", encoding="utf-8"
        )
        with self.assertRaises(self.core.HandoffError):
            self._install(recovery)

    def test_install_refuses_when_shared_lock_held(self) -> None:
        recovery = self._verified_handoff()
        lock_path = self.tmp / "tasknotes.lock"
        fd = __import__("os").open(str(lock_path), __import__("os").O_RDWR | __import__("os").O_CREAT, 0o600)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(self.core.LockError):
                self.core.install_generation(
                    recovery, self.live_vault, self.live_gbrain, confirm=True,
                    journal_root=self.journal_root, lock_path=str(lock_path),
                )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            __import__("os").close(fd)
        # Refused install must not have created a journal.
        self.assertFalse((self.journal_root / self.gen_id).exists())

    def test_install_acquires_lock_before_any_handoff_read(self) -> None:
        """Fix 5 (install TOCTOU): the shared lock is acquired BEFORE any
        handoff read. With the lock held by another process, install refuses
        with LockError even for a MISSING handoff — the lock ordering comes
        first, so a concurrent verifier can never be mid-write while the
        install reads sentinels/bundle."""
        recovery = self.tmp / "recovery"
        recovery.mkdir()  # no RECOVERY_READY, no bundle at all
        with LockContext(self.lock_path):
            with self.assertRaises(self.core.LockError):
                self._install(recovery)
        # Nothing was created: no journal, no install roots on the live fs.
        self.assertFalse((self.journal_root / self.gen_id).exists())
        self.assertFalse((self.live_vault / ".vault-recovery-install").exists())

    # ------------------------------------------------------------------
    # Requested --generation binding (council fix): the operator's
    # requested generation is bound in the CORE, AFTER the lock — a
    # concurrent lock-less recover download that replaced the handoff with
    # a different generation can never be installed.
    # ------------------------------------------------------------------

    def test_install_rejects_requested_generation_mismatch_after_lock(self) -> None:
        """The requested --generation is compared against the RECOVERY_READY
        handoff generation AFTER the lock is acquired. A handoff carrying a
        DIFFERENT generation refuses the install: nothing is read beyond
        the sentinel, no bundle validation, no staging, no journal."""
        recovery = self._verified_handoff()
        other = "20260101T000000000000Z-ffffffff"
        with self.assertRaises(self.core.HandoffError) as cm:
            self._install(recovery, generation=other)
        self.assertIn("does not match", str(cm.exception))
        self.assertIn(other, str(cm.exception))
        self.assertFalse((self.journal_root / self.gen_id).exists())
        self.assertFalse((self.live_vault / ".vault-recovery-install").exists())
        self.assertFalse((self.live_gbrain.parent / ".vault-recovery-install").exists())

    def test_install_rejects_invalid_requested_generation(self) -> None:
        """A malformed requested generation id is refused up front (it can
        never match any handoff)."""
        recovery = self._verified_handoff()
        with self.assertRaises(self.core.HandoffError) as cm:
            self._install(recovery, generation="../not-a-gen")
        self.assertIn("invalid", str(cm.exception))

    def test_install_with_matching_requested_generation_succeeds(self) -> None:
        """A requested generation that matches the handoff installs
        normally (the binding is not a rejection of the flag itself)."""
        recovery = self._verified_handoff()
        result = self._install(recovery, generation=self.gen_id)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["generation_id"], self.gen_id)

    def test_concurrent_handoff_replacement_cannot_install_different_generation(self) -> None:
        """A lock-less rclone recover step that replaced the WHOLE handoff
        with a different generation (new bundle + RECOVERY_READY +
        VERIFIED_READY) between the wrapper's pre-check and the install
        cannot install that generation when the operator requested the
        original one: the mismatch is rejected under the lock, with the
        live trees untouched and no journal/staging created."""
        recovery = self._verified_handoff()
        other = "20260102T000000000000Z-aaaaaaaa"
        other_bundle = recovery / other
        shutil.copytree(recovery / self.gen_id, other_bundle)
        manifest_path = other_bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["generation_id"] = other
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        (other_bundle / "READY").write_text(f"{other}\n", encoding="utf-8")
        other_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        (recovery / "RECOVERY_READY").write_text(f"{other}\n{other_sha}\n", encoding="utf-8")
        (recovery / "VERIFIED_READY").write_text(
            f"{other}\n{other_sha}\n{{}}\n", encoding="utf-8"
        )
        with self.assertRaises(self.core.HandoffError) as cm:
            self._install(recovery, generation=self.gen_id)
        self.assertIn("does not match", str(cm.exception))
        # No journal and no staging on the destination filesystems.
        self.assertFalse((self.journal_root / self.gen_id).exists())
        self.assertFalse((self.journal_root / other).exists())
        self.assertFalse((self.live_vault / ".vault-recovery-install").exists())
        self.assertFalse((self.live_gbrain.parent / ".vault-recovery-install").exists())
        # Live trees untouched.
        self.assertEqual(
            (self.live_vault / "notes" / "hello.md").read_text("utf-8"),
            "OLD hello\n",
        )
        self.assertEqual(
            (self.live_gbrain / "old-state").read_text("utf-8"),
            "old gbrain content\n",
        )

    def test_install_aborts_when_handoff_changed_mid_install(self) -> None:
        """Fix 5: the bundle is re-validated immediately before the first
        mutation. A lock-less rclone recover step replacing the bundle
        mid-install (after staging) aborts BEFORE any rename: the live
        trees stay untouched and the journal records no mutation steps."""
        recovery = self._verified_handoff()
        bundle_manifest = recovery / self.gen_id / "manifest.json"
        real_copy_tree = self.core.core.copy_tree
        calls = {"n": 0}

        def tampering_copy_tree(root, records, dst_root):
            real_copy_tree(root, records, dst_root)
            calls["n"] += 1
            if calls["n"] == 2:
                # After both trees are staged, the recover step replaces the
                # bundle (simulated): the manifest changes under the install.
                with open(bundle_manifest, "a", encoding="utf-8") as fh:
                    fh.write("REPLACED_MID_INSTALL\n")

        with mock.patch.object(self.core.core, "copy_tree", side_effect=tampering_copy_tree):
            with self.assertRaises(self.core.HandoffError) as cm:
                self._install(recovery)
        self.assertIn("changed during install", str(cm.exception))
        # Live trees untouched (no swap happened).
        self.assertEqual(
            (self.live_vault / "notes" / "hello.md").read_text("utf-8"),
            "OLD hello\n",
        )
        self.assertEqual(
            (self.live_gbrain / "old-state").read_text("utf-8"),
            "old gbrain content\n",
        )
        # The journal exists (created before staging) but has NO mutation
        # steps and is marked rolled-back (nothing to undo).
        journal = json.loads(
            (self.journal_root / self.gen_id / "journal.json").read_text("utf-8")
        )
        self.assertEqual(journal["status"], "rolled-back")
        self.assertEqual(journal["steps"], [])

    def test_rollback_acquires_lock_before_reading_journal(self) -> None:
        """Fix 5: rollback takes the shared lock BEFORE reading the journal,
        so it can never race a concurrent install's journaled transaction."""
        recovery = self._verified_handoff()
        self._install(recovery)
        journal_path = self.journal_root / self.gen_id / "journal.json"
        journal_before = journal_path.read_text("utf-8")
        with LockContext(self.lock_path):
            with self.assertRaises(self.core.LockError):
                self.core.rollback_generation(
                    self.journal_root, self.gen_id, lock_path=str(self.lock_path)
                )
        # Refused: the journal is untouched and the install stays complete.
        self.assertEqual(journal_path.read_text("utf-8"), journal_before)
        journal = json.loads(journal_before)
        self.assertEqual(journal["status"], "complete")


class InstallAndRollbackTests(RestoreCoreBase):
    def test_install_swaps_both_trees_and_journals(self) -> None:
        recovery = self._verified_handoff()
        result = self._install(recovery)
        self.assertEqual(result["status"], "complete")
        # .gbrain: sibling backup -> whole-tree atomic rename. vault: the
        # backup root lives INSIDE the live tree (same filesystem as the
        # vault volume), so rename(2) fails EINVAL and the journaled
        # per-entry swap takes over - the transaction contract (all or
        # rolled back) is unchanged.
        self.assertEqual(result["swap_modes"], {".gbrain": "atomic", "vault": "per-entry"})
        # Live trees now carry the bundle content.
        self.assertEqual(
            (self.live_vault / "notes" / "hello.md").read_text("utf-8"),
            "# Hello\nmarker-1\n",
        )
        self.assertTrue((self.live_gbrain / "config.json").exists())
        self.assertFalse((self.live_vault / "old-note.md").exists())
        self.assertFalse((self.live_gbrain / "old-state").exists())
        # Backups retain the old content; journal is complete.
        journal = json.loads((self.journal_root / self.gen_id / "journal.json").read_text("utf-8"))
        self.assertEqual(journal["status"], "complete")
        backup_vault = Path(journal["backup_vault"])
        self.assertEqual((backup_vault / "old-note.md").read_text("utf-8"), "old vault content\n")

    def test_install_refused_when_journal_already_exists(self) -> None:
        recovery = self._verified_handoff()
        self._install(recovery)
        with self.assertRaises(self.core.InstallError):
            self._install(recovery)

    def test_install_rolls_back_on_staged_rename_failure(self) -> None:
        """A failure after the live tree was moved aside must restore the
        ORIGINAL live content automatically (journal status rolled-back)."""
        recovery = self._verified_handoff()
        staged_gbrain = str(self.live_gbrain.parent / ".vault-recovery-install" / self.gen_id / "gbrain-staged")
        real_rename = self.core.os.rename

        def failing_rename(src, dst):
            if str(src) == staged_gbrain and str(dst) == str(self.live_gbrain):
                raise OSError(errno.EIO, "simulated staged->live rename failure")
            return real_rename(src, dst)

        with mock.patch.object(self.core.os, "rename", side_effect=failing_rename):
            with self.assertRaises(OSError):
                self._install(recovery)
        # Automatic rollback restored the ORIGINAL live .gbrain.
        self.assertEqual(
            (self.live_gbrain / "old-state").read_text("utf-8"), "old gbrain content\n"
        )
        self.assertEqual(
            (self.live_gbrain / "config.json").read_text("utf-8"), '{"old": true}\n'
        )
        self.assertFalse((self.live_gbrain / "base").exists())
        journal = json.loads((self.journal_root / self.gen_id / "journal.json").read_text("utf-8"))
        self.assertEqual(journal["status"], "rolled-back")

    def test_install_mount_root_falls_back_to_per_entry_swap(self) -> None:
        """The production vault is the root of a mounted volume; rename(2)
        onto the mount root fails with EBUSY. The journaled per-top-level
        entry swap must take over, install correctly, and stay reversible."""
        recovery = self._verified_handoff()
        real_rename = self.core.os.rename
        live_vault_str = str(self.live_vault)
        backup_vault_str = str(self.live_vault / ".vault-recovery-install" / self.gen_id / "vault-backup")

        def busy_rename(src, dst):
            if str(src) == live_vault_str and str(dst) == backup_vault_str:
                raise OSError(errno.EBUSY, "Device or resource busy (mount root)")
            return real_rename(src, dst)

        with mock.patch.object(self.core.os, "rename", side_effect=busy_rename):
            result = self._install(recovery)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["swap_modes"], {".gbrain": "atomic", "vault": "per-entry"})
        self.assertEqual(
            (self.live_vault / "notes" / "hello.md").read_text("utf-8"),
            "# Hello\nmarker-1\n",
        )
        self.assertFalse((self.live_vault / "old-note.md").exists())
        journal = json.loads((self.journal_root / self.gen_id / "journal.json").read_text("utf-8"))
        self.assertEqual(journal["status"], "complete")
        ops = [s["op"] for s in journal["steps"] if s["tree"] == "vault"]
        self.assertIn("move-live-entry", ops)
        self.assertIn("move-staged-entry", ops)
        self.assertIn("add-entry", ops)

    def test_rollback_restores_original_live_trees(self) -> None:
        recovery = self._verified_handoff()
        self._install(recovery)
        rollback = self.core.rollback_generation(
            self.journal_root, self.gen_id, lock_path=str(self.lock_path)
        )
        self.assertEqual(rollback["status"], "rolled-back")
        # The ORIGINAL live content is back (the install had overwritten it).
        self.assertEqual(
            (self.live_vault / "old-note.md").read_text("utf-8"), "old vault content\n"
        )
        self.assertEqual(
            (self.live_vault / "notes" / "hello.md").read_text("utf-8"), "OLD hello\n"
        )
        self.assertEqual(
            (self.live_gbrain / "old-state").read_text("utf-8"), "old gbrain content\n"
        )
        self.assertEqual(
            (self.live_gbrain / "config.json").read_text("utf-8"), '{"old": true}\n'
        )
        self.assertFalse((self.live_vault / "empty").exists())
        self.assertFalse((self.live_gbrain / "empty-dir").exists())
        # Idempotent: a second rollback reports already-rolled-back.
        second = self.core.rollback_generation(
            self.journal_root, self.gen_id, lock_path=str(self.lock_path)
        )
        self.assertEqual(second["status"], "already-rolled-back")

    def test_rollback_removes_staged_only_files(self) -> None:
        """Regression: an install over a live vault that lacks the staged
        entries (e.g. after a destroy — the DR drill scenario) adds top-level
        FILES via add-entry; rollback must remove them. `_rmtree_durable`
        used `shutil.rmtree(..., ignore_errors=True)`, which silently no-ops
        on a regular file (scandir raises NotADirectoryError, swallowed), so
        installed files survived the rollback."""
        core = import_core()
        gbrain_dir = self.tmp / "src2" / ".gbrain"
        vault_dir = self.tmp / "src2" / "obsidian"
        write_tree(
            gbrain_dir,
            {"config.json": (0o600, '{"search": {"mcp_keyword_only": true}}\n')},
        )
        write_tree(
            vault_dir,
            {
                "note.md": (0o644, "# Vault note\nMARKER\n"),
                "pa.md": (0o644, "# Page A\n\nMARKER A\n"),
                "attachments/a.bin": (0o644, "attachment-bytes\n"),
            },
        )
        staging = self.tmp / "staging2"
        lock_path = self.tmp / "locks2" / "tasknotes.lock"
        lock_path.parent.mkdir(parents=True)
        with LockContext(lock_path):
            manifest = core.export_generation(
                gbrain_dir, vault_dir, staging,
                gbrain_bin=fake_gbrain_bin(doctor_ok()), lock_path=str(lock_path),
            )
        gen_id = manifest["generation_id"]
        recovery, _ = make_recovery_handoff(self.tmp, staging, gen_id)
        self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        # The destroyed mount layout: empty live trees (mount roots stay).
        live_vault = self.tmp / "live2" / "obsidian"
        live_gbrain = self.tmp / "live2" / ".gbrain"
        live_vault.mkdir(parents=True)
        live_gbrain.mkdir(parents=True)
        install_lock_path = self.tmp / "locks2b" / "tasknotes.lock"
        install_lock_path.parent.mkdir(parents=True)
        install = self.core.install_generation(
            recovery,
            live_vault,
            live_gbrain,
            confirm=True,
            journal_root=self.tmp / "install-journal2",
            lock_path=str(install_lock_path),
        )
        self.assertEqual(install["swap_modes"]["vault"], "per-entry")
        rollback = self.core.rollback_generation(
            self.tmp / "install-journal2", gen_id,
            lock_path=str(install_lock_path),
        )
        self.assertEqual(rollback["status"], "rolled-back")
        # Every installed entry is gone; only the documented install
        # leftover (staged/backup roots) remains in the live vault.
        self.assertEqual(
            sorted(p.name for p in live_vault.iterdir()),
            [".vault-recovery-install"],
        )
        self.assertEqual(list(live_gbrain.iterdir()), [])

    def test_rollback_unknown_generation_refused(self) -> None:
        """An unknown generation id is refused with JournalError (the
        lock-first ordering still checks the journal under the lock)."""
        with self.assertRaises(self.core.JournalError):
            self.core.rollback_generation(
                self.journal_root, "20260101T000000000000Z-ffffffff",
                lock_path=str(self.lock_path),
            )


class WriteAheadJournalTests(RestoreCoreBase):
    def test_write_ahead_step_recorded_before_rename(self) -> None:
        """The journal must record each mutation step (state pending) BEFORE
        the rename happens: at the moment the first live->backup rename
        fails, the pending step is already durable in the journal."""
        recovery = self._verified_handoff()
        journal_path = self.journal_root / self.gen_id / "journal.json"
        real_rename = self.core.os.rename
        seen: dict = {}

        def crashing_rename(src, dst):
            if str(src) == str(self.live_gbrain):
                seen["steps"] = json.loads(journal_path.read_text("utf-8"))["steps"]
                raise OSError(errno.EIO, "simulated crash before the first rename")
            return real_rename(src, dst)

        with mock.patch.object(self.core.os, "rename", side_effect=crashing_rename):
            with self.assertRaises(self.core.InstallError):
                self._install(recovery)
        self.assertEqual(seen["steps"][-1]["op"], "move-live-tree")
        self.assertEqual(seen["steps"][-1]["state"], "pending")
        # Automatic rollback probes the pending step (rename never happened:
        # backup absent -> live untouched) and reports rolled-back.
        journal = json.loads(journal_path.read_text("utf-8"))
        self.assertEqual(journal["status"], "rolled-back")
        self.assertEqual(
            (self.live_gbrain / "old-state").read_text("utf-8"), "old gbrain content\n"
        )
        self.assertEqual(
            (self.live_gbrain / "config.json").read_text("utf-8"), '{"old": true}\n'
        )
        self.assertFalse((self.live_gbrain / "base").exists())

    def test_crash_window_journal_recovered_by_rollback(self) -> None:
        """A crash mid-transaction (journal in-progress with a mix of done
        and pending write-ahead steps, auto-rollback never ran) is recovered
        by the operator rollback command, which probes the filesystem."""
        self._verified_handoff()
        vault_install_root = self.live_vault / ".vault-recovery-install" / self.gen_id
        gbrain_install_root = self.live_gbrain.parent / ".vault-recovery-install" / self.gen_id
        staged_vault = vault_install_root / "vault-staged"
        backup_vault = vault_install_root / "vault-backup"
        staged_gbrain = gbrain_install_root / "gbrain-staged"
        backup_gbrain = gbrain_install_root / "gbrain-backup"

        # Crash point: the .gbrain atomic swap COMPLETED (live=new,
        # backup=old, staged gone); the vault per-entry swap moved the live
        # entries aside, then died BEFORE the staged notes twin was renamed
        # in (its step is still pending).
        backup_gbrain.mkdir(parents=True)
        shutil.copytree(self.live_gbrain, backup_gbrain, dirs_exist_ok=True)  # OLD tree aside
        shutil.rmtree(self.live_gbrain)
        shutil.copytree(self.bundle / ".gbrain", self.live_gbrain)  # NEW live
        staged_gbrain.mkdir(parents=True)  # renamed away by the atomic swap
        backup_vault.mkdir(parents=True)
        shutil.move(str(self.live_vault / "old-note.md"), str(backup_vault / "old-note.md"))
        shutil.move(str(self.live_vault / "notes"), str(backup_vault / "notes"))
        shutil.copytree(self.bundle / "vault", staged_vault)  # staged twin never moved
        journal_dir = self.journal_root / self.gen_id
        journal_dir.mkdir(parents=True)
        (journal_dir / "journal.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": self.gen_id,
                    "created_at_utc": "2026-01-01T00:00:00Z",
                    "status": "in-progress",
                    "live_vault": str(self.live_vault),
                    "live_gbrain": str(self.live_gbrain),
                    "staged_vault": str(staged_vault),
                    "backup_vault": str(backup_vault),
                    "staged_gbrain": str(staged_gbrain),
                    "backup_gbrain": str(backup_gbrain),
                    "steps": [
                        {"tree": ".gbrain", "op": "move-live-tree", "name": "", "state": "done"},
                        {"tree": ".gbrain", "op": "move-staged-tree", "name": "", "state": "done"},
                        {"tree": "vault", "op": "remove-entry", "name": "old-note.md", "state": "done"},
                        {"tree": "vault", "op": "move-live-entry", "name": "notes", "state": "done"},
                        {"tree": "vault", "op": "move-staged-entry", "name": "notes", "state": "pending"},
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        rollback = self.core.rollback_generation(
            self.journal_root, self.gen_id, lock_path=str(self.lock_path)
        )
        self.assertEqual(rollback["status"], "rolled-back")
        # The ORIGINAL live content is back.
        self.assertEqual(
            (self.live_gbrain / "old-state").read_text("utf-8"), "old gbrain content\n"
        )
        self.assertEqual(
            (self.live_gbrain / "config.json").read_text("utf-8"), '{"old": true}\n'
        )
        self.assertEqual(
            (self.live_vault / "old-note.md").read_text("utf-8"), "old vault content\n"
        )
        self.assertEqual(
            (self.live_vault / "notes" / "hello.md").read_text("utf-8"), "OLD hello\n"
        )
        journal = json.loads((journal_dir / "journal.json").read_text("utf-8"))
        self.assertEqual(journal["status"], "rolled-back")


class RollbackLockTests(RestoreCoreBase):
    def test_rollback_refuses_when_shared_lock_held(self) -> None:
        """Rollback mutates the live trees: it must hold the same exclusive
        nonblocking lock as the install and refuse while any gbrain user is
        active."""
        recovery = self._verified_handoff()
        self._install(recovery)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(self.core.LockError):
                self.core.rollback_generation(
                    self.journal_root, self.gen_id, lock_path=str(self.lock_path)
                )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        # The refused rollback must not have mutated the installed trees.
        self.assertEqual(
            (self.live_vault / "notes" / "hello.md").read_text("utf-8"),
            "# Hello\nmarker-1\n",
        )
        journal = json.loads((self.journal_root / self.gen_id / "journal.json").read_text("utf-8"))
        self.assertEqual(journal["status"], "complete")


class SentinelSchemaBoundTests(RestoreCoreBase):
    def test_verify_refuses_recovery_ready_sha_mismatch(self) -> None:
        recovery, _ = self._handoff()
        (recovery / "RECOVERY_READY").write_text(
            f"{self.gen_id}\n{'0' * 64}\n", encoding="utf-8"
        )
        with self.assertRaises(self.core.HandoffError):
            self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_verify_refuses_manifest_tampered_after_handoff(self) -> None:
        recovery, _ = self._handoff()
        manifest_path = recovery / self.gen_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["tampered"] = True
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        with self.assertRaises(self.core.HandoffError) as cm:
            self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertIn("RECOVERY_READY manifest sha256", str(cm.exception))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_unknown_manifest_schema_refused(self) -> None:
        recovery, _ = self._handoff()
        manifest_path = recovery / self.gen_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["schema_version"] = 99
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        # Re-point the handoff sha so the SCHEMA check (not the sha binding)
        # is what fires.
        new_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        (recovery / "RECOVERY_READY").write_text(
            f"{self.gen_id}\n{new_sha}\n", encoding="utf-8"
        )
        with self.assertRaises(self.core.HandoffError) as cm:
            self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertIn("schema_version", str(cm.exception))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_oversized_handoff_sentinel_refused(self) -> None:
        recovery, _ = self._handoff()
        with open(recovery / "RECOVERY_READY", "a", encoding="utf-8") as fh:
            fh.write("x" * (self.core.SENTINEL_MAX_BYTES + 1))
        with self.assertRaises(self.core.HandoffError) as cm:
            self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertIn("oversized", str(cm.exception))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_verify_removes_disposable_doctor_copy(self) -> None:
        """The disposable doctor copy is a transient artifact: a successful
        verify must not leave a stale .verify-<gen> tree behind."""
        recovery, _ = self._handoff()
        self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertFalse((recovery / f".verify-{self.gen_id}").exists())

    def test_verify_removes_stale_verified_ready_on_failure(self) -> None:
        """A failed RE-verification must not leave the earlier run's
        VERIFIED_READY behind: the sentinel may only exist when the most
        recent verification completed successfully."""
        recovery, _ = self._handoff()
        self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertTrue((recovery / "VERIFIED_READY").exists())
        # The bundle is tampered after the first (successful) verification;
        # the re-verify must fail AND remove the stale sentinel.
        (recovery / self.gen_id / "vault" / "notes" / "hello.md").write_text(
            "TAMPERED\n", encoding="utf-8"
        )
        with self.assertRaises(self.core.ValidationError):
            self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertFalse(
            (recovery / "VERIFIED_READY").exists(),
            "failed re-verify must remove the stale VERIFIED_READY",
        )

    def test_verify_removes_stale_verified_ready_when_doctor_fails(self) -> None:
        """Same stale-removal invariant for a doctor failure: a re-verify
        that dies in the disposable doctor must still leave no sentinel."""
        recovery, _ = self._handoff()
        self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertTrue((recovery / "VERIFIED_READY").exists())
        failing = fake_gbrain_bin(doctor_ok(checks=[]), exit_code=0)
        with self.assertRaises(self.core.core.DoctorPreflightError):
            self.core.verify_recovery(recovery, gbrain_bin=failing, lock_path=str(self.lock_path))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_verify_removes_stale_verified_ready_even_without_handoff(self) -> None:
        """The invariant holds regardless of WHY the verify fails: a verify
        run against a handoff that has no RECOVERY_READY still clears a
        stale sentinel from an earlier generation."""
        recovery, _ = self._handoff()
        self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        (recovery / "RECOVERY_READY").unlink()
        with self.assertRaises(self.core.HandoffError):
            self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_verify_success_replaces_stale_verified_ready(self) -> None:
        """A successful re-verify replaces the stale sentinel with its own
        (the sentinel always reflects the most recent completed run)."""
        recovery, manifest_sha = self._handoff()
        (recovery / "VERIFIED_READY").write_text(
            "20260101T000000000000Z-ffffffff\nstale-sha\n{}\n", encoding="utf-8"
        )
        result = self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))
        self.assertEqual(result["generation_id"], self.gen_id)
        verified = (recovery / "VERIFIED_READY").read_text("utf-8").splitlines()
        self.assertEqual(verified[0], self.gen_id)
        self.assertEqual(verified[1], manifest_sha)

    def test_verify_fsyncs_stale_removal_before_doctor(self) -> None:
        """fsync-before-verification: the stale-sentinel removal is fsynced
        up front, BEFORE any doctor work, so a crash/power loss during the
        verification cannot resurrect the stale sentinel."""
        recovery, _ = self._handoff()
        self.core.verify_recovery(recovery, gbrain_bin=self.fake_bin, lock_path=str(self.lock_path))  # stale sentinel
        failing = fake_gbrain_bin(doctor_ok(checks=[]), exit_code=0)
        # The fake doctor dumps its env to <bin>.env.json when it runs; the
        # recorder flags each fsync as before/after the doctor executed.
        fsync_calls: list[tuple[str, bool]] = []
        real_fsync = self.core._fsync_dir
        doctor_env_marker = Path(failing + ".env.json")

        def recording_fsync(path):
            doctor_ran = doctor_env_marker.exists()
            fsync_calls.append((str(path), doctor_ran))
            return real_fsync(path)

        with mock.patch.object(self.core, "_fsync_dir", side_effect=recording_fsync):
            with self.assertRaises(self.core.core.DoctorPreflightError):
                self.core.verify_recovery(recovery, gbrain_bin=failing, lock_path=str(self.lock_path))
        recovery_fsyncs = [c for c in fsync_calls if c[0] == str(recovery)]
        self.assertTrue(recovery_fsyncs, "the recovery dir must be fsynced for the removal")
        self.assertFalse(
            recovery_fsyncs[0][1],
            "the first recovery-dir fsync (stale removal) must happen BEFORE the doctor runs",
        )
        self.assertFalse((recovery / "VERIFIED_READY").exists())

    def test_install_refuses_bundle_changed_after_verification(self) -> None:
        """VERIFIED_READY's manifest sha256 must bind the exact bundle that
        was verified: a bundle replaced after verification refuses install."""
        recovery = self._verified_handoff()
        manifest_path = recovery / self.gen_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["tampered"] = True
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        # Re-point RECOVERY_READY at the new manifest (simulating a bundle
        # swap after verify); VERIFIED_READY still carries the ORIGINAL sha.
        new_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        (recovery / "RECOVERY_READY").write_text(
            f"{self.gen_id}\n{new_sha}\n", encoding="utf-8"
        )
        with self.assertRaises(self.core.HandoffError) as cm:
            self._install(recovery)
        self.assertIn("VERIFIED_READY manifest sha256", str(cm.exception))
        self.assertFalse((self.journal_root / self.gen_id).exists())

    def test_rollback_refuses_oversized_journal(self) -> None:
        recovery = self._verified_handoff()
        self._install(recovery)
        journal_path = self.journal_root / self.gen_id / "journal.json"
        with open(journal_path, "a", encoding="utf-8") as fh:
            fh.write(" " * (self.core.JOURNAL_MAX_BYTES + 1))
        with self.assertRaises(self.core.JournalError) as cm:
            self.core.rollback_generation(
                self.journal_root, self.gen_id, lock_path=str(self.lock_path)
            )
        self.assertIn("oversized", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
