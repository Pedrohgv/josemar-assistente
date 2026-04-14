# Vault Gateway Architecture (Current State)

## Goal

Provide MBIFC vault capabilities to Josemar through one public skill entrypoint, while keeping internal workflow components private and manageable.

## Topology

- Public entrypoint: `vault-gateway`
- Route contract is explicit (`route` + `payload`) and enforced strictly at runtime.
- Active operation routes: `template.list`, `template.inspect`, `note.read`, `note.capture`, `note.update`, `note.search`, `note.link`, `note.file`, `inbox.triage`, `vault.defrag`, `vault.audit`, `vault.deep-clean`, `tags.garden`, `onboarding`
- Internal active playbooks:
  - workflow playbooks: onboarding, inbox-triage, defrag, vault-audit, deep-clean, tag-garden
  - note route playbooks: note-read, note-capture, note-update, note-search, note-link, note-file
  - template route playbooks: template-list, template-inspect
- Internal dormant playbook: transcribe

## Source of Truth

- Bundle source of truth: repository files under `skills-factory/vault-gateway/`
- Runtime loading: image-managed directory via OpenClaw `skills.load.extraDirs` (`/opt/josemar/skills`)
- Legacy workspace vault skills `obsidian-power-user` and `obsidian-shared-vault` are disabled in OpenClaw config to avoid context bloat.
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
- Relative-path constraints at contract and filesystem layers.
- Vault root allowlist guard before route execution.
- Session-isolated onboarding state via required `state_key`.
