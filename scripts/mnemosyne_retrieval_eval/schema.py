"""Schema validation and dataset loading for the Mnemosyne retrieval harness.

Stdlib only. Validates the public synthetic fixtures and the activation eval
dataset against a stable JSONL schema. Enforces the public/activation boundary:
public fixtures must be synthetic and must not declare activation evidence;
activation datasets must declare provenance and a review status.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

# Review status constants.
REVIEW_READY = "REVIEWED"
REVIEW_NOT_READY = "NOT_READY"
REVIEW_AI_DRAFTED = "AI_DRAFTED"
MIN_REVIEWED_QUERIES = 50
MIN_REVIEWED_PER_SLICE = 10

# Difficulty values used across datasets.
DIFFICULTIES = ("easy", "medium", "hard")
FAQUAD_ARTIFACT_SHA256 = {
    "corpus": "ffcef36585e9ccdebc6f054b92a8692b34d222c45f6d0bfc0e46481b329435bd",
    "qrels": "546b931a4f4ade81a17ce1181540560c5b2d7b67b42f0c8d4ae974b1d613a768",
    "queries": "03c5d5907f6d1b75551e761f30d8ee7ad8c05d3ce45593141bf6b037c5ecd053",
}
FAQUAD_REVISION = "c081a26d706764f1d09de17792f5eb995f51b124"
FAQUAD_GENERATED_SHA256 = {
    "corpus_jsonl": "80a62cefe1a654c8aa3385708b330737081b8be0c01fe207af4b114f41493f91",
    "queries_jsonl": "d2edcb8f1fde3240246f7e5e15965229b8c708029f337373071086e48e072885",
    "qrels_jsonl": "aeaf4278f44c9fb2cfde9067d95a0f2b1919502332af62b6876992a3272bdfd5",
}
FAQUAD_ARTIFACT_PATHS = {
    "corpus": "corpus/test-00000-of-00001.parquet",
    "queries": "queries/test-00000-of-00001.parquet",
    "qrels": "qrels/test-00000-of-00001.parquet",
}


class DatasetError(ValueError):
    """Raised when a dataset fails schema or boundary validation."""


def _read_jsonl(path: Path) -> List[Dict]:
    if not path.is_file():
        raise DatasetError(f"missing file: {path}")
    out: List[Dict] = []
    seen_ids = set()
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path}:{lineno} invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise DatasetError(f"{path}:{lineno} not a JSON object")
            out.append(obj)
    return out


def _validate_corpus_row(row: Dict, lineno: int, path: Path) -> None:
    for key in ("id", "content", "source", "scope"):
        if key not in row:
            raise DatasetError(f"{path}:{lineno} missing required key '{key}' in corpus row")
        if not isinstance(row[key], str) or not row[key].strip():
            raise DatasetError(f"{path}:{lineno} corpus '{key}' must be a non-empty string")
    if not isinstance(row.get("notes", ""), str):
        raise DatasetError(f"{path}:{lineno} corpus 'notes' must be a string if present")


def _validate_query_row(row: Dict, lineno: int, path: Path) -> None:
    for key in ("id", "query", "expected_ids", "difficulty"):
        if key not in row:
            raise DatasetError(f"{path}:{lineno} missing required key '{key}' in query row")
    if not isinstance(row["id"], str) or not row["id"].strip():
        raise DatasetError(f"{path}:{lineno} query 'id' must be a non-empty string")
    if not isinstance(row["query"], str) or not row["query"].strip():
        raise DatasetError(f"{path}:{lineno} query 'query' must be a non-empty string")
    if not isinstance(row["expected_ids"], list) or not row["expected_ids"]:
        raise DatasetError(f"{path}:{lineno} 'expected_ids' must be a non-empty list")
    if not all(isinstance(x, str) and x.strip() for x in row["expected_ids"]):
        raise DatasetError(f"{path}:{lineno} 'expected_ids' must be a list of non-empty strings")
    if row["difficulty"] not in DIFFICULTIES:
        raise DatasetError(
            f"{path}:{lineno} 'difficulty' must be one of {DIFFICULTIES}, got {row['difficulty']!r}"
        )
    if not isinstance(row.get("notes", ""), str):
        raise DatasetError(f"{path}:{lineno} query 'notes' must be a string if present")
    if not isinstance(row.get("provenance", ""), str):
        raise DatasetError(f"{path}:{lineno} query 'provenance' must be a string if present")


def load_corpus(path: Path) -> List[Dict]:
    rows = _read_jsonl(path)
    if not rows:
        raise DatasetError(f"corpus is empty: {path}")
    seen = set()
    for i, row in enumerate(rows, 1):
        _validate_corpus_row(row, i, path)
        if row["id"] in seen:
            raise DatasetError(f"{path}:{i} duplicate corpus id {row['id']!r}")
        seen.add(row["id"])
    return rows


def load_queries(path: Path) -> List[Dict]:
    rows = _read_jsonl(path)
    if not rows:
        raise DatasetError(f"queries file is empty: {path}")
    seen = set()
    for i, row in enumerate(rows, 1):
        _validate_query_row(row, i, path)
        if row["id"] in seen:
            raise DatasetError(f"{path}:{i} duplicate query id {row['id']!r}")
        seen.add(row["id"])
    return rows


def load_qrels(path: Path) -> List[Dict]:
    rows = _read_jsonl(path)
    if not rows:
        raise DatasetError(f"qrels file is empty: {path}")
    for i, row in enumerate(rows, 1):
        for key in ("query_id", "corpus_id", "score"):
            if key not in row:
                raise DatasetError(f"{path}:{i} qrel missing required key '{key}'")
        if not isinstance(row["query_id"], str) or not isinstance(row["corpus_id"], str):
            raise DatasetError(f"{path}:{i} qrel IDs must be strings")
        if not isinstance(row["score"], (int, float)) or isinstance(row["score"], bool):
            raise DatasetError(f"{path}:{i} qrel score must be numeric")
    return rows


def load_manifest(path: Path) -> Dict:
    if not path.is_file():
        raise DatasetError(f"missing manifest: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"manifest invalid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise DatasetError("manifest must be a JSON object")
    for key in ("schema_version", "dataset_kind", "language", "provenance", "review_status"):
        if key not in manifest:
            raise DatasetError(f"manifest missing required key '{key}'")
    if not isinstance(manifest["schema_version"], int) or manifest["schema_version"] < 1:
        raise DatasetError("manifest schema_version must be a positive int")
    if not isinstance(manifest["provenance"], str) or not manifest["provenance"].strip():
        raise DatasetError("manifest provenance must be a non-empty string")
    if manifest["review_status"] not in (REVIEW_READY, REVIEW_NOT_READY, REVIEW_AI_DRAFTED, "NOT_APPLICABLE"):
        raise DatasetError(
            f"manifest review_status must be one of "
            f"{REVIEW_READY!r}/{REVIEW_NOT_READY!r}/{REVIEW_AI_DRAFTED!r}"
        )
    # Optional thresholds object: if present, must be a dict of string->number.
    # Key validation (against the known gate keys) is deferred to the gate
    # merge step so schema.py stays decoupled from gate.py key sets; here we
    # only enforce the structural shape.
    if "thresholds" in manifest:
        th = manifest["thresholds"]
        if not isinstance(th, dict):
            raise DatasetError("manifest 'thresholds' must be a JSON object")
        for k, v in th.items():
            if not isinstance(k, str):
                raise DatasetError(f"manifest 'thresholds' key must be a string, got {type(k).__name__}")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise DatasetError(
                    f"manifest 'thresholds' value for {k!r} must be a number, got {type(v).__name__}"
                )
    return manifest


def _load_review(review_path: Path) -> Dict:
    if not review_path.is_file():
        return {}
    try:
        rev = json.loads(review_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"review.json invalid JSON: {exc}") from exc
    if not isinstance(rev, dict):
        raise DatasetError("review.json must be a JSON object")
    return rev


def dataset_fingerprint(dataset_dir: Path, manifest: Dict | None = None) -> str:
    """Return the stable identity of the evaluated dataset.

    The hash deliberately excludes review.json and review-only manifest fields,
    while including the manifest policy and exact corpus/query bytes.
    """
    dataset_dir = Path(dataset_dir)
    manifest = manifest if manifest is not None else load_manifest(dataset_dir / "manifest.json")
    policy = {k: v for k, v in manifest.items()
              if k not in {"review_status", "review_status_detail", "review_path"}}
    h = hashlib.sha256()
    for name, value in (("manifest-policy", json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()),
                        ("corpus.jsonl", (dataset_dir / "corpus.jsonl").read_bytes()),
                        ("queries.jsonl", (dataset_dir / "queries.jsonl").read_bytes())):
        h.update(name.encode("utf-8") + b"\0" + str(len(value)).encode() + b"\0" + value)
    return h.hexdigest()


def _valid_review_timestamp(value: object) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _review_coverage_valid(review: Dict, queries: List[Dict]) -> bool:
    count = review.get("reviewed_query_count")
    slices = review.get("reviewed_slice_counts")
    if not isinstance(count, int) or isinstance(count, bool) or count < MIN_REVIEWED_QUERIES:
        return False
    if not isinstance(slices, dict) or set(slices) != set(DIFFICULTIES):
        return False
    available = {d: sum(q["difficulty"] == d for q in queries) for d in DIFFICULTIES}
    values = []
    for difficulty in DIFFICULTIES:
        value = slices[difficulty]
        if not isinstance(value, int) or isinstance(value, bool) or value < MIN_REVIEWED_PER_SLICE:
            return False
        if value > available[difficulty]:
            return False
        values.append(value)
    return sum(values) == count and count <= len(queries)


def validate_dataset_dir(
    dataset_dir: Path,
    *,
    expect_kind: str | None = None,
    expect_activation_evidence: bool | None = None,
    min_queries: int | None = None,
    require_review_ready: bool = False,
) -> Tuple[Dict, List[Dict], List[Dict], Dict]:
    """Validate a dataset directory end-to-end.

    Returns (manifest, corpus, queries, review). Raises DatasetError on any
    schema or boundary violation.
    """
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.is_dir():
        raise DatasetError(f"not a directory: {dataset_dir}")

    manifest = load_manifest(dataset_dir / "manifest.json")
    corpus = load_corpus(dataset_dir / "corpus.jsonl")
    queries = load_queries(dataset_dir / "queries.jsonl")
    review = _load_review(dataset_dir / "review.json")

    if expect_kind is not None and manifest["dataset_kind"] != expect_kind:
        raise DatasetError(
            f"expected dataset_kind={expect_kind!r}, got {manifest['dataset_kind']!r}"
        )

    if expect_activation_evidence is not None:
        if bool(manifest.get("activation_evidence", False)) != expect_activation_evidence:
            raise DatasetError(
                f"expected activation_evidence={expect_activation_evidence}, "
                f"got {manifest.get('activation_evidence')}"
            )

    if manifest["dataset_kind"] == "public-synthetic-activation":
        if manifest.get("activation_evidence") is not True:
            raise DatasetError("activation fixture must explicitly set activation_evidence=true")
        if "synthetic" not in manifest["provenance"].lower() or "no pii" not in str(manifest.get("pii_policy", "")).lower():
            raise DatasetError("activation fixture must declare synthetic, PII-safe provenance")

    if manifest["dataset_kind"] == "public-standard-activation":
        from .gate import STANDARD_ACTIVATION_THRESHOLDS, STANDARD_POLICY_DIGEST, STANDARD_POLICY_VERSION
        source = manifest.get("source")
        counts = manifest.get("counts")
        generated = manifest.get("generated_sha256")
        if manifest.get("activation_evidence") is not True or manifest.get("review_status") != "NOT_APPLICABLE":
            raise DatasetError("standard activation requires activation_evidence=true and review_status=NOT_APPLICABLE")
        if not isinstance(source, dict) or source.get("dataset_id") != "MTEB-BR/faquad-ir" or source.get("revision") != FAQUAD_REVISION:
            raise DatasetError("standard activation requires immutable FaQuAD-IR source metadata")
        if source.get("license") != "CC-BY-4.0" or source.get("artifact_sha256") != FAQUAD_ARTIFACT_SHA256:
            raise DatasetError("standard activation requires CC-BY-4.0 and source artifact hashes")
        expected_urls = {k: f"https://huggingface.co/datasets/MTEB-BR/faquad-ir/resolve/{FAQUAD_REVISION}/{v}" for k, v in FAQUAD_ARTIFACT_PATHS.items()}
        if source.get("urls") != expected_urls or not source.get("attribution") or not source.get("citation"):
            raise DatasetError("standard activation requires source URLs, attribution, and citation")
        if manifest.get("threshold_policy_version") != STANDARD_POLICY_VERSION or manifest.get("threshold_policy_digest") != STANDARD_POLICY_DIGEST:
            raise DatasetError("standard activation threshold policy does not match the code-pinned policy")
        if manifest.get("thresholds") != STANDARD_ACTIVATION_THRESHOLDS:
            raise DatasetError("standard activation thresholds must exactly match the code-pinned policy")
        if counts != {"corpus": 244, "queries": 900, "qrels": 900} or not isinstance(generated, dict) or generated != FAQUAD_GENERATED_SHA256:
            raise DatasetError("standard activation requires canonical 244/900/900 counts and generated hashes")
        qrels = load_qrels(dataset_dir / "qrels.jsonl")
        if len(corpus) != 244 or len(queries) != 900 or len(qrels) != 900:
            raise DatasetError("FaQuAD-IR standard fixture counts must be exactly 244/900/900")
        if any(not row["id"].startswith("faquad-c:") for row in corpus) or any(not row["id"].startswith("faquad-q:") for row in queries):
            raise DatasetError("standard fixture IDs must use faquad namespaces")
        if any(not isinstance(row.get("title"), str) or not row["content"].startswith(row["title"] + "\n") for row in corpus):
            raise DatasetError("standard corpus content must be canonical title + newline + text")
        if any(r["score"] != 1 for r in qrels):
            raise DatasetError("standard fixture must preserve the source score=1 positive judgments")
        qrel_pairs = {(r["query_id"], r["corpus_id"]) for r in qrels}
        expected_pairs = {(q["id"], eid) for q in queries for eid in q["expected_ids"]}
        if qrel_pairs != expected_pairs:
            raise DatasetError("queries expected_ids must preserve every qrel pair exactly")
        for name in ("corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
            actual = hashlib.sha256((dataset_dir / name).read_bytes()).hexdigest()
            key = name.replace(".jsonl", "_jsonl")
            if generated.get(key) != actual:
                raise DatasetError(f"generated hash mismatch for {name}")

    # Cross-reference: every expected_id must exist in the corpus.
    corpus_ids = {row["id"] for row in corpus}
    for q in queries:
        for eid in q["expected_ids"]:
            if eid not in corpus_ids:
                raise DatasetError(
                    f"query {q['id']!r} expected_ids references unknown corpus id {eid!r}"
                )

    if min_queries is not None and len(queries) < min_queries:
        raise DatasetError(
            f"dataset has {len(queries)} queries, minimum required is {min_queries}"
        )

    if require_review_ready:
        status = review.get("review_status", manifest.get("review_status", REVIEW_NOT_READY))
        if status != REVIEW_READY:
            raise DatasetError(
                f"review_status is {status!r}; activation gate requires {REVIEW_READY!r}"
            )
        if not isinstance(review.get("reviewer"), str) or not review.get("reviewer", "").strip():
            raise DatasetError("review.json reviewer must be a non-empty string when READY")
        if not _valid_review_timestamp(review.get("reviewed_at")):
            raise DatasetError("review.json reviewed_at must be a timezone-aware ISO-8601 timestamp when READY")
        if not isinstance(review.get("review_method"), str) or not review.get("review_method", "").strip():
            raise DatasetError("review.json review_method must be non-empty when READY")
        if review.get("dataset_fingerprint") != dataset_fingerprint(dataset_dir, manifest):
            raise DatasetError("review.json dataset_fingerprint does not match the evaluated dataset")
        if not _review_coverage_valid(review, queries):
            raise DatasetError("review.json reviewed_query_count/slice counts do not meet the >=50 and >=10-per-slice rule")

    return manifest, corpus, queries, review


def is_review_ready(review: Dict, manifest: Dict, dataset_dir: Path | None = None,
                    queries: List[Dict] | None = None) -> bool:
    """True iff the dataset is operator-reviewed and ready for the activation gate.

    Checks review.json first (authoritative), falling back to the manifest
    review_status. READY requires a non-empty reviewer and reviewed_at.
    """
    status = review.get("review_status", manifest.get("review_status", REVIEW_NOT_READY))
    if status != REVIEW_READY:
        return False
    if not isinstance(review.get("reviewer"), str) or not review.get("reviewer", "").strip():
        return False
    if not _valid_review_timestamp(review.get("reviewed_at")):
        return False
    if not isinstance(review.get("review_method"), str) or not review.get("review_method", "").strip():
        return False
    if dataset_dir is None:
        return False
    try:
        expected = dataset_fingerprint(dataset_dir, manifest)
        queries = queries if queries is not None else load_queries(Path(dataset_dir) / "queries.jsonl")
        return review.get("dataset_fingerprint") == expected and _review_coverage_valid(review, queries)
    except (DatasetError, OSError):
        return False


def is_activation_dataset(dataset_dir: Path) -> bool:
    """Return whether a fixture is eligible public activation evidence."""
    try:
        manifest = load_manifest(Path(dataset_dir) / "manifest.json")
        return manifest.get("dataset_kind") == "public-standard-activation" and manifest.get("activation_evidence") is True
    except (DatasetError, OSError):
        return False


def validated_standard_dataset_identity(dataset_dir: Path) -> Dict:
    """Validate the complete standard fixture and return its immutable identity."""
    from .gate import STANDARD_POLICY_DIGEST, STANDARD_POLICY_VERSION
    manifest, corpus, queries, _ = validate_dataset_dir(
        Path(dataset_dir), expect_kind="public-standard-activation",
        expect_activation_evidence=True, min_queries=900,
    )
    return {
        "validated_standard": True,
        "dataset_id": manifest["source"]["dataset_id"],
        "revision": manifest["source"]["revision"],
        "artifact_sha256": dict(manifest["source"]["artifact_sha256"]),
        "generated_sha256": dict(manifest["generated_sha256"]),
        "fixture_fingerprint": dataset_fingerprint(Path(dataset_dir), manifest),
        "threshold_policy_version": STANDARD_POLICY_VERSION,
        "threshold_policy_digest": STANDARD_POLICY_DIGEST,
        "corpus_count": len(corpus),
        "query_count": len(queries),
    }
