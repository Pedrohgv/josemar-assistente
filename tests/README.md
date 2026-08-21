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
  old-pin → candidate transition.
- `test-gbrain-upgrade-conformance-embeddings` — candidate upgrade with the
  real TEI gate.

### Candidate refs are exact SHAs only

`GBRAIN_CONFORMANCE_CANDIDATE_REF` must be an exact 40-character hexadecimal
Git commit SHA. The Make targets reject an empty ref before Python; the
conformance support layer validates the exact 40-hex form (rejecting branches,
tags, short SHAs, URLs, and shell fragments) and normalizes to lower-case
before any Docker invocation. The baseline image always uses the committed
Dockerfile default; the candidate is passed only as a test-only
`--build-arg GBRAIN_REF=<sha>` and never changes the production pin.

### Reports, cleanup, and TEI cost

- Reports are written under the gitignored `dump_folder/gbrain-conformance/`
  and contain synthetic command/result metadata only (argv, rc, stdout,
  stderr, elapsed) — never environment dumps.
- Final cleanup is unconditional `docker compose down -v --remove-orphans` for
  the disposable project.
- The embeddings gates download the E5/TEI model on first run and are
  expensive; they are explicit and infrequent by design.

### Known regression probes (#124 / #125)

The suite contains explicit reproducible probes for the known open regressions
#124 and #125. Each probe is classified in the report as `fixed`, `present`,
`changed_failure_mode`, or `inconclusive` — the canonical baseline target does
not fail permanently solely because a known issue is open. When the owning bug
is fixed, the fixing PR converts the corresponding probe to a hard regression
assertion.

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
