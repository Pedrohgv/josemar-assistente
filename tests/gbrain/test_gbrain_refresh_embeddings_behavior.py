"""Behavior tests for `josemar-gbrain refresh-embeddings` and its cron entrypoint.

These run the real wrapper against a fake `gbrain` binary (no Docker, no gbrain
needed) so the lock, semantic-mode gate, marker gates, command order, and the
timeout process-group/lock-release behavior are exercised end to end:

  - refresh-embeddings reads `search.mcp_keyword_only` through the exact stdout
    of `gbrain config get` while holding the shared tasknotes flock
  - exact `false` is required; anything else skips or fails closed
  - the completion-marker and embedding_disabled gates run under the lock
  - sync/extract must precede the stale-only embed, which runs at concurrency 1
  - the lock is released when the wrapper exits
  - the cron entrypoint terminates its whole process group on timeout, which
    releases a flock held by an orphaned child
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATH = REPO_ROOT / "scripts" / "josemar-gbrain"
HELPER_PATH = REPO_ROOT / "scripts" / "hermes-gbrain-embedding-refresh.py"
CRON_SCRIPT_PATH = REPO_ROOT / "scripts" / "hermes-gbrain-embedding-refresh-cron.sh"

MODEL = "llama-server:intfloat/multilingual-e5-small"
DIMENSIONS = "384"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class FakeGbrain:
    """A fake `gbrain` binary that logs every invocation and behaves per-command."""

    def __init__(self, tmp: Path, keyword_only: str = "false", config_rc: int = 0):
        self.tmp = tmp
        self.log = tmp / "gbrain-calls.log"
        self.script = tmp / "gbrain"
        self.keyword_only = keyword_only
        self.config_rc = config_rc
        self._write_script()

    def _write_script(self) -> None:
        _write(self.script, f"""#!/bin/sh
echo "$*" >> "{self.log}"
case "$1" in
  config)
    if [ "$2" = "get" ]; then
      if [ {self.config_rc} -ne 0 ]; then echo "boom" >&2; exit {self.config_rc}; fi
      printf '%s\\n' "{self.keyword_only}"
    else
      echo "ok"
    fi
    ;;
  sync) echo '{{"status":"ok"}}' ;;
  extract)
    if [ "$2" = "links" ]; then echo '{{"status":"ok"}}'; else echo '{{"status":"ok"}}'; fi
    ;;
  embed) echo '{{"status":"ok"}}' ;;
  *) echo '{{"status":"ok"}}' ;;
esac
""")
        self.script.chmod(0o755)

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln]


class RefreshEmbeddingsBehaviorBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state" / ".gbrain"
        self.brain = self.tmp / "brain"
        self.lock_path = self.tmp / "locks" / "tasknotes.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch()
        self.marker = self.state / "embedding-backfill-complete.json"
        self.config = self.state / "config.json"
        self.fake = FakeGbrain(self.tmp, keyword_only="false")
        _write(self.config, json.dumps({"search": {"mcp_keyword_only": False}}))
        _write(self.marker,
               json.dumps({"model": MODEL, "dimensions": 384, "revision": REVISION}))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def env(self, **extra) -> dict:
        env = os.environ.copy()
        env.update(
            {
                "JOSEMAR_GBRAIN_DROPPED_PRIVS": "1",
                "GBRAIN_BIN": str(self.fake.script),
                "GBRAIN_HOME": str(self.state.parent),
                "GBRAIN_BRAIN_REPO": str(self.brain),
                "GBRAIN_TASKNOTES_LOCK": str(self.lock_path),
                "GBRAIN_EMBEDDING_MODEL": MODEL,
                "GBRAIN_EMBEDDING_DIMENSIONS": DIMENSIONS,
                "GBRAIN_EMBEDDING_MODEL_REVISION": REVISION,
                "HOME": str(self.tmp),
            }
        )
        env.update(extra)
        return env

    def run_wrapper(self, subcommand: str = "refresh-embeddings",
                    **extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(WRAPPER_PATH), subcommand],
            env=self.env(**extra),
            capture_output=True,
            text=True,
            timeout=60,
        )


class RefreshEmbeddingsSuccessTests(RefreshEmbeddingsBehaviorBase):
    def test_success_path_emits_success_and_embeds(self) -> None:
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"success": true', result.stdout)
        self.assertIn('"action": "refresh-embeddings"', result.stdout)
        self.assertIn('"message"', result.stdout)

    def test_exact_command_order(self) -> None:
        """Order: config get (under lock) -> sync -> extract -> extract links
        -> embed --stale (last)."""
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.fake.calls()
        self.assertIn("config get search.mcp_keyword_only", calls)
        order = [
            next(i for i, c in enumerate(calls) if c.startswith("config get")),
            next(i for i, c in enumerate(calls) if c.startswith("sync ")),
            next(i for i, c in enumerate(calls) if c.startswith("extract --stale")),
            next(i for i, c in enumerate(calls) if c.startswith("extract links")),
            next(i for i, c in enumerate(calls) if c.startswith("embed --stale")),
        ]
        self.assertEqual(order, sorted(order), f"wrong command order: {calls}")
        self.assertEqual(calls[-1].split()[0], "embed",
                         f"embed must be the final gbrain invocation: {calls}")

    def test_lock_released_after_success(self) -> None:
        self.run_wrapper()
        with open(self.lock_path, "r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class RefreshEmbeddingsGateTests(RefreshEmbeddingsBehaviorBase):
    def test_keyword_only_true_skips_without_embedding(self) -> None:
        self.fake = FakeGbrain(self.tmp, keyword_only="true")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"reason": "keyword_only"', result.stdout)
        self.assertNotIn("embed", self.fake.calls()[0] if self.fake.calls() else "")

    def test_keyword_only_non_exact_fails_closed(self) -> None:
        """Anything other than the exact stdout `false` must fail closed."""
        self.fake = FakeGbrain(self.tmp, keyword_only="false ")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 1)
        self.assertIn("refresh_embeddings_semantic_mode_invalid", result.stdout)
        self.assertNotIn("embed", self.fake.calls()[0] if self.fake.calls() else "")

    def test_config_read_failure_fails(self) -> None:
        self.fake = FakeGbrain(self.tmp, keyword_only="false", config_rc=1)
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 1)
        self.assertIn("refresh_embeddings_config_read_failed", result.stdout)
        self.assertNotIn("embed", self.fake.calls()[0] if self.fake.calls() else "")

    def test_marker_missing_skips(self) -> None:
        self.marker.unlink()
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"reason": "completion_marker_missing"', result.stdout)
        self.assertNotIn("embed", self.fake.calls()[0] if self.fake.calls() else "")

    def test_marker_tuple_mismatch_fails(self) -> None:
        _write(self.marker,
               json.dumps({"model": "other/model", "dimensions": 768, "revision": REVISION}))
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 1)
        self.assertIn("refresh_embeddings_marker_tuple_mismatch", result.stdout)
        self.assertNotIn("embed", self.fake.calls()[0] if self.fake.calls() else "")

    def test_embedding_disabled_sentinel_skips(self) -> None:
        _write(self.config, json.dumps({"embedding_disabled": True}))
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"reason": "embedding_disabled"', result.stdout)
        self.assertNotIn("embed", self.fake.calls()[0] if self.fake.calls() else "")

    def test_uninitialized_config_skips(self) -> None:
        self.config.unlink()
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"reason": "uninitialized"', result.stdout)


class RefreshEmbeddingsLockTests(RefreshEmbeddingsBehaviorBase):
    def test_lock_busy_skips_before_any_gbrain_access(self) -> None:
        with open(self.lock_path, "r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                result = self.run_wrapper()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"reason": "lock_busy"', result.stdout)
        self.assertEqual(self.fake.calls(), [],
                         "no gbrain command may run while the lock is busy")

    def test_lock_unavailable_fails(self) -> None:
        missing = self.tmp / "no-such-dir" / "tasknotes.lock"
        result = self.run_wrapper(**{"GBRAIN_TASKNOTES_LOCK": str(missing)})
        self.assertEqual(result.returncode, 1)
        self.assertIn("refresh_embeddings_lock_unavailable", result.stdout)


class EmbeddingsMarkerLifecycleTests(RefreshEmbeddingsBehaviorBase):
    """Marker write/removal failures must be explicit structured nonzero
    errors and must never claim success or fall through to the block-level
    lock_unavailable handler."""

    def _marker_as_directory(self) -> None:
        """Make the marker path unremovable/unreplaceable by turning it into a
        directory (rm -f fails on a directory; os.replace to a directory
        raises)."""
        if self.marker.is_dir():
            return
        self.marker.unlink()
        self.marker.mkdir(parents=True, exist_ok=True)

    def test_embed_backfill_marker_write_failure_is_structured(self) -> None:
        self._marker_as_directory()
        result = self.run_wrapper(subcommand="embed-backfill")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("embed_backfill_marker_write_failed", result.stdout)
        self.assertNotIn("embed_backfill_lock_unavailable", result.stdout)
        self.assertNotIn('"success": true', result.stdout)

    def test_enable_embeddings_marker_removal_failure_is_structured(self) -> None:
        self._marker_as_directory()
        result = self.run_wrapper(subcommand="enable-embeddings")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("enable_embeddings_marker_removal_failed", result.stdout)
        self.assertNotIn("enable_embeddings_lock_unavailable", result.stdout)
        self.assertNotIn('"success": true', result.stdout)

    def test_disable_embeddings_marker_removal_failure_is_structured(self) -> None:
        self._marker_as_directory()
        result = self.run_wrapper(subcommand="disable-embeddings")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("disable_embeddings_marker_removal_failed", result.stdout)
        self.assertNotIn("disable_embeddings_lock_unavailable", result.stdout)
        self.assertNotIn('"success": true', result.stdout)

    def test_success_never_emitted_on_marker_write_failure(self) -> None:
        """The backfill must not print its success envelope when the marker
        write fails (the failure happens before the success emit)."""
        self._marker_as_directory()
        result = self.run_wrapper(subcommand="embed-backfill")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('"action": "embed-backfill"', result.stdout)


class EmbeddingRefreshTimeoutBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_fake(self, body: str) -> Path:
        path = self.tmp / "fake-cmd"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _run_helper(self, cmd: Path, timeout: float = 1.0, grace: float = 0.3,
                    **extra) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["GBRAIN_EMBED_REFRESH_CMD"] = str(cmd)
        env["GBRAIN_EMBED_REFRESH_TIMEOUT"] = str(timeout)
        env["GBRAIN_EMBED_REFRESH_KILL_GRACE"] = str(grace)
        env.update(extra)
        return subprocess.run(
            ["python3", str(HELPER_PATH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_timeout_terminates_process_group_and_releases_lock(self) -> None:
        lock_file = self.tmp / "tasknotes.lock"
        pid_file = self.tmp / "child.pid"
        fake = self._write_fake(f"""#!/bin/sh
echo $$ > "{pid_file}"
exec 9<>"{lock_file}"
flock -n 9 || exit 7
sleep 60
""")
        result = self._run_helper(fake, timeout=1.0, grace=0.3)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertIn("timed out", result.stderr)

        # The flock held by the orphaned child must be released after the
        # whole process group is terminated.
        with open(lock_file, "r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        # The child itself must be gone (not orphaned).
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"orphaned child {child_pid} still alive after timeout")

    def test_timeout_releases_lock_even_when_child_ignores_term(self) -> None:
        lock_file = self.tmp / "tasknotes.lock"
        fake = self._write_fake(f"""#!/bin/sh
exec 9<>"{lock_file}"
flock -n 9 || exit 7
trap '' TERM
sleep 60
""")
        result = self._run_helper(fake, timeout=1.0, grace=0.3)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        with open(lock_file, "r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def test_leader_exits_on_term_while_grandchild_ignores_term_and_holds_flock(
        self,
    ) -> None:
        """Regression: the leader process exits on SIGTERM, so proc.poll()
        turns non-None, but a grandchild that ignores SIGTERM and holds the
        flock keeps the process group alive. The helper must check the GROUP
        (not the leader) and escalate to SIGKILL so the flock is released and
        the grandchild is reaped."""
        lock_file = self.tmp / "tasknotes.lock"
        gc_pid_file = self.tmp / "grandchild.pid"
        fake = self._write_fake(f"""#!/bin/sh
sh -c 'trap "" TERM; exec 9<>"{lock_file}"; flock -n 9 || exit 9; echo $$ > "{gc_pid_file}"; while :; do sleep 1; done' &
echo $! > "{gc_pid_file}"
sleep 60
""")
        result = self._run_helper(fake, timeout=1.0, grace=0.3)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertIn("timed out", result.stderr)

        # The flock held by the TERM-ignoring grandchild must be released once
        # the whole process group is SIGKILLed.
        with open(lock_file, "r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        # The grandchild itself must be gone (not orphaned).
        gc_pid = int(gc_pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(gc_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"orphaned grandchild {gc_pid} still alive after timeout")

    def test_cleanup_finishes_before_simulated_hermes_outer_timeout(self) -> None:
        """The constrained helper budget must finish group cleanup before the
        simulated Hermes subprocess.run timeout boundary, rather than letting
        the outer timeout kill the helper mid-cleanup."""
        lock_file = self.tmp / "tasknotes-boundary.lock"
        fake = self._write_fake(f"""#!/bin/sh
exec 9<>"{lock_file}"
flock -n 9 || exit 7
sleep 60
""")
        env = os.environ.copy()
        env.update(
            {
                "GBRAIN_EMBED_REFRESH_CMD": str(fake),
                "HERMES_CRON_SCRIPT_TIMEOUT": "3",
                "GBRAIN_EMBED_REFRESH_TIMEOUT": "0.2",
                "GBRAIN_EMBED_REFRESH_KILL_GRACE": "0.1",
                "GBRAIN_EMBED_REFRESH_GROUP_DRAIN": "0.1",
                "GBRAIN_EMBED_REFRESH_TIMEOUT_MARGIN": "0.2",
            }
        )
        try:
            result = subprocess.run(
                ["python3", str(HELPER_PATH)],
                env=env,
                capture_output=True,
                text=True,
                timeout=2.8,
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - assertion path
            self.fail(f"outer Hermes timeout preempted helper cleanup: {exc}")
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        with open(lock_file, "r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # --- through the cron entrypoint (timeout hierarchy alignment) ---
    #
    # The cron script computes safe_timeout = outer - grace - drain - margin - 1
    # and caps requested_timeout with `-ge safe_timeout`, which equals the
    # helper's `maximum`. These tests run the real cron entrypoint so the
    # capped value must be ACCEPTED by the helper (equality allowed), while a
    # direct invocation above the maximum is still rejected.

    def _run_cron(self, lock_file: Path, pid_file: Path, requested_timeout: int,
                  boundary: float) -> subprocess.CompletedProcess:
        fake = self._write_fake(f"""#!/bin/sh
exec 9<>"{lock_file}"
flock -n 9 || exit 7
echo $$ > "{pid_file}"
trap '' TERM
sleep 60
""")
        env = os.environ.copy()
        env.update(
            {
                "GBRAIN_EMBED_REFRESH_CMD": str(fake),
                "GBRAIN_EMBED_REFRESH_HELPER": str(HELPER_PATH),
                # outer=5, grace=1, drain=1, margin=1 -> safe_timeout=1
                "HERMES_CRON_SCRIPT_TIMEOUT": "5",
                "GBRAIN_EMBED_REFRESH_TIMEOUT": str(requested_timeout),
                "GBRAIN_EMBED_REFRESH_KILL_GRACE": "1",
                "GBRAIN_EMBED_REFRESH_GROUP_DRAIN": "1",
                "GBRAIN_EMBED_REFRESH_TIMEOUT_MARGIN": "1",
            }
        )
        try:
            return subprocess.run(
                ["bash", str(CRON_SCRIPT_PATH)],
                env=env,
                capture_output=True,
                text=True,
                timeout=boundary,
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - assertion path
            self.fail(f"outer Hermes timeout preempted cron cleanup: {exc}")

    def _assert_lock_released(self, lock_file: Path) -> None:
        with open(lock_file, "r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _assert_lock_holder_gone(self, pid_file: Path) -> None:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"lock holder {pid} still alive after timeout cleanup")

    def test_cron_entrypoint_exact_boundary_requested_timeout(self) -> None:
        """requested == safe_timeout: the cron caps (no change) and the helper
        must accept the capped value (timeout == maximum), run its own
        timeout, and clean up the TERM-ignoring lock holder before the
        simulated Hermes outer boundary fires."""
        lock_file = self.tmp / "cron-exact.lock"
        pid_file = self.tmp / "cron-exact.pid"
        result = self._run_cron(lock_file, pid_file, requested_timeout=1, boundary=4.0)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertIn("timed out", result.stderr)
        self._assert_lock_released(lock_file)
        self._assert_lock_holder_gone(pid_file)

    def test_cron_entrypoint_over_boundary_requested_timeout(self) -> None:
        """requested > safe_timeout: the cron caps it down to safe_timeout, the
        helper accepts the capped value, and the TERM-ignoring lock holder is
        cleaned up before the simulated Hermes outer boundary fires."""
        lock_file = self.tmp / "cron-over.lock"
        pid_file = self.tmp / "cron-over.pid"
        result = self._run_cron(lock_file, pid_file, requested_timeout=3, boundary=4.0)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertIn("timed out", result.stderr)
        self._assert_lock_released(lock_file)
        self._assert_lock_holder_gone(pid_file)

    def test_helper_forwards_child_exit_code(self) -> None:
        fake = self._write_fake("#!/bin/sh\nexit 3\n")
        result = self._run_helper(fake, timeout=5.0, grace=0.3)
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("timed out", result.stderr)

    def test_helper_success_path(self) -> None:
        fake = self._write_fake("#!/bin/sh\nexit 0\n")
        result = self._run_helper(fake, timeout=5.0, grace=0.3)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
