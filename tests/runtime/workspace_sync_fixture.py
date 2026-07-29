"""Shared test-only fixtures for workspace sync contract tests.

Consolidates the git workspace + bare-remote fixture patterns that were
duplicated across ``test_workspace_sync_runtime.py``,
``test_workspace_sync_skill_registration.py``, and the unified contract
tests. Provides explicit state builders for the five canonical workspace
states used by the contract tests:

- **dirty worktree** — uncommitted changes on a manifest-tracked file
- **clean local-ahead** — a local commit not yet on the remote
- **remote-ahead** — remote has a commit the local workspace lacks
- **true divergence** — both local and remote have independent commits
  on the same file with different content
- **unchanged** — local and remote point at the same commit

All builders use local bare remotes and temp directories; no network.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


TEST_GIT_EMAIL = "test" + "@example.invalid"
REMOTE_GIT_EMAIL = "remote" + "@example.invalid"

# Approved protected runtime entries — the canonical manifest policy.
# Reject bare/broad ``.gbrain`` and protected runtime entries, but allow
# explicit ``.gbrain/schema-packs/josemar/pack.yaml``.
PROTECTED_RUNTIME_ENTRIES: tuple[str, ...] = (
    "config.yaml",
    "credentials",
    ".config",
    "obsidian",
    "sessions",
    "logs",
    ".env",
    "auth.json",
    ".locks",
    ".gbrain/config.json",
    ".gbrain/brain.pglite",
    ".gbrain/last-update-check",
    ".gbrain/readiness.json",
    ".gbrain/audit",
    ".gbrain/migrations",
)
# Bare/broad .gbrain forms must be rejected; only explicit narrow subpaths allowed.
REJECTED_BARE_GBRAIN: tuple[str, ...] = (".gbrain", ".gbrain/", ".gbrain/*", ".gbrain/**")
# Explicit narrow schema pack path must be allowed.
ALLOWED_EXPLICIT_GBRAIN = ".gbrain/schema-packs/josemar/pack.yaml"

# Approved tool-mode actions.
TOOL_ACTIONS: tuple[str, ...] = (
    "status",
    "diff",
    "log",
    "commit",
    "push",
    "pull",
    "sync",
    "gh",
)

# Default deny-by-default .gitignore used by all fixture workspaces.
_DEFAULT_GITIGNORE = "\n".join(
    [
        "*",
        "!.gitignore",
        "!.sync-manifest",
        "!skills/",
        "!skills/.gitkeep",
        "",
    ]
)


class WorkspaceRepo:
    """A temp git workspace + bare remote pair with state builders.

    Encapsulates the low-level git subprocess calls so test classes can
    express state transitions declaratively. All paths are absolute.
    """

    def __init__(self) -> None:
        self._temp_dirs: list[tempfile.TemporaryDirectory] = []
        self.workspace = self._mk_dir()
        self.remote = self._mk_dir() + ".git"
        self._init_bare_remote_and_workspace()

    # -- lifecycle --

    def cleanup(self) -> None:
        for td in self._temp_dirs:
            td.cleanup()

    # -- temp dirs --

    def _mk_dir(self) -> str:
        td = tempfile.TemporaryDirectory(prefix="ws-unify-")
        self._temp_dirs.append(td)
        return td.name

    def mk_temp_dir(self) -> str:
        """Public temp dir factory for tests that need extra scratch space."""
        return self._mk_dir()

    # -- bootstrap --

    def _init_bare_remote_and_workspace(self) -> None:
        _run(["git", "init", "-q", "--bare", self.remote])
        _run(["git", "init", "-q", self.workspace])
        self.git(["config", "user.email", TEST_GIT_EMAIL])
        self.git(["config", "user.name", "Test User"])
        self.git(["checkout", "-q", "-B", "main"])
        ws = Path(self.workspace)
        (ws / "skills").mkdir(exist_ok=True)
        (ws / "skills" / ".gitkeep").touch()
        (ws / ".gitignore").write_text(_DEFAULT_GITIGNORE, encoding="utf-8")
        (ws / ".sync-manifest").write_text("skills/.gitkeep\n", encoding="utf-8")
        self.git(["add", ".gitignore", ".sync-manifest", "skills/.gitkeep"])
        self.git(["commit", "-qm", "initial state"])
        self.git(["remote", "add", "origin", self.remote])
        self.git(["push", "-q", "-u", "origin", "main"])
        # Point the bare remote's HEAD at refs/heads/main so
        # `git show HEAD:<path>` resolves (git init --bare defaults to master).
        _run(["git", "-C", self.remote, "symbolic-ref", "HEAD", "refs/heads/main"])

    # -- git helpers (workspace-local) --

    def git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return _run(["git", "-C", self.workspace, *args])

    def git_check(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", self.workspace, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def log_oneline(self, *, limit: int) -> str:
        proc = _run(["git", "-C", self.workspace, "log", "--oneline", f"-n{limit}"])
        return proc.stdout

    def tracked_files(self) -> set[str]:
        proc = _run(["git", "-C", self.workspace, "ls-files"])
        return set(proc.stdout.splitlines())

    def current_branch(self) -> str:
        proc = _run(["git", "-C", self.workspace, "branch", "--show-current"])
        return proc.stdout.strip()

    def remote_url(self) -> str:
        proc = _run(["git", "-C", self.workspace, "remote", "get-url", "origin"])
        return proc.stdout.strip()

    def rev_parse(self, ref: str) -> str:
        proc = _run(["git", "-C", self.workspace, "rev-parse", ref])
        return proc.stdout.strip()

    # -- state queries on the bare remote --

    def remote_show_file(self, relative_path: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", self.remote, "show", f"HEAD:{relative_path}"],
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_remote_tracks_file(self, relative_path: str, expected_content: str) -> None:
        proc = self.remote_show_file(relative_path)
        assert proc.returncode == 0, f"remote missing {relative_path}: {proc.stderr}"
        assert proc.stdout == expected_content, (
            f"remote {relative_path}: expected {expected_content!r}, got {proc.stdout!r}"
        )

    # -- manifest-tracked file helper --

    def add_tracked_file(self, relative_path: str, content: str) -> None:
        """Create a manifest-tracked file and append it to .sync-manifest.

        Updates .gitignore allow rules so the file is not ignored by the
        deny-by-default policy. The file is left unstaged; commit/sync
        actions handle staging via the manifest.
        """
        target = Path(self.workspace) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        gitignore = Path(self.workspace) / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8")
        parts = relative_path.split("/")
        allow_rules: list[str] = []
        for i in range(1, len(parts)):
            allow_rules.append(f"!{'/'.join(parts[:i])}/")
        allow_rules.append(f"!{relative_path}")
        new_lines = existing.rstrip("\n").splitlines()
        for rule in allow_rules:
            if rule not in new_lines:
                new_lines.append(rule)
        gitignore.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        with (Path(self.workspace) / ".sync-manifest").open("a", encoding="utf-8") as fh:
            fh.write(f"{relative_path}\n")

    def commit_all(self, message: str) -> None:
        """Stage all manifest files and commit (bypasses the tool)."""
        self.git(["add", ".gitignore", ".sync-manifest"])
        # Stage all manifest-tracked files.
        manifest = (Path(self.workspace) / ".sync-manifest").read_text(encoding="utf-8")
        for line in manifest.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            self.git_check(["add", "--", line])
        self.git(["commit", "-qm", message])

    def push_to_remote(self) -> None:
        _run(["git", "-C", self.workspace, "push", "-q", "origin", "main"])

    def allow_schema_pack_in_gitignore(self) -> None:
        """Add .gitignore allow rules for the explicit schema pack path."""
        gitignore = Path(self.workspace) / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8")
        allow = (
            "\n!.gbrain/\n!.gbrain/schema-packs/\n"
            "!.gbrain/schema-packs/josemar/\n"
            f"!{ALLOWED_EXPLICIT_GBRAIN}\n"
        )
        gitignore.write_text(existing + allow, encoding="utf-8")

    def write_schema_pack_file(self, content: str = "api_version: \"gbrain-schema-pack-v1\"\n") -> None:
        """Create the explicit schema pack file on disk."""
        target = Path(self.workspace) / ALLOWED_EXPLICIT_GBRAIN
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def set_manifest(self, paths: str) -> None:
        """Overwrite .sync-manifest with the given content."""
        (Path(self.workspace) / ".sync-manifest").write_text(paths, encoding="utf-8")

    # -- state builders --

    def build_dirty_worktree(self, relative_path: str, content: str) -> None:
        """Leave a manifest-tracked file modified but unstaged."""
        self.add_tracked_file(relative_path, content)

    def build_clean_local_ahead(self, relative_path: str, content: str) -> None:
        """Create a local commit on a manifest-tracked file, not yet pushed."""
        self.add_tracked_file(relative_path, content)
        self.commit_all("local-ahead commit")
        # Remote is still at the initial commit.

    def build_remote_ahead(self, relative_path: str, content: str) -> None:
        """Advance the remote with an independent commit the local lacks."""
        self.push_to_remote()  # Ensure remote has the initial state.
        self._advance_remote_with_file(relative_path, content)

    def build_true_divergence(
        self,
        relative_path: str,
        local_content: str,
        remote_content: str,
    ) -> None:
        """Produce real divergent heads from a common pushed base.

        Steps:
        1. Push the initial state to the remote (common base).
        2. Create an independent local commit on ``relative_path`` (unpushed).
        3. Clone the remote base into a separate repo, create an
           independent commit on the same path, and push it to the
           remote.

        After this, neither HEAD nor ``origin/main`` is an ancestor of
        the other. Fixture invariants are checked with
        ``git merge-base --is-ancestor`` in both directions; setup fails
        if either head is an ancestor of the other.
        """
        # 1. Push the common base.
        self.push_to_remote()
        base_sha = self.rev_parse("HEAD")

        # 2. Independent local commit (unpushed).
        self.add_tracked_file(relative_path, local_content)
        self.commit_all("local divergent commit")
        local_sha = self.rev_parse("HEAD")

        # 3. Independent remote commit from a clone of the base.
        #    We must clone the remote *before* the local commit was
        #    pushed, so the clone starts from the common base. Since we
        #    haven't pushed the local commit, the remote is still at the
        #    base, so a fresh clone gives us the base.
        self._advance_remote_with_file(relative_path, remote_content)
        remote_sha = subprocess.run(
            ["git", "-C", self.remote, "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # 4. Fetch so origin/main reflects the remote commit.
        subprocess.run(
            ["git", "-C", self.workspace, "fetch", "-q", "origin"],
            check=True,
            capture_output=True,
            text=True,
        )

        # 5. Fixture invariants: neither head is an ancestor of the other.
        #    git merge-base --is-ancestor A B exits 0 if A is ancestor of B.
        local_is_ancestor = subprocess.run(
            ["git", "-C", self.workspace, "merge-base", "--is-ancestor",
             local_sha, f"origin/main"],
            capture_output=True,
            text=True,
            check=False,
        ).returncode == 0
        remote_is_ancestor = subprocess.run(
            ["git", "-C", self.workspace, "merge-base", "--is-ancestor",
             f"origin/main", local_sha],
            capture_output=True,
            text=True,
            check=False,
        ).returncode == 0
        if local_is_ancestor or remote_is_ancestor:
            raise AssertionError(
                f"build_true_divergence did not produce divergent heads: "
                f"base={base_sha[:8]} local={local_sha[:8]} "
                f"remote={remote_sha[:8]} "
                f"local_is_ancestor_of_remote={local_is_ancestor} "
                f"remote_is_ancestor_of_local={remote_is_ancestor}"
            )

    def build_unchanged(self) -> None:
        """Ensure local and remote point at the same commit."""
        self.push_to_remote()

    # -- remote advance helper --

    def _advance_remote_with_file(self, relative_path: str, content: str) -> None:
        clone_dir = self._mk_dir()
        clone_path = clone_dir
        _run(["git", "clone", "-q", self.remote, clone_path])
        _run(["git", "-C", clone_path, "config", "user.email", REMOTE_GIT_EMAIL])
        _run(["git", "-C", clone_path, "config", "user.name", "Remote Author"])
        target = Path(clone_path) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _run(["git", "-C", clone_path, "add", "-f", relative_path])
        _run(["git", "-C", clone_path, "commit", "-qm", "remote advance"])
        _run(["git", "-C", clone_path, "push", "-q", "origin", "HEAD:main"])

    # -- bare remote with arbitrary state (for initial-clone tests) --

    @classmethod
    def build_bare_remote_with_state(
        cls,
        temp_dirs: list[tempfile.TemporaryDirectory],
        *,
        initial_commit_subject: str,
        extra_tracked: dict[str, str] | None = None,
    ) -> str:
        """Build a local bare remote on ``main`` with committed state files.

        Authors the state in a temp source repo, pushes to a freshly
        created bare remote, and returns the bare remote path. The
        ``temp_dirs`` list is appended to so the caller owns cleanup.
        """
        source = _mk_temp_dir(temp_dirs)
        _run(["git", "init", "-q", source])
        _run(["git", "-C", source, "config", "user.email", TEST_GIT_EMAIL])
        _run(["git", "-C", source, "config", "user.name", "Test User"])
        _run(["git", "-C", source, "checkout", "-q", "-B", "main"])
        src = Path(source)
        (src / "skills").mkdir(exist_ok=True)
        (src / "skills" / ".gitkeep").touch()
        gitignore_lines = [
            "*",
            "!.gitignore",
            "!.sync-manifest",
            "!skills/",
            "!skills/.gitkeep",
        ]
        manifest_lines = ["skills/.gitkeep"]
        if extra_tracked:
            for relative_path, content in extra_tracked.items():
                target = src / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                parts = relative_path.split("/")
                for i in range(1, len(parts)):
                    gitignore_lines.append(f"!{'/'.join(parts[:i])}/")
                gitignore_lines.append(f"!{relative_path}")
                manifest_lines.append(relative_path)
        (src / ".gitignore").write_text("\n".join(gitignore_lines) + "\n", encoding="utf-8")
        (src / ".sync-manifest").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
        add_args = [".gitignore", ".sync-manifest", "skills/.gitkeep"]
        if extra_tracked:
            add_args.extend(extra_tracked.keys())
        _run(["git", "-C", source, "add", *add_args])
        _run(["git", "-C", source, "commit", "-qm", initial_commit_subject])
        bare = _mk_temp_dir(temp_dirs) + ".git"
        _run(["git", "init", "-q", "--bare", bare])
        _run(["git", "-C", source, "remote", "add", "origin", bare])
        _run(["git", "-C", source, "push", "-q", "-u", "origin", "main"])
        _run(["git", "-C", bare, "symbolic-ref", "HEAD", "refs/heads/main"])
        return bare


# ---------------------------------------------------------------------------
# Git environment isolation mixin
# ---------------------------------------------------------------------------


class GitEnvIsolation:
    """Mixin that isolates GIT_* env vars for the duration of a test.

    Call ``_isolate_git_environment()`` in setUp and the cleanup is
    registered automatically via ``unittest.TestCase.addCleanup``.
    """

    def _isolate_git_environment(self) -> None:
        self._saved_git_env = {
            key: os.environ.pop(key) for key in list(os.environ) if key.startswith("GIT_")
        }
        # addCleanup is provided by unittest.TestCase; the mixin must be
        # used as a base alongside TestCase.
        self.addCleanup(self._restore_git_environment)  # type: ignore[attr-defined]

    def _restore_git_environment(self) -> None:
        for key in list(os.environ):
            if key.startswith("GIT_"):
                os.environ.pop(key)
        os.environ.update(self._saved_git_env)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=True)


def _mk_temp_dir(temp_dirs: list[tempfile.TemporaryDirectory]) -> str:
    td = tempfile.TemporaryDirectory(prefix="ws-unify-")
    temp_dirs.append(td)
    return td.name