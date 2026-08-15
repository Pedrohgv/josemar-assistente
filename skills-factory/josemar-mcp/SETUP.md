# Josemar Knowledge MCP — First-Time Setup

This is the operator-facing setup walkthrough for the remote Josemar Knowledge
MCP feature. The companion `SKILL.md` covers runtime guidance; this file
covers the one-time setup that makes the connection possible. When the user
asks how to set up the remote MCP client for the first time, read this file
and walk them through it. Do not paraphrase from memory.

The authoritative source of truth for any detail here is `docs/josemar-mcp.md`
in the repo. If this file and that doc disagree, the doc wins; flag the
discrepancy so the doc and this file can be reconciled.

## Prerequisites

- The Josemar Tailscale node is up and the client machine is a member of the
  same tailnet.
- The operator has access to the GitHub repository Settings (Secrets and
  Variables > Actions) for the Josemar repo.
- The Hermes API server is enabled on the server
  (`HERMES_API_SERVER_ENABLED=true` with `HERMES_API_SERVER_KEY` set). The
  deploy workflow rejects `JOSEMAR_MCP_ENABLED=true` otherwise, because
  `josemar_chat` calls the internal Hermes API using that key.

## One-time setup

### 1. Generate an SSH keypair on the client machine

```bash
ssh-keygen -t ed25519 -f ~/.ssh/josemar_mcp -N ""
```

This produces:
- `~/.ssh/josemar_mcp` (private key, keep secret on the client)
- `~/.ssh/josemar_mcp.pub` (public key, upload to GitHub)

### 2. Add the public key as the `JOSEMAR_MCP_AUTHORIZED_KEY` secret

In GitHub: Settings > Secrets and variables > Actions > New repository secret.

- Name: `JOSEMAR_MCP_AUTHORIZED_KEY`
- Value: the single-line contents of `~/.ssh/josemar_mcp.pub`
  (OpenSSH format, e.g. `ssh-ed25519 AAAA... user@example.com`)

### 3. Allow the client to reach tcp:2223 on the server (Tailscale ACL)

In the Tailscale admin console (ACLs), add an accept rule for the client to
reach the server's tcp:2223. Use an explicit tag or host alias for both
`src` and `dst`; do not use a wildcard `src`.

```json
{
  "action": "accept",
  "src":    ["tag:client"],
  "dst":    ["josemar-server:2223"]
}
```

Replace `tag:client` with the client's tag (or a specific user) and
`josemar-server` with the Tailscale node name set by `TAILSCALE_HOSTNAME`
(default `josemar-server`). If you also use browser-control (tcp:2222), add a
parallel rule for `josemar-server:2222`.

### 4. Enable the overlay on the server

In GitHub: Settings > Secrets and variables > Actions > Variables.

- Set `JOSEMAR_MCP_ENABLED` to `true`.

Then run the deploy workflow. It will:
- Validate `JOSEMAR_MCP_AUTHORIZED_KEY` is a single-line SSH public key.
- Validate `HERMES_API_SERVER_ENABLED=true` and `HERMES_API_SERVER_KEY` is set.
- Populate the `tailscale-serve-config` named volume with the tcp:2223
  TCPForward to Hermes's static `JOSEMAR_MCP_HERMES_IP:2223` (combined with
  tcp:2222 if browser-control is also enabled).
- Populate the `josemar-mcp-authorized-keys` named volume with the operator's
  public key.
- Apply `docker-compose.josemar-mcp.yml`; it modifies the existing Hermes and Tailscale services and defines no separate service or profile.
- Verify the supervised Hermes sshd is running, Tailscale Serve
  tcp:2223 targets the exact Hermes IP with no Funnel, and the forced-command
  wrapper + MCP server are present and import cleanly in the Hermes image.

Defaults for the internal Docker subnet and IPs are almost always fine; only
override `JOSEMAR_MCP_SUBNET`, `JOSEMAR_MCP_GATEWAY`,
`JOSEMAR_MCP_HERMES_IP`, and `JOSEMAR_MCP_TAILSCALE_IP` together if the
default `172.31.251.0/29` collides with an existing network on the host
(browser-control uses `172.31.250.0/29`, so both can be enabled together).

### 5. Configure the remote MCP client (OpenCode example)

The repo `opencode.json` is intentionally NOT modified with a user-specific
local SSH key/path. Add the MCP server to your **local** OpenCode config
(`~/.config/opencode/opencode.json` or the project-local
`.opencode/opencode.json` you control), not the committed repo config.

Copyable snippet (replace `<tailscale-host>` with the server's Tailscale
hostname or IP, and `<path-to-private-key>` with the absolute path to the
private key generated in step 1):

```json
{
  "mcp": {
    "josemar-knowledge": {
      "type": "local",
      "command": [
        "ssh",
        "-i", "<path-to-private-key>",
        "-p", "2223",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=<path-to-private-key>.known_hosts",
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "hermes@<tailscale-host>"
      ],
      "enabled": true
    }
  }
}
```

Notes:
- `StrictHostKeyChecking=accept-new` pins the host key on first connect and
  fails loudly if it changes later. For stricter pinning, pre-populate the
  known_hosts file with Hermes's Ed25519 host key (see step 6).
- `IdentitiesOnly=yes` ensures only the specified key is offered.
- `BatchMode=yes` prevents interactive prompts from hanging the MCP client.
- The SSH user is `hermes` and the port is `2223` (fixed constants). The forced
  command is set server-side; no `command=` is needed in the client config.

### 6. (Recommended) Pin the host key

Fetch Hermes's host key once (after the first successful deploy) and
pin it:

```bash
ssh-keyscan -p 2223 -t ed25519 <tailscale-host> >> ~/.ssh/josemar_mcp.known_hosts
```

Then set `StrictHostKeyChecking=yes` (instead of `accept-new`) in the client
config so a changed host key is rejected without prompting.

### 7. Verify end-to-end

From the client machine, after configuring the MCP server:

```bash
ssh -i ~/.ssh/josemar_mcp -p 2223 \
  -o StrictHostKeyChecking=accept-new \
  -o IdentitiesOnly=yes -o BatchMode=yes \
  hermes@<tailscale-host>
```

A successful connection starts the MCP server over stdio (the forced command).
The client should see MCP protocol output on stdout; no shell prompt appears.
If the connection drops immediately, check the Hermes logs on the server:

```bash
docker compose -f docker-compose.yml -f docker-compose.josemar-mcp.yml logs --tail=80 hermes
```

## Disable / rollback

1. Set `JOSEMAR_MCP_ENABLED=false` (or unset it) and re-run deploy.
2. The workflow writes the Tailscale Serve config without tcp:2223 (or `{}`
   if browser-control is also disabled), clears `josemar-mcp-authorized-keys`,
   uses base Compose only (plus any other enabled overlays), and tears down
   any previous in-container josemar-mcp sshd.
3. Remove the `josemar-knowledge` MCP server entry from your local OpenCode
   config.
4. Keep both named volumes. Do not remove
   `<project>_josemar-mcp-hostkeys`: it preserves the SSH host identity across
   disable/re-enable cycles. The authorized-keys volume is retained as well.

## Troubleshooting

- If the connection is refused, confirm Tailscale is up
  (`tailscale status`), the supervised Hermes sshd is running, the
  `JOSEMAR_MCP_AUTHORIZED_KEY` secret matches this client's public key, and
  the Tailscale ACL allows the client to reach tcp:2223.
- If the connection drops immediately after auth, the forced command may be
  failing. Check the hermes container logs and that the image contains
  `/usr/local/bin/josemar-knowledge-mcp-forced` and
  `/opt/josemar/scripts/josemar_knowledge_mcp.py`.
- If `josemar_chat` returns "chat API key is not configured server-side",
  the `API_SERVER_KEY` env is not in the Hermes container env. Confirm
  `HERMES_API_SERVER_ENABLED=true` and `HERMES_API_SERVER_KEY` is set in the
  deploy.

## Warnings

- **Tailnet-only.** Never expose the SSH endpoint via Tailscale Funnel or bind
  it to `0.0.0.0`. The only supported path is Tailscale Serve TCP on the
  tailnet.
- **Explicit ACLs.** Do not use broad Tailscale ACL `src` rules (e.g.
  `["*"]`); use an explicit tag or user for the client.
- **josemar_chat cost.** Each `josemar_chat` call invokes the Hermes model and
  may trigger Hermes tools. The operator is responsible for prompts sent
  through the remote client.
