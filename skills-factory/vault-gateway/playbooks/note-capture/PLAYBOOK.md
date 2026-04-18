# Note Capture Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/agents/scribe.md`
- Mode: OpenClaw-native gateway flow

## Route Mapping

- Route: `note.capture`
- Payload: `text` (optional), `title` (optional), `target_folder` (optional), `template_hint` (optional), `template_path` (optional), `template_id` (optional), `field_values` (optional object), `template_mode` (optional), `missing_fields_policy` (optional), `append_captured_context` (optional), `tags` (optional)

## Purpose

- Capture fast, rough user input and turn it into a clean Obsidian note.
- Preserve meaning and intent while improving readability.
- Store captures in the vault safely, defaulting to `00-Inbox`.

## MBIFC Method Port (Scribe)

The route follows Scribe principles from MBIFC:

1. Interpret user intent first, then polish form.
2. Preserve substance; only improve clarity and structure.
3. Prefer concise output over over-expanded prose.
4. Use `00-Inbox` as safe landing zone when destination is unclear.

## Capture Modes

### Mode 1: Standard Capture (default)

- Convert user text into a well-titled note.
- Use a short, descriptive title if user did not provide one.
- Keep the body close to original wording and intent.

### Mode 2: Voice-to-Note

Trigger signals:
- Run-on text, missing punctuation, filler words, transcription artifacts.

Behavior:
- Clean punctuation and paragraph breaks.
- Keep names, numbers, and technical terms intact.
- If topics are unrelated, recommend splitting into multiple captures.

### Mode 3: Thread Capture

Trigger signals:
- User sends chained thoughts or explicitly requests thread capture.

Behavior:
- Split into atomic ideas when needed.
- Preserve sequence of ideas.
- Suggest follow-up linking after creation.

### Mode 4: Quote Capture

Trigger signals:
- User is saving a quote, passage, or citation.

Behavior:
- Keep quote text verbatim.
- Add context fields when available: author, source, page/timestamp.

### Mode 5: Reading Notes

Trigger signals:
- User shares notes from a book, article, lecture, or paper.

Behavior:
- Separate source ideas from user reflections when possible.
- Keep a section for key takeaways and action items.

### Mode 6: Brainstorm

Trigger signals:
- Rapid ideation, stream of ideas, explicit brainstorm request.

Behavior:
- Capture breadth first.
- Keep idea list format and avoid premature filtering.

## Content Categories (from MBIFC)

Classify captures where possible:
- Idea / thought
- Task / to-do
- Note / information
- Person note
- Link / reference
- List / collection

If category is uncertain, keep generic note format and leave in inbox.

## Obsidian Integration Rules

- Use Dataview-friendly YAML when creating frontmatter.
- Keep note paths relative to vault root.
- Prefer wikilinks for known people/projects in follow-up enrichment turns.

## Runtime Reality (Current Implementation)

Current deterministic handler behavior:
- Requires `text` when no template is selected.
- Creates target folder if missing.
- Uses `title` if provided, else infers from template fields/metadata, then from `text`.
- Supports template selection by `template_path`, `template_id`, or `template_hint`.
- Supports structured template fill when template metadata defines `vg_fields`.
- If required template fields are missing and policy is `ask`, returns `needs_user_input` with `missing_fields` and does not write the note yet.
- Falls back to legacy template behavior (copy template + captured context) when metadata is absent.
- Adds simple default frontmatter when no template is selected.
- Supports optional `tags` normalization for non-template fallback notes.
- Refreshes structural context files after write: updates `Meta/vault-structure.md` managed block and updates or auto-creates folder `_index.md` (when folder has enough notes).

Current handler does not:
- Auto-split one message into multiple notes.
- Auto-run semantic mode selection beyond provided payload.
- Auto-link to people/projects/MOCs during capture.

Those richer MBIFC behaviors are conversational guidance for Josemar across turns.

## Output Expectations

After execution, return:
- created note relative path
- final title
- template used (if any)
- target folder

## Safety

- Never write outside vault root.
- Never accept absolute or traversal paths for target folders.
- If folder intent is ambiguous, default to `00-Inbox`.
