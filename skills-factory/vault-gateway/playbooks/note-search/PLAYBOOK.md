# Note Search Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/seeker.md`
- Mode: OpenClaw-native gateway flow

## Route Mapping

- Route: `note.search`
- Payload: `query` (required), `limit` (optional), `path_prefix` (optional)

## Purpose

- Retrieve relevant notes quickly from vault content.
- Support lookup, recall, and follow-up actions.
- Port MBIFC Seeker search strategy into gateway route guidance.

## MBIFC Method Port (Seeker Search)

Primary search strategies:

1. Full-text search (title/path/body).
2. Metadata-oriented filtering mindset (type/tags/date/status) when context requires.
3. Relationship-aware reading (backlinks/forward links) for follow-up turns.
4. Fuzzy broadening if strict match fails.

## Search Presentation Rules

When reporting results:
- Rank strong matches first.
- Include relative path for context.
- Include one short snippet where query matched.
- Distinguish archived/older context when relevant.

When no results:
- Suggest adjacent terms or broader scope.
- Suggest `path_prefix` removal if search was too narrow.

## Runtime Reality (Current Implementation)

Current deterministic handler behavior:
- Requires `query`.
- Searches markdown files recursively.
- Optional `path_prefix` limits search scope.
- Scoring model: title stem > path > content.
- Returns snippet from first matching line.
- Clamps `limit` to safe range.

Current handler does not:
- Run semantic/vector retrieval.
- Parse frontmatter fields as structured filters.
- Compute graph/backlink intelligence.

Those MBIFC Seeker capabilities remain conversational/iterative guidance.

## Output Expectations

Return:
- query
- result list with `path`, `title`, `score`, `snippet`
- total match count

## Safety

- `path_prefix` must resolve inside vault root.
- only markdown files are scanned.
