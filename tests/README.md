# Tests

Josemar tests are split into fast fake-vault tests and opt-in Docker runtime tests.

## Fast Tests

Run the default suite with:

```bash
python3 -m unittest discover -s tests -v
```

Or use the repository target:

```bash
make test
```

Vault gateway tests never use the real Obsidian vault. They create temporary fake vaults under the system temp directory and set `OBSIDIAN_VAULT_DIR` plus `VAULT_GATEWAY_ALLOWED_ROOTS` to that fake path.

Run only vault tests with:

```bash
python3 -m unittest discover -s tests/vault_gateway -v
```

Or:

```bash
make test-vault
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

Aux-ML runtime tests are separately gated because the aux-ml image can be expensive to build:

```bash
RUN_DOCKER_TESTS=1 RUN_AUX_ML_RUNTIME_TESTS=1 python3 -m unittest tests.runtime.test_aux_ml_runtime_contract -v
```

Or:

```bash
make test-aux-runtime
```
