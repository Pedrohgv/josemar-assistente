#!/bin/sh
# Browser tunnel entrypoint.
#
# Renders sshd_config.template with the runtime bind IP, prepares a persistent
# Ed25519 host key in /var/lib/browser-tunnel (browser-tunnel-state volume),
# copies authorized_keys from the read-only named volume into a tmpfs runtime
# dir with restrictive key options prefixed, and starts sshd in the foreground.
#
# Constants (not configurable): SSH user `tunnel`, SSH port 2222, CDP port 9222.
#
# Required input (fail clearly if missing):
#   BROWSER_CONTROL_HERMES_IP   IPv4 on the browser-control network to bind.
#   /authorized-keys/authorized_keys  Read-only authorized keys (mounted from
#                                      the browser-tunnel-authorized-keys
#                                      named volume populated by the deploy
#                                      workflow).
#
# The container runs with a read-only root filesystem. /run, /tmp, and
# /etc/ssh/runtime are tmpfs. The host key lives in the persistent
# browser-tunnel-state volume.

set -eu

log() {
    echo "[browser-tunnel] $*"
}

die() {
    echo "[browser-tunnel] ERROR: $*" >&2
    exit 1
}

require_ipv4() {
    name="$1"
    value="$2"
    case "$value" in
        *[!0-9.]*)
            die "$name must be an IPv4 address, got: $value"
            ;;
        *)
            ;;
    esac
    # crude dotted-quad check
    dots=$(printf '%s' "$value" | tr -cd '.' | wc -c)
    if [ "$dots" -ne 3 ]; then
        die "$name must be an IPv4 address (a.b.c.d), got: $value"
    fi
}

HERMES_IP="${BROWSER_CONTROL_HERMES_IP:-172.31.250.2}"
SSH_PORT=2222
CDP_PORT=9222
TUNNEL_USER=tunnel
AUTHORIZED_KEYS=/authorized-keys/authorized_keys

require_ipv4 BROWSER_CONTROL_HERMES_IP "$HERMES_IP"

if ! id "$TUNNEL_USER" >/dev/null 2>&1; then
    die "forwarding-only user '$TUNNEL_USER' does not exist in the image"
fi

if [ ! -f "$AUTHORIZED_KEYS" ]; then
    die "authorized keys file not found at $AUTHORIZED_KEYS (mount the browser-tunnel-authorized-keys named volume)"
fi

# Validate that authorized_keys contains at least one non-comment, non-blank
# line that looks like an SSH public key. We do not parse the key fully here;
# sshd will reject malformed keys at load time.
if ! grep -Ev '^[[:space:]]*(#|$)' "$AUTHORIZED_KEYS" | grep -Eq '^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)) '; then
    die "no valid SSH public key line found in $AUTHORIZED_KEYS"
fi

# Prepare runtime tmpfs dirs.
mkdir -p /etc/ssh/runtime /run /tmp
chmod 0755 /etc/ssh/runtime
chmod 1777 /run /tmp

# Persistent Ed25519 host key in the browser-tunnel-state volume so laptop
# known_hosts stays stable across redeploys. Generate on first start only.
# The container starts as root (sshd needs to drop privileges), so the key is
# owned by root:root with 0600.
HOST_KEY_DIR=/var/lib/browser-tunnel
HOST_KEY="$HOST_KEY_DIR/ssh_host_ed25519_key"
mkdir -p "$HOST_KEY_DIR"
chmod 0700 "$HOST_KEY_DIR"
if [ ! -f "$HOST_KEY" ]; then
    log "generating new Ed25519 SSH host key at $HOST_KEY"
    ssh-keygen -q -t ed25519 -N "" -C "browser-tunnel-host" -f "$HOST_KEY"
fi
chmod 0600 "$HOST_KEY"
chmod 0644 "$HOST_KEY.pub"

# Render sshd config from template.
RENDERED=/etc/ssh/runtime/sshd_config
sed -e "s|__BROWSER_CONTROL_HERMES_IP__|$HERMES_IP|g" \
    /etc/ssh/sshd_config.template > "$RENDERED"
chmod 0644 "$RENDERED"

# Copy authorized_keys into tmpfs with restrictive key options prefixed so a
# supplied plain public key cannot gain a shell and can only create the
# intended listener. The options are written on the same line as the key,
# separated by a space, per authorized_keys format.
#
# Options enforced per key:
#   no-pty,no-X11-forwarding,no-agent-forwarding,no-user-rc
#   permitlisten="127.0.0.1:9222"
#
# We intentionally do NOT set no-port-forwarding here: that would also disable
# remote forwarding. Remote-only forwarding is enforced globally by
# AllowTcpForwarding remote + PermitListen 127.0.0.1:9222 + GatewayPorts no.
# OpenSSH applies the most restrictive of global and per-key options, so the
# per-key permitlisten does not relax the global PermitListen.
RUNTIME_KEYS=/etc/ssh/runtime/authorized_keys
: > "$RUNTIME_KEYS"
chmod 0600 "$RUNTIME_KEYS"
KEY_OPTS='no-pty,no-X11-forwarding,no-agent-forwarding,no-user-rc,permitlisten="127.0.0.1:9222"'
while IFS= read -r line; do
    # Skip blank lines and comments.
    case "$line" in
        ''|'#'*)
            continue
            ;;
    esac
    printf '%s %s\n' "$KEY_OPTS" "$line" >> "$RUNTIME_KEYS"
done < "$AUTHORIZED_KEYS"
# sshd StrictModes requires authorized_keys to not be writable by group/others.
# The file is read by sshd after dropping privileges to the `tunnel` user, so it
# must be readable by that user. Root-owned 0644 satisfies StrictModes (no
# group/other write) and is readable by tunnel. Do NOT chown to tunnel: that
# would require extra caps (FOWNER/DAC_OVERRIDE) to chmod afterward.
chmod 0644 "$RUNTIME_KEYS"

# Validate sshd config before starting.
log "validating sshd config"
if ! /usr/sbin/sshd -t -f "$RENDERED"; then
    die "sshd config validation failed"
fi

log "starting sshd on ${HERMES_IP}:${SSH_PORT} (remote forward -> 127.0.0.1:${CDP_PORT})"
exec /usr/sbin/sshd -D -e -f "$RENDERED"