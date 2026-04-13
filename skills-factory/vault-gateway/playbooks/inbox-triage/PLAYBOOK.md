# Inbox Triage Playbook

## Status

- State: active
- Source: `dump_folder/My-Brain-Is-Full-Crew/skills/inbox-triage/SKILL.md`
- Mode: OpenClaw-native gateway flow

## Purpose

- Inspect `00-Inbox` and produce a triage snapshot.
- Classify inbox notes by content type and route to the correct vault location.
- Update MOC files after filing.
- Generate a daily digest summarizing what was processed.
- This is the daily housekeeping workflow that keeps the vault clean and navigable.

## User Profile

Before processing notes, read `Meta/user-profile.md` to understand the user's context, active projects, and preferences. Use this to make better filing decisions.

---

## Triage Workflow

### Step 1: Scan the Inbox

1. List all files in `00-Inbox/`
2. Read each file's YAML frontmatter and content
3. Present a summary to the user:
   ```
   Inbox: {{N}} notes to process

   1. [Meeting] 2026-03-18 — Sprint Planning Q2
   2. [Idea] 2026-03-19 — New Onboarding Approach
   3. [Task] 2026-03-20 — Call Supplier
   ...
   ```

### Step 2: Classify and Route

For each note, determine the destination based on content type and context. **Analyze the full content, not just the frontmatter.** Auto-detect project and area from the text body, mentioned people, topics, and keywords.

| Content Type | Destination | Criteria |
|-------------|-------------|----------|
| Meeting notes | `06-Meetings/{{YYYY}}/{{MM}}/` | Has `type: meeting` in frontmatter |
| Project-related | `01-Projects/{{Project Name}}/` | References an active project |
| Area-related | `02-Areas/{{Area Name}}/` | Relates to an ongoing responsibility |
| Reference material | `03-Resources/{{Topic}}/` | How-tos, guides, reference info |
| Person info | `05-People/` | About a specific person |
| Task/To-do | Extract to daily note or project | Standalone tasks get merged |
| Archivable | `04-Archive/{{Year}}/` | Old, completed, or historical |
| Unclear | Keep in Inbox, flag for user | Ambiguous — ask the user |

### Step 3: Pre-Move Checklist (for each note)

Before moving any note:

1. **Verify destination exists** — create the subfolder if needed
2. **Check for duplicates** — search the destination for notes with similar titles or content
3. **Update frontmatter**: change `status: inbox` to `status: filed`, add `filed-date` and `location` fields
4. **Create/verify wikilinks** in the note body:
   - People: `[[05-People/Name]]`
   - Projects: `[[01-Projects/Project Name]]`
   - Related notes: `[[note title]]`
   - Areas: `[[02-Areas/Area Name]]`
5. **Extract action items** — if the note contains tasks, ensure they're also captured in the relevant Daily Note or project note

### Step 4: Update MOC Files

After filing notes, update the relevant Map of Content files in `MOC/`:

1. Check if a relevant MOC exists for the topic/area/project
2. If yes: add a wikilink to the new note in the appropriate section
3. If no: evaluate if a new MOC is warranted (3+ notes on the same topic = create a MOC)
4. MOC format:
   ```markdown
   ---
   type: moc
   tags: [moc, {{topic}}]
   updated: {{date}}
   ---

   # {{Topic}} — Map of Content

   ## Overview
   {{Brief description}}

   ## Notes
   - [[Note Title 1]] — {{one-line summary}}
   - [[Note Title 2]] — {{one-line summary}}

   ## Related MOCs
   - [[MOC/Related Topic]]
   ```

### Step 5: Generate Digest

After completing triage, produce a digest summary:

```
Triage Complete — {{date}}

Filed:
- "Sprint Planning Q2" -> 06-Meetings/2026/03/
- "New Onboarding Approach" -> 01-Projects/Rebrand/
- "Client Feedback Pricing" -> 02-Areas/Sales/

MOCs Updated:
- MOC/Meetings Q2
- MOC/Rebrand Project

Archive Candidates (not touched in 30+ days):
- [[02-Areas/Marketing/Old Campaign Brief]] — last updated 2026-02-10

Remaining in Inbox (needs your input):
- "random notes" — can't classify, what is this about?

Stats: {{N}} notes filed, {{N}} MOCs updated, {{N}} links created
```

### Step 6: Suggest Archive Candidates

At the end of every triage session, scan active areas for notes not touched in 30+ days:
1. Check `date`, `updated`, and file modification time
2. List candidates with last-touched date
3. Ask the user if any should be moved to `04-Archive/`
4. **Never auto-archive** — always get confirmation

---

## Intelligent Filing Decisions

### Content-Based Detection

Don't rely solely on frontmatter. Analyze the full note:
- Keywords and phrases that indicate a project or area
- People mentioned — which projects are they associated with?
- Temporal context — when was this written and what was the user working on?
- Technical content — notes with code or architecture discussions go to the relevant project

### Learning from Past Decisions

When filing is ambiguous:
1. Search for previously filed notes with similar content
2. Check where similar notes were placed
3. Follow the established pattern
4. If no pattern exists, file provisionally and note the decision

---

## Conflict Resolution

- **Ambiguous destination**: if 2-3 reasonable options exist, ask the user. If the vault is missing the right area entirely, file provisionally in the best available location and flag it
- **Note belongs to multiple areas**: file in the primary location, create wikilinks from secondary locations
- **Duplicate detected**: show both notes to the user, ask which to keep or whether to merge
- **Missing project/area folder**: if it's a minor subfolder, create it. If it's a new area warranting structural design, file in `03-Resources/` temporarily and suggest onboarding the new area

---

## Filing Rules

1. **Never delete notes** — only move them
2. Always preserve the original filename unless it violates naming conventions
3. Rename files to match convention: `YYYY-MM-DD — {{Type}} — {{Title}}.md`
4. Create year/month subfolders for Meetings and Archive: `06-Meetings/2026/03/`
5. Update all internal wikilinks if a note is renamed
6. Use Dataview-compatible frontmatter for all modifications
7. Respect existing tag taxonomy — don't invent new tags without checking `Meta/tag-taxonomy.md`

---

## Operating Principles

1. **Conservative by default** — never delete, only move
2. **Transparent** — always show what was found and what was changed
3. **Respect existing structure** — adapt to the vault as it is, suggest improvements
4. **Log everything** — every change should be traceable in `Meta/vault-gateway-log.md`