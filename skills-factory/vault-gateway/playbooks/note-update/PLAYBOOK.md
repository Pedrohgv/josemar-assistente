# Note Update Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/seeker.md`
- Mode: OpenClaw-native gateway flow

## Route Mapping

- Route: `note.update`
- Payload: `path` (required), `text` (required), `mode` (optional: append|prepend|replace)

## Purpose

- Apply focused edits to existing markdown notes.
- Keep note history readable and intentional.
- Follow MBIFC Seeker "read before edit" method.

## MBIFC Method Port (Seeker Modification Capabilities)

Core sequence:

1. Read full note before modifying.
2. Decide edit type: append, prepend, or replace.
3. Apply smallest change that satisfies the request.
4. Confirm what changed and where.

## Update Modes

### Append (default)

- Add new content at the end of the existing note.
- Preserve prior content exactly.

### Prepend

- Add new content at the beginning.
- Useful for summaries or urgent updates.

### Replace

- Replace full note body with provided text.
- Use only when user intent is explicit.

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
