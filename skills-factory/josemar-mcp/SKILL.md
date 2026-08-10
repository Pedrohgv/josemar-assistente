---
name: josemar-mcp
description: Remote MCP client setup and runtime guidance for the Josemar Knowledge MCP server. Use when the user asks to set up, connect, or troubleshoot a remote MCP client (OpenCode, Claude Desktop, etc.) that calls Josemar's curated read-only vault tools and the josemar_chat tool.
categories:
  - retrieval
  - search
  - knowledge
  - mcp
---

# Josemar Knowledge MCP Skill

This skill guides the operator through setting up and using the remote Josemar
Knowledge MCP server. The server is repo-owned
(`scripts/josemar_knowledge_mcp.py`) and launched as a forced SSH command by
a hardened sshd running **inside the Hermes container** (not a separate
sidecar). It is exposed privately via Tailscale Serve TCP (never Funnel) on a
fixed SSH port `2223`, distinct from the browser-control tunnel on port
`2222`. The sshd shares Hermes's PID namespace, gbrain PGLite state, and
locks, and the forced command runs as the `hermes` user.

The server offers two distinct classes of tools:

- **Curated read-only gbrain tools** (no vault write access):
  - `vault_search(query, max_results=...)` — bounded `gbrain search`.
  - `vault_get(path)` — bounded `gbrain get` (path-traversal-safe).
  - `project_context()` — allowlisted gbrain reads of project-context/memory
    notes only.
- **`josemar_chat(prompt, max_tokens=...)`** — a **full trusted/user-equivalent
  capability**, not a read-only tool. It is a bounded POST to the local
  Hermes OpenAI-compatible API (`/v1/chat/completions`) on the internal Docker
  network. It can trigger Hermes tools (which may read/write the vault, send
  messages, run skills, etc.) and incur model cost on every call. The
  `API_SERVER_KEY` is injected server-side and never sent to the client. The
  operator is responsible for prompts sent through it.

No shell, arbitrary command, port forwarding, or direct API exposure is
available to the remote client. The forced command is the only path. The MCP
server as a whole is **not** read-only — `josemar_chat` is intentionally a full
trusted capability; only the three gbrain tools are read-only.

## When to use

- The user asks how to set up a remote MCP client to call Josemar.
- A remote MCP client reports a connection failure that looks like the
  `josemar-mcp` overlay is disabled or misconfigured.
- The user asks what tools the remote MCP server exposes.

## First-time setup

Read `SETUP.md` in this skill directory and walk the operator through it. Do
not paraphrase from memory. The authoritative source of truth for any detail
is `docs/josemar-mcp.md` in the repo; if this file and that doc disagree, the
doc wins — flag the discrepancy.

## Connection failures

If a remote MCP client cannot connect, the cause is one of:

- The `josemar-mcp` Compose overlay is disabled on the server (no supervised sshd, no tcp:2223 forward). This is the most likely
  cause on a fresh or recently-redeployed server.
- The overlay is enabled but Tailscale is down, or the client is not on the
  same tailnet, or the Tailscale ACL does not allow the client to reach
  tcp:2223 on the server.
- The overlay is enabled and the client was connected, but the SSH session
  dropped (e.g. after a server redeploy or client sleep).

Surface all three to the operator and let them disambiguate. Do not attempt to
shell into the server or restart services on the operator's behalf; recovery
is an operator action.

## Tool reference

For the full tool schemas, bounds, validation rules, and security posture, load
the reference on demand: `skill_view("josemar-mcp", file_path="references/tools.md")`.

## Safety

- **Read-only vault tools.** `vault_search`, `vault_get`, and
  `project_context` are read-only and cannot mutate the vault. The MCP server
  as a whole is **not** read-only — `josemar_chat` is a full trusted capability.
- **josemar_chat is user-equivalent and can incur cost.** It can trigger
  Hermes tools (which may read/write the vault, send messages, run skills)
  and incurs model cost on every call. It is distinct from the read-only
  gbrain tools. The operator is responsible for prompts sent through it.
- **No secrets to the client.** The API key is injected server-side and never
  appears in client config, logs, or responses.
- **Tailnet-only.** The SSH endpoint is exposed only via Tailscale Serve TCP,
  never Funnel. Use explicit Tailscale ACL `src`/`dst` rules; never wildcard.
- **Host-key pinning.** The client should pin the Hermes's Ed25519 host key
  to prevent MITM. See `SETUP.md`.