# MBIFC Triage Matrix (Vault-Only)

## Decisions

- Keep vault interaction and management capabilities.
- Remove email/calendar/contact capabilities from this bundle.
- Keep transcription capabilities dormant.

## Agents

| Agent | Source Path | Decision | Exposure | Notes |
| --- | --- | --- | --- | --- |
| architect | `dump_folder/My-Brain-Is-Full-Crew/agents/architect.md` | KEEP/ADAPT | Internal | Core vault structure and governance logic. |
| scribe | `dump_folder/My-Brain-Is-Full-Crew/agents/scribe.md` | KEEP/ADAPT | Internal | Core note capture/refinement. |
| sorter | `dump_folder/My-Brain-Is-Full-Crew/agents/sorter.md` | KEEP/ADAPT | Internal | Core inbox filing workflow. |
| seeker | `dump_folder/My-Brain-Is-Full-Crew/agents/seeker.md` | KEEP/ADAPT | Internal | Core search and synthesis in vault. |
| connector | `dump_folder/My-Brain-Is-Full-Crew/agents/connector.md` | KEEP/ADAPT | Internal | Core linking and graph improvement. |
| librarian | `dump_folder/My-Brain-Is-Full-Crew/agents/librarian.md` | KEEP/ADAPT | Internal | Core quality and health audits. |
| transcriber | `dump_folder/My-Brain-Is-Full-Crew/agents/transcriber.md` | DORMANT | Internal | Kept for future backend activation. |
| postman | `dump_folder/My-Brain-Is-Full-Crew/agents/postman.md` | DROP | None | Out of scope (gmail/calendar/contacts). |

## Skills

| Skill | Source Path | Decision | Exposure | Notes |
| --- | --- | --- | --- | --- |
| onboarding | `dump_folder/My-Brain-Is-Full-Crew/skills/onboarding/SKILL.md` | KEEP/ADAPT | Through `vault-gateway` | Includes "port existing vault" flow. |
| inbox-triage | `dump_folder/My-Brain-Is-Full-Crew/skills/inbox-triage/SKILL.md` | KEEP/ADAPT | Through `vault-gateway` | Inbox processing core flow. |
| defrag | `dump_folder/My-Brain-Is-Full-Crew/skills/defrag/SKILL.md` | KEEP/ADAPT | Through `vault-gateway` | Structural maintenance. |
| vault-audit | `dump_folder/My-Brain-Is-Full-Crew/skills/vault-audit/SKILL.md` | KEEP/ADAPT | Through `vault-gateway` | Full vault audit. |
| deep-clean | `dump_folder/My-Brain-Is-Full-Crew/skills/deep-clean/SKILL.md` | KEEP/ADAPT | Through `vault-gateway` | Extended cleanup. |
| tag-garden | `dump_folder/My-Brain-Is-Full-Crew/skills/tag-garden/SKILL.md` | KEEP/ADAPT | Through `vault-gateway` | Tag quality and taxonomy care. |
| transcribe | `dump_folder/My-Brain-Is-Full-Crew/skills/transcribe/SKILL.md` | DORMANT | Internal | Dormant until transcription backend is configured. |
| create-agent | `dump_folder/My-Brain-Is-Full-Crew/skills/create-agent/SKILL.md` | DROP | None | Out of scope for vault-only release. |
| manage-agent | `dump_folder/My-Brain-Is-Full-Crew/skills/manage-agent/SKILL.md` | DROP | None | Out of scope for vault-only release. |
| email-triage | `dump_folder/My-Brain-Is-Full-Crew/skills/email-triage/SKILL.md` | DROP | None | Out of scope (email). |
| meeting-prep | `dump_folder/My-Brain-Is-Full-Crew/skills/meeting-prep/SKILL.md` | DROP | None | Out of scope (email/calendar). |
| weekly-agenda | `dump_folder/My-Brain-Is-Full-Crew/skills/weekly-agenda/SKILL.md` | DROP | None | Out of scope (email/calendar). |
| deadline-radar | `dump_folder/My-Brain-Is-Full-Crew/skills/deadline-radar/SKILL.md` | DROP | None | Out of scope (email/calendar). |
| contact-sync | `dump_folder/My-Brain-Is-Full-Crew/skills/contact-sync/SKILL.md` | DROP | None | Out of scope (contacts). |
