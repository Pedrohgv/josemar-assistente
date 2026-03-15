#!/bin/sh
# Docker entrypoint script for OpenClaw

set -e

echo "🔧 Starting Josemar Assistente..."

# Check for required environment variables
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "⚠️  TELEGRAM_BOT_TOKEN not set. Telegram channel will not work."
fi

if [ -z "$ZAI_API_KEY" ]; then
    echo "⚠️  ZAI_API_KEY not set. Z.AI provider will not work."
fi

# Create .openclaw directory if it doesn't exist
mkdir -p /root/.openclaw

# If config directory is mounted, ensure it's copied correctly
if [ -d "/root/.openclaw-source" ]; then
    echo "📋 Copying configuration from source..."
    cp -r /root/.openclaw-source/* /root/.openclaw/ 2>/dev/null || true
fi

# Validate configuration
if [ -f /root/.openclaw/openclaw.json5 ]; then
    echo "✅ Configuration file found"
else
    echo "❌ Configuration file not found at /root/.openclaw/openclaw.json5"
    echo "   Make sure config directory is mounted correctly."
    exit 1
fi

# Run OpenClaw
echo "🚀 Starting OpenClaw gateway..."
exec "$@"
