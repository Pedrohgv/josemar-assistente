"""Report building and writing for the Mnemosyne retrieval harness.

Stdlib only. Builds a JSON-serializable report dict and a concise Markdown
summary. Activation raw text is redacted by default: reports carry IDs and
metrics only, never the raw query or corpus content from a activation dataset.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List

REPORT_DIR_NAME = "mnemosyne-retrieval-eval"

# Marker embedded into every ingested corpus content so the harness can
# confirm the right store was queried and detect accidental cross-store
# leakage. Kept short and unique enough to grep for.
CONTENT_MARKER_PREFIX = "MNEMOSYNE_EVAL_MARKER"


def redact_activation_text(text: str | None) -> str:
    """Replace any raw text with a fixed placeholder.

    Reports must not include activation raw query/corpus text. Only IDs and
    metrics are emitted. This function is the single chokepoint the tests
    assert on.
    """
    if text is None:
        return ""
    return "[REDACTED-ACTIVATION]"


def build_report(
    *,
    mode: str,
    dataset_manifest: Dict,
    dataset_kind: str,
    dataset_count: int,
    review_ready: bool,
    per_query: List[Dict],
    aggregate: Dict,
    thresholds: Dict,
    gate_result: Dict,
    is_activation: bool,
) -> Dict:
    """Build the JSON-serializable report dict.

    For activation datasets, per_query rows carry only IDs, ranks, scores, and
    metrics — never raw query or corpus text. For public synthetic datasets,
    raw text is also omitted from per_query to keep reports uniform and small.
    """
    report = {
        "schema_version": 1,
        "mode": mode,
        "dataset_kind": dataset_kind,
        "dataset_count": dataset_count,
        "review_ready": review_ready,
        "is_activation": is_activation,
        "thresholds": thresholds,
        "gate": gate_result,
        "aggregate": aggregate,
        "per_query": [],
    }
    for row in per_query:
        entry = {
            "query_id": row["query_id"],
            "difficulty": row["difficulty"],
            "expected_ids": list(row.get("expected_ids", [])),
            "ranked_ids": list(row.get("ranked_ids", [])),
            "metrics": {
                "recall@1": row["recall@1"],
                "recall@3": row["recall@3"],
                "recall@5": row["recall@5"],
                "mrr": row["mrr"],
                "ndcg@5": row["ndcg@5"],
                "ndcg@10": row.get("ndcg@10", row["ndcg@5"]),
            },
            "latency_ms": row.get("latency_ms", 0.0),
        }
        # Signal scores from the recall result dicts (keyword_score,
        # dense_score, score, tier) if present. These are numeric and not
        # activation text.
        for sig in ("score", "keyword_score", "dense_score", "tier"):
            if sig in row:
                entry[sig] = row[sig]
        report["per_query"].append(entry)
    return report


def render_markdown(report: Dict) -> str:
    """Render a concise Markdown summary of the report."""
    agg = report["aggregate"]
    overall = agg.get("overall", {})
    lines: List[str] = []
    lines.append("# Mnemosyne Retrieval Quality Report")
    lines.append("")
    lines.append(f"- Mode: `{report['mode']}`")
    lines.append(f"- Dataset kind: `{report['dataset_kind']}`")
    lines.append(f"- Dataset count: {report['dataset_count']}")
    lines.append(f"- Activation dataset: `{report['is_activation']}`")
    lines.append(f"- Review ready: `{report['review_ready']}`")
    lines.append(f"- Gate: **{report['gate'].get('status', 'UNKNOWN')}**")
    lines.append("")
    lines.append("## Overall Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    for key in ("recall@1", "recall@3", "recall@5", "mrr", "ndcg@5"):
        lines.append(f"| {key} | {overall.get(key, 0.0):.4f} |")
    lines.append("")
    lat = agg.get("latency_ms", {})
    if lat:
        lines.append("## Latency (ms)")
        lines.append("")
        lines.append("| p50 | p90 | p95 | p99 | max | mean |")
        lines.append("|---|---|---|---|---|---|")
        lines.append(
            f"| {lat.get('p50',0):.1f} | {lat.get('p90',0):.1f} | "
            f"{lat.get('p95',0):.1f} | {lat.get('p99',0):.1f} | "
            f"{lat.get('max',0):.1f} | {lat.get('mean',0):.1f} |"
        )
        lines.append("")
    slices = agg.get("difficulty_slices", {})
    if slices:
        lines.append("## Difficulty Slices")
        lines.append("")
        lines.append("| Slice | Count | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@5 |")
        lines.append("|---|---|---|---|---|---|---|")
        for diff, m in sorted(slices.items()):
            lines.append(
                f"| {diff} | {m['count']} | {m['recall@1']:.4f} | {m['recall@3']:.4f} | "
                f"{m['recall@5']:.4f} | {m['mrr']:.4f} | {m['ndcg@5']:.4f} |"
            )
        lines.append("")
    lines.append("## Thresholds")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(report["thresholds"], indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append("## Gate")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(report["gate"], indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append(
        "_Raw activation text is redacted from this report. Per-query rows carry "
        "IDs, ranks, scores, and metrics only._"
    )
    return "\n".join(lines) + "\n"


def write_report(report: Dict, out_dir: Path) -> Dict[str, Path]:
    """Write report.json and report.md under out_dir. Returns the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    md_path = out_dir / "report.md"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
