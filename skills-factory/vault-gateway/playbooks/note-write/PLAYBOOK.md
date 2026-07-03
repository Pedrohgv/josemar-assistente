# Note Write Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/scribe.md`
- Mode: Hermes vault-gateway flow

## Route Mapping

- Route: `note.write`
- Payload: `path` (required relative path, must end with `.md`), `content` (required string, raw markdown), `if_exists` (optional: fail|skip, default fail)

## Purpose

- Write raw markdown content to a vault note exactly as supplied.
- No template resolution, no frontmatter injection, no heading injection, no `vg_*` stripping.
- Never overwrite an existing note.

## When to Use

- Imports and migrations where the caller already holds the final markdown.
- Programmatic note creation that must not be mutated by the gateway.
- Any case where `note.capture` (plain capture) or `note.instantiate` (template rendering) would inject unwanted metadata.

## Decision Flow

1. **Validate the path.** Must be a safe relative path ending with `.md`. Parent directories are created as needed.
2. **Check existence.**
   - `if_exists=fail` (default): if the target exists, the route raises `Note already exists at path: ...`.
   - `if_exists=skip`: if the target exists, returns `action: already_exists`, `created: false`, and does **not** write or refresh maintenance context.
3. **Write verbatim.** Content is written exactly as supplied. A trailing newline is normalized when the supplied content is non-empty and does not already end with one.
4. **Maintenance refresh.** After a successful write, the gateway refreshes `Meta/vault-structure.md` and the folder `_index.md` managed blocks.

## Output Expectations

After execution, return:
- `path` (relative)
- `action` (`created` or `already_exists`)
- `created` (boolean)
- `if_exists`
- `maintenance_updates`
- `context` (when created)

## Safety

- Never write outside vault root.
- Reject absolute or traversal paths.
- Reject non-`.md` paths.
- Reject empty content.
- Never overwrite; collisions are surfaced via `if_exists`.

## Compatibility

- Use `note.capture` for conversational capture (auto-injects `type`/`created` and a `# title` heading when no template is used).
- Use `note.instantiate` for deterministic template rendering (strips `vg_*`, preserves template frontmatter, no `created`/`# title` injection unless the template includes them).
- Use `note.write` only when the caller already has the final markdown and wants zero mutation.