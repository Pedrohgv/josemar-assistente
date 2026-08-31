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
| [`../tests/README.md`](../tests/README.md) | Validation target selection, test-suite contracts, gated runtime tests, timeout guidance | When adding/changing tests or choosing validation for a change |
| [`../.github/workflows/AGENTS.md`](../.github/workflows/AGENTS.md) | Subtree-specific workflow change constraints and documentation routing | Before changing `.github/workflows/**` |
| [`../credentials/README.md`](../credentials/README.md) | Credential setup | When changing credential setup contracts or related onboarding |

## Skill documentation

Runtime skills live under `skills-factory/<skill>/SKILL.md`. The main skill is the routine-use contract; `references/*.md` contains non-routine depth.

Workers changing a skill should follow the root skill organization rules and [`documentation-policy.md`](documentation-policy.md). Do not move common operations out of a main skill merely to reduce line count.

Notable deep-reference patterns:

- `skills-factory/backup-operations/references/` — status observation and recovery checklists.
- `skills-factory/tasknotes/references/` — custom-field details.
- `skills-factory/gbrain/references/` — non-routine gbrain detail; routine note work must remain in the main skill.

## Change-class routing examples

These examples are navigation rules, not a substitute for applicable nested `AGENTS.md` files.

- **Workflow variable, secret, input, deployment gate, or operator-visible deploy behavior:** read `.github/workflows/AGENTS.md` and the specific runbook(s) it routes to; update the canonical catalog/procedure in the same change.
- **gbrain wrapper, cron, locking, PGLite, embeddings, reindex, or maintenance:** read root gbrain safety rules plus `gbrain-operations.md`; update the gbrain skill only if runtime-agent behavior changes.
- **TaskNotes mutation/projection/reconciliation behavior:** read `tasknotes-mcp.md` and the TaskNotes skill; preserve the private-native-gbrain/shared-lock boundary.
- **Skill behavior:** keep the routine contract in `SKILL.md`; use `references/` only for non-routine depth.
- **Test command or validation-gate change:** update `tests/README.md` when the supported contributor/harness procedure changes.
- **Top-level architecture/onboarding change:** update `README.md` only for the orientation-level representation; keep detailed specifications in their canonical runbooks.

When a new documentation domain is introduced, add it here and update the nearest parent guidance that should route workers to it.
