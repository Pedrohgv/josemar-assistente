# Tests

Josemar tests are split into fast unit/contract tests and opt-in Docker runtime tests.

## Fast Tests

Run the default suite with:

```bash
python3 -m unittest discover -s tests -v
```

Or use the repository target:

```bash
make test
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
