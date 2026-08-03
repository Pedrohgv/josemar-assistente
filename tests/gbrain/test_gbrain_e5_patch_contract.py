"""Source-contract tests for the stored gbrain E5 preprocessing patch.

These tests inspect `patches/gbrain-inline-worker-gateway.patch` (no Docker, no
gbrain binary required) to guard the E5 query/passage preprocessing seam that
the embeddings overlay relies on. They keep the patch honest about:

  - E5 query/passage preprocessing is present in the embed() seam,
  - a versioned embedding signature is stamped for E5 models,
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
        # currentEmbeddingSignature must append the E5 preprocessing version
        # for E5 models and keep the exact pre-patch shape for non-E5.
        self.assertIn("E5_PREPROCESS_VERSION", self.patch)
        self.assertIn("isE5EmbeddingModel(gatewayGetModel())", self.patch)
        # The signature must branch: E5 -> `${base}#${version}`, non-E5 -> base.
        self.assertIn("`${base}#${E5_PREPROCESS_VERSION}`", self.patch)

    def test_patch_includes_upstream_test_file(self) -> None:
        # The patch must add the upstream test file for the E5 preprocessing
        # seam so the contract is executable in the gbrain tree.
        self.assertIn("test/ai/e5-preprocess.test.ts", self.patch)

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


if __name__ == "__main__":
    unittest.main()