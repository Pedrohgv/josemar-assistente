# Note Link Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/connector.md`
- Mode: OpenClaw-native gateway flow

## Route Mapping

- Route: `note.link`
- Payload: `source_path` (required), `target_path` (required), `bidirectional` (optional)

## Purpose

- Create explicit wikilink relationships between notes.
- Improve graph connectivity in small, safe steps.
- Port MBIFC Connector link discipline.

## MBIFC Method Port (Connector)

Linking principles:

1. Quality over quantity.
2. Add links only when they improve navigation or understanding.
3. Keep links contextual and easy to maintain.
4. Prefer reversible, explicit edits.

## Link Creation Rules

- Use wikilinks, not external markdown URLs, for note-to-note links.
- Avoid duplicate insertions.
- Use bidirectional linking when relationship is symmetrical.

Recommended follow-up when doing larger graph work:
- run search first to validate strongest targets
- update MOCs for major new clusters

## Runtime Reality (Current Implementation)

Current deterministic handler behavior:
- Validates both note paths exist and are markdown files.
- Inserts `[[target-stem]]` into source note under `## Links`.
- Optionally inserts reverse link into target when `bidirectional=true`.
- Skips duplicate insertions if link already exists.

Current handler does not:
- Do semantic connection discovery.
- Rank candidate links.
- Compute graph health score or cluster reports.

Those richer Connector modes remain guidance for multi-turn work.

## Output Expectations

Return:
- source path
- target path
- whether forward/back links were inserted
- bidirectional flag

## Safety

- Both paths must be relative and inside vault root.
- Reject non-markdown files.
