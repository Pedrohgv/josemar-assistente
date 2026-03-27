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

# Copy avatars to workspace
if [ -d "/root/.openclaw-source/avatars" ]; then
    echo "📸 Copying avatars to workspace..."
    mkdir -p /root/.openclaw/workspace/avatars
    cp -r /root/.openclaw-source/avatars/* /root/.openclaw/workspace/avatars/
fi

# ============================================
# REPO SKILLS DEPLOYMENT
# ============================================
if [ -d "/root/.openclaw-source-skills" ]; then
    echo "📦 Deploying repo-maintained skills..."
    mkdir -p /root/.openclaw/repo-skills
    
    # Parse force overwrite list
    IFS=',' read -ra FORCE_LIST <<< "$FORCE_OVERWRITE_SKILLS"
    
    for skill_source in /root/.openclaw-source-skills/*; do
        [ -d "$skill_source" ] || continue  # Skip non-directories
        
        skill_name=$(basename "$skill_source")
        skill_dest="/root/.openclaw/repo-skills/$skill_name"
        
        # Check if this skill is in the force overwrite list
        force_overwrite=false
        for forced_skill in "${FORCE_LIST[@]}"; do
            [ "$forced_skill" = "$skill_name" ] && force_overwrite=true
        done
        
        if [ -e "$skill_dest" ]; then
            if [ "$force_overwrite" = true ]; then
                echo "   🔄 Force overwriting $skill_name"
                rm -rf "$skill_dest"
                cp -r "$skill_source" "$skill_dest"
            else
                echo "   ⏭️  Skipping $skill_name (already exists - preserving agent version)"
            fi
        else
            echo "   📥 Copying $skill_name"
            cp -r "$skill_source" "$skill_dest"
        fi
    done
    echo "✅ Repo skills deployment complete"
else
    echo "ℹ️  No repo skills to deploy (source directory not mounted)"
fi

# ============================================
# VALIDATION
# ============================================
if [ -f /root/.openclaw/openclaw.json ]; then
    echo "✅ Configuration file found"
else
    echo "❌ Configuration file not found at /root/.openclaw/openclaw.json"
    echo "   Make sure config directory is mounted correctly."
    exit 1
fi

# Fix Telegram config if needed (change 'closed' to 'allowlist' if present)
if [ -f /root/.openclaw/openclaw.json ]; then
    sed -i 's/"dmPolicy": "closed"/"dmPolicy": "allowlist"/g' /root/.openclaw/openclaw.json
fi

# Run OpenClaw
echo "🚀 Starting OpenClaw gateway..."
exec "$@"
