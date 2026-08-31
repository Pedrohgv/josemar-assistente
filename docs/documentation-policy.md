# Documentation Architecture Policy

This document is the canonical policy for how Josemar documentation is organized, discovered, and maintained by coding agents and maintainers.

## Goals

Documentation should be complete and reliable without forcing agents to load large amounts of unrelated context. Put information at the narrowest scope where every consumer that needs it will reliably see it.

The system optimizes for two things at once:

1. **Correctness:** behavioral, configuration, safety, and operational changes must update their durable documentation in the same change.
2. **Context efficiency:** routine work should load only the instructions and references relevant to that work.

## Documentation hierarchy

Use the following roles consistently.

| Layer | Role | Load behavior |
| --- | --- | --- |
| source, config, tests | Executable truth for implemented behavior and contracts | Inspect when implementation depends on it |
| root `AGENTS.md` | Universal coding-harness constraints and routing | Always applicable |
| nested `AGENTS.md` | Subtree-specific change constraints and documentation dependencies | Applicable when working in that subtree |
| `SKILL.md` | Runtime-agent contract for routine skill usage | Loaded when the skill is active |
| `references/*.md` | Non-routine skill detail | Load only when the named topic is needed |
| `docs/*.md` | Maintainer/operator architecture and runbooks | Load by topic as routed from parent guidance or `docs/README.md` |
| `README.md` | Orientation, architecture overview, onboarding, and navigation | Use for project entry/orientation |
| issue / plan / PR report | Change-specific engineering record | Never the only durable documentation for shipped behavior |

No document layer overrides source/config/tests when they disagree. A disagreement is documentation drift that must be corrected.

## Documentation dependency awareness

Nested documentation must not be discoverable only by chance.

A parent guidance file must do both of the following when narrower documentation is required:

1. point to the narrower document; and
2. identify the **change classes** that make that document relevant.

Example: `.github/workflows/AGENTS.md` should tell a worker that a change to repository variables, secrets, workflow inputs, deployment gates, or operator-visible deployment behavior requires consulting and updating the canonical workflow catalog/runbook. A worker making an unrelated formatting or implementation-only workflow change should not need to load that full catalog.

When adding a new durable document, update the nearest parent guidance or documentation index that should route consumers to it.

## Canonicality and duplication

Prefer one canonical detailed definition for a fact. Other documents should summarize it only when the summary is necessary at the point of use.

Intentional duplication is appropriate for safety or operational invariants that must be visible in more than one execution context. When an intentionally duplicated invariant changes, inspect and update every applicable copy in the same change.

Do not duplicate large catalogs, schemas, matrices, or procedures across `README.md`, `AGENTS.md`, skills, and runbooks. Keep one detailed canonical source and route to it.

## Documentation impact is part of implementation

A code/config/test change and its durable documentation are one change.

Before completion, classify whether the change affects any of these documentation domains:

- coding-harness instructions;
- runtime-agent or skill behavior;
- operator procedure or recovery behavior;
- configuration, variables, secrets, workflow inputs, or defaults;
- contributor/test procedure or validation gates;
- onboarding or architecture overview;
- templates or starter state.

For every affected domain:

1. identify the canonical document through the applicable parent guidance and `docs/README.md`;
2. update the canonical document in the same PR;
3. update any deliberately duplicated safety/invariant summary in the same PR;
4. update links/routing if the document moves or a new document is introduced.

If no durable documentation change is required, the implementation report must say `Docs: none` and briefly explain why.

Do not use an issue comment, plan, PR description, review, or implementation report as the only documentation for behavior that ships.

## Context placement rules

### Root and nested `AGENTS.md`

Keep `AGENTS.md` focused on rules a coding worker needs while changing files in that scope:

- hard constraints and invariants;
- change-completion requirements;
- required validation;
- documentation dependencies;
- routing to deeper material.

Move lengthy rationale, full catalogs, schemas, compatibility matrices, recovery procedures, and uncommon operational detail to linked on-demand docs.

### Skills

A main `SKILL.md` must be **self-contained for routine operations**. Context size is a heuristic, not a hard correctness limit.

Keep in the main skill:

- purpose and invocation model;
- critical safety constraints;
- common commands/actions;
- ordinary inputs/outputs and most-used paths;
- common decision rules needed for normal use;
- pointers that clearly say when a reference is needed.

Move to `references/<topic>.md`:

- uncommon operations and edge cases;
- full schemas or taxonomies;
- large compatibility/support matrices;
- upgrade, migration, recovery, and operator-only procedures;
- deep output-format detail;
- background rationale not needed for routine execution.

Frequently used skills may be larger than rarely used skills when that avoids an extra reference load on the routine path. Do not optimize line count at the expense of normal usability.

For gbrain specifically, creating/finding/reading/updating ordinary notes and using normal links/backlinks must not require loading a secondary reference.

### Maintainer/operator runbooks

Long runbooks may remain substantial when their workflow is naturally sequential, but split them when recurring tasks can be selected independently and loading the monolith materially wastes context.

Prefer an index/overview plus topic documents over arbitrary small fragments.

## Mutable state vocabulary

Do not describe mutable runtime state as timeless repository truth.

Use these terms deliberately:

- **Repository default:** behavior/configuration committed by the repository when no operator override is applied.
- **Supported mode:** an alternate mode the repository supports but does not necessarily enable by default.
- **Operator-enabled state:** a deployment/configuration choice that an operator has enabled outside immutable repository defaults.
- **Current runtime state:** what a running installation reports now.

When an operational decision depends on current runtime state and a mechanical status/configuration command exists, query that state instead of relying on prose that may be stale.

Documentation may explain how to interpret the status command and what repository defaults mean, but should not claim a mutable current state without evidence.

## Moving or splitting documentation

When moving a document or section:

1. search the repository for all references to the old path or heading;
2. update applicable parent routing and `docs/README.md`;
3. preserve routine-path information in the parent/skill when required;
4. decide whether an old-path compatibility stub is necessary because an external consumer is verified to depend on it;
5. otherwise remove the old path rather than maintaining two canonical copies;
6. run documentation-integrity validation.

Avoid broad filename churn with no context or correctness benefit.

## Validation expectations

Documentation architecture should have lightweight deterministic checks that do not require external network access.

Required mechanical coverage should include, where practical:

- repository-relative references to required docs/skill references resolve;
- explicitly routed `references/*.md` files exist;
- context-size heuristics are visible as warnings or focused tests rather than rigid line-count correctness rules;
- moved paths do not leave stale repository references.

Mechanical checks supplement review; they do not prove semantic completeness or correctness.

## Review checklist

For a change that modifies code, config, tests, workflows, skills, or documentation, verify:

- the applicable root and nested `AGENTS.md` files were followed;
- documentation impact was classified;
- every affected canonical document was updated;
- deliberately duplicated invariants remain synchronized;
- routine paths remain self-contained;
- deeper material is reachable through an explicit parent route;
- mutable-state wording uses the correct category;
- all changed/moved internal references resolve;
- no issue/PR artifact is serving as the sole durable documentation for shipped behavior.
