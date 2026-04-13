# Vault Gateway Bundle

This directory contains the repo-shipped MBIFC vault bundle for Josemar.

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
- `note.capture`, `note.update`, `note.search`, `note.link`, and `note.file` provide flexible day-to-day vault manipulation.
- `inbox.triage`, `vault.defrag`, `vault.audit`, `vault.deep-clean`, and `tags.garden` return structured maintenance summaries.
- `transcribe` remains dormant.

## Next Implementation Phases

- Expand summary routes into deterministic write-mode operations.
