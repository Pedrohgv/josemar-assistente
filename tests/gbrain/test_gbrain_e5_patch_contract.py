"""Source-contract tests for the stored gbrain E5 preprocessing patch.

These tests inspect `patches/gbrain-inline-worker-gateway.patch` (no Docker, no
gbrain binary required) and pin the v0.46.25.0-rebased contents. The patch is
rebased onto the upstream tranche-1 refactor, so:

  - put_page lives in `src/core/ops/pages.ts` (NOT the pre-refactor
    `src/core/operations.ts`),
  - the `query` op lives in `src/core/ops/search.ts`,
  - the migrate-embeddings completion-probe fix (bare -> shared
    `migrationSignature()`) is UPSTREAM in v0.46.25.0, so the patch no longer
    carries that hunk; the fixture pins it against real source instead,
  - the old no-work migration hunk (`plan.chunks_to_embed === 0 && ...` with
    probe/persist/finalize) is GONE, subsumed by the candidate's
    `ctx.verify.complete` / `skipped_no_work` verified skip.

Contracts guarded:

  - generic provider API-key env/file-plane bridge (any `*_api_key` /
    `*_API_KEY` key, not just upstream's fixed provider list),
  - chronicle_extract queue registration via `registerBuiltinJob`,
  - no-embed (put_page) and keyword-only (query op) enforcement,
  - E5 prefix / model detection / revision-aware signatures,
  - E5 preprocessing before truncation,
  - file-plane `embedding_disabled` clear in migrate-embeddings.ts: apply
    path (persistEmbeddingFileConfig) + the D2 verified-skip carve-out that
    lifts the sentinel before its `skipped_no_work` exit on converged brains,
  - no reintroduction of the old no-work migration hunk,
  - candidate-compatible post-backfill migration finalization (embed.ts hook),
  - the adapted upstream behavioral fixture (test/ai/e5-preprocess.test.ts)
    reading the refactored source paths.

The upstream fixture is the authoritative behavioral spec: it reads the REAL
patched sources at runtime (fs.readFileSync) and asserts the moved semantics;
these tests guard that the patch still ships that fixture with the right pins.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH_PATH = REPO_ROOT / "patches" / "gbrain-inline-worker-gateway.patch"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _hunk(patch: str, file_header: str) -> str:
    """Isolate a patched file's diff region: from its `a/...` diff header to
    the next `diff --git` header (or the end of the patch)."""
    pos = patch.find(file_header)
    if pos < 0:
        raise AssertionError(f"{file_header} hunk missing from patch")
    next_diff = patch.find("\ndiff --git", pos + 1)
    return patch[pos:next_diff] if next_diff > 0 else patch[pos:]


def _added_lines(patch: str, file_header: str) -> str:
    """Return the ADDED (+) lines of a file's diff region, stripped of the
    leading '+', joined as text. The `+++ b/...` header line is excluded."""
    region = _hunk(patch, file_header)
    return "\n".join(
        ln[1:] for ln in region.splitlines() if ln.startswith("+") and not ln.startswith("+++")
    )


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
        # at the head of any truncated value. The embed() body hunk is the
        # second gateway.ts hunk; anchor on its first added line (the function
        # signature itself is not part of the hunk) and inspect only the ADDED
        # lines, since the diff's removed/context lines would otherwise place
        # the old truncation call before the new preprocessing.
        embed_region = self.patch[self.patch.find("+  const e5 = isE5EmbeddingModel(resolveTarget);"):]
        self.assertGreater(
            self.patch.find("+  const e5 = isE5EmbeddingModel(resolveTarget);"),
            0,
            "embed() hunk not found in patch",
        )
        next_diff = embed_region.find("\ndiff --git")
        embed_region = embed_region[:next_diff] if next_diff > 0 else embed_region
        added = [ln[1:] for ln in embed_region.splitlines() if ln.startswith("+")]
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
        self.assertIn("isE5EmbeddingModel(resolveTarget)", self.patch)

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
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
        self.assertIn("query: what is foo?", test_body)
        self.assertIn("passage: a document", test_body)

    def test_patch_test_file_covers_non_e5_unchanged(self) -> None:
        # The added test file must assert non-E5 models see no prefix.
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
        self.assertIn("values are UNCHANGED", test_body)
        self.assertIn("no prefix", test_body.lower())

    def test_patch_test_file_covers_signature_version(self) -> None:
        # The added test file must assert the E5 signature carries the
        # preprocessing version and the non-E5 signature is unchanged.
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
        self.assertIn("e5-query-passage-v1", test_body)
        self.assertIn("UNCHANGED", test_body)

    def test_patch_test_file_covers_prefix_before_truncation(self) -> None:
        # The added test file must assert the prefix is applied before
        # truncation (long input keeps the prefix head).
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
        self.assertIn("BEFORE truncation", test_body)
        self.assertIn("MAX_CHARS", test_body)

    def test_patch_test_file_covers_exactly_once_no_double_prefix(self) -> None:
        # The added test file must assert the prefix is applied exactly once
        # across batch split / recursive halving (no double prefix).
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
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
        mig_body = _hunk(self.patch, "a/src/core/embedding-migration.ts")
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
        mig_body = _hunk(self.patch, "a/src/core/embedding-migration.ts")
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
        mig_body = _hunk(self.patch, "a/src/core/embedding-migration.ts")
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

    def test_patch_preserves_unrelated_patch_behavior(self) -> None:
        """The signature alignment must not disturb the unrelated patch hunks
        (config *_api_key bridge, chronicle registration, provider-env fold,
        config env merge, no-embed / keyword-only enforcement, E5 seam,
        embedding_disabled clear, embed finalization, and the upstream test
        file). Each unrelated hunk must still be present."""
        for marker in (
            "matchesApiKeySuffix",  # config.ts key validation
            "FILE_PLANE_API_KEYS.includes(key) || key.endsWith('_api_key')",  # config.ts file-plane routing
            "registerBuiltinJob(worker, engine, 'chronicle_extract'",  # jobs.ts queue registration
            "k.endsWith('_api_key') && typeof v === 'string'",  # provider-env.ts fold
            "k.endsWith('_API_KEY')",  # config.ts env merge
            "loadConfig()?.embedding_disabled === true",  # ops/pages.ts no-embed
            "queryKeywordOnly",  # ops/search.ts keyword-only
            "isE5EmbeddingModel",
            "preprocessE5Input",
            "E5_PREPROCESS_VERSION",
            "e5SignatureSuffix",
            "fileCfg.embedding_disabled = undefined",  # migrate-embeddings.ts clear
            "fileCfg?.embedding_disabled === true",  # migrate-embeddings.ts D2 carve-out
            "completeEmbeddingMigration",  # embed.ts finalization
            "test/ai/e5-preprocess.test.ts",
        ):
            self.assertIn(marker, self.patch, f"unrelated patch behavior lost: {marker}")


class GbrainGenericProviderKeyBridgeContractTests(unittest.TestCase):
    """Generic provider API-key bridge (upstream bug #3394).

    The patch routes ANY config `*_api_key` / env `*_API_KEY` through the same
    planes upstream reserves for its fixed provider list, so deepseek/groq/
    together/mistral keys reach the gateway without process.env propagation:

      - config.ts set/unset: `*_api_key` keys go to the FILE plane (the
        gateway folds config into env via mergedProviderEnv, so a DB-plane
        write would be a silent no-op),
      - config.ts validation: `*_api_key` keys are accepted without --force,
      - provider-env.ts: `*_api_key` config entries fold into `*_API_KEY` env
        ONLY when upstream has not already mapped them,
      - config.ts loadConfig: `*_API_KEY` env vars fold into `*_api_key`
        config fields, excluding the four upstream-explicit keys.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_config_set_and_unset_route_api_keys_to_file_plane(self) -> None:
        """Both the set and unset config paths must route any *_api_key suffix
        to the file plane (loadConfigFileOnly/saveConfig), not the DB plane."""
        self.assertEqual(
            self.patch.count("FILE_PLANE_API_KEYS.includes(key) || key.endsWith('_api_key')"),
            2,
            "both the set and unset config paths must route *_api_key to the file plane",
        )
        # The routing must stay file-plane canonical: loadConfigFileOnly +
        # saveConfig immediately follow the extended condition.
        cfg_pos = self.patch.find("FILE_PLANE_API_KEYS.includes(key) || key.endsWith('_api_key')")
        self.assertGreater(cfg_pos, 0)
        cfg_region = self.patch[cfg_pos:cfg_pos + 400]
        self.assertIn("loadConfigFileOnly", cfg_region)
        self.assertIn("saveConfig", cfg_region)

    def test_patch_config_validation_accepts_api_key_suffix_without_force(self) -> None:
        """Both the known-key validation and the --force warn path must accept
        any *_api_key suffix instead of rejecting it as unknown."""
        self.assertEqual(
            self.patch.count("const matchesApiKeySuffix = key.endsWith('_api_key');"),
            2,
            "both config validation paths must compute the *_api_key suffix match",
        )
        self.assertEqual(
            self.patch.count("!isKnown && !matchesPrefix && !matchesApiKeySuffix"),
            2,
            "both validation paths must gate on the *_api_key suffix",
        )

    def test_patch_provider_env_folds_generic_api_keys(self) -> None:
        """mergedProviderEnv must fold any config *_api_key entry into its
        uppercase *_API_KEY env var, only when upstream has not already mapped
        it (the explicit openai/anthropic/... mappings win)."""
        self.assertIn(
            "for (const [k, v] of Object.entries(cfg ?? {}))",
            self.patch,
        )
        self.assertIn(
            "k.endsWith('_api_key') && typeof v === 'string' && v && !(k.toUpperCase() in fromConfig)",
            self.patch,
        )
        self.assertIn("fromConfig[k.toUpperCase()] = v;", self.patch)

    def test_patch_config_env_merge_folds_generic_api_keys(self) -> None:
        """loadConfig must fold any *_API_KEY env var into its lowercase
        *_api_key config field, excluding the four upstream-explicit keys (so
        the explicit mappings are preserved, not duplicated)."""
        self.assertIn(
            "k.endsWith('_API_KEY') && !['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ZEROENTROPY_API_KEY', 'OPENROUTER_API_KEY'].includes(k)",
            self.patch,
        )
        self.assertIn("Object.entries(process.env)", self.patch)
        self.assertIn("[k.toLowerCase(), v]", self.patch)


class GbrainChronicleQueueRegistrationContractTests(unittest.TestCase):
    """chronicle_extract queue registration (v0.46.25.0 rebase).

    chronicle_extract is LLM-backed; without gateway refresh it silently
    returned no_events when the worker's gateway singleton was unconfigured.
    The patch adds it to the gateway-refresh job-name coverage and registers
    it via registerBuiltinJob so refreshGatewayForJob runs first.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_adds_chronicle_extract_to_refresh_job_names(self) -> None:
        """The gateway-refresh job-name list must include chronicle_extract."""
        jobs_hunk = _hunk(self.patch, "a/src/commands/jobs.ts")
        self.assertIn("'chronicle_extract',", jobs_hunk)
        # The rationale must be the gateway-refresh coverage, not a duplicate
        # gateway configuration path.
        self.assertIn("refreshGatewayForJob", jobs_hunk)

    def test_patch_registers_chronicle_extract_via_register_builtin_job(self) -> None:
        """The handler must register via registerBuiltinJob (so the
        GATEWAY_REFRESH_JOB_NAMES coverage applies) instead of the bare
        worker.register form."""
        jobs_hunk = _hunk(self.patch, "a/src/commands/jobs.ts")
        added = _added_lines(self.patch, "a/src/commands/jobs.ts")
        self.assertIn(
            "registerBuiltinJob(worker, engine, 'chronicle_extract', async (job) => {",
            added,
        )
        # The old direct registration must be removed.
        self.assertIn(
            "-  worker.register('chronicle_extract', async (job) => {",
            jobs_hunk,
        )
        # The handler's data.slug requirement must be preserved.
        self.assertIn(
            "chronicle_extract job requires data.slug",
            jobs_hunk,
        )


class GbrainNoEmbedKeywordOnlyEnforcementContractTests(unittest.TestCase):
    """No-embed (put_page) and keyword-only (query op) enforcement.

    Post-refactor locations: put_page lives in src/core/ops/pages.ts and the
    query op in src/core/ops/search.ts (upstream v0.46.x tranche-1 refactor).
    The pre-refactor src/core/operations.ts must NOT be patched.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_no_stale_prerefactor_paths(self) -> None:
        """The v0.46.25.0 rebase moved the hunks; the pre-refactor
        operations.ts must not be patched."""
        self.assertNotIn("a/src/core/operations.ts", self.patch)

    # --- put_page noEmbed when embedding_disabled ---

    def test_patch_touches_put_page_in_ops_pages(self) -> None:
        self.assertIn("src/core/ops/pages.ts", self.patch)

    def test_patch_put_page_forces_noembed_when_disabled(self) -> None:
        """put_page must force noEmbed when config embedding_disabled=true."""
        pages_hunk = _hunk(self.patch, "a/src/core/ops/pages.ts")
        self.assertIn("const { loadConfig } = await import('../config.ts');", pages_hunk)
        self.assertIn("loadConfig()?.embedding_disabled === true", pages_hunk)
        self.assertIn(
            "const noEmbed = cfgDisabled || !isAvailable('embedding')",
            pages_hunk,
        )
        # The old unconditional noEmbed computation must be replaced.
        self.assertIn(
            "-    const noEmbed = ctx.deferEmbeds === true || !isAvailable('embedding');",
            pages_hunk,
        )

    def test_patch_put_page_noembed_before_importfromcontent_fixture(self) -> None:
        """The importFromContent call is outside the pages.ts hunk, so the
        ordering pin (noEmbed computed before importFromContent) lives in the
        upstream behavioral fixture, which reads the real refactored source."""
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
        self.assertIn("put_page noEmbed check precedes the importFromContent call", test_body)

    # --- query op keyword-only enforcement ---

    def test_patch_touches_query_op_in_ops_search(self) -> None:
        self.assertIn("src/core/ops/search.ts", self.patch)

    def test_patch_query_op_keyword_only_check(self) -> None:
        """The query op must check search.mcp_keyword_only."""
        search_hunk = _hunk(self.patch, "a/src/core/ops/search.ts")
        self.assertIn(
            "const queryKeywordOnly = (await ctx.engine.getConfig('search.mcp_keyword_only')) === 'true';",
            search_hunk,
        )
        self.assertIn("if (queryKeywordOnly) {", search_hunk)

    def test_patch_query_op_keyword_only_uses_searchkeyword(self) -> None:
        """The keyword-only block must route text queries to searchKeyword and
        must NOT touch the embedding transport (no hybridSearch /
        embedMultimodal / embedQuery inside the block)."""
        search_hunk = _hunk(self.patch, "a/src/core/ops/search.ts")
        kw_pos = search_hunk.find("if (queryKeywordOnly) {")
        self.assertGreater(kw_pos, 0, "keyword-only block not found")
        kw_end = search_hunk.find("// v0.27.1: image-similarity branch", kw_pos)
        kw_block = search_hunk[kw_pos:kw_end if kw_end > 0 else kw_pos + 1800]
        self.assertIn("ctx.engine.searchKeyword(queryText", kw_block)
        self.assertNotIn("hybridSearch", kw_block)
        self.assertNotIn("embedMultimodal(", kw_block)
        self.assertNotIn("embedQuery(", kw_block)

    def test_patch_query_op_rejects_image_under_keyword_only(self) -> None:
        """Under keyword-only, image-only queries must REJECT with an
        OperationError (they require the embedding transport)."""
        search_hunk = _hunk(self.patch, "a/src/core/ops/search.ts")
        kw_pos = search_hunk.find("if (queryKeywordOnly) {")
        kw_block = search_hunk[kw_pos:kw_pos + 1800]
        self.assertIn("if (imageData) { throw new OperationError", kw_block)
        self.assertIn(
            "Image-similarity search requires the embedding transport",
            kw_block,
        )

    def test_patch_query_op_rejects_cross_modal_image_under_keyword_only(self) -> None:
        """Under keyword-only, cross_modal='image' or 'both' must REJECT."""
        search_hunk = _hunk(self.patch, "a/src/core/ops/search.ts")
        kw_pos = search_hunk.find("if (queryKeywordOnly) {")
        kw_block = search_hunk[kw_pos:kw_pos + 1800]
        self.assertIn("kwCrossModal === 'image' || kwCrossModal === 'both'", kw_block)
        self.assertIn("throw new OperationError", kw_block)
        self.assertIn("requires the embedding transport", kw_block)

    def test_patch_query_op_keyword_only_precedes_image_branch_fixture(self) -> None:
        """The real image-similarity branch sits outside the search.ts hunk;
        the fixture pins the ordering (keyword-only check before the image
        branch) and the no-downstream-reject behavior against the refactored
        source."""
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
        self.assertIn("query op keyword-only check precedes the image branch", test_body)
        self.assertIn("does NOT reject cross_modal image/both when keyword-only is false", test_body)


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
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
        self.assertIn("revision-aware signature", test_body)
        self.assertIn("GBRAIN_EMBEDDING_MODEL_REVISION", test_body)
        self.assertIn("e5-query-passage-v1@", test_body)
        self.assertIn("revision change causes signature change", test_body)
        self.assertIn("non-E5 signature is UNCHANGED regardless of revision", test_body)
        self.assertIn("empty revision env falls back", test_body)


class GbrainEmbeddingDisabledClearContractTests(unittest.TestCase):
    """Issue #65: native migration must clear persisted `embedding_disabled`
    in FILE config.

    Historical brains initialized with `gbrain init --no-embedding` carry
    `embedding_disabled: true` in config.json. Without clearing it during
    migration, a successful migration would leave the sentinel in place and
    every later `gbrain embed` / `gbrain import` would refuse via
    assertEmbeddingEnabled. The clear is on the FILE plane (fileCfg /
    loadConfigFileOnly), so env-sourced keys are never persisted (round-2 #2).

    Two seams, both FILE-plane only:

      - the apply path (persistEmbeddingFileConfig) clears the sentinel right
        after persisting the target model/dims on a real migration,
      - the D2 verified-skip branch (converged brain — e.g. after the
        operator's disable-embeddings rollback, which retains the target
        model/dims + vectors) clears the sentinel BEFORE its skipped_no_work
        exit so enable-embeddings can lift the sentinel with zero migration
        work. The skip carve-out stays a pure skip: no probe, no tuple
        persistence (persistEmbeddingFileConfig), no finalization.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_touches_migrate_embeddings_file_config_persist(self) -> None:
        """The patch must modify migrate-embeddings.ts to clear the sentinel
        on the FILE plane."""
        self.assertIn("src/commands/migrate-embeddings.ts", self.patch)

    def test_patch_clears_embedding_disabled_on_success(self) -> None:
        """The patch must clear embedding_disabled on the FILE plane (fileCfg,
        the refactored variable name — not the merged cfg)."""
        mig_body = _hunk(self.patch, "a/src/commands/migrate-embeddings.ts")
        self.assertIn("fileCfg.embedding_disabled = undefined", mig_body)
        self.assertNotIn("cfg.embedding_disabled = undefined", mig_body)

    def test_patch_clears_sentinel_only_when_present(self) -> None:
        """The sentinel must only be cleared when it is present (not
        unconditionally written) to avoid unnecessary config.json rewrites
        on brains that never had the sentinel."""
        self.assertIn("if (fileCfg.embedding_disabled !== undefined)", self.patch)
        self.assertIn("if (fileCfg?.embedding_disabled === true)", self.patch)
        # The D2 carve-out must NOT clear an explicit `false` or an absent
        # sentinel: only the explicit `true` sentinel is lifted (round-2
        # verified-skip semantics).
        self.assertNotIn("if (fileCfg?.embedding_disabled !== undefined)", self.patch)

    def test_patch_clear_is_file_plane_and_uses_save_config(self) -> None:
        """The apply-path clear must go through saveConfig(fileCfg) — the
        file-plane write — after the model/dimensions persistence in the same
        flow."""
        mig_body = _hunk(self.patch, "a/src/commands/migrate-embeddings.ts")
        clear_pos = mig_body.find("fileCfg.embedding_disabled = undefined")
        self.assertGreater(clear_pos, 0, "clear block not found")
        clear_block = mig_body[clear_pos:clear_pos + 120]
        self.assertIn("saveConfig(fileCfg)", clear_block)
        # The clear follows the file-plane model/dims persistence.
        self.assertIn("fileCfg.embedding_model = toModel", mig_body)
        self.assertIn("fileCfg.embedding_dimensions = toDims", mig_body)

    def test_patch_d2_verified_skip_carves_out_sentinel_before_exit(self) -> None:
        """The D2 verified-skip branch must clear the sentinel BEFORE its
        skipped_no_work exit: after disable-embeddings the brain keeps the
        target model/dims + vectors, so the next enable-embeddings run
        converges (ctx.verify.complete) and never reaches the apply path —
        without the carve-out the sentinel would stay set. The carve-out is
        FILE-plane only (loadConfigFileOnly + saveConfig), guarded on the
        sentinel being explicitly `true`, and skipped entirely on `--dry-run`."""
        mig_body = _hunk(self.patch, "a/src/commands/migrate-embeddings.ts")
        self.assertIn("ctx.verify.complete && !ctx.inflightOther && ctx.rerankerPlan.action.kind === 'none'", mig_body)
        self.assertIn("skipped_no_work", mig_body)
        # The clear must be added (present) and must precede the skip output
        # inside the migrate-embeddings.ts hunk.
        skip_pos = mig_body.find("ctx.verify.complete && !ctx.inflightOther && ctx.rerankerPlan.action.kind === 'none'")
        self.assertGreater(skip_pos, 0, "D2 verified-skip branch not in hunk")
        skip_region = mig_body[skip_pos:skip_pos + 2600]
        self.assertIn("loadConfigFileOnly()", skip_region)
        self.assertIn("fileCfg?.embedding_disabled === true", skip_region)
        self.assertIn("saveConfig(fileCfg)", skip_region)
        clear_pos = skip_region.find("fileCfg.embedding_disabled = undefined")
        self.assertGreater(clear_pos, 0, "sentinel clear not inside the D2 skip")
        self.assertLess(clear_pos, skip_region.find("skipped_no_work"),
                        "sentinel clear must precede the skipped_no_work exit")

    def test_patch_d2_carve_out_does_not_mutate_on_dry_run(self) -> None:
        """Requirement (1): the D2 carve-out must not mutate config on
        --dry-run. The whole carve-out (loadConfigFileOnly + saveConfig)
        must sit behind an `if (!flags.dryRun)` guard, and the dry-run skip
        must still exit via the retained skipped_no_work result."""
        mig_body = _hunk(self.patch, "a/src/commands/migrate-embeddings.ts")
        skip_pos = mig_body.find("ctx.verify.complete && !ctx.inflightOther && ctx.rerankerPlan.action.kind === 'none'")
        self.assertGreater(skip_pos, 0, "D2 verified-skip branch not in hunk")
        skip_region = mig_body[skip_pos:skip_pos + 2600]
        dry_guard_pos = skip_region.find("if (!flags.dryRun) {")
        self.assertGreater(dry_guard_pos, 0, "dry-run guard missing from the D2 carve-out")
        # The file-plane write (saveConfig) must be INSIDE the dry-run guard.
        self.assertLess(dry_guard_pos, skip_region.find("saveConfig(fileCfg)"),
                        "saveConfig must be inside the !flags.dryRun guard")
        self.assertLess(dry_guard_pos, skip_region.find("loadConfigFileOnly()"),
                        "loadConfigFileOnly must be inside the !flags.dryRun guard")
        # The dry-run skip still reaches the existing skipped_no_work result.
        self.assertIn("skipped_no_work", skip_region[dry_guard_pos:])

    def test_patch_d2_carve_out_clears_only_explicit_true(self) -> None:
        """Requirement (2): the D2 carve-out must clear ONLY an explicit
        `fileCfg.embedding_disabled === true`, preserving an explicit `false`
        or an absent value (no rewrite, no `!== undefined` guard)."""
        mig_body = _hunk(self.patch, "a/src/commands/migrate-embeddings.ts")
        skip_pos = mig_body.find("ctx.verify.complete && !ctx.inflightOther && ctx.rerankerPlan.action.kind === 'none'")
        self.assertGreater(skip_pos, 0, "D2 verified-skip branch not in hunk")
        skip_region = mig_body[skip_pos:skip_pos + 2600]
        self.assertIn("if (fileCfg?.embedding_disabled === true) {", skip_region)
        self.assertNotIn("fileCfg?.embedding_disabled !== undefined", skip_region)
        self.assertNotIn("fileCfg?.embedding_disabled !== false", skip_region)
        # The apply path's guard (`!== undefined`) stays untouched: the
        # explicit-true narrowing is scoped to the D2 verified-skip carve-out.
        self.assertIn("if (fileCfg.embedding_disabled !== undefined)", mig_body)

    def test_patch_d2_carve_out_emits_operator_note_on_clear(self) -> None:
        """Requirement (3): when the sentinel is actually cleared, the D2
        carve-out must emit a concise operator-facing note, while retaining
        the existing skipped_no_work result."""
        mig_body = _hunk(self.patch, "a/src/commands/migrate-embeddings.ts")
        skip_pos = mig_body.find("ctx.verify.complete && !ctx.inflightOther && ctx.rerankerPlan.action.kind === 'none'")
        self.assertGreater(skip_pos, 0, "D2 verified-skip branch not in hunk")
        skip_region = mig_body[skip_pos:skip_pos + 2600]
        note_pos = skip_region.find("lifted the deferred-setup sentinel embedding_disabled")
        self.assertGreater(note_pos, 0, "operator-facing note missing from the D2 carve-out")
        # The note must be emitted after the clear and before the
        # skipped_no_work result.
        clear_pos = skip_region.find("fileCfg.embedding_disabled = undefined")
        self.assertGreater(clear_pos, 0)
        self.assertLess(clear_pos, note_pos, "note must follow the sentinel clear")
        self.assertLess(note_pos, skip_region.find("skipped_no_work"),
                        "note must precede the skipped_no_work result")
        # The note must ride the operator-facing channel (serr/stderr) so
        # --json stdout stays JSON-clean.
        self.assertIn("serr(", skip_region[:note_pos + 200])

    def test_patch_d2_carve_out_is_pure_skip(self) -> None:
        """The D2 carve-out must preserve the verified-skip semantics: no
        probe, no tuple persistence (persistEmbeddingFileConfig call), no
        migration finalization, and no work when the sentinel is absent. The
        added lines of the migrate-embeddings.ts hunk must not reintroduce
        those operation-path calls inside the skip."""
        mig_body = _hunk(self.patch, "a/src/commands/migrate-embeddings.ts")
        skip_pos = mig_body.find("ctx.verify.complete && !ctx.inflightOther && ctx.rerankerPlan.action.kind === 'none'")
        self.assertGreater(skip_pos, 0, "D2 verified-skip branch not in hunk")
        skip_region = mig_body[skip_pos:skip_pos + 2600]
        self.assertNotIn("persistEmbeddingFileConfig(", skip_region)
        self.assertNotIn("probeTargetProvider", skip_region)
        self.assertNotIn("completeEmbeddingMigration", skip_region)
        self.assertNotIn("plan.chunks_to_embed === 0", skip_region)


class GbrainNoWorkMigrationHunkAbsentContractTests(unittest.TestCase):
    """The old migrate-embeddings no-work hunk must NOT come back.

    The historical no-work condition
    (plan.chunks_to_embed === 0 && !plan.dim_change && plan.from_model === plan.to_model)
    is subsumed by the candidate's planMigrationFlow DB-reality verification
    (ctx.verify.complete / skipped_no_work): the verified skip stays
    side-effect-free apart from the issue #65 sentinel carve-out (a pure
    FILE-plane clear — no probe, no tuple persistence, no finalization). The
    v0.46.25.0-rebased patch only carries the file-plane embedding_disabled
    clear in migrate-embeddings.ts (apply path + D2 verified-skip carve-out)
    and must not reintroduce the probe/persist/finalize no-work hunk.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_no_work_condition_not_reintroduced(self) -> None:
        """No added line in the migrate-embeddings.ts hunk may reintroduce the
        no-work probe/persist/finalize block."""
        added = _added_lines(self.patch, "a/src/commands/migrate-embeddings.ts")
        self.assertNotIn("plan.chunks_to_embed === 0", added)
        self.assertNotIn("probeTargetProvider(plan.to_model, plan.to_dims)", added)
        self.assertNotIn("persistEmbeddingFileConfig(plan.to_model, plan.to_dims)", added)
        self.assertNotIn("completeEmbeddingMigration(engine, plan)", added)

    def test_patch_fixture_pins_the_verified_skip(self) -> None:
        """The upstream fixture must pin the candidate's verified no-work skip
        and assert the stale probe/persist calls are GONE from the real
        (refactored) migrate-embeddings.ts source."""
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
        self.assertIn("migrate-embeddings no-work skip is the verified upstream flow", test_body)
        self.assertIn("ctx.verify.complete", test_body)
        self.assertIn("skipped_no_work", test_body)
        self.assertIn('not.toContain("probeTargetProvider(plan.to_model, plan.to_dims)")', test_body)
        self.assertIn('not.toContain("persistEmbeddingFileConfig(plan.to_model, plan.to_dims)")', test_body)


class GbrainEmbedBackfillFinalizationContractTests(unittest.TestCase):
    """Issue #65: candidate-compatible post-backfill migration finalization.

    The split enable-embeddings + embed-backfill flow runs
    `migrate embeddings --no-embed` (which sets MIGRATION_STATE_KEY +
    invalidates + persists config but skips the re-embed pass), then
    `embed --stale --include-null-signature` separately. The embed.ts hook
    finalizes a matching in-flight migration after a verified zero-failure
    stale backfill via the SAME E5-aware migrationSignature() used by the
    migration planner.

    Upstream v0.46.25.0 already uses migrationSignature() at the native
    completion probe in migrate-embeddings.ts, so the patch pins that as
    upstream (via the fixture) instead of carrying a stale hunk.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_embed_imports_shared_migration_finalization(self) -> None:
        """The hook must import the shared finalization + revision-aware
        signature from embedding-migration.ts in one dynamic import."""
        embed_hunk = _hunk(self.patch, "a/src/commands/embed.ts")
        self.assertIn(
            "MIGRATION_STATE_KEY, completeEmbeddingMigration, reconcilePageSignatures, migrationSignature",
            embed_hunk,
        )
        self.assertIn("import('../core/embedding-migration.ts')", embed_hunk)

    def test_patch_embed_finalization_gate_is_exact(self) -> None:
        """The hook must be gated on stale + includeNullSignature + not
        dry-run + zero failures — asserted in the ADDED lines of the embed.ts
        hunk (not merely anywhere in the patch, where the appended upstream
        test file would also match)."""
        added = _added_lines(self.patch, "a/src/commands/embed.ts")
        self.assertIn(
            "if (opts.stale && opts.includeNullSignature === true && !opts.dryRun && result.failures === 0) {",
            added,
            "the runEmbedCore finalization gate must require opts.includeNullSignature === true",
        )
        # The old incomplete gate (no includeNullSignature) must not be added
        # back to the embed.ts source hunk.
        self.assertNotIn(
            "if (opts.stale && !opts.dryRun && result.failures === 0) {",
            added,
            "the finalization gate must not omit opts.includeNullSignature",
        )

    def test_patch_embed_finalization_uses_revision_aware_comparison(self) -> None:
        """The marker must match via the shared E5-aware migrationSignature,
        not a bare model:dims string compare."""
        embed_hunk = _hunk(self.patch, "a/src/commands/embed.ts")
        self.assertIn(
            "migrationSignature(state.to_model, state.to_dims) === currentSig",
            embed_hunk,
        )
        self.assertIn("const currentSig = currentEmbeddingSignature();", embed_hunk)

    def test_patch_embed_finalization_completes_when_zero_remaining(self) -> None:
        """After reconcilePageSignatures, a zero finalRemaining stale count
        must call completeEmbeddingMigration."""
        embed_hunk = _hunk(self.patch, "a/src/commands/embed.ts")
        self.assertIn(
            "engine.countStaleChunks({ signature: currentSig, includeNullSignature: true })",
            embed_hunk,
        )
        self.assertIn("if (finalRemaining === 0)", embed_hunk)
        self.assertIn("await completeEmbeddingMigration(engine, plan as any);", embed_hunk)

    def test_patch_embed_finalization_has_no_blanket_catch(self) -> None:
        """Matching finalization errors must PROPAGATE (no blanket catch) so
        a real failure surfaces instead of silently leaving the marker set."""
        embed_hunk = _hunk(self.patch, "a/src/commands/embed.ts")
        hook_pos = embed_hunk.find(
            "if (opts.stale && opts.includeNullSignature === true && !opts.dryRun && result.failures === 0) {"
        )
        self.assertGreater(hook_pos, 0, "finalization hook not found")
        hook_end = embed_hunk.find("\ndiff --git", hook_pos)
        hook_block = embed_hunk[hook_pos:hook_end if hook_end > 0 else hook_pos + 1400]
        self.assertNotIn("} catch {", hook_block)
        self.assertNotIn("} catch (", hook_block)

    def test_patch_completion_probe_is_upstream_not_patched(self) -> None:
        """v0.46.25.0 upstream already uses the shared E5-aware
        migrationSignature() at the migrate-embeddings completion probe; the
        patch must not carry a hunk for it (no added line, and no bare
        signature either). The fixture pins the upstream probe against real
        source."""
        added = _added_lines(self.patch, "a/src/commands/migrate-embeddings.ts")
        self.assertNotIn("migrationSignature(plan.to_model, plan.to_dims)", added)
        self.assertNotIn("`${plan.to_model}:${plan.to_dims}`", added)
        test_body = self.patch[self.patch.find("test/ai/e5-preprocess.test.ts"):]
        self.assertIn(
            "migrate-embeddings completion probe uses the shared E5-aware migrationSignature",
            test_body,
        )
        self.assertIn("Upstream v0.46.25.0 already uses migrationSignature()", test_body)
        self.assertIn('toContain("signature: migrationSignature(plan.to_model, plan.to_dims)")', test_body)


class GbrainAdaptedUpstreamFixtureContractTests(unittest.TestCase):
    """The upstream behavioral fixture must be adapted to the refactored tree.

    test/ai/e5-preprocess.test.ts contains source-contract tests that read the
    REAL patched sources at runtime (no engine). They must reference the
    v0.46.x tranche-1 refactor paths (src/core/ops/pages.ts,
    src/core/ops/search.ts) and pin the moved semantics: the no-work hunk is
    gone, the completion probe is upstream, and the embed.ts hook is the
    patched finalization seam.
    """

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def _test_body(self) -> str:
        test_pos = self.patch.find("test/ai/e5-preprocess.test.ts")
        self.assertGreater(test_pos, 0, "test file not added by patch")
        return self.patch[test_pos:]

    def test_patch_fixture_reads_refactored_source_paths(self) -> None:
        body = self._test_body()
        self.assertIn("src/core/ops/pages.ts", body)
        self.assertIn("src/core/ops/search.ts", body)
        # The fixture header must acknowledge the refactor.
        self.assertIn("v0.46.x tranche-1 refactor", body)

    def test_patch_fixture_covers_runtime_enforcement_scope(self) -> None:
        body = self._test_body()
        self.assertIn("runtime opt-in/rollback enforcement", body)

    def test_patch_fixture_covers_put_page_enforcement(self) -> None:
        body = self._test_body()
        self.assertIn("put_page forces noEmbed when config embedding_disabled=true", body)
        self.assertIn("loadConfig()?.embedding_disabled === true", body)
        self.assertIn("const noEmbed = cfgDisabled || !isAvailable('embedding')", body)
        self.assertIn("put_page noEmbed check precedes the importFromContent call", body)

    def test_patch_fixture_covers_query_keyword_only_enforcement(self) -> None:
        body = self._test_body()
        self.assertIn("query op honors search.mcp_keyword_only=true for text queries", body)
        self.assertIn("ctx.engine.searchKeyword(queryText", body)
        self.assertIn("Image-similarity search requires the embedding transport", body)
        self.assertIn("query op rejects image-only queries under keyword-only", body)
        self.assertIn("query op rejects cross_modal image/both under keyword-only", body)
        self.assertIn("query op keyword-only check precedes the image branch", body)
        self.assertIn("does NOT reject cross_modal image/both when keyword-only is false", body)

    def test_patch_fixture_covers_no_work_skip_and_completion_probe(self) -> None:
        body = self._test_body()
        self.assertIn("migrate-embeddings no-work skip is the verified upstream flow", body)
        self.assertIn("skipped_no_work", body)
        self.assertIn("migrate-embeddings completion probe uses the shared E5-aware migrationSignature", body)

    def test_patch_fixture_covers_embed_finalization(self) -> None:
        body = self._test_body()
        self.assertIn("embed backfill finalizes matching in-flight migration", body)
        self.assertIn("embed backfill finalization uses revision-aware migrationSignature", body)
        self.assertIn("migrationSignature(state.to_model, state.to_dims) === currentSig", body)
        self.assertIn("embed backfill finalization has no blanket catch", body)
        self.assertIn("embed backfill finalization gated on stale, null-signature, and not dry-run", body)

    def test_patch_fixture_covers_file_plane_sentinel_clear(self) -> None:
        body = self._test_body()
        self.assertIn("persistEmbeddingFileConfig clears the embedding_disabled sentinel on success", body)
        self.assertIn("fileCfg.embedding_disabled = undefined", body)
        self.assertIn("saveConfig(fileCfg)", body)
        # The D2 verified-skip carve-out must also be pinned by the fixture:
        # the clear sits INSIDE the verified no-work skip, before its
        # skipped_no_work exit, and stays a pure skip (no tuple persistence /
        # probe / finalization). All three round-2 requirements are pinned:
        # (1) no config mutation on --dry-run, (2) only an explicit `true`
        # sentinel is cleared (false/absent preserved), (3) an operator-facing
        # note is emitted when the sentinel is actually cleared.
        self.assertIn("D2 verified-skip clears the embedding_disabled sentinel (file-plane carve-out)", body)
        self.assertIn("fileCfg?.embedding_disabled === true", body)
        self.assertIn("loadConfigFileOnly()", body)
        self.assertIn("The skip stays a pure skip: no tuple persistence, probe, or migration", body)
        self.assertIn("if (!flags.dryRun) {", body)
        self.assertIn("a dry-run skip stays read-only", body)
        self.assertIn("explicit `false` or an absent key", body)
        self.assertIn("lifted the deferred-setup sentinel embedding_disabled", body)


class GbrainPatchSafetyContractTests(unittest.TestCase):
    """The patch must not introduce destructive reinit and must keep the
    revision-aware signature seams."""

    def setUp(self) -> None:
        self.assertTrue(PATCH_PATH.is_file(), f"missing patch: {PATCH_PATH}")
        self.patch = _read(PATCH_PATH)

    def test_patch_preserves_revision_aware_signatures(self) -> None:
        self.assertIn("e5SignatureSuffix", self.patch)
        self.assertIn("GBRAIN_EMBEDDING_MODEL_REVISION", self.patch)
        self.assertIn("E5_PREPROCESS_VERSION", self.patch)

    def test_patch_no_destructive_reinit(self) -> None:
        """No reinit-pglite or destructive schema rebuild in the patch."""
        self.assertNotIn("reinit-pglite", self.patch)
        self.assertNotIn("reinit_pglite", self.patch)
        # The patch must not add a DROP TABLE or TRUNCATE statement in the
        # added lines. Match only SQL statements (word-boundary, not
        # substrings inside comments like "TRUNCATED").
        for line in self.patch.splitlines():
            if line.startswith("+"):
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
