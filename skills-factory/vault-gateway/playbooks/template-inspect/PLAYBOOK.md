# Template Inspect Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/architect.md`, `My-Brain-Is-Full-Crew/agents/scribe.md`
- Mode: OpenClaw-native gateway flow

## Route Mapping

- Route: `template.inspect`
- Payload: `template_path` (required), `include_body_preview` (optional), `include_placeholders` (optional)

## Purpose

- Inspect one template deterministically before writing notes.
- Expose dynamic template schema (`vg_fields`) and placeholders.
- Support conversational missing-field collection before execution.

## Expected Metadata Convention

Templates can expose dynamic metadata via frontmatter:
- `vg_template`
- `vg_template_id`
- `vg_title`
- `vg_description`
- `vg_default_target_folder`
- `vg_aliases`
- `vg_fields`

Templates without these keys are treated as legacy templates.

## Output Expectations

Return:
- normalized template metadata
- field definitions (`name`, `type`, `required`, `default`, `prompt`, `enum`)
- placeholders list (optional)
- body preview (optional)

## Recommended Usage Pattern

1. Call `template.list` to choose candidate.
2. Call `template.inspect` to get required fields.
3. Collect missing user values.
4. Call `note.capture` with `field_values`.

## Safety

- `template_path` must be relative and inside vault root.
- only markdown templates are supported.
