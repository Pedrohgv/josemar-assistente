# GitHub Workflows AGENTS.md

Purpose: scoped change guidance for `.github/workflows/**`. Keep workflow catalogs and detailed operator procedures out of this always-loaded file; route workers to the canonical documentation that a particular change affects.

Root `AGENTS.md` also applies.

## Prompt Language Policy

All prompts you write MUST be in English.
All AI-harness-facing instructions in this directory must be written in English.

## Scope and executable truth

Workflow YAML is executable truth for CI/CD behavior. Before editing a workflow, inspect the surrounding YAML, relevant tests, and the canonical documentation routed below. Do not infer deployment behavior from this file alone.

All workflows use the repository's self-hosted runner unless the workflow source explicitly says otherwise.

Current workflows and the full secret/variable catalog are documented in `docs/github-workflows.md`.

## Documentation dependency awareness

A workflow change is incomplete until its documentation dependencies have been inspected. The worker does not need to pre-load every runbook; use the change class to select the narrowest relevant documents.

Required routing:

- **Workflow add/delete/rename/purpose:** update the workflow index in `docs/github-workflows.md`; update `docs/README.md` if documentation routing/domains change.
- **Secret add/delete/rename/requiredness/meaning:** update the secret catalog in `docs/github-workflows.md` and the affected setup/operator runbook.
- **Variable add/delete/rename/default/validation/allowed values/meaning:** update the variable catalog in `docs/github-workflows.md` and every affected subsystem runbook. This applies even when the code change is otherwise local to this directory.
- **Vault-recovery deployment, rclone, backup, release-gate, restore, or teardown behavior:** read/update `docs/vault-recovery-operations.md`; inspect relevant runtime/contract tests and `tests/README.md` when validation procedure changes.
- **Browser-control overlay, routing, tunnel, authorized-key, network, or deploy behavior:** read/update `docs/browser-control.md`.
- **Mnemosyne overlay, deployment mode, backup, restore, or deploy verification:** read/update `docs/mnemosyne-operations.md`.
- **gbrain embedding overlay or manual activation/backfill:** read/update `docs/gbrain-operations.md`; use `docs/memory-embeddings-evaluation.md` when evaluation/activation criteria change.
- **TaskNotes flags or deployment behavior:** read/update `docs/tasknotes-mcp.md`; update the TaskNotes skill only if runtime-agent behavior changes.
- **aux-ml profile/config/deploy behavior:** read/update `docs/aux-ml.md`.
- **Test/release gate invocation, supported validation command, or timeout expectation:** inspect/update `tests/README.md`.
- **Harness-facing workflow policy:** update this file and any deliberately duplicated root invariant.

The source change may reveal additional dependencies. This list is a discovery floor, not an exemption from the repository-wide documentation-impact review in `docs/documentation-policy.md`.

## Deliberately duplicated deployment interface summaries

Keep these short summaries here because workflow changes can invalidate them before a worker opens the deeper runbook. The canonical full catalogs remain in `docs/github-workflows.md` and the subsystem runbooks.

- `MNEMOSYNE_DEPLOY_MODE` is the strict Mnemosyne mode selector with supported values `off`, `pilot`, and `backup`. Backup additionally requires `MNEMOSYNE_BACKUP_EXPORT_INTERVAL` under the workflow's bounded integer validation. The default encrypted vault-recovery lane independently validates `vault-recovery-crypt`. Mnemosyne selection/teardown remains fail-closed and must preserve the documented `aux-ml`/optional-overlay teardown behavior.
- Post-start Hermes configuration validation uses `hermes_cli.config.load_config()` against `/opt/data/config.yaml`; workflow changes must preserve the executable/tested contract or update all dependent docs/tests together.
- `TASKNOTES_DAILY_LINKS_ENABLED` is the strict master Daily Note projection switch, default `true` when missing/empty. `TASKNOTES_DAILY_LINKS_RECONCILE_ENABLED` is the strict reconciliation slave switch, also default `true`, and is effective only while `TASKNOTES_DAILY_LINKS_ENABLED` is also `true`. Both remain fail-closed strict booleans before mutation and in the runtime configuration path.

## Workflow safety invariants

Preserve these unless the authoritative issue/plan explicitly changes them and all affected tests/docs are updated:

1. **Fail closed before destructive mutation.** Validation that is intended to protect an existing deployment must happen before teardown or state mutation. Do not weaken a preflight/release gate by converting failure into warning/continue behavior.
2. **Secrets stay secret.** Never echo tokens, decoded configs, keys, or credential contents. Temporary secret material must be removed on success, failure, and cancellation paths according to the workflow's current safety contract.
3. **Preserve named state unless explicitly performing a reviewed destructive operation.** Normal deploy/stop teardown must not gain `-v` or equivalent state deletion accidentally.
4. **Optional overlays must be removed deterministically when disabled.** Keep maximal teardown/stale-state cleanup behavior consistent with the current set of optional services.
5. **Do not bypass runtime identity/locking contracts.** gbrain/TaskNotes operations launched by workflows must use the documented Hermes runtime user and safe operator/private-native path; never introduce concurrent PGLite access or route an already locked internal path through the public wrapper.
6. **Validate selected Compose configuration before mutation.** Invalid overlay combinations must fail before service/state changes wherever the deployment contract currently guarantees that ordering.
7. **Production dependency pins are compatibility contracts.** A workflow variable must not silently turn a reviewed pin into an unrestricted override. Upgrades require the repository-wide pinned-dependency process from root `AGENTS.md`.
8. **Public logs/artifacts must contain no secrets, private state contents, PII, or private host/network details.**

Detailed subsystem invariants belong in their canonical runbooks and tests rather than being copied wholesale here.

## Validation

- Inspect tests covering the workflow or affected subsystem before changing behavior.
- Prefer the repository's named Make targets and focused runtime gates described in `tests/README.md`.
- Do not claim a timed-out or unrun gate passed.
- When adding/changing a validation gate, document how maintainers/harness workers should run it if that procedure is not self-evident from the normal Make targets.

## Completion checklist

Before considering a workflow change complete:

- workflow YAML and affected tests agree;
- the secret/variable/workflow catalog in `docs/github-workflows.md` reflects any changed interface;
- every routed subsystem runbook affected by the change is updated;
- deliberately duplicated safety summaries remain consistent;
- supported validation documentation is updated when needed;
- the implementation report states which documentation changed, or explicitly records why no durable documentation change was required.
