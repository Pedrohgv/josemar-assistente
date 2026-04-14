# Template List Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/architect.md`, `My-Brain-Is-Full-Crew/agents/scribe.md`
- Mode: OpenClaw-native gateway flow

## Route Mapping

- Route: `template.list`
- Payload: `query` (optional), `path_prefix` (optional), `include_legacy` (optional), `limit` (optional), `mode` (optional)

## Purpose

- Provide deterministic discovery of templates available in the vault.
- Let Josemar inspect what can be used before calling `note.capture`.
- Keep MBIFC spirit: flexible templates with strict execution rails.

## MBIFC Method Port

- Architect defines and evolves templates.
- Scribe chooses the best template for each capture context.
- This route gives Scribe-like capability in deterministic form.

## Output Expectations

Return templates with:
- `template_id`
- `path`
- `title`
- `description`
- `legacy`
- field counts and required counts
- aliases
- default target folder

## Recommended Usage Pattern

1. Call `template.list` to discover candidates.
2. Call `template.inspect` for the selected template.
3. Call `note.capture` with `template_id` or `template_path` and `field_values`.

## Safety

- Only list markdown templates inside vault root.
- Respect relative path constraints for `path_prefix`.
