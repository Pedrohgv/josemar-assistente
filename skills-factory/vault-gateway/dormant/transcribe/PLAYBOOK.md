# Transcribe Playbook (Dormant)

## Status

- State: dormant
- Source: `My-Brain-Is-Full-Crew/skills/transcribe/SKILL.md`
- Phase: scaffold (backend unavailable)

## Purpose

- Preserve MBIFC transcription workflow for future activation.
- Process audio recordings, meeting transcripts, podcasts, lectures, interviews, and voice memos into richly structured Obsidian notes.
- Every output lands in `00-Inbox/` for later triage.

## Activation Prerequisites

- Configure and validate a transcription backend (e.g., Whisper, aux-ml service)
- Add runtime integration contract and timeout policy
- Add regression tests for transcript-to-note flow
- Set `TRANSCRIBE_ENABLED=true` in feature flags

## Current Behavior

- `vault-gateway` returns a clear dormant status message when transcription is requested.
- No routing path executes transcription actions until backend is configured.

---

## Future Workflow (When Activated)

### Intake Interview

Before processing any recording, gather context:

1. **Date & time** of the recording (default: today)
2. **Processing mode**: Meeting, Lecture Notes, Podcast Summary, Interview Extraction, Voice Journal, or General Transcription
3. **Participants / Speakers**: names and roles (if applicable)
4. **Project / area** the recording relates to (if any)
5. **Language**: detect automatically, or ask if ambiguous
6. **Priority flags**: is there anything urgent the user already knows about?
7. **Transcript format**: if providing text, ask which tool generated it (Whisper, Otter, Google Meet, Zoom, manual, or unknown)

Skip questions the user has already answered in their message. If the user says "quick" or similar, ask only for date and participants.

### Processing Modes

- **Meeting Notes** — standard meeting processing with executive summary, key points, decisions, action items, follow-up email draft
- **Lecture Notes** — academic/course content with key concepts, definitions, detailed notes, exam-relevant points
- **Podcast Summary** — TL;DR, key insights, notable quotes, detailed breakdown, resources mentioned
- **Interview Extraction** — structured Q&A, key takeaways, notable quotes, follow-up questions
- **Voice Journal** — personal reflections, stream of thought (structured), insights, questions to self
- **General Transcription** — clean transcript with executive summary and key points

### Action Item Extraction

For all modes with action items:

1. **Explicit actions**: directly stated commitments
2. **Implicit actions**: inferred from context
3. **Conditional actions**: dependent on other events
4. **Confidence scores**: high (explicitly stated), medium (implied), low (inferred)
5. **Deadline detection**: extract mentioned dates and relative timeframes
6. **Flag unassigned actions**: tasks without an owner

### File Naming Convention

`YYYY-MM-DD — {{Type}} — {{Short Title}}.md`

### Writing Rules

- Write the note structure in the same language the user writes in
- Transform rambling speech into concise, scannable prose
- Preserve exact quotes for important statements (use `> blockquote`)
- Tag action items with `[[Name]]` wikilinks to `05-People/`
- Add `#followup` tag to notes requiring action within 48 hours
- For voice journals, preserve the personal and reflective tone
- Use YAML frontmatter compatible with Dataview queries
- Save to `00-Inbox/` for later triage
