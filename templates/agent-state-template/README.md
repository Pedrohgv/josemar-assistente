# Josemar Agent State Template

Template for the [Josemar Assistente](../) agent state repository.

This is a **private** git repo that stores the agent's identity, personality, user-owned skills, cron jobs, and concise memory files. At runtime it is checked out directly at Hermes home (`/opt/data`) so tracked files follow Hermes' native structure.

LLM-facing prompt and instruction files in this template should be authored in English, even when the assistant is expected to interact with the user in another language.

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

On the first run, if the state repo has no personality files (`SOUL.md`, `memories/USER.md`, `AGENTS.md`), Hermes will guide initial setup through normal agent interaction.

**For clean bootstrap behavior, the initial commit to the state repo should NOT include personality `.md` files.** Only include:
- `.gitignore`
- `.sync-manifest`
- `skills/`
- `cron/jobs.json`
- `avatars/`

Personality and memory files (`SOUL.md`, `memories/USER.md`, `AGENTS.md`, optionally `memories/MEMORY.md`) are created/maintained by Hermes and automatically versioned by periodic sync.

## Skill Ownership Model

This project separates skills by ownership:

- **Core repo-owned skills** ship from the main repository (`skills-factory/`) and are bundled into the Docker image.
- **User-owned skills** live in this private state repo (`skills/`) and are specific to each user/deployment.

Do not copy user-specific skills into the main repository. Keep them in the state repo.

### Skill edit policy

- Treat repo-owned core skills (`/opt/josemar/skills/*`) as maintained through normal development in the main public repository (branch/commit/PR).
- In runtime self-improvement flows, prefer writing a patch proposal for repo-owned skills instead of creating sidecar skills (for example `*-pitfalls`).
- User-owned skills in `/opt/data/skills/*` can be patched directly and are expected to be versioned through the state repo sync flow.
- Runtime-created user skills are auto-registered for sync when their directory contains `SKILL.md`.
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
| `memories/USER.md` | User information and preferences | Agent / manual |
| `memories/MEMORY.md` | Long-term curated memory | Agent |
| `BOOT.md` | Startup checklist (optional) | Template / manual |
| `skills/` | Agent skills (SKILL.md + executables) | Agent / manual |
| `cron/jobs.json` | Cron job definitions loaded by Hermes | Manual / agent |
| `avatars/` | Agent avatar images | Manual |

## Mnemosyne Pilot: Archive Status of Memory Files

When the optional Mnemosyne pilot overlay (`docker-compose.mnemosyne.yml`) is
enabled, the assistant uses upstream-native Mnemosyne semantic memory instead
of static file injection. In that mode:

- `memories/MEMORY.md` and `memories/USER.md` are **archived but not injected**.
  They remain at their versioned paths (tracked by `.sync-manifest` and
  `.gitignore`) as explicit **rollback material**. No automatic migration or
  deletion is performed; the files are left untouched on disk.
- The runtime Mnemosyne database lives separately under
  `/opt/data/mnemosyne/data` (a runtime SQLite store, NOT versioned here) and
  is preserved across rollback.
- To roll back to static injection, disable the Mnemosyne overlay (remove
  `docker-compose.mnemosyne.yml` from `COMPOSE_FILE`). The container init
  restores `memory.memory_enabled`/`user_profile_enabled` to true and removes
  only the installer-owned plugin/override-skill artifacts, while preserving
  the Mnemosyne DB for future re-activation.

The pilot is **passive-only ingestion**: automatic ingestion is passive raw
user-turn capture (global cross-session). Explicit upstream-native
mutation/management tools (including mutating operations) remain available to
the agent. No auto-sleep, reflection, or LLM consolidation runs in the pilot.
Full native Mnemosyne tools are available; this is upstream-native behavior.
LLM consolidation can infer summaries/facts but is intentionally off pending a
later explicit user decision on LLM provider, privacy, and cost. That decision
and the future option are documented in `docs/memory-embeddings-evaluation.md`.

## Security

- This must be a **private** repository
- `.gitignore` is deny-by-default, so only explicit state paths can be staged normally
- `.sync-manifest` explicitly lists what gets synced (no wildcards)
- Never store API keys, tokens, or passwords here

## Sync Strategy

- On container start: agent's local changes are committed, then merged with remote (remote wins conflicts)
- Periodic: changes are auto-committed and pushed (configurable interval)
