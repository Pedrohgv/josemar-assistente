# Note Update Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/seeker.md`
- Mode: OpenClaw-native gateway flow

## Route Mapping

- Route: `note.update`
- Payload: `path` (required), `text` (required unless mode is frontmatter), `mode` (optional: append|prepend|replace|frontmatter), `frontmatter_fields` (required when mode is frontmatter)

## Purpose

- Apply focused edits to existing markdown notes.
- Keep note history readable and intentional.
- Follow MBIFC Seeker "read before edit" method.

## MBIFC Method Port (Seeker Modification Capabilities)

The Seeker uses a whole-note read-rewrite pattern. There is no structured patch API — the LLM reads, decides what to change, and rewrites the full file.

Core sequence:

1. **Read** — Call `note.read` to get the full note content and frontmatter.
2. **Decide** — Determine edit type and scope (append, targeted section, frontmatter field, etc.).
3. **Reconstruct** — Build the full modified note content, preserving structure and unchanged sections.
4. **Replace** — Call `note.update` with `mode: replace` and the complete modified text.
5. **Confirm** — Summarize what changed for the user.

This is how MBIFC's Seeker works in practice: the LLM does the intelligent editing, the file system just reads and writes safely.

## Update Modes

### Append (default)

- Add new content at the end of the existing note.
- Preserve prior content exactly.
- Lightweight: no need to read the note first.

### Prepend

- Add new content at the beginning.
- Useful for summaries or urgent updates.
- Lightweight: no need to read the note first.

### Replace

- Replace full note body with provided text.
- **Frontmatter auto-preserve**: if the replacement text does not include a YAML block (`---`), the existing frontmatter is automatically prepended. This prevents accidental frontmatter loss when the LLM provides only the body.
- If the replacement text does include a YAML block, it is used as-is (the LLM is responsible for including all frontmatter fields).
- A `warnings` field in the response indicates when frontmatter was auto-preserved.
- **Use with `note.read` for targeted edits**: read the full note, modify the relevant parts in your response, then write the whole thing back.
- This is the primary mode for MBIFC-style intelligent editing (section changes, frontmatter updates, tag changes, status changes, link fixes).

### Frontmatter

- Update specific frontmatter fields without touching the note body.
- Provide `frontmatter_fields` as a JSON object with the fields to set or update.
- Existing frontmatter fields are preserved; only provided keys are overwritten.
- The note body remains unchanged.
- Example: set `status: active` and add `updated: 2026-04-15` without re-reading or re-writing the body.

## Frontmatter Safety (Auto-Preserve)

When using `mode: replace` with content that lacks a YAML frontmatter block, the gateway automatically preserves the existing frontmatter. This prevents the most common read-then-replace mistake: the LLM reads a note with `note.read`, edits the body only, and forgets to include the frontmatter in the replacement text.

The response includes a `warnings` array when auto-preservation occurs, so Josemar can inform the user if needed.

For targeted frontmatter changes (tags, status, dates), prefer `mode: frontmatter` which is safer and more concise.

## Recommended Post-Edit Hygiene (MBIFC Guidance)

After successful edit, Josemar should usually check:
- frontmatter freshness (`updated` date if used in the vault convention)
- wikilink integrity for touched sections
- whether MOC entries should be updated for major changes

## Runtime Reality (Current Implementation)

Current deterministic handler behavior:
- Resolves note by relative path.
- Enforces `.md` target and existence.
- Applies `append`, `prepend`, or `replace` text operation.
- Supports `frontmatter` mode for surgical YAML field updates.
- Auto-preserves existing frontmatter on `replace` when replacement text has no YAML block.
- Refreshes structural context files after write: updates `Meta/vault-structure.md` managed block and updates folder `_index.md` managed summary.
- Logs operation in `Meta/vault-gateway-log.md`.

Current handler does not:
- Parse or merge frontmatter fields.
- Rebuild sections semantically.
- Auto-fix links, tags, or MOC references.

Those richer MBIFC refinements should happen in additional turns when needed.

## Output Expectations

Return:
- note path
- mode applied

## Safety

- Path must stay inside vault root and be relative.
- Only markdown files can be updated.
- Reject missing or empty `text`.
