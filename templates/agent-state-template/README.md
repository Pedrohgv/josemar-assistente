# Josemar Agent State Template

Template for the [Josemar Assistente](../) agent state repository.

This is a **private** git repo that stores the agent's identity, personality, skills, and memory. It is synced automatically with the running Docker container.

## Setup

1. Create a new **private** GitHub repository
2. Do NOT initialize with README (avoids merge conflicts)
3. Clone this template and push:
   ```bash
   git clone <this-template-url> my-agent-state
   cd my-agent-state
   rm -rf .git
   git init
   git add -A
   git commit -m "Initialize agent state from template"
   git branch -M main
   git remote add origin <your-private-repo-url>
   git push -u origin main
   ```
4. Set the repo URL in your josemar-assistente deployment:
   - **Environment variable:** `WORKSPACE_STATE_REPO=https://github.com/user/josemar-agent-state.git`
   - **GitHub secret:** `WORKSPACE_REPO_TOKEN` (GitHub PAT with `repo` scope)

## File Map

| File | Purpose |
|------|---------|
| `AGENTS.md` | Operating instructions for the agent |
| `SOUL.md` | Persona, tone, boundaries |
| `USER.md` | User information and preferences |
| `IDENTITY.md` | Agent name, vibe, emoji |
| `MEMORY.md` | Long-term curated memory |
| `TOOLS.md` | Notes about tools and conventions |
| `HEARTBEAT.md` | Heartbeat checklist (optional) |
| `BOOT.md` | Startup checklist (optional) |
| `skills/` | Agent skills (SKILL.md + executables) |
| `memory/` | Daily memory logs (rotated) |
| `avatars/` | Agent avatar images |

## Security

- This must be a **private** repository
- `.gitignore` prevents accidental secret commits
- `.sync-manifest` explicitly lists what gets synced (no wildcards)
- Never store API keys, tokens, or passwords here

## Sync Strategy

- On container start: agent's local changes are committed, then merged with remote (remote wins conflicts)
- Periodic: changes are auto-committed and pushed (configurable interval)
- Memory logs are rotated based on `WORKSPACE_MEMORY_DAYS`
