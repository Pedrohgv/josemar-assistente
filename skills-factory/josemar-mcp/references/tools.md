# Josemar Knowledge MCP — Tool Reference

Detailed tool schemas, bounds, and validation rules. Loaded on demand via
`skill_view("josemar-mcp", file_path="references/tools.md")`.

## Tool surface

The server exposes exactly four tools in two distinct classes. No shell,
arbitrary command, port forwarding, or direct API exposure is available.

### Read-only gbrain tools (no vault write access)

#### `vault_search(query, max_results=10)`

Search the Josemar Obsidian vault via `gbrain search` (read-only).

- `query` (str, required): non-empty, max 2000 chars, control characters
  rejected (newlines/tabs collapsed to spaces).
- `max_results` (int, optional, default 10): must be an integer between 1 and
  50. Booleans are rejected.
- Returns: a list of objects with `slug`, `title`, `type`, `score`, `snippet`.
  Never full page content. Use `vault_get` to read a specific page.
- Bounds: gbrain subprocess timeout 60s, stdout capped at 2 MiB.

#### `vault_get(path)`

Read one vault page by slug/path (read-only).

- `path` (str, required): non-empty, max 512 chars. Must be a relative vault
  slug; absolute paths (`/`, `~`), parent references (`..`), and control
  characters are rejected. Must match `^[a-z0-9][a-z0-9._/-]*$`.
- Returns: an object with `slug`, `title`, `type`, `tags` (list[str]),
  `frontmatter` (dict), `content` (str). Raises `ToolError` with
  "page not found" if the slug does not exist.
- Bounds: gbrain subprocess timeout 60s, stdout capped at 2 MiB.

#### `project_context()`

Return curated project-context/memory notes via allowlisted gbrain reads only.

- No parameters.
- Returns: a dict keyed by the allowlisted slugs. Missing notes are reported
  as `null` rather than failing the whole call.
- The allowlist is fixed in the server source
  (`PROJECT_CONTEXT_SLUGS`); a remote client cannot expand it. No write
  access is available or invented.

### `josemar_chat(prompt, max_tokens=2048)` — full trusted/user-equivalent

Send a bounded prompt to the local Josemar Hermes API
(`http://localhost:8642/v1/chat/completions`) from the Hermes container.

**This is a full trusted/user-equivalent capability, not a read-only tool.**
It is the remote equivalent of talking to Josemar directly: it can trigger
Hermes tools (which may read/write the vault, send messages, run skills,
etc.) and incurs model cost on every call. It is distinct from the curated
read-only gbrain tools above, which cannot mutate state and do not invoke
the model.

- `prompt` (str, required): non-empty, max 16000 chars.
- `max_tokens` (int, optional, default 2048): must be an integer between 1
  and 2048. Booleans are rejected.
- Returns: an object with `content` (str), `model` (str), `usage` (dict).
- The `API_SERVER_KEY` is recovered from the server-side s6 environment and
  sent as a `Bearer` token. It is never returned to the client or logged. HTTP errors are surfaced as generic
  `ToolError` messages (e.g. "chat API returned HTTP 401") without the key
  or full response body.
- Bounds: request timeout 120s, response capped at 2 MiB.
- **Cost note:** every call invokes the Hermes model and may trigger Hermes
  tools. The operator is responsible for prompts sent through the remote
  client.

## Validation rules (all tools)

- All inputs are type-checked. Booleans are rejected where integers are
  expected (Python `bool` is a subclass of `int`).
- Strings are stripped and length-bounded.
- Path traversal, absolute paths, and parent references are rejected in
  `vault_get`.
- Control characters are rejected in all string inputs.
- gbrain subcommands are allowlisted (`search`, `get`); anything else is
  rejected before exec.
- gbrain subprocess env is minimal: `HOME`, `PATH`, `LANG`, `LC_ALL`, `TZ`,
  `GBRAIN_HOME`, `GBRAIN_BRAIN_REPO`, `GBRAIN_SCHEMA_PACK`,
  `GBRAIN_SKIP_STARTUP_HOOKS=1`. No provider API credentials are inherited.

## Lock coordination

`vault_search`, `vault_get`, and `project_context` acquire an *exclusive*
(`LOCK_EX`) advisory lock on `/opt/data/.locks/tasknotes.lock` around the
gbrain subprocess, with a bounded wait (default 10s). gbrain's PGLite has its
own internal locking, but the remote side must serialize its own gbrain
invocations so they do not race with each other or with Hermes-side writers
(tasknotes mutations, the refresh cron, embed-backfill). The lock file is
shared with those writers (which also take an exclusive lock).

**Fail-closed:** if the lock cannot be acquired within the timeout (e.g. a
writer holds the lock), the tool raises a generic `ToolError` ("vault busy:
lock timed out") and does NOT perform the read without the lock. The lock is
held only for the duration of the gbrain subprocess, not around validation or
result normalization, to minimize contention.

The vault and gbrain state are the Hermes container's own mounts. gbrain state
(`/opt/data/.gbrain`) remains writable because search/get need PGLite WAL and
lock files. The exclusive lock serializes MCP calls with Hermes-side writers.

`josemar_chat` does not touch the vault lock (it calls the HTTP API, not
gbrain directly).

## Error handling

- Expected validation errors raise `ToolError` with a safe, user-facing
  message (the message may echo the invalid input shape, never content).
- Unexpected exceptions raise a generic `ToolError` ("<operation> failed
  unexpectedly") and log only the operation name, never arguments or content.
- gbrain failures surface a sanitized, truncated stderr tail (max 120 chars,
  newlines collapsed) plus the return code. Full stderr is never echoed.
- HTTP errors from `josemar_chat` surface only the status code, never the
  key or response body.

## Stdout discipline

Stdout is reserved for MCP protocol traffic. All diagnostics go to stderr.
The server uses `log_level="WARNING"` to avoid leaking prompt/content into
logs.
