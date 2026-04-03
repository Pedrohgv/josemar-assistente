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
if [ -f /root/.openclaw/openclaw.json ]; then
    if [ "${TELEGRAM_ENABLED}" = "false" ]; then
        echo "Disabling Telegram channel (TELEGRAM_ENABLED=false)"
        sed -i 's/enabled: "${TELEGRAM_ENABLED}"/enabled: false/g' /root/.openclaw/openclaw.json
    else
        echo "Enabling Telegram channel (TELEGRAM_ENABLED=true or not set)"
        sed -i 's/enabled: "${TELEGRAM_ENABLED}"/enabled: true/g' /root/.openclaw/openclaw.json
    fi
fi

# Symlink workspace skills to OpenClaw skills directory
if [ -d "${WORKSPACE_DIR:-/root/.openclaw/workspace}/skills" ] && [ ! -d /root/.openclaw/skills/finance-assistant ]; then
    echo "Linking workspace skills to OpenClaw skills directory..."
    rm -rf /root/.openclaw/skills
    ln -s "${WORKSPACE_DIR:-/root/.openclaw/workspace}/skills" /root/.openclaw/skills
fi

# Run OpenClaw
echo "Starting OpenClaw gateway..."
exec "$@"
