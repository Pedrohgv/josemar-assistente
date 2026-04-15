---
name: vault-gateway
description: Single entrypoint for Obsidian vault operations in Josemar.
---

# Vault Gateway

Use this skill as the only public interface for vault operations.

## Scope

- Route vault requests to internal playbooks.
- Keep external integrations (gmail/calendar/contacts) out of this bundle.
- Keep transcription capability present but dormant until backend setup is complete.
- Caller must provide explicit `route` in JSON input.

## Active Routes

- onboarding
- template.list
- template.inspect
- note.read
- note.create (alias for note.capture)
- note.capture
- note.update
- note.search
- note.link
- note.file
- inbox.triage
- vault.defrag
- vault.audit
- vault.deep-clean
- tags.garden

## Dormant Route

- transcribe (kept for future activation, currently disabled by design)

## Input Contract

Always provide structured input with explicit route:

```json
{
  "route": "note.capture",
  "payload": {
    "text": "anote que hj tive uma conversa com o cliente Claudio",
    "target_folder": "00-Inbox",
    "template_hint": "meeting"
  }
}
```

Structured template capture example:

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

For onboarding, include a session-scoped `state_key` in payload to isolate multi-turn state:

```json
{
  "route": "onboarding",
  "payload": {
    "state_key": "session-demo",
    "mode": "port",
    "input": "sim"
  }
}
```

Frontmatter surgical update example:

```json
{
  "route": "note.update",
  "payload": {
    "path": "01-Projects/my-task.md",
    "mode": "frontmatter",
    "frontmatter_fields": {
      "status": "active",
      "updated": "2026-04-15"
    }
  }
}
```

Contract rules:
- Top-level keys: only `route` and `payload`
- Route params must be inside `payload`
- Unknown payload keys are rejected
- If `route` is missing or unknown, the skill returns an error with `available_routes`

## Safety Requirements

- For "port existing vault", always propose a safe plan before execution.
- If user requests destructive mode, display a strong warning and strongly recommend a vault backup before continuing.
- When using `note.update` with `mode: replace`, existing frontmatter is auto-preserved if the replacement text has no YAML block. This prevents accidental frontmatter loss during read-then-replace edits.

## Internal Contract

- Routing metadata lives in `contracts/routes.json`.
- Per-route guidance lives in `playbooks/*/PLAYBOOK.md`.
- Dormant routes live in `dormant/*/PLAYBOOK.md`.
