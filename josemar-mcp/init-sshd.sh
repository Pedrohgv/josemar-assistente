#!/command/with-contenv sh
# Josemar MCP sshd init script (s6-overlay cont-init.d).
#
# Starts a hardened OpenSSH daemon inside the Hermes container when
# JOSEMAR_MCP_ENABLED=true. The sshd shares the same PID namespace, gbrain
# state, and locks as the Hermes gateway. It starts as root (cont-init runs
# as root) and drops to the hermes user for forced commands via sshd's own
# privilege separation. The Hermes gateway continues to run as non-root
# hermes via s6-setuidgid in main-wrapper.sh.
#
# Constants (not configurable): SSH user `hermes`, SSH port 2223, forced
# command /usr/local/bin/josemar-knowledge-mcp-forced.
#
# Required input (fail clearly if missing):
#   JOSEMAR_MCP_ENABLED          Must be "true" to start sshd.
#   JOSEMAR_MCP_HERMES_IP        IPv4 on the josemar-mcp network to bind.
#   /josemar-mcp-authorized-keys/authorized_keys  Read-only authorized keys
#       (mounted from the josemar-mcp-authorized-keys named volume populated
#       by the deploy workflow).

set -eu
SERVICE_DIR="/etc/services.d/josemar-mcp-sshd"

log() {
    echo "[josemar-mcp-sshd] $*"
}

die() {
    echo "[josemar-mcp-sshd] ERROR: $*" >&2
    # Non-fatal: cont-init scripts should not abort the whole container if
    # an optional feature is misconfigured. The gateway must still start.
    # The deploy verification will catch the failure.
    return 0
}

# Only start when the feature is explicitly enabled.
if [ "${JOSEMAR_MCP_ENABLED:-false}" != "true" ]; then
    # Keep the s6 service down on every startup/restart when disabled.
    touch "${SERVICE_DIR}/down"
    log "JOSEMAR_MCP_ENABLED is not true; skipping sshd startup"
    exit 0
fi

# Only start as root (sshd needs root for privilege separation).
if [ "$(id -u)" != "0" ]; then
    die "sshd init must run as root (cont-init); current UID is $(id -u)"
    exit 0
fi

HERMES_IP="${JOSEMAR_MCP_HERMES_IP:-172.31.251.2}"
SSH_PORT=2223
FORCED_CMD="/usr/local/bin/josemar-knowledge-mcp-forced"
AUTHORIZED_KEYS="/josemar-mcp-authorized-keys/authorized_keys"
HOST_KEY_DIR="/var/lib/josemar-mcp-hostkeys"
HOST_KEY="${HOST_KEY_DIR}/ssh_host_ed25519_key"
RUNTIME_DIR="/etc/josemar-mcp/runtime"

# Validate inputs.
case "$HERMES_IP" in
    *[!0-9.]*)
        die "JOSEMAR_MCP_HERMES_IP must be an IPv4 address, got: $HERMES_IP"
        exit 0
        ;;
esac
dots=$(printf '%s' "$HERMES_IP" | tr -cd '.' | wc -c)
if [ "$dots" -ne 3 ]; then
    die "JOSEMAR_MCP_HERMES_IP must be an IPv4 address (a.b.c.d), got: $HERMES_IP"
    exit 0
fi

# Verify the forced-command wrapper and MCP server exist.
if [ ! -x "$FORCED_CMD" ]; then
    die "forced-command wrapper not found or not executable at $FORCED_CMD"
    exit 0
fi
MCP_SCRIPT="/opt/josemar/scripts/josemar_knowledge_mcp.py"
if [ ! -f "$MCP_SCRIPT" ]; then
    die "MCP server script not found at $MCP_SCRIPT"
    exit 0
fi

# Verify the hermes user exists.
if ! id hermes >/dev/null 2>&1; then
    die "hermes user does not exist in the image"
    exit 0
fi

# OpenSSH rejects a locked Unix account before it evaluates a public key. Give
# hermes a freshly generated, random crypt(3) password hash that is not
# guessable and is never disclosed; sshd_config still disables password and
# keyboard-interactive authentication. This is safer than `usermod -U` plus an
# empty password, and permits public-key authentication to reach sshd's key
# checks.
HERMES_RANDOM_PASSWORD="$(openssl rand -hex 32)"
HERMES_PASSWORD_HASH="$(printf '%s' "$HERMES_RANDOM_PASSWORD" | openssl passwd -6 -stdin)"
unset HERMES_RANDOM_PASSWORD
usermod -p "$HERMES_PASSWORD_HASH" hermes
unset HERMES_PASSWORD_HASH
chage -E -1 -I -1 -m 0 -M 99999 hermes

# Verify authorized keys are present and valid.
if [ ! -f "$AUTHORIZED_KEYS" ]; then
    die "authorized keys file not found at $AUTHORIZED_KEYS (mount the josemar-mcp-authorized-keys named volume)"
    exit 0
fi
if ! grep -Ev '^[[:space:]]*(#|$)' "$AUTHORIZED_KEYS" | grep -Eq '^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)) '; then
    die "no valid SSH public key line found in $AUTHORIZED_KEYS"
    exit 0
fi

# Prepare runtime dir.
mkdir -p "$RUNTIME_DIR" /run/sshd
chmod 0755 "$RUNTIME_DIR" /run/sshd

# Persistent Ed25519 host key in the dedicated root-owned named volume so the client
# known_hosts stays stable across redeploys. Generate on first start only.
mkdir -p "$HOST_KEY_DIR"
chown root:root "$HOST_KEY_DIR"
chmod 0700 "$HOST_KEY_DIR"
if [ ! -f "$HOST_KEY" ]; then
    log "generating new Ed25519 SSH host key at $HOST_KEY"
    ssh-keygen -q -t ed25519 -N "" -C "josemar-mcp-host" -f "$HOST_KEY"
fi
chown root:root "$HOST_KEY" "$HOST_KEY.pub"
chmod 0600 "$HOST_KEY"
chmod 0644 "$HOST_KEY.pub"

# Render sshd config from template.
RENDERED="${RUNTIME_DIR}/sshd_config"
sed -e "s|__JOSEMAR_MCP_HERMES_IP__|$HERMES_IP|g" \
    /etc/josemar-mcp/sshd_config.template > "$RENDERED"
chmod 0644 "$RENDERED"

# Copy authorized_keys into runtime dir with restrictive key options prefixed
# so a supplied plain public key cannot gain a shell and can only run the
# forced MCP command.
RUNTIME_KEYS="${RUNTIME_DIR}/authorized_keys"
: > "$RUNTIME_KEYS"
KEY_OPTS="no-pty,no-X11-forwarding,no-agent-forwarding,no-user-rc,no-port-forwarding,command=\"$FORCED_CMD\""
while IFS= read -r line; do
    case "$line" in
        ''|'#'*)
            continue
            ;;
    esac
    printf '%s %s\n' "$KEY_OPTS" "$line" >> "$RUNTIME_KEYS"
done < "$AUTHORIZED_KEYS"
# sshd StrictModes requires authorized_keys to not be writable by group/others.
# Root-owned 0644 satisfies StrictModes and is readable by hermes.
chown root:root "$RUNTIME_KEYS"
chmod 0644 "$RUNTIME_KEYS"

# Validate sshd config before starting.
log "validating sshd config"
if ! /usr/sbin/sshd -t -f "$RENDERED"; then
    die "sshd config validation failed"
    exit 0
fi

# Enable the s6 longrun only after provisioning and validation succeeds. The
# longrun keeps sshd foregrounded and s6 restarts it if it exits.
rm -f "${SERVICE_DIR}/down"
log "enabled supervised sshd on ${HERMES_IP}:${SSH_PORT} (user: hermes)"
