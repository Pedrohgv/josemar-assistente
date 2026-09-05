---
name: workspace-sync
description: Sync Josemar/Hermes state to the private git repo. Use when the user asks to sync state, save state, push state, commit state changes, check state sync status, pull state, or inspect the workspace state repo.
user-invocable: true
command-dispatch: tool
command-tool: workspace-sync
command-arg-mode: raw
categories:
  - workspace
  - git
  - sync
---

# State Sync Skill

Manages the git-backed Hermes state tree. Use this skill whenever the user asks to sync, save, commit, push, pull, inspect, or troubleshoot Josemar/Hermes state.

Common user requests that should invoke this skill:

- "sync state"
- "sync the state repo"
- "save my state"
- "push state changes"
- "commit state changes"
- "check state sync"
- "is the workspace synced?"
- "pull latest state"
- "show state repo status"

## Invocation

Canonical interface: bare PATH command `workspace-sync <action> [args...]` (no stdin). Every action emits JSON on stdout.

| Form | Default | Notes |
|------|---------|-------|
| `workspace-sync status` | — | exact action only |
| `workspace-sync diff` | — | exact action only |
| `workspace-sync push` | — | exact action only |
| `workspace-sync pull` | — | exact action only |
| `workspace-sync log [COUNT]` | 10 | COUNT must be one positive decimal integer |
| `workspace-sync commit [MESSAGE...]` | `Manual commit` | extra args joined with single spaces |
| `workspace-sync sync [MESSAGE...]` | `Auto-sync` | extra args joined with single spaces |
| `workspace-sync gh ARGS...` | — | at least one token; argv passed losslessly to `gh` |

Examples:

```bash
workspace-sync status
workspace-sync diff
workspace-sync log 20
workspace-sync commit Update state
workspace-sync sync Deploy from chat
workspace-sync push
workspace-sync pull
workspace-sync gh repo view owner/repo
```

Terminal argv is validated before any workspace access, never reads stdin, and invalid action/arity/count fails with a concise stderr usage and zero stdout.

## Compatibility Protocols

Deterministic slash command: `/workspace_sync ...` — bypasses the model and dispatches directly to the tool. `/workspace_sync` alone runs `sync`; other forms mirror the table above (`/workspace_sync log 20`, `/workspace_sync commit Update state`, `/workspace_sync gh repo view owner/repo`).

Legacy JSON stdin (Hermes command dispatch) remains supported:

```bash
echo '{"action": "status"}' | workspace-sync
echo '{"action": "commit", "message": "Update skills"}' | workspace-sync
echo '{"action": "gh", "command": "repo view owner/repo"}' | workspace-sync
```

## Authentication

Remotes stay credential-free. HTTPS auth uses an ephemeral `GIT_ASKPASS` helper reading `WORKSPACE_REPO_TOKEN` from the environment (no persisted `~/.git-credentials`); `gh` commands inherit it as `GH_TOKEN`.

## Available Actions

### status
NOT strictly read-only: refreshes user-owned skill registration (writes `.sync-manifest` entries for new `skills/<name>/SKILL.md` trees), then shows git status, branch, remote URL, and tracked files from `.sync-manifest`.

Returns:
- `branch`: current git branch
- `remote`: clean remote URL (without embedded credentials)
- `auth_configured`: whether credentials are set up
- `tracked_patterns`: patterns from `.sync-manifest`
- `status`: list of changed/untracked files

### diff
Refresh user-owned skill registration, then show pending changes (unstaged and staged).

### log
Show recent commit history: `workspace-sync log [COUNT]` (default 10).

### commit
Stage files matching `.sync-manifest` and commit with a message: `workspace-sync commit [MESSAGE...]` (default `Manual commit`). Does NOT push.

### push
Push current branch to remote: `workspace-sync push`. Returns `success: false` with error details if push fails.

### pull
Fetch from remote and merge (remote wins on conflicts): `workspace-sync pull`.

### sync
Full sync: commit manifest files, then push to remote: `workspace-sync sync [MESSAGE...]` (default `Auto-sync`). Returns `success: false` if either commit or push fails; never silently succeeds.

### gh
Run any `gh` CLI command: `workspace-sync gh <args...>`. Args reach the binary losslessly (never through a shell); the JSON `command` field is a display echo only.

## Notes

- Only files listed in `.sync-manifest` are staged/committed
- User-owned skill files under `skills/<name>/` are auto-registered in `.sync-manifest` when the skill directory contains `SKILL.md`; `status`, `diff`, `commit`, `pull`, and `sync` all refresh this registration
- Merge conflicts use remote-wins strategy
- The state worktree directory is `/opt/data` inside the container
- Remote URLs are kept clean (no embedded tokens)

## Troubleshooting

- With the pinned Hermes v2026.8.31 gateway terminal, invoke the bare PATH command (`workspace-sync status`); the absolute form `/usr/local/bin/workspace-sync status` is falsely rejected by the referenced-script guard.
- The gateway's referenced-script guard may also flag `gh` command bodies containing lifecycle-shaped words (e.g. "startup"/"periodic"); such bodies are not guaranteed to evade the scanner — prefer plain non-lifecycle phrasing.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `WORKSPACE_REPO_TOKEN` | GitHub PAT for authentication |
| `WORKSPACE_STATE_REPO` | Remote repository URL |
| `WORKSPACE_GIT_USER_NAME` | Git commit author name |
| `WORKSPACE_GIT_USER_EMAIL` | Git commit author email |
| `WORKSPACE_GIT_BRANCH` | Default branch name |
