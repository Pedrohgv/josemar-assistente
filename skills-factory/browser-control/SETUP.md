# Browser Control First-Time Setup

This is the operator-facing setup walkthrough for the browser-control feature.
The companion `SKILL.md` covers runtime driving of an already-connected
browser; this file covers the one-time setup that makes the connection
possible. When the user asks how to set up browser control for the first
time, read this file and walk them through it. Do not paraphrase from memory.

The authoritative source of truth for any detail here is
`docs/browser-control.md` in the repo. If this file and that doc disagree,
the doc wins; flag the discrepancy so the doc and this file can be reconciled.

## Prerequisites

- The Josemar Tailscale node is up and the laptop is a member of the same
  tailnet.
- Chrome/Chromium 136 or newer on the laptop.
- The operator has access to the GitHub repository Settings (Secrets and
  Variables > Actions) for the Josemar repo.

## One-time setup (all platforms)

### 1. Generate an SSH keypair on the laptop

```bash
ssh-keygen -t ed25519 -f ~/.ssh/josemar_browser_tunnel -N ""
```

This produces:
- `~/.ssh/josemar_browser_tunnel` (private key, keep secret on the laptop)
- `~/.ssh/josemar_browser_tunnel.pub` (public key, upload to GitHub)

### 2. Add the public key as the `BROWSER_TUNNEL_AUTHORIZED_KEY` secret

In GitHub: Settings > Secrets and variables > Actions > New repository secret.

- Name: `BROWSER_TUNNEL_AUTHORIZED_KEY`
- Value: the single-line contents of `~/.ssh/josemar_browser_tunnel.pub`
  (OpenSSH format, e.g. `ssh-ed25519 AAAA... user@example.com`)

### 3. Allow the laptop to reach tcp:2222 on the server (Tailscale ACL)

In the Tailscale admin console (ACLs), add an accept rule for the laptop to
reach the server's tcp:2222. Use an explicit tag or host alias for both
`src` and `dst`; do not use a wildcard `src`.

```json
{
  "action": "accept",
  "src":    ["tag:laptop"],
  "dst":    ["josemar-server:2222"]
}
```

Replace `tag:laptop` with the laptop's tag (or a specific user) and
`josemar-server` with the Tailscale node name set by `TAILSCALE_HOSTNAME`
(default `josemar-server`).

### 4. Enable the overlay on the server

In GitHub: Settings > Secrets and variables > Actions > Variables.

- Set `BROWSER_CONTROL_ENABLED` to `true`.

Then run the deploy workflow. It will:
- Populate the `tailscale-serve-config` named volume with the tcp:2222
  TCPForward to the browser-tunnel sidecar.
- Populate the `browser-tunnel-authorized-keys` named volume with the
  operator's public key.
- Apply `docker-compose.browser-control.yml` and add `browser-control` to
  `COMPOSE_PROFILES`.
- Verify the `browser-tunnel` sidecar is running and Tailscale Serve
  tcp:2222 targets the exact Hermes IP with no Funnel.

Defaults for the internal Docker subnet and IPs are almost always fine; only
override `BROWSER_CONTROL_SUBNET`, `BROWSER_CONTROL_GATEWAY`,
`BROWSER_CONTROL_HERMES_IP`, and `BROWSER_CONTROL_TAILSCALE_IP` together if
the default `172.31.250.0/29` collides with an existing network on the host.

### 5. Install the laptop launcher (Linux Mint, tested)

From a checkout of this repo on the laptop:

```bash
bash laptop/linux/install-launcher.sh
```

Idempotent, user-level only (no sudo). Creates:
- `~/.local/bin/josemar-browser-control`
- `~/.local/share/applications/josemar-browser.desktop`

After install, "Josemar Browser" appears in the Mint application menu.

### 6. Start the browser and tunnel

Launch "Josemar Browser" from the Mint menu, or run:

```bash
josemar-browser-control start
```

The launcher starts the dedicated Chrome profile
(`~/.josemar-chrome-profile`) with loopback-only remote debugging on
`127.0.0.1:9222`, waits for the CDP endpoint, then opens the reverse SSH
tunnel to the server's `browser-tunnel` sidecar.

Status check:

```bash
josemar-browser-control status
```

### 7. Verify end-to-end

From the laptop, after the tunnel is up:

```bash
curl -s http://127.0.0.1:9222/json/version
```

On the server:

```bash
docker compose exec -T hermes curl -s http://127.0.0.1:9222/json/version
```

Both should return Chrome's CDP version JSON. The server-side call only
works while the laptop's reverse tunnel is up.

## macOS / Windows

No launcher is shipped for macOS or Windows. See the "macOS (untested,
best-effort)" and "Windows (untested, best-effort)" sections of
`docs/browser-control.md` for architectural suggestions on adapting the
Linux lifecycle script. These are not tested or supported.

## Daily use

- **Start**: launch "Josemar Browser" from the menu, or
  `josemar-browser-control start`.
- **Already running**: launching again opens/focuses a new dedicated window
  instead of starting a duplicate controller.
- **Stop**: closing all dedicated Josemar Chrome windows stops the tunnel.
  Also: right-click the menu entry > "Stop Josemar Browser", or
  `josemar-browser-control stop`.
- **Status**: `josemar-browser-control status` reports controller/chrome/
  tunnel/cdp state without reading page or session contents.

## What persists across reboot

- The dedicated Chrome profile (`~/.josemar-chrome-profile`).
- The SSH key (`~/.ssh/josemar_browser_tunnel`) and known_hosts
  (`~/.ssh/josemar_browser_tunnel_known_hosts`).
- The launcher install (`~/.local/bin`, `~/.local/share/applications`).
- No session, tunnel, or Chrome process survives a reboot; the launcher is
  strictly on-demand.

## Troubleshooting

- `josemar-browser-control status` — check controller/chrome/tunnel/cdp state.
- Logs live under `~/.local/state/josemar-browser-control/logs/` (no secrets).
- If the tunnel fails, confirm Tailscale is up (`tailscale status`), the
  server sidecar is running, and the `BROWSER_TUNNEL_AUTHORIZED_KEY` secret
  matches this laptop's public key.
- If Chrome fails to start, confirm a graphical session is active
  (`DISPLAY`/`WAYLAND_DISPLAY`) and Chrome 136+ is installed.

## Warnings

- **Dedicated profile only.** Never tunnel your normal Chrome profile. A
  remote automation agent with CDP access can read/act on everything in that
  profile, including logged-in sessions. The launcher uses
  `~/.josemar-chrome-profile` by design.
- Only one laptop can hold the reverse listener at a time.
- Never expose CDP to `0.0.0.0`, the LAN, or Tailscale Funnel. The only
  supported path is the reverse SSH tunnel described here.
- Do not use broad Tailscale ACL `src` rules (e.g. `["*"]`); use an explicit
  tag or user for the laptop.

## Disable / rollback

1. Set `BROWSER_CONTROL_ENABLED=false` (or unset it) and re-run deploy.
2. The workflow writes `{}` into `tailscale-serve-config`, clears
   `browser-tunnel-authorized-keys`, uses base Compose only, and tears down
   any previous overlay service/network.
3. Stop the laptop's tunnel: `josemar-browser-control stop`, and close the
   dedicated Chrome profile.
4. Optionally remove the `browser-tunnel-state` volume:
   `docker volume rm <project>_browser-tunnel-state`. The
   `tailscale-serve-config` and `browser-tunnel-authorized-keys` volumes are
   kept so a future re-enable does not require re-populating them.