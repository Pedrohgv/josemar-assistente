---
name: workspace-sync
description: Manage workspace git operations - sync, commit, push, pull, status, and GitHub CLI operations
user-invocable: true
command-dispatch: tool
command-tool: workspace-sync
command-arg-mode: raw
categories:
  - workspace
  - git
  - sync
---

# Workspace Sync Skill

Manages the git-backed workspace state. Use this skill to manually sync, commit changes, check status, or interact with GitHub.

## Automatic Authentication

The skill automatically configures Git credentials when `WORKSPACE_REPO_TOKEN` environment variable is present. It:

1. Cleans any existing tokens from the remote URL (prevents duplication)
2. Configures `credential.helper store` for persistent authentication
3. Creates `~/.git-credentials` with proper format
4. Also configures `gh` CLI if available
5. Sets `user.name` and `user.email` from `WORKSPACE_GIT_USER_NAME` and `WORKSPACE_GIT_USER_EMAIL`

## How to Use

Pass a JSON object via stdin with an `action` field:

```bash
echo '{"action": "status"}' | workspace-sync
```

## Slash Command (Deterministic)

This skill exposes a deterministic slash command: `/workspace_sync`.

- `/workspace_sync` -> runs `sync`
- `/workspace_sync status`
- `/workspace_sync diff`
- `/workspace_sync log 20`
- `/workspace_sync sync Manual sync from chat`
- `/workspace_sync commit Update workspace state`
- `/workspace_sync gh repo view owner/repo`

When called via slash command, execution bypasses the model and dispatches directly to the `workspace-sync` tool.

## Available Actions

### status
Show git status, branch, remote URL, and list tracked files from `.sync-manifest`.

```bash
echo '{"action": "status"}' | workspace-sync
```

Returns:
- `branch`: current git branch
- `remote`: clean remote URL (without embedded credentials)
- `auth_configured`: whether credentials are set up
- `tracked_patterns`: patterns from `.sync-manifest`
- `status`: list of changed/untracked files

### diff
Show pending changes (unstaged and staged).

```bash
echo '{"action": "diff"}' | workspace-sync
```

### log
Show recent commit history.

```bash
echo '{"action": "log"}' | workspace-sync
echo '{"action": "log", "count": 10}' | workspace-sync
```

### commit
Stage files matching `.sync-manifest` and commit with a message. Does NOT push.

```bash
echo '{"action": "commit", "message": "Update skills"}' | workspace-sync
```

### push
Push current branch to remote. Configures authentication automatically.

```bash
echo '{"action": "push"}' | workspace-sync
```

Returns `success: false` with error details if push fails.

### pull
Fetch from remote and merge (remote wins on conflicts).

```bash
echo '{"action": "pull"}' | workspace-sync
```

### sync
Full sync: commit manifest files, then push to remote.

```bash
echo '{"action": "sync", "message": "Auto-sync"}' | workspace-sync
```

Returns `success: false` if either commit or push fails. Does not silently succeed when push fails.

### gh
Run any `gh` CLI command. Pass the full command as a string.

```bash
echo '{"action": "gh", "command": "repo view owner/repo"}' | workspace-sync
echo '{"action": "gh", "command": "issue list --repo owner/repo"}' | workspace-sync
echo '{"action": "gh", "command": "pr create --title fix --body desc"}' | workspace-sync
```

## Notes

- Only files listed in `.sync-manifest` are staged/committed
- Merge conflicts use remote-wins strategy
- The workspace directory is `/root/.openclaw/workspace` inside the container
- `gh` commands run in the context of the workspace git repo
- Credentials are automatically configured from environment variables
- Remote URLs are kept clean (no embedded tokens) to prevent duplication issues

## Environment Variables

| Variable | Description |
|----------|-------------|
| `WORKSPACE_REPO_TOKEN` | GitHub PAT for authentication |
| `WORKSPACE_STATE_REPO` | Remote repository URL |
| `WORKSPACE_GIT_USER_NAME` | Git commit author name |
| `WORKSPACE_GIT_USER_EMAIL` | Git commit author email |
| `WORKSPACE_GIT_BRANCH` | Default branch name |
