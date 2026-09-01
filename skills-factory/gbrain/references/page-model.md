# gbrain Page Model and Reconciliation Edge Cases

Load this reference when diagnosing type/path mismatches, stale external edits, idempotent re-upsert behavior, or link-extraction/reconciliation details. Normal note creation and updates should not require this file; the main `SKILL.md` contains the routine path/type and read-then-write rules.

## Whole-page write model

`gbrain put` upserts the entire page content. There is no native patch, section append, surgical frontmatter edit, rename, template-instantiation, or physical move API.

For existing pages, the supported agent loop is:

1. `gbrain get <slug>` to read the complete current page.
2. Apply the desired change while preserving existing frontmatter and unrelated sections.
3. Write the complete result back with `gbrain put <slug> --content ...` or `gbrain capture --file PATH --slug SLUG` for large/file-based content.

If `gbrain get` fails or returns incomplete content, retry once. If it remains incomplete, stop and report rather than overwriting from partial content.

Never use `gbrain put --stdin`. The stdin path has caused silent corruption in Josemar (#71/#82). Use `gbrain capture --stdin` for new streamed capture, or `capture --file` / `put --content` for controlled full-page writes.

## Direct filesystem edits and stale external state

Agent-facing content creation/mutation must go through public `gbrain`, not direct vault filesystem writes. Direct writes bypass immediate gbrain indexing and can leave the database stale until external-edit reconciliation occurs.

This is a Josemar mitigation for the stale-index behavior tracked in #94, not a claim that upstream gbrain forbids all external editors. Obsidian/Syncthing edits are expected external changes and the scheduled refresh lane reconciles them.

`gbrain put` and `gbrain capture` are write-through: the index reflects those writes immediately. The recurring operator refresh reconciles external edits using incremental sync/extraction. Full sync is expensive/failure-gated and is reserved for the documented reconciliation cases in `docs/gbrain-operations.md`, not routine stale reads.

Direct filesystem access is reserved for operations the supported gbrain interface genuinely cannot perform, such as a reviewed physical rename with wikilink rewrites or template/bootstrap operation. Do not use it as an alternate note mutation path.

## Path-derived type inference

The main skill lists the routine path-prefix → type mapping. gbrain uses explicit frontmatter `type:` when present; otherwise it infers the type from the path. Do not set frontmatter `type` to a value that conflicts with the directory-derived type.

### Idempotent re-upsert caveat

gbrain can short-circuit a `put` when the content hash is unchanged. On that path, type inference may not be re-evaluated. A page originally indexed with the wrong type can therefore retain that type across an otherwise identical re-upsert.

If a page is known to have a stale/wrong type, use the documented operator/reconciliation procedure or deliberately provide the correct explicit type/content change appropriate to the case; do not perform arbitrary churn merely to force re-indexing. See `docs/gbrain-operations.md` for maintenance/reindex decisions.

## Wikilinks and backlinks

Obsidian `[[wikilinks]]` in page content are resolved automatically when a page is written with basename resolution enabled. `gbrain backlinks` returns incoming links, including resolved wikilink edges.

Manual chat-created links should use the normal manual/custom link source. Reconciliation-managed link sources (`markdown`, `frontmatter`, `mentions`, `wikilink-resolved`) belong to gbrain's own extraction/reconciliation logic.

Cross-page extraction for pre-existing/external pages is an operator/reconciliation concern. Follow `docs/gbrain-operations.md` rather than invoking private/native extraction paths from chat.

## Runtime state

gbrain runtime state lives under `/opt/data/.gbrain` (PGLite database, config, cache). It is runtime-only and is not versioned by workspace state sync.
