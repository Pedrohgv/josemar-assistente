#!/usr/bin/env python3
import json

with open("/root/.openclaw/openclaw.json", "r") as f:
    data = json.load(f)
    
# Use "allowlist" instead of "closed" - "closed" is not a valid option
data["channels"]["telegram"]["dmPolicy"] = "allowlist"
data["channels"]["telegram"]["allowFrom"] = [190731460]
data["channels"]["telegram"]["groupPolicy"] = "open"

with open("/root/.openclaw/openclaw.json", "w") as f:
    json.dump(data, f, indent=2)
    
print("Config updated successfully - using 'allowlist' policy")
