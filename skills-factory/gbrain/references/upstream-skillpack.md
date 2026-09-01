# Upstream gbrain Skillpack Compatibility

Load this reference when authoring/changing gbrain-integrated skills, prompts, cron jobs, or when upgrading the pinned gbrain version. It is not needed for routine note search/read/write/link operations.

The installed gbrain source tree at `/opt/gbrain/skills/` ships upstream skills documenting gbrain conventions, page structures, workflows, and optional features.

## How to use the upstream skillpack

Read only the relevant upstream skill on demand, for example:

```text
/opt/gbrain/skills/<skill>/SKILL.md
```

Do not load the entire upstream skillpack into context.

Upstream skillpack material is reference, not an override of Josemar policy. When an upstream gbrain skill conflicts with Josemar's root guidance, a Josemar skill, TaskNotes MCP behavior, or a Josemar runbook, the Josemar-specific contract wins.

## Generally compatible references

These upstream materials describe concepts that fit Josemar's current architecture, subject to Josemar's public-wrapper and task-write boundaries:

| Upstream skill/reference | What it is useful for |
| --- | --- |
| `meeting-ingestion` | Meeting frontmatter/section conventions and ingest workflow |
| `frontmatter-guard` | YAML frontmatter conventions, array form, quoting, validation |
| `capture` | `gbrain capture`, slug/idempotency concepts, type routing |
| `_brain-filing-rules.md` | Filing by primary subject and notability guidance |
| `conventions/quality.md` | Citation/source-precedence/backlink conventions |
| `conventions/brain-first.md` | Brain-first lookup pattern before external sources |
| `brain-ops` | Read → enrich → write page loop |
| `repo-architecture` | Upstream brain-directory conventions |
| `reports` | Timestamped report conventions |

Use these as design/reference material; agent-facing execution still uses Josemar's public `gbrain` command and TaskNotes task mutations still use the bounded TaskNotes MCP.

## Feature-gated or deployment-dependent references

Do not assume an upstream skill is usable merely because it exists in the pinned source tree. Verify the required runtime capability first.

- Semantic/hybrid `query` is supported when the deployed gbrain embedding capability is active; Josemar's main skill defines the normal current retrieval path and `gbrain status` is the diagnostic source for mutable current state.
- Briefing/recall, Dream-cycle synthesis, lineage, external-research, or advisor-style skills may depend on embeddings, LLM synthesis, external API keys, or other optional features. Read the specific upstream skill and Josemar operator docs before adopting any such workflow.

Do not document an operator-enabled or current runtime capability as a timeless repository default. Use the repository vocabulary in `docs/documentation-policy.md`.

## Known conflicts with Josemar architecture

Do not adopt upstream workflows that bypass Josemar's deliberate ownership boundaries. Examples include:

| Upstream pattern | Conflict |
| --- | --- |
| `daily-task-manager` | Uses an upstream task-file model; Josemar task mutation belongs to TaskNotes MCP |
| `daily-task-prep` | Assumes upstream calendar/task integration not equivalent to Josemar's bounded TaskNotes flow |
| `signal-detector` | Always-on ambient capture is not a Josemar default |
| `schema-author`, `schema-unify` | Chat-driven schema mutation conflicts with Josemar's guarded schema/skill policy |
| `soul-audit` | Generates user/persona state owned separately by Josemar's private agent-state model |
| `cron-scheduler`, `minion-orchestrator` | Assume gbrain-owned scheduling; Josemar uses Hermes-owned scheduling |
| `skill-creator`, `skillify`, `skill-optimizer`, `skillpack-harvest` | Automatic skill creation/curation conflicts with Josemar's disabled/approval-gated skill-write policy |

This table is illustrative, not permission to use every unlisted upstream skill. Check the skill's requirements and Josemar's current constraints before adoption.

## Upgrade maintenance

When changing `GBRAIN_REF` / the pinned gbrain version:

1. inspect upstream skillpack additions/removals/renames and material contract changes;
2. verify the compatibility statements in this file;
3. re-check Josemar's wrapper/locking/task-write boundaries against upstream behavior;
4. update the main gbrain skill only if routine runtime-agent behavior changes;
5. update `docs/gbrain-operations.md` and the relevant compatibility tests according to the repository pinned-dependency process.

Do not make a compatibility-table update the sole evidence for an upstream upgrade; source/runtime/test validation remains required.
