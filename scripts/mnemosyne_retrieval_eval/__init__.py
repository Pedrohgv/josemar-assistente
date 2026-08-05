"""Mnemosyne Portuguese vector retrieval quality harness (Phase 2).

Stdlib-only host code. Ingests a labeled PT-BR corpus into a disposable
Mnemosyne/Beam store, queries via ``BeamMemory.recall(query, top_k=...)``,
and computes Recall@1/@3/@5, MRR, nDCG@5, difficulty slices, latency
percentiles, and per-query ranked details/signal scores. Reports JSON and
concise Markdown under a gitignored output dir. Activation raw text is redacted
by default in reports (IDs/metrics only).

This package contains NO model download and NO Docker runtime. The Docker
runtime is driven by the test module ``tests/runtime/test_mnemosyne_retrieval_quality.py``
which imports the metric and schema helpers here for fast unit tests.
"""

from __future__ import annotations

from .metrics import (
    recall_at_k,
    mrr,
    ndcg_at_k,
    difficulty_slices,
    latency_percentiles,
    evaluate_run,
    evaluate_query,
)
from .schema import (
    load_corpus,
    load_queries,
    load_qrels,
    load_manifest,
    validate_dataset_dir,
    is_activation_dataset,
    validated_standard_dataset_identity,
    is_review_ready,
    DatasetError,
    REVIEW_READY,
    REVIEW_NOT_READY,
    REVIEW_AI_DRAFTED,
    dataset_fingerprint,
    MIN_REVIEWED_QUERIES,
    MIN_REVIEWED_PER_SLICE,
)
from .report import (
    redact_activation_text,
    build_report,
    write_report,
    REPORT_DIR_NAME,
    CONTENT_MARKER_PREFIX,
)
from .gate import (
    evaluate_gate,
    merge_thresholds,
    PUBLIC_SMOKE_THRESHOLDS,
    STANDARD_ACTIVATION_THRESHOLDS,
    STANDARD_POLICY_VERSION,
    STANDARD_POLICY_DIGEST,
    ACTIVATION_THRESHOLDS,
    PUBLIC_SMOKE_THRESHOLD_KEYS,
    ACTIVATION_THRESHOLD_KEYS,
)
from .runner import (
    make_disposable_input,
    generate_incontainer_script,
    parse_results_and_evaluate,
    build_comparison_report,
    write_comparison_report,
    render_comparison_markdown,
    run_eval_mode,
    EvalRuntime,
    get_report_dir,
    get_thresholds_for_dataset,
    E5_MODEL_ID,
    E5_MODEL_REVISION,
    E5_MODEL_DIMENSIONS,
)

__all__ = [
    "recall_at_k",
    "mrr",
    "ndcg_at_k",
    "difficulty_slices",
    "latency_percentiles",
    "evaluate_run",
    "evaluate_query",
    "load_corpus",
    "load_queries",
    "load_qrels",
    "load_manifest",
    "validate_dataset_dir",
    "is_activation_dataset",
    "validated_standard_dataset_identity",
    "is_review_ready",
    "DatasetError",
    "REVIEW_READY",
    "REVIEW_NOT_READY",
    "REVIEW_AI_DRAFTED",
    "dataset_fingerprint",
    "MIN_REVIEWED_QUERIES",
    "MIN_REVIEWED_PER_SLICE",
    "redact_activation_text",
    "build_report",
    "write_report",
    "REPORT_DIR_NAME",
    "CONTENT_MARKER_PREFIX",
    "evaluate_gate",
    "merge_thresholds",
    "PUBLIC_SMOKE_THRESHOLDS",
    "STANDARD_ACTIVATION_THRESHOLDS",
    "STANDARD_POLICY_VERSION",
    "STANDARD_POLICY_DIGEST",
    "ACTIVATION_THRESHOLDS",
    "PUBLIC_SMOKE_THRESHOLD_KEYS",
    "ACTIVATION_THRESHOLD_KEYS",
    "make_disposable_input",
    "generate_incontainer_script",
    "parse_results_and_evaluate",
    "build_comparison_report",
    "write_comparison_report",
    "render_comparison_markdown",
    "run_eval_mode",
    "EvalRuntime",
    "get_report_dir",
    "get_thresholds_for_dataset",
    "E5_MODEL_ID",
    "E5_MODEL_REVISION",
    "E5_MODEL_DIMENSIONS",
]
