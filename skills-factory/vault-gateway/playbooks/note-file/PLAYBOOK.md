# Note File Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/sorter.md`
- Mode: OpenClaw-native gateway flow

## Route Mapping

- Route: `note.file`
- Payload: `source_path` (required), `target_folder` (required)

## Purpose

- Move notes into their intended vault location.
- Keep inbox and working areas organized.
- Port MBIFC Sorter filing discipline.

## MBIFC Method Port (Sorter)

Before filing, apply Sorter intent checks:

1. Verify destination is the right semantic home.
2. Ensure destination folder exists (create if needed for minor structure).
3. Preserve note content and identity while moving.
4. Avoid destructive operations; move only.

After filing, recommended follow-up:
- update relevant MOC entries
- suggest linking pass if batch introduced many related notes

## Conflict Rules

- If a file with the same name exists, preserve both by using a unique destination filename.
- If destination is unclear, keep note in safer staging area and ask for clarification.

## Runtime Reality (Current Implementation)

Current deterministic handler behavior:
- Resolves source note from relative path.
- Creates destination folder if missing.
- Moves file to destination, auto-resolving name conflicts.
- Logs move result.

Current handler does not:
- Reclassify note type automatically.
- Update frontmatter status/location fields.
- Update MOCs or backlinks automatically.

Those richer Sorter behaviors should be orchestrated in additional turns.

## Output Expectations

Return:
- source path
- destination path

## Safety

- Paths must remain inside vault root.
- Source must be existing markdown note.
- Move only; no delete.
