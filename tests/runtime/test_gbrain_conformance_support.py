"""Fast host-side contract tests for the gbrain conformance support layer
(issue #127 W1b). No Docker required: these guard the pure helpers and the
enforced isolation contract so the opt-in Docker conformance scenarios cannot
silently regress when RUN_DOCKER_TESTS is unset."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from .gbrain_conformance_support import (
    CANONICAL_PACK_SOURCE,
    CONFORMANCE_EMPTY_ENV_KEYS,
    GBRAIN_CANONICAL_PATCH_FILE,
    GBRAIN_CONFORMANCE_BASELINE_REF_ENV,
    GBRAIN_LEGACY_PATCH_MAPPING,
    SYNC_MANIFEST_SOURCE,
    CommandEvidence,
    GbrainConformanceRuntime,
    OWNED_JOB_NAMES,
    REPO_ROOT,
    baseline_override_active,
    conformance_report_dir,
    effective_baseline_ref,
    normalize_baseline_ref,
    normalize_candidate_ref,
    parse_dockerfile_gbrain_ref,
    parse_gbrain_ref_text,
    resolve_gbrain_patch_file,
    seed_source_state,
    write_report,
)


class CandidateRefValidationTests(unittest.TestCase):
    """Strict exact 40-hex candidate validation, normalized lower-case before
    Docker."""

    def test_valid_uppercase_normalized_to_lowercase(self) -> None:
        ref = "A" * 40
        self.assertEqual(normalize_candidate_ref(ref), "a" * 40)

    def test_valid_lowercase_unchanged(self) -> None:
        ref = "b" * 40
        self.assertEqual(normalize_candidate_ref(ref), ref)

    def test_valid_mixed_case_normalized(self) -> None:
        ref = "aBcD" * 10
        self.assertEqual(normalize_candidate_ref(ref), ref.lower())

    def test_rejects_short_sha(self) -> None:
        with self.assertRaises(ValueError):
            normalize_candidate_ref("abc123")

    def test_rejects_long_sha(self) -> None:
        with self.assertRaises(ValueError):
            normalize_candidate_ref("a" * 41)

    def test_rejects_non_hex_characters(self) -> None:
        with self.assertRaises(ValueError):
            normalize_candidate_ref("g" + "a" * 39)

    def test_rejects_branch_name(self) -> None:
        for ref in ("main", "master", "feature/foo", "feat-127"):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    normalize_candidate_ref(ref)

    def test_rejects_tag(self) -> None:
        with self.assertRaises(ValueError):
            normalize_candidate_ref("v1.0.0")

    def test_rejects_url(self) -> None:
        with self.assertRaises(ValueError):
            normalize_candidate_ref("https://github.com/org/repo/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    def test_rejects_shell_fragment(self) -> None:
        with self.assertRaises(ValueError):
            normalize_candidate_ref("$(rm -rf /)")

    def test_rejects_empty_and_whitespace(self) -> None:
        for ref in ("", "   ", "\n"):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    normalize_candidate_ref(ref)

    def test_rejects_non_string(self) -> None:
        for ref in (None, 123, ["a" * 40]):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    normalize_candidate_ref(ref)  # type: ignore[arg-type]


class BaselineOverrideTests(unittest.TestCase):
    """Upgrade-only baseline override: optional (absent -> current behavior
    unchanged), exact 40-hex validated BEFORE any Docker invocation (fail
    closed), and never readable by core conformance paths."""

    def test_effective_baseline_without_override_is_dockerfile_ref(self) -> None:
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: ""}):
            self.assertEqual(effective_baseline_ref(), parse_dockerfile_gbrain_ref())
        self.assertFalse(baseline_override_active())

    def test_effective_baseline_with_override_is_validated_lowercased(self) -> None:
        ref = "aBcD" * 10
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: ref}):
            self.assertTrue(baseline_override_active())
            self.assertEqual(effective_baseline_ref(), ref.lower())

    def test_normalize_baseline_ref_uses_exact_40_hex_machinery(self) -> None:
        ref = "A" * 40
        self.assertEqual(normalize_baseline_ref(ref), "a" * 40)
        for bad in ("main", "abc123", "v1.0.0", "g" + "a" * 39, "a" * 41, "   "):
            with self.subTest(ref=bad):
                with self.assertRaises(ValueError):
                    normalize_baseline_ref(bad)

    def test_effective_baseline_rejects_invalid_override_fail_closed(self) -> None:
        for ref in ("main", "abc123", "v0.46.25.0", "g" + "a" * 39, "a" * 41):
            with self.subTest(ref=ref):
                with mock.patch.dict(
                    os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: ref}
                ):
                    with self.assertRaises(ValueError):
                        effective_baseline_ref()

    def test_whitespace_only_override_is_absent(self) -> None:
        """Empty/whitespace-only means NO override: absent behavior unchanged
        (the Dockerfile pin is the effective baseline)."""
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: "  "}):
            self.assertFalse(baseline_override_active())
            self.assertEqual(effective_baseline_ref(), parse_dockerfile_gbrain_ref())

    def test_baseline_gbrain_ref_stays_dockerfile_pin_with_override_set(self) -> None:
        """Core contract (requirement 3): ``baseline_gbrain_ref()`` is ALWAYS
        the committed Dockerfile pin; the override only affects the explicit
        effective-baseline helper."""
        runtime = GbrainConformanceRuntime()
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: "a" * 40}):
            self.assertEqual(
                runtime.baseline_gbrain_ref(), parse_dockerfile_gbrain_ref()
            )
            self.assertEqual(runtime.effective_baseline_gbrain_ref(), "a" * 40)

    def test_up_baseline_without_override_is_up_unchanged(self) -> None:
        runtime = GbrainConformanceRuntime()
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: ""}):
            with mock.patch.object(runtime, "up") as up_mock:
                with mock.patch.object(runtime, "build") as build_mock:
                    with mock.patch.object(runtime, "start") as start_mock:
                        runtime.up_baseline("hermes", timeout=900)
        up_mock.assert_called_once_with("hermes", timeout=900)
        build_mock.assert_not_called()
        start_mock.assert_not_called()

    def test_up_baseline_with_override_builds_validated_args_then_starts(self) -> None:
        runtime = GbrainConformanceRuntime()
        ref = "A" * 40
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: ref}):
            with mock.patch.object(runtime, "up") as up_mock:
                with mock.patch.object(runtime, "build") as build_mock:
                    with mock.patch.object(runtime, "start") as start_mock:
                        runtime.up_baseline("hermes", timeout=900)
        up_mock.assert_not_called()
        build_mock.assert_called_once_with(
            "hermes",
            build_args={
                "GBRAIN_REF": ref.lower(),
                "GBRAIN_PATCH_FILE": GBRAIN_CANONICAL_PATCH_FILE,
            },
            timeout=900,
        )
        start_mock.assert_called_once_with("hermes", timeout=900)

    def test_up_baseline_invalid_override_fails_closed_before_docker(self) -> None:
        runtime = GbrainConformanceRuntime()
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: "main"}):
            with mock.patch.object(runtime, "up") as up_mock:
                with mock.patch.object(runtime, "build") as build_mock:
                    with mock.patch.object(runtime, "start") as start_mock:
                        with self.assertRaises(ValueError):
                            runtime.up_baseline("hermes")
        up_mock.assert_not_called()
        build_mock.assert_not_called()
        start_mock.assert_not_called()

    def test_build_baseline_with_override_passes_validated_build_args(self) -> None:
        runtime = GbrainConformanceRuntime()
        ref = "aBcD" * 10
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: ref}):
            with mock.patch.object(runtime, "build") as build_mock:
                runtime.build_baseline("hermes")
        build_mock.assert_called_once_with(
            "hermes",
            build_args={
                "GBRAIN_REF": ref.lower(),
                "GBRAIN_PATCH_FILE": GBRAIN_CANONICAL_PATCH_FILE,
            },
            timeout=900,
        )

    def test_up_baseline_with_override_for_old_pin_selects_legacy_patch(self) -> None:
        """Requirement 3: a known historical pin drives BOTH validated build
        args — the pin itself and its exact legacy patch file."""
        runtime = GbrainConformanceRuntime()
        old_ref = "15b9863d13635d173562a54f55a1d388bfcf546b"
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: old_ref}):
            with mock.patch.object(runtime, "build") as build_mock:
                with mock.patch.object(runtime, "start") as start_mock:
                    runtime.up_baseline("hermes")
        build_mock.assert_called_once_with(
            "hermes",
            build_args={
                "GBRAIN_REF": old_ref,
                "GBRAIN_PATCH_FILE": (
                    "legacy/gbrain-inline-worker-gateway.0.42.73.2.patch"
                ),
            },
            timeout=600,
        )
        start_mock.assert_called_once_with("hermes", timeout=600)

    def test_build_baseline_invalid_override_fails_closed_before_docker(self) -> None:
        runtime = GbrainConformanceRuntime()
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: "main"}):
            with mock.patch.object(runtime, "build") as build_mock:
                with self.assertRaises(ValueError):
                    runtime.build_baseline("hermes")
        build_mock.assert_not_called()

    def test_candidate_build_semantics_untouched_by_override(self) -> None:
        """Requirement 4: candidate build semantics never change; the
        override affects baseline paths only."""
        runtime = GbrainConformanceRuntime()
        candidate = "b" * 40
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: "a" * 40}):
            with mock.patch.object(runtime, "build") as build_mock:
                runtime.build_candidate(candidate, "hermes")
        build_mock.assert_called_once_with(
            "hermes", build_args={"GBRAIN_REF": candidate}, timeout=900
        )

    def test_core_conformance_modules_never_read_the_override(self) -> None:
        """Core conformance stays bound to the Dockerfile pin: the core
        modules must not reference the override env, the effective-baseline
        helper, or the override-aware ``up_baseline`` path."""
        for name in (
            "test_gbrain_conformance.py",
            "test_gbrain_conformance_embeddings.py",
            "test_gbrain_conformance_chronicle.py",
            "gbrain_conformance_scenarios.py",
        ):
            text = (REPO_ROOT / "tests" / "runtime" / name).read_text(encoding="utf-8")
            for forbidden in (
                "GBRAIN_CONFORMANCE_BASELINE_REF",
                "effective_baseline_ref",
                "up_baseline",
            ):
                self.assertNotIn(forbidden, text, f"{name} must not use {forbidden}")


class DockerfileGbrainRefParserTests(unittest.TestCase):
    """Canonical single-default GBRAIN_REF parsing with clear malformed and
    ambiguous errors."""

    def test_parses_real_dockerfile(self) -> None:
        ref = parse_dockerfile_gbrain_ref()
        self.assertRegex(ref, r"^[0-9a-f]{40}$")

    def test_parses_single_valid_arg(self) -> None:
        ref = "a" * 40
        text = f"ARG GBRAIN_REF={ref.upper()}\n"
        self.assertEqual(parse_gbrain_ref_text(text), ref)

    def test_parses_ignores_other_args(self) -> None:
        ref = "b" * 40
        text = (
            "ARG HERMES_BASE_IMAGE=nousresearch/hermes-agent:v2026.8.18\n"
            f"ARG GBRAIN_REF={ref}\n"
            "ARG BUN_VERSION=1.3.14\n"
        )
        self.assertEqual(parse_gbrain_ref_text(text), ref)

    def test_rejects_malformed_non_hex(self) -> None:
        with self.assertRaisesRegex(ValueError, "malformed"):
            parse_gbrain_ref_text("ARG GBRAIN_REF=master\n")

    def test_rejects_malformed_short(self) -> None:
        with self.assertRaisesRegex(ValueError, "malformed"):
            parse_gbrain_ref_text("ARG GBRAIN_REF=abc123\n")

    def test_rejects_bare_arg_without_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "malformed"):
            parse_gbrain_ref_text("ARG GBRAIN_REF\n")

    def test_rejects_ambiguous_multiple_definitions(self) -> None:
        text = f"ARG GBRAIN_REF={'a' * 40}\nARG GBRAIN_REF={'b' * 40}\n"
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            parse_gbrain_ref_text(text)

    def test_rejects_no_definition(self) -> None:
        with self.assertRaisesRegex(ValueError, "no ARG GBRAIN_REF"):
            parse_gbrain_ref_text("ARG HERMES_BASE_IMAGE=foo\n")


# The pre-upgrade gbrain pin whose historical patch is preserved under
# patches/legacy/ (v0.42.73.2; the production patch captured at immutable
# commit 1fc78e6, immediately before the v0.46.25.0 upgrade).
LEGACY_OLD_REF = "15b9863d13635d173562a54f55a1d388bfcf546b"
LEGACY_PATCH_REL = "legacy/gbrain-inline-worker-gateway.0.42.73.2.patch"


class LegacyPatchMappingTests(unittest.TestCase):
    """Requirement 3: historical baseline pins resolve to their exact legacy
    patch file; every other ref resolves to the canonical current patch. The
    mapping is static — patch selection is derived from the validated ref and
    is never user-controlled."""

    def test_mapping_contains_only_the_old_pin_with_its_legacy_path(self) -> None:
        self.assertEqual(
            GBRAIN_LEGACY_PATCH_MAPPING,
            {LEGACY_OLD_REF: LEGACY_PATCH_REL},
        )

    def test_known_old_ref_resolves_to_legacy_patch(self) -> None:
        self.assertEqual(resolve_gbrain_patch_file(LEGACY_OLD_REF), LEGACY_PATCH_REL)

    def test_known_old_ref_uppercase_normalized_before_lookup(self) -> None:
        self.assertEqual(
            resolve_gbrain_patch_file(LEGACY_OLD_REF.upper()), LEGACY_PATCH_REL
        )

    def test_unknown_ref_resolves_to_canonical_current_patch(self) -> None:
        for ref in ("a" * 40, "b" * 40, "f" * 40):
            with self.subTest(ref=ref):
                self.assertEqual(
                    resolve_gbrain_patch_file(ref), GBRAIN_CANONICAL_PATCH_FILE
                )

    def test_invalid_ref_fails_closed_before_selection(self) -> None:
        for ref in ("main", "abc123", "v0.42.73.2", "g" + "a" * 39, "a" * 41, "  "):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    resolve_gbrain_patch_file(ref)

    def test_legacy_patch_file_exists_under_patches(self) -> None:
        path = REPO_ROOT / "patches" / LEGACY_PATCH_REL
        self.assertTrue(path.is_file(), "legacy patch file missing under patches/")

    def test_legacy_patch_is_distinguishable_from_current_patch_bytes(self) -> None:
        legacy = (REPO_ROOT / "patches" / LEGACY_PATCH_REL).read_bytes()
        current = (REPO_ROOT / "patches" / GBRAIN_CANONICAL_PATCH_FILE).read_bytes()
        self.assertNotEqual(legacy, current)

    def test_legacy_patch_is_exact_bytes_of_validated_historical_commit(self) -> None:
        """Requirement 2: byte-identical to
        ``git show 1fc78e6:patches/gbrain-inline-worker-gateway.patch`` — the
        production patch at immutable pre-upgrade commit 1fc78e6, immediately
        before the v0.46.25.0 upgrade (not merely the older pin-introduction
        commit 4f6a7c6) — no added header that could change apply behavior."""
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "show",
                "1fc78e6:patches/gbrain-inline-worker-gateway.patch",
            ],
            capture_output=True,
            check=True,
        )
        self.assertEqual(
            (REPO_ROOT / "patches" / LEGACY_PATCH_REL).read_bytes(), proc.stdout
        )


class DockerfilePatchSelectionTests(unittest.TestCase):
    """Requirement 1 + 4: Dockerfile ``GBRAIN_PATCH_FILE`` semantics — the
    canonical current default, the selected-copy source, and the fail-loud
    existence check before ``git apply`` with no fallback/skip behavior."""

    def setUp(self) -> None:
        self.src = (REPO_ROOT / "Dockerfile.hermes").read_text(encoding="utf-8")

    def test_canonical_patch_file_matches_dockerfile_default(self) -> None:
        """The support-layer canonical name must equal the committed
        Dockerfile default so a build without build args applies the same
        current patch as before."""
        self.assertIn(
            f"ARG GBRAIN_PATCH_FILE={GBRAIN_CANONICAL_PATCH_FILE}", self.src
        )

    def test_patch_copy_uses_selected_arg_to_existing_temp_destination(self) -> None:
        self.assertIn(
            "COPY patches/${GBRAIN_PATCH_FILE} "
            "/tmp/gbrain-inline-worker-gateway.patch",
            self.src,
        )

    def test_patch_apply_is_fail_loud_with_existence_check_before_apply(self) -> None:
        block = re.search(r"RUN cd /opt/gbrain.*?git apply[^\n]*", self.src, re.DOTALL)
        self.assertIsNotNone(block, "git apply block not found in Dockerfile")
        assert block is not None
        apply_block = block.group(0)
        self.assertIn("test -f /tmp/gbrain-inline-worker-gateway.patch", apply_block)
        self.assertLess(
            apply_block.find("test -f"),
            apply_block.find("git apply"),
            "the fail-closed existence check must precede git apply",
        )
        # Fail-loud only: no fallback/skip escape hatches around the apply.
        self.assertNotIn("|| true", apply_block)
        self.assertNotIn("|| :", apply_block)
        self.assertNotIn("if [ -f", apply_block)


class SourceStateSeedingTests(unittest.TestCase):
    """Disposable source-state seeding from the real template, preserving the
    expected paths and rejecting escape."""

    def test_seeds_real_template_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="seed-") as tmp:
            state_dir = Path(tmp)
            manifest_dst, pack_dst = seed_source_state(state_dir)
            self.assertTrue(manifest_dst.is_file())
            self.assertTrue(pack_dst.is_file())

    def test_preserves_expected_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="seed-paths-") as tmp:
            state_dir = Path(tmp)
            seed_source_state(state_dir)
            self.assertTrue((state_dir / ".sync-manifest").is_file())
            self.assertTrue(
                (state_dir / ".gbrain" / "schema-packs" / "josemar" / "pack.yaml").is_file()
            )

    def test_content_is_byte_identical_to_template(self) -> None:
        with tempfile.TemporaryDirectory(prefix="seed-bytes-") as tmp:
            state_dir = Path(tmp)
            manifest_dst, pack_dst = seed_source_state(state_dir)
            self.assertEqual(manifest_dst.read_bytes(), SYNC_MANIFEST_SOURCE.read_bytes())
            self.assertEqual(pack_dst.read_bytes(), CANONICAL_PACK_SOURCE.read_bytes())

    def test_copies_only_the_two_canonical_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="seed-only-") as tmp:
            state_dir = Path(tmp)
            seed_source_state(state_dir)
            manifest = state_dir / ".sync-manifest"
            pack = state_dir / ".gbrain" / "schema-packs" / "josemar" / "pack.yaml"
            all_files = [p for p in state_dir.rglob("*") if p.is_file()]
            self.assertEqual(sorted(all_files), sorted([manifest, pack]))

    def test_rejects_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="seed-abs-") as tmp:
            with self.assertRaises(ValueError):
                seed_source_state(Path(tmp), manifest_rel=Path("/etc/passwd"))

    def test_rejects_parent_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="seed-dotdot-") as tmp:
            with self.assertRaises(ValueError):
                seed_source_state(Path(tmp), manifest_rel=Path("../escape.yml"))

    def test_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="seed-escape-") as tmp:
            root = Path(tmp)
            with tempfile.TemporaryDirectory(prefix="seed-outside-") as outside_tmp:
                outside = Path(outside_tmp)
                (root / "link").symlink_to(outside)
                with self.assertRaises(ValueError):
                    seed_source_state(root, manifest_rel=Path("link/escape.yml"))


class ConformanceReportTests(unittest.TestCase):
    """Reports under dump_folder/gbrain-conformance contain synthetic
    command/result metadata only and never environment dumps."""

    @staticmethod
    def _sample_evidence() -> list[CommandEvidence]:
        return [
            CommandEvidence(
                command=["gbrain", "status", "--json"],
                returncode=0,
                stdout='{"ok": true}',
                stderr="",
                elapsed_seconds=0.5,
            ),
            CommandEvidence(
                command=["gbrain", "get", "notes/welcome"],
                returncode=1,
                stdout="",
                stderr="boom",
                elapsed_seconds=1.25,
            ),
        ]

    def test_default_report_dir_is_dump_folder_gbrain_conformance(self) -> None:
        self.assertEqual(
            conformance_report_dir(),
            REPO_ROOT / "dump_folder" / "gbrain-conformance",
        )

    def test_write_report_contains_command_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="report-") as tmp:
            path = write_report(
                Path(tmp),
                "sample",
                self._sample_evidence(),
                metadata={"baseline_ref": "a" * 40},
            )
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "sample")
        self.assertEqual(data["metadata"]["baseline_ref"], "a" * 40)
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["results"][0]["returncode"], 0)
        self.assertEqual(data["results"][0]["stdout"], '{"ok": true}')
        self.assertEqual(data["results"][1]["stderr"], "boom")
        self.assertIn("elapsed_seconds", data["results"][0])

    def test_report_structure_has_no_environment_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="report-struct-") as tmp:
            path = write_report(Path(tmp), "struct", self._sample_evidence())
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(data.keys()), {"name", "results"})
        for result in data["results"]:
            self.assertEqual(
                set(result.keys()),
                {"command", "returncode", "stdout", "stderr", "elapsed_seconds"},
            )

    def test_report_excludes_process_and_runtime_environment(self) -> None:
        runtime = GbrainConformanceRuntime()
        with tempfile.TemporaryDirectory(prefix="report-env-") as tmp:
            path = write_report(Path(tmp), "env-check", self._sample_evidence())
            text = path.read_text(encoding="utf-8")
        env_names = set(os.environ) | set(runtime.env)
        for key in env_names:
            # Skip trivial keys (e.g. `_`) that are substrings of ordinary
            # report tokens; the meaningful guard is on distinctive names.
            if len(key) < 5:
                continue
            self.assertNotIn(key, text, f"report must not contain env key {key!r}")
        for value in list(os.environ.values()) + list(runtime.env.values()):
            if value and len(value) >= 12:
                self.assertNotIn(value, text, "report must not contain env values")


class GbrainConformanceRuntimeEnvTests(unittest.TestCase):
    """The conformance runtime enforces the isolated default environment."""

    def test_workspace_sync_disabled(self) -> None:
        runtime = GbrainConformanceRuntime()
        self.assertEqual(runtime.env["WORKSPACE_SYNC_ON_START"], "false")
        self.assertEqual(runtime.env["WORKSPACE_SYNC_INTERVAL"], "0")
        self.assertEqual(runtime.env["WORKSPACE_STATE_REPO"], "")
        self.assertEqual(runtime.env["WORKSPACE_REPO_TOKEN"], "")

    def test_telegram_and_provider_credentials_blanked(self) -> None:
        runtime = GbrainConformanceRuntime()
        for key in CONFORMANCE_EMPTY_ENV_KEYS:
            self.assertEqual(runtime.env.get(key, ""), "", key)

    def test_owned_jobs_disabled(self) -> None:
        runtime = GbrainConformanceRuntime()
        self.assertEqual(runtime.env["GBRAIN_REFRESH_INTERVAL"], "0")
        self.assertEqual(runtime.env["GBRAIN_EMBED_REFRESH_SCHEDULE"], "0")
        self.assertEqual(runtime.env["VAULT_RECOVERY_EXPORT_ENABLED"], "false")

    def test_owned_job_names_cover_all_owned_jobs(self) -> None:
        self.assertEqual(
            OWNED_JOB_NAMES,
            ("gbrain-refresh", "gbrain-embedding-refresh", "vault-recovery-export"),
        )

    def test_no_sidecar_profiles(self) -> None:
        runtime = GbrainConformanceRuntime()
        self.assertEqual(runtime.env["COMPOSE_PROFILES"], "")

    def test_dashboard_credentials_still_set_for_compose_render(self) -> None:
        """The conformance env must NOT blank the dashboard credentials the
        base compose requires via `:?` interpolation."""
        runtime = GbrainConformanceRuntime()
        for key in (
            "HERMES_DASHBOARD_SESSION_TOKEN",
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
        ):
            self.assertTrue(runtime.env.get(key), key)

    def test_project_prefix_is_test_scoped(self) -> None:
        runtime = GbrainConformanceRuntime()
        self.assertTrue(runtime.project.startswith("josemar-test-"))
        self.assertEqual(runtime.env["JOSEMAR_CONTAINER_PREFIX"], runtime.project)

    def test_baseline_gbrain_ref_matches_dockerfile(self) -> None:
        runtime = GbrainConformanceRuntime()
        self.assertEqual(runtime.baseline_gbrain_ref(), parse_dockerfile_gbrain_ref())
        self.assertRegex(runtime.baseline_gbrain_ref(), r"^[0-9a-f]{40}$")


class GbrainConformanceRuntimeCommandTests(unittest.TestCase):
    """Host-side command construction for the conformance runtime (no Docker)."""

    def test_run_as_hermes_uses_su_hermes(self) -> None:
        runtime = GbrainConformanceRuntime()
        with mock.patch.object(runtime, "exec") as exec_mock:
            exec_mock.return_value = subprocess.CompletedProcess(
                ["x"], 0, stdout="out", stderr="err"
            )
            evidence = runtime.run_as_hermes("gbrain", "status", "--json")
        args = exec_mock.call_args.args
        self.assertEqual(args[0], "hermes")
        self.assertEqual(args[1], "su")
        self.assertIn("--", args)
        self.assertEqual(args[args.index("--") + 1], "hermes")
        self.assertEqual(args[args.index("--") + 2], "-c")
        self.assertEqual(args[-1], "gbrain status --json")
        self.assertEqual(evidence.returncode, 0)
        self.assertEqual(evidence.stdout, "out")
        self.assertEqual(evidence.stderr, "err")

    def test_build_baseline_has_no_build_arg_override(self) -> None:
        runtime = GbrainConformanceRuntime()
        with mock.patch.dict(os.environ, {GBRAIN_CONFORMANCE_BASELINE_REF_ENV: ""}):
            with mock.patch.object(runtime, "build") as build_mock:
                runtime.build_baseline("hermes")
        build_mock.assert_called_once_with("hermes", timeout=900)

    def test_build_candidate_passes_validated_lowercased_build_arg(self) -> None:
        runtime = GbrainConformanceRuntime()
        ref = "A" * 40
        with mock.patch.object(runtime, "build") as build_mock:
            runtime.build_candidate(ref, "hermes")
        build_mock.assert_called_once_with(
            "hermes", build_args={"GBRAIN_REF": ref.lower()}, timeout=900
        )

    def test_build_candidate_invalid_ref_rejected_before_docker(self) -> None:
        runtime = GbrainConformanceRuntime()
        with mock.patch.object(runtime, "build") as build_mock:
            with self.assertRaises(ValueError):
                runtime.build_candidate("main", "hermes")
        build_mock.assert_not_called()

    def test_recreate_same_volumes_uses_recreate(self) -> None:
        runtime = GbrainConformanceRuntime()
        with mock.patch.object(runtime, "recreate") as recreate_mock:
            runtime.recreate_same_volumes("hermes")
        recreate_mock.assert_called_once_with("hermes", timeout=600)

    def test_cleanup_calls_down(self) -> None:
        runtime = GbrainConformanceRuntime()
        with mock.patch.object(runtime, "down") as down_mock:
            runtime.cleanup()
        down_mock.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
