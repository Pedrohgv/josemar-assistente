---
name: gbrain
description: Native gbrain vault interface. Use the public `gbrain` command for routine safe search, read, authoring, and linking. Task-file mutations belong to the TaskNotes MCP. No chat-driven reindexing.
categories:
  - retrieval
  - search
  - knowledge
  - authoring
---

# GBrain Skill

Use this skill for normal vault search, reading, note creation/update, and linking. The main skill is intentionally self-contained for those routine operations; non-routine indexing/recovery/compatibility details live in `references/` and operator runbooks.

All agent-facing general vault access uses the public `gbrain` command. It provides Josemar's safe adapter behavior: the pinned native CLI runs as the `hermes` runtime user while cooperating with the shared TaskNotes/gbrain lock.

The bounded TaskNotes MCP is the only specialized task-write exception. Use the `task_*` MCP tools for TaskNotes task-file mutations. TaskNotes internally uses the private native gbrain path while holding its transaction lock; it must never nest the public `gbrain` wrapper.

## Safe-access non-negotiables

- **Never run gbrain/vault Git as root.** Runtime state belongs to the `hermes` user.
- **Use public `gbrain` for chat/skill/general vault work.** Never present `/opt/josemar/libexec/gbrain-native` or another private native path as an agent command.
- **Never open PGLite concurrently.** Josemar-owned gbrain paths cooperate on `/opt/data/.locks/tasknotes.lock`.
- **Do not invoke `josemar-gbrain` from chat**, except `josemar-gbrain refresh-embeddings` after an explicit user request. `reindex`, `refresh`, `enable-embeddings`, `disable-embeddings`, and `embed-backfill` remain operator-only.
- **Do not bypass TaskNotes for task mutations.** Task Markdown is mutated through `task_*` tools, not general note writes.
- The wrapper is a cooperative safety boundary against accidental/prompt-driven misuse, not a security boundary against a compromised same-UID runtime.

For maintenance windows, recovery, reindex/rebuild, migrations, vault swaps, private-native diagnostics, or job-pausing rules, do not improvise from this skill; use `docs/gbrain-operations.md` and `docs/vault-recovery-operations.md`.

## Routine retrieval

Start with `gbrain search` for normal retrieval. Josemar supports semantic/hybrid retrieval when the embedding capability is operator-enabled; concept-based queries are appropriate in that mode. Because activation is mutable runtime state, use `gbrain status` when current search capability matters after deployment/recovery rather than relying on static prose.

```bash
gbrain search "notes on obsidian sync" --limit 10
gbrain status
```

`gbrain query --no-expand` is useful for semantic/hybrid exploration when the current runtime reports the required capability:

```bash
gbrain query --no-expand "notes on obsidian sync"
```

Common `search` flags include `--limit` and `--offset`.

## Read a page

```bash
gbrain get inbox/my-note
```

If `gbrain get` fails or returns incomplete content, retry once. If it is still incomplete, stop and report rather than overwriting from partial content.

## Create a note

Use `capture` for normal note creation. Content can be positional, stdin, or file-based.

```bash
gbrain capture "remember to follow up on X" --slug inbox/custom --json
printf '%s' "remember to follow up on X" | gbrain capture --stdin --json
gbrain capture --file /tmp/note.md --slug notes/example --json
```

Common flags:
- `--slug` — target slug/path
- `--type` — optional explicit type; normally prefer correct path-derived type
- `--stdin` — read capture content from stdin
- `--file` — read content from a file; works for new or existing pages
- `--json` — emit JSON

## Update an existing note

`put` is whole-page replacement. There is no patch/section-append/frontmatter-surgical API.

Always use **read → modify in memory → write complete page**:

1. `gbrain get <slug>` and preserve all current content/frontmatter.
2. Apply the requested change.
3. Write the full resulting page with `gbrain put <slug> --content ...` or `gbrain capture --file PATH --slug SLUG` for large/file-based content.

```bash
gbrain put inbox/my-note --content "# Complete updated page"
```

**Never use `gbrain put --stdin`.** That path has caused silent corruption in Josemar. Use `capture --stdin`, `capture --file`, or `put --content` instead.

Do not use direct filesystem tools (`write_file`, Python `open()`, `cp`, etc.) as an alternate content-mutation path. gbrain writes update the index immediately; external Obsidian/Syncthing edits are reconciled by the operator refresh path. For stale-index/type-idempotency/link-extraction edge cases, load:

`skill_view("gbrain", file_path="references/page-model.md")`.

## Choose the note path/type

gbrain uses explicit frontmatter `type:` when present; otherwise it infers type from the directory path. For routine authoring, choose the path matching the intended type and avoid a conflicting frontmatter `type`.

| Path prefix | Inferred type |
| --- | --- |
| `people/`, `person/` | `person` |
| `companies/`, `company/` | `company` |
| `deals/`, `deal/` | `deal` |
| `projects/`, `project/` | `project` |
| `sources/`, `source/` | `source` |
| `notes/`, `note/` | `note` |
| `meetings/`, `meeting/` | `meeting` |
| `conversations/` | `conversation` |
| `cal/`, `calendar/` | `calendar-event` |
| `life/diary/` | `diary` |
| `life/events/` | `event` |
| `inbox/` or no match | `concept` |

For unusual existing-type mismatches or idempotent re-upsert behavior, use `references/page-model.md` rather than forcing arbitrary content changes.

## Links and backlinks

Obsidian `[[wikilinks]]` in content are resolved by gbrain on write. Create an explicit manual link with:

```bash
gbrain link inbox/a people/b --link-type mentions --context "meeting notes" --link-source manual
```

Use `manual` (default) or a custom kebab-case source for agent-created links. Do not claim/reuse reconciliation-managed sources such as `markdown`, `frontmatter`, `mentions`, or `wikilink-resolved`.

List incoming links with:

```bash
gbrain backlinks people/b
```

## Chronicle timeline

Chronicle can derive structured timeline events from supported meeting/conversation/calendar pages when the operator-enabled capability is active. Routine timeline queries include:

```bash
gbrain day 2026-08-31
gbrain since 2026-08-01 --kind decision
gbrain last-seen people/example
gbrain on-this-day
gbrain orient --days 7
```

Do not run Chronicle backfill/extraction jobs from chat. For the event model, eligible page types, taxonomy, ontology, output structure, and non-routine workflows, load:

`skill_view("gbrain", file_path="references/chronicle.md")`.

## Other useful agent commands

Where appropriate, the public command also exposes `gbrain tags`, `timeline`, `graph`, `delete`, `history`, `revert`, and soft-delete recovery with `gbrain restore <slug>`. Use `gbrain <command> --help` for current per-command arguments rather than embedding exhaustive CLI help here.

## What not to load for routine work

Normal search/read/create/update/link requests should be completed from this `SKILL.md` without loading another file.

Load references only when their trigger applies:

- `references/page-model.md` — stale external edits, type/idempotency edge cases, reconciliation/link extraction, deeper whole-page semantics.
- `references/chronicle.md` — Chronicle schema/taxonomy/ontology and deeper timeline workflows.
- `references/upstream-skillpack.md` — designing gbrain-integrated skills/prompts/jobs, evaluating upstream gbrain conventions/features, or upgrading the pinned gbrain version.

Operator activation/reindex/embedding lifecycle/recovery belongs in `docs/gbrain-operations.md`, not in the routine runtime skill.
