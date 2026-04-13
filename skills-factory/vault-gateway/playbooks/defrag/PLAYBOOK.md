# Defrag Playbook

## Status

- State: active
- Source: `dump_folder/My-Brain-Is-Full-Crew/skills/defrag/SKILL.md`
- Mode: OpenClaw-native gateway flow

## Purpose

- Weekly structural maintenance of the vault.
- 5-phase workflow: structural audit, tag hygiene, MOC refresh, structure evolution, report.
- This is a **structural** operation — not a quality audit (that is vault-audit). Focus on organizational skeleton, not content quality.

---

## The 5-Phase Defragmentation Workflow

### Phase 1: Structural Audit

1. **Scan `00-Inbox/`** — anything older than 48 hours still in Inbox is a failure. File it or flag it for the user.
2. **Scan `02-Areas/`** — for each area:
   - Does it have an `_index.md`? If not, create it.
   - Does it have a corresponding MOC in `MOC/`? If not, create it.
   - Are sub-folders still relevant? New clusters of notes that warrant a new sub-folder?
   - Notes that clearly belong to a different area? Move them.
3. **Scan `01-Projects/`** — completed projects that should be archived to `04-Archive/`?
4. **Scan `03-Resources/`** — resources that now belong to a specific area? Move them.
5. **Scan `MOC/`** — is the Master Index up to date? All area MOCs linked? Orphan MOCs with no corresponding area?
6. **Scan `Templates/`** — unused templates? Note types that lack a template?

### Phase 2: Tag Hygiene

1. Scan all notes for tags not listed in `Meta/tag-taxonomy.md` — either add them to the taxonomy or fix them
2. Look for tag synonyms (e.g., `#ml` and `#machine-learning`) — consolidate
3. Ensure hierarchical tags are consistent (all area tags use `#area/` prefix)

### Phase 3: MOC Refresh

1. For each MOC, verify that it actually links to the notes it should
2. Add links to new notes created since the last defrag
3. Remove links to notes that were archived or deleted
4. Verify that the Master Index (`MOC/Index.md`) links to every area MOC

### Phase 4: Structure Evolution

1. Check `Meta/user-profile.md` — has the user's situation changed? New jobs, interests, or goals mentioned in recent notes?
2. If you find a cluster of 3+ notes on a topic that has no dedicated area or sub-folder, **create the structure proactively** using the Area Scaffolding Procedure (see onboarding playbook)
3. Update `Meta/vault-structure.md` with all changes

### Phase 5: Report

Create a defragmentation report at `Meta/health-reports/YYYY-MM-DD — Defrag Report.md`:

```markdown
---
type: report
date: "{{today}}"
tags: [report, defrag, maintenance]
---

# Vault Defragmentation Report — {{date}}

## Summary
- Files moved: {{count}}
- Structures created: {{list}}
- Tags fixed: {{count}}
- MOCs updated: {{list}}
- Inbox items triaged: {{count}}
- Projects archived: {{list}}

## Structural Changes
{{Detailed list of what was created, moved, renamed, or archived}}

## Recommendations
{{Suggestions for the user — new areas to consider, templates to create, etc.}}

## Next Defrag
{{Anything to watch for next week}}
```

Log the defrag in `Meta/vault-gateway-log.md`.

---

## Area Scaffolding Procedure

When Phase 4 detects a new area or sub-area is needed, follow these 7 steps (same as onboarding playbook):

1. Create the folder structure under `02-Areas/` with appropriate sub-folders
2. Create the area index note (`_index.md`)
3. Create the area MOC at `MOC/{{Area Name}}.md`
4. Update the Master MOC in `MOC/Index.md`
5. Create area-specific templates in `Templates/`
6. Update `Meta/vault-structure.md`
7. Update `Meta/tag-taxonomy.md`

---

## Operating Principles

1. **Conservative by default** — never delete notes, only archive or move
2. **Transparent** — always show what was found and what was changed
3. **Respect existing structure** — adapt to the vault as it is, suggest improvements
4. **Log everything** — every change should be traceable in `Meta/vault-gateway-log.md`

## Output Format

1. Announce the defrag is starting
2. Execute each phase, reporting findings as you go
3. Generate the report file at `Meta/health-reports/`
4. Log the operation in `Meta/vault-gateway-log.md`
5. Summarize results to the user with key metrics (files moved, structures created, tags fixed, MOCs updated)