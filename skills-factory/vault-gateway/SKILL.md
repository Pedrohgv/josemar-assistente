---
name: vault-gateway
description: Single entrypoint for Obsidian vault operations in Josemar. Provides note capture/read/update/file/rename/link/search, template discovery, and vault maintenance routes.
---

# Vault Gateway

Use this skill as the only public interface for vault operations. Josemar must always prioritize vault-gateway routes over direct filesystem access, scripts, imports, or ad hoc programmatic workarounds.

If the gateway fails, rejects the request, or does not cover the intended operation, stop and report the limitation to the user. Do not access `/opt/data/obsidian/` directly, import internal modules, write custom scripts, or use other workarounds unless the user explicitly permits that workaround after being informed of the gap.

## Scope

- Route vault requests through a single executable.
- Treat the gateway executable and its routes as the only approved vault tools.
- Keep external integrations (gmail/calendar/contacts) out of this bundle.
- Keep transcription capability present but dormant until backend setup is complete.
- Caller must provide explicit `route` in JSON input.

## How to Invoke

The gateway is a standalone executable that reads JSON from stdin. Pipe your request directly:

```bash
echo '{"route":"note.read","payload":{"path":"07-Daily/2026-05-31.md"}}' | /opt/josemar/skills/vault-gateway/vault-gateway
```

**Do NOT import internal modules directly** (`lib/handlers.py`, `lib/router.py`, etc.) — always use the executable via stdin. Internal modules are implementation details, not a public API. If the executable cannot perform the needed operation, return to the user for explicit permission before using any workaround.

## Routes at a Glance

| Route | Purpose | Frequency |
|---|---|---|
| `note.capture` | Create a new note | High |
| `note.read` | Read full note content and frontmatter | High |
| `note.update` | Update an existing note | High |
| `note.file` | Move note to a different folder | Medium |
| `note.rename` | Rename note in place (and optionally rewrite wikilinks) | Medium |
| `note.search` | Search notes by text | Medium |
| `note.link` | Insert a wikilink between two notes | Medium |
| `template.list` | Discover available templates | Low |
| `template.inspect` | Inspect one template schema | Low |
| `onboarding` | Initialize/port a vault (multi-turn) | Low (one-time) |
| `inbox.triage` | Triage and file 00-Inbox notes | Low |
| `vault.defrag` | Weekly structural maintenance | Low |
| `vault.audit` | Comprehensive 7-phase health check | Low |
| `vault.deep-clean` | Audit + extended cleaning passes | Low |
| `tags.garden` | Tag taxonomy cleanup | Low |
| `transcribe` | (dormant) | Future |

`note.create` is an alias for `note.capture`.

## Input Contract

Always provide structured input with explicit route:

```json
{
  "route": "note.capture",
  "payload": {
    "text": "anote que hj tive uma conversa com o cliente Claudio",
    "title": "Reuniao com Claudio",
    "target_folder": "00-Inbox",
    "template_hint": "meeting"
  }
}
```

Contract rules:
- Top-level keys: only `route` and `payload`
- Route params must be inside `payload`
- Unknown payload keys are rejected
- If `route` is missing or unknown, the skill returns an error with `available_routes`
- For `onboarding`, include a session-scoped `state_key` in payload to isolate multi-turn state

---

# High-Traffic Routes (full guidance)

These routes are the ones the LLM uses most often. Their full MBIFC orchestration guidance is documented inline below so the contract is self-contained — no need to read external files.

## note.capture

Create a new markdown note in the vault. Alias: `note.create`.

**Contract:**
- `text` (string, the note body) — required when no template is selected
- `title` (string, becomes the filename after slugification) — required when no template is selected
- `target_folder` (string, default `00-Inbox`)
- `template_hint` | `template_path` | `template_id` (any of these makes `text` and `title` optional)
- `tags` (list[string])
- `field_values` (object, requires a template)
- `template_mode` (`legacy` | `auto` | `strict` | `off`, default `legacy`)
- `missing_fields_policy` (`ask` | `fail` | `defaults`, default `ask`)
- `append_captured_context` (boolean, default `true`)

**The `title` rule is critical.** `title` is the title AND the resulting filename (after slugification). The gateway rejects `note.capture` calls without `title` whenever no template is selected. Do not rely on body-text derivation — it produces broken filenames for non-ASCII input (e.g. `casa e decoração` → `casa-e-decorao-decorao-e-ambientes`). If you ever get a note with a bad filename, use `note.rename` to fix it.

**Template-based capture example** (no `text` or `title` needed; template's `vg_title` provides the title):

```json
{
  "route": "note.capture",
  "payload": {
    "template_id": "client-v1",
    "field_values": {
      "client_name": "Acme Ltd",
      "contact_email": "ops@acme.example"
    },
    "template_mode": "strict",
    "missing_fields_policy": "ask"
  }
}
```

**MBIFC Method Port (Scribe):**

1. Interpret user intent first, then polish form.
2. Preserve substance; only improve clarity and structure.
3. Prefer concise output over over-expanded prose.
4. Use `00-Inbox` as safe landing zone when destination is unclear.

**Capture Modes (the LLM picks based on input shape):**

- **Mode 1: Standard Capture** (default) — convert text into a well-titled note. Use a short descriptive title if the user didn't provide one. Keep body close to original wording.
- **Mode 2: Voice-to-Note** — run-on text, missing punctuation, filler words, transcription artifacts. Clean punctuation and paragraph breaks, keep names/numbers/terms intact, suggest splitting if topics are unrelated.
- **Mode 3: Thread Capture** — user sends chained thoughts or explicitly requests thread capture. Split into atomic ideas, preserve sequence, suggest follow-up linking.
- **Mode 4: Quote Capture** — saving a quote, passage, or citation. Keep quote verbatim, add author/source/page/timestamp when available.
- **Mode 5: Reading Notes** — user shares notes from a book/article/lecture/paper. Separate source ideas from user reflections, keep takeaways and action items.
- **Mode 6: Brainstorm** — rapid ideation, stream of ideas. Capture breadth first, avoid premature filtering.

**Content Categories** (classify where possible): Idea, Task, Note, Person note, Link/reference, List. If uncertain, keep generic and leave in inbox.

**Runtime Reality:**
- `template_path`/`template_id`/`template_hint` resolution order: explicit path → id → hint (fuzzy match against template stems).
- If required template fields are missing and `policy=ask`, returns `needs_user_input: true` with `phase: awaiting_template_fields` and does **not** write the note. Collect the missing values in the next turn and call again.
- After successful capture, gateway refreshes `Meta/vault-structure.md` and folder `_index.md` managed blocks.

## note.read

Read full note content and frontmatter.

**Contract:** `path` (required relative path), `include_frontmatter` (default `true`), `include_body` (default `true`).

**MBIFC Method Port (Seeker read-before-edit):**

This is the primary first step of any edit flow:

1. Call `note.read` with the target path.
2. Observe the full content, frontmatter, structure.
3. Decide what to change.
4. Construct the modified note content in full.
5. Call `note.update` with `mode: replace` and the full modified text.

**Selective retrieval:** set `include_frontmatter: false` for body-only, `include_body: false` for frontmatter-only.

**Context ingestion:** the gateway auto-loads nearest folder `_index.md` (with `## Working Rules` and managed summary) and `Meta/vault-structure.md` managed snapshot. This gives you deterministic local context.

## note.update

Update an existing markdown note.

**Contract:** `path` (required), `text` (required unless `mode: frontmatter`), `mode` (default `append`).

**Modes:**

- **`append`** — add new content at the end, preserve prior content exactly. No read needed.
- **`prepend`** — add at the beginning. No read needed.
- **`replace`** — replace full body. **Frontmatter auto-preserve**: if replacement text lacks a YAML block, existing frontmatter is automatically prepended. The response includes a `warnings` field when this happens. This is the primary mode for MBIFC-style intelligent editing (sections, frontmatter, tags, status, links).
- **`frontmatter`** — surgical YAML field updates. `frontmatter_fields` required. Existing fields preserved; only provided keys are overwritten. Body unchanged. **Use this for tag/status/date changes** — safer and more concise than `replace`.

  ```json
  {
    "route": "note.update",
    "payload": {
      "path": "01-Projects/my-task.md",
      "mode": "frontmatter",
      "frontmatter_fields": { "status": "active", "updated": "2026-04-15" }
    }
  }
  ```

- **`section_append` / `section_prepend`** — insert content at the end/start of an existing markdown section. `section_heading` required. The section must already exist; the handler does not create a new heading. If the heading does not exist or appears more than once, the operation fails with validation guidance.

  ```json
  {
    "route": "note.update",
    "payload": {
      "path": "07-Daily/2026-04-26.md",
      "mode": "section_append",
      "section_heading": "Tasks",
      "text": "- [ ] Confirmar fechamento com cliente"
    }
  }
  ```

**Section-Intent Policy (mandatory):** for any user request that implies "add content inside an existing section" (tasks, decisions, action items, notes, wins, blockers), follow this decision flow:

1. **Read first** — call `note.read` for the target note before writing. Identify whether the target heading exists and is unique.
2. **Heading exists exactly once** — use `mode: section_append` (default) or `section_prepend`. Pass explicit `section_heading`.
3. **Heading does not exist** — do NOT silently fallback to `append`/`prepend`. Ask one focused question to confirm creating a new section heading. After explicit confirmation, use read-then-replace to add the new heading in the right location.
4. **Heading exists more than once** — do NOT guess. Ask which heading instance should receive the content.
5. **Never create duplicate headings by default.** For section-intent requests, raw `append`/`prepend` is disallowed unless the user explicitly asks for free-form insertion.

Examples:
- "crie X tarefas" in a daily note → section intent targeting `Tasks`
- "adicione em decisões" → section intent targeting `Decisões`
- "anota isso no final da nota" → free-form append (not section intent)

## note.file

Move a note to a different folder.

**Contract:** `source_path` (required), `target_folder` (required).

**MBIFC Method Port (Sorter):**

Before filing:
1. Verify destination is the right semantic home.
2. Ensure destination folder exists (the gateway creates it if needed).
3. Preserve note content and identity while moving.
4. Avoid destructive operations; move only.

**Conflict rules:** if a file with the same name exists in the destination, the gateway auto-resolves via unique suffix (`name-2.md`, `name-3.md`, ...). If the destination is unclear, keep the note in a safer staging area and ask the user for clarification.

**Follow-up:** after filing, update relevant MOC entries; suggest a linking pass if the batch introduced many related notes.

The handler does not update frontmatter `status`/`location` fields, does not update MOCs or backlinks. Those are LLM-driven follow-up turns.

## note.rename

Rename a note in place. Use this when a note's filename is wrong (e.g. caused by `note.capture` without an explicit `title`, resulting in a slugified body like `casa-e-decorao-decorao-e-ambientes`).

**Contract:** `path` (required), `new_title` (required, becomes the filename after slugification), `rewrite_wikilinks` (boolean, default `true`).

```json
{
  "route": "note.rename",
  "payload": {
    "path": "00-Inbox/casa-e-decorao-decorao-e-ambientes.md",
    "new_title": "Casa e Decoracao e Ambientes",
    "rewrite_wikilinks": true
  }
}
```

**Behavior:**
- Slugifies `new_title` using the same rules as `note.capture` (predictable filename).
- Refuses no-op renames (slug matches current stem).
- Auto-uniquifies the target name if it collides.
- When `rewrite_wikilinks` is `true` (default), walks every other markdown file in the vault and rewrites:
  - `[[old-stem]]` → `[[new-stem]]`
  - `[[old-stem|alias]]` → `[[new-stem|alias]]` (preserves the alias)
  - Leaves all other wikilinks untouched.
- Skips the source file (the file being moved) and the post-move target.
- Refreshes `Meta/vault-structure.md` and the affected folder `_index.md`.
- Logs the operation in `Meta/vault-gateway-log.md`.

**What the handler does NOT do:** update frontmatter `title` field, rewrite plain `[text](old-stem.md)` links, rewrite wikilinks inside `Meta/vault-gateway-log.md`. For those, run `note.update` or accept the partial automation.

**Never rename a note by hand in `/opt/data/obsidian/`** — always go through this route so maintenance and log entries stay consistent.

---

# Low-Traffic Routes (use playbooks)

The routes below are specialized, run infrequently, or are pure dispatches. Full guidance lives in their playbook files. **Fetch a playbook when its trigger signal matches the user's request** — do not read playbooks preemptively, since their content is not part of the public skill surface.

| Route | Playbook | When to fetch (trigger signal) |
|---|---|---|
| `note.search` | `playbooks/note-search/PLAYBOOK.md` | User asks to find a note by content, lookup, recall, or follow-up search. Scoring: title stem > path > content. |
| `note.link` | `playbooks/note-link/PLAYBOOK.md` | User asks to connect two notes, build a relationship, or insert a wikilink. Use bidirectional when the relationship is symmetrical. |
| `template.list` | `playbooks/template-list/PLAYBOOK.md` | Before any template-based capture; to discover candidates. |
| `template.inspect` | `playbooks/template-inspect/PLAYBOOK.md` | After `template.list`; to fetch the field schema of a chosen template before filling it. |
| `onboarding` | `playbooks/onboarding/PLAYBOOK.md` | First-run setup, vault initialization, or porting an existing vault. Multi-turn; requires `state_key` per session. |
| `inbox.triage` | `playbooks/inbox-triage/PLAYBOOK.md` | Daily housekeeping: process `00-Inbox`, classify by content type, file to canonical locations, update MOCs. |
| `vault.defrag` | `playbooks/defrag/PLAYBOOK.md` | Weekly structural maintenance: inbox scan, area MOC refresh, tag hygiene, structure evolution. Distinct from `vault.audit`. |
| `vault.audit` | `playbooks/vault-audit/PLAYBOOK.md` | Comprehensive 7-phase health check: structure, duplicates, link integrity, frontmatter, MOCs, cross-references, trend report. |
| `vault.deep-clean` | `playbooks/deep-clean/PLAYBOOK.md` | When the vault needs more than routine defrag/audit: extended cleaning passes for stale content, redundant tags, broken links, template compliance. |
| `tags.garden` | `playbooks/tag-garden/PLAYBOOK.md` | Tag analysis: unused/orphan/near-duplicate/over-used/under-used tags; merge suggestions; references `Meta/tag-taxonomy.md`. |
| `transcribe` | `dormant/transcribe/PLAYBOOK.md` | (Dormant) Returns `route_dormant` until a transcription backend is configured. |

**Read pattern:** when a trigger matches, run `cat /opt/josemar/skills/vault-gateway/playbooks/<route>/PLAYBOOK.md` (or the `dormant/` path for `transcribe`). Do this *once per session per route* — the playbook content is the source of truth for that route's multi-step workflow.

---

# Safety Requirements

- For "port existing vault", always propose a safe plan before execution.
- If user requests destructive mode, display a strong warning and strongly recommend a vault backup before continuing.
- When using `note.update` with `mode: replace`, existing frontmatter is auto-preserved if the replacement text has no YAML block. This prevents accidental frontmatter loss during read-then-replace edits.
- For "add item to existing section" edits, prefer `note.update` with `mode: section_append` (or `section_prepend`) and explicit `section_heading` to avoid duplicate section creation.
- For section-intent writes, run a mandatory read-first flow: `note.read` → verify heading → `note.update` with `section_append/section_prepend`.
- If target heading is missing or duplicated, do not silently fallback to raw append/prepend; ask one focused clarification before writing.
- Read/write note routes ingest contextual guidance from nearest folder `_index.md` (including `## Working Rules`) and from managed snapshot in `Meta/vault-structure.md` when present.
- After vault write routes (`note.capture`, `note.update`, `note.file`, `note.rename`), gateway refreshes managed context blocks in `Meta/vault-structure.md` and folder `_index.md` files while preserving human-authored sections.
- Never touch `/opt/data/obsidian/` directly. Vault reads and mutations must go through gateway routes so contextual loading, maintenance, wikilink rewriting, and `Meta/vault-gateway-log.md` stay consistent.
- If gateway tools fail or lack coverage for a requested vault operation, stop and explain the failure or missing route to the user. Only use direct filesystem access, custom scripts, internal imports, or other non-gateway workarounds after the user explicitly approves that specific workaround.

## Internal Contract

- Routing metadata lives in `contracts/routes.json` (the data contract, not surfaced to the LLM).
- High-traffic route guidance is inlined above.
- Low-traffic route guidance lives in `playbooks/*/PLAYBOOK.md` and `dormant/*/PLAYBOOK.md`.
