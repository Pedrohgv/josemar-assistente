#!/bin/sh
# Docker entrypoint script for OpenClaw

set -e

echo "Starting Josemar Assistente..."

# Check for required environment variables
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "WARNING: TELEGRAM_BOT_TOKEN not set. Telegram channel will not work."
fi

if [ -z "$ZAI_API_KEY" ]; then
    echo "WARNING: ZAI_API_KEY not set. Z.AI provider will not work."
fi

# Create .openclaw directory if it doesn't exist
mkdir -p /root/.openclaw

# If config directory is mounted, ensure it's copied correctly
if [ -d "/root/.openclaw-source" ]; then
    echo "Copying configuration from source..."
    cp -r /root/.openclaw-source/* /root/.openclaw/ 2>/dev/null || true

    # Keep OpenClaw recovery snapshot aligned with repo source config.
    # This prevents stale openclaw.json.last-good content from restoring
    # obsolete env variable names after config schema changes.
    if [ -f "/root/.openclaw-source/openclaw.json" ]; then
        cp /root/.openclaw-source/openclaw.json /root/.openclaw/openclaw.json
        cp /root/.openclaw-source/openclaw.json /root/.openclaw/openclaw.json.last-good
    fi

    # Normalize config snapshots used by OpenClaw recovery.
    # This keeps TELEGRAM_ENABLED typed as boolean even when OpenClaw
    # restores from a backup snapshot in JSON form.
    for cfg in /root/.openclaw/openclaw.json /root/.openclaw/openclaw.json.last-good /root/.openclaw/openclaw.json.bak /root/.openclaw/openclaw.json.bak.*; do
        [ -f "$cfg" ] || continue

        if [ "${TELEGRAM_ENABLED}" = "false" ]; then
            sed -E -i 's/enabled[[:space:]]*:[[:space:]]*"\$\{TELEGRAM_ENABLED\}"/enabled: false/g' "$cfg"
            sed -E -i 's/"enabled"[[:space:]]*:[[:space:]]*"\$\{TELEGRAM_ENABLED\}"/"enabled": false/g' "$cfg"
        else
            sed -E -i 's/enabled[[:space:]]*:[[:space:]]*"\$\{TELEGRAM_ENABLED\}"/enabled: true/g' "$cfg"
            sed -E -i 's/"enabled"[[:space:]]*:[[:space:]]*"\$\{TELEGRAM_ENABLED\}"/"enabled": true/g' "$cfg"
        fi
    done
fi

# Copy credentials from read-only mount to writable location
# Structure: credentials/<service>/<files> -> /root/.openclaw/credentials/<service>/<files>
if [ -d "/root/.openclaw-credentials" ]; then
    echo "Mounting credentials..."
    for service_dir in /root/.openclaw-credentials/*/; do
        [ -d "$service_dir" ] || continue
        service_name=$(basename "$service_dir")
        mkdir -p "/root/.openclaw/credentials/$service_name"
        cp -r "$service_dir"* "/root/.openclaw/credentials/$service_name/" 2>/dev/null || true
    done
fi

# Copy avatars to workspace
if [ -d "/root/.openclaw-source/avatars" ]; then
    echo "Copying avatars to workspace..."
    mkdir -p /root/.openclaw/workspace/avatars
    cp -r /root/.openclaw-source/avatars/* /root/.openclaw/workspace/avatars/
fi

# ============================================
# WORKSPACE GIT SYNC
# ============================================
if [ -n "$WORKSPACE_STATE_REPO" ]; then
    echo "Running workspace git sync..."
    /usr/local/bin/workspace-sync.sh || echo "WARNING: Workspace git sync failed, continuing without sync"
else
    echo "WORKSPACE_STATE_REPO not configured, skipping git sync"
fi

# ============================================
# VALIDATION
# ============================================
if [ -f /root/.openclaw/openclaw.json ]; then
    echo "Configuration file found"
else
    echo "ERROR: Configuration file not found at /root/.openclaw/openclaw.json"
    echo "   Make sure config directory is mounted correctly."
    exit 1
fi

# Fix Telegram config if needed (change 'closed' to 'allowlist' if present)
if [ -f /root/.openclaw/openclaw.json ]; then
    sed -i 's/"dmPolicy": "closed"/"dmPolicy": "allowlist"/g' /root/.openclaw/openclaw.json
fi

# Handle TELEGRAM_ENABLED environment variable
# Convert string "true"/"false" to proper boolean in config
# (kept for direct openclaw.json path normalization too)
if [ -f /root/.openclaw/openclaw.json ]; then
    if [ "${TELEGRAM_ENABLED}" = "false" ]; then
        echo "Disabling Telegram channel (TELEGRAM_ENABLED=false)"
        sed -i 's/enabled: "${TELEGRAM_ENABLED}"/enabled: false/g' /root/.openclaw/openclaw.json
    else
        echo "Enabling Telegram channel (TELEGRAM_ENABLED=true or not set)"
        sed -i 's/enabled: "${TELEGRAM_ENABLED}"/enabled: true/g' /root/.openclaw/openclaw.json
    fi
fi

# Keep /root/.openclaw/skills as a real directory.
# Legacy deployments may have it symlinked to workspace/skills; remove that to
# avoid source confusion now that repo-shipped skills are loaded from /opt/josemar/skills.
if [ -L /root/.openclaw/skills ]; then
    echo "Removing legacy /root/.openclaw/skills symlink..."
    rm -f /root/.openclaw/skills
fi
mkdir -p /root/.openclaw/skills

# Run OpenClaw
echo "Starting OpenClaw gateway..."
exec "$@"
