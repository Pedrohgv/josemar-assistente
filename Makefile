.PHONY: test test-runtime test-aux-runtime verify

test:
	python3 -m unittest discover -s tests -v

test-runtime:
	RUN_DOCKER_TESTS=1 python3 -m unittest discover -s tests/runtime -v

test-aux-runtime:
	RUN_DOCKER_TESTS=1 RUN_AUX_ML_RUNTIME_TESTS=1 python3 -m unittest tests.runtime.test_aux_ml_runtime_contract -v

verify: test
	HERMES_DASHBOARD_SESSION_TOKEN=test \
	HERMES_DASHBOARD_BASIC_AUTH_USERNAME=test \
	HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=test \
	HERMES_DASHBOARD_BASIC_AUTH_SECRET=test \
	docker compose config --quiet
