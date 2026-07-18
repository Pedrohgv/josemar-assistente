#!/usr/bin/env bash
# Idempotent installer for the Josemar browser control Linux desktop launcher.
#
# Creates only user-level paths (~/.local/bin, ~/.local/share/applications)
# and renders the desktop entry from the repo template with a stable Exec path.
# Does NOT launch the browser or tunnel, does NOT configure autostart, and does
# NOT install any packages or require sudo.
#
# Ownership safety: the installer will NOT overwrite or delete a pre-existing
# ~/.local/bin/josemar-browser-control or ~/.local/share/applications/josemar-browser.desktop
# unless it is owned by this installer (a symlink to this repo's launcher, or a
# desktop file carrying the X-Josemar-Managed=true marker). Foreign files are
# left in place and the installer exits with an error.
#
# Usage: install-launcher.sh            # install/update
#        install-launcher.sh --uninstall # remove only files this installer owns
#
# --uninstall removes only a symlink pointing to this repo launcher and a
# desktop file carrying the Josemar managed marker. It does NOT delete the
# Chrome profile (~/.josemar-chrome-profile), the SSH key
# (~/.ssh/josemar_browser_tunnel), the known_hosts file, or any credentials.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
readonly SCRIPT_DIR REPO_ROOT

readonly LAUNCHER_SRC="$SCRIPT_DIR/josemar-browser-control"
readonly DESKTOP_SRC="$SCRIPT_DIR/josemar-browser.desktop.in"

readonly BIN_DIR="$HOME/.local/bin"
readonly APPS_DIR="$HOME/.local/share/applications"
readonly INSTALLED_BIN="$BIN_DIR/josemar-browser-control"
readonly INSTALLED_DESKTOP="$APPS_DIR/josemar-browser.desktop"
readonly MANAGED_MARKER="X-Josemar-Managed=true"

UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=1 ;;
        *) echo "install-launcher.sh: unknown argument: $arg" >&2; exit 2 ;;
    esac
done

die() { echo "install-launcher.sh: error: $*" >&2; exit 1; }
warn() { echo "install-launcher.sh: warning: $*" >&2; }

[ -n "$HOME" ] && [ -d "$HOME" ] || die "HOME is not set or does not exist"
[ -f "$LAUNCHER_SRC" ] || die "launcher script not found: $LAUNCHER_SRC"
[ -f "$DESKTOP_SRC" ] || die "desktop template not found: $DESKTOP_SRC"

# Returns 0 if $1 is a symlink pointing to this repo's launcher.
is_owned_symlink() {
    local path="$1"
    [ -L "$path" ] || return 1
    local target
    target="$(readlink -f "$path" 2>/dev/null)" || return 1
    [ "$target" = "$LAUNCHER_SRC" ]
}

# Returns 0 if $1 is a regular file containing the managed marker.
is_managed_desktop() {
    local path="$1"
    [ -f "$path" ] || return 1
    grep -q "^$MANAGED_MARKER$" "$path" 2>/dev/null
}

if [ "$UNINSTALL" -eq 1 ]; then
    echo "Uninstalling Josemar browser control launcher (user-level only)..."
    removed=0
    # Remove the symlink only if it points to this repo's launcher.
    if [ -e "$INSTALLED_BIN" ] || [ -L "$INSTALLED_BIN" ]; then
        if is_owned_symlink "$INSTALLED_BIN"; then
            rm -f "$INSTALLED_BIN" 2>/dev/null || true
            echo "Removed: $INSTALLED_BIN"
            removed=1
        else
            warn "not removing $INSTALLED_BIN: not a symlink to this repo's launcher (foreign file)"
        fi
    fi
    # Remove the desktop entry only if it carries the managed marker.
    if [ -e "$INSTALLED_DESKTOP" ]; then
        if is_managed_desktop "$INSTALLED_DESKTOP"; then
            rm -f "$INSTALLED_DESKTOP" 2>/dev/null || true
            echo "Removed: $INSTALLED_DESKTOP"
            removed=1
        else
            warn "not removing $INSTALLED_DESKTOP: missing managed marker (foreign file)"
        fi
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPS_DIR" 2>/dev/null || true
    fi
    echo "Preserved (not removed): Chrome profile, SSH key, known_hosts, credentials."
    if [ "$removed" -eq 0 ]; then
        echo "No owned launcher files were found to remove."
    fi
    exit 0
fi

echo "Installing Josemar browser control launcher (user-level only)..."
mkdir -p "$BIN_DIR" "$APPS_DIR"

# Preflight: validate BOTH destination paths are absent or installer-owned
# before creating/updating either, so a foreign file at one destination does
# not leave a partial install at the other.
bin_ok=0
if [ -e "$INSTALLED_BIN" ] || [ -L "$INSTALLED_BIN" ]; then
    if is_owned_symlink "$INSTALLED_BIN"; then
        bin_ok=1  # own symlink; refresh
    else
        die "refusing to overwrite $INSTALLED_BIN: not a symlink to this repo's launcher (foreign file). Remove it manually if you want this installer to manage it."
    fi
else
    bin_ok=1  # absent; create
fi

desktop_ok=0
if [ -e "$INSTALLED_DESKTOP" ]; then
    if is_managed_desktop "$INSTALLED_DESKTOP"; then
        desktop_ok=1  # own managed entry; re-render
    else
        die "refusing to overwrite $INSTALLED_DESKTOP: missing managed marker (foreign file). Remove it manually if you want this installer to manage it."
    fi
else
    desktop_ok=1  # absent; create
fi

# Both destinations are confirmed absent or installer-owned; commit the install.
if [ "$bin_ok" -eq 1 ]; then
    ln -sf "$LAUNCHER_SRC" "$INSTALLED_BIN"
    chmod 0755 "$LAUNCHER_SRC"
    echo "Installed: $INSTALLED_BIN -> $LAUNCHER_SRC"
fi
if [ "$desktop_ok" -eq 1 ]; then
    sed "s|__INSTALL_BIN__|$INSTALLED_BIN|g" "$DESKTOP_SRC" > "$INSTALLED_DESKTOP"
    chmod 0644 "$INSTALLED_DESKTOP"
    echo "Installed: $INSTALLED_DESKTOP"
fi

# Validate the desktop entry only if the validator is already installed.
if command -v desktop-file-validate >/dev/null 2>&1; then
    if ! desktop-file-validate "$INSTALLED_DESKTOP"; then
        warn "desktop-file-validate reported issues with $INSTALLED_DESKTOP"
    fi
fi
# Refresh the desktop database only if the tool is already installed.
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

echo "Done. Launch 'Josemar Browser' from your application menu."
echo "No autostart was configured. To uninstall, run: $0 --uninstall"