#!/bin/sh
# workspace-sync.sh - Git sync for agent workspace
# Handles initial clone, merge with remote-wins conflicts, and periodic commits

set -e

WORKSPACE_DIR="${WORKSPACE_DIR:-/opt/data}"
REPO_URL="${WORKSPACE_STATE_REPO:-}"
REPO_TOKEN="${WORKSPACE_REPO_TOKEN:-}"
BRANCH="${WORKSPACE_GIT_BRANCH:-main}"
GIT_EMAIL="${WORKSPACE_GIT_USER_EMAIL:-agent@josemar.local}"
GIT_NAME="${WORKSPACE_GIT_USER_NAME:-Josemar Agent}"
SYNC_MODE="${WORKSPACE_SYNC_MODE:-}"
SYNC_ON_START="${WORKSPACE_SYNC_ON_START:-true}"
MANIFEST_PATH="${WORKSPACE_DIR}/.sync-manifest"
PROTECTED_RUNTIME_PATHS="config.yaml credentials .config obsidian sessions logs .env auth.json .gbrain/config.json .gbrain/brain.pglite .gbrain/last-update-check .gbrain/readiness.json .gbrain/audit .gbrain/migrations"

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

    register_user_skill_files
    validate_manifest_files || return 1

    git add -A -- .gitignore .sync-manifest 2>/dev/null || true

    while IFS= read -r pattern || [ -n "$pattern" ]; do
        case "$pattern" in
            \#*|"") continue ;;
            *) ;;
        esac

        git add -A -- "$pattern" 2>/dev/null || true
    done < "$MANIFEST_PATH"
}

ensure_skills_gitignore_allows_files() {
    if [ ! -f .gitignore ]; then
        return 0
    fi

    if grep -Fxq '!skills/**' .gitignore 2>/dev/null; then
        return 0
    fi

    cat >> .gitignore <<'EOF'

# Allow explicit user-owned skill files to be tracked via .sync-manifest.
!skills/**
EOF
}

manifest_contains_path() {
    grep -Fxq "$1" "$MANIFEST_PATH" 2>/dev/null
}

append_manifest_path() {
    if manifest_contains_path "$1"; then
        return 0
    fi

    printf '%s\n' "$1" >> "$MANIFEST_PATH"
    log_info "Registered user-owned skill path in .sync-manifest: $1"
}

register_user_skill_files() {
    local skills_dir skill_dir file relative_path
    skills_dir="${WORKSPACE_DIR}/skills"

    if [ ! -d "$skills_dir" ]; then
        return 0
    fi

    for skill_dir in "$skills_dir"/*; do
        if [ ! -d "$skill_dir" ]; then
            continue
        fi

        relative_skill_dir="skills/${skill_dir##*/}"

        if [ ! -f "$skill_dir/SKILL.md" ]; then
            continue
        fi

        ensure_skills_gitignore_allows_files

        while IFS= read -r file; do
            relative_path="${file#"$WORKSPACE_DIR"/}"
            append_manifest_path "$relative_path"
        done <<EOF
$(find "$skill_dir" -type f | sort)
EOF
    done
}

validate_manifest_files() {
    while IFS= read -r pattern || [ -n "$pattern" ]; do
        case "$pattern" in
            \#*|"") continue ;;
            *) ;;
        esac

        assert_manifest_path_safe "$pattern" || return 1
        assert_manifest_path_not_ignored "$pattern" || return 1
    done < "$MANIFEST_PATH"
}

validate_manifest_if_present() {
    if [ ! -f "$MANIFEST_PATH" ]; then
        return 0
    fi

    validate_manifest_files
}

assert_manifest_path_safe() {
    candidate="${1#./}"

    case "$candidate" in
        /*|../*|*/../*|*/..|..|:*)
            log_error ".sync-manifest contains unsafe pathspec: ${1}"
            return 1
            ;;
    esac

    for protected_path in $PROTECTED_RUNTIME_PATHS; do
        case "$candidate" in
            "$protected_path"|"$protected_path"/*)
                log_error ".sync-manifest includes protected runtime path: ${candidate}"
                return 1
                ;;
        esac
    done

    case "$candidate" in
        skills/*\**|skills/*\?*|skills/*\[*)
            log_error ".sync-manifest must use explicit skills paths: ${candidate}"
            return 1
            ;;
    esac
}

assert_manifest_path_not_ignored() {
    candidate="${1#./}"

    case "$candidate" in
        *\**|*\?*|*\[*)
            return 0
            ;;
    esac

    if git check-ignore -q -- "$candidate" 2>/dev/null; then
        log_error ".sync-manifest path is ignored by .gitignore: ${candidate}"
        return 1
    fi
}

assert_remote_tree_safe() {
    for protected_path in $PROTECTED_RUNTIME_PATHS; do
        if git ls-tree -r --name-only "origin/$BRANCH" -- "$protected_path" 2>/dev/null | grep -q .; then
            log_error "State repo tracks protected runtime path: ${protected_path}"
            return 1
        fi
    done
}

commit_changes() {
    local msg="$1"

    stage_manifest_files || return 1

    if git diff --cached --quiet; then
        log_info "No changes to commit"
        return 1
    fi

    git commit -m "$msg"
    return 0
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
    assert_remote_tree_safe

    git reset --hard "origin/$BRANCH"
    validate_manifest_if_present || return 1

    log_info "Workspace files restored from remote ($(git log --oneline -1))"

    log_info "Initial clone complete; skipping bootstrap auto-commit"
}

do_sync_start() {
    cd "$WORKSPACE_DIR"

    configure_git
    configure_remote
    validate_manifest_if_present || return 1

    log_info "Committing local changes before sync..."
    commit_changes "Auto-commit before sync: $(date -Iseconds 2>/dev/null || date)" || true

    log_info "Fetching from remote..."
    git fetch origin "$BRANCH" || {
        log_warn "Failed to fetch from remote, continuing with local state"
        return
    }

    assert_remote_tree_safe || return

    local has_remote
    has_remote=$(git ls-remote --heads origin "$BRANCH" 2>/dev/null | wc -l)

    if [ "$has_remote" -eq 0 ]; then
        log_info "Remote branch has no commits yet, pushing local state"
        git push -u origin "HEAD:$BRANCH" || log_warn "Failed to push to remote"
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

        stage_manifest_files
        git commit -m "Merge remote: conflict resolution (remote wins)" 2>/dev/null || true
    fi

    log_info "Pushing merged result..."
    git push origin "HEAD:$BRANCH" || log_warn "Failed to push to remote"
}

do_periodic_sync() {
    cd "$WORKSPACE_DIR"

    configure_git
    configure_remote
    validate_manifest_if_present || return 1

    committed=0

    log_info "Periodic sync: committing changes..."
    if commit_changes "Auto-sync: $(date -Iseconds 2>/dev/null || date)"; then
        committed=1
    fi

    log_info "Fetching from remote..."
    if git fetch origin "$BRANCH"; then
        assert_remote_tree_safe || return

        local_commit=$(git rev-parse HEAD 2>/dev/null || echo "none")
        remote_commit=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "none")

        if [ "$local_commit" != "$remote_commit" ]; then
            log_info "Merging remote changes (conflicts: remote wins)..."
            if git merge "origin/$BRANCH" -X theirs -m "Merge remote with conflict resolution"; then
                log_info "Merge completed successfully"
            else
                log_warn "Merge conflicts detected. Logging conflicted files:"
                git diff --name-only --diff-filter=U 2>/dev/null | while read -r f; do
                    log_warn "  Conflict resolved (remote won): $f"
                done

                stage_manifest_files
                git commit -m "Merge remote: conflict resolution (remote wins)" 2>/dev/null || true
            fi
        fi
    else
        log_warn "Failed to fetch from remote, pushing local state if needed"
    fi

    local_commit=$(git rev-parse HEAD 2>/dev/null || echo "none")
    remote_commit=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "none")

    if [ "$committed" -eq 1 ] || [ "$local_commit" != "$remote_commit" ]; then
        log_info "Pushing to remote..."
        git push origin "HEAD:$BRANCH" || log_warn "Failed to push to remote"
    fi
}

main() {
    if [ -z "$REPO_URL" ]; then
        log_info "WORKSPACE_STATE_REPO not configured, skipping git sync"
        return 0
    fi

    mkdir -p "$WORKSPACE_DIR"

    if [ ! -d "$WORKSPACE_DIR/.git" ]; then
        do_initial_clone
    elif [ "$SYNC_MODE" = "periodic" ]; then
        do_periodic_sync
    elif [ "$SYNC_ON_START" = "true" ]; then
        do_sync_start
    else
        cd "$WORKSPACE_DIR"
        configure_git
        configure_remote
        log_info "Sync on start disabled, configuring git only"
    fi

    return 0
}

main "$@"
