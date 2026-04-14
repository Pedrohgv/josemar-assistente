# Onboarding Playbook

## Status

- State: active
- Source: `My-Brain-Is-Full-Crew/skills/onboarding/SKILL.md`
- Mode: OpenClaw-native gateway flow

## Purpose

- Initialize a new vault with standard baseline structure.
- Support deterministic "port existing vault" workflow.
- Enforce destructive safety gate with explicit backup confirmation.
- Guide Josemar through a conversational onboarding that shapes the vault around the user's life and work.

## Foundational Principle

The user never manually organizes the vault. Josemar is the sole custodian of vault order. This means:
- Every note must have a home; every folder must have a purpose.
- If the user mentions a project, area, or goal and the vault doesn't have a home for it, create the structure now.
- All structural changes are logged in `Meta/vault-gateway-log.md`.

---

## Onboarding Flow

### Phase 1: Choose Path

Ask the user to choose between:
- `novo vault` — create a fresh vault from scratch
- `port existing vault` — adapt an existing vault to the baseline structure

If `Meta/user-profile.md` already exists, the vault was initialized before. Offer to:
- Re-run onboarding (overwrite profile)
- Update specific sections
- Skip onboarding entirely

### Phase 2: Collect Preferences (for both paths)

Before executing any structural change, gather context through conversation. Ask one question at a time:

1. **Preferred name** — "Como quer ser chamado?"
2. **Primary language** — "Qual idioma prefere para interacoes?"
3. **Role/occupation** — "O que voce faz? Estudante, profissional, pesquisador, criativo?"
4. **Motivation** — "O que te trouxe aqui? Qual problema quer resolver?"
5. **Life areas** — "Quais areas da sua vida quer gerenciar no vault?" Common options:
   - Work (job projects, meetings, professional development)
   - Finance (budgets, expenses, investments)
   - Learning (courses, books, certifications, research)
   - Personal (hobbies, relationships, goals, journaling)
   - Side Projects (freelance, startups, creative endeavors)
   - Custom — any area the user describes
6. **Deep-dive per area** — for each selected area, ask one follow-up to shape the sub-structure:
   - Work: "Quantos cargos/projetos? Um ou multiplos?"
   - Finance: "Que aspecto quer rastrear? orcamento, investimentos, impostos?"
   - Learning: "Que tipo? cursos online, faculdade, livros, certificacoes?"
   - Personal: "O que significa 'pessoal' para voce? hobbies, diario, viagens?"
   - Side Projects: "Que tipo? freelance, startup, open source, criativo?"
   - Custom: "Me conte mais sobre [area] — que tipo de notas vai guardar la?"

### Phase 3: Confirm and Execute

Summarize everything the user told you. Ask them to confirm or correct. Then execute the appropriate port mode.

---

## Safety Gates

The gateway handles these deterministically, but Josemar should explain them conversationally:

- **Backup confirmation phrase**: `eu tenho backup e quero continuar` — required before destructive port can proceed
- **Non-destructive execution phrase**: `aprovar port nao destrutivo`
- **Destructive execution phrase**: `executar port destrutivo`

### Destructive Flow

If the user requests destructive mode:
1. Emit strong warning: destructive mode can move content to `04-Archive/` and alter root organization
2. Strongly recommend a complete vault backup before continuing
3. Require exact backup confirmation phrase
4. Show the deterministic plan
5. Require exact execution confirmation phrase

### Non-destructive Flow

1. Show the deterministic plan
2. Require exact approval phrase before execution

---

## Deterministic Plan Output

The gateway produces a plan with:
- Whether vault exists
- Missing baseline directories
- Non-standard top-level entries
- Exact action list for selected mode (create dirs, create baseline files, move entries)

---

## Vault Baseline Structure

```
Vault/
├── 00-Inbox/
├── 01-Projects/
├── 02-Areas/
│   └── {{Area Name}}/          ← One per selected life area
│       ├── {{Sub-Area}}/       ← Based on Phase 2 deep-dive
│       └── _index.md
├── 03-Resources/
├── 04-Archive/
├── 05-People/
├── 06-Meetings/
│   └── {{current year}}/
├── 07-Daily/
├── MOC/
│   ├── Index.md                ← Master MOC
│   └── {{Area Name}}.md        ← One MOC per area
├── Templates/
│   ├── Meeting.md
│   ├── Idea.md
│   ├── Task.md
│   ├── Note.md
│   ├── Person.md
│   ├── Project.md
│   ├── Area.md
│   ├── MOC.md
│   ├── Daily Note.md
│   ├── Weekly Review.md
│   └── {{Area-specific}}.md    ← e.g., Book.md, Budget Entry.md
└── Meta/
    ├── user-profile.md
    ├── vault-structure.md
    ├── naming-conventions.md
    ├── tag-taxonomy.md
    ├── agent-log.md
    ├── health-reports/
    └── vault-gateway-log.md
```

Only create areas the user actually selected in Phase 2. Do not create empty placeholders for unused areas.

---

## User Profile Format

Save to `Meta/user-profile.md` after onboarding completes:

```markdown
---
name: "{{preferred name}}"
primary-language: "{{language code}}"
role: "{{role/occupation}}"
motivation: "{{what brought them here}}"
life-areas: [{{list of areas}}]
onboarding-date: "{{YYYY-MM-DD}}"
profile-version: 1
---

# User Profile

Single source of truth for Josemar vault operations.

## Personal
- **Name**: {{preferred name}}
- **Role**: {{role}}
- **Primary Language**: {{language}}

## Vault Configuration
- **Life Areas**: {{list}}

## Notes
{{Any additional context from the conversation}}
```

---

## Area Scaffolding Procedure

When creating a new area (during onboarding or later), follow these steps:

1. **Create the folder structure** — area folder under `02-Areas/` with sub-folders based on user's description
2. **Create the area index note** — `_index.md` with purpose, active projects, sub-areas, key resources, and link to its MOC
3. **Create the area MOC** — `MOC/{{Area Name}}.md` with overview, structure, key notes, active projects
4. **Update the Master MOC** — add link to new area MOC in `MOC/Index.md`
5. **Create area-specific templates** — if the area needs specialized templates (e.g., Finance needs Budget Entry and Investment), create them in `Templates/`
6. **Update `Meta/vault-structure.md`** — document the new area, its sub-folders, and its purpose
7. **Update `Meta/tag-taxonomy.md`** — add area-specific tags (e.g., `#area/finance`, `#budget`)

### Area Index Template

```markdown
---
type: area
date: "{{today}}"
tags: [area, {{area-tag}}]
---

# {{Area Name}}

## Purpose
{{Brief description}}

## Active Projects
{{Links to projects in this area}}

## Sub-Areas
{{Links to sub-folders}}

## Key Resources
{{Links to important reference notes}}

## MOC
→ [[MOC/{{Area Name}}]]
```

### Area MOC Template

```markdown
---
type: moc
date: "{{today}}"
tags: [moc, {{area-tag}}]
---

# {{Area Name}} — Map of Content

## Overview
{{Description}}

## Structure
{{List of sub-folders and their purpose}}

## Key Notes
{{Will be populated as notes are added}}

## Active Projects
{{Links to active projects}}

## Related MOCs
- [[MOC/Index|Master Index]]
```

---

## Core Templates

Create the following templates in `Templates/` during onboarding. Each uses YAML frontmatter compatible with Dataview queries.

### Meeting.md
```markdown
---
type: meeting
date: ""
attendees: []
project: ""
tags: [meeting]
status: inbox
---

# {{Title}}

## Attendees
-

## Agenda
1.

## Notes


## Action Items
- [ ]

## Decisions Made


## Follow-up
```

### Idea.md
```markdown
---
type: idea
date: ""
tags: [idea]
status: inbox
---

# {{Title}}

## The Idea


## Why It Matters


## Next Steps
- [ ]
```

### Task.md
```markdown
---
type: task
date: ""
due: ""
priority: medium
project: ""
tags: [task]
status: inbox
---

# {{Title}}

## Description


## Acceptance Criteria
- [ ]

## Notes
```

### Note.md
```markdown
---
type: note
date: ""
tags: [note]
status: inbox
---

# {{Title}}


## Related
```

### Person.md
```markdown
---
type: person
name: ""
role: ""
organization: ""
tags: [person]
---

# {{Title}}

## About


## Interactions


## Notes
```

### Project.md
```markdown
---
type: project
date: ""
status: active
priority: medium
deadline: ""
tags: [project]
---

# {{Title}}

## Objective


## Key Results
- [ ]

## Tasks
- [ ]
```

### Area.md
```markdown
---
type: area
date: ""
tags: [area]
---

# {{Title}}

## Purpose


## Active Projects


## Key Resources


## Notes
```

### MOC.md
```markdown
---
type: moc
date: ""
tags: [moc]
---

# {{Title}} — Map of Content

## Overview


## Key Notes


## Related MOCs
```

### Daily Note.md
```markdown
---
type: daily
date: ""
tags: [daily]
---

# {{Date}}

## Morning Intention


## Tasks
- [ ]

## Notes



## End of Day Reflection
```

### Weekly Review.md
```markdown
---
type: weekly-review
date: ""
week: ""
tags: [weekly-review]
---

# Weekly Review — {{Week}}

## What Went Well


## What Didn't Go Well


## Key Accomplishments
-

## Open Loops
- [ ]

## Priorities for Next Week
1.
2.
3.
```

### Area-Specific Templates (conditional)

Create these only if the corresponding area was selected:

- **Work Log.md** — if "work" selected
- **Budget Entry.md** — if "finance" selected
- **Investment.md** — if "finance" selected
- **Book.md** — if "learning" selected
- **Course.md** — if "learning" selected
- **Journal Entry.md** — if "personal" selected

Use the same YAML-frontmatter + markdown-body pattern as core templates.

---

## Execution Behavior

- **Non-destructive**: create missing baseline dirs and files only. Non-standard root entries stay in place.
- **Destructive**: non-destructive baseline + move non-standard root entries into `04-Archive/Imported-Root-<timestamp>`.
- **Every execution** appends to `Meta/vault-gateway-log.md`.

---

## Onboarding Checklist

Before telling the user onboarding is complete, verify:

- [ ] `Meta/user-profile.md` exists and is complete
- [ ] `Meta/vault-structure.md` exists
- [ ] `Meta/tag-taxonomy.md` exists with area-specific tags
- [ ] `00-Inbox/` exists
- [ ] `01-Projects/` exists
- [ ] `02-Areas/` has a sub-folder for each selected life area
- [ ] Each area has `_index.md`
- [ ] Each area has a corresponding MOC in `MOC/`
- [ ] `03-Resources/` exists
- [ ] `04-Archive/` exists
- [ ] `05-People/` exists
- [ ] `06-Meetings/{{current year}}/` exists
- [ ] `07-Daily/` exists
- [ ] `MOC/Index.md` exists and links to all area MOCs
- [ ] `Templates/` has all core templates
- [ ] `Templates/` has area-specific templates for selected areas
- [ ] Welcome note exists in `00-Inbox/`
