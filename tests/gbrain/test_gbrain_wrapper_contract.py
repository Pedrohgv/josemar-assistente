"""Source-contract tests for scripts/josemar-gbrain and the Docker gbrain wrapper.

These tests inspect the wrapper source (no Docker, no gbrain binary required)
to guard against regressions in the Phase 1 native action surface and
activation/config readiness redesign:

  - readiness marker stores activation/config data, NOT vault HEAD
  - marker validation checks pinned ref/version, schema, realpaths,
    sync.repo_path, keyword_only, AND live gbrain config
  - no git HEAD/clean-worktree checks for status/query/actions
  - reindex performs initial activation (init, config, sync, extract, marker)
  - allowed native actions: status, search, get, capture, put, link, backlinks, reindex
  - no generic call/query/sync from chat; old note.* (dotted/underscored) rejected
  - search forces keyword-only and dispatches via `gbrain call search`
  - capture uses argv array with --stdin --json
  - put uses `gbrain call put_page` with JSON content
  - get uses `gbrain call get_page`
  - link uses `gbrain call add_link` and rejects managed link sources
  - backlinks uses `gbrain call get_backlinks`
  - capture/put parse native output for write-through failure
  - provider env stripping for all actions
  - GBRAIN_SKIP_STARTUP_HOOKS=1 exported for all gbrain commands
  - timeouts and output caps on all actions
  - slug validation rejects backslash, URL-encoded traversal, control/bidi chars, long slugs
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


class GbrainActivationMarkerContractTests(unittest.TestCase):
    """Phase 1: readiness marker stores activation/config data, not vault HEAD."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_marker_fields_are_activation_config(self) -> None:
        body = _extract_function(self.src, "marker_is_valid")
        for field in ["gbrain_ref", "gbrain_version", "schema_pack",
                      "gbrain_home_realpath", "brain_repo_realpath",
                      "sync_repo_path", "keyword_only",
                      "schema_source_pack", "schema_source_path",
                      "schema_source_sha256", "schema_installed_sha256",
                      "schema_installed_path", "completed_at"]:
            self.assertIn(field, body, f"Marker must validate field: {field}")
        self.assertNotIn("vault_head", body)

    def test_marker_matches_current_checks_config_not_git(self) -> None:
        body = _extract_function(self.src, "marker_matches_current")
        self.assertIn("$GBRAIN_PINNED_REF", body)
        self.assertIn("$GBRAIN_PINNED_VERSION", body)
        self.assertIn("$GBRAIN_SCHEMA_PACK", body)
        self.assertIn("resolve_realpath", body)
        self.assertIn("sync_repo_path", body)
        self.assertIn("keyword_only", body)
        self.assertNotIn("vault_head", body)
        self.assertNotIn("vault_is_clean", body)

    def test_marker_matches_current_reads_live_config(self) -> None:
        body = _extract_function(self.src, "marker_matches_current")
        # Must read live gbrain config for sync.repo_path and keyword_only.
        self.assertIn("read_gbrain_config", body)
        self.assertIn("sync.repo_path", body)
        self.assertIn("search.mcp_keyword_only", body)

    def test_read_gbrain_config_function(self) -> None:
        body = _extract_function(self.src, "read_gbrain_config")
        self.assertIn("config get", body)
        self.assertIn("GBRAIN_SKIP_STARTUP_HOOKS", body)

    def test_write_marker_stores_activation_config(self) -> None:
        body = _extract_function(self.src, "write_marker")
        self.assertIn("gbrain_ref", body)
        self.assertIn("gbrain_version", body)
        self.assertIn("schema_pack", body)
        self.assertIn("gbrain_home_realpath", body)
        self.assertIn("brain_repo_realpath", body)
        self.assertIn("sync_repo_path", body)
        self.assertIn("keyword_only", body)
        self.assertNotIn("vault_head", body)

    def test_no_vault_head_function(self) -> None:
        self.assertNotIn("vault_head()", self.src)

    def test_no_vault_is_clean_function(self) -> None:
        self.assertNotIn("vault_is_clean()", self.src)

    def test_no_git_clean_check_in_actions(self) -> None:
        for func in ["do_status", "do_search", "do_get", "do_capture",
                     "do_put", "do_link", "do_backlinks", "do_reindex"]:
            body = _extract_function(self.src, func)
            self.assertNotIn("vault_is_clean", body, f"{func} must not check git clean")
            self.assertNotIn("vault_head", body, f"{func} must not check vault HEAD")

    def test_status_does_not_check_git(self) -> None:
        body = _extract_function(self.src, "do_status")
        self.assertNotIn("vault_ok", body)
        self.assertNotIn("vault_head", body)
        self.assertNotIn("vault_is_clean", body)
        self.assertIn("state_dir_exists", body)
        self.assertIn("brain_repo_exists", body)
        self.assertIn("marker_ok", body)
        self.assertIn("marker_matches", body)

    def test_gate_requires_marker_not_git(self) -> None:
        body = _extract_function(self.src, "require_gate")
        self.assertIn("GBRAIN_ENABLED", body)
        self.assertIn("marker_is_valid", body)
        self.assertIn("marker_matches_current", body)
        self.assertNotIn("vault_is_clean", body)
        self.assertNotIn("vault_head", body)


class GbrainSchemaSourcePackContractTests(unittest.TestCase):
    """Schema source pack install/hash marker contract tests."""

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

    def test_installed_pack_path_function(self) -> None:
        body = _extract_function(self.src, "installed_pack_path")
        self.assertIn("SCHEMA_INSTALL_DIR", body)

    def test_compute_sha256_function(self) -> None:
        body = _extract_function(self.src, "compute_sha256")
        self.assertIn("sha256sum", body)

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

    def test_marker_matches_current_checks_schema_hashes(self) -> None:
        body = _extract_function(self.src, "marker_matches_current")
        self.assertIn("schema_source_sha256", body)
        self.assertIn("schema_installed_sha256", body)
        self.assertIn("schema_source_pack", body)

    def test_marker_matches_current_bundled_pack_branch(self) -> None:
        body = _extract_function(self.src, "marker_matches_current")
        self.assertIn("is_bundled_pack", body)
        self.assertIn("bundled", body)

    def test_marker_matches_current_custom_pack_branch(self) -> None:
        body = _extract_function(self.src, "marker_matches_current")
        self.assertIn("source_pack_path", body)
        self.assertIn("compute_sha256", body)
        self.assertIn("installed_pack_path", body)

    def test_write_marker_stores_schema_fields(self) -> None:
        body = _extract_function(self.src, "write_marker")
        self.assertIn("schema_source_pack", body)
        self.assertIn("schema_source_path", body)
        self.assertIn("schema_source_sha256", body)
        self.assertIn("schema_installed_sha256", body)
        self.assertIn("schema_installed_path", body)

    def test_reindex_installs_custom_pack(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("is_bundled_pack", body)
        self.assertIn("source_pack_path", body)
        self.assertIn("install_source_pack", body)
        self.assertIn("validate_installed_pack", body)

    def test_reindex_handles_bundled_pack_fallback(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("bundled", body)

    def test_reindex_fails_on_missing_custom_source(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("schema_source_missing", body)

    def test_reindex_passes_schema_args_to_write_marker(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("write_marker", body)
        # Must pass 5 schema args to write_marker
        self.assertIn("m_source_pack", body)
        self.assertIn("m_source_path", body)
        self.assertIn("m_source_sha256", body)
        self.assertIn("m_installed_sha256", body)
        self.assertIn("m_installed_path", body)

    def test_schema_status_function_exists(self) -> None:
        body = _extract_function(self.src, "do_schema_status")
        self.assertIn("schema_status", body)
        self.assertIn("selected_pack", body)
        self.assertIn("is_bundled", body)
        self.assertIn("source_sha256", body)
        self.assertIn("installed_sha256", body)

    def test_main_dispatches_schema_status(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertIn("schema-status", body)
        self.assertIn("do_schema_status", body)

    def test_no_generic_schema_mutation_in_main(self) -> None:
        body = _extract_function(self.src, "main")
        # Must not dispatch generic schema mutation verbs
        self.assertNotRegex(body, r'schema-use\)')
        self.assertNotRegex(body, r'schema-sync\)')
        self.assertNotRegex(body, r'schema-edit\)')
        self.assertNotRegex(body, r'schema-init\)')
        self.assertNotRegex(body, r'schema-fork\)')

    # --- Must-fix tests ---

    def test_marker_is_valid_uses_bundled_sentinel_for_hashes(self) -> None:
        """Fix 1: marker_is_valid must accept 'bundled' sentinel for
        bundled pack hash fields, not require empty string."""
        body = _extract_function(self.src, "marker_is_valid")
        # Must check schema_source_sha256 and schema_installed_sha256
        self.assertIn("schema_source_sha256", body)
        self.assertIn("schema_installed_sha256", body)

    def test_marker_matches_current_bundled_uses_sentinel_hashes(self) -> None:
        """Fix 1: bundled pack branch must check for 'bundled' sentinel,
        not empty string, for hash fields."""
        body = _extract_function(self.src, "marker_matches_current")
        # The bundled branch must check sentinel "bundled" for hashes.
        self.assertIn('"bundled"', body)

    def test_reindex_bundled_pack_uses_sentinel_hashes(self) -> None:
        """Fix 1: reindex bundled branch must set 'bundled' sentinel for
        hashes, not leave them empty."""
        body = _extract_function(self.src, "do_reindex")
        # Find the bundled branch where m_source_sha256 is set.
        self.assertIn('m_source_sha256="bundled"', body)
        self.assertIn('m_installed_sha256="bundled"', body)

    def test_reindex_schema_install_before_init(self) -> None:
        """Fix 2: schema pack install must happen BEFORE gbrain init."""
        body = _extract_function(self.src, "do_reindex")
        install_pos = body.find("install_source_pack")
        init_pos = body.find("init --pglite")
        self.assertLess(install_pos, init_pos,
                        "schema pack install must precede gbrain init")

    def test_reindex_schema_install_before_sync(self) -> None:
        """Fix 2: schema pack install must happen BEFORE gbrain sync."""
        body = _extract_function(self.src, "do_reindex")
        install_pos = body.find("install_source_pack")
        sync_pos = body.find("sync --full")
        self.assertLess(install_pos, sync_pos,
                        "schema pack install must precede gbrain sync")

    def test_reindex_schema_install_before_extract(self) -> None:
        """Fix 2: schema pack install must happen BEFORE gbrain extract."""
        body = _extract_function(self.src, "do_reindex")
        install_pos = body.find("install_source_pack")
        extract_pos = body.find("extract --stale")
        self.assertLess(install_pos, extract_pos,
                        "schema pack install must precede gbrain extract")

    def test_reindex_schema_sync_apply_after_file_sync(self) -> None:
        """Fix 3: native schema sync --apply must run after file sync."""
        body = _extract_function(self.src, "do_reindex")
        sync_pos = body.find("sync --full")
        schema_sync_pos = body.find("schema sync --apply")
        self.assertLess(sync_pos, schema_sync_pos,
                        "schema sync --apply must follow file sync")

    def test_reindex_schema_sync_before_marker(self) -> None:
        """Fix 3: schema sync --apply must run before write_marker."""
        body = _extract_function(self.src, "do_reindex")
        schema_sync_pos = body.find("schema sync --apply")
        marker_pos = body.find("write_marker")
        self.assertLess(schema_sync_pos, marker_pos,
                        "schema sync --apply must precede write_marker")

    def test_reindex_schema_sync_failure_blocks_readiness(self) -> None:
        """Fix 3: schema sync failure must block readiness (return 1)."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("schema_sync_failed", body)

    def test_reindex_validates_schema_pack_name(self) -> None:
        """Nice-to-have: reindex validates GBRAIN_SCHEMA_PACK name."""
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("validate_schema_pack_name", body)

    def test_validate_schema_pack_name_function(self) -> None:
        """Nice-to-have: validate_schema_pack_name rejects invalid names."""
        body = _extract_function(self.src, "validate_schema_pack_name")
        self.assertIn("invalid_schema_pack_name", body)

    def test_install_source_pack_removes_stale_alternates(self) -> None:
        """Nice-to-have: install removes stale alternate extension files."""
        body = _extract_function(self.src, "install_source_pack")
        self.assertIn("stale", body.lower())

    def test_install_source_pack_uses_atomic_copy(self) -> None:
        """Nice-to-have: install uses temp file + atomic rename."""
        body = _extract_function(self.src, "install_source_pack")
        self.assertIn("tmp", body.lower())
        self.assertIn("mv ", body)

    def test_schema_status_reports_marker_ok(self) -> None:
        """Nice-to-have: schema_status reports marker_ok."""
        body = _extract_function(self.src, "do_schema_status")
        self.assertIn("marker_ok", body)

    def test_schema_status_reports_marker_matches(self) -> None:
        """Nice-to-have: schema_status reports marker_matches."""
        body = _extract_function(self.src, "do_schema_status")
        self.assertIn("marker_matches", body)

    def test_schema_status_reports_gate_open(self) -> None:
        """Nice-to-have: schema_status reports gate_open."""
        body = _extract_function(self.src, "do_schema_status")
        self.assertIn("gate_open", body)

    def test_schema_status_bundled_reports_sentinel_hashes(self) -> None:
        """Nice-to-have: schema_status for bundled packs reports sentinel."""
        body = _extract_function(self.src, "do_schema_status")
        # The bundled branch must set sentinel values.
        self.assertIn('source_sha256="bundled"', body)
        self.assertIn('installed_sha256="bundled"', body)


class GbrainReindexActivationContractTests(unittest.TestCase):
    """Phase 1: reindex performs initial activation, not git-anchored reindex."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_reindex_does_not_require_clean_git(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertNotIn("vault_is_clean", body)
        self.assertNotIn("vault_head", body)
        self.assertNotIn("vault_no_head", body)
        self.assertNotIn("vault_dirty_after_sync", body)

    def test_reindex_performs_init(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("init --pglite --no-embedding", body)

    def test_reindex_configures_repo_path(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("sync.repo_path", body)
        self.assertIn("$GBRAIN_BRAIN_REPO", body)

    def test_reindex_configures_keyword_only(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("config set search.mcp_keyword_only true", body)

    def test_reindex_runs_full_sync(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("sync --full --no-embed", body)
        self.assertIn("--repo", body)
        self.assertIn("$GBRAIN_BRAIN_REPO", body)

    def test_reindex_runs_extract_stale(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("extract --stale", body)

    def test_reindex_writes_activation_marker(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("write_marker", body)

    def test_reindex_does_not_pass_schema_pack_flag(self) -> None:
        match = re.search(r'init_output=\$\(.*?init\b(.*?)2>&1\)', self.src, re.DOTALL)
        self.assertIsNotNone(match, "Could not find init invocation in wrapper")
        init_args = match.group(1)
        self.assertNotIn("--schema-pack", init_args)

    def test_reindex_does_not_pass_non_interactive_flag(self) -> None:
        match = re.search(r'init_output=\$\(.*?init\b(.*?)2>&1\)', self.src, re.DOTALL)
        self.assertIsNotNone(match, "Could not find init invocation in wrapper")
        init_args = match.group(1)
        self.assertNotIn("--non-interactive", init_args)

    def test_reindex_exports_skip_startup_hooks(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("export_gbrain_env", body)

    def test_reindex_strips_provider_env(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn("export_gbrain_env", body)


class GbrainAllowedActionsContractTests(unittest.TestCase):
    """Phase 1: allowed native actions and rejection of old surfaces."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_main_dispatches_allowed_actions(self) -> None:
        body = _extract_function(self.src, "main")
        for action in ["status", "search", "get", "capture", "put", "link", "backlinks", "reindex"]:
            self.assertIn(action, body, f"main must dispatch {action}")

    def test_no_query_action(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertNotRegex(body, r'query\)\s*\n\s*do_query')

    def test_no_do_query_function(self) -> None:
        self.assertNotIn("do_query()", self.src)

    def test_no_generic_call_action(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertNotRegex(body, r'call\)\s*\n\s*do_call')

    def test_no_sync_action(self) -> None:
        body = _extract_function(self.src, "main")
        self.assertNotRegex(body, r'sync\)\s*\n\s*do_sync')

    def test_rejects_dotted_note_routes(self) -> None:
        body = _extract_function(self.src, "main")
        # Must reject note.* (dotted) patterns.
        self.assertIn("note.*", body)

    def test_rejects_underscored_note_routes(self) -> None:
        body = _extract_function(self.src, "main")
        # Must reject note_* (underscored) patterns.
        self.assertIn("note_*", body)

    def test_rejects_query_call_sync_admin(self) -> None:
        body = _extract_function(self.src, "main")
        for name in ["query", "call", "sync", "admin", "files"]:
            self.assertIn(name, body, f"main must reject {name}")


class GbrainSearchContractTests(unittest.TestCase):
    """Phase 1: search replaces query, keyword-only via call search."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_search_uses_call_search(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("call search", body)

    def test_search_sets_keyword_only_before_search(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("config set search.mcp_keyword_only true", body)
        kw_pos = body.find("config set search.mcp_keyword_only true")
        search_pos = body.find("call search")
        self.assertLess(kw_pos, search_pos, "keyword-only config must precede call search")

    def test_search_builds_json_safely(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("search_json=$(python3", body)
        self.assertIn('"query"', body)
        self.assertIn('"limit"', body)
        self.assertIn('"offset"', body)

    def test_search_passes_json_as_single_argv(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertRegex(body, r'call\s+search\s+"\$search_json"')

    def test_search_uses_timeout(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("timeout", body)

    def test_search_validates_limit(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("invalid_limit", body)
        self.assertIn("limit_out_of_range", body)

    def test_search_validates_offset(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("invalid_offset", body)

    def test_search_requires_gate(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("require_gate", body)

    def test_search_exports_skip_startup_hooks(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("export_gbrain_env", body)

    def test_search_timeout_exit_124_handled(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("124", body)
        self.assertIn("gbrain_search_timeout", body)

    def test_search_reads_json_payload(self) -> None:
        body = _extract_function(self.src, "do_search")
        self.assertIn("json_get", body)


class GbrainGetContractTests(unittest.TestCase):
    """Phase 1: get uses call get_page, validates slug."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_get_uses_call_get_page(self) -> None:
        body = _extract_function(self.src, "do_get")
        self.assertIn("call get_page", body)

    def test_get_validates_slug(self) -> None:
        body = _extract_function(self.src, "do_get")
        self.assertIn("validate_slug", body)

    def test_get_requires_gate(self) -> None:
        body = _extract_function(self.src, "do_get")
        self.assertIn("require_gate", body)

    def test_get_uses_timeout(self) -> None:
        body = _extract_function(self.src, "do_get")
        self.assertIn("timeout", body)

    def test_get_exports_env(self) -> None:
        body = _extract_function(self.src, "do_get")
        self.assertIn("export_gbrain_env", body)

    def test_get_reads_json_payload(self) -> None:
        body = _extract_function(self.src, "do_get")
        self.assertIn("json_get", body)


class GbrainCaptureContractTests(unittest.TestCase):
    """Phase 1: capture uses native capture CLI with --stdin --json."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_capture_requires_content(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("missing_content", body)

    def test_capture_enforces_content_cap(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("GBRAIN_CONTENT_MAX_CHARS", body)
        self.assertIn("content_too_long", body)

    def test_capture_uses_stdin_json(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("--stdin", body)
        self.assertIn("--json", body)

    def test_capture_no_file_source_flags(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertNotIn("--file", body)
        self.assertNotIn("--source", body)

    def test_capture_validates_optional_slug(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("validate_slug", body)

    def test_capture_validates_optional_type(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("validate_type", body)

    def test_capture_requires_gate(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("require_gate", body)

    def test_capture_uses_timeout(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("timeout", body)

    def test_capture_exports_env(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("export_gbrain_env", body)

    def test_capture_reads_json_payload(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("json_get", body)

    def test_capture_checks_write_through(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertIn("check_write_through", body)

    def test_capture_no_stderr_merge_before_write_through(self) -> None:
        """Blocker fix: capture must not merge stderr into stdout before
        check_write_through, or stderr noise will corrupt JSON parsing and
        fail-open the write-through check."""
        body = _extract_function(self.src, "do_capture")
        # Must capture stderr to a temp file, not merge with 2>&1.
        self.assertIn("mktemp", body)
        self.assertIn("jg_capture_err", body)
        # Must NOT use 2>&1 for the capture invocation.
        self.assertNotIn("2>&1", body)

    def test_capture_checks_stdout_only_for_write_through(self) -> None:
        """check_write_through must receive stdout only, not combined output."""
        body = _extract_function(self.src, "do_capture")
        self.assertIn('check_write_through "$raw_stdout" "capture"', body)

    def test_capture_includes_stderr_in_envelope(self) -> None:
        """Successful capture should include stderr in the result envelope."""
        body = _extract_function(self.src, "do_capture")
        self.assertIn("stderr", body)

    def test_capture_no_unused_capture_args(self) -> None:
        body = _extract_function(self.src, "do_capture")
        self.assertNotIn("capture_args", body)


class GbrainPutContractTests(unittest.TestCase):
    """Phase 1: put is whole-page upsert via call put_page."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_put_uses_call_put_page(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertIn("call put_page", body)

    def test_put_requires_slug_and_content(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertIn("validate_slug", body)
        self.assertIn("missing_content", body)

    def test_put_enforces_content_cap(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertIn("GBRAIN_CONTENT_MAX_CHARS", body)
        self.assertIn("content_too_long", body)

    def test_put_no_patch_api(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertNotIn("--append", body)
        self.assertNotIn("--patch", body)
        self.assertNotIn("--section", body)
        self.assertNotIn("--frontmatter", body)

    def test_put_requires_gate(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertIn("require_gate", body)

    def test_put_uses_timeout(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertIn("timeout", body)

    def test_put_exports_env(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertIn("export_gbrain_env", body)

    def test_put_builds_json_safely(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertIn("put_json=$(python3", body)
        self.assertIn('"slug"', body)
        self.assertIn('"content"', body)

    def test_put_reads_json_payload(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertIn("json_get", body)

    def test_put_checks_write_through(self) -> None:
        body = _extract_function(self.src, "do_put")
        self.assertIn("check_write_through", body)

    def test_put_no_stderr_merge_before_write_through(self) -> None:
        """Blocker fix: put must not merge stderr into stdout before
        check_write_through, or stderr noise will corrupt JSON parsing and
        fail-open the write-through check."""
        body = _extract_function(self.src, "do_put")
        self.assertIn("mktemp", body)
        self.assertIn("jg_put_err", body)
        self.assertNotIn("2>&1", body)

    def test_put_checks_stdout_only_for_write_through(self) -> None:
        """check_write_through must receive stdout only, not combined output."""
        body = _extract_function(self.src, "do_put")
        self.assertIn('check_write_through "$raw_stdout" "put"', body)

    def test_put_includes_stderr_in_envelope(self) -> None:
        """Successful put should include stderr in the result envelope."""
        body = _extract_function(self.src, "do_put")
        self.assertIn("stderr", body)


class GbrainLinkContractTests(unittest.TestCase):
    """Phase 1: link uses call add_link, rejects managed sources."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_link_uses_call_add_link(self) -> None:
        body = _extract_function(self.src, "do_link")
        self.assertIn("call add_link", body)

    def test_link_requires_from_and_to(self) -> None:
        body = _extract_function(self.src, "do_link")
        self.assertIn("validate_slug", body)

    def test_link_rejects_managed_sources(self) -> None:
        self.assertIn("MANAGED_LINK_SOURCES", self.src)
        for managed in ["markdown", "frontmatter", "mentions", "wikilink-resolved"]:
            self.assertIn(managed, self.src, f"MANAGED_LINK_SOURCES must include {managed}")
        body = _extract_function(self.src, "validate_link_source")
        self.assertIn("MANAGED_LINK_SOURCES", body)
        self.assertIn("managed_link_source", body)

    def test_link_validates_link_type(self) -> None:
        body = _extract_function(self.src, "do_link")
        self.assertIn("validate_link_type", body)

    def test_link_bounds_context(self) -> None:
        body = _extract_function(self.src, "do_link")
        self.assertIn("validate_bounded_string", body)
        self.assertIn("context", body)

    def test_link_requires_gate(self) -> None:
        body = _extract_function(self.src, "do_link")
        self.assertIn("require_gate", body)

    def test_link_uses_timeout(self) -> None:
        body = _extract_function(self.src, "do_link")
        self.assertIn("timeout", body)

    def test_link_exports_env(self) -> None:
        body = _extract_function(self.src, "do_link")
        self.assertIn("export_gbrain_env", body)

    def test_managed_link_sources_constant(self) -> None:
        self.assertIn("MANAGED_LINK_SOURCES", self.src)
        self.assertIn("wikilink-resolved", self.src)

    def test_link_reads_json_payload(self) -> None:
        body = _extract_function(self.src, "do_link")
        self.assertIn("json_get", body)


class GbrainBacklinksContractTests(unittest.TestCase):
    """Phase 1: backlinks uses call get_backlinks."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_backlinks_uses_call_get_backlinks(self) -> None:
        body = _extract_function(self.src, "do_backlinks")
        self.assertIn("call get_backlinks", body)

    def test_backlinks_validates_slug(self) -> None:
        body = _extract_function(self.src, "do_backlinks")
        self.assertIn("validate_slug", body)

    def test_backlinks_requires_gate(self) -> None:
        body = _extract_function(self.src, "do_backlinks")
        self.assertIn("require_gate", body)

    def test_backlinks_uses_timeout(self) -> None:
        body = _extract_function(self.src, "do_backlinks")
        self.assertIn("timeout", body)

    def test_backlinks_exports_env(self) -> None:
        body = _extract_function(self.src, "do_backlinks")
        self.assertIn("export_gbrain_env", body)

    def test_backlinks_reads_json_payload(self) -> None:
        body = _extract_function(self.src, "do_backlinks")
        self.assertIn("json_get", body)


class GbrainWriteThroughContractTests(unittest.TestCase):
    """Phase 1: capture/put parse native output for write-through failure."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_check_write_through_function(self) -> None:
        body = _extract_function(self.src, "check_write_through")
        self.assertIn("write_through_degraded", body)
        self.assertIn("written", body)

    def test_check_write_through_detects_written_false(self) -> None:
        body = _extract_function(self.src, "check_write_through")
        self.assertIn('"written"', body)

    def test_check_write_through_detects_write_through_dict(self) -> None:
        body = _extract_function(self.src, "check_write_through")
        self.assertIn("write_through", body)
        self.assertIn("error", body)
        self.assertIn("skipped", body)


class GbrainSlugValidationContractTests(unittest.TestCase):
    """Phase 1: strengthened slug validation."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_validate_slug_rejects_traversal(self) -> None:
        body = _extract_function(self.src, "validate_slug")
        self.assertIn("..", body)

    def test_validate_slug_rejects_backslash(self) -> None:
        body = _extract_function(self.src, "validate_slug")
        self.assertIn("backslash", body.lower())

    def test_validate_slug_rejects_url_encoded(self) -> None:
        body = _extract_function(self.src, "validate_slug")
        self.assertIn("%2e", body.lower())
        self.assertIn("%2f", body.lower())
        self.assertIn("%5c", body.lower())

    def test_validate_slug_rejects_control_chars(self) -> None:
        body = _extract_function(self.src, "validate_slug")
        self.assertIn("control", body.lower())

    def test_validate_slug_rejects_bidi_chars(self) -> None:
        body = _extract_function(self.src, "validate_slug")
        self.assertIn("bidi", body.lower())

    def test_validate_slug_rejects_long_slugs(self) -> None:
        body = _extract_function(self.src, "validate_slug")
        self.assertIn("512", body)

    def test_validate_slug_rejects_leading_slash(self) -> None:
        body = _extract_function(self.src, "validate_slug")
        self.assertIn("start", body.lower())


class GbrainEnvStrippingContractTests(unittest.TestCase):
    """Provider env stripping and startup hooks for all actions."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_strip_provider_env_preserved(self) -> None:
        body = _extract_function(self.src, "strip_provider_env")
        self.assertIn("VOYAGE_API_KEY", body)
        self.assertIn("OPENAI_API_KEY", body)
        self.assertIn("GBRAIN_EMBEDDING_API_KEY", body)

    def test_export_gbrain_env_exports_skip_startup_hooks(self) -> None:
        body = _extract_function(self.src, "export_gbrain_env")
        self.assertIn("export GBRAIN_SKIP_STARTUP_HOOKS=1", body)
        self.assertIn("strip_provider_env", body)

    def test_all_actions_export_env(self) -> None:
        for func in ["do_search", "do_get", "do_capture", "do_put",
                     "do_link", "do_backlinks", "do_reindex"]:
            body = _extract_function(self.src, func)
            self.assertIn("export_gbrain_env", body, f"{func} must call export_gbrain_env")


class GbrainTimeoutContractTests(unittest.TestCase):
    """Timeout validation and fallback default."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_timeout_validated_as_positive_integer(self) -> None:
        self.assertIn("GBRAIN_QUERY_TIMEOUT_SECONDS", self.src)
        self.assertIsNotNone(
            re.search(r'case\s+"\$GBRAIN_QUERY_TIMEOUT_SECONDS"\s+in.*?\*\[!0-9\]\*', self.src, re.DOTALL),
            "Timeout env is not validated against non-numeric values",
        )

    def test_timeout_fallback_default(self) -> None:
        body = _extract_function(self.src, "resolve_timeout")
        self.assertIn('"30"', body)

    def test_resolve_timeout_function(self) -> None:
        body = _extract_function(self.src, "resolve_timeout")
        self.assertIn("GBRAIN_QUERY_TIMEOUT_SECONDS", body)
        self.assertIn("30", body)


class GbrainOutputCapContractTests(unittest.TestCase):
    """Output caps on all actions."""

    def setUp(self) -> None:
        self.src = _read(WRAPPER_PATH)

    def test_cap_output_function(self) -> None:
        body = _extract_function(self.src, "cap_output")
        self.assertIn("GBRAIN_QUERY_MAX_OUTPUT_CHARS", body)
        self.assertIn("head -c", body)

    def test_all_actions_cap_output(self) -> None:
        for func in ["do_search", "do_get", "do_capture", "do_put",
                     "do_link", "do_backlinks"]:
            body = _extract_function(self.src, func)
            self.assertIn("cap_output", body, f"{func} must cap output")


class GbrainDockerWrapperCwdContractTests(unittest.TestCase):
    """The /usr/local/bin/gbrain Docker wrapper must cd to /opt/gbrain."""

    def setUp(self) -> None:
        self.src = _read(DOCKERFILE_PATH)

    def test_gbrain_wrapper_cds_to_opt_gbrain(self) -> None:
        match = re.search(r"printf.*?/usr/local/bin/gbrain", self.src, re.DOTALL)
        self.assertIsNotNone(match, "Could not find gbrain wrapper creation in Dockerfile")
        wrapper_line = match.group(0)
        self.assertIn("cd /opt/gbrain", wrapper_line)

    def test_gbrain_wrapper_uses_relative_cli_path(self) -> None:
        match = re.search(r"printf.*?/usr/local/bin/gbrain", self.src, re.DOTALL)
        self.assertIsNotNone(match)
        wrapper_line = match.group(0)
        self.assertIn("src/cli.ts", wrapper_line)

    def test_chat_skill_has_unambiguous_short_alias(self) -> None:
        self.assertIn("/usr/local/bin/gbrain-skill", self.src)
        self.assertIn("/opt/josemar/skills/gbrain/gbrain", self.src)


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
        stage = stage_match.group(1)
        apt_match = re.search(r'apt-get\s+install\s+-y.*?(?=&&)', stage, re.DOTALL)
        self.assertIsNotNone(apt_match, "Could not find apt-get install in bun-installer stage")
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

    def test_marker_path_under_state_dir(self) -> None:
        self.assertIn('MARKER_PATH="${GBRAIN_STATE_DIR}/${MARKER_NAME}"', self.src)

    def test_write_marker_creates_state_dir(self) -> None:
        body = _extract_function(self.src, "write_marker")
        self.assertIn('mkdir -p "$GBRAIN_STATE_DIR"', body)

    def test_reindex_creates_state_dir(self) -> None:
        body = _extract_function(self.src, "do_reindex")
        self.assertIn('mkdir -p "$GBRAIN_STATE_DIR"', body)

    def test_enabled_default_is_true(self) -> None:
        self.assertRegex(self.src, r'GBRAIN_ENABLED="\$\{GBRAIN_ENABLED:-true\}"')


if __name__ == "__main__":
    unittest.main()
