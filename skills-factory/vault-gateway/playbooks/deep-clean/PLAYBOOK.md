# Deep Clean Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/skills/deep-clean/SKILL.md`
- Mode: OpenClaw-native gateway flow

## Purpose

- Most thorough maintenance mode: full 7-phase audit PLUS extended deep-cleaning passes.
- Stale content, outdated references, content quality, redundant tags, broken external links, template compliance.
- Run this when the vault needs more than a routine defrag or audit.

## User Profile

Before starting, read `Meta/user-profile.md` to understand the user's context, preferences, and active projects.

---

## Stage 1: Full 7-Phase Audit

Run the complete vault-audit workflow first:

1. **Structural Scan** — verify folder hierarchy, detect orphans and misplaced files
2. **Duplicate Detection** — exact matches, "(updated)" variants, similar content, sync conflicts
3. **Link Integrity** — broken links, orphan notes, path mismatches, alias inconsistencies
4. **Frontmatter Audit** — missing fields, invalid values, tag consistency, status hygiene
5. **MOC Review** — completeness, broken links, stale MOCs, missing MOCs
6. **Cross-Reference** — check operation logs, compare with previous reports
7. **Health Report** — generate comprehensive report at `Meta/health-reports/{{date}} — Vault Health.md`

(See vault-audit playbook for details on each phase.)

---

## Stage 2: Extended Deep-Clean Passes

After completing the 7-phase audit, run these additional passes:

### Pass 1: Stale Content Scan

Find notes not updated in 60+ days in active areas:

1. Scan active areas (not Archive) for notes with old modification dates
2. Categorize by staleness:
   - **30-60 days**: possibly stale, flag for review
   - **60-90 days**: likely stale, suggest archiving
   - **90+ days**: almost certainly stale unless it's reference material
3. Exclude reference material and templates from staleness checks
4. Cross-reference with link activity — a stale note that's frequently linked is still valuable

Report:
```
Stale Content Report — {{date}}

Likely Stale (60-90 days, suggest archiving):
- [[Note 1]] — last updated {{date}}, in {{location}}, linked from {{N}} notes

Possibly Stale (30-60 days, review recommended):
- [[Note 3]] — last updated {{date}}, {{reason it might still be relevant}}

Ancient but Still Referenced (keep):
- [[Note 4]] — last updated {{date}}, linked from {{N}} recent notes

Want me to move the stale notes to Archive?
```

### Pass 2: Outdated References

Find notes referencing completed projects, past events, or expired deadlines:

1. Scan for notes that reference projects marked `status: completed` or `status: archived`
2. Find notes with dates in the past that reference future events
3. Identify expired deadlines and action items that were never completed
4. Suggest updates or archiving for each

### Pass 3: Content Quality

Find notes that are low-quality or incomplete:

1. Notes that are just a title with no content (empty body)
2. Notes that are just a URL with no context or summary
3. Notes with only 1-2 sentences that could be merged with related notes
4. Notes with broken formatting (unclosed code blocks, malformed YAML)

For each: suggest whether to expand, merge, or archive.

### Pass 4: Redundant Tags

Find tags that add no value:

1. Tags used on only 1 note (probably a typo or too specific)
2. Tags that are synonyms of other tags (#marketing, #mktg, #market)
3. Tags not in `Meta/tag-taxonomy.md` (orphan tags)
4. Tags used on 50%+ of notes (too broad to be useful)

Suggest merges, deletions, and taxonomy updates.

### Pass 5: Broken External Links

Check URLs in notes for validity:

1. Scan notes for external URLs (http/https links)
2. Flag URLs that are likely broken (404, domain expired)
3. Suggest alternatives or removal

### Pass 6: Template Compliance

Check if notes follow the expected template for their type:

1. Read expected templates from `Templates/`
2. Compare each note's structure against its type's template
3. Flag notes missing required sections
4. Suggest reformatting for non-compliant notes

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
3. Flag regressions
4. Include trend data in every new health report

---

## Operating Principles

1. **Conservative by default** — never delete, only archive. Never auto-merge, always ask.
2. **Transparent** — always show what was found and what was changed
3. **Batch confirmations** — group similar changes together for user approval
4. **Respect existing structure** — adapt to the vault as it is, suggest improvements
5. **Log everything** — every change should be traceable in `Meta/vault-gateway-log.md`
