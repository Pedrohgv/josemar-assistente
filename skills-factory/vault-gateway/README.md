# Vault Gateway Bundle

This directory contains the repo-shipped MBIFC vault bundle for Josemar.

## MBIFC Reference

- MBIFC stands for **My Brain Is Full Crew**.
- Upstream repository: https://github.com/gnekt/My-Brain-Is-Full-Crew
- Reference date: 2026-04-13

## Design

- One public skill: `vault-gateway`.
- Internal playbooks for vault workflows.
- Transcription playbook kept dormant until backend setup is complete.
- Explicit route contract (`route` + `payload`), no keyword auto-classification.
- Strict contract: only `route` and `payload` allowed at top-level.

## Source of Truth

- Runtime source: this repository (copied into image during build).
- Not intended to be edited through `agent-state` sync.

## Current Runtime Behavior

- `onboarding` supports deterministic new-vault and port-existing flows.
- `onboarding` requires a `state_key` in payload for multi-turn isolation.
- Port flow includes destructive confirmation gates with backup warning.
- `note.capture`, `note.read`, `note.update` (supports append, prepend, replace with frontmatter auto-preserve, and surgical frontmatter mode), `note.search`, `note.link`, and `note.file` provide flexible day-to-day vault manipulation.
- After write routes (`note.capture`, `note.update`, `note.file`), managed context blocks are refreshed in `Meta/vault-structure.md` and folder `_index.md`; human-authored sections remain untouched.
- `inbox.triage`, `vault.defrag`, `vault.audit`, `vault.deep-clean`, and `tags.garden` return structured maintenance summaries.
- `transcribe` remains dormant.

## Playbooks

- `playbooks/template-list/PLAYBOOK.md` — Deterministic template discovery for capture flows (`template.list`)
- `playbooks/template-inspect/PLAYBOOK.md` — Deterministic template schema/placeholder inspection (`template.inspect`)
- `playbooks/note-read/PLAYBOOK.md` — MBIFC Seeker-derived read-before-edit guidance for `note.read`
- `playbooks/note-capture/PLAYBOOK.md` — MBIFC Scribe-derived capture/refinement guidance for `note.capture`
- `playbooks/note-update/PLAYBOOK.md` — MBIFC Seeker-derived read-before-edit/update guidance for `note.update`
- `playbooks/note-search/PLAYBOOK.md` — MBIFC Seeker-derived retrieval guidance for `note.search`
- `playbooks/note-link/PLAYBOOK.md` — MBIFC Connector-derived link discipline for `note.link`
- `playbooks/note-file/PLAYBOOK.md` — MBIFC Sorter-derived filing workflow for `note.file`
- `playbooks/onboarding/PLAYBOOK.md` — Full onboarding flow: choose path, collect preferences, area scaffolding, vault baseline structure, core & area-specific templates, user profile, safety gates for destructive port
- `playbooks/inbox-triage/PLAYBOOK.md` — 6-step triage workflow: scan, classify, pre-move checklist, MOC update, digest, archive candidates
- `playbooks/defrag/PLAYBOOK.md` — 5-phase structural maintenance: audit, tag hygiene, MOC refresh, structure evolution, report
- `playbooks/vault-audit/PLAYBOOK.md` — 7-phase health check: structural scan, duplicate detection, link integrity, frontmatter audit, MOC review, cross-reference, health report with trend analysis
- `playbooks/deep-clean/PLAYBOOK.md` — 7-phase audit + 6 extended passes: stale content, outdated references, content quality, redundant tags, broken external links, template compliance
- `playbooks/tag-garden/PLAYBOOK.md` — Focused tag analysis: collect, identify issues, suggest actions, visualize distribution
- `dormant/transcribe/PLAYBOOK.md` — Dormant until transcription backend configured; preserves MBIFC workflow spec

## Next Implementation Phases

- Expand summary routes into deterministic write-mode operations.
