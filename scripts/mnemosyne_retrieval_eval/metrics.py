"""Retrieval metrics for the Mnemosyne quality harness.

Stdlib only. Pure functions over ranked result IDs and expected IDs so the
math is unit-testable without Docker or the Mnemosyne package.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Sequence


def _rank_of(expected_id: str, ranked_ids: Sequence[str]) -> int | None:
    """1-indexed rank of expected_id in ranked_ids, or None if absent."""
    for i, rid in enumerate(ranked_ids):
        if rid == expected_id:
            return i + 1
    return None


def recall_at_k(expected_ids: Sequence[str], ranked_ids: Sequence[str], k: int) -> float:
    """Fraction of expected_ids that appear in the top-k of ranked_ids.

    Returns a float in [0.0, 1.0]. With a single expected_id this is the
    classic hit@k indicator (0.0 or 1.0).
    """
    if k <= 0:
        return 0.0
    if not expected_ids:
        return 0.0
    topk = set(ranked_ids[:k])
    hits = sum(1 for eid in expected_ids if eid in topk)
    return hits / len(expected_ids)


def mrr(expected_ids: Sequence[str], ranked_ids: Sequence[str]) -> float:
    """Reciprocal rank of the first relevant result (1/rank), 0.0 if absent."""
    for i, rid in enumerate(ranked_ids):
        if rid in expected_ids:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(expected_ids: Sequence[str], ranked_ids: Sequence[str], k: int) -> float:
    """Normalized Discounted Cumulative Gain at k with binary relevance.

    DCG = sum over i in 1..k of rel_i / log2(i + 1), where rel_i is 1 if the
    item at rank i is relevant else 0. IDCG is the DCG of the ideal ranking
    (all relevant items first), capped at min(k, len(expected_ids)) relevant
    items. nDCG = DCG / IDCG, or 0.0 if IDCG is 0.
    """
    if k <= 0 or not expected_ids:
        return 0.0
    rel_set = set(expected_ids)
    dcg = 0.0
    for i, rid in enumerate(ranked_ids[:k]):
        if rid in rel_set:
            dcg += 1.0 / math.log2(i + 2)  # i is 0-indexed, so rank = i+1, log2(rank+1)=log2(i+2)
    ideal_hits = min(len(expected_ids), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    return dcg / idcg if idcg > 0 else 0.0


def difficulty_slices(
    per_query: Sequence[Dict],
) -> Dict[str, Dict[str, float]]:
    """Aggregate metrics by difficulty slice.

    ``per_query`` is a list of dicts each containing at least 'difficulty',
    'recall@1', 'recall@3', 'recall@5', 'mrr', 'ndcg@5', 'ndcg@10'. Returns a dict keyed
    by difficulty with averaged metrics and a count.
    """
    buckets: Dict[str, List[Dict]] = {}
    for row in per_query:
        buckets.setdefault(row["difficulty"], []).append(row)
    out: Dict[str, Dict[str, float]] = {}
    for diff, rows in sorted(buckets.items()):
        n = len(rows)
        if n == 0:
            continue
        out[diff] = {
            "count": n,
            "recall@1": sum(r["recall@1"] for r in rows) / n,
            "recall@3": sum(r["recall@3"] for r in rows) / n,
            "recall@5": sum(r["recall@5"] for r in rows) / n,
            "mrr": sum(r["mrr"] for r in rows) / n,
            "ndcg@5": sum(r["ndcg@5"] for r in rows) / n,
            "ndcg@10": sum(r.get("ndcg@10", r["ndcg@5"]) for r in rows) / n,
        }
    return out


def latency_percentiles(latencies_ms: Sequence[float]) -> Dict[str, float]:
    """Compute p50/p90/p95/p99/max latency in ms using nearest-rank method."""
    if not latencies_ms:
        return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    s = sorted(latencies_ms)
    n = len(s)

    def pct(p: float) -> float:
        # nearest-rank
        idx = max(0, min(n - 1, math.ceil(p / 100.0 * n) - 1))
        return float(s[idx])

    return {
        "p50": pct(50),
        "p90": pct(90),
        "p95": pct(95),
        "p99": pct(99),
        "max": float(s[-1]),
        "mean": float(statistics.fmean(s)) if n else 0.0,
    }


def evaluate_query(
    expected_ids: Sequence[str],
    ranked_ids: Sequence[str],
) -> Dict[str, float]:
    """Compute the per-query metric bundle."""
    return {
        "recall@1": recall_at_k(expected_ids, ranked_ids, 1),
        "recall@3": recall_at_k(expected_ids, ranked_ids, 3),
        "recall@5": recall_at_k(expected_ids, ranked_ids, 5),
        "mrr": mrr(expected_ids, ranked_ids),
        "ndcg@5": ndcg_at_k(expected_ids, ranked_ids, 5),
        "ndcg@10": ndcg_at_k(expected_ids, ranked_ids, 10),
    }


def evaluate_run(
    per_query: Sequence[Dict],
    latencies_ms: Sequence[float],
) -> Dict:
    """Aggregate a full run: overall metrics, difficulty slices, latency.

    ``per_query`` rows must contain the evaluate_query() bundle plus
    'difficulty' and 'query_id'.
    """
    n = len(per_query)
    overall = {
        "count": n,
        "recall@1": sum(r["recall@1"] for r in per_query) / n if n else 0.0,
        "recall@3": sum(r["recall@3"] for r in per_query) / n if n else 0.0,
        "recall@5": sum(r["recall@5"] for r in per_query) / n if n else 0.0,
        "mrr": sum(r["mrr"] for r in per_query) / n if n else 0.0,
        "ndcg@5": sum(r["ndcg@5"] for r in per_query) / n if n else 0.0,
        "ndcg@10": sum(r.get("ndcg@10", r["ndcg@5"]) for r in per_query) / n if n else 0.0,
    }
    by_relevant = {}
    for row in per_query:
        for expected_id in row.get("expected_ids", []):
            by_relevant.setdefault(expected_id, []).append(row)
    macro = {}
    for key in ("recall@1", "recall@3", "recall@5", "mrr", "ndcg@5", "ndcg@10"):
        passage_means = [sum(row.get(key, row["ndcg@5"]) for row in rows) / len(rows) for rows in by_relevant.values()]
        macro[key] = sum(passage_means) / len(passage_means) if passage_means else 0.0
    overall["query_micro"] = dict(overall)
    return {
        "overall": overall,
        "macro_by_relevant_passage": macro,
        "difficulty_slices": difficulty_slices(per_query),
        "latency_ms": latency_percentiles(latencies_ms),
    }
