# Memory & Embeddings Evaluation (Issues #86 / #65)

Operator evaluation of an optional local embedding service and the Hermes
Mnemosyne memory layer, plus the gbrain embedding prerequisites. This document
is an **evaluation and operator guide**, not an activation runbook: nothing
described here is enabled by default, and no production data was changed to
produce it.

> **Status:** Evaluated only. Neither Mnemosyne nor gbrain embeddings are
> enabled in this branch. The optional `docker-compose.embeddings.yml` overlay
> and the pinned gbrain E5 preprocessing patch are implemented prerequisites;
> activation is separate future work. See
> [Current Branch Scope and Status](#current-branch-scope-and-status).

## Contents

- [Why Evaluate This Now](#why-evaluate-this-now)
- [What Mnemosyne Provides — and What It Does Not](#what-mnemosyne-provides-and-what-it-does-not)
- [Accuracy Caveats and Upstream Risks](#accuracy-caveats-and-upstream-risks)
- [Embedding Model Selection](#embedding-model-selection)
- [Benchmark Evidence](#benchmark-evidence)
- [Shared Embedding Service and Queue Assessment](#shared-embedding-service-and-queue-assessment)
- [Service and Deployment Implications](#service-and-deployment-implications)
- [Current Branch Scope and Status](#current-branch-scope-and-status)
- [Recommended Staged Transition and Rollback](#recommended-staged-transition-and-rollback)
- [References](#references)

## Why Evaluate This Now

Josemar currently injects static context files (`memories/MEMORY.md`,
`memories/USER.md`) into every prompt regardless of relevance. As the vault and
conversation history grow, this bloats the prompt with material that is often
irrelevant to the current turn, costing tokens and diluting signal. Issues #86
and #65 ask whether a semantic memory layer (Mnemosyne) and gbrain vector search
can replace blanket injection with relevant recall.

This document captures the evaluation: what Mnemosyne is, the accuracy caveats
around a young upstream, which embedding model fits Josemar's Portuguese corpus
and N100 hardware, measured benchmark numbers, the shared-service/queue design,
and the deployment implications — without enabling anything.

## What Mnemosyne Provides — and What It Does Not

Mnemosyne is a Hermes-side memory layer that aims to give the assistant
relevant, operational recall instead of static, always-injected context.

**What it provides (per upstream design):**

- **Relevant semantic recall** instead of injecting the entirety of
  `MEMORY.md`/`USER.md` into every prompt. Only memory items that match the
  current turn are surfaced.
- **Tiered memory:** working memory (current task/turn), episodic memory
  (events/interactions), and a triple-store (entity-relation-entity facts).
- **Hybrid retrieval** combining lexical, vector, and importance scoring, so
  recall is not purely vector-dependent.
- **Consolidation and decay:** older/low-importance items are consolidated or
  decay out of the active working set, keeping the surfaced context focused.
- **Local SQLite store:** the memory database is a local file, not a hosted
  service.
- **Reduced prompt bloat and automatic operational memory:** the assistant
  accumulates and recalls operational memory without manual curation of a
  static file.

**What it does NOT do (important for planning):**

- **Does not replace curated gbrain/Obsidian knowledge.** Mnemosyne is
  operational/episodic memory; gbrain remains the canonical curated vault
  interface. They serve different purposes and must coexist.
- **Does not guarantee truth.** Consolidated/decayed memory can be wrong,
  stale, or hallucinated. It is recall, not a source of record.
- **Does not migrate static files automatically.** There is no documented
  `MEMORY.md`/`USER.md` → Mnemosyne migration path. Moving curated static
  context into the memory store is a manual, operator-driven step.
- **Does not replace vault freshness, Syncthing, or `josemar-gbrain refresh`.**
  Vault sync, Syncthing reconciliation, and the periodic no-embed refresh
  remain independent and required.
- **Does not remove embedding or LLM cost.** Embedding every turn and every
  consolidated memory item has a real CPU/latency cost (see benchmarks) and, if
  an LLM is used for consolidation, an LLM cost.
- **Does not provide independent benchmark certainty.** No standardized,
  third-party quality benchmark is shipped; quality must be validated locally
  (see the smoke-test caveat under [Benchmark Evidence](#benchmark-evidence)).
- **Does not remove backup or privacy needs.** The memory store contains
  user-derived data and must be backed up and privacy-treated like other
  runtime state (see [Service and Deployment Implications](#service-and-deployment-implications)).

**Coexistence caveat:** running Mnemosyne alongside static `MEMORY.md`/`USER.md`
injection can duplicate prompt content (the same fact surfaced both as static
text and as a recalled memory item). The recommended path is a staged
transition that gradually reduces static injection as Mnemosyne recall is
validated, with an explicit rollback to the static files (see
[Recommended Staged Transition and Rollback](#recommended-staged-transition-and-rollback)).

## Accuracy Caveats and Upstream Risks

Mnemosyne and the surrounding embedding tooling are young and moving fast.
Treat all claims here as time-bound.

- **Upstream is young and rapidly changing.** APIs, config keys, store layout,
  and retrieval behavior may change between releases. Pin versions and
  re-validate on every upgrade.
- **Marketing claims are conditional/misleading.** Upstream "zero-dependency,
  zero-cloud, sub-ms" style claims are conditional. The "sub-ms" figure refers
  to **narrow warmed SQLite/retrieval microbenchmarks** (a local DB query
  against an already-loaded index), not end-to-end embedding latency — it does
  not include model inference, HTTP round-trip, or consolidation. The measured
  ~17 ms p50 on the N100 is **embedding-service latency** (TEI model inference)
  and does **not** disprove a narrow warmed DB-query claim; the two measure
  different things. "Zero-cloud" describes the local-store design, not the
  embedding model download or any LLM consolidation calls. Do not propagate
  these claims unqualified, and do not contrast the embedding-service latency
  directly against the microbenchmark number as if they were the same metric.
- **`vector_type` is not fully wired (Mnemosyne provider config).** The
  Mnemosyne provider schema reserves a `vector_type` key, but it is not a
  fully wired, documented, operator-facing knob in the pinned Hermes schema;
  a separate env var (e.g. `MNEMOSYNE_VEC_TYPE`) may be read independently. Do
  not assume setting `vector_type` transparently re-shapes Mnemosyne storage;
  verify against the pinned Hermes source. (This is unrelated to gbrain's
  `embedding_columns` registry, which does expose `vector`/`halfvec` types
  through its own documented config path.)
- **Embedding selection is env-driven.** gbrain reads `GBRAIN_EMBEDDING_MODEL`
  and `GBRAIN_EMBEDDING_DIMENSIONS` from the environment; Mnemosyne reads its
  own vars (verified overlay set: `MNEMOSYNE_EMBEDDINGS_VIA_API`,
  `MNEMOSYNE_EMBEDDING_MODEL`, `MNEMOSYNE_EMBEDDING_DIM`,
  `MNEMOSYNE_EMBEDDING_QUERY_PREFIX`, `MNEMOSYNE_EMBEDDING_DOC_PREFIX`,
  `MNEMOSYNE_EMBEDDING_API_URL`). There is no single in-process model selector
  that governs both — they are independent config surfaces that must be kept
  aligned to the same model tuple (see below).
- **No documented `MEMORY.md` migration.** As noted above, there is no
  upstream-provided path to import static memory files into Mnemosyne.
- **Store location and backup.** The Mnemosyne store should live under
  `hermes-data` (the `/opt/data` writable tree), like gbrain state, and must
  **not** be added to `.sync-manifest` (it is runtime state, not
  agent-state-repo content). Before any authoritative use, it needs an
  application-consistent, encrypted backup story — quiescing writes during
  backup or using a snapshot that captures a consistent point-in-time state.
  Copying a live SQLite file mid-write is not safe.
- **Mnemosyne is not installed/enabled in this branch.** The
  `docker-compose.embeddings.yml` overlay wires deployment-ready Mnemosyne
  remote-API env defaults (including `MNEMOSYNE_EMBEDDINGS_VIA_API=true` and
  the model/dim/prefix/API-URL vars listed above), but those are inert until
  Mnemosyne is explicitly installed and enabled by the operator. Nothing here
  claims otherwise.

## Embedding Model Selection

Model choice is governed by a **migration tuple**: model ID + revision +
dimensions + query/passage prefixes + normalization. Changing **any** element
requires a restart and a **full re-embedding of both stores** (gbrain facts and
Mnemosyne memory), because mixing vectors from different models/spaces is
invalid. The overlay pins the tuple in `.env.example` and
`docker-compose.embeddings.yml`; override all elements together when switching.

### Default recommendation: Brazilian Portuguese (pt-BR) on Josemar hardware

**`intfloat/multilingual-e5-small`** — 384 dimensions, MIT license, 512-token
context.

- Requires exact `query: ` / `passage: ` prefixes applied client-side before
  embedding. The pinned gbrain patch adds this preprocessing seam (see
  [Current Branch Scope and Status](#current-branch-scope-and-status)); the
  overlay exposes `EMBEDDING_QUERY_PREFIX` / `EMBEDDING_PASSAGE_PREFIX`, which
  feed Mnemosyne's `MNEMOSYNE_EMBEDDING_QUERY_PREFIX` /
  `MNEMOSYNE_EMBEDDING_DOC_PREFIX` and gbrain's prefix path, so the same
  prefixes reach both consumers.
- Native Portuguese MTEB-BR evidence is roughly **0.561** — an **aggregate
  score across MTEB-BR tasks** (not retrieval-only). Treat it as a relative
  cross-task signal, not an absolute retrieval-quality guarantee; retrieval
  specifically must be validated with retrieval tests (see the smoke-test
  caveat under [Benchmark Evidence](#benchmark-evidence)).
- Fits the N100 4 GiB budget with margin (peak ~1.47 GiB) and is fast enough
  for interactive use (see benchmarks).

### Alternatives and caveats

| Model | Native PT MTEB-BR (approx.) | Notes / caveats |
|---|---|---|
| `intfloat/multilingual-e5-large-instruct` | ~0.641 | Better native Portuguese score, but heavier and prompt-based; higher memory and latency. Re-evaluate against the 4 GiB budget before adopting. |
| `BAAI/bge-m3` | ~0.616 | Symmetric (no prefixes), 8K context. **Observed ~11 GiB resident on the TEI CPU image** and failed at both 4 GiB and 8 GiB limits (OOM-killed). Only ran at 12 GiB as an informational, constraint-exceeding data point. Not viable under the Josemar budget. |
| `Alibaba-NLP/gte-multilingual-base` | — | Prefix-free, 8K context, but at the **benchmarked revision / current repo state** the HF repo ships **no ONNX weights**, forcing TEI's Candle CPU fallback, which **OOMs at 4 GiB** (both float16 and float32). Not viable on this image/budget at the tested revision; a later revision shipping ONNX artifacts could behave differently — re-verify before re-considering. |
| Portuguese STS-specific encoders | — | STS (semantic textual similarity) encoders are **not necessarily retrieval models**. A strong STS score does not imply good asymmetric query→passage retrieval. Validate with retrieval tests, not STS numbers. |
| `BAAI/bge-small-en-v1.5` (English-only default) | n/a | **Wrong for a Portuguese corpus.** Do not use as a default for Josemar. |

### General selection guidance

Choose by **target language and corpus**, not by headline benchmark numbers
measured on a different language:

1. **Match the language.** A multilingual or Portuguese-capable model is
   required for Josemar. English-only models (e.g., `BAAI/bge-small-en-v1.5`)
   are wrong here even if they score well on English benchmarks.
2. **Match the task.** Prefer retrieval-trained models (E5, BGE, GTE retrieval
   variants) over STS/similarity models. Check the model card for asymmetric
   retrieval training, not just STS.
3. **Respect the memory budget.** On the N100, the 4 GiB cap is the hard
   constraint. Larger models (bge-m3, e5-large) may exceed it on the TEI CPU
   image; measure warmup peak memory, not just model file size.
4. **Account for prefixes.** E5-family models require `query: `/`passage: `
   prefixes; mixing prefixed and un-prefixed vectors against the same index is
   invalid. Prefix-free models (bge-m3, gte) avoid this but failed the budget
   here.
5. **Pin the tuple.** Record model ID + revision + dimensions + prefixes +
   normalization as one migration tuple. Any change is a full re-embedding
   event for both stores.
6. **Validate per target language.** The pinned `multilingual-e5-small` tuple
   and the synthetic pt-BR activation fixtures/tests in
   `docs/mnemosyne-retrieval-quality.md` are a validated baseline **only when
   Brazilian Portuguese (pt-BR) is the intended primary language** — not a
   general quality guarantee for arbitrary languages. The model is
   multilingual and may work for other languages, but each language needs its
   own evidence: select and evaluate a suitable model, create representative
   language-specific synthetic fixtures/labels, run the same retrieval quality
   gate, and treat any model/prefix/dimension change as a new migration tuple
   requiring fresh vectors/reindex — never reuse the pt-BR scores.

## Benchmark Evidence

Benchmarks ran in isolated sandboxes under `dump_folder/` (git-ignored, not
versioned). They are **local evidence**, not committed artifacts. Durable
results are summarized here; raw JSON/logs live only in the sandbox.

Two hosts were measured with the same pinned TEI image
(`ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`, version `cpu-1.9.3`)
and the same `intfloat/multilingual-e5-small` model, with `query: `/`passage: `
prefixes applied client-side.

### Local host — Intel Core Ultra 5 225H (14 CPUs, AVX2, 4 GiB cap, 8 CPU limit)

| Metric | E5-small |
|---|---|
| Peak memory | 2.12 GiB / 4 GiB |
| Sequential p50 / p95 | 7.45 ms / 8.32 ms |
| Batch-32 throughput | 355.2 emb/s |
| 4-client concurrency | 80/80 success, 174.0 req/s, p95 31.04 ms |
| Retrieval smoke Recall@1 / @3 | 1.00 / 1.00 |

`BAAI/bge-m3` was OOM-killed at 4 GiB and 8 GiB (needs ~11 GiB warmup); the 12
GiB informational run (p50 ~79.5 ms, 27.4 emb/s, 11.02 GiB peak) is
**caveat-only** and does not satisfy the resource constraint.

### Josemar host — Intel N100 (3 CPUs, 2 CPU cap, 4 GiB cap)

| Metric | E5-small |
|---|---|
| Peak memory | 1.47 GiB / 4 GiB |
| Sequential p50 / p95 | 17.00 ms / 17.34 ms |
| Batch-32 throughput | 102.0 emb/s |
| 4-client concurrency | 80/80 success, 73.0 req/s, p95 58.15 ms |
| Mixed-load interactive p95 | 338.82 ms (p50 19.28 ms, 0 errors) |
| Retrieval smoke Recall@1 / @3 | 1.00 / 1.00 |

The mixed-load test (one batch producer at concurrency 1 plus 60 interactive
single-query requests in parallel) produced **zero errors** but an interactive
p95 of ~339 ms — concrete evidence that batch indexing contends with
interactive queries (see
[Shared Embedding Service and Queue Assessment](#shared-embedding-service-and-queue-assessment)).

### Smoke-test caveat

The retrieval smoke test is a **10-query synthetic set** over a 12-item
Portuguese corpus. Perfect Recall@1/@3 on this tiny set is a sanity signal,
**not a quality benchmark**. It cannot establish that the model retrieves well
across the real, larger, noisier vault. Before any production activation, run
**≥50 representative labeled Portuguese queries** drawn from real assistant
usage and measure Recall@k against the actual corpus. That gate and its
fixtures are a **pt-BR-specific baseline**: for another intended primary
language the model must be re-evaluated with language-specific fixtures (see
`docs/mnemosyne-retrieval-quality.md`), not by reusing the Portuguese scores.

## Shared Embedding Service and Queue Assessment

One shared embedding service is viable for both gbrain and Mnemosyne **after**
the stored gbrain E5 preprocessing patch is in place (so gbrain applies the
`query: `/`passage: ` prefixes correctly). The two **databases remain
separate**; only the embedding endpoint is shared.

**Why synchronous HTTP is safe for concurrency:** a single TEI endpoint serving
multiple callers does **not** misroute concurrent requests. Each HTTP
request/response pair is correlated by the connection and request boundary; TEI
returns each vector array to its originating caller. Internally TEI batches and
queues inputs, but the response mapping is per-request. There is no cross-talk
between gbrain's and Mnemosyne's embedding calls.

**Separate interactive from deferred work:**

- **Interactive query embeddings** need an immediate response. If the service is
  overloaded or down, fall back to **keyword search** (gbrain's existing
  keyword-only path) rather than blocking the turn.
- **Deferred write/backfill jobs** (gbrain reindex/backfill, Mnemosyne
  consolidation writes) can queue and tolerate higher latency.

**Recommended starting architecture:**

- **Direct HTTP** from each consumer (gbrain, Mnemosyne) to the TEI service on
  the dedicated `embeddings-net` network — no pub/sub, no aux-ml involvement.
- A **bounded internal queue with backpressure** on the write/backfill side,
  and **gbrain backfill concurrency = 1** to avoid saturating the N100.
- Add a **durable write-side queue / weighted scheduler** only if measured
  contention warrants it. The mixed-load p95 of 339 ms validates that
  contention is a real concern, but does not by itself justify a durable queue;
  start simple and instrument first.

**Monitorability (TEI exposes):**

- `GET /health` for liveness/readiness.
- Prometheus metrics on the internal metrics port (TEI serves metrics on
  `9000`; the overlay exposes `9000` inside the network only).
- Queue depth / in-flight requests, latency, errors, HTTP 429/503 rates.
- Model identity, dimensions, and revision via `/info` (useful for detecting
  tuple drift).
- Container RSS and restarts via Docker/cgroup stats.

**Logging privacy:** TEI structured JSON logs must **not** log input text or
resulting vectors. Verify this on upgrade; the pinned image does not log
payloads, but a config change or TEI upgrade could regress it.

## Service and Deployment Implications

When the operator eventually enables embeddings, the following must hold:

- **Dedicated, resource-limited container.** The `embeddings` service runs in
  its own container with hard CPU/memory limits (defaults: 2 CPUs, 4 GiB),
  matching the benchmark. Do not share it with aux-ml's memory budget.
- **Model cache only — no user data.** The container mounts only the
  `embedding-model-cache` volume at `/data` (public model weights). No
  `/shared`, no `obsidian-vault`, no `credentials/`, no `hermes-data`/`/opt/data`
  mount. The overlay enforces this.
- **Dedicated network, no host port.** `embeddings-net` is a bridge network
  joined only by `hermes` and `embeddings`. No host ports are published; TEI's
  `80` (embeddings) and `9000` (Prometheus metrics) are exposed only inside the
  network.
- **Account for concurrent aux-ml.** aux-ml can use up to ~8 GiB for OCR jobs.
  The embeddings 4 GiB limit and aux-ml's usage are additive; confirm total
  headroom on the N100 before running both heavily at once.
- **Keyword fallback.** Interactive query embedding must fall back to gbrain
  keyword search on timeout or service unavailability, not fail the turn.
- **Timeouts.** Set explicit embed-request timeouts on both consumers;
  interactive timeouts should be tight (the N100 p95 unloaded is ~17 ms, so a
  few hundred ms is a reasonable upper bound before fallback).
- **Initial gbrain backfill is separate from refresh and TaskNotes.** The
  first gbrain embedding backfill is a one-time operator job, distinct from the
  5-minute `josemar-gbrain refresh` and from the TaskNotes lock. Do not run the
  initial backfill through the periodic refresh path.
- **Refresh must remain `--no-embed`.** The periodic `josemar-gbrain refresh`
  continues to use `gbrain sync --no-embed`. Embedding stale pages is a
  **separate scheduled job**, not folded into refresh. See
  `docs/gbrain-operations.md`.
- **Model migration / reindex.** Changing the model tuple requires a full
  re-embedding of both stores. Plan it as a maintenance window.
- **Mnemosyne backup.** The Mnemosyne SQLite store needs
  application-consistent, encrypted backup before authoritative use. It is
  runtime state under `hermes-data`, not agent-state-repo content, and must
  not be added to `.sync-manifest`.
- **Deploy health checks.** The overlay defines a `curl /health` healthcheck
  with a generous `start_period` for the first model download; honor it and do
  not shorten it below the cold-download time.

## Current Branch Scope and Status

This branch implements **prerequisites and an opt-in Mnemosyne pilot integration
(Phase 1)**. It does **not** enable gbrain embeddings, and it changed no
production data. Mnemosyne is installed into the image but only activated at
runtime when the operator opts in via the `docker-compose.mnemosyne.yml`
overlay.

**Phase 1 policy (user-selected):** upstream-native Mnemosyne, NOT
curated-static coexistence. `MEMORY.md`/`USER.md` remain at their existing,
versioned paths as explicit archive/rollback material; no automatic
migration/deletion; they are NOT injected while the Mnemosyne pilot is enabled.
The first pilot is passive-only ingestion: automatic ingestion is passive raw
user-turn capture (global cross-session); explicit upstream-native
mutation/management tools (including mutating operations) remain available to
the agent. No auto-sleep, reflection, or LLM consolidation runs in the pilot
until the user later makes an explicit LLM-provider/privacy/cost decision. That
decision and the future option are recorded here. Full native Mnemosyne tools
are available (including mutating operations); this is upstream-native
behavior. The `tools` key is omitted from the nested config so the provider
exposes all tools. `memory.write_approval: true` protects built-in archive
writes but does not prevent passive capture or necessarily all Mnemosyne tools.
Upstream's bundled override skill/provider prompt is retained (no custom
suppression patches).

**Implemented and verified:**

- **Pinned gbrain E5 preprocessing patch** (`patches/gbrain-inline-worker-gateway.patch`):
  adds a model-gated `query: `/`passage: ` prefixing seam in the gbrain
  `embed()` path, applied before truncation/batching/retry so each input is
  prefixed exactly once. Non-E5 models are unaffected (the helper is a no-op
  for them). The embedding signature gains a preprocessing-version segment
  (`E5_PREPROCESS_VERSION = 'e5-query-passage-v1'`) for E5 models so raw and
  prefixed vectors are detected as incompatible; non-E5 signatures are
  byte-identical to the pre-patch form. Tests/proof passed.
- **Optional selectable embedding overlay** (`docker-compose.embeddings.yml`
  + `.env.example` `EMBEDDING_*` variables): adds the `embeddings` TEI service,
  the dedicated `embeddings-net` network, the `embedding-model-cache` volume,
  and deployment-ready-but-inert env wiring for Mnemosyne (remote API mode:
  `MNEMOSYNE_EMBEDDINGS_VIA_API` plus model/dim/query+doc prefix/API-URL vars)
  and gbrain (`GBRAIN_EMBEDDING_MODEL` as `llama-server:<model>`,
  `GBRAIN_EMBEDDING_DIMENSIONS`, `LLAMA_SERVER_BASE_URL`). The overlay is truly
  opt-in: base-only deploys are unchanged. Contract tests cover true
  optionality, no host ports, no private mounts, dedicated network membership,
  pinned tuple defaults, and inertness (no `MNEMOSYNE_ENABLED`, no gbrain
  keyword-only alteration).
- **Pinned Mnemosyne packages** (`Dockerfile.hermes`): `mnemosyne-hermes==0.5.0`
  and `mnemosyne-memory==3.15.1` are installed into the Hermes venv using the
  supported full dependency closure (no `--no-deps`). Local FastEmbed/sqlite-vec
  extras may be present but remote TEI is the runtime path. Source-contract
  tests cover the exact pins and venv install. A `RUN_DOCKER_TESTS=1`-gated
  build/import test (`tests/runtime/test_mnemosyne_pilot.py`) verifies the pins
  are installed and importable in the built image without starting project
  services.
- **Base template** (`config/hermes-config.yaml`): native static injection
  enabled (`memory_enabled: true`, `user_profile_enabled: true`) for non-pilot
  deployment. `memory.write_approval: true` is retained as archive protection
  (NOT enforced by `josemar_skill_state.py` `POLICY_KEYS`). `memory.provider` is
  NOT set in the base template; the container init sets it at runtime only when
  `MNEMOSYNE_PROVIDER=mnemosyne`.
- **Opt-in Mnemosyne overlay** (`docker-compose.mnemosyne.yml`): a true opt-in
  overlay intended to be used with `docker-compose.embeddings.yml`. It defines
  no new service, host port, data volume, or deployment setting. It attaches
  hermes to `embeddings-net` (provided by the embeddings overlay), sets
  `MNEMOSYNE_PROVIDER=mnemosyne`, `MNEMOSYNE_DATA_DIR=/opt/data/mnemosyne/data`,
  and validated passive env mirrors (`MNEMOSYNE_DEFAULT_SCOPE=global`,
  `MNEMOSYNE_SYNC_ROLES=user`, `MNEMOSYNE_SKIP_CONTEXTS`,
  `MNEMOSYNE_SYNC_TURN_USER_LIMIT=500` / `MNEMOSYNE_SYNC_TURN_ASSISTANT_LIMIT=800`,
  `MNEMOSYNE_AUTO_SLEEP_ENABLED=false`,
  `MNEMOSYNE_REFLECT_DISABLED_FOR_CRON=true`,
  `MNEMOSYNE_REFLECT_MAX_CALLS_PER_SESSION=0`). `profile_isolation` and `tools`
  have no env var mapping and are set in nested runtime config by init. Remote
  E5 settings flow from the embeddings overlay and are NOT duplicated. Contract
  tests cover base-absence, overlay source/rendered contracts, exact env values,
  no duplicated model settings, and combination with browser-control.
- **Container init activation/rollback** (`docker-hermes-init.sh`): on every
  startup, after copying the source config and as the Hermes user, when
  `MNEMOSYNE_PROVIDER=mnemosyne`: creates the dedicated data directory (inside
  `/opt/data`, no writable-volume allowlist change), runs the pinned wrapper
  installer idempotently against the Hermes venv/runtime home, and sets the
  full nested runtime config (`memory.provider=mnemosyne`,
  `memory.memory_enabled=false`, `memory.user_profile_enabled=false`, and the
  `memory.mnemosyne` block: `default_scope=global`, `profile_isolation=false`,
  `auto_sleep=false`, `reflect_disabled_for_cron=true`,
  `reflect_max_calls_per_session=0`, `sync_roles=[user]`,
  `skip_contexts=[cron,flush,subagent,background,skill_loop]`,
  `sync_turn_user_limit=500`, `sync_turn_assistant_limit=800`) through the
  supported Hermes config interface (`hermes_cli.config`). The `tools` key is
  intentionally omitted so the provider exposes all upstream-native tools
  (including mutating operations). `memory.write_approval` is NOT touched
  (stays true as archive protection for built-in archive writes; does not
  prevent passive capture or necessarily all Mnemosyne tools).
  When unset/empty: runs rollback cleanup — uses the upstream
  `mnemosyne-hermes cleanup` CLI (safe, never touches database) plus narrow
  managed-skill cleanup (validates the `.sha256` sidecar before deletion),
  resets provider/static flags to base template values, and preserves the
  Mnemosyne DB at `/opt/data/mnemosyne/data`. No blanket `rm -rf` of generic
  skills. Static `memories/` directory behavior and existing init-order
  contracts are preserved.
- **Template archive/rollback material** (`templates/agent-state-template/`):
  `README.md` and `BOOT.md` document that `MEMORY.md`/`USER.md` are
  archived-but-not-injected rollback material while the pilot is active. The
  `.sync-manifest` and `.gitignore` continue to preserve the paths (no removal).

**Not enabled / not done:**

- Mnemosyne is **installed in the image** but **not enabled by default**. It is
  activated at runtime only when the operator applies the
  `docker-compose.mnemosyne.yml` overlay (which sets
  `MNEMOSYNE_PROVIDER=mnemosyne`). The env vars wired by the embeddings overlay
  are inert until the operator explicitly enables Mnemosyne.
- gbrain embeddings are **not enabled**. The `josemar-gbrain` wrapper remains
  keyword-only (`search.mcp_keyword_only=true`, `--no-embed`); the overlays do
  not alter this.
- No production data was re-indexed, embedded, or modified. All benchmarks ran
  in isolated containers against synthetic corpora in `dump_folder/`.
- The Mnemosyne encrypted-backup services/volumes/crons ARE implemented but
  **disabled by default**. `docker-compose.mnemosyne-backup.yml` (opt-in,
  layered last) adds the `mnemosyne-backup-uploader` rclone service, the
  profile-gated `mnemosyne-backup-recover` recovery step, the
  `mnemosyne-backup-staging` / `mnemosyne-backup-state` /
  `mnemosyne-backup-recovery` named volumes, and the opt-in Hermes no-agent
  export cron (interval in minutes; defaults to 0/off). The staging path is in
  `HERMES_WRITABLE_VOLUMES` and the scripts ship in the image. Production
  activation still requires the operator to provision the rclone `crypt`
  remote secret in the `obsidian-rclone-config` volume via the existing
  deployment secret mechanism (`RCLONE_CONFIG_B64`); without it, backups
  cannot be encrypted or decrypted. See `docs/mnemosyne-operations.md`.
- LLM consolidation/reflection is intentionally off pending a later explicit
  user decision on LLM provider, privacy, and cost.

**Future activation work (out of scope here):** enabling Mnemosyne at runtime
(apply the `docker-compose.mnemosyne.yml` overlay), optionally enabling the
backup overlay (provision the `crypt` remote secret), flipping gbrain to
embedding mode, running the initial gbrain backfill, and validating with ≥50
labeled Portuguese queries. These are separate, explicitly-gated steps.

## Recommended Staged Transition and Rollback

1. **Prerequisites (done in this branch):** overlay + gbrain E5 patch landed,
   tests green, nothing enabled.
2. **Operator evaluation:** apply the overlay locally, run the embedding
   service, run a gbrain backfill against a copy/staging vault, run the ≥50
   labeled Portuguese query benchmark. Do not point production at it yet.
3. **Mnemosyne pilot (Phase 1 policy):** enable Mnemosyne in remote-API mode
   against the shared TEI service. Static `MEMORY.md`/`USER.md` injection is
   disabled (upstream-native Mnemosyne); the files remain on disk as
   archived-but-not-injected rollback material. The pilot is passive-only (raw
   user-turn capture, no LLM consolidation/reflection). Observe recall quality.
4. **LLM consolidation decision (future):** the user makes an explicit decision
   on LLM provider, privacy, and cost before enabling LLM-driven
   consolidation/reflection. This is intentionally off in Phase 1.
5. **Rollback:** to revert, disable Mnemosyne (remove
   `docker-compose.mnemosyne.yml` from `COMPOSE_FILE`). The container init
   restores static injection (`memory_enabled`/`user_profile_enabled` to true),
   removes installer-owned plugin/override-skill artifacts (sha256-verified),
   and preserves the Mnemosyne DB at `/opt/data/mnemosyne/data` for future
   re-activation. Return gbrain to keyword-only (`--no-embed`) if it was enabled.
   The stores are independent, so rolling back Mnemosyne does not require
   re-indexing gbrain and vice versa.

## References

- Upstream TEI: `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`
  (Hugging Face Text Embeddings Inference).
- `intfloat/multilingual-e5-small` model card and revision
  `614241f622f53c4eeff9890bdc4f31cfecc418b3` (384 dims, MIT, 512 tokens).
- `docker-compose.embeddings.yml` and `.env.example` `EMBEDDING_*` section for
  the pinned migration tuple and inert wiring.
- `patches/gbrain-inline-worker-gateway.patch` for the E5 preprocessing seam
  and signature change.
- `docs/gbrain-operations.md` for the keyword-only/no-embed policy and the
  refresh-must-stay-no-embed rule.
- Local benchmark artifacts (non-versioned, sandbox only):
  `dump_folder/embedding-benchmark/results/RESULTS.md`,
  `dump_folder/embedding-benchmark-remote/RESULTS.md`,
  `dump_folder/embedding-benchmark-gte/results/RESULTS.md`, and the
  corresponding `results/*.json` and `logs/*.log` files. These are local
  evidence only and are not committed.