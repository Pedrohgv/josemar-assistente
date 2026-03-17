#!/usr/bin/env python3
import json
import os

config_path = "/root/.openclaw/openclaw.json"

with open(config_path, "r") as f:
    data = json.load(f)

# Add Z.AI auth profile if ZAI_API_KEY is set
zai_key = os.environ.get("ZAI_API_KEY")
if zai_key:
    if "auth" not in data:
        data["auth"] = {}
    if "profiles" not in data["auth"]:
        data["auth"]["profiles"] = []
    
    # Check if zai profile already exists
    zai_exists = False
    for profile in data["auth"]["profiles"]:
        if profile.get("id") == "zai":
            zai_exists = True
            break
    
    if not zai_exists:
        data["auth"]["profiles"].append({
            "id": "zai",
            "apiKey": zai_key,
            "provider": "zai"
        })
        print("Added Z.AI auth profile")

# Set agent model to zai/glm-4.7
if "agents" not in data:
    data["agents"] = {}
if "defaults" not in data["agents"]:
    data["agents"]["defaults"] = {}

# Set the primary model
data["agents"]["defaults"]["model"] = {
    "primary": "zai/glm-4.7"
}

# Set model aliases
if "models" not in data["agents"]["defaults"]:
    data["agents"]["defaults"]["models"] = {}

data["agents"]["defaults"]["models"]["zai/glm-4.7"] = {
    "alias": "GLM 4.7"
}

# Fix Telegram config
data["channels"]["telegram"]["dmPolicy"] = "allowlist"

# Get Telegram user IDs from environment variable (comma-separated)
telegram_user_ids = os.environ.get("TELEGRAM_USER_IDS", "")
if telegram_user_ids:
    # Parse comma-separated list of user IDs
    try:
        user_ids = [int(id_str.strip()) for id_str in telegram_user_ids.split(",") if id_str.strip()]
        data["channels"]["telegram"]["allowFrom"] = user_ids
        print(f"- Added {len(user_ids)} Telegram user ID(s) to allowFrom")
    except ValueError as e:
        print(f"Warning: Could not parse TELEGRAM_USER_IDS: {e}")
        data["channels"]["telegram"]["allowFrom"] = []
else:
    data["channels"]["telegram"]["allowFrom"] = []
    print("Warning: TELEGRAM_USER_IDS not set, no users pre-approved")

data["channels"]["telegram"]["groupPolicy"] = "open"

with open(config_path, "w") as f:
    json.dump(data, f, indent=2)

print("Config updated successfully:")
print("- Set primary model to zai/glm-4.7")
print("- Fixed Telegram dmPolicy to 'allowlist'")
print("- Set Telegram groupPolicy to 'open'")
if zai_key:
    print("- Added Z.AI auth profile")
