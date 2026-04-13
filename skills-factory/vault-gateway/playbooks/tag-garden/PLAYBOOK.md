# Tag Garden Playbook

## Status

- State: active
- Source: `dump_folder/My-Brain-Is-Full-Crew/skills/tag-garden/SKILL.md`
- Mode: OpenClaw-native gateway flow

## Purpose

- Focused tag analysis and cleanup.
- Identifies unused, orphan, near-duplicate, over-used, and under-used tags.
- Suggests merges and taxonomy updates.
- References `Meta/tag-taxonomy.md` as the canonical source of truth for valid tags.

## User Profile

Before starting, read `Meta/user-profile.md` to understand the user's context, preferences, and active projects.

---

## Tag Garden Workflow

### Step 1: Collect All Tags

1. List all tags used in the vault with usage counts
2. Read `Meta/tag-taxonomy.md` for the canonical tag list
3. Compare actual usage against the taxonomy

### Step 2: Identify Issues

Categorize all tag issues:

- **Unused tags**: defined in taxonomy but never used in any note
- **Orphan tags**: used in notes but not defined in `Meta/tag-taxonomy.md`
- **Near-duplicate tags**: tags that are likely the same thing (#marketing, #mktg, #market)
- **Over-used tags**: tags on 50%+ of notes (too broad to be useful)
- **Under-used tags**: tags on only 1-2 notes (probably typos or too specific)

### Step 3: Suggest Actions

For each issue category, provide specific actionable suggestions:
- Merge near-duplicates (specify which tag to keep)
- Add orphan tags to taxonomy (if legitimate) or correct them (if typos)
- Split over-used tags into more specific sub-tags
- Remove or merge under-used tags

### Step 4: Visualize Distribution

Provide a tag usage distribution showing:
- Top tags by usage count
- Tags per category/area
- Tag growth trends (if previous reports exist)

---

## Tag Garden Report Format

```
Tag Garden Report — {{date}}

Total unique tags: {{N}}
Tags in taxonomy: {{N}}
Orphan tags (not in taxonomy): {{N}}

Top Tags:
1. #{{tag}} — {{N}} notes
2. #{{tag}} — {{N}} notes
3. #{{tag}} — {{N}} notes

Suggested Merges:
- #marketing + #mktg -> #marketing ({{N}} notes affected)
- #dev + #development -> #development ({{N}} notes affected)

Possibly Unused:
- #{{tag}} — 0 uses, in taxonomy since {{date}}

Possibly Too Broad:
- #{{tag}} — used on {{N}}% of notes, consider splitting

Possibly Typos:
- #{{tag}} — only 1 use, did you mean #{{similar-tag}}?

Want me to apply the suggested merges?
```

---

## Tag Format Standards

When evaluating tags, enforce these standards:
- **Lowercase**: all tags should be lowercase
- **Hyphenated**: multi-word tags use hyphens (e.g., `#project-management`, not `#projectManagement`)
- **No spaces**: tags should not contain spaces
- **Consistent naming**: prefer full words over abbreviations unless the abbreviation is universally understood

---

## Automated Fix Suggestions

When presenting issues, offer a clear fix path:

```
Found {{N}} auto-fixable tag issues:

1. [Fix] Merge #dev -> #development (3 notes)
2. [Fix] Merge #mktg -> #marketing (5 notes)
3. [Fix] Normalize #ProjectManagement -> #project-management (2 notes)
4. [Fix] Add 4 orphan tags to Meta/tag-taxonomy.md

Apply all {{N}} fixes? [Yes / Let me review each / Skip]
```

---

## Operating Principles

1. **Conservative by default** — never delete tags without asking. Always present merges as suggestions first.
2. **Transparent** — always show what was found and what would change
3. **Batch confirmations** — group similar changes together for user approval
4. **Respect existing taxonomy** — adapt to the vault's tag conventions, suggest improvements
5. **Reference `Meta/tag-taxonomy.md`** — this is the canonical source of truth for valid tags