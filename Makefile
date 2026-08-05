.PHONY: test test-runtime test-aux-runtime verify \
	test-mnemosyne-retrieval test-mnemosyne-retrieval-smoke test-mnemosyne-retrieval-tei-smoke \
	test-mnemosyne-retrieval-synthetic-regression test-mnemosyne-retrieval-activation test-mnemosyne-retrieval-activation-evidence \
	test-mnemosyne-retrieval-activation-reviewed

test:
	python3 -m unittest discover -s tests -v

test-runtime:
	RUN_DOCKER_TESTS=1 python3 -m unittest discover -s tests/runtime -v

test-aux-runtime:
	RUN_DOCKER_TESTS=1 RUN_AUX_ML_RUNTIME_TESTS=1 python3 -m unittest tests.runtime.test_aux_ml_runtime_contract -v

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

verify: test
	HERMES_DASHBOARD_SESSION_TOKEN=test \
	HERMES_DASHBOARD_BASIC_AUTH_USERNAME=test \
	HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=test \
	HERMES_DASHBOARD_BASIC_AUTH_SECRET=test \
	docker compose config --quiet
