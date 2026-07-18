# Laptop launcher support boundaries

This directory ships the **Linux** on-demand launcher for the Josemar remote
browser control feature. Linux Mint is the tested/supported implementation.

## Linux (tested/supported)

- `linux/josemar-browser-control` — bash lifecycle controller (`start`,
  `stop`, `status`).
- `linux/josemar-browser.desktop.in` — desktop entry template.
- `linux/install-launcher.sh` — idempotent user-level installer with
  `--uninstall`.

See `docs/browser-control.md` for one-time install, menu/panel use, stop
behavior, status/troubleshooting, uninstall, and what persists across reboot.

Nothing here starts at system/login startup. The launcher is strictly
on-demand, launched from the Mint application menu / clickable desktop entry.

## macOS and Windows (untested, best-effort suggestions only)

No shipped implementations. See the "macOS (untested, best-effort)" and
"Windows (untested, best-effort)" sections in `docs/browser-control.md` for
high-level architectural suggestions that require native testing and are not
authoritative. Do not treat them as supported implementations.