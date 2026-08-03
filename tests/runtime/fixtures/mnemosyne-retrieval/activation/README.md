# Mnemosyne Portuguese Retrieval — Public Synthetic Activation Fixture

This public fixture is a retained synthetic regression fixture for Mnemosyne retrieval quality checks; it is not the activation benchmark. It is synthetic, contains no personal information, and was not
derived from agent state. The small smoke fixture remains in the parent
directory; this fixture contains the full 60-corpus/123-query evaluation set.

## Status: NOT READY

The labels in `queries.jsonl` are **AI-drafted** and remain useful only for
regression checks. The manifest sets `activation_evidence: false`; this fixture
cannot provide activation evidence and no review is performed by the activation
target.

Use `make test-mnemosyne-retrieval-synthetic-regression` for the advisory
checks. Public activation uses the complete FaQuAD-IR standard fixture instead.

## Contents

- `manifest.json` — dataset metadata and advisory regression thresholds.
- `corpus.jsonl` — JSONL corpus passages (one JSON object per line).
- `queries.jsonl` — JSONL labeled queries (one JSON object per line).
- `review.json` — retained NOT_READY metadata for regression compatibility; it is not used for activation.

## Provenance

The current content is **synthetic-representative** operational Portuguese,
authored to exercise the assistant's domain (household, finance, health,
travel, work, food) **without** using real user content, vault notes, or
conversation excerpts. All entities are fabricated. There is no PII by
construction.

If an operator later replaces or augments this with content derived from
agent-state, that content MUST be scrubbed to synthetic paraphrase before
commit and MUST NOT include names, addresses, account numbers, or health
records of real persons. The public repo's boundary tests enforce that no
agent-state-derived content leaks into public fixtures, logs, or reports.

## Schema

Corpus object (one per line):
```json
{"id": "act-c-001", "content": "...", "source": "...", "scope": "global", "topic": "...", "notes": "..."}
```

Query object (one per line):
```json
{"id": "act-q-001", "query": "...", "expected_ids": ["act-c-001"], "difficulty": "easy|medium|hard", "notes": "...", "provenance": "..."}
```

`expected_ids` may list one or more corpus IDs that a correct retrieval should
rank highly. `difficulty` drives the difficulty-slice metrics in the report.

## Regression Use

This fixture remains a public, synthetic, advisory regression set. Its labels are
not human-attested and it cannot provide activation evidence. Use:

```bash
make test-mnemosyne-retrieval-synthetic-regression
```

The activation target uses the complete public FaQuAD-IR standard fixture under
`../faquad-ir/`; that immutable benchmark has its own source hashes, qrels,
license, attribution, and no `review.json` requirement.

## Privacy

This directory is a tracked public repository fixture. It is synthetic, contains
no personal information, and is never copied into `agent-state`.
