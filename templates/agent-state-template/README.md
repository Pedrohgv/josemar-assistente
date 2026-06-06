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
   - **GitHub secret:** `WORKSPACE_REPO_TOKEN` (GitHub PAT with `repo` scope and **write** permissions)

## First-Time Bootstrap

On the first run, if the workspace has no personality files (`SOUL.md`, `USER.md`, `AGENTS.md`), Hermes will guide initial setup through normal agent interaction.

**For clean bootstrap behavior, the initial commit to the state repo should NOT include personality `.md` files.** Only include:
- `.gitignore`
- `.sync-manifest`
- `skills/`
- `cron/jobs.json`
- `memory/`
- `avatars/`

Personality files (`SOUL.md`, `USER.md`, `AGENTS.md`, optionally `MEMORY.md`) are created/maintained by Hermes and automatically versioned by periodic sync.

This template also includes a memory checkpoint cron job (`cron/jobs.json`) plus `memory/flush-state.json` to keep daily memory logs updated incrementally with reduced duplication.

## Skill Ownership Model

This project separates skills by ownership:

- **Core repo-owned skills** ship from the main repository (`skills-factory/`) and are bundled into the Docker image.
- **User-owned skills** live in this private state repo (`skills/`) and are specific to each user/deployment.

Do not copy user-specific skills into the main repository. Keep them in the state repo.

### Skill edit policy

- Treat repo-owned core skills (`/opt/josemar/skills/*`) as maintained through normal development in the main public repository (branch/commit/PR).
- In runtime self-improvement flows, prefer writing a patch proposal for repo-owned skills instead of creating sidecar skills (for example `*-pitfalls`).
- User-owned skills in `/opt/data/workspace/skills/*` can be patched directly and are expected to be versioned through the state repo sync flow.
- Avoid duplicate skill sprawl: patch an existing user-owned skill before creating a new skill with overlapping scope.

**To trigger bootstrap-like setup on an existing deployment:**
1. Delete all personality `.md` files from the state repo and push
2. Deploy with `fresh_start: true` (deletes Docker volume, forces fresh clone)
3. On first message, Hermes will rebuild baseline context from your prompts and state

## File Map

| File | Purpose | Created by |
|------|---------|------------|
| `AGENTS.md` | Operating instructions for the agent | Agent / manual |
| `SOUL.md` | Persona, tone, boundaries | Agent / manual |
| `USER.md` | User information and preferences | Agent / manual |
| `MEMORY.md` | Long-term curated memory | Agent |
| `BOOT.md` | Startup checklist (optional) | Template / manual |
| `skills/` | Agent skills (SKILL.md + executables) | Manual |
| `cron/jobs.json` | Cron job definitions loaded by Hermes | Manual / agent |
| `memory/` | Daily memory logs (rotated) | Agent |
| `avatars/` | Agent avatar images | Manual |

## Security

- This must be a **private** repository
- `.gitignore` prevents accidental secret commits
- `.sync-manifest` explicitly lists what gets synced (no wildcards)
- Never store API keys, tokens, or passwords here

## Sync Strategy

- On container start: agent's local changes are committed, then merged with remote (remote wins conflicts)
- Periodic: changes are auto-committed and pushed (configurable interval)
- Memory logs are rotated based on `WORKSPACE_MEMORY_DAYS`
