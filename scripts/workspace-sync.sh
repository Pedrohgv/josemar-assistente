#!/bin/sh
# workspace-sync.sh - Git sync for agent workspace
# Handles initial clone, merge with remote-wins conflicts, and periodic commits

set -e

WORKSPACE_DIR="${WORKSPACE_DIR:-/root/.openclaw/workspace}"
REPO_URL="${WORKSPACE_STATE_REPO:-}"
REPO_TOKEN="${WORKSPACE_REPO_TOKEN:-}"
BRANCH="${WORKSPACE_GIT_BRANCH:-main}"
GIT_EMAIL="${WORKSPACE_GIT_USER_EMAIL:-agent@josemar.local}"
GIT_NAME="${WORKSPACE_GIT_USER_NAME:-Josemar Agent}"
SYNC_ON_START="${WORKSPACE_SYNC_ON_START:-true}"
SYNC_INTERVAL="${WORKSPACE_SYNC_INTERVAL:-60}"
MEMORY_DAYS="${WORKSPACE_MEMORY_DAYS:-30}"
MANIFEST_PATH="${WORKSPACE_DIR}/.sync-manifest"

log_info() {
    echo "[workspace-sync] $1"
}

log_warn() {
    echo "[workspace-sync] WARNING: $1" >&2
}

log_error() {
    echo "[workspace-sync] ERROR: $1" >&2
}

configure_git() {
    git config user.email "$GIT_EMAIL"
    git config user.name "$GIT_NAME"
}

configure_remote() {
    if [ -n "$REPO_TOKEN" ]; then
        token_url=$(echo "$REPO_URL" | sed "s|https://|https://${REPO_TOKEN}@|")
        git remote set-url origin "$token_url"
    fi
}

stage_manifest_files() {
    if [ ! -f "$MANIFEST_PATH" ]; then
        log_warn "No .sync-manifest found, skipping selective staging"
        return 0
    fi

    while IFS= read -r pattern; do
        case "$pattern" in
            \#*|"") continue ;;
            *) ;;
        esac

        expanded=$(echo "$pattern" | sed "s|skills|${WORKSPACE_DIR}/skills|; s|memory|${WORKSPACE_DIR}/memory|; s|avatars|${WORKSPACE_DIR}/avatars|")

        for f in $expanded; do
            if [ -e "$f" ]; then
                git add "$f" 2>/dev/null || true
            fi
        done
    done < "$MANIFEST_PATH"
}

commit_changes() {
    local msg="$1"

    stage_manifest_files

    if git diff --cached --quiet; then
        log_info "No changes to commit"
        return 1
    fi

    git commit -m "$msg"
    return 0
}

rotate_memory_logs() {
    if [ "$MEMORY_DAYS" -le 0 ] 2>/dev/null; then
        return
    fi

    memory_dir="${WORKSPACE_DIR}/memory"
    if [ ! -d "$memory_dir" ]; then
        return
    fi

    count=0
    for f in "$memory_dir"/*.md; do
        [ -f "$f" ] || continue

        if find "$f" -mtime +"$MEMORY_DAYS" 2>/dev/null | grep -q .; then
            log_info "Rotating old memory log: $(basename "$f")"
            git rm "$f" 2>/dev/null || rm -f "$f"
            count=$((count + 1))
        fi
    done

    if [ "$count" -gt 0 ]; then
        git commit -m "Rotate memory logs: removed $count files older than ${MEMORY_DAYS} days" 2>/dev/null || true
    fi
}

do_initial_clone() {
    log_info "No git repo found. Cloning from remote..."

    local tmp_clone
    tmp_clone=$(mktemp -d)

    local clone_url="$REPO_URL"
    if [ -n "$REPO_TOKEN" ]; then
        clone_url=$(echo "$REPO_URL" | sed "s|https://|https://${REPO_TOKEN}@|")
    fi

    git clone --branch "$BRANCH" --single-branch "$clone_url" "$tmp_clone"

    if [ -d "$tmp_clone/.git" ]; then
        cp -r "$tmp_clone/.git" "$WORKSPACE_DIR/.git"
        log_info "Git repo initialized from remote"
    fi

    rm -rf "$tmp_clone"

    cd "$WORKSPACE_DIR"
    configure_git
    configure_remote

    git reset --hard "origin/$BRANCH"

    log_info "Workspace files restored from remote ($(git log --oneline -1))"

    commit_changes "Initial commit from container start" || true
}

do_sync_start() {
    cd "$WORKSPACE_DIR"

    configure_git
    configure_remote

    log_info "Committing local changes before sync..."
    commit_changes "Auto-commit before sync: $(date -Iseconds 2>/dev/null || date)" || true

    log_info "Fetching from remote..."
    git fetch origin "$BRANCH" || {
        log_warn "Failed to fetch from remote, continuing with local state"
        return
    }

    local has_remote
    has_remote=$(git ls-remote --heads origin "$BRANCH" 2>/dev/null | wc -l)

    if [ "$has_remote" -eq 0 ]; then
        log_info "Remote branch has no commits yet, pushing local state"
        git push -u origin "$BRANCH" || log_warn "Failed to push to remote"
        return
    fi

    local local_commit
    local_commit=$(git rev-parse HEAD 2>/dev/null || echo "none")
    local remote_commit
    remote_commit=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "none")

    if [ "$local_commit" = "$remote_commit" ]; then
        log_info "Local and remote are in sync"
        return
    fi

    log_info "Merging remote changes (conflicts: remote wins)..."
    if git merge "origin/$BRANCH" -X theirs -m "Merge remote with conflict resolution"; then
        log_info "Merge completed successfully"
    else
        log_warn "Merge conflicts detected. Logging conflicted files:"
        git diff --name-only --diff-filter=U 2>/dev/null | while read -r f; do
            log_warn "  Conflict resolved (remote won): $f"
        done

        git add -A
        git commit -m "Merge remote: conflict resolution (remote wins)" 2>/dev/null || true
    fi

    log_info "Pushing merged result..."
    git push origin "HEAD:$BRANCH" || log_warn "Failed to push to remote"
}

do_periodic_sync() {
    cd "$WORKSPACE_DIR"

    configure_git
    configure_remote

    log_info "Periodic sync: committing changes..."
    if commit_changes "Auto-sync: $(date -Iseconds 2>/dev/null || date)"; then
        rotate_memory_logs
        log_info "Pushing to remote..."
        git push origin "HEAD:$BRANCH" || log_warn "Failed to push to remote"
    fi
}

start_sync_daemon() {
    if [ -z "$SYNC_INTERVAL" ] || [ "$SYNC_INTERVAL" = "0" ]; then
        log_info "Periodic sync disabled (WORKSPACE_SYNC_INTERVAL=0)"
        return
    fi

    log_info "Starting periodic sync daemon (every ${SYNC_INTERVAL} minutes)"

    while true; do
        sleep "${SYNC_INTERVAL}m"
        do_periodic_sync
    done &
}

main() {
    if [ -z "$REPO_URL" ]; then
        log_info "WORKSPACE_STATE_REPO not configured, skipping git sync"
        return 0
    fi

    mkdir -p "$WORKSPACE_DIR"

    if [ ! -d "$WORKSPACE_DIR/.git" ]; then
        do_initial_clone
    elif [ "$SYNC_ON_START" = "true" ]; then
        do_sync_start
    else
        cd "$WORKSPACE_DIR"
        configure_git
        configure_remote
        log_info "Sync on start disabled, configuring git only"
    fi

    rotate_memory_logs
    start_sync_daemon
}

main "$@"
