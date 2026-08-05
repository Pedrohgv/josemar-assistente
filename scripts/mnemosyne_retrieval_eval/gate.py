"""Activation gate policy for the Mnemosyne Portuguese retrieval quality gate.

Defines the default thresholds for the activation gate and the lower
public smoke sanity thresholds. The authoritative threshold source for a
activation run is the ``thresholds`` object in the dataset manifest
(``tests/runtime/fixtures/mnemosyne-retrieval/activation/manifest.json``); the constants here
are defaults used only when the manifest does not provide them, and for the
public smoke sanity gate (which is never activation evidence).

The gate is the single source of truth for READY / NOT_READY / SMOKE-ONLY
decisions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

# --- Public smoke sanity thresholds (NOT activation evidence) ---
# Deliberately low so the harness wiring, schema, and E5 prefix behavior can
# be validated against the tiny synthetic public fixtures without
# masquerading as activation evidence. These are defaults only; the public
# smoke manifest may override them via its own ``thresholds`` object.
PUBLIC_SMOKE_THRESHOLDS: Dict[str, float] = {
    "min_queries": 5,
    "min_recall_at_1": 0.10,
    "min_recall_at_3": 0.20,
    "min_mrr": 0.15,
}

STANDARD_ACTIVATION_THRESHOLDS: Dict[str, float] = {
    "min_queries": 900,
    "min_recall_at_1": 0.35,
    "min_recall_at_3": 0.55,
    "min_mrr": 0.45,
    "min_ndcg_at_10": 0.55,
    "min_macro_recall_at_3": 0.20,
    "max_regression_vs_keyword_recall_at_3": 0.10,
}
STANDARD_POLICY_VERSION = "faquad-ir-v1"
STANDARD_POLICY_DIGEST = "49b1b984a767082a7fb61131790da1239e35af404a8ccec8d136858c1fc9030e"

# --- Activation gate default thresholds ---
# These are the floors a TEI-backed E5-small run must clear to be considered
# READY. The authoritative thresholds for a activation run come from the
# manifest ``thresholds`` object; these constants are the defaults used when
# the manifest omits a key (and for unit tests that construct synthetic
# aggregates without a manifest).
ACTIVATION_THRESHOLDS: Dict[str, float] = {
    "min_queries": 50,
    "min_recall_at_1": 0.55,
    "min_recall_at_3": 0.75,
    "min_mrr": 0.65,
    "min_ndcg_at_10": 0.0,
    "min_macro_recall_at_3": 0.0,
    # Difficulty slice floors (Recall@3 per slice).
    "min_slice_easy_recall_at_3": 0.80,
    "min_slice_medium_recall_at_3": 0.70,
    "min_slice_hard_recall_at_3": 0.55,
    # No material regression vs keyword-only: TEI Recall@3 may be up to this
    # much BELOW keyword Recall@3 (absolute tolerance). The policy allows TEI
    # to be slightly worse than keyword, not requires it to be better. When
    # keyword is very low this is easy; when keyword is already high the TEI
    # run must not regress below it by more than this tolerance.
    "max_regression_vs_keyword_recall_at_3": 0.05,
}

# All threshold keys the gate knows how to evaluate. Used for manifest schema
# validation and for merging manifest thresholds over defaults.
ACTIVATION_THRESHOLD_KEYS = (
    "min_queries",
    "min_recall_at_1",
    "min_recall_at_3",
    "min_mrr",
    "min_ndcg_at_10",
    "min_macro_recall_at_3",
    "min_slice_easy_recall_at_3",
    "min_slice_medium_recall_at_3",
    "min_slice_hard_recall_at_3",
    "max_regression_vs_keyword_recall_at_3",
)
PUBLIC_SMOKE_THRESHOLD_KEYS = (
    "min_queries",
    "min_recall_at_1",
    "min_recall_at_3",
    "min_mrr",
)
STANDARD_ACTIVATION_THRESHOLD_KEYS = tuple(STANDARD_ACTIVATION_THRESHOLDS)


def merge_thresholds(defaults: Dict, overrides: Dict | None) -> Dict:
    """Return a copy of ``defaults`` with any ``overrides`` applied.

    Only keys already present in ``defaults`` are accepted; unknown keys raise
    ValueError so manifest typos are caught early.
    """
    out = dict(defaults)
    if not overrides:
        return out
    if not isinstance(overrides, dict):
        raise ValueError(f"thresholds overrides must be a dict, got {type(overrides).__name__}")
    for k, v in overrides.items():
        if k not in defaults:
            raise ValueError(
                f"unknown threshold key {k!r}; known keys: {sorted(defaults.keys())}"
            )
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"threshold {k!r} must be a number, got {type(v).__name__}")
        out[k] = v
    return out


def evaluate_gate(
    *,
    aggregate: Dict,
    dataset_count: int,
    review_ready: bool,
    thresholds: Dict,
    is_activation: bool,
    is_smoke: bool,
    keyword_aggregate: Dict | None = None,
    embeddings_available: bool = False,
    dense_signal: float = 0.0,
    review_status: str | None = None,
    precondition_failures: List[str] | None = None,
    standard_dataset_dir: Path | str | None = None,
) -> Dict:
    """Evaluate the activation gate against an aggregate run.

    Returns a dict with status (READY / NOT_READY / SMOKE_ONLY / BLOCKED) and
    a list of failing checks with rationale.

    Standard activation (`review_status == "NOT_APPLICABLE"`) is fail-closed:
    it is authorized only by a full, independent validation of the standard
    fixture directory performed inside this function. Callers cannot supply a
    forged identity dict, and the code-pinned ``STANDARD_ACTIVATION_THRESHOLDS``
    are always used (any caller-provided ``thresholds`` are ignored for the
    standard path).
    """
    overall = aggregate.get("overall", {})
    slices = aggregate.get("difficulty_slices", {})
    failures: List[str] = []
    status = "NOT_READY"
    standard_identity: Dict | None = None
    vector_evidence = {
        "embeddings_available": bool(embeddings_available),
        "dense_signal": dense_signal if isinstance(dense_signal, (int, float)) else 0.0,
    }

    if is_smoke:
        # Public smoke: lower sanity thresholds, never READY.
        status = "SMOKE_ONLY"
        if dataset_count < int(thresholds.get("min_queries", 0)):
            failures.append(
                f"smoke dataset_count {dataset_count} < min_queries {int(thresholds.get('min_queries', 0))}"
            )
        if overall.get("recall@1", 0.0) < thresholds.get("min_recall_at_1", 0.0):
            failures.append(
                f"smoke recall@1 {overall['recall@1']:.4f} < {thresholds['min_recall_at_1']:.4f}"
            )
        if overall.get("recall@3", 0.0) < thresholds.get("min_recall_at_3", 0.0):
            failures.append(
                f"smoke recall@3 {overall['recall@3']:.4f} < {thresholds['min_recall_at_3']:.4f}"
            )
        if overall.get("mrr", 0.0) < thresholds.get("min_mrr", 0.0):
            failures.append(
                f"smoke mrr {overall['mrr']:.4f} < {thresholds['min_mrr']:.4f}"
            )
        return {
            "status": status,
            "is_activation_evidence": False,
            "failures": failures,
            "thresholds": thresholds,
            "rationale": (
                "Public smoke run. Sanity thresholds only. NOT activation evidence."
            ),
            "vector_evidence": vector_evidence,
        }

    if not is_activation:
        failures.append("activation gate requires a fixture with activation_evidence=true")
        return {
            "status": "BLOCKED",
            "is_activation_evidence": False,
            "failures": failures,
            "thresholds": thresholds,
            "rationale": "Fixture is not explicitly marked as activation evidence.",
            "vector_evidence": vector_evidence,
        }

    # Every activation request is standard activation. It must be authorized
    # by independently validating the actual FaQuAD fixture directory; review
    # status and caller-created identities are never routing signals.
    if standard_dataset_dir is None:
        return {
            "status": "BLOCKED",
            "is_activation_evidence": False,
            "failures": ["activation requires a dataset directory for authoritative validation"],
            "non_human_failures": ["activation requires a dataset directory for authoritative validation"],
            "thresholds": thresholds,
            "rationale": "Activation identity was not validated end-to-end.",
            "vector_evidence": vector_evidence,
            "standard_identity": None,
        }
    from .schema import validated_standard_dataset_identity
    try:
        standard_identity = validated_standard_dataset_identity(Path(standard_dataset_dir))
    except Exception as exc:
        return {
            "status": "BLOCKED",
            "is_activation_evidence": False,
            "failures": [f"activation identity validation failed: {exc}"],
            "non_human_failures": [f"activation identity validation failed: {exc}"],
            "thresholds": thresholds,
            "rationale": "Activation fixture failed full validation.",
            "vector_evidence": vector_evidence,
            "standard_identity": None,
        }
    # Standard activation uses ONLY the code-pinned threshold copy. Any
    # caller-provided or manifest weakened thresholds are ignored here.
    thresholds = STANDARD_ACTIVATION_THRESHOLDS

    # Activation gate.
    if dataset_count < int(thresholds.get("min_queries", 0)):
        failures.append(
            f"dataset_count {dataset_count} < min_queries {int(thresholds.get('min_queries', 0))}"
        )
    aggregate_count = overall.get("count")
    if isinstance(aggregate_count, int) and aggregate_count != dataset_count:
        failures.append(
            f"aggregate_count {aggregate_count} != dataset_count {dataset_count}"
        )
    # Structural/review metadata failures are non-human blockers.  In
    # particular, a file claiming REVIEWED but carrying a stale fingerprint
    # must not be reported as merely awaiting human review.
    if precondition_failures:
        failures.extend(precondition_failures)
    human_review_blocked = not review_ready and review_status not in ("REVIEWED", "NOT_APPLICABLE")
    if human_review_blocked:
        failures.append(
            "review_status is not REVIEWED; AI-drafted labels cannot activate the gate "
            "(human-review blocker: operator must review labels and set review.json)"
        )
    if not embeddings_available:
        failures.append("TEI vector evidence missing: embeddings_available must be true")
    if not isinstance(dense_signal, (int, float)) or dense_signal <= 0.0:
        failures.append("TEI vector evidence missing: dense signal must be meaningful and nonzero")
    if overall.get("recall@1", 0.0) < thresholds.get("min_recall_at_1", 0.0):
        failures.append(
            f"recall@1 {overall['recall@1']:.4f} < {thresholds['min_recall_at_1']:.4f}"
        )
    if overall.get("recall@3", 0.0) < thresholds.get("min_recall_at_3", 0.0):
        failures.append(
            f"recall@3 {overall['recall@3']:.4f} < {thresholds['min_recall_at_3']:.4f}"
        )
    if overall.get("mrr", 0.0) < thresholds.get("min_mrr", 0.0):
        failures.append(
            f"mrr {overall['mrr']:.4f} < {thresholds['min_mrr']:.4f}"
        )
    if overall.get("ndcg@10", 0.0) < thresholds.get("min_ndcg_at_10", 0.0):
        failures.append(f"nDCG@10 {overall.get('ndcg@10', 0.0):.4f} < {thresholds['min_ndcg_at_10']:.4f}")
    macro = aggregate.get("macro_by_relevant_passage", {})
    if macro.get("recall@3", 0.0) < thresholds.get("min_macro_recall_at_3", 0.0):
        failures.append(f"macro relevant-passage recall@3 {macro.get('recall@3', 0.0):.4f} < {thresholds['min_macro_recall_at_3']:.4f}")
    # Difficulty slice floors.
    for diff, key in (
        ("easy", "min_slice_easy_recall_at_3"),
        ("medium", "min_slice_medium_recall_at_3"),
        ("hard", "min_slice_hard_recall_at_3"),
    ):
        sl = slices.get(diff)
        if key not in thresholds:
            continue
        floor = thresholds[key]
        if sl is None:
            failures.append(f"difficulty slice '{diff}' missing")
        elif sl.get("recall@3", 0.0) < floor:
            failures.append(
                f"slice '{diff}' recall@3 {sl['recall@3']:.4f} < {floor:.4f}"
            )
    # Regression vs keyword: TEI Recall@3 may be up to max_reg BELOW keyword
    # Recall@3. The policy allows TEI to be slightly worse, not requires it to
    # be better by this margin.
    if keyword_aggregate is not None:
        kw_r3 = keyword_aggregate.get("overall", {}).get("recall@3", 0.0)
        tei_r3 = overall.get("recall@3", 0.0)
        max_reg = thresholds.get("max_regression_vs_keyword_recall_at_3", 0.0)
        if tei_r3 < kw_r3 - max_reg:
            failures.append(
                f"TEI recall@3 {tei_r3:.4f} regresses vs keyword recall@3 {kw_r3:.4f} "
                f"by more than the allowed tolerance {max_reg:.4f} "
                f"(TEI is {kw_r3 - tei_r3:.4f} below keyword; allowed up to {max_reg:.4f} below)"
            )

    status = "READY" if not failures else "NOT_READY"
    non_human_failures = [
        failure for failure in failures
        if "human-review blocker" not in failure
    ]
    result = {
        "status": status,
        "is_activation_evidence": status == "READY",
        "failures": failures,
        "non_human_failures": non_human_failures,
        "human_review_blocked": human_review_blocked,
        "thresholds": thresholds,
        "vector_evidence": vector_evidence,
    }
    if is_activation and not is_smoke:
        result["standard_identity"] = standard_identity
    result.update({
        "rationale": (
            "Activation gate. Requires the declared dataset provenance, "
            "TEI-backed E5-small vector evidence, query-micro and "
            "relevant-passage macro metric floors, and no material regression "
            "vs keyword-only (TEI Recall@3 may be up to "
            f"{thresholds.get('max_regression_vs_keyword_recall_at_3', 0.0):.4f} below keyword). "
            "Synthetic labels require review; standard benchmark fixtures do not."
        ),
    })
    return result
