# MBIFC Source Map

This file maps MBIFC source artifacts to the current vault-gateway bundle layout.

## Layout Overview

MBIFC content is now surfaced through two channels:

1. **Public skill surface** — `skills-factory/vault-gateway/SKILL.md` is what the LLM actually reads. High-traffic routes have their full MBIFC orchestration content inlined as `##` sections in SKILL.md. Low-traffic routes are linked by trigger signal, with the playbook fetched on demand.
2. **Playbook files** — `skills-factory/vault-gateway/playbooks/*/PLAYBOOK.md` and `dormant/*/PLAYBOOK.md` keep the full MBIFC multi-step flows for low-traffic and dormant routes.

The `playbook` field was removed from `contracts/routes.json` (v2) — playbook paths are no longer in the data contract, they are referenced from SKILL.md prose only.

## Core Mapping

| MBIFC Source | New Location | Status |
| --- | --- | --- |
| `skills/onboarding/SKILL.md` | `skills-factory/vault-gateway/playbooks/onboarding/PLAYBOOK.md` (low-traffic) | mapped |
| `skills/inbox-triage/SKILL.md` | `skills-factory/vault-gateway/playbooks/inbox-triage/PLAYBOOK.md` (low-traffic) | mapped |
| `skills/defrag/SKILL.md` | `skills-factory/vault-gateway/playbooks/defrag/PLAYBOOK.md` (low-traffic) | mapped |
| `skills/vault-audit/SKILL.md` | `skills-factory/vault-gateway/playbooks/vault-audit/PLAYBOOK.md` (low-traffic) | mapped |
| `skills/deep-clean/SKILL.md` | `skills-factory/vault-gateway/playbooks/deep-clean/PLAYBOOK.md` (low-traffic) | mapped |
| `skills/tag-garden/SKILL.md` | `skills-factory/vault-gateway/playbooks/tag-garden/PLAYBOOK.md` (low-traffic) | mapped |
| `skills/transcribe/SKILL.md` | `skills-factory/vault-gateway/dormant/transcribe/PLAYBOOK.md` | mapped (dormant) |

## Note Route Mapping (Agent-Derived)

High-traffic routes have their MBIFC content inlined into `SKILL.md` directly. Low-traffic routes keep the playbook for on-demand fetching.

| MBIFC Source | Surface | Status |
| --- | --- | --- |
| `agents/scribe.md` (capture) | inlined in SKILL.md `## note.capture` | inlined (high-traffic) |
| `agents/scribe.md` (templates) | `playbooks/template-list/PLAYBOOK.md` + `playbooks/template-inspect/PLAYBOOK.md` | split (low-traffic) |
| `agents/seeker.md` (read-before-edit) | inlined in SKILL.md `## note.read` | inlined (high-traffic) |
| `agents/seeker.md` (modification) | inlined in SKILL.md `## note.update` | inlined (high-traffic) |
| `agents/seeker.md` (search modes) | `playbooks/note-search/PLAYBOOK.md` | mapped (low-traffic) |
| `agents/connector.md` | `playbooks/note-link/PLAYBOOK.md` | mapped (low-traffic) |
| `agents/sorter.md` | inlined in SKILL.md `## note.file` | inlined (high-traffic) |
| `agents/architect.md` | `playbooks/template-list/PLAYBOOK.md` + `playbooks/template-inspect/PLAYBOOK.md` | split (low-traffic) |

`note.rename` is a Josemar-native route, not derived from MBIFC. Its content is inlined in SKILL.md.

## Registry and Dispatch References

| MBIFC Source | New Location | Status |
| --- | --- | --- |
| `references/agents-registry.md` | `docs/mbifc-vault/KEEP-ADAPT-DROP.md` | transformed |
| `references/agent-orchestration.md` | `docs/mbifc-vault/ARCHITECTURE.md` | transformed |
| `DISPATCHER.md` | `skills-factory/vault-gateway/contracts/routes.json` (v2, no playbook field) | transformed |

## Explicitly Excluded Sources

- `agents/postman.md`
- `skills/email-triage/SKILL.md`
- `skills/meeting-prep/SKILL.md`
- `skills/weekly-agenda/SKILL.md`
- `skills/deadline-radar/SKILL.md`
- `skills/contact-sync/SKILL.md`
- `skills/create-agent/SKILL.md`
- `skills/manage-agent/SKILL.md`
