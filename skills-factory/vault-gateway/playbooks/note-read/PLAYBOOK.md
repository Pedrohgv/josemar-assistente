# Note Read Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/seeker.md`
- Mode: OpenClaw-native gateway flow

## Route Mapping

- Route: `note.read`
- Payload: `path` (required), `include_frontmatter` (optional, default true), `include_body` (optional, default true)

## Purpose

- Retrieve the full content of an existing note so Josemar can reason about it before editing.
- Enable the MBIFC Seeker "read before edit" protocol as a deterministic first step.
- Expose parsed frontmatter separately so the LLM can inspect metadata without parsing YAML itself.

## MBIFC Method Port (Seeker)

The Seeker always reads a note fully before modifying it. This route gives Josemar the same capability: see the complete content, understand the structure, then decide how to edit.

## Read-Before-Edit Pattern

This is the primary use case for `note.read`:

1. Call `note.read` with the target note path.
2. Observe the full content, frontmatter, and structure.
3. Decide what to change (specific section, tags, frontmatter field, etc.).
4. Construct the modified note content in full.
5. Call `note.update` with `mode: replace` and the full modified text.

This mirrors MBIFC's whole-note read-rewrite approach — the LLM decides what to change, the gateway handles file I/O safely.

## Selective Retrieval

- Set `include_frontmatter: false` to get only the note body (for size-sensitive contexts).
- Set `include_body: false` to inspect frontmatter metadata alone (e.g. check tags, status, dates).

## Output Expectations

Return:
- note relative path
- note title (from filename stem)
- frontmatter dict (when `include_frontmatter` is true)
- body text (when `include_body` is true)
- file size in bytes (when body is included)

## Safety

- Path must be relative and inside vault root.
- Only markdown files can be read.
- Reject missing `path` or non-existent notes.