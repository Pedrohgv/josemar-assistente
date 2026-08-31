# Browser Control First-Time Setup

This is the operator-facing setup walkthrough for the browser-control feature. The companion `SKILL.md` covers runtime driving of the connected browser via `connected_browser_exec`; this file covers the one-time setup that makes that connection possible. When the user asks how to set up browser control for the first time, read this file and walk them through it. Do not paraphrase from memory.

The authoritative source of truth for architecture and operator details is `docs/browser-control.md`. If this reference and that document disagree, the runbook wins; reconcile both in the same documentation change.

## What this enables

This setup exposes the operator's dedicated laptop browser to Hermes as the **connected browser**, driven by `connected_browser_exec`. The tool reads `browser.connected_cdp_url`, configured to reach `http://127.0.0.1:9222` inside the Hermes container.

Two browsers, two scopes — this setup covers only the connected one:

- ordinary interactive/rendered work can use the built-in server-headless `browser_*` tools independently of this overlay/tunnel;
- `connected_browser_exec` uses only the connected CDP endpoint and fails closed when that endpoint is unavailable; it does not fall back to the headless browser or another browser.

## Prerequisites

- The Josemar Tailscale node is up and the laptop is in the same tailnet.
- Chrome/Chromium 136 or newer on the laptop.
- Access to the repository Actions Secrets and Variables settings.

## One-time setup

### 1. Generate an SSH keypair on the laptop

```bash
ssh-keygen -t ed25519 -f ~/.ssh/josemar_browser_tunnel -N ""
```

This creates the private key `~/.ssh/josemar_browser_tunnel` and public key `~/.ssh/josemar_browser_tunnel.pub`. Keep the private key on the laptop.

### 2. Add `BROWSER_TUNNEL_AUTHORIZED_KEY`

Create repository secret `BROWSER_TUNNEL_AUTHORIZED_KEY` with the single-line contents of the public key file.

Never put the private key in repository settings or documentation.

### 3. Allow laptop → server tcp:2222 in Tailscale ACLs

Add a narrow accept rule for the laptop/user/tag to the Josemar node on tcp:2222. Do not use a wildcard source.

Example shape:

```json
{
  "action": "accept",
  "src": ["tag:laptop"],
  "dst": ["josemar-server:2222"]
}
```

Use the actual operator-controlled tag/user and Tailscale node name; do not publish private host/network values in repository docs.

### 4. Enable the server overlay

Set repository variable `BROWSER_CONTROL_ENABLED=true` and run the deploy workflow.

Deployment populates the Tailscale Serve config and authorized-keys volumes, applies `docker-compose.browser-control.yml`, enables the browser-control profile, and verifies the sidecar/routing contract.

The internal browser-control subnet/IP variables normally use repository defaults. Override the subnet/gateway/Hermes/Tailscale IP set together only to resolve a real collision. See `docs/github-workflows.md` for the variable catalog and `docs/browser-control.md` for the architecture.

### 5. Install the Linux laptop launcher

From a checkout of this repository on the laptop:

```bash
bash laptop/linux/install-launcher.sh
```

The installer is user-level and creates the launcher command and desktop entry. The tested/supported launcher path is Linux Mint; macOS/Windows adaptations remain best-effort as documented in `docs/browser-control.md`.

### 6. Start the dedicated browser and tunnel

Launch the installed Josemar Browser entry or run:

```bash
josemar-browser-control start
```

The launcher starts the dedicated Chrome profile at `~/.josemar-chrome-profile`, exposes CDP only on loopback, waits for it, then opens the reverse SSH tunnel.

Check status with:

```bash
josemar-browser-control status
```

### 7. Verify end to end

From the laptop, verify the local CDP endpoint:

```bash
curl -s http://127.0.0.1:9222/json/version
```

From the server/runtime, verify the connected endpoint through the documented container path:

```bash
docker compose exec -T hermes curl -s http://127.0.0.1:9222/json/version
```

Both should return Chrome CDP version JSON while the reverse tunnel is active. The ordinary server-headless browser tools do not use this endpoint.

## Daily use

- Start: launch Josemar Browser or run `josemar-browser-control start`.
- Re-launch while running: focus/open the dedicated browser without starting a duplicate controller.
- Stop: close the dedicated Josemar Chrome windows or run `josemar-browser-control stop`.
- Status: `josemar-browser-control status` reports controller/Chrome/tunnel/CDP state without reading page/session contents.

A connected route failure does not imply the ordinary server-headless browser is unavailable; the two routes are independent. However, do not substitute the headless browser when the requested work depends on the connected browser's authenticated session.

## Persistence

The dedicated Chrome profile, SSH key/known-hosts material, and launcher installation persist across reboot. Browser/tunnel processes do not; the connection is intentionally on demand.

## Troubleshooting

- Run `josemar-browser-control status` first.
- Launcher logs are under `~/.local/state/josemar-browser-control/logs/` and must not contain secrets.
- Tunnel failure: verify Tailscale connectivity, the server sidecar, ACLs, and that the configured public key matches the laptop key.
- Chrome start failure: verify a graphical session and supported Chrome/Chromium installation.
- For server-side deployment/routing diagnosis, follow `docs/browser-control.md` rather than changing tunnel/network settings from chat.

## Warnings

- **Dedicated profile only.** Never connect the operator's ordinary day-to-day browser profile. CDP automation can access everything available to that profile.
- Only one laptop can hold the reverse listener at a time.
- Never expose CDP on `0.0.0.0`, the LAN, or Tailscale Funnel. The supported path is the loopback CDP endpoint through the reverse SSH tunnel.
- Keep Tailscale ACL sources narrow; do not use wildcard sources.
- Authentication, CAPTCHA, permission, payment, 2FA, or credential entry remains operator-controlled as defined in the main skill.

## Disable / rollback

1. Set repository variable `BROWSER_CONTROL_ENABLED=false`, then redeploy.
2. Deployment removes stale Serve/authorized-key state and tears down the optional overlay while preserving normal named state according to the workflow contract.
3. Stop the laptop launcher/tunnel and close the dedicated profile.
4. Optional destructive volume cleanup is an operator action; follow `docs/browser-control.md` rather than performing it from chat.
