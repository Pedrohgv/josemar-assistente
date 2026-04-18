# Vault Audit Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/skills/vault-audit/SKILL.md`
- Mode: OpenClaw-native gateway flow

## Purpose

- Comprehensive 7-phase vault health check.
- Structural scan, duplicate detection, link integrity, frontmatter audit, MOC review, cross-reference, health report.
- Tracks trends over time by comparing against previous reports.

## User Profile

Before starting any audit, read `Meta/user-profile.md` to understand the user's context, preferences, and active projects.

---

## Full Audit Workflow

### Phase 1: Structural Scan

Scan the entire vault directory structure:

1. **Verify folder hierarchy** matches the canonical structure in `Meta/vault-structure.md`
2. **Detect orphan folders** — empty directories or folders not in the expected structure
3. **Find misplaced files** — notes in the wrong location based on their `type` frontmatter
4. **Check for files outside the structure** — anything in the vault root that should be in a folder

Report:
```
Vault Structure

Folders compliant: {{N}}/{{N}}
Empty folders: {{list}}
Misplaced files: {{N}} notes found in wrong location
```

### Phase 2: Duplicate Detection

Search for duplicate or near-duplicate content:

1. **Exact filename matches** — files with identical names in different folders
2. **"(updated)" or "(copy)" variants** — files like `Note (updated).md`, `Note 2.md`, `Note (1).md`
3. **Similar content** — notes with >70% content overlap
4. **Sync conflicts** — `Note (conflict).md` files from Obsidian sync

For each duplicate:
- Read both versions completely
- Identify which is more recent/complete (check `date`, `updated`, file modification time)
- Present a comparison to the user with a recommendation
- **Never auto-merge** — always ask the user for confirmation

### Phase 3: Link Integrity

Audit all wikilinks in the vault:

1. **Broken links** — `[[Note Title]]` that point to non-existent notes
2. **Orphan notes** — notes with zero incoming links
3. **Incorrect paths** — links that don't match the actual file path
4. **Alias inconsistencies** — same person/concept linked differently across notes

Fixes:
- If the target note was moved, update the link
- If the target note was deleted, ask the user
- If it's a typo, fix it
- For orphan notes, suggest connections based on content/tags

### Phase 4: Frontmatter Audit

Check YAML frontmatter consistency:

1. **Missing required fields** — every note should have at minimum: `type`, `date`, `tags`, `status`
2. **Invalid values** — dates in wrong format, unknown types, malformed tags
3. **Tag consistency** — check against `Meta/tag-taxonomy.md`, flag unknown tags
4. **Status hygiene** — notes still marked `status: inbox` but not in Inbox folder

Auto-fix (without asking):
- Date format normalization (all to YYYY-MM-DD)
- Tag format normalization (lowercase, hyphenated)
- Add missing `status` field based on file location

Ask before fixing:
- Missing `type` field (need user input)
- Unknown tags (add to taxonomy or correct?)

### Phase 5: MOC Review

Audit all Map of Content files:

1. **Completeness** — every filed note should be reachable from at least one MOC
2. **Broken MOC links** — links in MOCs pointing to moved/deleted notes
3. **Stale MOCs** — MOCs not updated in >30 days with new notes available
4. **Missing MOCs** — clusters of 3+ notes on the same topic without a MOC

### Phase 6: Cross-Reference

Pull insights from across the vault:

1. Check `Meta/vault-gateway-log.md` for recent operations
2. Cross-reference findings — if link integrity found orphans, check if they should be in a MOC
3. If previous health reports exist, compare trends

### Phase 7: Health Report

Generate a comprehensive vault health report at `Meta/health-reports/{{date}} — Vault Health.md`:

```markdown
---
type: report
date: {{date}}
tags: [meta, vault-health, report]
---

# Vault Health Report — {{date}}

## Summary
- Total notes: {{N}}
- Health score: {{percentage}}
- Trend: {{improving/stable/declining}} (vs last report)

## Structure
- Folders: {{OK count}}/{{total}}
- Misplaced files: {{count}} (fixed: {{count}})
- Empty folders: {{count}}

## Duplicates
- Found: {{count}}
- Awaiting user decision: {{count}}

## Links
- Broken links fixed: {{count}}
- Orphan notes found: {{count}}
- New connections suggested: {{count}}

## Frontmatter
- Notes audited: {{count}}
- Issues found: {{count}}
- Auto-fixed: {{count}}

## MOC Status
- MOCs up to date: {{count}}/{{total}}
- MOCs updated: {{count}}
- New MOCs created: {{count}}

## Tag Health
- Total tags: {{count}}
- Orphan tags: {{count}}
- Suggested merges: {{count}}

## Recommendations
{{Specific, actionable suggestions, ordered by impact}}
```

---

## Automated Fix Suggestions

When presenting issues, offer a clear fix path:

```
Found {{N}} auto-fixable issues:

1. [Fix] Rename "note (updated).md" -> "note.md" (archive old version)
2. [Fix] Add missing `status: filed` to 5 notes in 01-Projects/
3. [Fix] Normalize 8 dates from DD/MM/YYYY to YYYY-MM-DD
4. [Fix] Merge tags: #dev -> #development (3 notes)

Apply all {{N}} fixes? [Yes / Let me review each / Skip]
```

---

## Monthly Trend Analysis

When 2+ health reports exist, compare them:

1. Track key metrics over time (health score, orphan rate, link density, note count)
2. Identify trends: is the vault getting healthier or deteriorating?
3. Flag regressions ("Link density has been declining for 3 weeks")
4. Include trend data in every new health report

---

## Operating Principles

1. **Conservative by default** — never delete, only archive. Never auto-merge, always ask.
2. **Transparent** — always show what was found and what was changed
3. **Batch confirmations** — group similar changes together for user approval
4. **Respect existing structure** — adapt to the vault as it is, suggest improvements
5. **Log everything** — every change should be traceable in `Meta/vault-gateway-log.md`
