"""Source-contract tests for scripts/josemar-gbrain and the Docker gbrain wrapper.

These tests inspect the wrapper source (no Docker, no gbrain binary required)
to guard the simplified direct-CLI gbrain integration:

  - reindex performs initial activation (init, config, sync, extract,
    extract links, schema sync for custom packs, git safe.directory)
  - refresh performs lightweight vault-file reconciliation without init/schema
  - refresh and reindex self-acquire the shared tasknotes lock (issue #110)
    unless the lock runner already holds it and passed the validated lock fd
    (TASKNOTES_LOCK_FD)
  - schema pack install logic for custom packs (source path resolution,
    confinement validation, atomic install, native validate)
  - no readiness marker, no gate, no provider stripping for chat actions,
    no per-action functions, no output capping, no timeout resolution
  - the private native wrapper (/opt/josemar/libexec/gbrain-native) must cd to /opt/gbrain
  - Bun installer stage must apt-install curl
  - GBRAIN_HOME is a parent; state lives under $GBRAIN_HOME/.gbrain
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATH = REPO_ROOT / "scripts" / "josemar-gbrain"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile.hermes"
HERMES_INIT_PATH = REPO_ROOT / "docker-hermes-init.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """Extract a shell function body by name."""
    header = re.compile(rf"^{name}\(\)\s*\{{", re.MULTILINE)
    start = header.search(src)
    assert start is not None, f"Could not find function {name}"
    body_start = start.end()
    close = re.compile(r"^}$", re.MULTILINE)
    match = close.search(src, body_start)
    assert match is not None, f"Could not find end of function {name}"
    return src[body_start:match.start()]


class GbrainSchemaSourcePackContractTests(unittest.TestCase):
    """Schema source pack install contract tests."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_schema_source_root_env_var(self) -> None:
        self.assertIn('GBRAIN_SCHEMA_SOURCE_ROOT', self.src)

    def test_bundled_packs_constant(self) -> None:
        self.assertIn('BUNDLED_PACKS', self.src)
        self.assertIn('gbrain-base-v2', self.src)

    def test_is_bundled_pack_function(self) -> None:
        body = _extract_function(self.src, "is_bundled_pack")
        self.assertIn("BUNDLED_PACKS", body)

    def test_source_pack_path_function(self) -> None:
        body = _extract_function(self.src, "source_pack_path")
        self.assertIn("pack.yaml", body)
        self.assertIn("pack.yml", body)
        self.assertIn("pack.json", body)

    def test_validate_source_pack_path_rejects_symlinks(self) -> None:
        body = _extract_function(self.src, "validate_source_pack_path")
        self.assertIn("symlink", body.lower())

    def test_validate_source_pack_path_rejects_path_escape(self) -> None:
        body = _extract_function(self.src, "validate_source_pack_path")
        self.assertIn("path_escape", body.lower())

    def test_install_source_pack_function(self) -> None:
        body = _extract_function(self.src, "install_source_pack")
        self.assertIn("mkdir -p", body)
        self.assertIn("cp ", body)

    def test_validate_installed_pack_function(self) -> None:
        body = _extract_function(self.src, "validate_installed_pack")
        self.assertIn("schema validate", body)

    def test_install_source_pack_removes_stale_alternates(self) -> None:
        """Nice-to-have: install removes stale alternate extension files."""
        body = _extract_function(self.src, "install_source_pack")
        self.assertIn("stale", body.lower())

    def test_install_source_pack_uses_atomic_copy(self) -> None:
        """Nice-to-have: install uses temp file + atomic rename."""
        body = _extract_function(self.src, "install_source_pack")
        self.assertIn("tmp", body.lower())
        self.assertIn("mv ", body)

    def test_validate_schema_pack_name_function(self) -> None:
        """Nice-to-have: validate_schema_pack_name rejects invalid names."""
        body = _extract_function(self.src, "validate_schema_pack_name")
        self.assertIn("invalid_schema_pack_name", body)


class GbrainReindexActivationContractTests(unittest.TestCase):
    """reindex performs initial activation via the native gbrain CLI."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_reindex_performs_init(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("init --pglite --no-embedding", body)

    def test_reindex_configures_repo_path(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("sync.repo_path", body)
        self.assertIn("$GBRAIN_BRAIN_REPO", body)

    def test_reindex_configures_global_basename(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("config set link_resolution.global_basename true", body)

    def test_reindex_configures_keyword_only(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("config set search.mcp_keyword_only true", body)

    def test_reindex_configures_chronicle_judge_token_budget(self) -> None:
        """Chronicle uses the supported upstream config rather than a source patch."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("config set chronicle.judge_max_tokens 8000", body)

    def test_reindex_runs_full_sync(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("run_sync_extract_links full", body)

    def test_reindex_full_sync_helper_passes_full_flag(self) -> None:
        """The shared helper must invoke sync with --full when called with 'full'."""
        body = _extract_function(self.src, "run_sync_extract_links")
        self.assertIn('"full"', body)
        self.assertIn("sync_full_flag=\"--full\"", body)
        self.assertIn("sync $sync_full_flag --no-embed --yes --no-pull --json", body)
        self.assertIn("--repo", body)
        self.assertIn("$GBRAIN_BRAIN_REPO", body)

    def test_reindex_runs_extract_stale(self) -> None:
        body = _extract_function(self.src, "run_sync_extract_links")
        self.assertIn("extract --stale", body)

    def test_reindex_runs_extract_links(self) -> None:
        """reindex must run `extract links --source db` to populate the link graph."""
        body = _extract_function(self.src, "run_sync_extract_links")
        self.assertIn("extract links", body)
        self.assertIn("--source db", body)

    def test_reindex_extract_links_after_extract_stale(self) -> None:
        """extract links must run AFTER extract --stale."""
        body = _extract_function(self.src, "run_sync_extract_links")
        stale_pos = body.find("extract --stale")
        links_pos = body.find("extract links")
        self.assertLess(stale_pos, links_pos,
                        "extract links must follow extract --stale")

    def test_reindex_extract_links_failure_returns_nonzero(self) -> None:
        """extract links failure must emit gbrain_extract_links_failed and return 1."""
        body = _extract_function(self.src, "run_sync_extract_links")
        self.assertIn("gbrain_extract_links_failed", body)

    def test_reindex_does_not_pass_schema_pack_flag(self) -> None:
        match = re.search(r'init_output=\$\(.*?init\b(.*?)2>&1\)', self.src, re.DOTALL)
        self.assertIsNotNone(match, "Could not find init invocation in wrapper")
        assert match is not None
        init_args = match.group(1)
        self.assertNotIn("--schema-pack", init_args)

    def test_reindex_does_not_pass_non_interactive_flag(self) -> None:
        match = re.search(r'init_output=\$\(.*?init\b(.*?)2>&1\)', self.src, re.DOTALL)
        self.assertIsNotNone(match, "Could not find init invocation in wrapper")
        assert match is not None
        init_args = match.group(1)
        self.assertNotIn("--non-interactive", init_args)

    def test_reindex_exports_skip_startup_hooks(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("export_gbrain_env", body)

    def test_reindex_installs_custom_pack(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("is_bundled_pack", body)
        self.assertIn("source_pack_path", body)
        self.assertIn("install_source_pack", body)
        self.assertIn("validate_installed_pack", body)

    def test_reindex_fails_on_missing_custom_source(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("schema_source_missing", body)

    def test_reindex_schema_install_before_init(self) -> None:
        """schema pack install must happen BEFORE gbrain init."""
        body = _extract_function(self.src, "do_reindex")
        install_pos = body.find("install_source_pack")
        init_pos = body.find("init --pglite")
        self.assertLess(install_pos, init_pos,
                        "schema pack install must precede gbrain init")

    def test_reindex_schema_install_before_sync(self) -> None:
        """schema pack install must happen BEFORE gbrain sync."""
        body = _extract_function(self.src, "do_reindex")
        install_pos = body.find("install_source_pack")
        sync_pos = body.find("run_sync_extract_links")
        self.assertLess(install_pos, sync_pos,
                        "schema pack install must precede gbrain sync")

    def test_reindex_schema_install_before_extract(self) -> None:
        """schema pack install must happen BEFORE gbrain extract."""
        body = _extract_function(self.src, "do_reindex")
        install_pos = body.find("install_source_pack")
        extract_pos = body.find("run_sync_extract_links")
        self.assertLess(install_pos, extract_pos,
                        "schema pack install must precede gbrain extract")

    def test_reindex_schema_sync_apply_after_file_sync(self) -> None:
        """native schema sync --apply must run after file sync."""
        body = _extract_function(self.src, "do_reindex")
        sync_pos = body.find("run_sync_extract_links")
        schema_sync_pos = body.find("schema sync --apply")
        self.assertLess(sync_pos, schema_sync_pos,
                        "schema sync --apply must follow file sync")

    def test_reindex_schema_sync_failure_returns_nonzero(self) -> None:
        """schema sync failure must fail reindex."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("schema_sync_failed", body)

    def test_reindex_schema_sync_only_for_custom_pack(self) -> None:
        """schema sync --apply must only run when a custom pack is in use."""
        body = _extract_function(self.src, "do_reindex")
        schema_sync_pos = body.find("schema sync --apply")
        # The schema sync block must be guarded by is_bundled_pack check.
        bundled_check_pos = body.rfind("is_bundled_pack", 0, schema_sync_pos)
        self.assertLess(bundled_check_pos, schema_sync_pos,
                        "schema sync --apply must be guarded by is_bundled_pack")

    def test_reindex_validates_schema_pack_name(self) -> None:
        """reindex validates GBRAIN_SCHEMA_PACK name."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("validate_schema_pack_name", body)

    def test_reindex_sets_git_safe_directory(self) -> None:
        """reindex must mark the vault repo as a git safe.directory."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("safe.directory", body)
        self.assertIn("$GBRAIN_BRAIN_REPO", body)

    def test_reindex_sets_git_safe_directory_before_sync(self) -> None:
        """safe.directory must be configured before native sync/extract."""
        body = _extract_function(self.src, "do_reindex")
        safe_pos = body.find("mark_brain_repo_safe_directory")
        sync_pos = body.find("run_sync_extract_links")
        extract_pos = sync_pos
        self.assertLess(safe_pos, sync_pos)
        self.assertLess(safe_pos, extract_pos)

    def test_reindex_drops_root_to_hermes_when_possible(self) -> None:
        self.assertIn("drop_root_if_possible", self.src)
        # The drop is unconditional: no env sentinel may skip it, and the
        # su re-exec must not export one.
        self.assertNotIn("JOSEMAR_GBRAIN_DROPPED_PRIVS", self.src)
        # The drop re-exec uses the fixed su path, not PATH resolution.
        self.assertIn('"$SU_BIN" -p -s /bin/sh -- hermes', self.src)
        self.assertNotIn("su -p -s /bin/sh -- hermes", self.src.replace('"$SU_BIN" ', ""))

    def test_root_drop_fails_closed_and_preserves_runtime_environment(self) -> None:
        body = _extract_function(self.src, "drop_root_if_possible")
        self.assertIn("runtime_identity_unavailable", body)
        self.assertIn('export HOME="$runtime_home"', body)
        self.assertIn('export HERMES_HOME="$runtime_hermes_home"', body)
        self.assertIn('export GBRAIN_HOME="$runtime_gbrain_home"', body)
        self.assertIn('export XDG_CONFIG_HOME="$runtime_xdg_config"', body)
        self.assertIn('runtime_home="${HERMES_HOME:-/opt/data}"', body)
        # GBRAIN_HOME is a fixed constant; the su re-exec passes it through
        # verbatim without any env-default read.
        self.assertIn('runtime_gbrain_home="$GBRAIN_HOME"', body)
        self.assertNotIn('runtime_gbrain_home="${GBRAIN_HOME:-', body)

    def test_reindex_creates_state_dir(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn('mkdir -p "$GBRAIN_STATE_DIR"', body)

    def test_reindex_persists_schema_pack_marker_atomically(self) -> None:
        """The active schema pack marker must be written atomically: temp
        file in the SAME directory, fsync, then rename (replacing any stale
        marker), plus a directory fsync for durability."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("tempfile.mkstemp(dir=parent", body)
        self.assertIn("os.fsync(fd)", body)
        self.assertIn("os.replace(tmp, path)", body)
        self.assertIn('os.fsync(dfd)', body)

    def test_reindex_marker_write_is_required_not_best_effort(self) -> None:
        """A marker write/replace failure must be a structured nonzero error:
        reindex must NOT report success without the marker. The old
        best-effort warning path must be gone."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("schema_pack_marker_write_failed", body)
        self.assertIn("return 1", body)
        self.assertNotIn("WARNING: could not persist active schema pack marker", body)
        # The failure must precede the success envelope.
        fail_pos = body.find("schema_pack_marker_write_failed")
        success_pos = body.find('"success": True')
        self.assertGreater(fail_pos, -1)
        self.assertGreater(success_pos, -1)
        self.assertLess(fail_pos, success_pos,
                        "marker failure must be reported before any success")

    def test_reindex_marker_path_safety_is_fail_closed(self) -> None:
        """A symlinked marker or symlinked parent must be refused (no clobber
        of a decoy target)."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("os.path.islink(path)", body)
        self.assertIn("os.path.islink(parent)", body)


class GbrainRefreshContractTests(unittest.TestCase):
    """refresh reconciles manual vault-file edits without activation work."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_refresh_dispatch_exists(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertIn("refresh)", body)
        self.assertIn("do_refresh", body)

    def test_refresh_does_not_run_init_or_schema(self) -> None:
        body = _extract_function(self.src, "do_refresh")
        self.assertNotIn("init --pglite", body)
        self.assertNotIn("schema sync --apply", body)
        self.assertNotIn("install_source_pack", body)

    def test_refresh_runs_sync_extract_links(self) -> None:
        body = _extract_function(self.src, "do_refresh")
        self.assertIn("run_sync_extract_links", body)

    def test_refresh_runs_incremental_sync(self) -> None:
        """refresh must call the shared helper WITHOUT the 'full' flag."""
        body = _extract_function(self.src, "do_refresh")
        self.assertIn("run_sync_extract_links", body)
        self.assertNotIn("run_sync_extract_links full", body)

    def test_refresh_helper_omits_full_flag_by_default(self) -> None:
        """The shared helper must default to incremental sync (no --full)."""
        body = _extract_function(self.src, "run_sync_extract_links")
        # The full flag must only be set when $1 == "full"; default is empty.
        self.assertIn('"${1:-}" = "full"', body)
        self.assertIn('sync_full_flag=""', body)

    def test_refresh_helper_does_not_hardcode_full(self) -> None:
        """Regression: the shared sync helper must not hardcode --full for refresh.

        A previous defect had run_sync_extract_links always pass --full, making
        the lightweight five-minute refresh a full sync. This guards against any
        shared helper reintroducing --full unconditionally.
        """
        body = _extract_function(self.src, "run_sync_extract_links")
        # The only literal --full in the helper must be the conditional assignment.
        self.assertEqual(body.count('"--full"'), 1, "exactly one '--full' literal expected")
        self.assertNotIn("sync --full ", body, "sync must not hardcode --full")
        self.assertNotIn("sync --full--no-embed", body)

    def test_refresh_message_mentions_embeddings_skipped(self) -> None:
        body = _extract_function(self.src, "do_refresh")
        self.assertIn("Embeddings skipped", body)

    def test_refresh_does_not_invoke_embed(self) -> None:
        """refresh must remain no-embedding; it must not invoke `gbrain embed`."""
        body = _extract_function(self.src, "do_refresh")
        self.assertNotIn("embed --stale", body)
        self.assertNotIn("embed-backfill", body)

    def test_refresh_does_not_require_embedding_env(self) -> None:
        """refresh must not require GBRAIN_EMBEDDING_* env vars."""
        body = _extract_function(self.src, "do_refresh")
        self.assertNotIn("GBRAIN_EMBEDDING_MODEL", body)
        self.assertNotIn("GBRAIN_EMBEDDING_DIMENSIONS", body)

    def test_refresh_acquires_tasknotes_lock(self) -> None:
        """refresh must self-acquire the shared tasknotes lock so direct
        manual runs cannot open PGLite unprotected (issue #110)."""
        body = _extract_function(self.src, "do_refresh")
        self.assertIn("acquire_tasknotes_lock refresh", body)


class GbrainTasknotesLockAcquisitionContractTests(unittest.TestCase):
    """refresh/reindex self-lock through acquire_tasknotes_lock (issue #110).

    The helper must skip only when the lock runner's inherited lock fd is
    validated (TASKNOTES_LOCK_FD + flock actually held, cron chains), fail
    closed when the lock file is unavailable, and acquire the flock
    nonblocking so a busy lock never starts gbrain work. No env boolean can
    forge the skip.
    """

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_acquire_helper_exists(self) -> None:
        self.assertIn("acquire_tasknotes_lock()", self.src)

    def test_acquire_skips_only_when_inherited_lock_fd_validated(self) -> None:
        body = _extract_function(self.src, "run_under_lock")
        self.assertIn("lock_held_by_runner", body)
        self.assertNotIn("TASKNOTES_LOCK_HELD", body)

    def test_inherited_lock_fd_validation_helper_exists(self) -> None:
        body = _extract_function(self.src, "lock_held_by_runner")
        self.assertIn("TASKNOTES_LOCK_FD", body)
        self.assertIn("fstat", body)
        self.assertIn("fdinfo", body)
        self.assertIn("FLOCK", body)

    def test_inherited_lock_fd_requires_exclusive_write_state(self) -> None:
        """Only an EXCLUSIVE flock (fdinfo 'FLOCK ... WRITE') validates; a
        shared LOCK_SH lock shows READ and must not satisfy the check."""
        body = _extract_function(self.src, "lock_held_by_runner")
        self.assertIn('"FLOCK" in line and "WRITE" in line', body)
        self.assertIn("WRITE", body)

    def test_lock_open_is_safe_creation_with_no_follow_verification(self) -> None:
        """The lock open must create the file on fresh installs (O_CREAT via
        `exec 9<>`) and verify the inode against a no-follow open so a
        symlinked lock is refused — no check→open TOCTOU."""
        body = _extract_function(self.src, "open_tasknotes_lock_fd")
        self.assertIn('exec 9<>"$GBRAIN_TASKNOTES_LOCK"', body)
        self.assertIn("O_NOFOLLOW", body)
        self.assertIn("fstat(9)", body)
        self.assertIn("mkdir -p", body)
        self.assertNotIn("[ ! -r", body)

    def test_lock_acquired_nonblocking_then_reenters_through_runner(self) -> None:
        """run_under_lock must flock fd 9 nonblocking and, once held,
        exec-replace itself with the fixed lock runner passing the inherited
        fd — the flock is never released between wrapper and runner."""
        body = _extract_function(self.src, "run_under_lock")
        self.assertIn('"$FLOCK_BIN" -n 9', body)
        self.assertIn("TASKNOTES_LOCK_FD=9", body)
        self.assertIn('exec "$PYTHON_BIN" -I "$TASKNOTES_LOCK_RUNNER"', body)
        self.assertIn('--lock-path "$GBRAIN_TASKNOTES_LOCK"', body)
        self.assertIn('-- "$0" "$action"', body)

    def test_acquire_fails_closed_when_lock_unavailable(self) -> None:
        body = _extract_function(self.src, "open_tasknotes_lock_fd")
        self.assertIn("_lock_unavailable", body)

    def test_acquire_reports_busy_lock(self) -> None:
        body = _extract_function(self.src, "acquire_tasknotes_lock")
        self.assertIn("_lock_busy", body)

    def test_production_paths_are_fixed_constants(self) -> None:
        """GBRAIN_BIN and GBRAIN_TASKNOTES_LOCK must be fixed constants, not
        env-overridable: they are binary/lock security boundaries."""
        self.assertIn('GBRAIN_BIN="/opt/josemar/libexec/gbrain-native"', self.src)
        self.assertNotIn('GBRAIN_BIN="/usr/local/bin/gbrain"', self.src)
        self.assertIn(
            'GBRAIN_TASKNOTES_LOCK="/opt/data/.locks/tasknotes.lock"', self.src
        )
        self.assertNotIn('GBRAIN_BIN="${GBRAIN_BIN:-', self.src)
        self.assertNotIn('GBRAIN_TASKNOTES_LOCK="${GBRAIN_TASKNOTES_LOCK:-', self.src)

    def test_fixed_tool_paths_no_path_resolution(self) -> None:
        """python3/flock/su/getent/id must be fixed paths (no PATH injection)
        for everything that touches the lock, executes gbrain, or drops
        privileges."""
        self.assertIn('PYTHON_BIN="/opt/hermes/.venv/bin/python3"', self.src)
        self.assertIn('FLOCK_BIN="/usr/bin/flock"', self.src)
        self.assertIn('SU_BIN="/usr/bin/su"', self.src)
        self.assertIn('GETENT_BIN="/usr/bin/getent"', self.src)
        self.assertIn('ID_BIN="/usr/bin/id"', self.src)
        self.assertNotIn("command -v python3", self.src)
        self.assertNotIn("command -v su", self.src)
        # No bare PATH-resolved invocations of the security-relevant tools.
        self.assertNotIn("\"python3\" - ", self.src)
        self.assertNotIn(" flock -n 9", self.src)
        self.assertNotIn("su -p", self.src)

    def test_no_dropped_privs_env_sentinel(self) -> None:
        """Root must always drop before the lock; no env flag may claim the
        drop already happened."""
        self.assertNotIn("JOSEMAR_GBRAIN_DROPPED_PRIVS", self.src)

    def test_chat_policy_header_routes_through_adapter(self) -> None:
        """The wrapper header must not claim chat calls the bare gbrain CLI
        directly: chat-facing actions run through the gbrain-chat-run
        adapter (issue #110)."""
        self.assertIn("gbrain-chat-run", self.src.split("set -eu")[0])
        self.assertNotIn(
            "invoked directly via the `gbrain` CLI", self.src.split("set -eu")[0]
        )


class GbrainRefreshEmbeddingsContractTests(unittest.TestCase):
    """refresh-embeddings is the explicit-request / daily stale-only embed path.

    The wrapper owns the TaskNotes lock itself (not the cron entrypoint),
    reads `search.mcp_keyword_only` through the exact stdout of
    `gbrain config get search.mcp_keyword_only` while holding that lock (NOT
    from config.json), requires the exact value `false`, then runs the
    marker-guarded stale-only embed at concurrency 1.
    """

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    # --- usage, dispatch, privilege drop ---

    def test_usage_includes_refresh_embeddings(self) -> None:
        body = _extract_function(self.src, "usage")
        self.assertIn("refresh-embeddings", body)

    def test_main_dispatches_refresh_embeddings(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertIn("refresh-embeddings)", body)
        self.assertIn("do_refresh_embeddings", body)

    def test_main_dispatch_drops_root_for_refresh_embeddings(self) -> None:
        body = _extract_function(self.src, "main")
        drop_pos = body.find("drop_root_if_possible refresh-embeddings")
        do_pos = body.find("do_refresh_embeddings")
        self.assertGreater(drop_pos, -1, "refresh-embeddings must drop root privileges")
        self.assertGreater(do_pos, -1, "do_refresh_embeddings must be dispatched")
        self.assertLess(drop_pos, do_pos,
                        "drop_root_if_possible must precede do_refresh_embeddings")

    # --- keyword_only read through exact gbrain stdout under the lock ---

    def test_reads_keyword_only_via_gbrain_config_get(self) -> None:
        """The semantic-mode gate must read `gbrain config get
        search.mcp_keyword_only`, never config.json."""
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn('"$GBRAIN_BIN" config get search.mcp_keyword_only', body)

    def test_does_not_read_keyword_only_from_config_json(self) -> None:
        """config.json is only used for the sentinel/marker checks; the
        mcp_keyword_only decision must not come from it."""
        body = _extract_function(self.src, "do_refresh_embeddings")
        config_block = body[body.find("validation=$("):body.find('esac', body.find("validation=$("))]
        self.assertNotIn("mcp_keyword_only", config_block)
        self.assertNotIn('config.get("search")', config_block)

    def test_keyword_only_exact_false_required(self) -> None:
        """Any stdout other than exactly `false` must fail closed as
        semantic_mode_invalid (only exact `false` means semantic mode)."""
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn('if [ "$keyword_only" = "true" ]; then', body)
        self.assertIn('if [ "$keyword_only" != "false" ]; then', body)
        self.assertIn("refresh_embeddings_semantic_mode_invalid", body)

    def test_keyword_only_true_skips(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn('"keyword_only"', body)
        self.assertIn('"skipped"', body)

    def test_config_read_failure_is_structured(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn("refresh_embeddings_config_read_failed", body)
        self.assertIn('"success": false', body)

    # --- lock ownership and command order ---

    def test_lock_owned_here_not_by_cron_entrypoint(self) -> None:
        """The shared lock must be acquired inside the wrapper so the cron
        entrypoint needs no external lock."""
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn("run_under_lock refresh-embeddings", body)
        self.assertIn("GBRAIN_TASKNOTES_LOCK", self.src)

    def test_lock_non_nested_not_double_acquired(self) -> None:
        """do_refresh_embeddings must go through run_under_lock (single
        safe open of fd 9 + single flock -n + runner re-entry) — it must NOT
        use the nested `{ ... } 9<"$GBRAIN_TASKNOTES_LOCK"` compound-block
        pattern (that would double-open/double-lock and deadlock)."""
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn("run_under_lock refresh-embeddings refresh_embeddings", body)
        self.assertNotIn('} 9<"$GBRAIN_TASKNOTES_LOCK"', body)

    def test_config_get_after_lock_acquisition(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        lock_pos = body.find("run_under_lock")
        config_pos = body.find("config get search.mcp_keyword_only")
        self.assertGreater(lock_pos, -1)
        self.assertGreater(config_pos, -1)
        self.assertLess(lock_pos, config_pos,
                        "config get must run while holding the lock")

    def test_lock_busy_skips_without_gbrain_access(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn('"reason": "lock_busy"', body)
        # The skip must return before any gbrain command runs.
        busy_pos = body.find("lock_busy")
        config_pos = body.find("config get search.mcp_keyword_only")
        self.assertLess(busy_pos, config_pos,
                        "lock-busy skip must precede the config read")

    def test_lock_unavailable_is_structured_error(self) -> None:
        """The safe open must emit the structured ${err_tag}_lock_unavailable
        JSON (per-action tag, e.g. refresh_embeddings for the
        refresh-embeddings path) and never a bare shell death."""
        body = _extract_function(self.src, "open_tasknotes_lock_fd")
        self.assertIn("${err_tag}_lock_unavailable", body)
        self.assertIn("\\\"success\\\": false", body)
        self.assertIn("Could not open the tasknotes lock file.", body)

    def test_lock_openability_probe_keeps_failure_in_shell(self) -> None:
        """A failed `exec` redirection would kill the shell, so the safe open
        must probe openability in a subshell first and emit the structured
        lock_unavailable error from the probe; the authoritative no-follow
        verification then guards the real open."""
        body = _extract_function(self.src, "open_tasknotes_lock_fd")
        probe_pos = body.find("( : ) 9<>")
        exec_pos = body.find('exec 9<>"$GBRAIN_TASKNOTES_LOCK"')
        self.assertGreater(probe_pos, -1)
        self.assertGreater(exec_pos, -1)
        self.assertLess(probe_pos, exec_pos,
                        "openability probe must precede the real exec 9<>")

    # --- validation and lifecycle gates ---

    def test_marker_checked_after_config_get(self) -> None:
        """The completion-marker gate must run after the semantic-mode read."""
        body = _extract_function(self.src, "do_refresh_embeddings")
        config_pos = body.find("config get search.mcp_keyword_only")
        marker_pos = body.find("completion_marker_missing")
        self.assertLess(config_pos, marker_pos,
                        "marker gate must follow the semantic-mode read")

    def test_marker_gate_skips_when_missing(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn('"completion_marker_missing"', body)
        self.assertIn('"skipped"', body)

    def test_marker_tuple_mismatch_fails(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn("refresh_embeddings_marker_tuple_mismatch", body)
        self.assertIn('"success": false', body)

    def test_embedding_disabled_sentinel_gate(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn("embedding_disabled", body)
        self.assertIn('"skipped"', body)

    # --- embed invocation and order ---

    def test_embed_after_sync(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        sync_pos = body.find("run_sync_extract_links")
        embed_pos = body.find("embed --stale")
        self.assertGreater(sync_pos, -1)
        self.assertGreater(embed_pos, -1)
        self.assertLess(sync_pos, embed_pos,
                        "embed must run after the sync/extract reconcile")

    def test_embed_runs_at_concurrency_one(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        embed_pos = body.find("embed --stale")
        cmd_start = body.rfind("\n", 0, embed_pos)
        cmd_line = body[cmd_start:embed_pos]
        self.assertIn("GBRAIN_EMBED_CONCURRENCY=1", cmd_line)

    def test_embed_failure_is_structured(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn("gbrain_embed_failed", body)
        self.assertIn('"success": false', body)

    def test_success_emitted_after_embed(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        embed_pos = body.find("embed --stale")
        success_pos = body.rfind('"success": true, "action": "refresh-embeddings"')
        self.assertGreater(embed_pos, -1)
        self.assertGreater(success_pos, -1)
        self.assertLess(embed_pos, success_pos,
                        "success JSON must follow the embed run")

    def test_does_not_init_or_migrate(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertNotIn("init --pglite", body)
        self.assertNotIn("migrate embeddings", body)

    def test_exports_gbrain_env_and_state_dir(self) -> None:
        body = _extract_function(self.src, "do_refresh_embeddings")
        self.assertIn("export_gbrain_env", body)
        self.assertIn('mkdir -p "$GBRAIN_STATE_DIR"', body)
        self.assertIn("mark_brain_repo_safe_directory", body)


class GbrainEmbedBackfillContractTests(unittest.TestCase):
    """embed-backfill is the operator-only one-shot embedding backfill (issue #65).

    Unlike reindex/refresh, this subcommand produces embeddings. These tests
    guard the source contract for every requirement point:
      (1) shell usage and main dispatch include it; privilege drop supports it
      (2) runs export_gbrain_env, state dir creation, and safe-directory setup
      (3) requires nonempty GBRAIN_EMBEDDING_MODEL and GBRAIN_EMBEDDING_DIMENSIONS
      (4) must not invoke init or reinit-pglite
      (5) acquires /opt/data/.locks/tasknotes.lock flock nonblocking before gbrain
      (6) invokes `gbrain embed --stale --include-null-signature` with
          GBRAIN_EMBED_CONCURRENCY=1
      (7) after success, runs dry-run equivalent to assert no stale remain
      (8) emits structured success JSON
    """

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    # --- (1) usage, dispatch, privilege drop ---

    def test_usage_includes_embed_backfill(self) -> None:
        body = _extract_function(self.src, "usage")
        self.assertIn("embed-backfill", body)

    def test_main_dispatches_embed_backfill(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertIn("embed-backfill)", body)
        self.assertIn("do_embed_backfill", body)

    def test_main_dispatch_drops_root_for_embed_backfill(self) -> None:
        body = _extract_function(self.src, "main")
        # The embed-backfill case must call drop_root_if_possible before do_embed_backfill.
        drop_pos = body.find("drop_root_if_possible embed-backfill")
        do_pos = body.find("do_embed_backfill")
        self.assertGreater(drop_pos, -1, "embed-backfill must drop root privileges")
        self.assertGreater(do_pos, -1, "do_embed_backfill must be dispatched")
        self.assertLess(drop_pos, do_pos,
                        "drop_root_if_possible must precede do_embed_backfill")

    def test_drop_root_passes_embed_backfill_subcommand(self) -> None:
        body = _extract_function(self.src, "drop_root_if_possible")
        # drop_root_if_possible re-execs with the subcommand name; the main
        # dispatch must pass 'embed-backfill' as that argument.
        main_body = _extract_function(self.src, "main")
        self.assertIn("drop_root_if_possible embed-backfill", main_body)

    # --- (2) env export, state dir, safe-directory ---

    def test_embed_backfill_exports_gbrain_env(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("export_gbrain_env", body)

    def test_embed_backfill_creates_state_dir(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn('mkdir -p "$GBRAIN_STATE_DIR"', body)

    def test_embed_backfill_sets_git_safe_directory(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("mark_brain_repo_safe_directory", body)

    def test_embed_backfill_env_before_lock(self) -> None:
        """export_gbrain_env and state dir setup must happen before gbrain access."""
        body = _extract_function(self.src, "do_embed_backfill")
        env_pos = body.find("export_gbrain_env")
        lock_pos = body.find("run_under_lock")
        self.assertLess(env_pos, lock_pos,
                        "export_gbrain_env must precede lock acquisition")

    # --- (3) prerequisites ---

    def test_embed_backfill_requires_embedding_model(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("GBRAIN_EMBEDDING_MODEL", body)

    def test_embed_backfill_requires_embedding_dimensions(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("GBRAIN_EMBEDDING_DIMENSIONS", body)

    def test_embed_backfill_prereq_check_is_nonempty(self) -> None:
        """The prerequisite check must use -z (nonempty) guards for both vars."""
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("-z", body)
        self.assertIn("GBRAIN_EMBEDDING_MODEL", body)
        self.assertIn("GBRAIN_EMBEDDING_DIMENSIONS", body)

    def test_embed_backfill_prereq_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("embed_backfill_prerequisites_missing", body)
        self.assertIn('"success": false', body)

    def test_embed_backfill_prereq_check_before_lock(self) -> None:
        """The prerequisite check must run before any lock acquisition."""
        body = _extract_function(self.src, "do_embed_backfill")
        prereq_pos = body.find("embed_backfill_prerequisites_missing")
        lock_pos = body.find("run_under_lock")
        self.assertLess(prereq_pos, lock_pos,
                        "prerequisite check must precede lock acquisition")

    def test_embed_backfill_prereq_failure_returns_nonzero(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        # The prereq block must return 1 on failure.
        prereq_block = body[:body.find("export_gbrain_env")]
        self.assertIn("return 1", prereq_block)

    # --- (4) must not init or reinit PGLite ---

    def test_embed_backfill_does_not_invoke_init(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertNotIn("init --pglite", body)
        self.assertNotIn("init ", body)

    def test_embed_backfill_does_not_reinit_pglite(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertNotIn("reinit", body)
        self.assertNotIn("reinit-pglite", body)

    def test_embed_backfill_does_not_run_schema_sync(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertNotIn("schema sync --apply", body)

    def test_embed_backfill_does_not_install_source_pack(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertNotIn("install_source_pack", body)

    # --- (5) tasknotes lock, nonblocking, before gbrain access ---

    def test_embed_backfill_references_tasknotes_lock_path(self) -> None:
        self.assertIn("/opt/data/.locks/tasknotes.lock", self.src)

    def test_embed_backfill_uses_tasknotes_lock_var(self) -> None:
        """The fixed lock path constant is used by the shared safe open."""
        body = _extract_function(self.src, "open_tasknotes_lock_fd")
        self.assertIn("$GBRAIN_TASKNOTES_LOCK", body)

    def test_embed_backfill_uses_flock_nonblocking(self) -> None:
        """The nonblocking flock lives in the shared run_under_lock helper."""
        body = _extract_function(self.src, "run_under_lock")
        self.assertIn('"$FLOCK_BIN" -n 9', body)

    def test_embed_backfill_lock_before_gbrain_embed(self) -> None:
        """The lock must be acquired before the `gbrain embed` invocation."""
        body = _extract_function(self.src, "do_embed_backfill")
        lock_pos = body.find("run_under_lock")
        embed_pos = body.find("embed --stale")
        self.assertGreater(lock_pos, -1, "run_under_lock must be present")
        self.assertGreater(embed_pos, -1, "embed --stale must be present")
        self.assertLess(lock_pos, embed_pos,
                        "flock acquisition must precede gbrain embed")

    def test_embed_backfill_lock_busy_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("embed_backfill_lock_busy", body)
        self.assertIn('"success": false', body)

    # --- (6) gbrain embed invocation with concurrency 1 ---

    def test_embed_backfill_invokes_gbrain_embed_stale(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("embed --stale", body)

    def test_embed_backfill_invokes_gbrain_embed_include_null_signature(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("--include-null-signature", body)

    def test_embed_backfill_sets_embed_concurrency_one(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("GBRAIN_EMBED_CONCURRENCY=1", body)

    def test_embed_backfill_concurrency_applied_to_embed(self) -> None:
        """GBRAIN_EMBED_CONCURRENCY=1 must be applied to the embed invocation."""
        body = _extract_function(self.src, "do_embed_backfill")
        # The concurrency env must appear in the same command line as `embed --stale`.
        # Find the embed invocation and check the concurrency prefix precedes it
        # within the same command substitution.
        embed_pos = body.find("embed --stale")
        self.assertGreater(embed_pos, -1)
        # Look backwards from embed_pos for the concurrency assignment.
        cmd_start = body.rfind("\n", 0, embed_pos)
        cmd_line = body[cmd_start:embed_pos]
        self.assertIn("GBRAIN_EMBED_CONCURRENCY=1", cmd_line)

    def test_embed_backfill_embed_failure_is_structured(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("gbrain_embed_failed", body)
        self.assertIn('"success": false', body)

    # --- (7) dry-run verification after success ---

    def test_embed_backfill_runs_dry_run_verify(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("--dry-run", body)

    def test_embed_backfill_dry_run_after_embed(self) -> None:
        """The dry-run verification must run AFTER the embed backfill."""
        body = _extract_function(self.src, "do_embed_backfill")
        embed_pos = body.find("embed --stale --include-null-signature")
        # The dry-run invocation is the second occurrence of embed --stale.
        dry_pos = body.find("--dry-run")
        self.assertGreater(embed_pos, -1)
        self.assertGreater(dry_pos, -1)
        self.assertLess(embed_pos, dry_pos,
                        "dry-run verification must follow the embed backfill")

    def test_embed_backfill_dry_run_uses_same_flags(self) -> None:
        """The dry-run must use the same --stale --include-null-signature flags."""
        body = _extract_function(self.src, "do_embed_backfill")
        # Count occurrences of the embed flags; should appear at least twice
        # (once for the real run, once for the dry-run verify).
        self.assertGreaterEqual(
            body.count("embed --stale --include-null-signature"), 2,
            "embed --stale --include-null-signature must appear for both "
            "the backfill and the dry-run verification",
        )

    def test_embed_backfill_dry_run_uses_concurrency_one(self) -> None:
        """The dry-run verification must also set GBRAIN_EMBED_CONCURRENCY=1."""
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertGreaterEqual(
            body.count("GBRAIN_EMBED_CONCURRENCY=1"), 2,
            "GBRAIN_EMBED_CONCURRENCY=1 must be set for both the backfill "
            "and the dry-run verification",
        )

    def test_embed_backfill_stale_remaining_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("embed_backfill_stale_remaining", body)
        self.assertIn('"success": false', body)

    def test_embed_backfill_verify_failure_is_structured(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("embed_backfill_verify_failed", body)

    # --- (8) structured success JSON ---

    def test_embed_backfill_emits_structured_success(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn('"success": true', body)
        self.assertIn('"action": "embed-backfill"', body)

    def test_embed_backfill_success_message_mentions_completion(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        # The success message must indicate the backfill completed and no stale remain.
        self.assertIn("no stale", body.lower())

    def test_embed_backfill_success_after_verify(self) -> None:
        """The success JSON must be emitted after the dry-run verification."""
        body = _extract_function(self.src, "do_embed_backfill")
        dry_pos = body.find("--dry-run")
        success_pos = body.find('"action": "embed-backfill"')
        self.assertGreater(dry_pos, -1)
        self.assertGreater(success_pos, -1)
        self.assertLess(dry_pos, success_pos,
                        "success JSON must be emitted after dry-run verification")

    # --- (9) marker write failure is explicit and structured ---

    def test_embed_backfill_marker_write_failure_is_structured(self) -> None:
        """A marker write failure must emit a structured
        embed_backfill_marker_write_failed error and exit nonzero — it must
        not claim success or fall through to the block-level
        lock_unavailable handler."""
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("embed_backfill_marker_write_failed", body)
        self.assertIn('"success": false', body)
        # The write must be guarded so a failure exits before the success emit.
        marker_pos = body.find("embed_backfill_marker_write_failed")
        success_pos = body.find('"action": "embed-backfill"')
        self.assertGreater(marker_pos, -1)
        self.assertGreater(success_pos, -1)
        self.assertLess(marker_pos, success_pos,
                        "marker write failure must precede any success JSON")


class GbrainEmbedBackfillPreservationContractTests(unittest.TestCase):
    """embed-backfill must not alter the behavior of reindex/refresh.

    reindex and refresh must remain no-embedding and keyword-only. The
    embedding-producing paths are do_embed_backfill (operator-only one-shot)
    and do_refresh_embeddings (explicit-request/daily stale-only refresh).
    """

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_reindex_still_uses_no_embedding_init(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("init --pglite --no-embedding", body)

    def test_reindex_still_sets_keyword_only(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("config set search.mcp_keyword_only true", body)

    def test_reindex_does_not_invoke_embed(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertNotIn("embed --stale", body)
        self.assertNotIn("embed-backfill", body)

    def test_reindex_does_not_require_embedding_env(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertNotIn("GBRAIN_EMBEDDING_MODEL", body)
        self.assertNotIn("GBRAIN_EMBEDDING_DIMENSIONS", body)

    def test_reindex_acquires_tasknotes_lock(self) -> None:
        """reindex must self-acquire the shared tasknotes lock so direct
        manual runs cannot open PGLite unprotected (issue #110)."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("acquire_tasknotes_lock reindex", body)

    def test_refresh_still_uses_no_embed_sync(self) -> None:
        body = _extract_function(self.src, "run_sync_extract_links")
        self.assertIn("--no-embed", body)

    def test_refresh_does_not_invoke_embed(self) -> None:
        body = _extract_function(self.src, "do_refresh")
        self.assertNotIn("embed --stale", body)
        self.assertNotIn("embed-backfill", body)

    def test_refresh_does_not_require_embedding_env(self) -> None:
        body = _extract_function(self.src, "do_refresh")
        self.assertNotIn("GBRAIN_EMBEDDING_MODEL", body)
        self.assertNotIn("GBRAIN_EMBEDDING_DIMENSIONS", body)

    def test_refresh_acquires_tasknotes_lock(self) -> None:
        """refresh must self-acquire the shared tasknotes lock so direct
        manual runs cannot open PGLite unprotected (issue #110)."""
        body = _extract_function(self.src, "do_refresh")
        self.assertIn("acquire_tasknotes_lock refresh", body)

    def test_embed_invocation_confined_to_embed_paths(self) -> None:
        """Only do_embed_backfill and do_refresh_embeddings may invoke
        `gbrain embed --stale`; reindex/refresh/sync helpers stay no-embed."""
        for func in ("do_reindex", "do_refresh", "run_sync_extract_links"):
            body = _extract_function(self.src, func)
            self.assertNotIn(
                "embed --stale", body,
                f"{func} must not invoke `gbrain embed --stale`",
            )
        for func in ("do_embed_backfill", "do_refresh_embeddings"):
            body = _extract_function(self.src, func)
            self.assertIn(
                "embed --stale", body,
                f"{func} must invoke `gbrain embed --stale`",
            )

    def test_embed_backfill_does_not_share_sync_helper(self) -> None:
        """embed-backfill must not route through run_sync_extract_links.

        The shared sync helper is no-embed by design; embed-backfill must use
        `gbrain embed` directly, not the sync/extract helper.
        """
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertNotIn("run_sync_extract_links", body)


class GbrainEnableEmbeddingsContractTests(unittest.TestCase):
    """enable-embeddings is the operator-only non-destructive semantic-search
    switch (issue #65).

    Unlike embed-backfill, this subcommand does NOT produce embeddings. It is
    the explicit, non-destructive switch from keyword-only to semantic search.
    These tests guard the source contract for every requirement point:
      (1) shell usage and main dispatch include it; privilege drop supports it
      (2) requires nonempty GBRAIN_EMBEDDING_MODEL and GBRAIN_EMBEDDING_DIMENSIONS
      (3) must not invoke init or reinit-pglite
      (4) acquires /opt/data/.locks/tasknotes.lock flock nonblocking before gbrain
      (5) invokes `gbrain migrate embeddings --to <model> --dim <dims> --yes
          --no-embed --ignore-env-override`
      (6) does NOT initiate backfill itself (no `gbrain embed --stale`)
      (7) ONLY after a successful migration sets search.mcp_keyword_only false
      (8) on migration failure keyword-only stays enabled (the keyword-only
          flip must come AFTER the migration success guard)
      (9) emits structured failure JSON for every failure path
      (10) emits structured success JSON
    """

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    # --- (1) usage, dispatch, privilege drop ---

    def test_usage_includes_enable_embeddings(self) -> None:
        body = _extract_function(self.src, "usage")
        self.assertIn("enable-embeddings", body)

    def test_main_dispatches_enable_embeddings(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertIn("enable-embeddings)", body)
        self.assertIn("do_enable_embeddings", body)

    def test_main_dispatch_drops_root_for_enable_embeddings(self) -> None:
        body = _extract_function(self.src, "main")
        drop_pos = body.find("drop_root_if_possible enable-embeddings")
        do_pos = body.find("do_enable_embeddings")
        self.assertGreater(drop_pos, -1, "enable-embeddings must drop root privileges")
        self.assertGreater(do_pos, -1, "do_enable_embeddings must be dispatched")
        self.assertLess(drop_pos, do_pos,
                        "drop_root_if_possible must precede do_enable_embeddings")

    # --- (2) prerequisites ---

    def test_enable_embeddings_requires_embedding_model(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("GBRAIN_EMBEDDING_MODEL", body)

    def test_enable_embeddings_requires_embedding_dimensions(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("GBRAIN_EMBEDDING_DIMENSIONS", body)

    def test_enable_embeddings_prereq_check_is_nonempty(self) -> None:
        """The prerequisite check must use -z (nonempty) guards for both vars."""
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("-z", body)
        self.assertIn("GBRAIN_EMBEDDING_MODEL", body)
        self.assertIn("GBRAIN_EMBEDDING_DIMENSIONS", body)

    def test_enable_embeddings_prereq_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("enable_embeddings_prerequisites_missing", body)
        self.assertIn('"success": false', body)

    def test_enable_embeddings_prereq_check_before_lock(self) -> None:
        """The prerequisite check must run before any lock acquisition."""
        body = _extract_function(self.src, "do_enable_embeddings")
        prereq_pos = body.find("enable_embeddings_prerequisites_missing")
        lock_pos = body.find("run_under_lock")
        self.assertLess(prereq_pos, lock_pos,
                        "prerequisite check must precede lock acquisition")

    def test_enable_embeddings_prereq_failure_returns_nonzero(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        prereq_block = body[:body.find("export_gbrain_env")]
        self.assertIn("return 1", prereq_block)

    # --- (3) must not init or reinit PGLite ---

    def test_enable_embeddings_does_not_invoke_init(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertNotIn("init --pglite", body)
        self.assertNotIn("init ", body)

    def test_enable_embeddings_does_not_reinit_pglite(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertNotIn("reinit", body)
        self.assertNotIn("reinit-pglite", body)

    def test_enable_embeddings_does_not_run_schema_sync_apply(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertNotIn("schema sync --apply", body)

    def test_enable_embeddings_does_not_install_source_pack(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertNotIn("install_source_pack", body)

    # --- (4) tasknotes lock, nonblocking, before gbrain access ---

    def test_enable_embeddings_references_tasknotes_lock_path(self) -> None:
        # The lock path is a shared global; just confirm it is still present.
        self.assertIn("/opt/data/.locks/tasknotes.lock", self.src)

    def test_enable_embeddings_uses_tasknotes_lock_var(self) -> None:
        """The fixed lock path constant is used by the shared safe open."""
        body = _extract_function(self.src, "open_tasknotes_lock_fd")
        self.assertIn("$GBRAIN_TASKNOTES_LOCK", body)

    def test_enable_embeddings_uses_flock_nonblocking(self) -> None:
        """The nonblocking flock lives in the shared run_under_lock helper."""
        body = _extract_function(self.src, "run_under_lock")
        self.assertIn('"$FLOCK_BIN" -n 9', body)

    def test_enable_embeddings_lock_before_migrate(self) -> None:
        """The lock must be acquired before the `gbrain migrate` invocation."""
        body = _extract_function(self.src, "do_enable_embeddings")
        lock_pos = body.find("run_under_lock")
        migrate_pos = body.find("migrate embeddings")
        self.assertGreater(lock_pos, -1, "run_under_lock must be present")
        self.assertGreater(migrate_pos, -1, "migrate embeddings must be present")
        self.assertLess(lock_pos, migrate_pos,
                        "lock acquisition must precede gbrain migrate")

    def test_enable_embeddings_lock_busy_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("enable_embeddings_lock_busy", body)
        self.assertIn('"success": false', body)

    def test_enable_embeddings_lock_unavailable_error_is_structured(self) -> None:
        """The structured unavailable error is emitted by the shared safe
        open with the per-action tag."""
        body = _extract_function(self.src, "open_tasknotes_lock_fd")
        self.assertIn("${err_tag}_lock_unavailable", body)

    # --- (5) gbrain migrate embeddings invocation ---

    def test_enable_embeddings_invokes_migrate_embeddings(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("migrate embeddings", body)

    def test_enable_embeddings_passes_to_flag(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("--to", body)
        self.assertIn("$GBRAIN_EMBEDDING_MODEL", body)

    def test_enable_embeddings_passes_dim_flag(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("--dim", body)
        self.assertIn("$GBRAIN_EMBEDDING_DIMENSIONS", body)

    def test_enable_embeddings_passes_yes_flag(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("--yes", body)

    def test_enable_embeddings_passes_no_embed_flag(self) -> None:
        """The migration must skip the re-embed pass (--no-embed)."""
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("--no-embed", body)

    def test_enable_embeddings_passes_ignore_env_override_flag(self) -> None:
        """The migration must proceed even when env vars would override."""
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("--ignore-env-override", body)

    def test_enable_embeddings_migrate_failure_is_structured(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("gbrain_migrate_embeddings_failed", body)
        self.assertIn('"success": false', body)

    def test_enable_embeddings_migrate_blocked_is_structured(self) -> None:
        """A refused/failed status in the migrate output must be surfaced."""
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("gbrain_migrate_embeddings_blocked", body)

    # --- (6) does NOT initiate backfill ---

    def test_enable_embeddings_does_not_invoke_embed_stale(self) -> None:
        """enable-embeddings must NOT initiate backfill (no `gbrain embed --stale`).

        The success message may legitimately reference `embed-backfill` as the
        next operator step, but the function must not invoke `gbrain embed` or
        `embed --stale` itself.
        """
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertNotIn("embed --stale", body)
        self.assertNotIn("$GBRAIN_BIN\" embed", body)
        self.assertNotIn("GBRAIN_EMBED_CONCURRENCY", body)

    def test_enable_embeddings_does_not_invoke_embed(self) -> None:
        """enable-embeddings must not invoke any `gbrain embed` subcommand.

        The success message may reference `embed-backfill` as the next step,
        but no `gbrain embed` invocation may appear in the function.
        """
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertNotIn("$GBRAIN_BIN\" embed", body)
        self.assertNotIn("GBRAIN_EMBED_CONCURRENCY", body)

    # --- (7) ONLY after success set keyword_only false ---

    def test_enable_embeddings_sets_keyword_only_false(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("config set search.mcp_keyword_only false", body)

    def test_enable_embeddings_forces_keyword_only_true_first(self) -> None:
        """enable-embeddings must FIRST force search.mcp_keyword_only true so
        any error path leaves keyword-only enabled (issue #65)."""
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("config set search.mcp_keyword_only true", body)

    def test_enable_embeddings_keyword_only_true_before_migrate(self) -> None:
        """The keyword_only=true force must happen BEFORE the migration so
        any migration failure leaves keyword-only enabled."""
        body = _extract_function(self.src, "do_enable_embeddings")
        force_pos = body.find("search.mcp_keyword_only true")
        migrate_pos = body.find("migrate embeddings")
        self.assertGreater(force_pos, -1)
        self.assertGreater(migrate_pos, -1)
        self.assertLess(force_pos, migrate_pos,
                        "keyword_only=true force must precede the migration")

    def test_enable_embeddings_keyword_only_force_failure_structured(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("enable_embeddings_keyword_only_force_failed", body)

    def test_enable_embeddings_keyword_only_force_exits_on_failure(self) -> None:
        """The keyword_only=true force failure must exit before the migration."""
        body = _extract_function(self.src, "do_enable_embeddings")
        force_failed_pos = body.find("enable_embeddings_keyword_only_force_failed")
        migrate_pos = body.find("migrate embeddings")
        self.assertGreater(force_failed_pos, -1)
        self.assertGreater(migrate_pos, -1)
        force_block = body[force_failed_pos:migrate_pos]
        self.assertIn("exit 1", force_block,
                      "keyword_only force failure must exit before migration")

    def test_enable_embeddings_keyword_only_flip_after_migrate(self) -> None:
        """The keyword_only=false flip must come AFTER the migration success."""
        body = _extract_function(self.src, "do_enable_embeddings")
        migrate_pos = body.find("migrate embeddings")
        flip_pos = body.find("search.mcp_keyword_only false")
        self.assertGreater(migrate_pos, -1)
        self.assertGreater(flip_pos, -1)
        self.assertLess(migrate_pos, flip_pos,
                        "keyword_only=false must be set after the migration")

    def test_enable_embeddings_keyword_only_flip_failure_structured(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("enable_embeddings_keyword_only_flip_failed", body)

    # --- (8) on migration failure keyword-only stays enabled ---

    def test_enable_embeddings_no_keyword_flip_on_migrate_failure(self) -> None:
        """On migration failure the function must exit before the keyword flip.

        The migrate-failure handler must `exit 1` (inside the flock block) so
        the keyword_only=false line is never reached on a failed migration.
        """
        body = _extract_function(self.src, "do_enable_embeddings")
        migrate_failed_pos = body.find("gbrain_migrate_embeddings_failed")
        flip_pos = body.find("search.mcp_keyword_only false")
        self.assertGreater(migrate_failed_pos, -1)
        self.assertGreater(flip_pos, -1)
        # The migrate-failure block must contain an `exit 1` before the flip.
        migrate_block = body[migrate_failed_pos:flip_pos]
        self.assertIn("exit 1", migrate_block,
                      "migrate failure must exit before the keyword flip")

    def test_enable_embeddings_no_keyword_flip_on_blocked(self) -> None:
        """A blocked migration (refused/failed status) must also exit before
        the keyword flip."""
        body = _extract_function(self.src, "do_enable_embeddings")
        blocked_pos = body.find("gbrain_migrate_embeddings_blocked")
        flip_pos = body.find("search.mcp_keyword_only false")
        self.assertGreater(blocked_pos, -1)
        self.assertGreater(flip_pos, -1)
        blocked_block = body[blocked_pos:flip_pos]
        self.assertIn("exit 1", blocked_block,
                      "blocked migration must exit before the keyword flip")

    # --- (9) structured failure JSON ---

    def test_enable_embeddings_all_failures_are_structured(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        for error in (
            "enable_embeddings_prerequisites_missing",
            "enable_embeddings_lock_busy",
            "enable_embeddings_keyword_only_force_failed",
            "gbrain_migrate_embeddings_failed",
            "gbrain_migrate_embeddings_blocked",
            "enable_embeddings_keyword_only_flip_failed",
        ):
            self.assertIn(error, body)

    # --- (10) structured success JSON ---

    def test_enable_embeddings_emits_structured_success(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn('"success": true', body)
        self.assertIn('"action": "enable-embeddings"', body)

    def test_enable_embeddings_success_message_mentions_no_backfill(self) -> None:
        """The success message must indicate backfill is a separate step."""
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("embed-backfill", body.lower())

    def test_enable_embeddings_success_after_keyword_flip(self) -> None:
        """The success JSON must be emitted after the keyword_only flip."""
        body = _extract_function(self.src, "do_enable_embeddings")
        flip_pos = body.find("search.mcp_keyword_only false")
        success_pos = body.find('"action": "enable-embeddings"')
        self.assertGreater(flip_pos, -1)
        self.assertGreater(success_pos, -1)
        self.assertLess(flip_pos, success_pos,
                        "success JSON must be emitted after the keyword flip")

    # --- (11) marker invalidation failure is explicit and structured ---

    def test_enable_embeddings_marker_removal_failure_is_structured(self) -> None:
        """A marker removal failure must emit a structured
        enable_embeddings_marker_removal_failed error and exit nonzero —
        it must not claim success or fall through to the block-level
        lock_unavailable handler."""
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("enable_embeddings_marker_removal_failed", body)
        self.assertIn('"success": false', body)
        marker_pos = body.find("enable_embeddings_marker_removal_failed")
        success_pos = body.find('"action": "enable-embeddings"')
        self.assertGreater(marker_pos, -1)
        self.assertGreater(success_pos, -1)
        self.assertLess(marker_pos, success_pos,
                        "marker removal failure must precede any success JSON")

    # --- env export, state dir, safe-directory ---

    def test_enable_embeddings_exports_gbrain_env(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("export_gbrain_env", body)

    def test_enable_embeddings_creates_state_dir(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn('mkdir -p "$GBRAIN_STATE_DIR"', body)

    def test_enable_embeddings_sets_git_safe_directory(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("mark_brain_repo_safe_directory", body)

    def test_enable_embeddings_env_before_lock(self) -> None:
        """export_gbrain_env and state dir setup must happen before gbrain access."""
        body = _extract_function(self.src, "do_enable_embeddings")
        env_pos = body.find("export_gbrain_env")
        lock_pos = body.find("run_under_lock")
        self.assertLess(env_pos, lock_pos,
                        "export_gbrain_env must precede lock acquisition")


class GbrainDisableEmbeddingsContractTests(unittest.TestCase):
    """disable-embeddings is the operator-only rollback to keyword-only (issue #65).

    It is the safe inverse of enable-embeddings: flips search mode back to
    keyword-only WITHOUT destroying data. These tests guard:
      (1) shell usage and main dispatch include it; privilege drop supports it
      (2) sets search.mcp_keyword_only true FIRST
      (3) does NOT remove the embeddings overlay or delete vectors
      (4) does NOT invoke migrate embeddings or embed
      (5) emits structured failure JSON
      (6) emits structured success JSON
    """

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    # --- (1) usage, dispatch, privilege drop ---

    def test_usage_includes_disable_embeddings(self) -> None:
        body = _extract_function(self.src, "usage")
        self.assertIn("disable-embeddings", body)

    def test_main_dispatches_disable_embeddings(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertIn("disable-embeddings)", body)
        self.assertIn("do_disable_embeddings", body)

    def test_main_dispatch_drops_root_for_disable_embeddings(self) -> None:
        body = _extract_function(self.src, "main")
        drop_pos = body.find("drop_root_if_possible disable-embeddings")
        do_pos = body.find("do_disable_embeddings")
        self.assertGreater(drop_pos, -1, "disable-embeddings must drop root privileges")
        self.assertGreater(do_pos, -1, "do_disable_embeddings must be dispatched")
        self.assertLess(drop_pos, do_pos,
                        "drop_root_if_possible must precede do_disable_embeddings")

    # --- (2) sets keyword_only true FIRST ---

    def test_disable_embeddings_sets_keyword_only_true(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("config set search.mcp_keyword_only true", body)

    def test_disable_embeddings_keyword_only_true_is_first_action(self) -> None:
        """The keyword_only=true flip must be the first gbrain action."""
        body = _extract_function(self.src, "do_disable_embeddings")
        flip_pos = body.find("search.mcp_keyword_only true")
        # No other gbrain command (migrate, embed, init, schema) may precede it.
        for cmd in ("migrate embeddings", "embed --stale", "init --pglite",
                    "schema sync --apply", "config set search.mcp_keyword_only false"):
            cmd_pos = body.find(cmd)
            if cmd_pos != -1:
                self.assertGreater(flip_pos, -1)
                self.assertLess(flip_pos, cmd_pos,
                                f"keyword_only=true must precede {cmd}")

    def test_disable_embeddings_keyword_only_flip_failure_structured(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("disable_embeddings_keyword_only_flip_failed", body)

    # --- (3) does NOT remove overlay or delete vectors ---

    def test_disable_embeddings_does_not_remove_overlay(self) -> None:
        """disable-embeddings must not remove the embeddings overlay config."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertNotIn("config unset embedding_model", body)
        self.assertNotIn("config unset embedding_dimensions", body)
        self.assertNotIn("config delete embedding_model", body)
        self.assertNotIn("config delete embedding_dimensions", body)

    def test_disable_embeddings_does_not_delete_vectors(self) -> None:
        """disable-embeddings must not delete or null embedding vectors."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertNotIn("invalidate", body)
        self.assertNotIn("DELETE", body)
        self.assertNotIn("drop", body.lower())

    def test_disable_embeddings_does_not_migrate(self) -> None:
        """disable-embeddings must not invoke migrate embeddings."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertNotIn("migrate embeddings", body)

    # --- (4) does NOT invoke embed or migrate ---

    def test_disable_embeddings_does_not_invoke_embed(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertNotIn("embed --stale", body)
        self.assertNotIn("embed-backfill", body)
        self.assertNotIn("$GBRAIN_BIN\" embed", body)

    def test_disable_embeddings_does_not_require_embedding_env(self) -> None:
        """disable-embeddings must not require GBRAIN_EMBEDDING_* env vars."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertNotIn("GBRAIN_EMBEDDING_MODEL", body)
        self.assertNotIn("GBRAIN_EMBEDDING_DIMENSIONS", body)

    def test_disable_embeddings_acquires_tasknotes_lock(self) -> None:
        """disable-embeddings must acquire the shared tasknotes lock (issue #65
        safe rollback: it writes to the file plane, so it must hold the lock
        to avoid concurrent vault writes)."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("run_under_lock disable-embeddings", body)

    def test_disable_embeddings_lock_busy_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("disable_embeddings_lock_busy", body)
        self.assertIn('"success": false', body)

    def test_disable_embeddings_lock_unavailable_error_is_structured(self) -> None:
        """The structured unavailable error is emitted by the shared safe
        open with the per-action tag."""
        body = _extract_function(self.src, "open_tasknotes_lock_fd")
        self.assertIn("${err_tag}_lock_unavailable", body)

    def test_disable_embeddings_does_not_init_or_reinit(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertNotIn("init --pglite", body)
        self.assertNotIn("reinit", body)

    # --- (3b) writes embedding_disabled sentinel atomically (issue #65) ---

    def test_disable_embeddings_writes_embedding_disabled_sentinel(self) -> None:
        """disable-embeddings must write embedding_disabled=true into the file
        plane so direct gbrain embedding operations refuse (issue #65)."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("embedding_disabled", body)
        self.assertIn("True", body)

    def test_disable_embeddings_writes_sentinel_via_python(self) -> None:
        """The sentinel must be written atomically via Python (temp file +
        rename), not via `gbrain config set` (which would be a shell
        subprocess vulnerable to partial writes)."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn('"$PYTHON_BIN"', body)
        self.assertNotIn(" python3 ", body)
        self.assertIn("tempfile", body)
        self.assertIn("os.replace", body)

    def test_disable_embeddings_sentinel_write_failure_is_structured(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("disable_embeddings_sentinel_write_failed", body)

    def test_disable_embeddings_keyword_only_before_sentinel(self) -> None:
        """keyword-only true must be set BEFORE the sentinel write so
        retrieval falls back to keyword search even if the sentinel write
        fails."""
        body = _extract_function(self.src, "do_disable_embeddings")
        flip_pos = body.find("search.mcp_keyword_only true")
        sentinel_pos = body.find("embedding_disabled")
        self.assertGreater(flip_pos, -1)
        self.assertGreater(sentinel_pos, -1)
        self.assertLess(flip_pos, sentinel_pos,
                        "keyword_only=true must precede the sentinel write")

    # --- (5) structured failure JSON ---

    def test_disable_embeddings_failure_is_structured(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("disable_embeddings_keyword_only_flip_failed", body)
        # The failure JSON is Python-generated (json.dumps), so the source
        # contains the Python literal `False` which serializes to JSON `false`.
        self.assertIn('"success": False', body)

    # --- (6) structured success JSON ---

    def test_disable_embeddings_emits_structured_success(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn('"success": true', body)
        self.assertIn('"action": "disable-embeddings"', body)

    def test_disable_embeddings_marker_removal_failure_is_structured(self) -> None:
        """A marker removal failure must emit a structured
        disable_embeddings_marker_removal_failed error and exit nonzero —
        it must not claim success or fall through to the block-level
        lock_unavailable handler."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("disable_embeddings_marker_removal_failed", body)
        self.assertIn('"success": false', body)
        marker_pos = body.find("disable_embeddings_marker_removal_failed")
        success_pos = body.find('"action": "disable-embeddings"')
        self.assertGreater(marker_pos, -1)
        self.assertGreater(success_pos, -1)
        self.assertLess(marker_pos, success_pos,
                        "marker removal failure must precede any success JSON")

    def test_disable_embeddings_success_message_mentions_preserved(self) -> None:
        """The success message must indicate vectors/overlay are preserved."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("preserved", body.lower())

    def test_disable_embeddings_success_after_keyword_flip(self) -> None:
        """The success JSON must be emitted after the keyword_only flip."""
        body = _extract_function(self.src, "do_disable_embeddings")
        flip_pos = body.find("search.mcp_keyword_only true")
        success_pos = body.find('"action": "disable-embeddings"')
        self.assertGreater(flip_pos, -1)
        self.assertGreater(success_pos, -1)
        self.assertLess(flip_pos, success_pos,
                        "success JSON must be emitted after the keyword flip")

    # --- env export, state dir, safe-directory ---

    def test_disable_embeddings_exports_gbrain_env(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("export_gbrain_env", body)

    def test_disable_embeddings_creates_state_dir(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn('mkdir -p "$GBRAIN_STATE_DIR"', body)

    def test_disable_embeddings_sets_git_safe_directory(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("mark_brain_repo_safe_directory", body)

    # --- (3b-2) fail-closed sentinel write (issue #65 final review) ---

    def test_disable_embeddings_fail_closed_on_unreadable_config(self) -> None:
        """disable-embeddings must fail closed when the existing config.json
        cannot be read/parsed, without replacing it. The Python block must
        emit a structured disable_embeddings_config_unreadable error."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("disable_embeddings_config_unreadable", body)

    def test_disable_embeddings_fail_closed_on_non_object_config(self) -> None:
        """disable-embeddings must fail closed when the existing config.json
        is a non-object (e.g. JSON array), without replacing it. The Python
        block must emit a structured disable_embeddings_config_not_object
        error."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("disable_embeddings_config_not_object", body)

    def test_disable_embeddings_fail_closed_does_not_silently_reset(self) -> None:
        """The Python block must NOT silently reset cfg to {} on parse/read
        failure (the old behavior). It must fail closed instead."""
        body = _extract_function(self.src, "do_disable_embeddings")
        # The old behavior had `except (json.JSONDecodeError, OSError): cfg = {}`
        # which silently reset. The new behavior must NOT contain that silent
        # reset — it must sys.exit(1) with a structured error instead.
        self.assertNotIn("except (json.JSONDecodeError, OSError):\n        cfg = {}", body)
        # The new behavior must sys.exit(1) on parse/read failure.
        self.assertIn("sys.exit(1)", body)

    def test_disable_embeddings_fail_closed_checks_isinstance_dict(self) -> None:
        """The Python block must check isinstance(cfg, dict) and fail closed
        when the config is a non-object."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("isinstance(cfg, dict)", body)

    def test_disable_embeddings_fail_closed_only_starts_fresh_when_missing(self) -> None:
        """The Python block must only start fresh (cfg = {}) when the config
        file is MISSING, not when it exists but cannot be parsed."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("if os.path.exists(config_path):", body)
        self.assertIn("else:", body)
        # The else branch (file missing) sets cfg = {}.
        else_pos = body.find("else:")
        self.assertGreater(else_pos, 0)
        else_block = body[else_pos:else_pos + 50]
        self.assertIn("cfg = {}", else_block)

    def test_disable_embeddings_fail_closed_errors_are_structured(self) -> None:
        """The fail-closed errors must be structured JSON with success: false."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn('"success": false', body.lower())
        self.assertIn("disable_embeddings_config_unreadable", body)
        self.assertIn("disable_embeddings_config_not_object", body)

    def test_disable_embeddings_fail_closed_surfaces_right_error(self) -> None:
        """The wrapper must surface the right structured error depending on
        which fail-closed condition fired (config_unreadable vs
        config_not_object vs sentinel_write_failed)."""
        body = _extract_function(self.src, "do_disable_embeddings")
        # The case statement must dispatch on the Python re-check output.
        self.assertIn("disable_embeddings_config_unreadable", body)
        self.assertIn("disable_embeddings_config_not_object", body)
        self.assertIn("disable_embeddings_sentinel_write_failed", body)
        # The case must use a case/esac dispatch.
        self.assertIn("case", body)
        self.assertIn("esac", body)


class GbrainEnableDisableEmbeddingsPreservationContractTests(unittest.TestCase):
    """enable-embeddings/disable-embeddings must not alter reindex/refresh/embed-backfill.

    reindex and refresh must remain no-embedding and keyword-only. The
    subcommands are operator-only switches; they must not introduce embedding
    production into reindex/refresh. Embedding production stays confined to
    embed-backfill and the explicit-request refresh-embeddings path.
    """

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_reindex_still_sets_keyword_only_true(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("config set search.mcp_keyword_only true", body)

    def test_reindex_does_not_invoke_enable_embeddings(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertNotIn("enable-embeddings", body)
        self.assertNotIn("do_enable_embeddings", body)

    def test_refresh_does_not_invoke_enable_embeddings(self) -> None:
        body = _extract_function(self.src, "do_refresh")
        self.assertNotIn("enable-embeddings", body)
        self.assertNotIn("do_enable_embeddings", body)

    def test_embed_backfill_does_not_invoke_enable_embeddings(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertNotIn("enable-embeddings", body)
        self.assertNotIn("do_enable_embeddings", body)
        self.assertNotIn("migrate embeddings", body)

    def test_enable_embeddings_does_not_share_sync_helper(self) -> None:
        """enable-embeddings must not route through run_sync_extract_links."""
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertNotIn("run_sync_extract_links", body)

    def test_disable_embeddings_does_not_share_sync_helper(self) -> None:
        """disable-embeddings must not route through run_sync_extract_links."""
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertNotIn("run_sync_extract_links", body)

    def test_enable_embeddings_is_not_called_by_cron_paths(self) -> None:
        """reindex/refresh must not call enable-embeddings or migrate embeddings."""
        for func in ("do_reindex", "do_refresh", "run_sync_extract_links"):
            body = _extract_function(self.src, func)
            self.assertNotIn("migrate embeddings", body,
                              f"{func} must not invoke migrate embeddings")
            self.assertNotIn("enable-embeddings", body,
                              f"{func} must not invoke enable-embeddings")


class GbrainSimplificationContractTests(unittest.TestCase):
    """The wrapper must NOT contain removed gating/bounding/per-action logic."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_no_readiness_marker_functions(self) -> None:
        for func in ["marker_exists", "marker_field", "marker_is_valid",
                     "marker_matches_current", "write_marker", "remove_marker"]:
            self.assertNotIn(f"{func}()", self.src,
                             f"removed marker function {func} must not exist")

    def test_no_gate_function(self) -> None:
        self.assertNotIn("require_gate()", self.src)
        self.assertNotIn("gate_open", self.src)

    def test_no_per_action_functions(self) -> None:
        for func in ["do_search", "do_get", "do_capture", "do_put",
                     "do_link", "do_backlinks", "do_status", "do_schema_status"]:
            self.assertNotIn(f"{func}()", self.src,
                             f"removed action function {func} must not exist")

    def test_no_provider_stripping(self) -> None:
        self.assertNotIn("strip_provider_env", self.src)
        self.assertNotIn("VOYAGE_API_KEY", self.src)
        self.assertNotIn("GBRAIN_EMBEDDING_API_KEY", self.src)

    def test_no_output_capping(self) -> None:
        self.assertNotIn("cap_output()", self.src)
        self.assertNotIn("GBRAIN_QUERY_MAX_OUTPUT_CHARS", self.src)

    def test_no_timeout_resolution(self) -> None:
        self.assertNotIn("resolve_timeout()", self.src)
        self.assertNotIn("GBRAIN_QUERY_TIMEOUT_SECONDS", self.src)

    def test_no_query_bounding_env_vars(self) -> None:
        for var in ["GBRAIN_QUERY_MAX_INPUT_CHARS", "GBRAIN_QUERY_MAX_LIMIT",
                    "GBRAIN_CONTENT_MAX_CHARS", "GBRAIN_ENABLED"]:
            self.assertNotIn(var, self.src,
                             f"removed env var {var} must not be referenced")

    def test_no_status_or_schema_status_subcommands(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertNotIn("status)", body)
        self.assertNotIn("schema-status", body)

    def test_main_only_dispatches_operator_commands(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertIn("reindex", body)
        self.assertIn("do_reindex", body)
        self.assertIn("refresh", body)
        self.assertIn("do_refresh", body)
        self.assertIn("refresh-embeddings", body)
        self.assertIn("do_refresh_embeddings", body)

    def test_no_managed_link_sources_constant(self) -> None:
        self.assertNotIn("MANAGED_LINK_SOURCES", self.src)

    def test_no_slug_validation(self) -> None:
        self.assertNotIn("validate_slug()", self.src)

    def test_no_write_through_check(self) -> None:
        self.assertNotIn("check_write_through()", self.src)

    def test_no_json_get_helper(self) -> None:
        self.assertNotIn("json_get()", self.src)

    def test_no_read_gbrain_config_helper(self) -> None:
        self.assertNotIn("read_gbrain_config()", self.src)


class GbrainEnvExportContractTests(unittest.TestCase):
    """export_gbrain_env is the single place where the wrapper exports the
    gbrain runtime env before every gbrain invocation (issue #112: the
    startup-hook skip must be enforced regardless of caller environment)."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_skip_startup_hooks_exported_with_override_semantics(self) -> None:
        body = _extract_function(self.src, "export_gbrain_env")
        self.assertIn("export GBRAIN_SKIP_STARTUP_HOOKS=1", body)
        # An export assignment overrides any caller-provided value; a
        # ${VAR:-default} form would let a hostile "0" or empty value
        # through to the private launcher.
        self.assertNotIn("GBRAIN_SKIP_STARTUP_HOOKS:-", body)
        self.assertNotIn("GBRAIN_SKIP_STARTUP_HOOKS:+", body)


class GbrainDockerLayoutContractTests(unittest.TestCase):
    """The native CLI must live at the private non-PATH path
    /opt/josemar/libexec/gbrain-native; the PUBLIC /usr/local/bin/gbrain must
    be the issue #110 adapter, with gbrain-chat-run kept only as a
    backwards-compatible symlink alias."""

    def setUp(self) -> None:
        self.src = _read(DOCKERFILE_PATH)

    def test_native_wrapper_lives_at_private_non_path_location(self) -> None:
        match = re.search(r"printf.*?/opt/josemar/libexec/gbrain-native", self.src, re.DOTALL)
        self.assertIsNotNone(match, "Could not find native wrapper creation in Dockerfile")
        assert match is not None
        wrapper_line = match.group(0)
        self.assertIn("cd /opt/gbrain", wrapper_line)
        self.assertIn("src/cli.ts", wrapper_line)

    def test_native_wrapper_enforces_skip_startup_hooks(self) -> None:
        """Issue #112: the private launcher itself must enforce
        GBRAIN_SKIP_STARTUP_HOOKS=1 as an inline assignment before exec, so
        no caller environment (unset, empty, or "0") can re-enable gbrain's
        detached startup-hook network call."""
        match = re.search(r"printf.*?/opt/josemar/libexec/gbrain-native", self.src, re.DOTALL)
        self.assertIsNotNone(match, "Could not find native wrapper creation in Dockerfile")
        assert match is not None
        wrapper_line = match.group(0)
        self.assertIn("GBRAIN_SKIP_STARTUP_HOOKS=1 exec", wrapper_line)
        self.assertLess(
            wrapper_line.find("GBRAIN_SKIP_STARTUP_HOOKS=1"),
            wrapper_line.find("exec"),
            "the assignment must precede exec so it overrides caller env",
        )
        self.assertLess(
            wrapper_line.find("GBRAIN_SKIP_STARTUP_HOOKS=1"),
            wrapper_line.find("src/cli.ts"),
            "the assignment must apply to the bun CLI invocation",
        )

    def test_no_native_wrapper_at_public_path(self) -> None:
        """The public /usr/local/bin/gbrain must never be the native wrapper:
        no printf-style wrapper may redirect to it (the adapter COPY is the
        only thing at the public path)."""
        match = re.search(r"printf[^\n]*> /usr/local/bin/gbrain", self.src)
        self.assertIsNone(match, "native wrapper must not be installed at the public path")

    def test_public_gbrain_is_the_safe_adapter(self) -> None:
        self.assertIn(
            "COPY scripts/gbrain_chat_run.py /usr/local/bin/gbrain", self.src
        )
        self.assertIn("chmod +x /usr/local/bin/gbrain", self.src)

    def test_gbrain_chat_run_is_a_symlink_alias_not_a_duplicate(self) -> None:
        self.assertIn(
            "ln -s /usr/local/bin/gbrain /usr/local/bin/gbrain-chat-run", self.src
        )
        self.assertNotIn(
            "COPY scripts/gbrain_chat_run.py /usr/local/bin/gbrain-chat-run", self.src
        )

    def test_gbrain_ref_is_pinned_to_the_supported_release(self) -> None:
        self.assertIn(
            "ARG GBRAIN_REF=15b9863d13635d173562a54f55a1d388bfcf546b",
            self.src,
        )

    def test_no_gbrain_skill_symlink(self) -> None:
        """The gbrain-skill symlink must not be created (Josemar uses gbrain directly)."""
        self.assertNotIn("/usr/local/bin/gbrain-skill", self.src)
        self.assertNotIn("gbrain/gbrain", self.src)

    def test_gbrain_skill_dir_still_copied(self) -> None:
        """The gbrain skill directory (for SKILL.md) must still be copied."""
        self.assertIn("COPY skills-factory/gbrain /opt/josemar/skills/gbrain", self.src)

    def test_no_chmod_gbrain_skill_binary(self) -> None:
        """The removed Python skill binary must not be chmod'd."""
        self.assertNotIn("gbrain/gbrain", self.src)

    def test_gbrain_refresh_cron_script_installed(self) -> None:
        self.assertIn("hermes-gbrain-refresh-cron.sh", self.src)
        self.assertIn("/opt/josemar/scripts/hermes-gbrain-refresh-cron.sh", self.src)


class GbrainCronContractTests(unittest.TestCase):
    """Hermes init must install a 5-minute gbrain refresh cron by default."""

    def setUp(self) -> None:
        self.src = _read(HERMES_INIT_PATH)

    def test_gbrain_refresh_cron_defaults_to_five_minutes(self) -> None:
        body = _extract_function(self.src, "install_gbrain_refresh_cron")
        self.assertIn('refresh_interval="${GBRAIN_REFRESH_INTERVAL:-5}"', body)
        self.assertIn('"every ${refresh_interval}m"', body)

    def test_gbrain_refresh_cron_installs_script_job(self) -> None:
        body = _extract_function(self.src, "install_gbrain_refresh_cron")
        self.assertIn("hermes-gbrain-refresh-cron.sh", body)
        self.assertIn("--no-agent", body)
        self.assertIn("--name gbrain-refresh", body)

    def test_gbrain_refresh_cron_can_be_disabled(self) -> None:
        body = _extract_function(self.src, "install_gbrain_refresh_cron")
        self.assertIn("GBRAIN_REFRESH_INTERVAL", body)
        self.assertIn('""|0|*[!0-9]*)', body)

    def test_cron_creation_uses_hermes_cli_absolute_path(self) -> None:
        self.assertIn('HERMES_CLI="${HERMES_CLI:-/opt/hermes/.venv/bin/hermes}"', self.src)
        self.assertIn('exec "$HERMES_CLI" cron create "$@"', self.src)
        self.assertNotIn('exec hermes cron create "$@"', self.src)

    def test_cron_creation_stops_su_option_parsing_before_hermes_flags(self) -> None:
        """su must not consume cron-create flags such as --no-agent."""
        for function_name in ("install_workspace_sync_cron", "install_gbrain_refresh_cron"):
            with self.subTest(function_name=function_name):
                body = _extract_function(self.src, function_name)
                self.assertIn("su -s /bin/sh -- hermes -c", body)
                self.assertNotIn("su -s /bin/sh hermes -c", body)


class GbrainEmbeddingRefreshCronContractTests(unittest.TestCase):
    """Hermes init must install the daily gbrain-embedding-refresh cron with a
    validated local-timezone schedule and reconcile full drift (schedule,
    script, workdir, no_agent), not merely the job name."""

    def setUp(self) -> None:
        self.src = _read(HERMES_INIT_PATH)

    def test_default_schedule_is_local_5am(self) -> None:
        """The default is 0 5 * * * in the Hermes local timezone, not UTC."""
        body = _extract_function(self.src, "install_gbrain_embedding_refresh_cron")
        self.assertIn('schedule="${GBRAIN_EMBED_REFRESH_SCHEDULE:-0 5 * * *}"', body)
        # The schedule must be handed to the scheduler as-is (local time), with
        # no conversion to a UTC offset expression.
        self.assertNotIn("TZ=UTC", body)
        self.assertNotIn("offset", body.lower())

    def test_schedule_passed_verbatim_to_cron_create(self) -> None:
        """The schedule string must be handed to `hermes cron create` verbatim
        (the scheduler evaluates it in the container's local timezone)."""
        body = _extract_function(self.src, "install_gbrain_embedding_refresh_cron")
        self.assertIn('"$schedule"', body)
        self.assertIn('cron create "$@"', body)

    def test_schedule_validated_before_install(self) -> None:
        body = _extract_function(self.src, "install_gbrain_embedding_refresh_cron")
        self.assertIn("WARNING: invalid GBRAIN_EMBED_REFRESH_SCHEDULE", body)
        self.assertIn("re.fullmatch", body)
        self.assertIn("len(s.split()) == 5", body)

    def test_disabled_schedule_removes_owned_job(self) -> None:
        body = _extract_function(self.src, "install_gbrain_embedding_refresh_cron")
        self.assertIn('""|0)', body)
        self.assertIn("remove_gbrain_embedding_refresh_cron_job", body)

    def test_invalid_schedule_removes_owned_job(self) -> None:
        """A malformed schedule must not leave the owned job behind."""
        body = _extract_function(self.src, "install_gbrain_embedding_refresh_cron")
        invalid_pos = body.find("WARNING: invalid GBRAIN_EMBED_REFRESH_SCHEDULE")
        self.assertGreater(invalid_pos, -1)
        self.assertIn("remove_gbrain_embedding_refresh_cron_job",
                      body[invalid_pos:invalid_pos + 200])

    def test_remove_helper_uses_named_remove(self) -> None:
        body = _extract_function(self.src, "remove_gbrain_embedding_refresh_cron_job")
        self.assertIn("cron remove gbrain-embedding-refresh", body)
        self.assertIn('"$HERMES_CLI"', body)

    def test_reconciles_drift_not_merely_name(self) -> None:
        """The existing-job check must compare the real cron schedule field
        (schedule.expr with kind=cron), script name, no_agent flag, and workdir
        — not just the job name."""
        body = _extract_function(self.src, "install_gbrain_embedding_refresh_cron")
        self.assertIn('s.get("kind") == "cron"', body)
        self.assertIn('s.get("expr")', body)
        self.assertIn('j.get("script") == "hermes-gbrain-embedding-refresh-cron.sh"', body)
        self.assertIn('j.get("no_agent") is True', body)
        self.assertIn('j.get("workdir")', body)

    def test_drift_logs_reconciliation(self) -> None:
        body = _extract_function(self.src, "install_gbrain_embedding_refresh_cron")
        self.assertIn("Reconciling Hermes gbrain-embedding-refresh cron job drift", body)

    def test_create_uses_expected_flags(self) -> None:
        body = _extract_function(self.src, "install_gbrain_embedding_refresh_cron")
        self.assertIn("--no-agent", body)
        self.assertIn("--script hermes-gbrain-embedding-refresh-cron.sh", body)
        self.assertIn("--workdir", body)
        self.assertIn("--name gbrain-embedding-refresh", body)

    def test_called_after_jobs_json_creation(self) -> None:
        jobs_pos = self.src.find("Creating empty Hermes cron/jobs.json")
        call_pos = self.src.rfind("install_gbrain_embedding_refresh_cron")
        self.assertGreater(jobs_pos, 0)
        self.assertGreater(call_pos, jobs_pos,
                           "embedding refresh cron must be called after jobs.json creation")

    def test_called_alongside_other_cron_installers(self) -> None:
        ws_pos = self.src.rfind("install_workspace_sync_cron")
        gb_pos = self.src.rfind("install_gbrain_refresh_cron")
        emb_pos = self.src.rfind("install_gbrain_embedding_refresh_cron")
        self.assertGreater(gb_pos, ws_pos)
        self.assertGreater(emb_pos, gb_pos)


class GbrainEmbeddingRefreshCronReconcileBehaviorTests(unittest.TestCase):
    """Behavior tests for the reconcile comparison embedded in
    install_gbrain_embedding_refresh_cron: the real Hermes cron schedule schema
    is {"kind": "cron", "expr": "0 5 * * *"}, and a matching existing job must
    be recognized as already correct (exit 0) so the init does NOT perpetually
    recreate it. The exact python heredoc from the init script is executed
    against fixture jobs.json files."""

    SCHEDULE = "0 5 * * *"
    WORKDIR = "/opt/data"
    SCRIPT = "hermes-gbrain-embedding-refresh-cron.sh"

    def setUp(self) -> None:
        self.src = _read(HERMES_INIT_PATH)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        match = re.search(
            r'python3 - "\$jobs_file" "\$schedule" "\$WORKSPACE_DIR" <<\'PY\'\n(.*?)\nPY',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(match, "Could not find the reconcile heredoc in init")
        assert match is not None
        self.reconcile_py = match.group(1)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reconcile(self, fixture: dict) -> int:
        jobs = self.tmpdir / "jobs.json"
        jobs.write_text(json.dumps(fixture), encoding="utf-8")
        result = subprocess.run(
            ["python3", "-", str(jobs), self.SCHEDULE, self.WORKDIR],
            input=self.reconcile_py,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode

    def _owned_job(self, **overrides) -> dict:
        job = {
            "name": "gbrain-embedding-refresh",
            "schedule": {"kind": "cron", "expr": self.SCHEDULE},
            "script": self.SCRIPT,
            "no_agent": True,
            "workdir": self.WORKDIR,
        }
        job.update(overrides)
        return job

    def test_real_cron_expr_fixture_is_recognized(self) -> None:
        """{"kind":"cron","expr":"0 5 * * *"} with the expected script/no_agent/
        workdir must be treated as already-correct (exit 0), not recreated."""
        fixture = {"jobs": [self._owned_job()]}
        self.assertEqual(self._reconcile(fixture), 0)

    def test_drifted_expr_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(schedule={"kind": "cron", "expr": "0 4 * * *"})]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_interval_kind_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(schedule={"kind": "interval", "minutes": 5})]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_drifted_script_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(script="other-script.sh")]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_no_agent_false_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(no_agent=False)]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_drifted_workdir_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(workdir="/tmp/other")]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_string_schedule_shape_is_recreated(self) -> None:
        """A schedule persisted as a bare string must be treated as drift, not
        guessed into an enabled state."""
        fixture = {"jobs": [self._owned_job(schedule=self.SCHEDULE)]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_absent_job_is_recreated(self) -> None:
        fixture = {"jobs": []}
        self.assertEqual(self._reconcile(fixture), 1)


class GbrainRefreshCronInstallerContractTests(unittest.TestCase):
    """Hermes init must install the gbrain-refresh cron with full drift
    reconciliation (interval schedule, script, workdir, no_agent) like the
    embedding refresh cron — not merely check the job name."""

    def setUp(self) -> None:
        self.src = _read(HERMES_INIT_PATH)

    def test_reconciles_drift_not_merely_name(self) -> None:
        """The existing-job check must compare the real interval schedule
        (kind=interval, minutes), script name, no_agent flag, and workdir —
        not just the job name."""
        body = _extract_function(self.src, "install_gbrain_refresh_cron")
        self.assertIn('s.get("kind") == "interval"', body)
        self.assertIn('isinstance(actual, int)', body)
        self.assertIn('not isinstance(actual, bool)', body)
        self.assertIn('j.get("script") == "hermes-gbrain-refresh-cron.sh"', body)
        self.assertIn('j.get("no_agent") is True', body)
        self.assertIn('j.get("workdir") == workdir', body)
        self.assertNotIn('job.get("name") == "gbrain-refresh"', body.replace("continue", ""))

    def test_drift_logs_reconciliation(self) -> None:
        body = _extract_function(self.src, "install_gbrain_refresh_cron")
        self.assertIn("Reconciling Hermes gbrain-refresh cron job drift", body)

    def test_disabled_interval_removes_owned_job(self) -> None:
        body = _extract_function(self.src, "install_gbrain_refresh_cron")
        self.assertIn('""|0|*[!0-9]*)', body)
        self.assertIn("remove_gbrain_refresh_cron_job", body)

    def test_remove_helper_uses_named_remove(self) -> None:
        body = _extract_function(self.src, "remove_gbrain_refresh_cron_job")
        self.assertIn('cron remove "$@"', body)
        self.assertIn("gbrain-refresh", body)
        self.assertIn('"$HERMES_CLI"', body)

    def test_create_uses_expected_flags(self) -> None:
        body = _extract_function(self.src, "install_gbrain_refresh_cron")
        self.assertIn("--no-agent", body)
        self.assertIn("--script hermes-gbrain-refresh-cron.sh", body)
        self.assertIn("--workdir", body)
        self.assertIn("--name gbrain-refresh", body)
        self.assertIn("every ${refresh_interval}m", body)


class GbrainRefreshCronReconcileBehaviorTests(unittest.TestCase):
    """Behavior tests for the reconcile comparison embedded in
    install_gbrain_refresh_cron: the real Hermes interval schedule schema is
    {"kind": "interval", "minutes": N}, and a matching existing job must be
    recognized as already correct (exit 0) so the init does NOT perpetually
    recreate it. The exact python heredoc from the init script is executed
    against fixture jobs.json files."""

    INTERVAL = 5
    WORKDIR = "/opt/data"
    SCRIPT = "hermes-gbrain-refresh-cron.sh"

    def setUp(self) -> None:
        self.src = _read(HERMES_INIT_PATH)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        match = re.search(
            r'python3 - "\$jobs_file" "\$refresh_interval" "\$WORKSPACE_DIR" <<\'PY\'\n(.*?)\nPY',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(match, "Could not find the refresh reconcile heredoc in init")
        assert match is not None
        self.reconcile_py = match.group(1)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reconcile(self, fixture: dict) -> int:
        jobs = self.tmpdir / "jobs.json"
        jobs.write_text(json.dumps(fixture), encoding="utf-8")
        result = subprocess.run(
            ["python3", "-", str(jobs), str(self.INTERVAL), self.WORKDIR],
            input=self.reconcile_py,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode

    def _owned_job(self, **overrides) -> dict:
        job = {
            "name": "gbrain-refresh",
            "schedule": {"kind": "interval", "minutes": self.INTERVAL,
                         "display": "every 5m"},
            "script": self.SCRIPT,
            "no_agent": True,
            "workdir": self.WORKDIR,
        }
        job.update(overrides)
        return job

    def test_real_interval_fixture_is_recognized(self) -> None:
        """{"kind":"interval","minutes":5} with the expected script/no_agent/
        workdir must be treated as already-correct (exit 0), not recreated."""
        fixture = {"jobs": [self._owned_job()]}
        self.assertEqual(self._reconcile(fixture), 0)

    def test_drifted_minutes_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(schedule={"kind": "interval", "minutes": 30})]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_drifted_kind_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(schedule={"kind": "cron", "expr": "0 5 * * *"})]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_drifted_script_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(script="other-script.sh")]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_no_agent_false_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(no_agent=False)]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_drifted_workdir_is_recreated(self) -> None:
        fixture = {"jobs": [self._owned_job(workdir="/tmp/other")]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_bool_minutes_is_recreated(self) -> None:
        """True is an int in Python but must not satisfy the interval check."""
        fixture = {"jobs": [self._owned_job(schedule={"kind": "interval", "minutes": True})]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_string_schedule_shape_is_recreated(self) -> None:
        """A schedule persisted as a bare string must be treated as drift, not
        guessed into an enabled state."""
        fixture = {"jobs": [self._owned_job(schedule="every 5m")]}
        self.assertEqual(self._reconcile(fixture), 1)

    def test_absent_job_is_recreated(self) -> None:
        fixture = {"jobs": []}
        self.assertEqual(self._reconcile(fixture), 1)


class GbrainEmbeddingRefreshTimeoutContractTests(unittest.TestCase):
    """The daily cron entrypoint must terminate its whole process group on
    timeout so a gbrain child holding the tasknotes flock cannot be orphaned,
    and the wrapper (not the cron entrypoint) must own the lock."""

    def setUp(self) -> None:
        self.helper = _read(REPO_ROOT / "scripts" / "hermes-gbrain-embedding-refresh.py")
        self.cron = _read(REPO_ROOT / "scripts" / "hermes-gbrain-embedding-refresh-cron.sh")
        self.dockerfile = _read(DOCKERFILE_PATH)
        self.compose = _read(REPO_ROOT / "docker-compose.yml")

    def test_cron_script_routes_through_timeout_helper(self) -> None:
        """The cron must exec the helper with the fixed image interpreter in
        isolated mode and an immutable helper path: no env redirection of the
        helper, no PATH-resolved python."""
        self.assertIn(
            'exec "/opt/hermes/.venv/bin/python3" -I "/opt/josemar/scripts/hermes-gbrain-embedding-refresh.py"',
            self.cron,
        )
        self.assertNotIn("GBRAIN_EMBED_REFRESH_HELPER", self.cron)
        self.assertNotIn("josemar-gbrain refresh-embeddings", self.cron)

    def test_cron_script_constrains_helper_below_hermes_outer_timeout(self) -> None:
        self.assertIn('outer_timeout="${HERMES_CRON_SCRIPT_TIMEOUT:-300}"', self.cron)
        self.assertIn('requested_timeout="${GBRAIN_EMBED_REFRESH_TIMEOUT:-240}"', self.cron)
        self.assertIn('safe_timeout=$((outer_timeout - kill_grace - group_drain - safety_margin - 1))', self.cron)
        self.assertIn('requested_timeout="$safe_timeout"', self.cron)
        self.assertIn('export GBRAIN_EMBED_REFRESH_TIMEOUT="$requested_timeout"', self.cron)
        self.assertIn('export GBRAIN_EMBED_REFRESH_KILL_GRACE="$kill_grace"', self.cron)

    def test_helper_spawns_child_in_own_session(self) -> None:
        self.assertIn("start_new_session=True", self.helper)

    def test_helper_kills_whole_process_group(self) -> None:
        self.assertIn("_signal_group(proc.pid, signal.SIGTERM)", self.helper)
        self.assertIn("_signal_group(proc.pid, signal.SIGKILL)", self.helper)

    def test_helper_cleanup_checks_group_not_leader(self) -> None:
        """After SIGTERM, cleanup must be driven by the liveness of the whole
        process GROUP (killpg(..., 0)), not the leader's proc.poll() state."""
        self.assertIn("os.killpg(proc.pid, 0)", self.helper)
        self.assertIn("_group_cleared(proc)", self.helper)
        self.assertIn("proc.poll()", self.helper)

    def test_helper_returns_124_on_timeout(self) -> None:
        self.assertIn("return 124", self.helper)

    def test_helper_outer_timeout_protection_sigterm_handler(self) -> None:
        """If the Hermes outer timeout signals the helper itself, the helper
        must forward cleanup to the process group before exiting (bounded
        helper semantics) so no orphaned lock holder remains."""
        self.assertIn("signal.SIGTERM", self.helper)
        self.assertIn("signal.SIGINT", self.helper)
        self.assertIn("_shutdown_group(proc, grace, drain)", self.helper)
        self.assertIn("os._exit(128 + signum)", self.helper)

    def test_helper_total_runtime_bounded(self) -> None:
        """The helper must self-bound its total wall time to
        timeout + grace + a small post-KILL drain, so a configured Hermes cron
        timeout >= that bound never preempts cleanup."""
        self.assertIn('timeout = _env_float("GBRAIN_EMBED_REFRESH_TIMEOUT", 240.0)', self.helper)
        self.assertIn('grace = _env_float("GBRAIN_EMBED_REFRESH_KILL_GRACE", 5.0)', self.helper)
        self.assertIn("deadline = time.monotonic() + drain", self.helper)

    def test_helper_accepts_cron_capped_boundary_equality(self) -> None:
        """The cron entrypoint caps with `-ge safe_timeout` where safe_timeout
        equals this helper's maximum. The helper must therefore accept
        timeout == maximum (the capped value) while rejecting timeout strictly
        above it, so a direct invocation still cannot exceed the outer
        deadline."""
        self.assertIn("maximum = outer - grace - drain - margin - 1.0", self.helper)
        self.assertIn("if maximum < 0.1 or timeout > maximum:", self.helper)
        self.assertNotIn("timeout >= maximum", self.helper)

    def test_helper_does_not_acquire_external_lock(self) -> None:
        """The cron entrypoint must not take the tasknotes lock itself; the
        wrapper owns it. The helper must not open or flock any lock file."""
        self.assertNotIn("tasknotes.lock", self.helper)
        self.assertNotIn("fcntl", self.helper)
        self.assertNotIn("flock(", self.helper)

    def test_helper_env_defaults(self) -> None:
        """The helper's command is an immutable constant (the environment
        cannot redirect it to an uncooperative command); the bounded duration
        knobs stay env-driven."""
        self.assertIn('REFRESH_CMD = "/usr/local/bin/josemar-gbrain refresh-embeddings"', self.helper)
        self.assertNotIn("GBRAIN_EMBED_REFRESH_CMD", self.helper)
        self.assertIn("240.0", self.helper)
        self.assertIn("GBRAIN_EMBED_REFRESH_KILL_GRACE", self.helper)

    def test_helper_copied_into_image_and_executable(self) -> None:
        self.assertIn(
            "COPY scripts/hermes-gbrain-embedding-refresh.py "
            "/opt/josemar/scripts/hermes-gbrain-embedding-refresh.py",
            self.dockerfile,
        )
        self.assertIn("hermes-gbrain-embedding-refresh.py", self.dockerfile)
        self.assertIn("/opt/josemar/scripts/hermes-gbrain-embedding-refresh.py \\", self.dockerfile)

    def test_compose_wires_schedule_with_default(self) -> None:
        self.assertIn(
            "GBRAIN_EMBED_REFRESH_SCHEDULE=${GBRAIN_EMBED_REFRESH_SCHEDULE:-0 5 * * *}",
            self.compose,
        )

    def test_compose_wires_timeout_with_default(self) -> None:
        self.assertIn(
            "GBRAIN_EMBED_REFRESH_TIMEOUT=${GBRAIN_EMBED_REFRESH_TIMEOUT:-240}",
            self.compose,
        )

    def test_compose_wires_timeout_hierarchy_defaults(self) -> None:
        for line in (
            "GBRAIN_EMBED_REFRESH_KILL_GRACE=${GBRAIN_EMBED_REFRESH_KILL_GRACE:-5}",
            "GBRAIN_EMBED_REFRESH_GROUP_DRAIN=${GBRAIN_EMBED_REFRESH_GROUP_DRAIN:-2}",
            "GBRAIN_EMBED_REFRESH_TIMEOUT_MARGIN=${GBRAIN_EMBED_REFRESH_TIMEOUT_MARGIN:-10}",
            "HERMES_CRON_SCRIPT_TIMEOUT=${HERMES_CRON_SCRIPT_TIMEOUT:-300}",
        ):
            with self.subTest(line=line):
                self.assertIn(line, self.compose)


class GbrainBunInstallerCurlContractTests(unittest.TestCase):
    """Bun installer stage must apt-install curl."""

    def setUp(self) -> None:
        self.src = _read(DOCKERFILE_PATH)

    def test_bun_installer_stage_installs_curl(self) -> None:
        stage_match = re.search(
            r'FROM\s+\$\{HERMES_BASE_IMAGE\}\s+AS\s+bun-installer(.*?)(?=\nFROM|\Z)',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(stage_match, "Could not find bun-installer stage")
        assert stage_match is not None
        stage = stage_match.group(1)
        apt_match = re.search(r'apt-get\s+install\s+-y.*?(?=&&)', stage, re.DOTALL)
        self.assertIsNotNone(apt_match, "Could not find apt-get install in bun-installer stage")
        assert apt_match is not None
        self.assertIn("curl", apt_match.group(0))


class GbrainBunArchiveLayoutContractTests(unittest.TestCase):
    """The Bun 1.3.14 linux-x64 zip extracts `bun-linux-x64/bun` (no `bin/` dir)."""

    def setUp(self) -> None:
        self.src = _read(DOCKERFILE_PATH)

    def test_bun_installer_moves_real_archive_path(self) -> None:
        stage_match = re.search(
            r'FROM\s+\$\{HERMES_BASE_IMAGE\}\s+AS\s+bun-installer(.*?)(?=\nFROM|\Z)',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(stage_match, "Could not find bun-installer stage")
        assert stage_match is not None
        stage = stage_match.group(1)
        self.assertIn("mv bun-linux-x64/bun /opt/bun/bin/bun", stage)
        self.assertNotIn("bun-linux-x64/bin/bun", stage)


class GbrainHomeSemanticsContractTests(unittest.TestCase):
    """GBRAIN_HOME is the fixed canonical parent; state lives under
    $GBRAIN_HOME/.gbrain."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_gbrain_home_is_fixed(self) -> None:
        self.assertIn('GBRAIN_HOME="/opt/data"', self.src)
        self.assertNotIn("${GBRAIN_HOME:-", self.src)

    def test_state_dir_derived_from_home(self) -> None:
        self.assertIn('GBRAIN_STATE_DIR="${GBRAIN_HOME}/.gbrain"', self.src)

    def test_schema_install_dir_under_state_dir(self) -> None:
        self.assertIn('SCHEMA_INSTALL_DIR="${GBRAIN_STATE_DIR}/schema-packs"', self.src)


class GbrainNativeLauncherBehaviorTests(unittest.TestCase):
    """Issue #112: the private native launcher must enforce
    GBRAIN_SKIP_STARTUP_HOOKS=1 itself, regardless of caller environment.

    The launcher command is materialized exactly as Dockerfile.hermes writes
    it (image paths substituted for local fakes) and executed under hostile
    caller env values; the fake CLI must always observe the enforced value.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gbrain-launcher-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _launcher_command(self) -> str:
        dockerfile = _read(DOCKERFILE_PATH)
        match = re.search(
            r"printf '%s\\n' '#!/bin/sh' '([^']*)' > /opt/josemar/libexec/gbrain-native",
            dockerfile,
        )
        self.assertIsNotNone(
            match, "Could not extract the native launcher line from Dockerfile.hermes"
        )
        assert match is not None
        return match.group(1)

    def _fake_chain(self) -> tuple[Path, Path]:
        """One fake gbrain tree: a `bun` stand-in that execs a fake
        src/cli.ts which logs the enforced env value."""
        gbrain_dir = self.tmp / "gbrain"
        (gbrain_dir / "src").mkdir(parents=True, exist_ok=True)
        cli = gbrain_dir / "src" / "cli.ts"
        cli.write_text(
            "#!/bin/sh\n"
            f"printf 'GBRAIN_SKIP_STARTUP_HOOKS=%s\\n' "
            f"\"${{GBRAIN_SKIP_STARTUP_HOOKS:-}}\" > \"{self.cli_env_log}\"\n",
            encoding="utf-8",
        )
        cli.chmod(0o755)
        fake_bun = self.tmp / "bun"
        fake_bun.write_text(f"#!/bin/sh\nexec \"{cli}\" \"$@\"\n", encoding="utf-8")
        fake_bun.chmod(0o755)
        return gbrain_dir, fake_bun

    def _run_launcher(
        self, caller_value: str | None, gbrain_dir: Path, fake_bun: Path
    ) -> subprocess.CompletedProcess[str]:
        command = (
            self._launcher_command()
            .replace("/opt/gbrain", str(gbrain_dir))
            .replace("/usr/local/bin/bun", str(fake_bun))
        )
        env = os.environ.copy()
        if caller_value is None:
            env.pop("GBRAIN_SKIP_STARTUP_HOOKS", None)
        else:
            env["GBRAIN_SKIP_STARTUP_HOOKS"] = caller_value
        return subprocess.run(
            ["/bin/sh", "-c", command, "sh", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=env,
        )

    def test_launcher_enforces_skip_startup_hooks_for_any_caller_env(self) -> None:
        cases = [("0", "caller sets 0"), ("", "caller sets empty"), (None, "caller unsets")]
        for index, (value, label) in enumerate(cases):
            with self.subTest(caller=label):
                self.cli_env_log = self.tmp / f"cli-env-{index}.log"
                gbrain_dir, fake_bun = self._fake_chain()
                result = self._run_launcher(value, gbrain_dir, fake_bun)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.cli_env_log.read_text(encoding="utf-8").strip(),
                    "GBRAIN_SKIP_STARTUP_HOOKS=1",
                )


if __name__ == "__main__":
    unittest.main()
