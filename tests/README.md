# Tests

Josemar tests are split into fast unit/contract tests and opt-in Docker runtime tests.

## Fast Tests

Run the default suite with:

```bash
make test
```

The `test` target uses a Makefile-resolved interpreter (`PYTHON`) that prefers
`venv/bin/python3` when a local virtualenv exists and otherwise falls back to
`python3`. This keeps `make test` working both in a developer checkout with a
venv and in CI, where there is no venv and dependencies are injected via
`TEST_DEPS_DIR`/`PYTHONPATH`. To run the suite directly without `make`, use
whichever interpreter applies to your environment:

```bash
# with a local venv
venv/bin/python3 -m unittest discover -s tests -v

# venv-less (e.g. CI)
python3 -m unittest discover -s tests -v
```

## Development Cycle

Fast tests run automatically in two places:

- `pre-commit`, after running `scripts/setup-pre-commit.sh`.
- `.github/workflows/fast-tests.yml` on pull requests, pushes to `main`, and manual dispatch.

The same fast suite runs in both places; Docker runtime tests remain opt-in.

The `verify` target runs fast tests plus compose validation:

```bash
make verify
```

### gbrain reindex state preflight fast gates (PR #132)

The fail-closed reindex state preflight (see
`docs/gbrain-operations.md` → "Safe Initial Production Activation") is
enforced without Docker by two fast contract suites that run on ordinary
`make test`:

- `tests.gbrain.test_gbrain_manual_refresh_reindex_lock` — preflight
  semantics: fresh only when BOTH canonical artifacts (`config.json` and
  `brain.pglite`) are absent; healthy existing state (regular JSON config,
  engine exactly `pglite`, canonical `database_path`, no persisted
  `database_url`, non-symlink PGLite directory) runs migrate-only; every
  partial/malformed/Postgres/env-override case fails closed with a structured
  nonzero envelope and no native gbrain activity.
- `tests.gbrain.test_gbrain_wrapper_contract` — wrapper wiring: the preflight
  runs under the shared lock with the fixed isolated interpreter and before
  any native command; init selection is driven exclusively by the validated
  preflight state (`fresh` / `existing`), never reclassified; the
  `DATABASE_URL` exception matches the pinned cwd-dotenv parser exactly
  (`.env`, `.env.local`, `.env.development`, `.env.production`, `.env.test`).

These are unit/contract tests — no Docker, no gbrain binary.

## Runtime Docker Tests

Runtime tests are skipped unless explicitly enabled:

```bash
RUN_DOCKER_TESTS=1 python3 -m unittest discover -s tests/runtime -v
```

Or:

```bash
make test-runtime
```

These tests create an isolated Docker Compose project with unique container names and Compose-project-scoped test volumes. They must not attach to the production `obsidian-vault` volume. Cleanup runs `docker compose down -v --remove-orphans` for the test project.

### TaskNotes real-gbrain lifecycle

Build the Hermes image, then run the isolated TaskNotes MCP lifecycle against
the pinned real gbrain CLI. The test uses an ephemeral in-container vault,
disables networking, and passes Telegram/provider credentials as empty values.
It never mounts the production vault.

```bash
HERMES_DASHBOARD_SESSION_TOKEN=test \
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=test \
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=test \
HERMES_DASHBOARD_BASIC_AUTH_SECRET=test \
  docker compose build hermes

RUN_DOCKER_TESTS=1 \
  python3 -m unittest tests.tasknotes_mcp.test_docker_runtime -v
```

Aux-ML runtime tests are separately gated because the aux-ml image can be expensive to build:

```bash
RUN_DOCKER_TESTS=1 RUN_AUX_ML_RUNTIME_TESTS=1 python3 -m unittest tests.runtime.test_aux_ml_runtime_contract -v
```

Or:

```bash
make test-aux-runtime
```

## Mnemosyne Portuguese Retrieval Quality Gate (Phase 3)

A three-tier harness validates Portuguese vector retrieval quality for the
Mnemosyne pilot. The fast tier runs on ordinary discovery; the Docker tiers
are explicitly gated so model download and container builds never run on
ordinary `make test` or pre-commit.

### Fast unit/schema/boundary tests (always run)

Validates the public/activation boundary, report redaction, metric math
(Recall@1/@3/@5, MRR, nDCG@5), minimum dataset count, strict review metadata
(timestamp, method, fingerprint, >=50 total and >=10 per difficulty),
disposable-input cleanup, and the generated in-container script. No Docker,
no Mnemosyne package, no model download.

```bash
python3 -m unittest tests.runtime.test_mnemosyne_retrieval_quality -v
```

Or:

```bash
make test-mnemosyne-retrieval
```

### Gated public Docker smoke

Builds the isolated hermes image, ingests the public synthetic PT-BR fixtures
(`tests/runtime/fixtures/mnemosyne-retrieval/`) into a disposable BeamMemory
store, queries via `beam.recall(query, top_k=...)`, and checks lower sanity
thresholds. Public smoke is **not** activation evidence.

```bash
RUN_DOCKER_TESTS=1 RUN_MNEMOSYNE_RETRIEVAL_SMOKE=1 \
  python3 -m unittest \
  tests.runtime.test_mnemosyne_retrieval_quality.MnemosyneRetrievalPublicSmokeTests.test_keyword_smoke_meets_sanity_thresholds -v
```

Or:

```bash
make test-mnemosyne-retrieval-smoke
```

The TEI-backed E5-small smoke is a separate, more expensive gate that
requires the embeddings overlay + service:

```bash
RUN_DOCKER_TESTS=1 RUN_MNEMOSYNE_RETRIEVAL_SMOKE=1 RUN_MNEMOSYNE_RETRIEVAL_TEI=1 \
  python3 -m unittest \
  tests.runtime.test_mnemosyne_retrieval_quality.MnemosyneRetrievalPublicSmokeTests.test_tei_smoke_meets_sanity_thresholds -v
```

Or:

```bash
make test-mnemosyne-retrieval-tei-smoke
```

### Gated public FaQuAD-IR activation

The activation target evaluates the complete public `MTEB-BR/faquad-ir` test
fixture (`tests/runtime/fixtures/mnemosyne-retrieval/faquad-ir/`): 244 corpus
passages, 900 queries, and all positive qrels. It runs keyword-only and
TEI-backed E5-small modes, computes nDCG@10 plus query-micro and
relevant-passage macro metrics, and requires the gate to be exactly `READY`.
The immutable standard benchmark has no human-review dependency.

```bash
RUN_DOCKER_TESTS=1 RUN_MNEMOSYNE_RETRIEVAL_ACTIVATION=1 \
  python3 -m unittest \
  tests.runtime.test_mnemosyne_retrieval_quality.MnemosyneRetrievalActivationEvalTests -v
```

Or:

```bash
make test-mnemosyne-retrieval-activation
```

The retained synthetic fixture is advisory only:

```bash
make test-mnemosyne-retrieval-synthetic-regression
```

Reports are written under the gitignored `dump_folder/mnemosyne-retrieval-eval/`
directory. See `docs/mnemosyne-retrieval-quality.md` for the full threshold,
provenance, and migration policy.

## Gbrain Autopilot Runtime Experiment

A separately gated, reusable experiment harness observes native gbrain
sync/dream/autopilot behavior against a dummy Obsidian vault. It is skipped by
default and asserts only safety invariants; adoption conclusions must be made
by manual inspection of the printed reports and the JSON summary written to
`dump_folder/gbrain-autopilot-experiment-report.json`.

```bash
RUN_DOCKER_TESTS=1 RUN_GBRAIN_AUTOPILOT_EXPERIMENT=1 \
  python3 -m unittest tests.runtime.test_gbrain_autopilot_experiment -v
```

See issue #67 for the autopilot/dream follow-up discussion.

## gbrain Dream Cycle-Start Recovery Conformance (issue #126/#67, #4390)

An opt-in, provider-gated Docker runtime suite that builds the EXACT
v0.46.26 candidate gbrain commit (`GBRAIN_DREAM_RECOVERY_CANDIDATE_REF`,
an exact 40-hex SHA validated before any Docker invocation) and proves the
#4390/v0.46.26 automatic PGLite Dream cycle-start recovery against a
deterministic loopback Anthropic-compatible mock (the fixture
`tests/runtime/fixtures/gbrain_dream_mock.py`, fake key only — no
production provider credentials, no external network):

1. Runs exactly `gbrain dream --phase synthesize --json` as the native
   binary under the shared TaskNotes lock, with short bounded timings
   (subagent timeout 10s, well below the inline drain's 30s claim lock;
   wait timeout 8s; serial PGLite-safe inline handling) and one seeded
   qualifying transcript.
2. The mock returns a high-score triage verdict and a valid one-page
   synthesis JSON but delays the first synthesis call, so the test
   SIGKILLs the parent right after the real private `dream-inline-*`
   child has been claimed, then proves the lock is released/reacquirable
   and no live process remains.
3. An IMMEDIATE identical rerun is refused with the supported
   `skipped: cycle_already_running` report (the dead parent's cycle lock
   is younger than the 60s holder-takeover grace); then, after the
   stranded row's owner lease (`private_queue_lease_until`, observed via
   `gbrain jobs get`) lapses — bounded, no `gbrain jobs cancel`, no
   `jobs retry`, no DB writes — ONE identical rerun automatically
   reconciles the provably-orphaned private queue at Dream cycle start:
   the stranded row is cancelled with the machine-readable reason
   `private_queue_reconciled: cycle startup recovery: orphaned
   dream-inline private queue` (observable via `gbrain jobs get`), and
   the same input completes: the page is written and visible through the
   supported public `gbrain get` surface, and queue state is inspected
   via `gbrain jobs list/get --json`.

Honest scope: #4390/v0.46.26 incorporates the #4361/#4332 terminal-path
lifecycle upstream. The gate claims EXACTLY automatic PGLite Dream
cycle-start recovery of orphaned private child work; it does not claim
mid-cycle live healing of the interrupted invocation itself. A candidate
build failure because the canonical local patch no longer applies is an
upgrade incompatibility to record, not a harness failure.

```bash
make test-gbrain-dream-recovery GBRAIN_DREAM_RECOVERY_CANDIDATE_REF=<40-hex-v0.46.26-sha>
```

It is skipped by default (gated on `RUN_DOCKER_TESTS=1` AND
`RUN_GBRAIN_DREAM_RECOVERY=1` AND a non-empty candidate ref) and never
runs on ordinary `make test`, `make test-runtime`, or `make verify`.
Reports land under the gitignored `dump_folder/gbrain-conformance/`.

## gbrain Conformance (issue #127)

An opt-in, pass/fail Docker runtime suite that mechanically validates the real
pinned gbrain integration against synthetic state only. It builds the actual
Josemar Hermes image, seeds the disposable source-agent-state with the real
template `.sync-manifest` and canonical `josemar` schema pack, initializes a
synthetic vault committed as the `hermes` runtime user, and exercises the
documented Josemar-supported gbrain surface (activation, retrieval, authoring,
linking, history/revert, delete/restore, refresh, shared-lock contention,
public/private boundary, and zero-LLM Chronicle reads).

### Safety model

- Everything runs against a disposable Compose project: unique project name,
  disposable agent-state/credentials mounts, repo `.env` bypassed, and the
  test-isolation overlay always last.
- Workspace sync, Telegram/hosted-provider credentials, and all owned
  gbrain/vault-recovery jobs are disabled by default.
- All in-container gbrain/vault commands run as the `hermes` runtime user,
  never root.
- No production state, no production Telegram credentials, no root gbrain/vault
  operations.

### Gates

None of the conformance targets run on ordinary `make test`, `make test-runtime`,
or `make verify`: each module is gated on `RUN_DOCKER_TESTS=1` AND its own
`RUN_GBRAIN_*` gate, so generic discovery never invokes them. They are
deliberately NOT wired into CI, pre-commit, or the default/verify targets.

```bash
make test-gbrain-conformance
make test-gbrain-conformance-embeddings
make test-gbrain-conformance-chronicle
make test-gbrain-upgrade-conformance GBRAIN_CONFORMANCE_CANDIDATE_REF=<40-hex-sha>
make test-gbrain-upgrade-conformance-embeddings GBRAIN_CONFORMANCE_CANDIDATE_REF=<40-hex-sha>
```

- `test-gbrain-conformance` — core provider-free suite against the currently
  committed pin (baseline build, no build-arg override).
- `test-gbrain-conformance-embeddings` — adds the real
  `docker-compose.embeddings.yml` overlay and validates the real E5/TEI
  embedding lifecycle.
- `test-gbrain-conformance-chronicle` — provider-gated Chronicle lifecycle
  against a credential-free loopback LiteLLM mock started inside the container
  (no external network, no provider credentials): the real `chronicle_extract`
  job, timeline projection, and semantic reads on deterministic synthetic
  state. This is the fifth explicit opt-in gate; it complements the core
  suite's zero-event Chronicle smoke with the provider-gated event behavior.
- `test-gbrain-upgrade-conformance` — builds an exact candidate gbrain commit
  SHA against the same disposable volumes and proves logical state survives the
  effective-baseline → candidate transition (baseline = the committed
  Dockerfile pin, or the validated upgrade-only
  `GBRAIN_CONFORMANCE_BASELINE_REF` override; see below).
- `test-gbrain-upgrade-conformance-embeddings` — candidate upgrade with the
  real TEI gate; the candidate `josemar-gbrain reindex` runs as the issue #124
  hard preservation regression (classification required exactly `fixed`, no
  recovery path).

### Candidate refs are exact SHAs only

`GBRAIN_CONFORMANCE_CANDIDATE_REF` must be an exact 40-character hexadecimal
Git commit SHA. The Make targets reject an empty ref before Python; the
conformance support layer validates the exact 40-hex form (rejecting branches,
tags, short SHAs, URLs, and shell fragments) and normalizes to lower-case
before any Docker invocation. The baseline image always uses the committed
Dockerfile default; the candidate is passed only as a test-only
`--build-arg GBRAIN_REF=<sha>` and never changes the production pin.

### Upgrade baseline override (upgrade-conformance runs only)

`GBRAIN_CONFORMANCE_BASELINE_REF` is an OPTIONAL, upgrade-only override for
the BASELINE image ref. It exists for one case: when the committed
`Dockerfile.hermes` `GBRAIN_REF` pin is the POST-upgrade gbrain commit and
the upgrade suite must prove the real old → new migration (baseline =
pre-upgrade commit, candidate = the committed pin).

- Absent (the default): behavior is unchanged — the baseline image builds at
  the committed Dockerfile pin, exactly like core conformance.
- Set: the two upgrade suites build/start the baseline image with
  `--build-arg GBRAIN_REF=<ref>` and reject a candidate equal to the
  effective baseline. The value must be an exact 40-hex Git commit SHA,
  validated with the same machinery as candidate refs BEFORE any Docker
  invocation; invalid values fail closed.
- Provenance: the suite asserts `/opt/gbrain/.git/HEAD` inside the baseline
  container equals the effective baseline ref, and the report persists
  `baseline_ref`, `baseline_ref_source` (`override` / `dockerfile`),
  `dockerfile_gbrain_ref`, and the proven `baseline_source_ref` alongside the
  candidate source-ref proof — an old-candidate downgrade cannot pass as a
  migration.

```bash
make test-gbrain-upgrade-conformance \
  GBRAIN_CONFORMANCE_CANDIDATE_REF=<new-40-hex> \
  GBRAIN_CONFORMANCE_BASELINE_REF=<old-40-hex>
```

Caveats: the override is read ONLY by the two upgrade-conformance suites;
core/chronicle/embeddings conformance stays bound to the Dockerfile pin and
never reads it. Do not set it for non-upgrade runs, and never set it equal to
the candidate ref (the suite rejects that as a no-op).

### Historical baseline patches (legacy mapping)

A baseline image for an OLD pin must be patched exactly as it was when that
pin was validated — the current patch is rebased on the new source and does
not apply to the old ref. A static legacy mapping in
`tests/runtime/gbrain_conformance_support.py` (`GBRAIN_LEGACY_PATCH_MAPPING`)
pairs the pre-upgrade pin `15b9863d13635d173562a54f55a1d388bfcf546b`
(gbrain 0.42.73.2) with `patches/legacy/gbrain-inline-worker-gateway.0.42.73.2.patch`,
which is byte-identical to `git show 1fc78e6:patches/gbrain-inline-worker-gateway.patch`
— the production patch at immutable pre-upgrade commit `1fc78e6`,
immediately before the v0.46.25.0 upgrade (not merely the older
pin-introduction commit `4f6a7c6`).

The v0.46.25 legacy mapping is distinct from that v0.42.73.2 historical
mapping: the pre-upgrade pin `055ac6c75a116aafdf3d00b47c9db2294612a134`
(gbrain 0.46.25.0) pairs with
`patches/legacy/gbrain-inline-worker-gateway.0.46.25.0.patch`, byte-identical
to `git show 62605045542ba0fcc558312f3adcdfb2771ad80f:patches/gbrain-inline-worker-gateway.patch`
— the production patch at immutable pre-upgrade commit
`62605045542ba0fcc558312f3adcdfb2771ad80f`, immediately before the
v0.46.26.0 upgrade.

- When the baseline override is set, the baseline build passes BOTH validated
  build args: `--build-arg GBRAIN_REF=<ref> --build-arg GBRAIN_PATCH_FILE=<selected file>`.
- Patch selection is derived from the validated ref only — it is never
  user-controlled (no environment variable can select a patch file).
- Any ref without a legacy mapping (including a mapped ref whose declared
  file is missing from `patches/`) resolves to the canonical current patch
  `gbrain-inline-worker-gateway.patch`; the Dockerfile applies the selected
  file fail-loudly (`test -f` then `git apply`, no fallback/skip).
- Candidate builds and production builds are untouched: they always apply the
  canonical current patch via the committed `ARG GBRAIN_PATCH_FILE` default.

### Reports, cleanup, and TEI cost

- Reports are written under the gitignored `dump_folder/gbrain-conformance/`
  and contain synthetic command/result metadata only (argv, rc, stdout,
  stderr, elapsed) — never environment dumps.
- Persisted config evidence is narrow-only (PR #132): when the suites
  need the file-plane config (`/opt/data/.gbrain/config.json`), they read it
  through an in-container parser on the pinned runtime `python3` that emits
  exactly the explicitly necessary non-secret fields (`embedding_disabled`,
  `embedding_model`, `embedding_dimensions`) as a minimal JSON object — never
  whole `config.json` stdout. Structure tests
  (`test_no_raw_config_capture_in_evidence`,
  `test_config_read_helpers_route_through_narrow_extract`,
  `test_config_extract_emits_only_necessary_fields`) enforce that no raw
  config capture exists in either embeddings suite.
- Final cleanup is unconditional `docker compose down -v --remove-orphans` for
  the disposable project.
- The embeddings gates download the E5/TEI model on first run and are
  expensive; they are explicit and infrequent by design.

### Known regression probes (#125)

The suite contains an explicit reproducible probe for the known open regression
#125, classified in the report as `fixed`, `present`, `changed_failure_mode`,
or `inconclusive` — the canonical baseline target does not fail permanently
solely because a known issue is open. When the owning bug is fixed, the fixing
PR converts the corresponding probe to a hard regression assertion.

Issue #124 is NOT a probe anymore: it is a hard preservation regression. The
former report-only reindex probe and its workaround path were converted into a
hard gate — the reindex classification must be exactly `fixed` in the
embeddings and upgrade-embeddings suites (see the "Operation-level coverage
index" below), and the suite fails on any other outcome. The `fixed`
classification covers semantic-mode preservation: search mode, embedding
config, completion marker, corpus coverage, and semantic retrieval
(`issue124_proof`, `reindex_mode_preserved`, `reindex_config_preserved`,
`reindex_marker_preserved`, `reindex_coverage_preserved`,
`reindex_semantic_retrieval`) are all hard-asserted — the tests enforce the
semantic-preservation `fixed` gate (PR #132). The only remaining
report-only classifications are `schema-status` and the #125 upgrade probe.

### Sync-move regression characterization (issue #125 W1)

The dedicated W1 gate is an explicit Docker test against the current pinned
gbrain. It uses the existing disposable Docker/PGLite conformance harness and
canonical template/schema seeding, runs every command as the `hermes` runtime
user, and uses only public `gbrain` probes plus normal operator
`josemar-gbrain refresh` reconciliation.

It hard-asserts the repaired behavior for both committed, unchanged `git mv`
paths: Case A (`sync-originated`) creates and indexes a committed vault file
through refresh before moving it; Case B (`capture-originated`) creates the
page through public `gbrain capture` before moving it. It records the source
ref/version, pre/post commits, `git diff --name-status -M` rename
classification, file hashes/existence, both refresh envelopes, old/new public
`get`, unique-token `search`, second-refresh probes, and supported metadata.
Raw runtime evidence is written only to the gitignored
`dump_folder/gbrain-conformance/gbrain-sync-move-regression.json`; no transient
evidence is tracked.

```bash
make test-gbrain-sync-move-regression
```

The gate is skipped by default and requires `RUN_DOCKER_TESTS=1` plus
`RUN_GBRAIN_SYNC_MOVE_REGRESSION=1` (the Make target supplies both). It
hard-asserts the repaired behavior for both cases: while #125 was open it
failed with the full raw evidence path and the specific failure signature in
the assertion output, and it turns green unchanged once the owning fix — the
#125 compatibility hunks in
`patches/gbrain-inline-worker-gateway.patch` — is present in the built
image.

### Operation coverage and the Chronicle gates

Every operation documented as supported in `skills-factory/gbrain/SKILL.md`
(and its references/) is classified in the conformance matrix
(`scripts/gbrain_chat_run.py::GBRAIN_OPERATION_CLASSIFICATION`) as core,
Chronicle zero-LLM read smoke, embeddings/provider-gated, operator-only,
forbidden, or probe/unavailable. The provider-free core suite covers the
Chronicle read commands (`timeline`, `day`, `day --week`, `since`,
`last-seen`, `on-this-day`, `orient`, `ontology`) against deterministic
synthetic state with no LLM. Chronicle auto-emission requires an LLM judge and
is not part of the provider-free core gate; if no supported non-LLM way to seed
deterministic Chronicle events exists on the selected pin, the core suite
exercises the Chronicle reads against empty/absent synthetic state and that
limitation is reported rather than silently skipped.

The provider-gated Chronicle gate (`test-gbrain-conformance-chronicle`) closes
that gap: it runs the real `chronicle_extract` lifecycle against a synthetic,
credential-free, OpenAI-compatible LiteLLM mock HTTP server started as the
`hermes` runtime user inside the container (loopback only, no external network
or provider credentials). The mock returns a deterministic event set, so the
gate proves the provider-gated event behavior — extraction, timeline
projection, and the semantic reads on the projected event — without any real
LLM. The core suite's zero-event Chronicle smoke and this provider-gated event
behavior are deliberately separate gates: the former stays provider-free and
always runnable, the latter is the explicit opt-in that exercises the LLM
judge path.

### Operation-level coverage index

The table below is the maintainers' index for the current manifests: every
operation owned by the conformance suites, its surface, its gate, and its
coverage depth. "Deep" means the operation is exercised end-to-end against
deterministic synthetic state; "smoke" means a probe records a classification
without hard-asserting the outcome. Scenario names in parentheses are the
`CoreScenarioMixin` / suite methods that own the coverage.

| Operation | Surface | Gate | Coverage | Probe status |
| --- | --- | --- | --- | --- |
| `status` | public `gbrain` | core | deep (`status`) | — |
| `search` | public `gbrain` | core | deep (`search`, `get_search_tags`) | — |
| `get` | public `gbrain` | core | deep (`get`, `type_inference`) | — |
| `capture` | public `gbrain` | core | deep (`capture`, `public_write_contracts`) | — |
| `put` | public `gbrain` | core | deep (`put`, `public_write_contracts`) | — |
| `link` | public `gbrain` | core | deep (`link`, `links_backlinks_graph`) | — |
| `backlinks` | public `gbrain` | core | deep (`backlinks`, `links_backlinks_graph`) | — |
| `graph` | public `gbrain` | core | deep (`graph`, `links_backlinks_graph`) | — |
| `tags` | public `gbrain` | core | deep (`tags`, `get_search_tags`) | — |
| `history` | public `gbrain` | core | deep (`history`, `recovery_history_revert`) | — |
| `delete` | public `gbrain` | core | deep (`delete`, `soft_delete_restore`) | — |
| `revert` | public `gbrain` | core | deep (`revert`, `recovery_history_revert`) | — |
| `restore` | public `gbrain` | core | deep (`restore`, `soft_delete_restore`) | — |
| `doctor` | public `gbrain` | core | deep (`doctor`) | — |
| `sources list` | public `gbrain` | core | deep (`sources_list`) | — |
| `put --stdin` | public `gbrain` (rejected) | core | deep (`put --stdin` asserts rejection) | — |
| `timeline`, `day`/`day --week`, `since`, `last-seen`, `on-this-day`, `orient`, `ontology` | public `gbrain` | core + chronicle | core zero-event smoke (`chronicle_*`); chronicle provider-gated deep event behavior | — |
| `search` (semantic/hybrid) | public `gbrain` | embeddings | deep (`semantic_search`) | — |
| `query --no-expand` | public `gbrain` | embeddings | deep (`query_no_expand`) | — |
| `reindex` | operator (`josemar-gbrain`) | core + embeddings | deep (`reindex`, `public_reindex_rejected`); hard #124 preservation gate (`issue124_proof`, `reindex_probe`, `reindex_mode_preserved`, `reindex_config_preserved`, `reindex_marker_preserved`, `reindex_coverage_preserved`, `reindex_semantic_retrieval`) | #124 hard gate: classification required exactly `fixed` |
| `refresh` | operator (`josemar-gbrain`) | core | deep (`refresh`, `external_edit_pre_refresh`, `external_edit_post_refresh`, `refresh_lock_busy`) | — |
| `embed-backfill` | operator (`josemar-gbrain`) | embeddings | deep (`embed_backfill`) | — |
| `enable-embeddings` | operator (`josemar-gbrain`) | embeddings | deep (`enable_embeddings`) | — |
| `disable-embeddings` | operator (`josemar-gbrain`) | embeddings | deep (`disable_embeddings`, `disable_keyword_sentinel`, `disable_vectors_preserved`) | — |
| `refresh-embeddings` | operator (`josemar-gbrain`; sole chat-allowed maintenance command) | embeddings | deep (`stale_edit_refresh`) | — |
| `schema-status` | public `gbrain` (allowlisted read-only diagnostic) | core | smoke (`schema_status_probe`) | `fixed` / `present` / `changed_failure_mode` / `inconclusive` (report-only) |
| reindex preservation (issue #124) | operator-only classification | embeddings | hard (`issue124_proof`, `reindex_probe`, `reindex_mode_preserved`, `reindex_config_preserved`, `reindex_marker_preserved`, `reindex_coverage_preserved`, `reindex_semantic_retrieval`) | hard gate: classification required exactly `fixed` (no recovery path) |

Notes:

- **Surface.** "public" is the agent-facing `gbrain` command (issue #110 safe
  adapter); "operator" is the `scripts/josemar-gbrain` wrapper, never
  agent-facing.
- **Gates.** core = `RUN_DOCKER_TESTS=1` + `RUN_GBRAIN_CONFORMANCE=1`;
  chronicle = + `RUN_GBRAIN_CHRONICLE_CONFORMANCE=1`; embeddings = +
  `RUN_GBRAIN_EMBEDDING_CONFORMANCE=1`. The upgrade gates
  (`RUN_GBRAIN_UPGRADE_CONFORMANCE`) re-run the applicable provider-free core
  scenarios (and the TEI gate for the embeddings variant) against a candidate
  pin; they own no additional operations.
- **Probe status.** Report-only classifications recorded in the report
  metadata, never hard-asserted; a fixing PR converts the probe to a hard
  regression assertion. Issue #124 is the converted case: its reindex
  classification is hard-asserted (exactly `fixed`) in the embeddings and
  upgrade-embeddings suites, and its former workaround/recovery path is
  eliminated.
- **Not owned by any suite.** Native commands classified `operator_only` in
  the adapter inventory but without a direct coverage entry (`init`, `config`,
  `sync`, `extract`, `embed`, `migrate`, `schema`, `import`, `export`, `jobs`,
  `chronicle-backfill`) are not all reached through the `josemar-gbrain`
  subcommands above: `init`, `config`, `sync`, `extract`, `embed`, `migrate`,
  and `schema` are exercised indirectly by those subcommands; the chronicle
  gate exercises `jobs submit chronicle_extract --follow` through a separately
  shared-lock-protected private-native command
  (`/opt/josemar/libexec/gbrain-native` under the lock runner), not the
  wrapper; the remaining commands (`import`, `export`, other `jobs` forms,
  `chronicle-backfill`) stay unsupported/unowned unless adopted.
