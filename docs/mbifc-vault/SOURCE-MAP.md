# MBIFC Source Map

This file maps MBIFC source artifacts to the new vault-gateway bundle layout.

## Core Mapping

| MBIFC Source | New Location | Status |
| --- | --- | --- |
| `skills/onboarding/SKILL.md` | `skills-factory/vault-gateway/playbooks/onboarding/PLAYBOOK.md` | mapped |
| `skills/inbox-triage/SKILL.md` | `skills-factory/vault-gateway/playbooks/inbox-triage/PLAYBOOK.md` | mapped |
| `skills/defrag/SKILL.md` | `skills-factory/vault-gateway/playbooks/defrag/PLAYBOOK.md` | mapped |
| `skills/vault-audit/SKILL.md` | `skills-factory/vault-gateway/playbooks/vault-audit/PLAYBOOK.md` | mapped |
| `skills/deep-clean/SKILL.md` | `skills-factory/vault-gateway/playbooks/deep-clean/PLAYBOOK.md` | mapped |
| `skills/tag-garden/SKILL.md` | `skills-factory/vault-gateway/playbooks/tag-garden/PLAYBOOK.md` | mapped |
| `skills/transcribe/SKILL.md` | `skills-factory/vault-gateway/dormant/transcribe/PLAYBOOK.md` | mapped (dormant) |

## Note Route Mapping (Agent-Derived)

| MBIFC Source | New Location | Status |
| --- | --- | --- |
| `agents/scribe.md` | `skills-factory/vault-gateway/playbooks/note-capture/PLAYBOOK.md` | mapped |
| `agents/seeker.md` (search modes) | `skills-factory/vault-gateway/playbooks/note-search/PLAYBOOK.md` | mapped |
| `agents/seeker.md` (read-before-edit) | `skills-factory/vault-gateway/playbooks/note-read/PLAYBOOK.md` | mapped |
| `agents/seeker.md` (modification capabilities) | `skills-factory/vault-gateway/playbooks/note-update/PLAYBOOK.md` | mapped |
| `agents/connector.md` | `skills-factory/vault-gateway/playbooks/note-link/PLAYBOOK.md` | mapped |
| `agents/sorter.md` | `skills-factory/vault-gateway/playbooks/note-file/PLAYBOOK.md` | mapped |
| `agents/architect.md` + `agents/scribe.md` | `skills-factory/vault-gateway/playbooks/template-list/PLAYBOOK.md` | mapped |
| `agents/architect.md` + `agents/scribe.md` | `skills-factory/vault-gateway/playbooks/template-inspect/PLAYBOOK.md` | mapped |

## Registry and Dispatch References

| MBIFC Source | New Location | Status |
| --- | --- | --- |
| `references/agents-registry.md` | `docs/mbifc-vault/KEEP-ADAPT-DROP.md` | transformed |
| `references/agent-orchestration.md` | `docs/mbifc-vault/ARCHITECTURE.md` | transformed |
| `DISPATCHER.md` | `skills-factory/vault-gateway/contracts/routes.json` | transformed |

## Explicitly Excluded Sources

- `agents/postman.md`
- `skills/email-triage/SKILL.md`
- `skills/meeting-prep/SKILL.md`
- `skills/weekly-agenda/SKILL.md`
- `skills/deadline-radar/SKILL.md`
- `skills/contact-sync/SKILL.md`
- `skills/create-agent/SKILL.md`
- `skills/manage-agent/SKILL.md`
