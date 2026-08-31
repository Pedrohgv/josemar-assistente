# Documentation Index

Use this index to select the narrowest durable documentation needed for a change or operation. Coding workers should first follow root and applicable nested `AGENTS.md`; those files route change classes here or directly to canonical documents.

For documentation ownership, canonicality, context placement, and update requirements, see [`documentation-policy.md`](documentation-policy.md).

## How to use this index

- Load a document when its **When to load** condition matches the work.
- Do not load all runbooks preemptively.
- Source/config/tests remain executable truth if prose disagrees with implementation.
- Runtime facts that can change after deployment should be verified mechanically when relevant.

## Maintainer and operator documentation

| Document | Role / audience | When to load |
| --- | --- | --- |
| [`documentation-policy.md`](documentation-policy.md) | Canonical documentation architecture and maintenance policy for coding workers/maintainers | When adding, moving, restructuring, or deciding ownership/canonicality of documentation; when a change has ambiguous documentation impact |
| [`github-workflows.md`](github-workflows.md) | Canonical GitHub Actions workflow index, secret/variable catalog, and operator-facing workflow summary | When changing workflow interfaces/configuration or operating/troubleshooting CI/CD; `.github/workflows/AGENTS.md` determines which sections/runbooks become mandatory for a workflow change |
| [`gbrain-operations.md`](gbrain-operations.md) | gbrain operator architecture, activation, reindex, embeddings, safe adapter, recovery/maintenance procedures | When changing gbrain runtime integration, wrappers, jobs, activation, embeddings, database/reindex behavior, or operator maintenance |
| [`tasknotes-mcp.md`](tasknotes-mcp.md) | TaskNotes MCP architecture, profile gating, locking, reconciliation, recovery | When changing TaskNotes tools, task-file writes, Daily Note projection, TaskNotes/gbrain locking, task schema behavior, or recovery |
| [`obsidian-operations.md`](obsidian-operations.md) | Obsidian/Syncthing operational flow | When changing vault sync behavior, Obsidian-facing operational procedures, or related recovery assumptions |
| [`vault-recovery-operations.md`](vault-recovery-operations.md) | Vault/gbrain recovery export, upload, restore, validation, disaster-recovery procedure | When changing recovery jobs, backup generations, crypt upload, recovery/install ordering, recovery validation, or related workflow variables |
| [`browser-control.md`](browser-control.md) | Optional browser-control architecture, setup, security, and operations | When changing browser-control runtime/tunnel behavior, deployment variables, setup, routing, or operator procedure |
| [`aux-ml.md`](aux-ml.md) | Auxiliary ML/OCR service architecture and operations | When changing aux-ml service behavior, queue/file handoff, Docker profile, configuration, or operations |
| [`mnemosyne-operations.md`](mnemosyne-operations.md) | Optional Mnemosyne deployment, backup, rollback, and recovery | When changing Mnemosyne deployment/runtime integration, storage, recovery, or operator configuration |
| [`mnemosyne-retrieval-quality.md`](mnemosyne-retrieval-quality.md) | Mnemosyne retrieval-quality evaluation and tuning evidence | When changing retrieval-quality assumptions, evaluation methodology, or tuning decisions |
| [`memory-embeddings-evaluation.md`](memory-embeddings-evaluation.md) | Embedding evaluation/activation context | When evaluating or changing embedding models, dimensions, activation criteria, or related quality gates |
| [`graphify.md`](graphify.md) | Graphify navigation, freshness, governance, and regeneration | When using/regenerating `graphify-out/` or changing Graphify integration/governance |

## Other canonical guidance

| Path | Role / audience | When to load |
| --- | --- | --- |
| [`../README.md`](../README.md) | Project orientation, architecture overview, onboarding, and top-level operations navigation | At project entry or when changing user-facing architecture/onboarding summaries |
| [`../templates/agent-state-template/README.md`](../templates/agent-state-template/README.md) | Canonical private state bootstrap/ownership/model-selection contract | When changing `.sync-manifest`, state ownership, bootstrap/template behavior, `hermes/models.yaml`, skill toggles, or state-sync semantics |
| [`../tests/README.md`](../tests/README.md) | Validation target selection, test-suite contracts, gated runtime tests, timeout guidance | When adding/changing tests or choosing validation for a change |
| [`../.github/workflows/AGENTS.md`](../.github/workflows/AGENTS.md) | Subtree-specific workflow change constraints and documentation routing | Before changing `.github/workflows/**` |
| [`../credentials/README.md`](../credentials/README.md) | Credential setup | When changing credential setup contracts or related onboarding |

## Skill documentation

Runtime skills live under `skills-factory/<skill>/SKILL.md`. The main skill is the routine-use contract; `references/*.md` contains non-routine depth.

Workers changing a skill should follow the root skill organization rules and [`documentation-policy.md`](documentation-policy.md). Do not move common operations out of a main skill merely to reduce line count.

Notable deep-reference patterns:

- `skills-factory/backup-operations/references/` — status observation and recovery checklists.
- `skills-factory/tasknotes/references/` — custom-field details.
- `skills-factory/gbrain/references/` — non-routine page-model, Chronicle, and upstream-compatibility detail; routine note work stays in the main skill.
- `skills-factory/browser-control/references/setup.md` — first-time/operator-side connected-browser setup, loaded only when setup or connection recovery is needed.

## Change-class routing examples

These examples are navigation rules, not a substitute for applicable nested `AGENTS.md` files.

- **Workflow variable, secret, input, deployment gate, or operator-visible deploy behavior:** read `.github/workflows/AGENTS.md`; update the canonical catalog in `github-workflows.md` and each subsystem runbook routed by the scoped guidance.
- **Private state schema/bootstrap/sync change:** inspect the state-template README, template files, sync implementation/tests, and any affected runtime docs; do not duplicate the full state schema into root guidance.
- **gbrain wrapper, cron, locking, PGLite, embeddings, reindex, or maintenance:** read root gbrain safety rules plus `gbrain-operations.md`; update the gbrain skill only if runtime-agent behavior changes.
- **TaskNotes mutation/projection/reconciliation behavior:** read `tasknotes-mcp.md` and the TaskNotes skill; preserve the private-native-gbrain/shared-lock boundary.
- **Skill behavior:** keep the routine contract in `SKILL.md`; use `references/` only for non-routine depth.
- **Test command or validation-gate change:** update `tests/README.md` when the supported contributor/harness procedure changes.
- **Top-level architecture/onboarding change:** update `README.md` only for the orientation-level representation; keep detailed specifications in their canonical runbooks.

## Mechanical integrity check

Run the focused documentation contract with:

```bash
make docs-check
```

The target runs `scripts/docs_check.py`, which validates public repository-local Markdown links (including templates), skill reference targets, required documentation architecture files, and egregious always-loaded context-size regressions without network access. The same repository-contract check also runs through normal unit-test discovery, so `make test` / `make verify` exercise it automatically.

Context-size warnings remain review heuristics: the checker only fails a `SKILL.md` at an intentionally high guardrail, because routine self-containment is more important than satisfying an arbitrary line target.

When a new documentation domain is introduced, add it here and update the nearest parent guidance that should route workers to it.
