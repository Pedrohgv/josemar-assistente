# Vault Gateway Bundle

This directory contains the repo-shipped MBIFC vault bundle for Josemar.

## MBIFC Reference

- MBIFC stands for **My Brain Is Full Crew**.
- Upstream repository: https://github.com/gnekt/My-Brain-Is-Full-Crew
- Reference date: 2026-04-13

## Design

- One public skill: `vault-gateway`.
- The LLM-facing surface is `SKILL.md`. High-traffic routes (`note.capture`, `note.read`, `note.update`, `note.file`, `note.rename`) have their full MBIFC guidance inlined there.
- Low-traffic routes are reachable via playbooks; `SKILL.md` has a "Low-Traffic Routes" table mapping each route to its playbook with a trigger signal.
- Transcription playbook kept dormant until backend setup is complete.
- Explicit route contract (`route` + `payload`), no keyword auto-classification.
- Strict contract: only `route` and `payload` allowed at top-level.
- Heuristic capture rules in `lib/router.py:heuristic_capture_rules()` enforce conditional required keys (e.g. `title` required on `note.capture` when no template is selected).

## Source of Truth

- Runtime source: this repository (copied into image during build).
- Not intended to be edited through `agent-state` sync.

## Current Runtime Behavior

- `onboarding` supports deterministic new-vault and port-existing flows.
- `onboarding` requires a `state_key` in payload for multi-turn isolation.
- Port flow includes destructive confirmation gates with backup warning.
- `note.capture` requires both `text` and `title` when no template is selected; `title` becomes the filename after unsafe filename/link characters are normalized/removed.
- `note.read`, `note.update` (supports append, prepend, replace with frontmatter auto-preserve, surgical frontmatter mode, and section-targeted append/prepend), `note.search`, `note.link`, `note.file`, and `note.rename` provide flexible day-to-day vault manipulation.
- `note.rename` rewrites `[[old-stem]]` and `[[old-stem|alias]]` wikilinks across the vault (toggleable via `rewrite_wikilinks: false`).
- Section-intent updates follow a read-first policy (`note.read` before `note.update`) and avoid silent fallback to raw append/prepend when the heading is missing or ambiguous.
- Read/write note routes ingest folder context from nearest `_index.md` (including `## Working Rules`) plus `Meta/vault-structure.md` managed snapshot when available.
- After write routes (`note.capture`, `note.update`, `note.file`, `note.rename`), managed context blocks are refreshed in `Meta/vault-structure.md` and folder `_index.md`; human-authored sections remain untouched.
- `inbox.triage`, `vault.defrag`, `vault.audit`, `vault.deep-clean`, and `tags.garden` return structured maintenance summaries.
- `transcribe` remains dormant.

## Playbooks

Playbook files are kept for low-traffic routes and the dormant transcription route. High-traffic route guidance lives in `SKILL.md` directly. The LLM should consult a playbook only when its trigger signal matches the user's request.

### Low-Traffic Note and Template Routes

- `playbooks/note-search/PLAYBOOK.md` — `note.search` retrieval guidance (search modes, scoring, presentation)
- `playbooks/note-link/PLAYBOOK.md` — `note.link` wikilink discipline and bidirectional rules
- `playbooks/template-list/PLAYBOOK.md` — `template.list` template discovery
- `playbooks/template-inspect/PLAYBOOK.md` — `template.inspect` template schema inspection

### Workflow Routes

- `playbooks/onboarding/PLAYBOOK.md` — Full onboarding flow: choose path, collect preferences, area scaffolding, vault baseline structure, core & area-specific templates, user profile, safety gates for destructive port
- `playbooks/inbox-triage/PLAYBOOK.md` — 6-step triage workflow: scan, classify, pre-move checklist, MOC update, digest, archive candidates
- `playbooks/defrag/PLAYBOOK.md` — 5-phase structural maintenance: audit, tag hygiene, MOC refresh, structure evolution, report
- `playbooks/vault-audit/PLAYBOOK.md` — 7-phase health check: structural scan, duplicate detection, link integrity, frontmatter audit, MOC review, cross-reference, health report with trend analysis
- `playbooks/deep-clean/PLAYBOOK.md` — 7-phase audit + 6 extended passes: stale content, outdated references, content quality, redundant tags, broken external links, template compliance
- `playbooks/tag-garden/PLAYBOOK.md` — Focused tag analysis: collect, identify issues, suggest actions, visualize distribution

### Dormant

- `dormant/transcribe/PLAYBOOK.md` — Dormant until transcription backend configured; preserves MBIFC workflow spec

## Next Implementation Phases

- Expand summary routes into deterministic write-mode operations.
