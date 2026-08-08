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
