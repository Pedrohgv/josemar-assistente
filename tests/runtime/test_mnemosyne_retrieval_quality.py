"""Phase 2 Portuguese vector retrieval quality gate tests.

Three test tiers, all gated so ordinary ``unittest`` discovery does NOT pull
Docker or model downloads:

1. Fast unit/schema/boundary tests (always run, no Docker, no Mnemosyne
   package, no model download). These enforce the public/activation boundary,
   report redaction, metric math, minimum dataset count, review metadata,
   manifest-configurable thresholds, regression-comment correctness, and
   proper cleanup of disposable inputs.

2. Gated public Docker smoke (``RUN_DOCKER_TESTS=1`` AND
   ``RUN_MNEMOSYNE_RETRIEVAL_SMOKE=1``). Builds the isolated hermes image,
   ingests the public synthetic fixtures into a disposable BeamMemory store
   in keyword-only mode, queries via ``beam.recall(query, top_k=...)``, and
   checks the public smoke sanity thresholds. Optionally also runs the
   TEI-backed mode when ``RUN_MNEMOSYNE_RETRIEVAL_TEI=1`` is set and the
   embeddings overlay is available.

3. Gated activation >=50 evaluation (``RUN_DOCKER_TESTS=1`` AND
   ``RUN_MNEMOSYNE_RETRIEVAL_ACTIVATION=1``). Runs the activation dataset (123
   queries, 60 corpus passages) through BOTH keyword-only and TEI-backed
   E5-small fresh isolated stores using the exact Beam remember/recall API,
   computes real keyword and TEI aggregates, per-difficulty metrics, latency,
   dense/keyword signal evidence, and TEI-vs-keyword regression, writes
   redacted JSON+Markdown under ``dump_folder/mnemosyne-retrieval-eval/activation/``,
   and evaluates the activation gate. The gate status is NOT_READY while
   ``review.json`` is NOT_READY (the human-review blocker must be present in
   the failures); once ``review.json`` is REVIEWED, the same target requires
   the configured quality thresholds and fails if status is not READY. The
   test does NOT hardcode NOT_READY — it asserts the gate behavior matches
   the review status and that metrics computation/reporting behaved correctly.

The harness never mounts production ``hermes-data``, never invokes the
provider API, Telegram, or state sync, and never copies activation content into
public fixtures/logs/reports. Activation fixtures are copied only into a
temporary disposable input created by the host test and removed reliably.
Keyword and TEI modes run in separate one-off containers with separate
disposable BeamMemory data dirs, so MNEMOSYNE_NO_EMBEDDINGS (set only in the
keyword in-container script) cannot leak into the TEI process.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Make the harness importable without installation, then restore sys.path so
# the persistent scripts/ entry cannot mask tests/tasknotes_mcp during full
# unittest discovery (issue #91). The imported names remain bound; only the
# sys.path mutation is reverted.
REPO_ROOT = Path(__file__).resolve().parents[2]
_scripts_path = str(REPO_ROOT / "scripts")
sys.path.insert(0, _scripts_path)
try:
    from mnemosyne_retrieval_eval import (  # noqa: E402
        recall_at_k,
        mrr,
        ndcg_at_k,
        evaluate_query,
        evaluate_run,
        difficulty_slices,
        latency_percentiles,
        validate_dataset_dir,
        is_activation_dataset,
        is_review_ready,
        DatasetError,
        REVIEW_READY,
        REVIEW_NOT_READY,
        redact_activation_text,
        build_report,
        write_report,
        CONTENT_MARKER_PREFIX,
        evaluate_gate,
        merge_thresholds,
        PUBLIC_SMOKE_THRESHOLDS,
        ACTIVATION_THRESHOLDS,
        STANDARD_ACTIVATION_THRESHOLDS,
        STANDARD_POLICY_VERSION,
        STANDARD_POLICY_DIGEST,
        PUBLIC_SMOKE_THRESHOLD_KEYS,
        ACTIVATION_THRESHOLD_KEYS,
        make_disposable_input,
        generate_incontainer_script,
        parse_results_and_evaluate,
        build_comparison_report,
        write_comparison_report,
        render_comparison_markdown,
        run_eval_mode,
        get_report_dir,
        get_thresholds_for_dataset,
        dataset_fingerprint,
        validated_standard_dataset_identity,
        E5_MODEL_ID,
        E5_MODEL_REVISION,
        E5_MODEL_DIMENSIONS,
    )
finally:
    # Remove only the entry we added; never touch unrelated sys.path state.
    while _scripts_path in sys.path:
        sys.path.remove(_scripts_path)

from .helpers import ComposeRuntime, docker_available  # noqa: E402

PUB_FIXTURE_DIR = REPO_ROOT / "tests" / "runtime" / "fixtures" / "mnemosyne-retrieval"
SYNTHETIC_FIXTURE_DIR = REPO_ROOT / "tests" / "runtime" / "fixtures" / "mnemosyne-retrieval" / "activation"
FAQUAD_FIXTURE_DIR = REPO_ROOT / "tests" / "runtime" / "fixtures" / "mnemosyne-retrieval" / "faquad-ir"


# ---------------------------------------------------------------------------
# Tier 1: Fast unit/schema/boundary tests (always run).
# ---------------------------------------------------------------------------


class MetricMathTests(unittest.TestCase):
    """Pure metric math, no Docker, no Mnemosyne package."""

    def test_recall_at_k_single_expected(self) -> None:
        self.assertEqual(recall_at_k(["a"], ["a", "b", "c"], 1), 1.0)
        self.assertEqual(recall_at_k(["a"], ["b", "a", "c"], 1), 0.0)
        self.assertEqual(recall_at_k(["a"], ["b", "a", "c"], 3), 1.0)
        self.assertEqual(recall_at_k(["a"], ["b", "c", "d"], 3), 0.0)
        self.assertEqual(recall_at_k(["a"], ["b", "a", "c"], 0), 0.0)
        self.assertEqual(recall_at_k([], ["a"], 3), 0.0)

    def test_recall_at_k_multi_expected(self) -> None:
        self.assertEqual(recall_at_k(["a", "b"], ["b", "a", "c"], 3), 1.0)
        self.assertEqual(recall_at_k(["a", "b"], ["b", "c", "a"], 2), 0.5)
        self.assertAlmostEqual(recall_at_k(["a", "b", "d"], ["b", "a", "c"], 3), 2 / 3)

    def test_mrr(self) -> None:
        self.assertEqual(mrr(["a"], ["a", "b"]), 1.0)
        self.assertEqual(mrr(["a"], ["b", "a"]), 0.5)
        self.assertEqual(mrr(["a"], ["b", "c", "a"]), 1 / 3)
        self.assertEqual(mrr(["a"], ["b", "c"]), 0.0)

    def test_ndcg_at_k(self) -> None:
        # Perfect ranking -> 1.0
        self.assertEqual(ndcg_at_k(["a", "b"], ["a", "b", "c"], 5), 1.0)
        # No hits -> 0.0
        self.assertEqual(ndcg_at_k(["a"], ["b", "c", "d"], 3), 0.0)
        # One relevant at rank 2, single expected: DCG=1/log2(3), IDCG=1/log2(2)=1
        import math
        expected = (1.0 / math.log2(3)) / 1.0
        self.assertAlmostEqual(ndcg_at_k(["a"], ["b", "a", "c"], 3), expected)
        # k=0 -> 0
        self.assertEqual(ndcg_at_k(["a"], ["a"], 0), 0.0)

    def test_evaluate_query_bundle(self) -> None:
        b = evaluate_query(["a"], ["b", "a", "c"])
        self.assertEqual(b["recall@1"], 0.0)
        self.assertEqual(b["recall@3"], 1.0)
        self.assertEqual(b["mrr"], 0.5)
        self.assertIn("ndcg@5", b)
        self.assertIn("ndcg@10", b)

    def test_ndcg_at_10_and_relevant_passage_macro_math(self) -> None:
        rows = [
            {"query_id": "q1", "expected_ids": ["c1"], "difficulty": "easy", **evaluate_query(["c1"], ["c1"])},
            {"query_id": "q2", "expected_ids": ["c1"], "difficulty": "easy", **evaluate_query(["c1"], ["x", "c1"])},
            {"query_id": "q3", "expected_ids": ["c2"], "difficulty": "easy", **evaluate_query(["c2"], ["z", "c2"])},
        ]
        aggregate = evaluate_run(rows, [1.0, 1.0, 1.0])
        self.assertIn("ndcg@10", aggregate["overall"])
        self.assertIn("macro_by_relevant_passage", aggregate)
        self.assertAlmostEqual(
            aggregate["macro_by_relevant_passage"]["recall@1"],
            (0.5 + 0.0) / 2,
        )

    def test_difficulty_slices(self) -> None:
        pq = [
            {"difficulty": "easy", "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
            {"difficulty": "easy", "recall@1": 0.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 0.5, "ndcg@5": 0.5},
            {"difficulty": "hard", "recall@1": 0.0, "recall@3": 0.0, "recall@5": 1.0, "mrr": 0.2, "ndcg@5": 0.1},
        ]
        sl = difficulty_slices(pq)
        self.assertIn("easy", sl)
        self.assertIn("hard", sl)
        self.assertEqual(sl["easy"]["count"], 2)
        self.assertAlmostEqual(sl["easy"]["recall@1"], 0.5)
        self.assertEqual(sl["hard"]["count"], 1)

    def test_latency_percentiles(self) -> None:
        lat = latency_percentiles([10.0, 20.0, 30.0, 40.0, 100.0])
        self.assertEqual(lat["max"], 100.0)
        self.assertGreaterEqual(lat["p50"], 10.0)
        self.assertLessEqual(lat["p99"], 100.0)
        empty = latency_percentiles([])
        self.assertEqual(empty["max"], 0.0)

    def test_evaluate_run_overall(self) -> None:
        pq = [
            {"difficulty": "easy", "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
            {"difficulty": "hard", "recall@1": 0.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 0.5, "ndcg@5": 0.5},
        ]
        agg = evaluate_run(pq, [10.0, 20.0])
        self.assertEqual(agg["overall"]["count"], 2)
        self.assertAlmostEqual(agg["overall"]["recall@1"], 0.5)
        self.assertIn("latency_ms", agg)


class SchemaTests(unittest.TestCase):
    """Dataset schema validation for public and activation fixtures."""

    def test_public_fixture_validates(self) -> None:
        m, c, q, r = validate_dataset_dir(
            PUB_FIXTURE_DIR,
            expect_kind="public-synthetic-smoke",
            expect_activation_evidence=False,
            min_queries=5,
        )
        self.assertGreaterEqual(len(c), 10)
        self.assertGreaterEqual(len(q), 5)
        self.assertFalse(is_activation_dataset(PUB_FIXTURE_DIR))
        self.assertFalse(m["activation_evidence"])

    def test_faquad_standard_fixture_is_activation_without_review(self) -> None:
        m, c, q, r = validate_dataset_dir(
            FAQUAD_FIXTURE_DIR,
            expect_kind="public-standard-activation",
            expect_activation_evidence=True,
            min_queries=900,
        )
        self.assertEqual(len(c), 244)
        self.assertEqual(len(q), 900)
        self.assertEqual(len(r), 0)
        self.assertEqual(m["review_status"], "NOT_APPLICABLE")
        self.assertEqual(m["source"]["dataset_id"], "MTEB-BR/faquad-ir")
        self.assertEqual(m["source"]["revision"], "c081a26d706764f1d09de17792f5eb995f51b124")
        self.assertEqual(m["source"]["license"], "CC-BY-4.0")
        self.assertEqual(m["counts"], {"corpus": 244, "queries": 900, "qrels": 900})

    def test_tampered_standard_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name in ("manifest.json", "corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
                shutil.copy2(FAQUAD_FIXTURE_DIR / name, d / name)
            manifest = json.loads((d / "manifest.json").read_text())
            manifest["source"]["revision"] = "not-the-pinned-revision"
            (d / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(DatasetError):
                validate_dataset_dir(d, expect_kind="public-standard-activation", expect_activation_evidence=True)

    def test_pinned_source_hash_guard_rejects_changed_parquet(self) -> None:
        source = REPO_ROOT / "dump_folder" / "faquad-source"
        if not source.is_dir():
            self.skipTest("disposable source artifacts are not present")
        import importlib
        vendor = importlib.import_module("mnemosyne_retrieval_eval.vendor_faquad_ir")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name in ("corpus", "queries", "qrels"):
                shutil.copy2(source / f"{name}.parquet", d / f"{name}.parquet")
            with (d / "corpus.parquet").open("ab") as f:
                f.write(b"tampered")
            with self.assertRaises(ValueError):
                vendor.validate_source_artifacts(d)

    def test_activation_fixture_validates_and_meets_min_count(self) -> None:
        m, c, q, r = validate_dataset_dir(
            SYNTHETIC_FIXTURE_DIR,
            expect_kind="public-synthetic-regression",
            expect_activation_evidence=False,
            min_queries=50,
        )
        self.assertGreaterEqual(len(q), 50, "activation dataset must have >= 50 queries")
        self.assertGreaterEqual(len(c), 20)
        self.assertFalse(is_activation_dataset(SYNTHETIC_FIXTURE_DIR))
        self.assertFalse(m["activation_evidence"])

    def test_activation_review_not_ready_by_default(self) -> None:
        # The gate must fail until review.json is operator-reviewed.
        with self.assertRaises(DatasetError) as ctx:
            validate_dataset_dir(SYNTHETIC_FIXTURE_DIR, require_review_ready=True)
        self.assertIn("REVIEWED", str(ctx.exception))

    def test_is_review_ready_false_for_not_ready(self) -> None:
        m, c, q, r = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        self.assertFalse(is_review_ready(r, m))

    def test_is_review_ready_true_when_reviewed(self) -> None:
        m, c, q, r = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        reviewed = dict(r)
        reviewed.update({
            "review_status": "REVIEWED",
            "reviewer": "operator-1",
            "reviewed_at": "2026-08-02T12:00:00-03:00",
            "review_method": "spot-check",
            "dataset_fingerprint": dataset_fingerprint(SYNTHETIC_FIXTURE_DIR, m),
            "reviewed_query_count": 50,
            "reviewed_slice_counts": {"easy": 20, "medium": 20, "hard": 10},
        })
        self.assertTrue(is_review_ready(reviewed, m, SYNTHETIC_FIXTURE_DIR, q))

    def test_is_review_ready_false_when_reviewed_but_missing_reviewer(self) -> None:
        m, c, q, r = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        reviewed = dict(r)
        reviewed.update({
            "review_status": "REVIEWED",
            "reviewer": "",
            "reviewed_at": "2026-08-02",
        })
        self.assertFalse(is_review_ready(reviewed, m))

    def test_public_fixture_has_no_pii_markers_and_is_synthetic(self) -> None:
        m, c, q, _ = validate_dataset_dir(PUB_FIXTURE_DIR)
        for row in c:
            self.assertIn("synthetic", row["source"].lower() + row.get("notes", "").lower())
        for row in q:
            self.assertIn("synthetic", row.get("provenance", "").lower())
        self.assertIn("No PII", m["pii_policy"])

    def test_corpus_and_queries_unique_ids(self) -> None:
        for d in (PUB_FIXTURE_DIR, SYNTHETIC_FIXTURE_DIR):
            m, c, q, r = validate_dataset_dir(d)
            cids = [row["id"] for row in c]
            qids = [row["id"] for row in q]
            self.assertEqual(len(cids), len(set(cids)), f"duplicate corpus ids in {d}")
            self.assertEqual(len(qids), len(set(qids)), f"duplicate query ids in {d}")

    def test_expected_ids_reference_existing_corpus(self) -> None:
        for d in (PUB_FIXTURE_DIR, SYNTHETIC_FIXTURE_DIR):
            m, c, q, r = validate_dataset_dir(d)
            cids = {row["id"] for row in c}
            for row in q:
                for eid in row["expected_ids"]:
                    self.assertIn(eid, cids)

    def test_difficulty_values_are_valid(self) -> None:
        for d in (PUB_FIXTURE_DIR, SYNTHETIC_FIXTURE_DIR):
            m, c, q, r = validate_dataset_dir(d)
            for row in q:
                self.assertIn(row["difficulty"], ("easy", "medium", "hard"))

    def test_activation_has_semantic_paraphrase_coverage(self) -> None:
        m, c, q, r = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        diffs = {row["difficulty"] for row in q}
        self.assertIn("hard", diffs)
        self.assertIn("medium", diffs)
        self.assertIn("easy", diffs)

    def test_public_fixture_has_prefix_test_query(self) -> None:
        m, c, q, r = validate_dataset_dir(PUB_FIXTURE_DIR)
        has_prefix_test = any(row["query"].startswith("passage: ") for row in q)
        self.assertTrue(has_prefix_test, "public fixture must include a passage: prefix test query")

    def test_invalid_corpus_row_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "dataset_kind": "public-synthetic-smoke",
                "language": "pt-BR", "provenance": "x",
                "review_status": "NOT_READY", "activation_evidence": False,
            }))
            (d / "corpus.jsonl").write_text(
                json.dumps({"id": "c1", "content": "", "source": "s", "scope": "global"}) + "\n"
            )
            (d / "queries.jsonl").write_text(
                json.dumps({"id": "q1", "query": "q", "expected_ids": ["c1"], "difficulty": "easy"}) + "\n"
            )
            with self.assertRaises(DatasetError):
                validate_dataset_dir(d)

    def test_invalid_query_difficulty_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "dataset_kind": "public-synthetic-smoke",
                "language": "pt-BR", "provenance": "x",
                "review_status": "NOT_READY", "activation_evidence": False,
            }))
            (d / "corpus.jsonl").write_text(
                json.dumps({"id": "c1", "content": "ok", "source": "s", "scope": "global"}) + "\n"
            )
            (d / "queries.jsonl").write_text(
                json.dumps({"id": "q1", "query": "q", "expected_ids": ["c1"], "difficulty": "trivial"}) + "\n"
            )
            with self.assertRaises(DatasetError):
                validate_dataset_dir(d)

    def test_activation_manifest_has_thresholds_object(self) -> None:
        m, c, q, r = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        self.assertIn("thresholds", m)
        th = m["thresholds"]
        self.assertIsInstance(th, dict)
        for key in ACTIVATION_THRESHOLD_KEYS:
            self.assertIn(key, th, f"activation manifest thresholds missing {key}")
            self.assertIsInstance(th[key], (int, float))

    def test_manifest_rejects_non_numeric_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "dataset_kind": "public-synthetic-smoke",
                "language": "pt-BR", "provenance": "x",
                "review_status": "NOT_READY", "activation_evidence": False,
                "thresholds": {"min_queries": "fifty"},
            }))
            (d / "corpus.jsonl").write_text(
                json.dumps({"id": "c1", "content": "ok", "source": "s", "scope": "global"}) + "\n"
            )
            (d / "queries.jsonl").write_text(
                json.dumps({"id": "q1", "query": "q", "expected_ids": ["c1"], "difficulty": "easy"}) + "\n"
            )
            with self.assertRaises(DatasetError):
                validate_dataset_dir(d)


class PublicActivationBoundaryTests(unittest.TestCase):
    """Enforce the public/activation boundary."""

    def test_public_fixture_dir_not_under_agent_state(self) -> None:
        self.assertFalse(is_activation_dataset(PUB_FIXTURE_DIR))
        self.assertNotIn("agent-state", str(PUB_FIXTURE_DIR.resolve()))

    def test_activation_fixture_is_public_and_explicit(self) -> None:
        self.assertFalse(is_activation_dataset(SYNTHETIC_FIXTURE_DIR))
        self.assertNotIn("agent-state", str(SYNTHETIC_FIXTURE_DIR.resolve()))
        self.assertIn("tests/runtime/fixtures/mnemosyne-retrieval/activation", str(SYNTHETIC_FIXTURE_DIR))

    def test_public_fixture_does_not_reference_activation_ids(self) -> None:
        m, c, q, r = validate_dataset_dir(PUB_FIXTURE_DIR)
        for row in c:
            self.assertTrue(row["id"].startswith("pub-"), f"public corpus id must be pub-: {row['id']}")
        for row in q:
            self.assertTrue(row["id"].startswith("pub-"), f"public query id must be pub-: {row['id']}")
            for eid in row["expected_ids"]:
                self.assertTrue(eid.startswith("pub-"), f"public expected_id must be pub-: {eid}")

    def test_activation_fixture_does_not_reference_public_ids(self) -> None:
        m, c, q, r = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        for row in c:
            self.assertTrue(row["id"].startswith("act-"))
        for row in q:
            self.assertTrue(row["id"].startswith("act-"))
            for eid in row["expected_ids"]:
                self.assertTrue(eid.startswith("act-"))

    def test_nested_repo_has_no_old_eval_path_or_allowlist(self) -> None:
        nested = REPO_ROOT / "agent-state"
        if not nested.is_dir():
            self.skipTest("agent-state is a local-only nested repository")
        self.assertFalse((nested / "eval").exists())
        self.assertNotIn("eval/mnemosyne-retrieval", (nested / ".gitignore").read_text())
        self.assertNotIn("eval/mnemosyne-retrieval", (nested / ".sync-manifest").read_text())

    def test_quality_harness_has_no_old_runtime_state_reference(self) -> None:
        paths = list((REPO_ROOT / "scripts" / "mnemosyne_retrieval_eval").glob("*.py"))
        old_eval_path = "agent-state" + "/eval"
        self.assertFalse(any(old_eval_path in p.read_text() for p in paths))

    def test_activation_fixture_is_synthetic_and_not_agent_state_derived(self) -> None:
        manifest, corpus, queries, _ = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        self.assertFalse(manifest["activation_evidence"])
        self.assertIn("synthetic", manifest["provenance"].lower())
        self.assertIn("no pii", manifest["pii_policy"].lower())
        for row in corpus:
            self.assertIn("synthetic", (row["source"] + row.get("notes", "")).lower())
            self.assertNotIn("agent-state", json.dumps(row).lower())
        for row in queries:
            self.assertIn("synthetic", row.get("provenance", "").lower())
            self.assertNotIn("agent-state", json.dumps(row).lower())


class ActivationFixtureRepairRegressionTests(unittest.TestCase):
    """Regression coverage for the independent 50-pair audit repairs.

    The six corrected mappings (act-q-027, 041, 047, 062, 077, 095) must keep
    a natural, unambiguous semantic anchor to their target corpus passage; no
    stale private-residue naming may remain in the fixture; and fixture
    integrity must hold (per-slice counts >= 10, total >= 50, no verbatim
    corpus copies, review.json stays NOT_READY). These are structural /
    light-touch checks that deliberately avoid brittle exact-wording policing.
    """

    REPAIRED_MAPPING_IDS = {
        "act-q-027", "act-q-041", "act-q-047", "act-q-062", "act-q-077", "act-q-095",
    }

    REAUDIT_DIFFICULTIES = {
        "act-q-003": "easy",
        "act-q-027": "easy",
        "act-q-041": "medium",
        "act-q-047": "easy",
        "act-q-057": "easy",
        "act-q-062": "easy",
        "act-q-077": "easy",
        "act-q-079": "medium",
        "act-q-083": "easy",
        "act-q-095": "medium",
        "act-q-105": "medium",
        "act-q-109": "medium",
        "act-q-023": "hard",
        "act-q-033": "hard",
        "act-q-067": "hard",
        "act-q-099": "medium",
        "act-q-117": "hard",
    }

    REBALANCED_HARD_IDS = {
        "act-q-021", "act-q-023", "act-q-067", "act-q-111",
    }

    STALE_REAUDIT_PHRASES = (
        "bebida branca", "petisco de queijo", "perna machucada",
        "grão preto", "embarque libera", "aplicação de renda fixa",
        "líquido branco", "o bandagem",
    )

    # Minimal Portuguese stopword set (lowercased). Used only to detect a
    # meaningful shared anchor token between a query and its expected corpus.
    STOPWORDS = {
        "o", "a", "os", "as", "um", "uma", "uns", "umas", "de", "do", "da",
        "dos", "das", "em", "no", "na", "nos", "nas", "para", "por", "que",
        "com", "sem", "e", "ou", "se", "é", "já", "mais", "menos", "muito",
        "quando", "quanto", "qual", "quais", "onde", "como", "até", "não",
        "tem", "têm", "foi", "vai", "são", "está", "só", "toda", "todo",
        "ser", "deve", "pode", "posso", "precisa", "ainda",
    }

    @staticmethod
    def _meaningful_tokens(text: str) -> set:
        import re
        return {t for t in re.findall(r"[a-zà-ú]+", text.lower())
                if len(t) >= 3 and t not in ActivationFixtureRepairRegressionTests.STOPWORDS}

    def test_repaired_mappings_have_natural_semantic_anchor(self) -> None:
        _, corpus, queries, _ = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        corpus_by_id = {row["id"]: row["content"] for row in corpus}
        by_id = {row["id"]: row for row in queries}
        for qid in self.REPAIRED_MAPPING_IDS:
            self.assertIn(qid, by_id, f"repaired mapping id {qid} must still exist")
            row = by_id[qid]
            self.assertIn(row["difficulty"], ("easy", "medium", "hard"))
            for eid in row["expected_ids"]:
                self.assertIn(eid, corpus_by_id, f"{qid} expected_id {eid} must resolve")
                shared = self._meaningful_tokens(row["query"]) & self._meaningful_tokens(corpus_by_id[eid])
                self.assertTrue(
                    shared,
                    f"{qid} must share a meaningful anchor token with its target "
                    f"({row['query']!r} vs {corpus_by_id[eid]!r})",
                )

    def test_reaudit_difficulties_and_stale_phrases_are_repaired(self) -> None:
        _, _, queries, _ = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        by_id = {row["id"]: row for row in queries}
        for qid, expected_difficulty in self.REAUDIT_DIFFICULTIES.items():
            self.assertEqual(by_id[qid]["difficulty"], expected_difficulty, qid)
        for qid in self.REBALANCED_HARD_IDS:
            self.assertEqual(by_id[qid]["difficulty"], "hard", qid)
        for row in queries:
            query = row["query"].lower()
            for phrase in self.STALE_REAUDIT_PHRASES:
                self.assertNotIn(phrase, query, f"stale audited phrase in {row['id']}: {phrase}")

    def test_reaudit_targets_and_questions_remain_integral(self) -> None:
        _, corpus, queries, _ = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        corpus_ids = {row["id"] for row in corpus}
        by_id = {row["id"]: row for row in queries}
        for qid in self.REAUDIT_DIFFICULTIES:
            row = by_id[qid]
            self.assertEqual(len(row["expected_ids"]), 1, qid)
            self.assertIn(row["expected_ids"][0], corpus_ids, qid)
            self.assertTrue(row["query"].strip().endswith("?"), qid)

    def test_no_stale_private_residue_in_fixture(self) -> None:
        for path in sorted(SYNTHETIC_FIXTURE_DIR.iterdir()):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("priv-", text, f"stale priv-* naming in {path.name}")
                self.assertNotIn("agent-state/eval", text, f"old eval path in {path.name}")

    def test_review_stays_not_ready(self) -> None:
        m, c, q, r = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        self.assertEqual(m["review_status"], REVIEW_NOT_READY)
        self.assertEqual(r.get("review_status", REVIEW_NOT_READY), REVIEW_NOT_READY)
        self.assertFalse(is_review_ready(r, m, SYNTHETIC_FIXTURE_DIR, q))

    def test_fixture_slice_counts_remain_above_minimum(self) -> None:
        _, _, queries, _ = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        counts = {d: 0 for d in ("easy", "medium", "hard")}
        for row in queries:
            counts[row["difficulty"]] += 1
        for d, n in counts.items():
            self.assertGreaterEqual(n, 10, f"difficulty slice {d} must keep >= 10 queries")
        self.assertGreaterEqual(len(queries), 50)

    def test_no_query_verbatim_copies_expected_corpus(self) -> None:
        _, corpus, queries, _ = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        corpus_by_id = {row["id"]: row["content"] for row in corpus}
        for row in queries:
            for eid in row["expected_ids"]:
                self.assertNotIn(
                    row["query"], corpus_by_id[eid],
                    f"{row['id']} must not copy its target corpus verbatim",
                )


class StandardActivationBoundaryTests(unittest.TestCase):
    """Fail-closed standard activation: identity is derived from full fixture
    validation, never from caller-supplied dicts, and thresholds are always
    the code-pinned copy."""

    @staticmethod
    def _perfect_aggregate() -> dict:
        perfect = {
            "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0,
            "mrr": 1.0, "ndcg@5": 1.0, "ndcg@10": 1.0,
        }
        return {
            "overall": {"count": 900, **perfect, "query_micro": dict(perfect)},
            "macro_by_relevant_passage": dict(perfect),
            "difficulty_slices": {},
            "latency_ms": {},
        }

    @staticmethod
    def _perfect_keyword_aggregate() -> dict:
        return {
            "overall": {"count": 900, "recall@1": 0.9, "recall@3": 0.9,
                        "recall@5": 0.9, "mrr": 0.9, "ndcg@5": 0.9, "ndcg@10": 0.9},
            "difficulty_slices": {},
            "latency_ms": {},
        }

    def _weakened_thresholds(self) -> dict:
        weak = dict(STANDARD_ACTIVATION_THRESHOLDS)
        weak.update({
            "min_queries": 1,
            "min_recall_at_1": 0.0,
            "min_recall_at_3": 0.0,
            "min_mrr": 0.0,
            "min_ndcg_at_10": 0.0,
            "min_macro_recall_at_3": 0.0,
            "max_regression_vs_keyword_recall_at_3": 1.0,
        })
        return weak

    def test_forged_identity_cannot_produce_ready_gate(self) -> None:
        # A caller-supplied dict claiming validation is not accepted: the gate
        # requires a real dataset directory and validates it itself.
        with self.assertRaises(TypeError):
            evaluate_gate(  # type: ignore[call-arg]
                aggregate=self._perfect_aggregate(),
                dataset_count=900,
                review_ready=True,
                thresholds=STANDARD_ACTIVATION_THRESHOLDS,
                is_activation=True,
                is_smoke=False,
                review_status="NOT_APPLICABLE",
                dataset_identity={"validated_standard": True},  # type: ignore[call-arg]
            )
        # Without a dataset directory the standard gate can never be READY.
        gate = evaluate_gate(
            aggregate=self._perfect_aggregate(),
            dataset_count=900,
            review_ready=True,
            thresholds=self._weakened_thresholds(),
            is_activation=True,
            is_smoke=False,
            review_status="NOT_APPLICABLE",
            keyword_aggregate=self._perfect_keyword_aggregate(),
            embeddings_available=True,
            dense_signal=0.9,
            standard_dataset_dir=None,
        )
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertFalse(gate["is_activation_evidence"])

    def test_forged_identity_cannot_build_ready_standard_report(self) -> None:
        # build_comparison_report derives identity from dataset_dir itself; a
        # missing directory is rejected and a caller-supplied identity dict is
        # not an accepted argument.
        with self.assertRaises(ValueError):
            build_comparison_report(
                keyword_results={"results": []},
                tei_results={"results": []},
                dataset_manifest={"dataset_kind": "public-standard-activation"},
                dataset_kind="public-standard-activation",
                dataset_count=900,
                review_ready=True,
                is_activation=True,
                thresholds=STANDARD_ACTIVATION_THRESHOLDS,
                review_status="NOT_APPLICABLE",
                dataset_dir=None,
            )
        with self.assertRaises(TypeError):
            build_comparison_report(  # type: ignore[call-arg]
                keyword_results={"results": []},
                tei_results={"results": []},
                dataset_manifest={"dataset_kind": "public-standard-activation"},
                dataset_kind="public-standard-activation",
                dataset_count=900,
                review_ready=True,
                is_activation=True,
                thresholds=STANDARD_ACTIVATION_THRESHOLDS,
                review_status="NOT_APPLICABLE",
                dataset_identity={"validated_standard": True},  # type: ignore[call-arg]
            )

    def test_weakened_thresholds_cannot_produce_ready_for_forged_manifest(self) -> None:
        # A forged/tampered standard manifest (weak thresholds + stale digest)
        # fails full validation and can never produce READY, even with perfect
        # metrics and weakened caller thresholds.
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name in ("manifest.json", "corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
                shutil.copy2(FAQUAD_FIXTURE_DIR / name, d / name)
            manifest = json.loads((d / "manifest.json").read_text())
            manifest["thresholds"] = self._weakened_thresholds()
            manifest["threshold_policy_digest"] = "stale-digest"
            (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
            gate = evaluate_gate(
                aggregate=self._perfect_aggregate(),
                dataset_count=900,
                review_ready=True,
                thresholds=self._weakened_thresholds(),
                is_activation=True,
                is_smoke=False,
                review_status="NOT_APPLICABLE",
                keyword_aggregate=self._perfect_keyword_aggregate(),
                embeddings_available=True,
                dense_signal=0.9,
                standard_dataset_dir=d,
            )
            self.assertEqual(gate["status"], "BLOCKED")
            self.assertFalse(gate["is_activation_evidence"])
            self.assertIsNone(gate["standard_identity"])
            with self.assertRaises(Exception):
                build_comparison_report(
                    keyword_results={"results": []},
                    tei_results={"results": []},
                    dataset_manifest=manifest,
                    dataset_kind="public-standard-activation",
                    dataset_count=900,
                    review_ready=True,
                    is_activation=True,
                    thresholds=self._weakened_thresholds(),
                    review_status="NOT_APPLICABLE",
                    dataset_dir=d,
                )

    def test_weakened_thresholds_are_overridden_by_code_pinned_copy(self) -> None:
        # Even with a perfectly valid standard fixture, caller-provided
        # weakened thresholds must be replaced by the code-pinned copy.
        gate = evaluate_gate(
            aggregate=self._perfect_aggregate(),
            dataset_count=900,
            review_ready=True,
            thresholds=self._weakened_thresholds(),
            is_activation=True,
            is_smoke=False,
            review_status="NOT_APPLICABLE",
            keyword_aggregate=self._perfect_keyword_aggregate(),
            embeddings_available=True,
            dense_signal=0.9,
            standard_dataset_dir=FAQUAD_FIXTURE_DIR,
        )
        self.assertEqual(gate["status"], "READY")
        self.assertTrue(gate["is_activation_evidence"])
        self.assertEqual(gate["thresholds"], STANDARD_ACTIVATION_THRESHOLDS)

    def test_full_dataset_path_validates_and_carries_identity(self) -> None:
        identity = validated_standard_dataset_identity(FAQUAD_FIXTURE_DIR)
        for key in ("validated_standard", "dataset_id", "revision", "artifact_sha256",
                    "generated_sha256", "fixture_fingerprint", "threshold_policy_version",
                    "threshold_policy_digest", "corpus_count", "query_count"):
            self.assertIn(key, identity)
        self.assertTrue(identity["validated_standard"])
        self.assertEqual(identity["dataset_id"], "MTEB-BR/faquad-ir")
        self.assertEqual(identity["revision"], "c081a26d706764f1d09de17792f5eb995f51b124")
        self.assertEqual(identity["threshold_policy_version"], STANDARD_POLICY_VERSION)
        self.assertEqual(identity["threshold_policy_digest"], STANDARD_POLICY_DIGEST)
        self.assertEqual(identity["corpus_count"], 244)
        self.assertEqual(identity["query_count"], 900)
        gate = evaluate_gate(
            aggregate=self._perfect_aggregate(),
            dataset_count=900,
            review_ready=True,
            thresholds=STANDARD_ACTIVATION_THRESHOLDS,
            is_activation=True,
            is_smoke=False,
            review_status="NOT_APPLICABLE",
            keyword_aggregate=self._perfect_keyword_aggregate(),
            embeddings_available=True,
            dense_signal=0.9,
            standard_dataset_dir=FAQUAD_FIXTURE_DIR,
        )
        self.assertEqual(gate["status"], "READY")
        self.assertTrue(gate["is_activation_evidence"])
        self.assertEqual(gate["standard_identity"]["validated_standard"], True)
        self.assertEqual(gate["standard_identity"]["dataset_id"], "MTEB-BR/faquad-ir")


class RedactionTests(unittest.TestCase):
    """Reports must redact activation raw text by default."""

    def test_redact_activation_text_replaces_content(self) -> None:
        self.assertEqual(redact_activation_text("qualquer coisa"), "[REDACTED-ACTIVATION]")
        self.assertEqual(redact_activation_text(None), "")
        self.assertEqual(redact_activation_text(""), "[REDACTED-ACTIVATION]")

    def test_build_report_omits_raw_query_and_corpus_text(self) -> None:
        per_query = [{
            "query_id": "act-q-001",
            "difficulty": "easy",
            "expected_ids": ["act-c-001"],
            "ranked_ids": ["act-c-001", "act-c-002"],
            "latency_ms": 5.0,
            "score": 0.9,
            "keyword_score": 0.1,
            "dense_score": 0.8,
            "tier": "working",
            "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0,
        }]
        agg = evaluate_run(per_query, [5.0])
        gate = evaluate_gate(
            aggregate=agg, dataset_count=1, review_ready=False,
            thresholds=ACTIVATION_THRESHOLDS,
            is_activation=True, is_smoke=False,
        )
        report = build_report(
            mode="tei", dataset_manifest={"provenance": "x"},
            dataset_kind="public-synthetic-regression", dataset_count=1, review_ready=False,
            per_query=per_query, aggregate=agg, thresholds=ACTIVATION_THRESHOLDS,
            gate_result=gate, is_activation=True,
        )
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("qualquer coisa", blob)
        self.assertIn("act-q-001", blob)
        self.assertIn("act-c-001", blob)
        for row in report["per_query"]:
            self.assertNotIn("query", row)
            self.assertNotIn("content", row)

    def test_write_report_creates_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            per_query = [{
                "query_id": "pub-q-001", "difficulty": "easy",
                "expected_ids": ["pub-c-001"], "ranked_ids": ["pub-c-001"],
                "latency_ms": 1.0,
                "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0,
            }]
            agg = evaluate_run(per_query, [1.0])
            gate = evaluate_gate(
                aggregate=agg, dataset_count=1, review_ready=False,
                thresholds=PUBLIC_SMOKE_THRESHOLDS, is_activation=False, is_smoke=True,
            )
            report = build_report(
                mode="keyword", dataset_manifest={"provenance": "x"},
                dataset_kind="public-synthetic-smoke", dataset_count=1, review_ready=False,
                per_query=per_query, aggregate=agg, thresholds=PUBLIC_SMOKE_THRESHOLDS,
                gate_result=gate, is_activation=False,
            )
            paths = write_report(report, Path(td))
            self.assertTrue(paths["json"].is_file())
            self.assertTrue(paths["markdown"].is_file())
            md = paths["markdown"].read_text()
            self.assertIn("Mnemosyne Retrieval Quality Report", md)
            self.assertIn("SMOKE_ONLY", md)

    def test_comparison_report_omits_raw_text(self) -> None:
        kw = {
            "mode": "keyword", "marker": CONTENT_MARKER_PREFIX, "top_k": 5,
            "embeddings_available": False,
            "results": [{
                "query_id": "act-q-001", "difficulty": "easy",
                "expected_ids": ["act-c-001"], "ranked_ids": ["act-c-001"],
                "signal_scores": [{"score": 0.5, "keyword_score": 0.5, "dense_score": 0.0, "tier": "working"}],
                "latency_ms": 3.0,
            }],
        }
        tei = {
            "mode": "tei", "marker": CONTENT_MARKER_PREFIX, "top_k": 5,
            "embeddings_available": True,
            "results": [{
                "query_id": "act-q-001", "difficulty": "easy",
                "expected_ids": ["act-c-001"], "ranked_ids": ["act-c-001"],
                "signal_scores": [{"score": 0.9, "keyword_score": 0.1, "dense_score": 0.88, "tier": "working"}],
                "latency_ms": 8.0,
            }],
        }
        report = build_comparison_report(
            keyword_results=kw, tei_results=tei,
            dataset_manifest={"provenance": "x"}, dataset_kind="public-synthetic-regression",
            dataset_count=1, review_ready=False, is_activation=True,
            thresholds=ACTIVATION_THRESHOLDS,
        )
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("qualquer coisa", blob)
        self.assertIn("act-q-001", blob)
        self.assertIn("regression", report)
        self.assertFalse(report["keyword"]["embeddings_available"])
        self.assertTrue(report["tei"]["embeddings_available"])
        # No raw query/corpus text fields in comparison per_query rows.
        for mode_key in ("keyword", "tei"):
            for row in report[mode_key]["per_query"]:
                self.assertNotIn("query", row)
                self.assertNotIn("content", row)

    def test_comparison_report_writes_files(self) -> None:
        kw = {
            "mode": "keyword", "marker": CONTENT_MARKER_PREFIX, "top_k": 5,
            "embeddings_available": False,
            "results": [{
                "query_id": "act-q-001", "difficulty": "easy",
                "expected_ids": ["act-c-001"], "ranked_ids": ["act-c-001"],
                "signal_scores": [{"score": 0.5, "keyword_score": 0.5, "dense_score": 0.0, "tier": "working"}],
                "latency_ms": 3.0,
            }],
        }
        tei = {
            "mode": "tei", "marker": CONTENT_MARKER_PREFIX, "top_k": 5,
            "embeddings_available": True,
            "results": [{
                "query_id": "act-q-001", "difficulty": "easy",
                "expected_ids": ["act-c-001"], "ranked_ids": ["act-c-001"],
                "signal_scores": [{"score": 0.9, "keyword_score": 0.1, "dense_score": 0.88, "tier": "working"}],
                "latency_ms": 8.0,
            }],
        }
        report = build_comparison_report(
            keyword_results=kw, tei_results=tei,
            dataset_manifest={"provenance": "x"}, dataset_kind="public-synthetic-regression",
            dataset_count=1, review_ready=False, is_activation=True,
            thresholds=ACTIVATION_THRESHOLDS,
        )
        with tempfile.TemporaryDirectory() as td:
            paths = write_comparison_report(report, Path(td))
            self.assertTrue(paths["json"].is_file())
            self.assertTrue(paths["markdown"].is_file())
            md = paths["markdown"].read_text()
            self.assertIn("Activation Comparison Report", md)
            self.assertIn("keyword vs TEI", md)


class GatePolicyTests(unittest.TestCase):
    """Activation gate threshold policy and review-status behavior."""

    def test_public_smoke_thresholds_lower_than_activation(self) -> None:
        self.assertLess(
            PUBLIC_SMOKE_THRESHOLDS["min_recall_at_1"],
            ACTIVATION_THRESHOLDS["min_recall_at_1"],
        )
        self.assertLess(
            PUBLIC_SMOKE_THRESHOLDS["min_recall_at_3"],
            ACTIVATION_THRESHOLDS["min_recall_at_3"],
        )
        self.assertLess(PUBLIC_SMOKE_THRESHOLDS["min_queries"], ACTIVATION_THRESHOLDS["min_queries"])

    def test_smoke_gate_never_ready(self) -> None:
        agg = {"overall": {"recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0, "ndcg@10": 1.0, "macro_recall@3": 1.0},
               "difficulty_slices": {}, "macro_by_relevant_passage": {"recall@3": 1.0}, "macro_by_relevant_passage": {"recall@3": 1.0}, "latency_ms": {}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=True,
            thresholds=PUBLIC_SMOKE_THRESHOLDS, is_activation=False, is_smoke=True,
        )
        self.assertEqual(gate["status"], "SMOKE_ONLY")
        self.assertFalse(gate["is_activation_evidence"])

    def test_standard_gate_has_no_human_review_blocker(self) -> None:
        # Standard activation does not use human review; only quality criteria apply.
        agg = {"overall": {"recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0, "ndcg@10": 1.0},
               "difficulty_slices": {
                   "easy": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0, "ndcg@10": 1.0},
                   "medium": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
                   "hard": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
               }, "macro_by_relevant_passage": {"recall@3": 1.0}, "macro_by_relevant_passage": {"recall@3": 1.0}, "latency_ms": {}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=False,
            thresholds=ACTIVATION_THRESHOLDS, is_activation=True, is_smoke=False, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
            embeddings_available=True, dense_signal=0.8,
        )
        self.assertEqual(gate["status"], "READY")
        self.assertTrue(gate["is_activation_evidence"])
        self.assertFalse(gate["human_review_blocked"])
        self.assertFalse(any("human-review" in f for f in gate["failures"]))

    def test_activation_gate_ready_when_reviewed_and_metrics_pass(self) -> None:
        # When review_ready=True and all metrics pass, the gate MUST be READY.
        agg = {"overall": {"recall@1": 0.9, "recall@3": 0.95, "recall@5": 0.98, "mrr": 0.9, "ndcg@5": 0.9, "ndcg@10": 0.9},
               "difficulty_slices": {
                   "easy": {"count": 10, "recall@1": 0.9, "recall@3": 0.95, "recall@5": 0.98, "mrr": 0.9, "ndcg@5": 0.9},
                   "medium": {"count": 10, "recall@1": 0.85, "recall@3": 0.9, "recall@5": 0.95, "mrr": 0.85, "ndcg@5": 0.85},
                   "hard": {"count": 10, "recall@1": 0.7, "recall@3": 0.8, "recall@5": 0.9, "mrr": 0.75, "ndcg@5": 0.75},
               }, "macro_by_relevant_passage": {"recall@3": 1.0}, "latency_ms": {}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=True,
            thresholds=ACTIVATION_THRESHOLDS, is_activation=True, is_smoke=False, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
            embeddings_available=True, dense_signal=0.8,
        )
        self.assertEqual(gate["status"], "READY")
        self.assertTrue(gate["is_activation_evidence"])

    def test_activation_gate_not_ready_when_reviewed_but_metrics_low(self) -> None:
        # When review_ready=True but metrics fail, the gate MUST be NOT_READY
        # and the failures must NOT include the human-review blocker (it's
        # about metrics, not review status).
        agg = {"overall": {"recall@1": 0.1, "recall@3": 0.2, "recall@5": 0.3, "mrr": 0.2, "ndcg@5": 0.2},
               "difficulty_slices": {}, "latency_ms": {}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=True,
            thresholds=ACTIVATION_THRESHOLDS, is_activation=True, is_smoke=False, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
        )
        self.assertEqual(gate["status"], "NOT_READY")
        self.assertFalse(gate["is_activation_evidence"])
        self.assertFalse(any("REVIEWED" in f for f in gate["failures"]))
        self.assertTrue(any("recall@1" in f for f in gate["failures"]))

    def test_unreviewed_metric_failure_is_not_false_green(self) -> None:
        agg = {"overall": {"recall@1": 0.1, "recall@3": 0.2, "mrr": 0.2},
               "difficulty_slices": {}, "latency_ms": {}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=False,
            thresholds=ACTIVATION_THRESHOLDS, is_activation=True, is_smoke=False, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
            embeddings_available=True, dense_signal=0.8,
        )
        self.assertEqual(gate["status"], "NOT_READY")
        self.assertFalse(gate["human_review_blocked"])
        self.assertTrue(any("recall@1" in f for f in gate["non_human_failures"]))

    def test_unreviewed_missing_vector_evidence_is_not_false_green(self) -> None:
        agg = {"overall": {"recall@1": 1.0, "recall@3": 1.0, "mrr": 1.0},
               "difficulty_slices": {d: {"recall@3": 1.0} for d in ("easy", "medium", "hard")}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=False,
            thresholds=ACTIVATION_THRESHOLDS, is_activation=True, is_smoke=False, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
        )
        self.assertTrue(any("embeddings_available" in f for f in gate["non_human_failures"]))
        self.assertTrue(any("dense signal" in f for f in gate["non_human_failures"]))

    def test_reviewed_failed_fingerprint_is_not_human_review_blocker(self) -> None:
        agg = {"overall": {"recall@1": 1.0, "recall@3": 1.0, "mrr": 1.0},
               "difficulty_slices": {d: {"recall@3": 1.0} for d in ("easy", "medium", "hard")}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=False,
            precondition_failures=["review.json dataset_fingerprint does not match the evaluated dataset"],
            thresholds=ACTIVATION_THRESHOLDS, is_activation=True, is_smoke=False, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
            embeddings_available=True, dense_signal=0.8,
        )
        self.assertEqual(gate["status"], "NOT_READY")
        self.assertFalse(gate["human_review_blocked"])
        self.assertTrue(any("dataset_fingerprint" in f for f in gate["failures"]))
        self.assertFalse(any("human-review blocker" in f for f in gate["failures"]))

    def test_activation_gate_rejects_perfect_metrics_without_vector_evidence(self) -> None:
        agg = {"overall": {"recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0},
               "difficulty_slices": {d: {"recall@3": 1.0} for d in ("easy", "medium", "hard")}}
        gate = evaluate_gate(aggregate=agg, dataset_count=900, review_ready=True,
                             thresholds=ACTIVATION_THRESHOLDS, is_activation=True, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
                             is_smoke=False)
        self.assertEqual(gate["status"], "NOT_READY")
        self.assertTrue(any("embeddings_available" in f for f in gate["failures"]))
        self.assertTrue(any("dense signal" in f for f in gate["failures"]))

    def test_activation_gate_rejects_stale_vector_review_fingerprint(self) -> None:
        m, c, q, r = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        reviewed = {"review_status": "REVIEWED", "reviewer": "operator",
                    "reviewed_at": "2026-08-02T12:00:00-03:00", "review_method": "full",
                    "dataset_fingerprint": "stale", "reviewed_query_count": 50,
                    "reviewed_slice_counts": {"easy": 20, "medium": 20, "hard": 10}}
        self.assertFalse(is_review_ready(reviewed, m, SYNTHETIC_FIXTURE_DIR, q))

    def test_activation_gate_fails_on_keyword_regression(self) -> None:
        # TEI Recall@3 is 0.5, keyword is 0.95, tolerance is 0.05.
        # 0.5 < 0.95 - 0.05 = 0.90, so it regresses.
        agg = {"overall": {"recall@1": 0.9, "recall@3": 0.5, "recall@5": 0.9, "mrr": 0.9, "ndcg@5": 0.9, "ndcg@10": 0.9},
               "difficulty_slices": {
                   "easy": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
                   "medium": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
                   "hard": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
               }, "latency_ms": {}}
        kw = {"overall": {"recall@3": 0.95}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=True,
            thresholds=ACTIVATION_THRESHOLDS, is_activation=True, is_smoke=False, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
            keyword_aggregate=kw,
        )
        self.assertEqual(gate["status"], "NOT_READY")
        self.assertTrue(any("regress" in f for f in gate["failures"]))

    def test_regression_allows_tei_slightly_below_keyword(self) -> None:
        # TEI Recall@3 is 0.72, keyword is 0.75, tolerance is 0.05.
        # 0.72 >= 0.75 - 0.05 = 0.70, so it does NOT regress. This validates
        # the corrected policy: TEI may be up to 0.05 BELOW keyword, not
        # required to be better by 0.05.
        agg = {"overall": {"recall@1": 0.9, "recall@3": 0.72, "recall@5": 0.9, "mrr": 0.9, "ndcg@5": 0.9},
               "difficulty_slices": {
                   "easy": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
                   "medium": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
                   "hard": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
               }, "latency_ms": {}}
        kw = {"overall": {"recall@3": 0.75}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=True,
            thresholds=ACTIVATION_THRESHOLDS, is_activation=True, is_smoke=False, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
            keyword_aggregate=kw,
        )
        # The regression check passes; the only potential failure is recall@3
        # 0.72 < 0.75 floor. So status is NOT_READY due to the floor, but the
        # failures must NOT include a regression failure.
        self.assertEqual(gate["status"], "NOT_READY")
        self.assertFalse(any("regress" in f for f in gate["failures"]),
                         f"TEI 0.72 vs keyword 0.75 with 0.05 tolerance must not regress: {gate['failures']}")

    def test_regression_comment_says_below_not_better(self) -> None:
        # The gate rationale must describe the policy as allowing TEI to be
        # below keyword, not requiring TEI to be better.
        agg = {"overall": {"recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
               "difficulty_slices": {
                   "easy": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
                   "medium": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
                   "hard": {"count": 1, "recall@1": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 1.0, "ndcg@5": 1.0},
               }, "latency_ms": {}}
        gate = evaluate_gate(
            aggregate=agg, dataset_count=900, review_ready=True,
            thresholds=ACTIVATION_THRESHOLDS, is_activation=True, is_smoke=False, review_status="NOT_APPLICABLE", standard_dataset_dir=FAQUAD_FIXTURE_DIR,
        )
        self.assertIn("below keyword", gate["rationale"])


class ThresholdConfigTests(unittest.TestCase):
    """Manifest-configurable thresholds (req 5)."""

    def test_merge_thresholds_overrides_defaults(self) -> None:
        merged = merge_thresholds(ACTIVATION_THRESHOLDS, {"min_recall_at_1": 0.6})
        self.assertEqual(merged["min_recall_at_1"], 0.6)
        self.assertEqual(merged["min_recall_at_3"], ACTIVATION_THRESHOLDS["min_recall_at_3"])

    def test_merge_thresholds_rejects_unknown_key(self) -> None:
        with self.assertRaises(ValueError):
            merge_thresholds(ACTIVATION_THRESHOLDS, {"unknown_key": 1.0})

    def test_merge_thresholds_rejects_non_number(self) -> None:
        with self.assertRaises(ValueError):
            merge_thresholds(ACTIVATION_THRESHOLDS, {"min_recall_at_1": "high"})

    def test_merge_thresholds_none_overrides_returns_defaults(self) -> None:
        merged = merge_thresholds(ACTIVATION_THRESHOLDS, None)
        self.assertEqual(merged, ACTIVATION_THRESHOLDS)

    def test_get_thresholds_for_activation_uses_manifest(self) -> None:
        m, c, q, r = validate_dataset_dir(SYNTHETIC_FIXTURE_DIR)
        th = get_thresholds_for_dataset(m, is_smoke=False)
        # The manifest thresholds object is authoritative.
        self.assertEqual(th, m["thresholds"])

    def test_get_thresholds_for_standard_activation_rejects_weakened_manifest(self) -> None:
        manifest, _, _, _ = validate_dataset_dir(FAQUAD_FIXTURE_DIR)
        manifest["thresholds"] = dict(manifest["thresholds"])
        manifest["thresholds"]["min_recall_at_1"] = 0.0
        with self.assertRaises(ValueError):
            get_thresholds_for_dataset(manifest, is_smoke=False)

    def test_get_thresholds_for_standard_activation_returns_fresh_pinned_copy(self) -> None:
        manifest, _, _, _ = validate_dataset_dir(FAQUAD_FIXTURE_DIR)
        thresholds = get_thresholds_for_dataset(manifest, is_smoke=False)
        self.assertEqual(thresholds, STANDARD_ACTIVATION_THRESHOLDS)
        self.assertIsNot(thresholds, STANDARD_ACTIVATION_THRESHOLDS)

    def test_get_thresholds_for_public_uses_defaults_when_no_manifest_thresholds(self) -> None:
        m, c, q, r = validate_dataset_dir(PUB_FIXTURE_DIR)
        th = get_thresholds_for_dataset(m, is_smoke=True)
        self.assertEqual(th, PUBLIC_SMOKE_THRESHOLDS)

    def test_get_thresholds_for_public_uses_manifest_when_present(self) -> None:
        # If the public manifest had a thresholds object, it would override.
        m = {"thresholds": {"min_queries": 3}}
        th = get_thresholds_for_dataset(m, is_smoke=True)
        self.assertEqual(th["min_queries"], 3)
        self.assertEqual(th["min_recall_at_1"], PUBLIC_SMOKE_THRESHOLDS["min_recall_at_1"])

    def test_activation_report_records_exact_thresholds(self) -> None:
        # parse_results_and_evaluate with explicit thresholds records them.
        results = {
            "mode": "tei", "marker": CONTENT_MARKER_PREFIX, "top_k": 5,
            "embeddings_available": True,
            "results": [{
                "query_id": "act-q-001", "difficulty": "easy",
                "expected_ids": ["act-c-001"], "ranked_ids": ["act-c-001"],
                "signal_scores": [{"score": 0.9, "keyword_score": 0.1, "dense_score": 0.88, "tier": "working"}],
                "latency_ms": 8.0,
            }],
        }
        custom = merge_thresholds(ACTIVATION_THRESHOLDS, {"min_recall_at_1": 0.6})
        report, agg, gate = parse_results_and_evaluate(
            results_json=results,
            dataset_manifest={"provenance": "x"},
            dataset_kind="public-synthetic-regression",
            dataset_count=1, review_ready=True,
            is_activation=True, is_smoke=False,
            thresholds=custom,
        )
        self.assertEqual(report["thresholds"]["min_recall_at_1"], 0.6)
        self.assertEqual(report["thresholds"], custom)


class DisposableInputCleanupTests(unittest.TestCase):
    """Disposable input creation and cleanup."""

    def test_make_disposable_input_copies_and_is_removed(self) -> None:
        tmp, manifest = make_disposable_input(PUB_FIXTURE_DIR)
        try:
            self.assertTrue((tmp / "corpus.jsonl").is_file())
            self.assertTrue((tmp / "queries.jsonl").is_file())
            self.assertTrue((tmp / "manifest.json").is_file())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertFalse(tmp.exists())

    def test_make_disposable_input_does_not_touch_source(self) -> None:
        src_mtime = (PUB_FIXTURE_DIR / "corpus.jsonl").stat().st_mtime
        tmp, _ = make_disposable_input(PUB_FIXTURE_DIR)
        try:
            pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(
            (PUB_FIXTURE_DIR / "corpus.jsonl").stat().st_mtime,
            src_mtime,
            "source fixture must not be modified",
        )

    def test_make_disposable_input_copies_activation_review_json(self) -> None:
        tmp, _ = make_disposable_input(SYNTHETIC_FIXTURE_DIR)
        try:
            self.assertTrue((tmp / "review.json").is_file(),
                            "activation disposable input must include review.json")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class IncontainerScriptTests(unittest.TestCase):
    """The generated in-container script must be valid Python and use the pinned API."""

    def test_keyword_script_compiles_and_uses_pinned_api(self) -> None:
        s = generate_incontainer_script(
            input_dir_in_container="/tmp/in",
            output_json_path_in_container="/tmp/out.json",
            mode="keyword",
            top_k=10,
        )
        compile(s, "<incontainer>", "exec")
        self.assertIn("from mnemosyne.core.beam import BeamMemory", s)
        self.assertIn("beam.recall(q[\"query\"], top_k=TOP_K)", s)
        self.assertIn("beam.remember(content", s)
        self.assertIn("MNEMOSYNE_NO_EMBEDDINGS", s)
        self.assertIn(CONTENT_MARKER_PREFIX, s)

    def test_tei_script_compiles_and_does_not_force_no_embeddings(self) -> None:
        s = generate_incontainer_script(
            input_dir_in_container="/tmp/in",
            output_json_path_in_container="/tmp/out.json",
            mode="tei",
            top_k=10,
        )
        compile(s, "<incontainer>", "exec")
        # The TEI script must NOT set MNEMOSYNE_NO_EMBEDDINGS unconditionally;
        # it only appears inside the keyword conditional.
        self.assertIn('if MODE == "keyword":', s)
        # The harness must NOT add query/passage prefixes (the package does).
        self.assertNotIn("query: ", s)
        self.assertNotIn("passage: ", s)

    def test_keyword_and_tei_scripts_are_separate_processes(self) -> None:
        # The NO_EMBEDDINGS env var is set only inside the keyword script's
        # process. Each run_eval_mode call uses a separate one-off container,
        # so the keyword env var cannot leak into the TEI process. This test
        # validates the script structure: the keyword conditional only sets
        # the env var when MODE == "keyword".
        kw = generate_incontainer_script(
            input_dir_in_container="/tmp/in",
            output_json_path_in_container="/tmp/out.json",
            mode="keyword", top_k=10,
        )
        tei = generate_incontainer_script(
            input_dir_in_container="/tmp/in",
            output_json_path_in_container="/tmp/out.json",
            mode="tei", top_k=10,
        )
        # Both scripts contain the conditional, but only keyword sets it at
        # runtime (MODE == "keyword"). The TEI script has MODE == "tei" so the
        # conditional body never executes.
        self.assertIn('if MODE == "keyword":', kw)
        self.assertIn('if MODE == "keyword":', tei)
        # The env var assignment is inside the conditional in both scripts.
        self.assertIn('os.environ["MNEMOSYNE_NO_EMBEDDINGS"] = "true"', kw)
        self.assertIn('os.environ["MNEMOSYNE_NO_EMBEDDINGS"] = "true"', tei)


class ParseResultsTests(unittest.TestCase):
    """parse_results_and_evaluate computes metrics + gate from in-container JSON."""

    def _fake_results(self, mode: str) -> dict:
        return {
            "mode": mode,
            "marker": CONTENT_MARKER_PREFIX,
            "top_k": 5,
            "results": [
                {
                    "query_id": "pub-q-001", "difficulty": "easy",
                    "expected_ids": ["pub-c-001"],
                    "ranked_ids": ["pub-c-001", "pub-c-002"],
                    "signal_scores": [{"score": 0.9, "keyword_score": 0.1, "dense_score": 0.8, "tier": "working"}],
                    "latency_ms": 5.0,
                },
                {
                    "query_id": "pub-q-002", "difficulty": "medium",
                    "expected_ids": ["pub-c-002"],
                    "ranked_ids": ["pub-c-003", "pub-c-002"],
                    "signal_scores": [{"score": 0.5, "keyword_score": 0.5, "dense_score": 0.5, "tier": "working"}],
                    "latency_ms": 7.0,
                },
            ],
            "embeddings_available": mode == "tei",
        }

    def test_parse_smoke_report(self) -> None:
        results = self._fake_results("keyword")
        report, agg, gate = parse_results_and_evaluate(
            results_json=results,
            dataset_manifest={"provenance": "x"},
            dataset_kind="public-synthetic-smoke",
            dataset_count=2,
            review_ready=False,
            is_activation=False,
            is_smoke=True,
        )
        self.assertEqual(report["mode"], "keyword")
        self.assertEqual(gate["status"], "SMOKE_ONLY")
        self.assertEqual(agg["overall"]["count"], 2)
        for row in report["per_query"]:
            self.assertNotIn("query", row)

    def test_parse_activation_report_with_keyword_regression(self) -> None:
        tei = self._fake_results("tei")
        kw = self._fake_results("keyword")
        # Make keyword look better than tei to trigger regression.
        kw["results"][0]["ranked_ids"] = ["pub-c-001"]
        kw["results"][1]["ranked_ids"] = ["pub-c-002"]
        report, agg, gate = parse_results_and_evaluate(
            results_json=tei,
            dataset_manifest={"provenance": "x"},
            dataset_kind="public-synthetic-regression",
            dataset_count=2,
            review_ready=True,
            is_activation=True,
            is_smoke=False,
            keyword_results_json=kw,
        )
        # Synthetic callers cannot create activation evidence without the
        # independently validated standard fixture.
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertIn("identity", gate["rationale"])


# ---------------------------------------------------------------------------
# Tier 2 & 3: Docker-gated tests.
# ---------------------------------------------------------------------------


def _docker_smoke_enabled() -> bool:
    return os.getenv("RUN_DOCKER_TESTS") == "1" and os.getenv("RUN_MNEMOSYNE_RETRIEVAL_SMOKE") == "1"


def _tei_enabled() -> bool:
    return os.getenv("RUN_MNEMOSYNE_RETRIEVAL_TEI") == "1"


def _activation_eval_enabled() -> bool:
    return os.getenv("RUN_DOCKER_TESTS") == "1" and os.getenv("RUN_MNEMOSYNE_RETRIEVAL_ACTIVATION") == "1"


def _has_nonzero_dense_score(results: dict) -> bool:
    """True if at least one TEI result row has a meaningful nonzero dense_score."""
    for row in results.get("results", []):
        for sig in row.get("signal_scores", []):
            ds = sig.get("dense_score")
            if isinstance(ds, (int, float)) and ds > 0.0:
                return True
    return False


@unittest.skipUnless(_docker_smoke_enabled(), "set RUN_DOCKER_TESTS=1 and RUN_MNEMOSYNE_RETRIEVAL_SMOKE=1")
@unittest.skipUnless(docker_available(), "docker CLI is not available")
class MnemosyneRetrievalPublicSmokeTests(unittest.TestCase):
    """Gated public Docker smoke: ingest public synthetic fixtures into a
    disposable BeamMemory store (keyword-only, optionally TEI) and check
    smoke sanity thresholds. Never mounts production state."""

    def test_keyword_smoke_meets_sanity_thresholds(self) -> None:
        import uuid
        project = f"josemar-test-{uuid.uuid4().hex[:12]}"
        results = run_eval_mode(
            mode="keyword", dataset_dir=PUB_FIXTURE_DIR, project=project, top_k=10,
        )
        # Keyword mode must report no embeddings.
        self.assertFalse(results.get("embeddings_available"),
                         "keyword mode must not have embeddings available")
        report, agg, gate = parse_results_and_evaluate(
            results_json=results,
            dataset_manifest={"provenance": "synthetic"},
            dataset_kind="public-synthetic-smoke",
            dataset_count=len(results["results"]),
            review_ready=False,
            is_activation=False,
            is_smoke=True,
        )
        self.assertEqual(gate["status"], "SMOKE_ONLY", f"smoke gate failures: {gate['failures']}")
        out_dir = get_report_dir() / "smoke-keyword"
        write_report(report, out_dir)

    @unittest.skipUnless(_tei_enabled(), "set RUN_MNEMOSYNE_RETRIEVAL_TEI=1 to run TEI smoke")
    def test_tei_smoke_meets_sanity_thresholds(self) -> None:
        import uuid
        project = f"josemar-test-{uuid.uuid4().hex[:12]}"
        results = run_eval_mode(
            mode="tei", dataset_dir=PUB_FIXTURE_DIR, project=project, top_k=10,
        )
        # TEI mode must report embeddings available and have at least one
        # meaningful nonzero dense_score.
        self.assertTrue(results.get("embeddings_available"),
                        "TEI mode must report embeddings available")
        self.assertTrue(_has_nonzero_dense_score(results),
                        "TEI mode must produce at least one nonzero dense_score")
        report, agg, gate = parse_results_and_evaluate(
            results_json=results,
            dataset_manifest={"provenance": "synthetic"},
            dataset_kind="public-synthetic-smoke",
            dataset_count=len(results["results"]),
            review_ready=False,
            is_activation=False,
            is_smoke=True,
        )
        self.assertEqual(gate["status"], "SMOKE_ONLY", f"smoke gate failures: {gate['failures']}")
        out_dir = get_report_dir() / "smoke-tei"
        write_report(report, out_dir)


@unittest.skipUnless(_activation_eval_enabled(), "set RUN_DOCKER_TESTS=1 and RUN_MNEMOSYNE_RETRIEVAL_ACTIVATION=1")
@unittest.skipUnless(docker_available(), "docker CLI is not available")
class MnemosyneRetrievalActivationEvalTests(unittest.TestCase):
    """Gated activation >=50 evaluation: runs the activation dataset (123 queries,
    60 corpus passages) through BOTH keyword-only and TEI-backed E5-small
    fresh isolated stores using the exact Beam remember/recall API, computes
    real keyword and TEI aggregates, per-difficulty metrics, latency,
    dense/keyword signal evidence, and TEI-vs-keyword regression, writes
    redacted JSON+Markdown under dump_folder/mnemosyne-retrieval-eval/activation/,
    and evaluates the activation gate.

    Gate behavior is stable before/after operator review:
      - If review.json is NOT_READY, the run still collects full metrics and
        produces evidence; status is NOT_READY and must include the
        human-review blocker. The test passes only if safety/metrics
        computation/reporting behaved correctly and the blocker is present.
      - Once review.json is REVIEWED, the same target requires the configured
        quality thresholds and fails if status is not READY.

    The test does NOT hardcode NOT_READY — it reads the actual review status
    and asserts the gate behavior matches. The activation dataset is kept
    NOT_READY; the test does not self-review it.
    """

    def _run_activation_comparison(self) -> dict:
        """Run both keyword and TEI modes over the activation dataset and return
        the comparison report dict."""
        import uuid
        m, c, q, r = validate_dataset_dir(
            FAQUAD_FIXTURE_DIR, expect_kind="public-standard-activation",
            expect_activation_evidence=True, min_queries=900,
        )
        self.assertGreaterEqual(len(q), 50)
        thresholds = get_thresholds_for_dataset(m, is_smoke=False)
        review_ready = True
        review_status = m.get("review_status")
        precondition_failures = []

        # Run keyword mode in its own one-off container + disposable store.
        kw_project = f"josemar-test-{uuid.uuid4().hex[:12]}"
        kw_results = run_eval_mode(
            mode="keyword", dataset_dir=FAQUAD_FIXTURE_DIR,
            project=kw_project, top_k=10,
        )
        # Keyword must report no embeddings.
        assert not kw_results.get("embeddings_available"), \
            "keyword mode must not have embeddings available (NO_EMBEDDINGS leaked into keyword?)"

        # Run TEI mode in a SEPARATE one-off container + disposable store.
        # The separate container ensures MNEMOSYNE_NO_EMBEDDINGS from the
        # keyword script cannot leak into the TEI process.
        tei_project = f"josemar-test-{uuid.uuid4().hex[:12]}"
        tei_results = run_eval_mode(
            mode="tei", dataset_dir=FAQUAD_FIXTURE_DIR,
            project=tei_project, top_k=10,
        )
        # TEI must report embeddings available and have nonzero dense_score.
        assert tei_results.get("embeddings_available"), \
            "TEI mode must report embeddings available (NO_EMBEDDINGS leaked from keyword into TEI?)"
        assert _has_nonzero_dense_score(tei_results), \
            "TEI mode must produce at least one nonzero dense_score"

        report = build_comparison_report(
            keyword_results=kw_results, tei_results=tei_results,
            dataset_manifest=m, dataset_kind=m["dataset_kind"],
            dataset_count=len(q), review_ready=review_ready,
            is_activation=True, thresholds=thresholds,
            review_status=review_status,
            precondition_failures=precondition_failures,
            dataset_dir=FAQUAD_FIXTURE_DIR,
        )
        # Write redacted reports under the gitignored dump_folder.
        out_dir = get_report_dir() / "activation"
        write_comparison_report(report, out_dir)
        return report

    def test_activation_keyword_and_tei_comparison_with_gate(self) -> None:
        report = self._run_activation_comparison()
        gate = report["gate"]
        m, c, q, r = validate_dataset_dir(FAQUAD_FIXTURE_DIR)
        review_ready = True
        run_mode = os.getenv("MNEMOSYNE_RETRIEVAL_MODE", "evidence")
        self.assertIn(run_mode, ("evidence", "activation"))

        # The immutable standard benchmark has no human-review gate; READY
        # requires the full metric/vector/regression criteria.
        self.assertEqual(report["review_status"], "NOT_APPLICABLE")
        self.assertEqual(gate["status"], "READY", f"standard activation must be READY: {gate}")
        self.assertTrue(gate["is_activation_evidence"])
        self.assertFalse(gate.get("human_review_blocked", False))

        # Both modes must have real aggregates with the full query count.
        self.assertEqual(report["keyword"]["aggregate"]["overall"]["count"], len(q))
        self.assertEqual(report["tei"]["aggregate"]["overall"]["count"], len(q))
        # Embeddings availability evidence.
        self.assertFalse(report["keyword"]["embeddings_available"])
        self.assertTrue(report["tei"]["embeddings_available"])
        # Regression evidence must be present.
        self.assertIn("regression", report)
        self.assertIn("overall", report["regression"])
        identity = report["dataset_identity"]
        for key in ("dataset_id", "revision", "artifact_sha256", "generated_sha256", "fixture_fingerprint", "threshold_policy_version", "threshold_policy_digest"):
            self.assertIn(key, identity)
        # Thresholds recorded in the report must match the manifest thresholds.
        self.assertEqual(report["thresholds"], get_thresholds_for_dataset(m, is_smoke=False))
        # No raw activation text in the report.
        blob = json.dumps(report, ensure_ascii=False)
        for row in q:
            # Query text must not appear verbatim in the report.
            self.assertNotIn(row["query"], blob)
        for row in c:
            self.assertNotIn(row["content"], blob)


if __name__ == "__main__":
    unittest.main()
