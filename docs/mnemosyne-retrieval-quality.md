# Mnemosyne Portuguese Retrieval Quality Gate (Phase 3)

This document defines the explicit, configurable activation gate for the
Mnemosyne Portuguese vector retrieval quality evaluation. It is the
authoritative source for the threshold policy. The implementation lives in
`scripts/mnemosyne_retrieval_eval/` and the tests in
`tests/runtime/test_mnemosyne_retrieval_quality.py`.

> **Status:** Evaluated only. The gate is implemented and the public smoke
> harness is available. The FaQuAD-IR standard benchmark is the activation
> target; the retained synthetic fixture is advisory only. Nothing here
> activates Mnemosyne in production.

## Language Scope: the pt-BR Validated Baseline

The pinned `intfloat/multilingual-e5-small` tuple (see
[E5 Prefix Behavior](#e5-prefix-behavior)) and the synthetic activation
fixtures/tests in this repository are a **validated baseline only when
Brazilian Portuguese (pt-BR) is the system's intended primary language**. They
are **not** a general quality guarantee for arbitrary languages: the
thresholds, labels, and prefix assumptions were authored and validated for
pt-BR.

This is not a claim that the model cannot support other languages — E5-small is
multilingual. It simply means each language needs **its own evidence**. For
another intended primary language, operators should:

1. **Select and evaluate a suitable model** for that language (see
   `docs/memory-embeddings-evaluation.md` for the migration-tuple model).
2. **Create representative, language-specific synthetic fixtures/labels** and
   run the same retrieval quality gate
   (`make test-mnemosyne-retrieval-activation`).
3. **Treat any model/prefix/dimension change as a new migration tuple**:
   changing any element requires fresh vectors and a reindex — never reuse the
   pt-BR scores as evidence for a different language or tuple.

## Scope and Boundary

- **Public repo** contains **synthetic PT-BR fixtures only**
  (`tests/runtime/fixtures/mnemosyne-retrieval/`), with schema validation and
  no PII / no real user content. The parent directory is the small smoke fixture; `activation/` is the full
  public activation fixture.
- **Synthetic regression fixture** (`tests/runtime/fixtures/mnemosyne-retrieval/activation/`)
  remains public and advisory. It has `activation_evidence: false` and cannot
  provide activation evidence.
- **FaQuAD-IR standard activation fixture**
  (`tests/runtime/fixtures/mnemosyne-retrieval/faquad-ir/`) is the complete
  immutable MTEB-BR test split: 244 corpus passages, 900 queries, and all 900
  positive qrels. It has `activation_evidence: true`; no human review file is
  required, but it becomes activation evidence only when the full gate is
  `READY`.
- The harness uses the exact pinned API: ingest via actual Mnemosyne/Beam
  into a disposable data dir, embed a stable marker in content and retain
  returned/generated IDs, query with `BeamMemory.recall(query, top_k=...)`
  (raw ranked dicts) rather than parsing `provider.prefetch` rendered text.

## Metrics

The harness computes, per mode (keyword-only and TEI-backed E5-small):

- **Recall@1 / @3 / @5** — fraction of `expected_ids` in the top-k ranked
  results.
- **MRR** — reciprocal rank of the first relevant result.
- **nDCG@5** — normalized discounted cumulative gain with binary relevance.
- **nDCG@10** — the activation ranking metric at the benchmark's top-k.
- **Query-micro metrics** — averages over all queries.
- **Relevant-passage macro metrics** — averages query metrics per relevant
  corpus passage before averaging passages, preventing query-heavy passages
  from dominating the result.
- **Difficulty slices** — retained only as a deterministic source-only proxy
  for FaQuAD-IR and not used as human difficulty or activation thresholds.
- **Latency percentiles** — p50 / p90 / p95 / p99 / max / mean (ms).
- **Per-query ranked details / signal scores** — `score`, `keyword_score`,
  `dense_score`, `tier` from the recall result dicts.

Reports are written as JSON + concise Markdown under the gitignored
`dump_folder/mnemosyne-retrieval-eval/` directory. Activation raw text is
redacted/omitted by default — reports carry IDs and metrics only.

## E5 Prefix Behavior

TEI uses the existing pinned tuple and overlay network:
`intfloat/multilingual-e5-small` at revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3` (384 dims) with the exact
client-side `query: ` / `passage: ` prefixes and API URL `/v1`.

The harness **does not** add query/passage prefixes. The Mnemosyne embeddings
module applies them itself from the env vars (`MNEMOSYNE_EMBEDDING_QUERY_PREFIX`
/ `MNEMOSYNE_EMBEDDING_DOC_PREFIX`), so the harness avoids double-prefixing.
The public fixture includes a dedicated `passage: `-prefixed query to detect
double-prefix bugs.

## Activation Gate Thresholds

### Public smoke sanity thresholds (NOT activation evidence)

Deliberately low so the harness wiring, schema, and E5 prefix behavior can be
validated against the tiny synthetic public fixtures without masquerading as
activation evidence.

| Threshold | Value |
|---|---|
| `min_queries` | 5 |
| `min_recall_at_1` | 0.10 |
| `min_recall_at_3` | 0.20 |
| `min_mrr` | 0.15 |

The smoke gate status is always `SMOKE_ONLY` and `is_activation_evidence` is
always `false`, regardless of metrics.

### FaQuAD-IR activation gate thresholds

These are the frozen floors a TEI-backed E5-small FaQuAD-IR run must clear to
be considered READY. The authoritative threshold source for the standard
fixture is the
``thresholds`` object in the activation manifest
(``tests/runtime/fixtures/mnemosyne-retrieval/faquad-ir/manifest.json``). The activation report records the
exact thresholds used.

| Threshold | Default | Manifest value |
|---|---|---|
| `min_queries` | 900 | 900 |
| `min_recall_at_1` | 0.35 | 0.35 |
| `min_recall_at_3` | 0.55 | 0.55 |
| `min_mrr` | 0.45 | 0.45 |
| `min_ndcg_at_10` | 0.55 | 0.55 |
| `min_macro_recall_at_3` | 0.20 | 0.20 |
| `max_regression_vs_keyword_recall_at_3` | 0.10 | 0.10 |

Policy identity: `faquad-ir-v1`, digest
`49b1b984a767082a7fb61131790da1239e35af404a8ccec8d136858c1fc9030e`.
These floors were frozen before the final FaQuAD-IR run: 900 queries and all
900 qrels, vector evidence, query-micro and relevant-passage macro coverage,
nDCG@10, and a 0.10 absolute Recall@3 regression tolerance. The 0.10 value is
the single reconciled policy value; it permits a bounded TEI-vs-keyword gap but
does not require TEI to outperform keyword retrieval.

### Review metadata

The retained synthetic regression fixture has AI-drafted labels and remains
advisory; its `review.json` is not used by the FaQuAD-IR activation target.
The immutable standard benchmark has no human-review gate. Its gate requires
the source invariants and all vector, query-micro, relevant-passage macro,
and regression criteria, then reports `READY` only when all pass.

For the retained synthetic fixture only, review metadata would be set in
`tests/runtime/fixtures/mnemosyne-retrieval/activation/review.json`:

- `review_status` = `"REVIEWED"`
- `reviewer` = non-empty operator identifier
- `reviewed_at` = non-empty ISO date
- `review_method` = short description of how the review was performed
- `reviewed_at` = timezone-aware ISO-8601 timestamp
- `dataset_fingerprint` = fingerprint printed by the operator helper below
- `reviewed_query_count` >= 50
- `reviewed_slice_counts` has easy/medium/hard counts, each >= 10, summing to
  `reviewed_query_count` (a review of 30 is never enough)

Print the current identity and review instructions without printing activation
query or corpus text:

```bash
PYTHONPATH=scripts python3 -m mnemosyne_retrieval_eval.fingerprint tests/runtime/fixtures/mnemosyne-retrieval/activation
```

The fingerprint is SHA-256 over canonical manifest policy fields and the exact
`corpus.jsonl` and `queries.jsonl` bytes. It excludes `review.json` and review
status-only manifest fields.

The standard fixture uses the official `MTEB-BR/faquad-ir` test split at the
immutable revision recorded in its manifest. Its source and generated JSONL
hashes, license, attribution, and conversion rules are recorded beside the
fixture. The word-count query labels are source-only proxies, not human
difficulty, and are not used for activation thresholds.

### Standard activation gate

The activation command runs the complete 900-query FaQuAD-IR comparison in
keyword-only and TEI-backed modes. It has no human-review transition: source
invariants, vector evidence, query-micro and relevant-passage macro floors,
nDCG@10, and TEI-vs-keyword regression must all pass, and the gate must be
exactly `READY`. Standard activation is fail-closed: the emitted report
identity is derived only from a full, independent validation of the fixture
directory (counts, qrels, source and generated hashes, and the code-pinned
threshold policy digest), and only the code-pinned threshold copy is used.

## Gate Statuses

- `READY` — standard activation run cleared all floors. This is the only status
  with `is_activation_evidence = true`; TEI must also report
  `embeddings_available=true` and a meaningful nonzero dense signal.
- `NOT_READY` — activation run failed one or more floors.
- `SMOKE_ONLY` — public smoke run. Never activation evidence.
- `BLOCKED` — a fixture without explicit `activation_evidence: true` was
  passed to the activation gate.

## Test Tiers

1. **Fast unit/schema/boundary tests** — always run on ordinary discovery
   and pre-commit. No Docker, no model download. Enforces the public/activation
   boundary, report redaction, metric math, minimum dataset count, review
   metadata, and disposable-input cleanup.
2. **Gated public Docker smoke** — `RUN_DOCKER_TESTS=1` AND
   `RUN_MNEMOSYNE_RETRIEVAL_SMOKE=1`. Builds the isolated hermes image,
   ingests public synthetic fixtures, checks smoke thresholds. Optionally
   TEI-backed with `RUN_MNEMOSYNE_RETRIEVAL_TEI=1`.
3. **Gated public FaQuAD-IR activation** — `RUN_DOCKER_TESTS=1` AND
   `RUN_MNEMOSYNE_RETRIEVAL_ACTIVATION=1`. Runs 900 queries over 244 corpus
   passages through BOTH keyword-only AND TEI-backed E5-small fresh isolated
   stores, computes query-micro and relevant-passage macro metrics, nDCG@10,
   latency, vector evidence, and TEI-vs-keyword regression, writes redacted
   JSON+Markdown under `dump_folder/mnemosyne-retrieval-eval/activation/`,
   and passes only when the gate is exactly `READY`.

Model download / runtime is never on ordinary unittest discovery.

## Isolation Guarantees

- No production `hermes-data`, state sync, provider API, Telegram, or
  activation mounts inside services.
- Activation fixtures are copied only into a temporary disposable input created
  by the host test and removed reliably.
- The harness compares fresh isolated stores for keyword-only and TEI-backed
  modes (separate disposable BeamMemory data dirs per run).
- Cleanup uses `docker compose down -v --remove-orphans` via the shared
  `ComposeRuntime`.

## Makefile Targets

```bash
make test-mnemosyne-retrieval            # fast unit/schema/boundary
make test-mnemosyne-retrieval-smoke      # gated public keyword smoke
make test-mnemosyne-retrieval-tei-smoke  # gated public TEI smoke
make test-mnemosyne-retrieval-synthetic-regression # advisory synthetic checks
make test-mnemosyne-retrieval-activation           # public FaQuAD-IR; exactly READY
```

## Residual Limitations

- The TEI smoke requires the embeddings overlay + service and a network
  egress for the first model download. If unavailable, the test reports the
  precise environmental blocker; the unit/schema tests still pass.
- The activation thresholds are initial floors, not tuned benchmarks. They
  should be revisited after the first operator-reviewed activation run produces
  real numbers.
- The activation fixture is synthetic (no real user or agent-state-derived
  content). It must remain PII-safe.
