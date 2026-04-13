# Transcribe Playbook (Dormant)

## Status

- State: dormant
- Source: `dump_folder/My-Brain-Is-Full-Crew/skills/transcribe/SKILL.md`
- Phase: scaffold (backend unavailable)

## Purpose

- Preserve MBIFC transcription workflow for future activation.
- Keep routing metadata ready without exposing route as active.

## Activation Prerequisites

- Configure and validate a transcription backend.
- Add runtime integration contract and timeout policy.
- Add regression tests for transcript-to-note flow.

## Current Behavior

- `vault-gateway` returns a clear dormant status message when transcription is requested.
- No routing path executes transcription actions until backend is configured.
