"""Source-contract tests for scripts/josemar-gbrain and the Docker gbrain wrapper.

These tests inspect the wrapper source (no Docker, no gbrain binary required)
to guard the simplified direct-CLI gbrain integration:

  - reindex performs initial activation (init, config, sync, extract,
    extract links, schema sync for custom packs, git safe.directory)
  - refresh performs lightweight vault-file reconciliation without init/schema
  - schema pack install logic for custom packs (source path resolution,
    confinement validation, atomic install, native validate)
  - no readiness marker, no gate, no provider stripping for chat actions,
    no per-action functions, no output capping, no timeout resolution
  - the /usr/local/bin/gbrain Docker wrapper must cd to /opt/gbrain
  - Bun installer stage must apt-install curl
  - GBRAIN_HOME is a parent; state lives under $GBRAIN_HOME/.gbrain
"""

from __future__ import annotations

import re
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
        self.assertIn("JOSEMAR_GBRAIN_DROPPED_PRIVS", self.src)
        self.assertIn("su -s /bin/sh hermes", self.src)

    def test_reindex_creates_state_dir(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn('mkdir -p "$GBRAIN_STATE_DIR"', body)


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

    def test_refresh_does_not_acquire_tasknotes_lock(self) -> None:
        """refresh must not acquire the tasknotes lock (no embedding writes)."""
        body = _extract_function(self.src, "do_refresh")
        self.assertNotIn("tasknotes.lock", body)
        self.assertNotIn("flock", body)


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
        lock_pos = body.find("flock")
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
        lock_pos = body.find("flock")
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
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("GBRAIN_TASKNOTES_LOCK", body)

    def test_embed_backfill_uses_flock_nonblocking(self) -> None:
        body = _extract_function(self.src, "do_embed_backfill")
        self.assertIn("flock -n", body)

    def test_embed_backfill_lock_before_gbrain_embed(self) -> None:
        """The lock must be acquired before the `gbrain embed` invocation."""
        body = _extract_function(self.src, "do_embed_backfill")
        lock_pos = body.find("flock -n")
        embed_pos = body.find("embed --stale")
        self.assertGreater(lock_pos, -1, "flock -n must be present")
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


class GbrainEmbedBackfillPreservationContractTests(unittest.TestCase):
    """embed-backfill must not alter the behavior of reindex/refresh.

    reindex and refresh must remain no-embedding and keyword-only. The new
    embed-backfill subcommand is the only wrapper path that produces embeddings.
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

    def test_reindex_does_not_acquire_tasknotes_lock(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertNotIn("tasknotes.lock", body)
        self.assertNotIn("flock", body)

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

    def test_refresh_does_not_acquire_tasknotes_lock(self) -> None:
        body = _extract_function(self.src, "do_refresh")
        self.assertNotIn("tasknotes.lock", body)
        self.assertNotIn("flock", body)

    def test_embed_backfill_is_only_embed_path(self) -> None:
        """Only do_embed_backfill may invoke `gbrain embed --stale`."""
        for func in ("do_reindex", "do_refresh", "run_sync_extract_links"):
            body = _extract_function(self.src, func)
            self.assertNotIn(
                "embed --stale", body,
                f"{func} must not invoke `gbrain embed --stale`",
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
        lock_pos = body.find("flock")
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
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("GBRAIN_TASKNOTES_LOCK", body)

    def test_enable_embeddings_uses_flock_nonblocking(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("flock -n", body)

    def test_enable_embeddings_lock_before_migrate(self) -> None:
        """The lock must be acquired before the `gbrain migrate` invocation."""
        body = _extract_function(self.src, "do_enable_embeddings")
        lock_pos = body.find("flock -n")
        migrate_pos = body.find("migrate embeddings")
        self.assertGreater(lock_pos, -1, "flock -n must be present")
        self.assertGreater(migrate_pos, -1, "migrate embeddings must be present")
        self.assertLess(lock_pos, migrate_pos,
                        "flock acquisition must precede gbrain migrate")

    def test_enable_embeddings_lock_busy_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("enable_embeddings_lock_busy", body)
        self.assertIn('"success": false', body)

    def test_enable_embeddings_lock_unavailable_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_enable_embeddings")
        self.assertIn("enable_embeddings_lock_unavailable", body)

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
            "enable_embeddings_lock_unavailable",
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
        lock_pos = body.find("flock")
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
        self.assertIn("flock -n", body)
        self.assertIn("GBRAIN_TASKNOTES_LOCK", body)

    def test_disable_embeddings_lock_busy_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("disable_embeddings_lock_busy", body)
        self.assertIn('"success": false', body)

    def test_disable_embeddings_lock_unavailable_error_is_structured(self) -> None:
        body = _extract_function(self.src, "do_disable_embeddings")
        self.assertIn("disable_embeddings_lock_unavailable", body)

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
        self.assertIn("python3", body)
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

    reindex and refresh must remain no-embedding and keyword-only. The new
    subcommands are operator-only switches; they must not introduce embedding
    production into reindex/refresh, and embed-backfill must remain the only
    wrapper path that produces embeddings.
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


class GbrainDockerWrapperCwdContractTests(unittest.TestCase):
    """The /usr/local/bin/gbrain Docker wrapper must cd to /opt/gbrain."""

    def setUp(self) -> None:
        self.src = _read(DOCKERFILE_PATH)

    def test_gbrain_wrapper_cds_to_opt_gbrain(self) -> None:
        match = re.search(r"printf.*?/usr/local/bin/gbrain", self.src, re.DOTALL)
        self.assertIsNotNone(match, "Could not find gbrain wrapper creation in Dockerfile")
        assert match is not None
        wrapper_line = match.group(0)
        self.assertIn("cd /opt/gbrain", wrapper_line)

    def test_gbrain_ref_is_pinned_to_the_supported_release(self) -> None:
        self.assertIn(
            "ARG GBRAIN_REF=15b9863d13635d173562a54f55a1d388bfcf546b",
            self.src,
        )

    def test_gbrain_wrapper_uses_relative_cli_path(self) -> None:
        match = re.search(r"printf.*?/usr/local/bin/gbrain", self.src, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        wrapper_line = match.group(0)
        self.assertIn("src/cli.ts", wrapper_line)

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
    """GBRAIN_HOME is a parent; state lives under $GBRAIN_HOME/.gbrain."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_gbrain_home_default_is_parent(self) -> None:
        self.assertRegex(self.src, r'GBRAIN_HOME="\$\{GBRAIN_HOME:-/opt/data\}"')

    def test_state_dir_derived_from_home(self) -> None:
        self.assertIn('GBRAIN_STATE_DIR="${GBRAIN_HOME}/.gbrain"', self.src)

    def test_schema_install_dir_under_state_dir(self) -> None:
        self.assertIn('SCHEMA_INSTALL_DIR="${GBRAIN_STATE_DIR}/schema-packs"', self.src)


if __name__ == "__main__":
    unittest.main()
