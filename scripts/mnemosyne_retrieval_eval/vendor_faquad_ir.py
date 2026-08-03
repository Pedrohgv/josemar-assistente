"""Vendor the pinned MTEB-BR/faquad-ir Parquet release as JSONL.

This is a transformation tool, not a runtime dependency. Install pyarrow only
in a disposable environment under dump_folder when regenerating the fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from collections import defaultdict
from pathlib import Path

DATASET_ID = "MTEB-BR/faquad-ir"
REVISION = "c081a26d706764f1d09de17792f5eb995f51b124"
BASE_URL = f"https://huggingface.co/datasets/{DATASET_ID}/resolve/{REVISION}"
ARTIFACTS = {
    "corpus": "corpus/test-00000-of-00001.parquet",
    "queries": "queries/test-00000-of-00001.parquet",
    "qrels": "qrels/test-00000-of-00001.parquet",
}
EXPECTED = {"corpus": 244, "queries": 900, "qrels": 900}
EXPECTED_SOURCE_SHA256 = {
    "corpus": "ffcef36585e9ccdebc6f054b92a8692b34d222c45f6d0bfc0e46481b329435bd",
    "queries": "03c5d5907f6d1b75551e761f30d8ee7ad8c05d3ce45593141bf6b037c5ecd053",
    "qrels": "546b931a4f4ade81a17ce1181540560c5b2d7b67b42f0c8d4ae974b1d613a768",
}
EXPECTED_GENERATED_SHA256 = {
    "corpus_jsonl": "80a62cefe1a654c8aa3385708b330737081b8be0c01fe207af4b114f41493f91",
    "queries_jsonl": "d2edcb8f1fde3240246f7e5e15965229b8c708029f337373071086e48e072885",
    "qrels_jsonl": "aeaf4278f44c9fb2cfde9067d95a0f2b1919502332af62b6876992a3272bdfd5",
}
PYARROW_VERSION = "25.0.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/legalcode"
THRESHOLD_POLICY_VERSION = "faquad-ir-v1"
THRESHOLD_POLICY_DIGEST = "49b1b984a767082a7fb61131790da1239e35af404a8ccec8d136858c1fc9030e"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url + "?download=true") as response:
        destination.write_bytes(response.read())


def validate_source_artifacts(source_dir: Path) -> dict[str, str]:
    hashes = {}
    for name, relative in ARTIFACTS.items():
        path = source_dir / f"{name}.parquet"
        if not path.is_file():
            download(f"{BASE_URL}/{relative}", path)
        actual = sha256(path)
        if actual != EXPECTED_SOURCE_SHA256[name]:
            raise ValueError(f"{name}: source SHA-256 mismatch; expected immutable pinned artifact")
        hashes[name] = actual
    return hashes


def write_jsonl(path: Path, rows: list[dict]) -> str:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return sha256(path)


def transform(source_dir: Path, output_dir: Path, retrieval_date: str, license_text_path: Path) -> None:
    import pyarrow.parquet as parquet
    import pyarrow
    if pyarrow.__version__ != PYARROW_VERSION:
        raise RuntimeError(f"pyarrow {PYARROW_VERSION} is required, got {pyarrow.__version__}")

    output_dir.mkdir(parents=True, exist_ok=True)
    source_hashes = validate_source_artifacts(source_dir)
    tables = {}
    for name, relative in ARTIFACTS.items():
        path = source_dir / f"{name}.parquet"
        tables[name] = parquet.read_table(path).to_pylist()
        if len(tables[name]) != EXPECTED[name]:
            raise ValueError(f"{name}: expected {EXPECTED[name]} rows, got {len(tables[name])}")

    corpus = []
    corpus_ids = set()
    for row in tables["corpus"]:
        cid = str(row["_id"])
        title = str(row.get("title", ""))
        text = str(row["text"])
        if not cid or not text.strip() or cid in corpus_ids:
            raise ValueError(f"invalid corpus row: {row!r}")
        corpus_ids.add(cid)
        # This exactly matches embeddings-benchmark/mteb's BM25 document text:
        # "\n".join([doc.get("title", ""), doc["text"]]).
        corpus.append({"id": f"faquad-c:{cid}", "content": title + "\n" + text, "source": "MTEB-BR/faquad-ir", "scope": "global", "title": title})

    query_rows = {str(row["_id"]): str(row["text"]) for row in tables["queries"]}
    if len(query_rows) != EXPECTED["queries"] or any(not text.strip() for text in query_rows.values()):
        raise ValueError("queries must have 900 unique non-empty IDs/texts")
    qrels = defaultdict(list)
    qrel_rows = []
    for row in tables["qrels"]:
        qid, cid, score = str(row["query-id"]), str(row["corpus-id"]), int(row["score"])
        if qid not in query_rows or cid not in corpus_ids:
            raise ValueError(f"qrel references unknown ID: {row!r}")
        qrels[qid].append(f"faquad-c:{cid}")
        qrel_rows.append({"query_id": f"faquad-q:{qid}", "corpus_id": f"faquad-c:{cid}", "score": score})
    if len(qrel_rows) != EXPECTED["qrels"] or set(qrels) != set(query_rows) or any(len(v) == 0 for v in qrels.values()):
        raise ValueError("qrels must cover every query and preserve every positive judgment")

    queries = []
    for row in tables["queries"]:
        qid = str(row["_id"])
        # Source-only deterministic proxy; this is not a human difficulty label.
        words = len(query_rows[qid].split())
        difficulty = "easy" if words <= 5 else "medium" if words <= 10 else "hard"
        queries.append({"id": f"faquad-q:{qid}", "query": query_rows[qid], "expected_ids": qrels[qid], "difficulty": difficulty, "provenance": "MTEB-BR/faquad-ir pinned source; deterministic length proxy, not human difficulty"})

    generated_hashes = {
        "corpus_jsonl": write_jsonl(output_dir / "corpus.jsonl", corpus),
        "queries_jsonl": write_jsonl(output_dir / "queries.jsonl", queries),
        "qrels_jsonl": write_jsonl(output_dir / "qrels.jsonl", qrel_rows),
    }
    if generated_hashes != EXPECTED_GENERATED_SHA256:
        raise ValueError(f"generated JSONL hashes do not match pinned output: {generated_hashes}")
    manifest = {
        "schema_version": 2,
        "dataset_kind": "public-standard-activation",
        "language": "pt-BR",
        "provenance": "Official MTEB-BR/faquad-ir benchmark, converted deterministically from pinned Parquet artifacts.",
        "activation_evidence": True,
        "review_status": "NOT_APPLICABLE",
        "source": {
            "dataset_id": DATASET_ID,
            "revision": REVISION,
            "split": "test",
            "official_split_names": ["test"],
            "urls": {name: f"{BASE_URL}/{relative}" for name, relative in ARTIFACTS.items()},
            "license": "CC-BY-4.0",
            "attribution": "MTEB-BR/faquad-ir, repackaged from FaQuAD; source https://github.com/liafacom/faquad.",
            "citation": "Sayama, H. F., Araujo, A. V., & Fernandes, E. R. (2019). FaQuAD: Reading Comprehension Dataset in the Domain of Brazilian Higher Education. BRACIS 2019, 443-448. DOI: 10.1109/BRACIS.2019.00084.",
            "retrieved_at": retrieval_date,
            "artifact_sha256": source_hashes,
        },
        "counts": EXPECTED,
        "generated_sha256": generated_hashes,
        "threshold_policy_version": THRESHOLD_POLICY_VERSION,
        "threshold_policy_digest": THRESHOLD_POLICY_DIGEST,
        "qrels": {"positive_score_policy": "All 900 source judgments have score=1; every judgment is preserved in qrels.jsonl and expected_ids."},
        "difficulty_proxy": {"name": "query_word_count", "rule": "<=5 easy, 6-10 medium, >10 hard", "human_difficulty": False, "used_for_thresholds": False},
        "thresholds": {
            "min_queries": 900, "min_recall_at_1": 0.35, "min_recall_at_3": 0.55,
            "min_mrr": 0.45, "min_ndcg_at_10": 0.55, "min_macro_recall_at_3": 0.20,
            "max_regression_vs_keyword_recall_at_3": 0.10,
        },
        "notes": "Public standard activation evidence. No review.json is required; READY still requires all vector, metric, macro, and regression criteria.",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    legal_text = license_text_path.read_text(encoding="utf-8")
    (output_dir / "LICENSE.txt").write_text("CC BY 4.0 — FaQuAD-IR / MTEB-BR.\n\nOfficial license URL: " + LICENSE_URL + "\nThis fixture is a modified conversion: Parquet records were deterministically converted to JSONL and corpus content follows the canonical title + newline + text representation.\nSource: https://huggingface.co/datasets/MTEB-BR/faquad-ir\nRevision: " + REVISION + "\n\n--- Full CC BY 4.0 legal code ---\n\n" + legal_text, encoding="utf-8")
    (output_dir / "README.md").write_text(f"""# Public FaQuAD-IR Activation Fixture\n\nThis is the complete `MTEB-BR/faquad-ir` test split, deterministically converted from revision `{REVISION}`. It contains 244 corpus passages, 900 queries, and all 900 positive qrels.\n\nThe benchmark is licensed CC-BY-4.0. See `manifest.json` and `LICENSE.txt` for source URLs, hashes, attribution, and citation. Query difficulty values are a deterministic word-count proxy only, not human difficulty, and are not used for activation thresholds. No `review.json` is required for this official immutable benchmark.\n\nRegenerate with `scripts/mnemosyne_retrieval_eval/vendor_faquad_ir.py` using pinned `pyarrow==25.0.0` in a disposable environment under `dump_folder/`, an explicit retrieval date, and the downloaded CC-BY-4.0 legal code.\n""", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retrieval-date", required=True)
    parser.add_argument("--license-text", type=Path, required=True)
    args = parser.parse_args()
    transform(args.source_dir, args.output_dir, args.retrieval_date, args.license_text)
