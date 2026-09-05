.PHONY: test test-runtime test-aux-runtime verify docs-check \
	test-browser-routing-runtime \
	test-hermes-desktop-gateway-compat \
	test-vault-recovery test-vault-recovery-portability test-vault-recovery-round-trip \
	test-vault-recovery-dr-drill \
	test-mnemosyne-retrieval test-mnemosyne-retrieval-smoke test-mnemosyne-retrieval-tei-smoke \
	test-mnemosyne-retrieval-synthetic-regression test-mnemosyne-retrieval-activation test-mnemosyne-retrieval-activation-evidence \
	test-mnemosyne-retrieval-activation-reviewed \
	test-gbrain-conformance test-gbrain-conformance-embeddings test-gbrain-conformance-chronicle \
	test-gbrain-sync-move-regression \
	test-gbrain-dream-recovery \
	test-gbrain-upgrade-conformance test-gbrain-upgrade-conformance-embeddings

# Python interpreter used by the default test target.
# Prefer the local virtualenv when present; otherwise fall back to `python3`
# so CI (which has no venv and injects deps via TEST_DEPS_DIR/PYTHONPATH) and
# other venv-less environments can run `make test` unchanged.
PYTHON ?= $(shell test -x venv/bin/python3 && echo venv/bin/python3 || echo python3)

test:
	$(PYTHON) -m unittest discover -s tests -v

docs-check:
	$(PYTHON) scripts/docs_check.py

test-runtime:
	RUN_DOCKER_TESTS=1 python3 -m unittest discover -s tests/runtime -v

test-aux-runtime:
	RUN_DOCKER_TESTS=1 RUN_AUX_ML_RUNTIME_TESTS=1 python3 -m unittest tests.runtime.test_aux_ml_runtime_contract -v

# Browser-routing runtime gate (issue #136, revision 2): builds the
# disposable Hermes image (which exercises the Dockerfile browser-routing
# patch + the agent-browser@0.26.0 bake, the pinned Chrome for Testing bake,
# and the /opt/josemar/browser-use venv bake) and
# proves the three-route design inside the container as the hermes runtime
# user (never root): config schema/startup, LIVE SESSION TOOLSET
# (browser_exec hidden under backend "off", connected_browser_exec visible),
# cold-start ordinary browser_* success with no runtime download, connected
# fail-closed with no fallback, and real connected_browser_exec success
# against a separate disposable CDP fixture (own-tab safety, env scrub,
# deterministic session mapping, bounded timeout). Skipped unless both
# RUN_DOCKER_TESTS=1 and RUN_BROWSER_ROUTING_RUNTIME_TESTS=1 are set (the
# target supplies both); the fast contract suite
# (tests.runtime.test_browser_routing_contract) runs on ordinary `make test`.
test-browser-routing-runtime:
	RUN_DOCKER_TESTS=1 RUN_BROWSER_ROUTING_RUNTIME_TESTS=1 \
	python3 -m unittest tests.runtime.test_browser_routing_runtime -v

# Hermes Desktop Remote gateway compat gate (issue #156 W3, revision 2):
# builds the real v0.21 candidate image via the test's own disposable
# Compose project and proves the production-equivalent gated-dashboard
# protocol inside the container as the hermes runtime user: public
# status/readiness with auth_required true and `basic` provider discovery,
# static session-token inertness (rejected by gated REST/WS), wrong-password
# rejection, password login -> private cookie jar -> fresh single-use
# ws-ticket per WebSocket connection, a streamed real-agent turn over a
# disposable loopback OpenAI-compatible fake provider with a deterministic
# persisted user + assistant transcript, fresh-ticket reconnect resume, and
# a real force-recreate proving cookie reuse without a new login plus
# durable transcript resume on a disposable volume. Skipped unless both
# RUN_DOCKER_TESTS=1 and RUN_HERMES_DESKTOP_GATEWAY_COMPAT_TESTS=1 are set
# (the target supplies both); the fast structural suite in the same module
# runs on ordinary `make test`.
test-hermes-desktop-gateway-compat:
	RUN_DOCKER_TESTS=1 RUN_HERMES_DESKTOP_GATEWAY_COMPAT_TESTS=1 \
	python3 -m unittest tests.runtime.test_hermes_desktop_gateway_compat -v

# Vault-recovery Phase 1: fast unit/contract suite (no Docker).
test-vault-recovery:
	python3 -m unittest discover -s tests/vault_recovery -v

# Vault-recovery Phase 1 portability proof (Docker-gated; the release/deploy
# workflow runs the same test with VAULT_RECOVERY_PORTABILITY_REQUIRED=1,
# which makes a missing docker CLI a FAILURE instead of a skip).
test-vault-recovery-portability:
	RUN_DOCKER_TESTS=1 python3 -m unittest tests.runtime.test_vault_recovery_portability -v

# Vault-recovery Phase 2 encrypted round trip (Docker-gated): real rclone
# crypt over a local underlying dir, upload -> ciphertext proof -> recover ->
# disposable doctor verify -> journaled install into the real mount layout.
test-vault-recovery-round-trip:
	RUN_DOCKER_TESTS=1 python3 -m unittest tests.runtime.test_vault_recovery_round_trip -v

# Vault-recovery Phase 3 full disaster-recovery drill (Docker-gated): real
# vector-bearing state + DB-only link -> export -> encrypted upload ->
# DESTROY both live trees -> recover/verify/install -> doctor/link/vectors/
# config/schema/markers/vault survive -> rollback. The migration-sequence
# proof for declaring the plaintext lane retired.
test-vault-recovery-dr-drill:
	RUN_DOCKER_TESTS=1 python3 -m unittest tests.runtime.test_vault_recovery_dr_drill -v

# Phase 2: Mnemosyne Portuguese vector retrieval quality gate.
# Fast unit/schema/boundary tests (no Docker, no model download). These run
# on ordinary unittest discovery and in pre-commit/CI.
test-mnemosyne-retrieval:
	python3 -m unittest tests.runtime.test_mnemosyne_retrieval_quality -v

# Fast advisory regression checks for the retained synthetic fixture. This is
# never activation evidence.
test-mnemosyne-retrieval-synthetic-regression:
	python3 -m unittest tests.runtime.test_mnemosyne_retrieval_quality.ActivationFixtureRepairRegressionTests -v

# Gated public Docker smoke (keyword-only). Builds the isolated hermes image
# and ingests the public synthetic fixtures into a disposable BeamMemory store.
test-mnemosyne-retrieval-smoke:
	RUN_DOCKER_TESTS=1 RUN_MNEMOSYNE_RETRIEVAL_SMOKE=1 \
	python3 -m unittest tests.runtime.test_mnemosyne_retrieval_quality.MnemosyneRetrievalPublicSmokeTests.test_keyword_smoke_meets_sanity_thresholds -v

# Gated public Docker smoke (TEI-backed E5-small). Requires the embeddings
# overlay + service (COMPOSE_FILE includes docker-compose.embeddings.yml).
# If the TEI service is unavailable, the test reports the precise blocker.
test-mnemosyne-retrieval-tei-smoke:
	RUN_DOCKER_TESTS=1 RUN_MNEMOSYNE_RETRIEVAL_SMOKE=1 RUN_MNEMOSYNE_RETRIEVAL_TEI=1 \
	python3 -m unittest tests.runtime.test_mnemosyne_retrieval_quality.MnemosyneRetrievalPublicSmokeTests.test_tei_smoke_meets_sanity_thresholds -v

# Gated public standard activation. Runs the complete FaQuAD-IR test split
# (900 queries, 244 corpus passages) through BOTH keyword-only AND TEI-backed E5-small
# fresh isolated stores using the exact Beam remember/recall API, computes
# real keyword and TEI aggregates, per-difficulty metrics, latency,
# dense/keyword signal evidence, and TEI-vs-keyword regression, writes
# redacted JSON+Markdown under dump_folder/mnemosyne-retrieval-eval/activation/,
# and evaluates the quality gate. This can take significant time (two
# full ingest+recall passes plus the E5-small model download on the first TEI run).
# The immutable standard benchmark has no human-review dependency and must be READY.
test-mnemosyne-retrieval-activation:
	MNEMOSYNE_RETRIEVAL_MODE=evidence RUN_DOCKER_TESTS=1 RUN_MNEMOSYNE_RETRIEVAL_ACTIVATION=1 \
	python3 -m unittest tests.runtime.test_mnemosyne_retrieval_quality.MnemosyneRetrievalActivationEvalTests -v

test-mnemosyne-retrieval-activation-evidence: test-mnemosyne-retrieval-activation

# Backward-compatible alias for the standard activation command.
test-mnemosyne-retrieval-activation-reviewed:
	$(MAKE) test-mnemosyne-retrieval-activation

# gbrain conformance (issue #127): opt-in Docker runtime suites against the
# real pinned gbrain integration. None of these run on ordinary `make test`,
# `make test-runtime`, or `make verify`: each module is gated on
# RUN_DOCKER_TESTS=1 AND its own RUN_GBRAIN_* gate, so generic discovery
# never invokes them. Reports land under the gitignored
# dump_folder/gbrain-conformance/ (command/result metadata only, no env
# dumps). See tests/README.md -> "gbrain Conformance" and
# docs/gbrain-operations.md -> "gbrain Upgrade Checklist".
test-gbrain-conformance:
	RUN_DOCKER_TESTS=1 RUN_GBRAIN_CONFORMANCE=1 \
	python3 -m unittest tests.runtime.test_gbrain_conformance -v

# Issue #125 W1: committed unchanged git-mv characterization against the
# current pinned gbrain. Runs both sync-originated and capture-originated
# paths through normal josemar-gbrain refresh. Raw runtime evidence is written
# only under dump_folder/gbrain-conformance/; it intentionally fails until the
# regression is fixed.
test-gbrain-sync-move-regression:
	RUN_DOCKER_TESTS=1 RUN_GBRAIN_SYNC_MOVE_REGRESSION=1 \
	python3 -m unittest tests.runtime.test_gbrain_sync_move_regression -v

# Real TEI/E5 embeddings gate: adds the docker-compose.embeddings.yml overlay
# and validates the real embedding lifecycle. A cold model download is
# acceptable because this gate is explicit and infrequent.
test-gbrain-conformance-embeddings:
	RUN_DOCKER_TESTS=1 RUN_GBRAIN_EMBEDDING_CONFORMANCE=1 \
	python3 -m unittest tests.runtime.test_gbrain_conformance_embeddings -v

# Provider-gated Chronicle conformance: runs the REAL chronicle_extract
# lifecycle against a credential-free loopback LiteLLM mock started INSIDE the
# container (no external network, no provider credentials). The core suite's
# zero-event Chronicle smoke stays provider-free; this gate proves the
# provider-gated event behavior (extract -> timeline projection -> semantic
# reads) on deterministic synthetic state.
test-gbrain-conformance-chronicle:
	RUN_DOCKER_TESTS=1 RUN_GBRAIN_CHRONICLE_CONFORMANCE=1 \
	python3 -m unittest tests.runtime.test_gbrain_conformance_chronicle -v

# Dream cycle-start recovery conformance (issue #126/#67, #4390): provider-
# gated gate that builds the EXACT v0.46.26 candidate gbrain commit
# (GBRAIN_DREAM_RECOVERY_CANDIDATE_REF, exact 40-hex SHA validated before any
# Docker invocation) and SIGKILLs `gbrain dream --phase synthesize --json`
# mid-synthesize against a loopback Anthropic-compatible mock (fake key
# only). The immediate identical rerun observes the dead-holder cycle lock
# (skipped: cycle_already_running); after the stranded row's owner lease
# (private_queue_lease_until via jobs get) lapses — bounded, observable,
# no `jobs cancel`, no `jobs retry` — ONE rerun proves #4390/v0.46.26
# automatic PGLite Dream cycle-start recovery: the provably-orphaned
# private dream-inline-* queue is reconciled (private_queue_reconciled:
# reason on the cancelled row) and the same input/page completes via public
# get. The claim is exactly automatic cycle-start recovery; mid-cycle live
# healing is not claimed.
test-gbrain-dream-recovery:
	@test -n "$(GBRAIN_DREAM_RECOVERY_CANDIDATE_REF)" || { echo "ERROR: GBRAIN_DREAM_RECOVERY_CANDIDATE_REF is required (exact 40-hex v0.46.26 gbrain commit SHA)" >&2; exit 2; }
	RUN_DOCKER_TESTS=1 RUN_GBRAIN_DREAM_RECOVERY=1 \
	GBRAIN_DREAM_RECOVERY_CANDIDATE_REF="$(GBRAIN_DREAM_RECOVERY_CANDIDATE_REF)" \
	python3 -m unittest tests.runtime.test_gbrain_dream_recovery -v

# Candidate upgrade conformance: builds an exact candidate gbrain commit SHA
# (GBRAIN_CONFORMANCE_CANDIDATE_REF) against the same disposable volumes and
# proves logical state survives the old-pin -> candidate transition. The
# empty-ref guard runs BEFORE Python so a missing candidate fails fast; the
# exact 40-hex validation happens in the conformance support layer.
test-gbrain-upgrade-conformance:
	@test -n "$(GBRAIN_CONFORMANCE_CANDIDATE_REF)" || { echo "ERROR: GBRAIN_CONFORMANCE_CANDIDATE_REF is required (exact 40-hex gbrain commit SHA)" >&2; exit 2; }
	RUN_DOCKER_TESTS=1 RUN_GBRAIN_UPGRADE_CONFORMANCE=1 \
	GBRAIN_CONFORMANCE_CANDIDATE_REF="$(GBRAIN_CONFORMANCE_CANDIDATE_REF)" \
	python3 -m unittest tests.runtime.test_gbrain_upgrade_conformance -v

# Candidate upgrade conformance with the real TEI/E5 embeddings gate.
test-gbrain-upgrade-conformance-embeddings:
	@test -n "$(GBRAIN_CONFORMANCE_CANDIDATE_REF)" || { echo "ERROR: GBRAIN_CONFORMANCE_CANDIDATE_REF is required (exact 40-hex gbrain commit SHA)" >&2; exit 2; }
	RUN_DOCKER_TESTS=1 RUN_GBRAIN_EMBEDDING_CONFORMANCE=1 RUN_GBRAIN_UPGRADE_CONFORMANCE=1 \
	GBRAIN_CONFORMANCE_CANDIDATE_REF="$(GBRAIN_CONFORMANCE_CANDIDATE_REF)" \
	python3 -m unittest tests.runtime.test_gbrain_upgrade_conformance_embeddings -v

verify: test
	HERMES_DASHBOARD_SESSION_TOKEN=test \
	HERMES_DASHBOARD_BASIC_AUTH_USERNAME=test \
	HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=test \
	HERMES_DASHBOARD_BASIC_AUTH_SECRET=test \
	docker compose config --quiet

# Graphify dev-tool (issue #116): regenerate the codebase knowledge graph.
# Dev-time only — never runs inside the Hermes service. Uses the dedicated
# dev-tools-venv (NOT the pinned test venv). Local AST + markdown structure,
# zero LLM, nothing leaves the machine. See docs/graphify.md.
# Committed artifacts: graphify-out/graph.json + GRAPH_REPORT.md.
# Regenerate deliberately (snapshot), not on every commit.
.PHONY: graphify
graphify:
	@test -x dev-tools-venv/bin/graphify || { echo "ERROR: dev-tools-venv/bin/graphify not found. Run: python3 -m venv dev-tools-venv && dev-tools-venv/bin/pip install graphifyy==0.9.45" >&2; exit 1; }
	dev-tools-venv/bin/graphify update .