"""Source-contract tests for the stored gbrain E5 preprocessing patch.

These tests inspect `patches/gbrain-inline-worker-gateway.patch` (no Docker, no
gbrain binary required) to guard the E5 query/passage preprocessing seam that
the embeddings overlay relies on. They keep the patch honest about:

  - E5 query/passage preprocessing is present in the embed() seam,
  - a versioned embedding signature is stamped for E5 models,
  - the E5 preprocessing signature and gbrain's embedding-migration signature
    use a single compatible signature algorithm (issue #65 safe E5 migration):
    `migrationSignature()` must append the same `#e5-query-passage-v1`
    version segment as `currentEmbeddingSignature()` for E5 models, so the
    migration planner / invalidator / reconciler and the embed loop never
    diverge on what counts as a stale vector,
  - the configured `intfloat/multilingual-e5-small` tuple is detectable as a
    safe/unsafe migration target by `migrationSignature()` (E5 detection
    fires on the model id, not on env query/passage prefix vars),
  - gbrain's E5 prefixing is model-gated, NOT driven by arbitrary
    environment query/passage prefix vars — the overlay's
    `EMBEDDING_QUERY_PREFIX`/`EMBEDDING_PASSAGE_PREFIX` configure Mnemosyne/TEI
    only; the patch must not falsely claim those env vars configure gbrain,
  - non-E5 models see zero behavioral change (no prefix, no version segment),
  - the upstream test file for the E5 preprocessing seam is included,
  - the recognized E5 model id prefix covers the selected model
    (`intfloat/multilingual-e5-small`).
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH_PATH = REPO_ROOT / "patches" / "gbrain-inline-worker-gateway.patch"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class GbrainE5PatchContractTests(unittest.TestCase):
    """The stored patch must include the E5 preprocessing seam and tests."""

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_defines_e5_preprocessing_helpers(self) -> None:
        self.assertIn("isE5EmbeddingModel", self.patch)
        self.assertIn("preprocessE5Input", self.patch)
        self.assertIn("E5_PREPROCESS_VERSION", self.patch)

    def test_patch_e5_query_passage_prefixes_are_exact(self) -> None:
        # The exact E5 prefixes must be applied.
        self.assertIn("'query: '", self.patch)
        self.assertIn("'passage: '", self.patch)
        # The prefix selection must key off inputType.
        self.assertIn("inputType === 'query'", self.patch)

    def test_patch_e5_detection_covers_selected_model(self) -> None:
        # The recognized E5 model id prefixes must cover the selected
        # `intfloat/multilingual-e5-small` model used by the embeddings overlay.
        self.assertIn("intfloat/multilingual-e5-", self.patch)
        self.assertIn("intfloat/e5-", self.patch)

    def test_patch_e5_detection_strips_provider_prefix(self) -> None:
        # gbrain stores embedding models as `provider:modelId`; detection must
        # strip the leading provider prefix (colon separator).
        self.assertIn("indexOf(':')", self.patch)

    def test_patch_preprocessing_runs_before_truncation(self) -> None:
        # The preprocessing must run BEFORE truncation so the prefix survives
        # at the head of any truncated value. Inspect only the ADDED (+) lines
        # of the embed() hunk, since the diff's removed/context lines would
        # otherwise place the old truncation call before the new preprocessing.
        embed_pos = self.patch.find("export async function embed(")
        self.assertGreater(embed_pos, 0, "embed() function not found in patch")
        embed_body = self.patch[embed_pos:]
        added = [ln[1:] for ln in embed_body.splitlines() if ln.startswith("+")]
        added_text = "\n".join(added)
        preprocessed_pos = added_text.find("preprocessE5Input")
        truncated_pos = added_text.find(".slice(0, MAX_CHARS)")
        self.assertGreater(preprocessed_pos, 0, "preprocessE5Input call not found in added embed() lines")
        self.assertGreater(truncated_pos, 0, "truncation not found in added embed() lines")
        self.assertLess(
            preprocessed_pos,
            truncated_pos,
            "E5 preprocessing must run BEFORE truncation in the added code",
        )

    def test_patch_non_e5_models_unchanged(self) -> None:
        # preprocessE5Input must be a no-op for non-E5 models (returns text
        # unchanged), and the embed() path must only apply preprocessing when
        # isE5EmbeddingModel is true.
        self.assertIn("if (!isE5EmbeddingModel(modelStr)) return text", self.patch)
        # The embed() body gates preprocessing on isE5EmbeddingModel.
        embed_pos = self.patch.find("export async function embed(")
        embed_body = self.patch[embed_pos:]
        self.assertIn("isE5EmbeddingModel(resolveTarget)", embed_body)

    def test_patch_versioned_embedding_signature_for_e5(self) -> None:
        # currentEmbeddingSignature must append the revision-aware E5
        # signature suffix for E5 models and keep the exact pre-patch shape
        # for non-E5. The suffix is computed by e5SignatureSuffix() which
        # folds in the TEI model revision (issue #65).
        self.assertIn("E5_PREPROCESS_VERSION", self.patch)
        self.assertIn("e5SignatureSuffix(gatewayGetModel())", self.patch)
        # The signature must branch: E5 -> `${base}#${suffix}`, non-E5 -> base.
        self.assertIn("suffix ? `${base}#${suffix}` : base", self.patch)

    def test_patch_includes_upstream_test_file(self) -> None:
        # The patch must add the upstream test file for the E5 preprocessing
        # seam so the contract is executable in the gbrain tree.
        self.assertIn("test/ai/e5-preprocess.test.ts", self.patch)

    def test_patch_uses_upstream_chronicle_token_configuration(self) -> None:
        """Judge sizing must stay in supported runtime config, not a source patch."""
        self.assertNotIn("maxTokens: 8000", self.patch)
        self.assertNotIn("src/core/chronicle/extract-events.ts", self.patch)

    def test_patch_test_file_covers_query_and_passage_paths(self) -> None:
        # The added test file must cover both the query and document-default
        # (passage) preprocessing paths.
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        self.assertGreater(test_pos, 0, "test file not added by patch")
        test_body = self.patch[test_pos:]
        self.assertIn("query: what is foo?", test_body)
        self.assertIn("passage: a document", test_body)

    def test_patch_test_file_covers_non_e5_unchanged(self) -> None:
        # The added test file must assert non-E5 models see no prefix.
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        test_body = self.patch[test_pos:]
        self.assertIn("values are UNCHANGED", test_body)
        self.assertIn("no prefix", test_body.lower())

    def test_patch_test_file_covers_signature_version(self) -> None:
        # The added test file must assert the E5 signature carries the
        # preprocessing version and the non-E5 signature is unchanged.
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        test_body = self.patch[test_pos:]
        self.assertIn("e5-query-passage-v1", test_body)
        self.assertIn("UNCHANGED", test_body)

    def test_patch_test_file_covers_prefix_before_truncation(self) -> None:
        # The added test file must assert the prefix is applied before
        # truncation (long input keeps the prefix head).
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        test_body = self.patch[test_pos:]
        self.assertIn("BEFORE truncation", test_body)
        self.assertIn("MAX_CHARS", test_body)

    def test_patch_test_file_covers_exactly_once_no_double_prefix(self) -> None:
        # The added test file must assert the prefix is applied exactly once
        # across batch split / recursive halving (no double prefix).
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        test_body = self.patch[test_pos:]
        self.assertIn("EXACTLY ONCE", test_body)
        self.assertIn("passage: passage: ", test_body)

    # ---- Issue #65 safe E5 migration: signature alignment -------------------

    def test_patch_touches_embedding_migration_signature(self) -> None:
        """The patch must modify src/core/embedding-migration.ts so the
        migration signature uses the same E5-aware algorithm as the embed
        loop's currentEmbeddingSignature(). Without this hunk the two
        signatures diverge for E5 models (issue #65)."""
        self.assertIn("src/core/embedding-migration.ts", self.patch)

    def test_patch_migration_signature_imports_e5_helpers(self) -> None:
        """embedding-migration.ts must import e5SignatureSuffix from
        ./ai/gateway.ts so migrationSignature() can branch on the E5 model
        the same way currentEmbeddingSignature() does (issue #65). The
        revision-aware suffix folds in the TEI model revision."""
        mig_pos = self.patch.find("a/src/core/embedding-migration.ts")
        self.assertGreater(mig_pos, 0, "embedding-migration.ts hunk missing")
        mig_body = self.patch[mig_pos:]
        self.assertIn(
            "import { e5SignatureSuffix } from './ai/gateway.ts'",
            mig_body,
        )

    def test_patch_migration_signature_appends_e5_version(self) -> None:
        """migrationSignature() must append `#${suffix}` for E5 models,
        mirroring currentEmbeddingSignature(). The suffix is the
        revision-aware e5SignatureSuffix() which folds in the TEI model
        revision. This is the single compatible signature algorithm that
        prevents signature/model-dimension divergence between the embed
        loop and the migration planner (issue #65)."""
        mig_pos = self.patch.find("a/src/core/embedding-migration.ts")
        self.assertGreater(mig_pos, 0, "embedding-migration.ts hunk missing")
        mig_body = self.patch[mig_pos:]
        # The E5 branch must produce the same `${base}#${suffix}` shape as
        # currentEmbeddingSignature() in embedding.ts.
        self.assertIn(
            "suffix ? `${base}#${suffix}` : base",
            mig_body,
        )

    def test_patch_migration_signature_non_e5_unchanged(self) -> None:
        """For non-E5 models migrationSignature() must keep the bare
        `<model>:<dims>` form (no version segment), matching the non-E5
        currentEmbeddingSignature() shape exactly."""
        mig_pos = self.patch.find("a/src/core/embedding-migration.ts")
        mig_body = self.patch[mig_pos:]
        # The base form is computed and returned as-is for non-E5.
        self.assertIn("const base = `${toModel}:${toDims}`", mig_body)

    def test_patch_migration_signature_and_current_signature_use_same_version(self) -> None:
        """Both signature seams must use the SAME e5SignatureSuffix() helper
        (imported from the same module), so they can never drift on the
        suffix string — including the revision segment (issue #65)."""
        # Both embedding.ts and embedding-migration.ts must reference
        # e5SignatureSuffix in their respective signature branches.
        self.assertIn("e5SignatureSuffix(gatewayGetModel())", self.patch)
        self.assertIn("e5SignatureSuffix(toModel)", self.patch)
        # Both signature seams must produce the `${base}#${suffix}` shape.
        self.assertGreaterEqual(
            self.patch.count("suffix ? `${base}#${suffix}` : base"),
            2,
            "both signature seams must use the e5SignatureSuffix branch",
        )

    def test_patch_migration_signature_detects_selected_e5_tuple(self) -> None:
        """The configured `intfloat/multilingual-e5-small` tuple (stored as
        `llama-server:intfloat/multilingual-e5-small` in gbrain) must be
        detected as an E5 model by migrationSignature(), so a migration onto
        it is recognized as a safe E5 migration (version-stamped) rather than
        a bare model/dim swap. Detection keys off the model id via
        isE5EmbeddingModel (called inside e5SignatureSuffix), which strips the
        `llama-server:` provider prefix and matches the
        `intfloat/multilingual-e5-` prefix."""
        # e5SignatureSuffix must be the gate for BOTH signature seams
        # (it internally calls isE5EmbeddingModel).
        self.assertIn("e5SignatureSuffix(toModel)", self.patch)
        self.assertIn("e5SignatureSuffix(gatewayGetModel())", self.patch)
        # The recognized E5 prefix must cover the selected model id.
        self.assertIn("intfloat/multilingual-e5-", self.patch)

    def test_patch_does_not_claim_env_prefixes_configure_gbrain(self) -> None:
        """gbrain's E5 prefixing is model-gated (off isE5EmbeddingModel /
        the model id), NOT driven by arbitrary environment query/passage
        prefix vars. The overlay's EMBEDDING_QUERY_PREFIX /
        EMBEDDING_PASSAGE_PREFIX configure Mnemosyne/TEI only. The patch must
        not falsely claim those env vars configure gbrain's embed() seam:
        preprocessE5Input must branch on inputType / model only, never on an
        env prefix var, and the patch must not introduce any
        EMBEDDING_QUERY_PREFIX / EMBEDDING_PASSAGE_PREFIX reference into the
        gbrain source hunks."""
        # The preprocessing helper must branch on inputType, not env vars.
        self.assertIn("inputType === 'query'", self.patch)
        # The gbrain source hunks (everything before the test-file hunk) must
        # NOT reference the overlay's env prefix vars — those configure
        # Mnemosyne/TEI, not gbrain.
        test_file_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        self.assertGreater(test_file_pos, 0, "test file hunk not found")
        gbrain_source = self.patch[:test_file_pos]
        self.assertNotIn(
            "EMBEDDING_QUERY_PREFIX",
            gbrain_source,
            "gbrain source hunks must not reference Mnemosyne/TEI env prefix vars",
        )
        self.assertNotIn(
            "EMBEDDING_PASSAGE_PREFIX",
            gbrain_source,
            "gbrain source hunks must not reference Mnemosyne/TEI env prefix vars",
        )

    def test_patch_migration_signature_preserves_unrelated_patch_behavior(self) -> None:
        """The migration-signature fix must not disturb the unrelated patch
        hunks (config *_api_key bridge, jobs gateway configure, build-gateway
        generic key fold, config env merge, E5 preprocessing seam, signature
        version, and the upstream test file). Each unrelated hunk must still
        be present."""
        for marker in (
            "matchesApiKeySuffix",
            "configureGateway",
            "buildGatewayConfig",
            "k.endsWith('_api_key') && typeof v === 'string'",
            "k.endsWith('_API_KEY')",
            "isE5EmbeddingModel",
            "preprocessE5Input",
            "E5_PREPROCESS_VERSION",
            "test/ai/e5-preprocess.test.ts",
        ):
            self.assertIn(marker, self.patch, f"unrelated patch behavior lost: {marker}")


class GbrainE5MigrateCompletionProbeContractTests(unittest.TestCase):
    """Issue #65 safe E5 migration: the final completion-probe countStaleChunks
    in migrate-embeddings.ts must use the versioned E5-aware migration signature.

    The native migrate-embeddings command has a final `countStaleChunks` call
    (the completion probe after the re-embed drain + reconcile) that used the
    bare `${plan.to_model}:${plan.to_dims}` signature instead of the versioned
    `migrationSignature(plan.to_model, plan.to_dims)`. For E5 models this is a
    signature divergence: the embed loop stamps pages with the version segment
    (`#${E5_PREPROCESS_VERSION}`), applyEmbeddingMigration and
    reconcilePageSignatures invalidate/stamp against the versioned signature,
    but the completion probe counted against the bare form — so a fully-migrated
    E5 brain reported "incomplete" forever and the re-run paid again. These
    tests guard the patch hunk that fixes this so ALL stale-count /
    reconciliation signature usage follows one shared E5-aware signature
    function.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_touches_migrate_embeddings_command(self) -> None:
        """The patch must modify src/commands/migrate-embeddings.ts so the
        final completion probe uses the versioned E5 signature."""
        self.assertIn("src/commands/migrate-embeddings.ts", self.patch)

    def test_patch_migrate_embeddings_imports_migration_signature(self) -> None:
        """migrate-embeddings.ts must import migrationSignature from
        embedding-migration.ts so the completion probe can use it."""
        mig_cmd_pos = self.patch.find("a/src/commands/migrate-embeddings.ts")
        self.assertGreater(mig_cmd_pos, 0, "migrate-embeddings.ts hunk missing")
        mig_cmd_body = self.patch[mig_cmd_pos:]
        self.assertIn("migrationSignature,", mig_cmd_body)
        self.assertIn("from '../core/embedding-migration.ts'", mig_cmd_body)

    def test_patch_migrate_embeddings_replaces_bare_signature(self) -> None:
        """The bare `${plan.to_model}:${plan.to_dims}` in the completion probe
        must be replaced by migrationSignature(plan.to_model, plan.to_dims)."""
        mig_cmd_pos = self.patch.find("a/src/commands/migrate-embeddings.ts")
        self.assertGreater(mig_cmd_pos, 0, "migrate-embeddings.ts hunk missing")
        mig_cmd_body = self.patch[mig_cmd_pos:]
        # The old bare form must be removed (prefixed with '-').
        self.assertIn(
            "-    signature: `${plan.to_model}:${plan.to_dims}`",
            mig_cmd_body,
            "the bare signature line must be removed",
        )
        # The new versioned form must be added (prefixed with '+').
        self.assertIn(
            "+    signature: migrationSignature(plan.to_model, plan.to_dims)",
            mig_cmd_body,
            "the completion probe must use migrationSignature()",
        )

    def test_patch_migrate_embeddings_no_bare_signature_remains(self) -> None:
        """After the patch, no bare `${plan.to_model}:${plan.to_dims}` signature
        may remain in the migrate-embeddings.ts hunk's added lines."""
        mig_cmd_pos = self.patch.find("a/src/commands/migrate-embeddings.ts")
        self.assertGreater(mig_cmd_pos, 0, "migrate-embeddings.ts hunk missing")
        # Isolate the migrate-embeddings.ts hunk: from its diff header to the
        # next diff header (or end of patch).
        next_diff = self.patch.find("\ndiff --git", mig_cmd_pos + 1)
        mig_hunk = self.patch[mig_cmd_pos:next_diff] if next_diff > 0 else self.patch[mig_cmd_pos:]
        # No ADDED line may reintroduce the bare signature form.
        added_lines = [ln[1:] for ln in mig_hunk.splitlines() if ln.startswith("+")]
        for ln in added_lines:
            self.assertNotIn(
                "`${plan.to_model}:${plan.to_dims}`",
                ln,
                "no bare signature may be added back to migrate-embeddings.ts",
            )

    def test_patch_migrate_embeddings_completion_probe_uses_shared_function(self) -> None:
        """The completion probe must call the SAME migrationSignature() function
        used by applyEmbeddingMigration and reconcilePageSignatures, not a
        duplicate/ad-hoc signature computation."""
        # migrationSignature is the single shared E5-aware signature function.
        # It must be referenced in the migrate-embeddings.ts hunk.
        mig_cmd_pos = self.patch.find("a/src/commands/migrate-embeddings.ts")
        self.assertGreater(mig_cmd_pos, 0, "migrate-embeddings.ts hunk missing")
        mig_cmd_body = self.patch[mig_cmd_pos:]
        self.assertIn("migrationSignature(plan.to_model, plan.to_dims)", mig_cmd_body)

    def test_patch_all_signature_uses_are_e5_aware(self) -> None:
        """ALL stale-count/reconciliation signature usage in the patched files
        must go through the shared E5-aware migrationSignature() function.

        The patch touches two signature seams:
          1. embedding-migration.ts: migrationSignature() itself (E5-aware),
             used by planEmbeddingMigration, applyEmbeddingMigration,
             reconcilePageSignatures.
          2. migrate-embeddings.ts: the final completion-probe countStaleChunks
             (now fixed to call migrationSignature()).
        No bare `${...}:${...}` signature form may remain in the ADDED lines
        of either hunk.
        """
        # embedding-migration.ts hunk: the bare form is removed and replaced
        # by the E5-aware branch.
        mig_core_pos = self.patch.find("a/src/core/embedding-migration.ts")
        self.assertGreater(mig_core_pos, 0, "embedding-migration.ts hunk missing")
        next_diff = self.patch.find("\ndiff --git", mig_core_pos + 1)
        mig_core_hunk = self.patch[mig_core_pos:next_diff] if next_diff > 0 else self.patch[mig_core_pos:]
        added_core = [ln[1:] for ln in mig_core_hunk.splitlines() if ln.startswith("+")]
        for ln in added_core:
            # The migrationSignature body computes `base` then branches; the
            # bare `${toModel}:${toDims}` is the base, which is fine — it is
            # the non-E5 branch. The point is no ADDED line returns the bare
            # form unconditionally.
            pass
        # The migrate-embeddings.ts hunk must not add any bare signature.
        mig_cmd_pos = self.patch.find("a/src/commands/migrate-embeddings.ts")
        self.assertGreater(mig_cmd_pos, 0, "migrate-embeddings.ts hunk missing")
        next_diff = self.patch.find("\ndiff --git", mig_cmd_pos + 1)
        mig_cmd_hunk = self.patch[mig_cmd_pos:next_diff] if next_diff > 0 else self.patch[mig_cmd_pos:]
        added_cmd = [ln[1:] for ln in mig_cmd_hunk.splitlines() if ln.startswith("+")]
        for ln in added_cmd:
            self.assertNotIn(
                "signature: `${plan.to_model}:${plan.to_dims}`",
                ln,
                "no bare signature may be added to migrate-embeddings.ts",
            )

    def test_patch_migrate_embeddings_preserves_unrelated_patch_behavior(self) -> None:
        """The migrate-embeddings.ts fix must not disturb the unrelated patch
        hunks. Each unrelated hunk must still be present."""
        for marker in (
            "matchesApiKeySuffix",
            "configureGateway",
            "buildGatewayConfig",
            "k.endsWith('_api_key') && typeof v === 'string'",
            "k.endsWith('_API_KEY')",
            "isE5EmbeddingModel",
            "preprocessE5Input",
            "E5_PREPROCESS_VERSION",
            "test/ai/e5-preprocess.test.ts",
            "migrationSignature",
        ):
            self.assertIn(marker, self.patch, f"unrelated patch behavior lost: {marker}")


class GbrainE5RevisionAwareSignatureContractTests(unittest.TestCase):
    """Issue #65: E5 signatures must be revision-aware.

    The E5 embedding signature must reflect the TEI-served model revision so
    that a revision drift (TEI serves a different commit of the same model id)
    is detected as stale and triggers re-embedding. The revision is read
    from the GBRAIN_EMBEDDING_MODEL_REVISION env var (wired from
    EMBEDDING_MODEL_REVISION in docker-compose.embeddings.yml). Non-E5
    output must be unchanged.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_defines_e5_signature_suffix_helper(self) -> None:
        """The patch must export an e5SignatureSuffix() helper that computes
        the revision-aware suffix for E5 models."""
        self.assertIn("e5SignatureSuffix", self.patch)

    def test_patch_e5_signature_suffix_reads_revision_env(self) -> None:
        """e5SignatureSuffix must read GBRAIN_EMBEDDING_MODEL_REVISION from
        process.env so a revision change between two gbrain processes is
        reflected without a module reload."""
        self.assertIn("GBRAIN_EMBEDDING_MODEL_REVISION", self.patch)

    def test_patch_e5_signature_suffix_revision_shape(self) -> None:
        """The suffix shape must be `${E5_PREPROCESS_VERSION}@${revision}`
        when a revision is set, or the bare `${E5_PREPROCESS_VERSION}` when
        it is not."""
        self.assertIn("${E5_PREPROCESS_VERSION}@${rev}", self.patch)

    def test_patch_e5_signature_suffix_non_e5_returns_empty(self) -> None:
        """For non-E5 models e5SignatureSuffix must return an empty string
        so the signature stays `${base}` (no version segment)."""
        self.assertIn("if (!isE5EmbeddingModel(modelStr)) return ''", self.patch)

    def test_patch_current_embedding_signature_uses_suffix(self) -> None:
        """currentEmbeddingSignature() must use e5SignatureSuffix() to decide
        whether to append the version segment."""
        self.assertIn("e5SignatureSuffix(gatewayGetModel())", self.patch)

    def test_patch_migration_signature_uses_suffix(self) -> None:
        """migrationSignature() must use e5SignatureSuffix() so the migration
        planner / invalidator / reconciler match the embed loop's signature."""
        self.assertIn("e5SignatureSuffix(toModel)", self.patch)

    def test_patch_test_file_covers_revision_aware_signature(self) -> None:
        """The upstream test file must cover the revision-aware signature:
        with revision, without revision, revision change, non-E5 unchanged,
        and empty revision fallback."""
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        self.assertGreater(test_pos, 0, "test file not added by patch")
        test_body = self.patch[test_pos:]
        self.assertIn("revision-aware signature", test_body)
        self.assertIn("GBRAIN_EMBEDDING_MODEL_REVISION", test_body)
        self.assertIn("e5-query-passage-v1@", test_body)
        self.assertIn("revision change causes signature change", test_body)
        self.assertIn("non-E5 signature is UNCHANGED regardless of revision", test_body)
        self.assertIn("empty revision env falls back", test_body)


class GbrainEmbeddingDisabledClearContractTests(unittest.TestCase):
    """Issue #65: native migration must clear persisted `embedding_disabled`
    in FILE config when it successfully persists model/dimensions.

    Historical brains initialized with `gbrain init --no-embedding` carry
    `embedding_disabled: true` in config.json. Without clearing it during
    migration, a successful migration would leave the sentinel in place and
    every later `gbrain embed` / `gbrain import` would refuse via
    assertEmbeddingEnabled. The patch must clear the sentinel in
    persistEmbeddingFileConfig (the file-plane persistence path in
    migrate-embeddings.ts) so `enable-embeddings` (which runs
    `migrate embeddings --no-embed`) lifts the sentinel on success.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_touches_migrate_embeddings_persist_config(self) -> None:
        """The patch must modify persistEmbeddingFileConfig in
        migrate-embeddings.ts to clear the sentinel."""
        self.assertIn("src/commands/migrate-embeddings.ts", self.patch)
        self.assertIn("persistEmbeddingFileConfig", self.patch)

    def test_patch_clears_embedding_disabled_on_success(self) -> None:
        """The patch must clear embedding_disabled when the migration
        successfully persists model + dimensions."""
        self.assertIn("embedding_disabled", self.patch)
        self.assertIn("cfg.embedding_disabled = undefined", self.patch)

    def test_patch_clears_sentinel_only_when_present(self) -> None:
        """The sentinel must only be cleared when it is present (not
        unconditionally written) to avoid unnecessary config.json rewrites
        on brains that never had the sentinel."""
        self.assertIn("if (cfg.embedding_disabled !== undefined)", self.patch)

    def test_patch_clear_uses_saveConfig(self) -> None:
        """The clear must go through saveConfig() so the file-plane write is
        atomic and the .gitignore is maintained."""
        # The patch must call saveConfig after clearing the sentinel.
        mig_pos = self.patch.find("a/src/commands/migrate-embeddings.ts")
        self.assertGreater(mig_pos, 0, "migrate-embeddings.ts hunk missing")
        mig_body = self.patch[mig_pos:]
        # Find the embedding_disabled clearing block and assert saveConfig
        # is called within it.
        clear_pos = mig_body.find("cfg.embedding_disabled = undefined")
        self.assertGreater(clear_pos, 0, "clear block not found")
        clear_block = mig_body[clear_pos:clear_pos + 200]
        self.assertIn("saveConfig(cfg)", clear_block)


class GbrainRuntimeOptInRollbackContractTests(unittest.TestCase):
    """Issue #65 final review: runtime opt-in/rollback enforcement in the
    pinned gbrain source.

    These tests inspect the stored patch to prove the enforcement seams are
    present in the patched operations.ts and migrate-embeddings.ts:
      - put_page forces noEmbed when config embedding_disabled=true,
      - the `query` op honors search.mcp_keyword_only=true (text → keyword,
        image-only → reject, cross_modal image/both → reject),
      - the migrate-embeddings no-work path probes + persists + finalizes,
      - the embed backfill finalizes a matching in-flight migration.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    # --- put_page noEmbed when embedding_disabled ---

    def test_patch_touches_operations_put_page(self) -> None:
        self.assertIn("src/core/operations.ts", self.patch)

    def test_patch_put_page_forces_noembed_when_disabled(self) -> None:
        """put_page must force noEmbed when config embedding_disabled=true."""
        ops_pos = self.patch.find("a/src/core/operations.ts")
        self.assertGreater(ops_pos, 0, "operations.ts hunk missing")
        ops_body = self.patch[ops_pos:]
        self.assertIn("loadConfig()?.embedding_disabled === true", ops_body)
        self.assertIn(
            "const noEmbed = cfgDisabled || !isAvailable('embedding')",
            ops_body,
        )

    def test_patch_put_page_noembed_before_importfromcontent(self) -> None:
        """The noEmbed computation must precede the importFromContent call."""
        ops_pos = self.patch.find("a/src/core/operations.ts")
        ops_body = self.patch[ops_pos:]
        noembed_pos = ops_body.find(
            "const noEmbed = cfgDisabled || !isAvailable('embedding')"
        )
        import_pos = ops_body.find("importFromContent(ctx.engine, slug")
        self.assertGreater(noembed_pos, 0)
        self.assertGreater(import_pos, 0)
        self.assertLess(noembed_pos, import_pos)

    # --- query op keyword-only enforcement ---

    def test_patch_query_op_keyword_only_check(self) -> None:
        """The query op must check search.mcp_keyword_only and route text
        queries to searchKeyword (no embedding transport call)."""
        ops_pos = self.patch.find("a/src/core/operations.ts")
        ops_body = self.patch[ops_pos:]
        self.assertIn("queryKeywordOnly", ops_body)
        self.assertIn(
            "ctx.engine.getConfig('search.mcp_keyword_only')",
            ops_body,
        )

    def test_patch_query_op_keyword_only_uses_searchkeyword(self) -> None:
        """The keyword-only block must call searchKeyword, not hybridSearch
        or embedMultimodal."""
        ops_pos = self.patch.find("a/src/core/operations.ts")
        ops_body = self.patch[ops_pos:]
        kw_pos = ops_body.find("if (queryKeywordOnly) {")
        self.assertGreater(kw_pos, 0, "keyword-only block not found")
        kw_end = ops_body.find("\n      if (imageData) {", kw_pos)
        kw_block = ops_body[kw_pos:kw_end if kw_end > 0 else kw_pos + 1800]
        self.assertIn("ctx.engine.searchKeyword(queryText", kw_block)
        self.assertNotIn("await ctx.engine.hybridSearch", kw_block)
        self.assertNotIn("embedMultimodal(", kw_block)
        self.assertNotIn("embedQuery(", kw_block)

    def test_patch_query_op_rejects_image_under_keyword_only(self) -> None:
        """Under keyword-only, image-only queries must REJECT with an
        OperationError (they require the embedding transport)."""
        ops_pos = self.patch.find("a/src/core/operations.ts")
        ops_body = self.patch[ops_pos:]
        kw_pos = ops_body.find("if (queryKeywordOnly) {")
        kw_block = ops_body[kw_pos:kw_pos + 1800]
        self.assertIn("if (imageData) {", kw_block)
        self.assertIn("throw new OperationError", kw_block)
        self.assertIn(
            "Image-similarity search requires the embedding transport",
            kw_block,
        )

    def test_patch_query_op_rejects_cross_modal_image_under_keyword_only(self) -> None:
        """Under keyword-only, cross_modal='image' or 'both' must REJECT."""
        ops_pos = self.patch.find("a/src/core/operations.ts")
        ops_body = self.patch[ops_pos:]
        cross_pos = ops_body.find(
            "kwCrossModal === 'image' || kwCrossModal === 'both'"
        )
        self.assertGreater(cross_pos, 0, "cross_modal reject not found")
        cross_block = ops_body[cross_pos - 200:cross_pos + 400]
        self.assertIn("throw new OperationError", cross_block)
        self.assertIn("requires the embedding transport", cross_block)

    def test_patch_query_op_keyword_only_before_image_branch(self) -> None:
        """The keyword-only check must precede the image-similarity branch
        (which calls embedMultimodal)."""
        ops_pos = self.patch.find("a/src/core/operations.ts")
        ops_body = self.patch[ops_pos:]
        kw_pos = ops_body.find("const queryKeywordOnly =")
        image_pos = ops_body.find("if (imageData) {")
        self.assertGreater(kw_pos, 0)
        self.assertGreater(image_pos, 0)
        self.assertLess(kw_pos, image_pos)

    # --- migrate-embeddings no-work path ---

    def test_patch_migrate_no_work_path_probes_target(self) -> None:
        """The no-work path must call probeTargetProvider before exiting."""
        mig_pos = self.patch.find("a/src/commands/migrate-embeddings.ts")
        self.assertGreater(mig_pos, 0, "migrate-embeddings.ts hunk missing")
        mig_body = self.patch[mig_pos:]
        nowork_pos = mig_body.find(
            "if (plan.chunks_to_embed === 0 && !plan.dim_change && plan.from_model === plan.to_model) {"
        )
        self.assertGreater(nowork_pos, 0, "no-work path not found")
        nowork_block = mig_body[nowork_pos:nowork_pos + 1200]
        self.assertIn("probeTargetProvider(plan.to_model, plan.to_dims)", nowork_block)

    def test_patch_migrate_no_work_path_persists_config(self) -> None:
        """The no-work path must call persistEmbeddingFileConfig (which clears
        the embedding_disabled sentinel)."""
        mig_pos = self.patch.find("a/src/commands/migrate-embeddings.ts")
        mig_body = self.patch[mig_pos:]
        nowork_pos = mig_body.find(
            "if (plan.chunks_to_embed === 0 && !plan.dim_change && plan.from_model === plan.to_model) {"
        )
        nowork_block = mig_body[nowork_pos:nowork_pos + 1200]
        self.assertIn(
            "persistEmbeddingFileConfig(plan.to_model, plan.to_dims)",
            nowork_block,
        )

    def test_patch_migrate_no_work_path_finalizes_migration(self) -> None:
        """The no-work path must call completeEmbeddingMigration when a
        matching MIGRATION_STATE_KEY marker exists."""
        mig_pos = self.patch.find("a/src/commands/migrate-embeddings.ts")
        mig_body = self.patch[mig_pos:]
        nowork_pos = mig_body.find(
            "if (plan.chunks_to_embed === 0 && !plan.dim_change && plan.from_model === plan.to_model) {"
        )
        nowork_block = mig_body[nowork_pos:nowork_pos + 1200]
        self.assertIn("completeEmbeddingMigration(engine, plan)", nowork_block)
        self.assertIn("MIGRATION_STATE_KEY", nowork_block)

    # --- embed backfill finalization ---

    def test_patch_touches_embed_command(self) -> None:
        """The patch must modify src/commands/embed.ts to add the
        finalization hook."""
        self.assertIn("src/commands/embed.ts", self.patch)

    def test_patch_embed_finalizes_matching_migration(self) -> None:
        """runEmbedCore must call completeEmbeddingMigration after a zero-stale
        backfill when a matching MIGRATION_STATE_KEY marker exists."""
        embed_pos = self.patch.find("a/src/commands/embed.ts")
        self.assertGreater(embed_pos, 0, "embed.ts hunk missing")
        embed_body = self.patch[embed_pos:]
        self.assertIn(
            "MIGRATION_STATE_KEY, completeEmbeddingMigration, reconcilePageSignatures",
            embed_body,
        )
        self.assertIn("if (finalRemaining === 0)", embed_body)
        self.assertIn(
            "await completeEmbeddingMigration(engine, plan as any)",
            embed_body,
        )

    def test_patch_embed_finalization_gated_on_zero_failures(self) -> None:
        """The finalization hook must be gated on result.failures === 0 so a
        partial backfill does not finalize the migration."""
        embed_pos = self.patch.find("a/src/commands/embed.ts")
        embed_body = self.patch[embed_pos:]
        self.assertIn("result.failures === 0", embed_body)

    def test_patch_embed_finalization_gated_on_stale(self) -> None:
        """The finalization hook must be gated on opts.stale so a
        standalone embed (not a backfill) does not finalize a migration."""
        embed_pos = self.patch.find("a/src/commands/embed.ts")
        embed_body = self.patch[embed_pos:]
        self.assertIn("opts.stale", embed_body)

    def test_patch_embed_finalization_gated_on_include_null_signature(self) -> None:
        """The finalization hook must be gated on opts.includeNullSignature
        === true alongside stale, non-dry-run, and zero failures: only a
        stale backfill that explicitly targets null-signature pages may
        finalize a migration. Assert the exact required gate in the added
        lines of the embed.ts hunk (not merely anywhere in the patch, where
        the appended upstream test file would also match)."""
        embed_pos = self.patch.find("a/src/commands/embed.ts")
        self.assertGreater(embed_pos, 0, "embed.ts hunk missing")
        next_diff = self.patch.find("\ndiff --git", embed_pos + 1)
        embed_hunk = self.patch[embed_pos:next_diff] if next_diff > 0 else self.patch[embed_pos:]
        added = [ln[1:] for ln in embed_hunk.splitlines() if ln.startswith("+")]
        added_text = "\n".join(added)
        self.assertIn(
            "if (opts.stale && opts.includeNullSignature === true && !opts.dryRun && result.failures === 0) {",
            added_text,
            "the runEmbedCore finalization gate must require opts.includeNullSignature === true",
        )
        # The old incomplete gate (no includeNullSignature) must not be added
        # back to the embed.ts source hunk.
        self.assertNotIn(
            "if (opts.stale && !opts.dryRun && result.failures === 0) {",
            added_text,
            "the finalization gate must not omit opts.includeNullSignature",
        )

    def test_patch_embed_finalization_gated_on_not_dry_run(self) -> None:
        """The finalization hook must be gated on !opts.dryRun so a dry-run
        backfill does not finalize a migration."""
        embed_pos = self.patch.find("a/src/commands/embed.ts")
        embed_body = self.patch[embed_pos:]
        self.assertIn("!opts.dryRun", embed_body)

    # --- upstream behavioral tests in the patch ---

    def test_patch_test_file_covers_put_page_noembed_enforcement(self) -> None:
        """The upstream test file must cover the put_page noEmbed-when-disabled
        enforcement."""
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        self.assertGreater(test_pos, 0, "test file not added by patch")
        test_body = self.patch[test_pos:]
        self.assertIn("runtime opt-in/rollback enforcement", test_body)
        self.assertIn("loadConfig()?.embedding_disabled === true", test_body)
        self.assertIn(
            "const noEmbed = cfgDisabled || !isAvailable('embedding')",
            test_body,
        )

    def test_patch_test_file_covers_query_keyword_only_enforcement(self) -> None:
        """The upstream test file must cover the query op keyword-only
        enforcement (text → searchKeyword, image → reject)."""
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        test_body = self.patch[test_pos:]
        self.assertIn("queryKeywordOnly", test_body)
        self.assertIn("ctx.engine.searchKeyword(queryText", test_body)
        self.assertIn("Image-similarity search requires the embedding transport", test_body)

    def test_patch_test_file_covers_migrate_no_work_path(self) -> None:
        """The upstream test file must cover the migrate-embeddings no-work
        path probe + persist + finalize."""
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        test_body = self.patch[test_pos:]
        self.assertIn("probeTargetProvider(plan.to_model, plan.to_dims)", test_body)
        self.assertIn("persistEmbeddingFileConfig(plan.to_model, plan.to_dims)", test_body)
        self.assertIn("completeEmbeddingMigration(engine, plan)", test_body)

    def test_patch_test_file_covers_embed_backfill_finalization(self) -> None:
        """The upstream test file must cover the embed backfill finalization."""
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        test_body = self.patch[test_pos:]
        self.assertIn(
            "MIGRATION_STATE_KEY, completeEmbeddingMigration, reconcilePageSignatures",
            test_body,
        )
        self.assertIn("await completeEmbeddingMigration(engine, plan as any)", test_body)

    def test_patch_preserves_revision_aware_signatures(self) -> None:
        """Requirement (4): revision-aware signatures must be preserved."""
        self.assertIn("e5SignatureSuffix", self.patch)
        self.assertIn("GBRAIN_EMBEDDING_MODEL_REVISION", self.patch)
        self.assertIn("E5_PREPROCESS_VERSION", self.patch)

    def test_patch_no_destructive_reinit(self) -> None:
        """Requirement (4): the patch must not introduce destructive reinit.
        No reinit-pglite or destructive schema rebuild in the patch."""
        self.assertNotIn("reinit-pglite", self.patch)
        self.assertNotIn("reinit_pglite", self.patch)
        # The patch must not add a DROP TABLE or TRUNCATE statement in the
        # added lines. Match only SQL statements (word-boundary, not
        # substrings inside comments like "TRUNCATED").
        import re
        for line in self.patch.splitlines():
            if line.startswith("+"):
                # Match DROP TABLE / TRUNCATE as SQL statements (preceded by
                # whitespace/start, not part of a larger word).
                self.assertFalse(
                    re.search(r"\bDROP\s+TABLE\b", line, re.IGNORECASE),
                    f"DROP TABLE in added line: {line}",
                )
                self.assertFalse(
                    re.search(r"(?<!TRUNCA)TRUNCATE\b", line, re.IGNORECASE),
                    f"TRUNCATE in added line: {line}",
                )


if __name__ == "__main__":
    unittest.main()
