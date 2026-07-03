# Note Instantiate Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/scribe.md`
- Mode: Hermes vault-gateway flow

## Route Mapping

- Route: `note.instantiate`
- Payload: `template_path` | `template_id` | `template_hint` (selector), `field_values` (object), `path` (optional), `target_folder` (optional), `title` (optional), `text` (optional captured context), `append_captured_context` (optional boolean, default true), `missing_fields_policy` (optional: ask|fail|defaults, default fail), `if_exists` (optional: fail|skip, default fail)

## Purpose

- Deterministically render a structured template into a vault note.
- Treat the template as the source of truth: no plain-capture fallback, no gateway-injected `created` or `# title` heading unless the template includes them.
- Strip `vg_*` template-control frontmatter keys from the final output while preserving the template's own note frontmatter (`type`, `date`, `tags`, ...).

## When to Use

- Daily notes, meeting notes, structured forms, or any case where a template defines the note shape.
- When you need deterministic output (same template + same field values => same note).
- When you want an explicit `path` that is never uniquified (e.g. `07-Daily/YYYY-MM-DD.md`).

## Decision Flow

1. **Select the template.** Use `template.list` then `template.inspect` if you do not already know the template id/path. Resolution order: `template_path` -> `template_id` -> `template_hint` (fuzzy match on template stems).
2. **Collect field values.** Inspect the template's `vg_fields` to learn required fields, types, and prompts. Fill `field_values` accordingly.
3. **Decide the path.**
   - Explicit `path` (recommended for deterministic notes like dailies): wins over derived title/target_folder; never uniquified; honors `if_exists`.
   - No `path`: derive from `title` (or the template's `title: true` field) + `target_folder` (or the template's `vg_default_target_folder`, default `00-Inbox`). Derived paths use `_unique_path` collision suffixing for compatibility with `note.capture`.
4. **Handle missing fields.** Default policy is `fail` (raises a validation error). Use `ask` to get a `needs_user_input` pending response and collect values in the next turn. Use `defaults` to fall back to template `default` values silently.
5. **Handle existing targets.** With an explicit `path`: `if_exists=fail` (default) raises `Note already exists at path: ...`; `if_exists=skip` returns `action: already_exists`, `created: false`, and does not write or refresh maintenance context.

## Captured Context

- Optional `text` is treated as captured context.
- If the template contains a `{{captured_context}}` placeholder, it is rendered in place.
- Otherwise, when `append_captured_context` is true (default), the context is appended under a `## Captured Context` section.

## Daily Note Example

Use this pattern whenever a daily note must exist before appending tasks or events:

```json
{
  "route": "note.instantiate",
  "payload": {
    "template_path": "Templates/Daily Note.md",
    "path": "07-Daily/<YYYY-MM-DD>.md",
    "field_values": {"Date": "<YYYY-MM-DD>"},
    "missing_fields_policy": "fail",
    "if_exists": "skip"
  }
}
```

If the note already exists, the route returns `action: already_exists`, `created: false`, and makes no changes. Continue with the intended `note.update` section operation.

## Output Expectations

After execution, return:
- `path` (relative)
- `template_used` (relative template path)
- `action` (`created` or `already_exists`)
- `created` (boolean)
- `if_exists`
- `warnings`
- `unresolved_placeholders`
- `maintenance_updates`
- `context` (when created)

## Safety

- Never write outside vault root.
- Reject absolute or traversal paths.
- Explicit `path` is never uniquified; collisions are surfaced via `if_exists`.
- `vg_*` control fields are stripped from the rendered output so they never leak into notes.

## Compatibility

- `note.capture` remains the plain-capture route and still supports structured templates via `template_mode=strict`.
- `note.instantiate` is the strict, deterministic alternative when the template is the source of truth.
