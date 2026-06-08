# Vault Gateway Architecture (Current State)

## Goal

Provide MBIFC vault capabilities to Josemar through one public skill entrypoint, while keeping internal workflow components private and manageable.

## Topology

- Public entrypoint: `vault-gateway` (executable at `/opt/josemar/skills/vault-gateway/vault-gateway`)
- Public surface: `skills-factory/vault-gateway/SKILL.md` is the only file the LLM reads. It contains:
  - **High-traffic route guidance inlined** as `## note.capture`, `## note.read`, `## note.update`, `## note.file`, `## note.rename`. The full MBIFC orchestration lives here so it is always in context when these routes are used.
  - **Low-traffic route table** with explicit trigger signals pointing at playbooks. The LLM fetches a playbook only when its trigger matches the user's request.
- Route contract is explicit (`route` + `payload`) and enforced strictly at runtime by `lib/router.py`.
- Heuristic capture rules in `lib/router.py:heuristic_capture_rules()` enforce conditional required keys (e.g. `title` is required on `note.capture` when no template is selected).
- Active operation routes: `note.capture`, `note.create` (alias), `note.read`, `note.update`, `note.file`, `note.rename`, `note.search`, `note.link`, `template.list`, `template.inspect`, `onboarding`, `inbox.triage`, `vault.defrag`, `vault.audit`, `vault.deep-clean`, `tags.garden`
- Low-traffic playbooks (fetched on demand): `playbooks/{onboarding,inbox-triage,defrag,vault-audit,deep-clean,tag-garden,note-search,note-link,template-list,template-inspect}/PLAYBOOK.md`
- Dormant playbook: `dormant/transcribe/PLAYBOOK.md`
- `contracts/routes.json` v2 — payload schema, no longer stores playbook paths.

## Source of Truth

- Bundle source of truth: repository files under `skills-factory/vault-gateway/`
- Runtime loading: image-managed directory bundled into the Hermes container at `/opt/josemar/skills`
- Legacy workspace vault skills are not part of the core bundle to avoid context bloat.
- This keeps Obsidian capability shipped with Josemar, not tied to state-repo drift

## Guardrail (Single Rule)

- Protected skill name: `vault-gateway`
- Workspace/state override at `workspace/skills/vault-gateway` is not allowed
- Enforcement location: `scripts/workspace-sync.sh`
- Behavior: if override appears after clone/sync/pull, remove it and push cleanup so image bundle remains authoritative

## Onboarding Port Requirement

The onboarding route supports "port existing vault" with deterministic phases:

1. discovery scan
2. migration plan preview
3. dry-run summary
4. execution

If destructive mode is selected, workflow shows strong warning and strongly recommends backup before execution, then requires explicit confirmation phrases.

## Runtime Safety

- Strict payload validation (required keys, unknown keys, type checks, enum checks).
- Heuristic capture rules: `title` and `text` are required on `note.capture`/`note.create` when no template is selected. Both the contract layer (`lib/router.py:heuristic_capture_rules`) and the handler layer (`lib/vault_ops.py:capture_note`) enforce this; the handler is ground truth for the `template_hint` case where the contract cannot pre-resolve.
- Relative-path constraints at contract and filesystem layers.
- Vault root allowlist guard before route execution.
- Session-isolated onboarding state via required `state_key`.
- Frontmatter auto-preserve on `note.update` replace mode when replacement text lacks YAML block.
- Deterministic managed-block maintenance for context files (`Meta/vault-structure.md`, folder `_index.md`) on write routes; non-managed sections are preserved verbatim.
- Deterministic context ingestion on note routes: nearest `_index.md` (including `## Working Rules`) plus `Meta/vault-structure.md` managed snapshot are attached to operation results.
