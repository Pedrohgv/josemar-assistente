# Josemar User Schema Pack

This directory holds the source-of-truth gbrain schema pack for the Josemar
deployment. It is part of the private agent-state repo and is installed into
gbrain's native user-pack directory during operator activation.

## Source-First Workflow

1. **Propose**: Josemar (or the user) proposes a diff to `pack.yaml` with
   impact analysis.
2. **Approve**: The user explicitly approves the change.
3. **Update**: The source `pack.yaml` is edited and committed to agent-state.
4. **Activate**: An operator runs `josemar-gbrain reindex` to validate,
   install, and activate the pack. No redeploy is required.

Never edit the installed copy under `$GBRAIN_HOME/.gbrain/schema-packs/`
directly. Always edit the source file here and reindex.

## Structure

```
gbrain/schema-packs/josemar-user/
  pack.yaml    # Schema pack manifest (extends gbrain-base-v2)
```

## See Also

- `docs/gbrain-operations.md` — activation runbook and schema workflow.
- `skills-factory/gbrain/SKILL.md` — chat-facing gbrain skill including
  `schema_status` for read-only schema introspection.