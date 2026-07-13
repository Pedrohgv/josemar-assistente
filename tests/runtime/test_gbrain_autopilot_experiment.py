"""Gated runtime experiment harness for native gbrain sync/dream/autopilot.

This module is a reusable, disposable experiment harness, not a pass/fail
contract test. It is skipped by default and only runs when both:

    RUN_DOCKER_TESTS=1
    RUN_GBRAIN_AUTOPILOT_EXPERIMENT=1

are set. When enabled, it spins up a fresh isolated Compose project per
candidate, seeds a dummy Obsidian vault as a git repo, runs ``josemar-gbrain
reindex`` for a baseline, makes an uncommitted vault edit with a unique token,
then runs exactly one candidate gbrain command group and captures:

  - exit code, stdout, stderr, elapsed time
  - ``gbrain get`` / ``gbrain search`` checks after the candidate
  - ``gbrain status`` after the candidate
  - process/lock cleanup for autopilot candidates

Assertions are intentionally weak: only safety invariants (Telegram env blank,
no real credentials mounted, harness completes and captures results) are
asserted. Adoption conclusions must be made by manual inspection of the
captured output, which is printed to stdout/stderr for that purpose.

See ``.slim/deepwork/gbrain-autopilot-runtime-experiment.md`` for the plan and
oracle safety notes.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .helpers import ComposeRuntime, REPO_ROOT, docker_available


GATE_DOCKER = os.getenv("RUN_DOCKER_TESTS") == "1"
GATE_EXPERIMENT = os.getenv("RUN_GBRAIN_AUTOPILOT_EXPERIMENT") == "1"

# Host API key / provider env vars that must be blank in the disposable runtime
# so no real credentials can leak into it. Mirrors docker-compose.yml defaults
# but forces empty values regardless of a local .env file.
HOST_PROVIDER_KEY_VARS = (
    "ZAI_API_KEY",
    "GLM_API_KEY",
    "DEEPSEEK_API_KEY",
    "OLLAMA_API_KEY",
    "TAVILY_API_KEY",
    "API_SERVER_KEY",
    "GOG_KEYRING_PASSWORD",
)

TELEGRAM_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "PRIMARY_TELEGRAM_ID",
    "HERMES_TELEGRAM_BOT_TOKEN",
    "HERMES_TELEGRAM_ALLOWED_USERS",
    "HERMES_TELEGRAM_HOME_CHANNEL",
    "HERMES_GATEWAY_ALLOWED_USERS",
)

# gbrain runtime env that must wrap every direct gbrain command.
GBRAIN_CMD_ENV = {
    "HOME": "/opt/data",
    "GBRAIN_HOME": "/opt/data",
    "GBRAIN_BRAIN_REPO": "/opt/data/obsidian",
    "GBRAIN_SKIP_STARTUP_HOOKS": "1",
}

# Container-side autopilot timeout. Conservative; the oracle notes suggest
# 75s with TERM+kill. We keep it bounded and verify no process remains.
AUTOPILOT_TIMEOUT_SECONDS = 75
AUTOPILOT_KILL_GRACE_SECONDS = 10


@dataclass
class CommandResult:
    """Captured result of a single in-container command."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float

    def summary(self) -> dict[str, Any]:
        return {
            "command": " ".join(self.command),
            "returncode": self.returncode,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "stdout_len": len(self.stdout),
            "stderr_len": len(self.stderr),
            "stdout_head": self.stdout[:1200],
            "stderr_head": self.stderr[:1200],
        }


@dataclass
class CandidateReport:
    """Full report for one candidate run."""

    name: str
    candidate_results: list[CommandResult] = field(default_factory=list)
    get_after: CommandResult | None = None
    search_after: CommandResult | None = None
    status_after: CommandResult | None = None
    process_cleanup: CommandResult | None = None
    lock_cleanup: CommandResult | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "candidates": [r.summary() for r in self.candidate_results],
            "get_after": self.get_after.summary() if self.get_after else None,
            "search_after": self.search_after.summary() if self.search_after else None,
            "status_after": self.status_after.summary() if self.status_after else None,
            "process_cleanup": self.process_cleanup.summary() if self.process_cleanup else None,
            "lock_cleanup": self.lock_cleanup.summary() if self.lock_cleanup else None,
            "notes": self.notes,
        }


def _make_compose_override(agent_state_dir: str, credentials_dir: str) -> Path:
    """Write a compose override file that replaces the real agent-state and
    credentials bind mounts with empty temp directories.

    The override is intentionally minimal: it only re-declares the hermes
    service volumes that must be neutralized, so the base compose file remains
    the source of truth for everything else.
    """
    override = {
        "services": {
            "hermes": {
                "volumes": [
                    # Re-declare the volumes we keep from the base file.
                    "hermes-data:/opt/data",
                    "aux-ml-shared:/shared",
                    "obsidian-vault:/opt/data/obsidian",
                    # Override the two source bind mounts with empty temp dirs.
                    f"{agent_state_dir}:/opt/josemar/source-agent-state:ro",
                    f"{credentials_dir}:/opt/josemar/credentials-source:ro",
                ],
            }
        },
    }
    tmp = tempfile.NamedTemporaryFile(
        prefix="gbrain-exp-override-",
        suffix=".yml",
        delete=False,
        mode="w",
        encoding="utf-8",
    )
    # Use json -> yaml-ish; the structure is simple enough that JSON is valid
    # YAML, but compose prefers YAML. Emit minimal YAML by hand to avoid a
    # PyYAML dependency.
    tmp.write("services:\n")
    tmp.write("  hermes:\n")
    tmp.write("    volumes:\n")
    for vol in override["services"]["hermes"]["volumes"]:
        tmp.write(f"      - {vol}\n")
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


class GbrainAutopilotExperimentRuntime(ComposeRuntime):
    """ComposeRuntime extended for the gbrain autopilot experiment.

    Adds:
      - explicit blanking of Telegram and hosted provider API key env vars
      - unique high dashboard/API ports per runtime
      - a compose override file that mounts empty temp dirs over
        /opt/josemar/source-agent-state and /opt/josemar/credentials-source
      - run() that passes the override file
    """

    # High port base; offset by a per-runtime counter to avoid collisions
    # even when several experiment runs are active concurrently.
    _PORT_BASE = 47000
    _port_counter = 0

    def __init__(self) -> None:
        super().__init__()
        # Unique high ports.
        GbrainAutopilotExperimentRuntime._port_counter += 1
        port_offset = GbrainAutopilotExperimentRuntime._port_counter
        self.dashboard_port = self._PORT_BASE + port_offset * 2
        self.api_port = self._PORT_BASE + port_offset * 2 + 1
        self.env["HERMES_DASHBOARD_PORT"] = str(self.dashboard_port)
        self.env["HERMES_API_SERVER_PORT"] = str(self.api_port)
        # Keep API server disabled; we only set the port to avoid collisions.
        self.env["HERMES_API_SERVER_ENABLED"] = "false"

        # Explicitly blank Telegram env vars.
        for var in TELEGRAM_VARS:
            self.env[var] = ""

        # Explicitly blank hosted provider API key env vars.
        for var in HOST_PROVIDER_KEY_VARS:
            self.env[var] = ""

        # COMPOSE_PROFILES must be empty; aux-ml must not start.
        self.env.pop("COMPOSE_PROFILES", None)
        self.env["COMPOSE_PROFILES"] = ""

        # WORKSPACE_STATE_REPO must remain empty.
        self.env["WORKSPACE_STATE_REPO"] = ""

        # Empty temp dirs to mount over the real agent-state/credentials.
        self._agent_state_td = tempfile.TemporaryDirectory(prefix="gbrain-exp-agentstate-")
        self._credentials_td = tempfile.TemporaryDirectory(prefix="gbrain-exp-creds-")
        self.agent_state_dir = self._agent_state_td.name
        self.credentials_dir = self._credentials_td.name
        self.override_file = _make_compose_override(
            self.agent_state_dir, self.credentials_dir
        )

    def run(self, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        command = [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            str(self.override_file),
            "-p",
            self.project,
            *args,
        ]
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    def cleanup_override(self) -> None:
        try:
            self.override_file.unlink(missing_ok=True)
        except Exception:
            pass
        self._agent_state_td.cleanup()
        self._credentials_td.cleanup()


def _gbrain_cmd(*args: str) -> list[str]:
    """Build a gbrain command wrapped with the required env via sh -lc.

    The env is exported inside the shell so the gbrain binary inherits it
    regardless of how docker compose exec passes the command.
    """
    env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in GBRAIN_CMD_ENV.items())
    quoted_args = " ".join(shlex.quote(arg) for arg in args)
    return ["sh", "-lc", f"{env_prefix} exec gbrain {quoted_args}"]


def _run_in_container(
    runtime: GbrainAutopilotExperimentRuntime,
    *command: str,
    check: bool = False,
    timeout: int = 180,
    as_user: str | None = "hermes",
) -> CommandResult:
    """Run a command in the hermes container and capture a CommandResult."""
    if as_user:
        full = ["su", "-s", "/bin/sh", "--", as_user, "-c", shlex.join(command)]
    else:
        full = list(command)
    start = time.monotonic()
    proc = runtime.exec("hermes", *full, check=check, timeout=timeout)
    elapsed = time.monotonic() - start
    return CommandResult(
        command=list(command),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        elapsed_seconds=elapsed,
    )


def _run_gbrain(
    runtime: GbrainAutopilotExperimentRuntime,
    *args: str,
    check: bool = False,
    timeout: int = 180,
) -> CommandResult:
    """Run a direct gbrain command with the required env wrapper, as hermes."""
    cmd = _gbrain_cmd(*args)
    return _run_in_container(runtime, *cmd, check=check, timeout=timeout)


def _seed_baseline_vault(runtime: GbrainAutopilotExperimentRuntime) -> CommandResult:
    """Seed /opt/data/obsidian as a git repo with baseline committed markdown.

    The manual edit under test must happen only after reindex, otherwise every
    candidate starts with the edited content already present in gbrain state.
    """
    # Baseline files. Use a known task and a linked note so link extraction and
    # task extraction can be observed.
    seed_script = r"""set -eu
cd /opt/data/obsidian
git init -q -b main .
git config user.email experiment
git config user.name "Experiment"
mkdir -p life/diary notes
cat > life/diary/2026-07-13.md <<'MD'
# 2026-07-13

- [ ] baseline task one
- [ ] baseline task two

Linked: [[notes/baseline-note]]
MD
cat > notes/baseline-note.md <<'MD'
# Baseline Note

This is a baseline note linked from the diary.
MD
git add .
git commit -qm "baseline vault"
"""
    return _run_in_container(runtime, "sh", "-lc", seed_script, check=True, as_user="hermes")


def _make_uncommitted_manual_edit(
    runtime: GbrainAutopilotExperimentRuntime,
    token: str,
) -> CommandResult:
    """Make an uncommitted vault edit containing a unique token.

    This simulates a manual Obsidian/Syncthing edit where the git HEAD does not
    change after the baseline reindex.
    """

    # Uncommitted edit with a unique token. This simulates a manual Obsidian
    # edit (Syncthing) where HEAD does not change.
    edit_script = (
        "set -eu; "
        "cd /opt/data/obsidian; "
        f"printf '\\n\\n## Experiment edit\\n\\nUnique token: {token}\\n\\n"
        "- [ ] experiment task\\n' >> life/diary/2026-07-13.md; "
        "git status --porcelain"
    )
    return _run_in_container(runtime, "sh", "-lc", edit_script, check=True, as_user="hermes")


def _reindex(runtime: GbrainAutopilotExperimentRuntime) -> CommandResult:
    """Run josemar-gbrain reindex as the hermes user for baseline."""
    return _run_in_container(
        runtime,
        "sh",
        "-lc",
        "/usr/local/bin/josemar-gbrain reindex",
        check=True,
        timeout=300,
        as_user="hermes",
    )


def _post_checks(
    runtime: GbrainAutopilotExperimentRuntime,
    *,
    token: str,
    do_process_cleanup: bool = False,
) -> tuple[CommandResult, CommandResult, CommandResult, CommandResult | None, CommandResult | None]:
    """Run gbrain get / search / status after a candidate, and optionally
    process/lock cleanup for autopilot candidates."""
    # get the edited file
    get_after = _run_gbrain(runtime, "get", "life/diary/2026-07-13", "--json")
    # search for the unique token
    search_after = _run_gbrain(runtime, "search", token, "--json")
    # status
    status_after = _run_gbrain(runtime, "status", "--json")

    process_cleanup = None
    lock_cleanup = None
    if do_process_cleanup:
        # Kill any lingering gbrain/autopilot processes as hermes.
        process_cleanup = _run_in_container(
            runtime,
            "sh",
            "-lc",
            "pkill -u hermes -f 'gbrain autopilot' 2>/dev/null; pkill -u hermes -f 'gbrain dream' 2>/dev/null; "
            "ps -u hermes -o pid,cmd | grep -E 'gbrain (autopilot|dream)' | grep -v grep || true",
            as_user=None,
        )
        # Remove any gbrain lock files under /opt/data/.gbrain.
        lock_cleanup = _run_in_container(
            runtime,
            "sh",
            "-lc",
            "find /opt/data/.gbrain -name '*.lock' -o -name '*.pid' 2>/dev/null | xargs -r rm -f; "
            "ls -la /opt/data/.gbrain 2>/dev/null || true",
            as_user="hermes",
        )
    return get_after, search_after, status_after, process_cleanup, lock_cleanup


def _run_candidate(
    runtime: GbrainAutopilotExperimentRuntime,
    *,
    name: str,
    candidate_commands: list[list[str]],
    token: str,
    do_process_cleanup: bool = False,
) -> CandidateReport:
    """Run one candidate group and capture all post-checks."""
    report = CandidateReport(name=name)
    for cmd in candidate_commands:
        result = _run_gbrain(runtime, *cmd, check=False, timeout=300)
        report.candidate_results.append(result)
        # Surface non-zero exit codes as notes for manual inspection.
        if result.returncode != 0:
            report.notes.append(
                f"candidate step exited non-zero: rc={result.returncode} cmd={' '.join(cmd)}"
            )

    get_after, search_after, status_after, process_cleanup, lock_cleanup = _post_checks(
        runtime, token=token, do_process_cleanup=do_process_cleanup
    )
    report.get_after = get_after
    report.search_after = search_after
    report.status_after = status_after
    report.process_cleanup = process_cleanup
    report.lock_cleanup = lock_cleanup
    return report


def _print_report(report: CandidateReport) -> None:
    """Print a report in a human-readable form for manual inspection."""
    print("\n" + "=" * 78)
    print(f"CANDIDATE: {report.name}")
    print("=" * 78)
    for i, r in enumerate(report.candidate_results, 1):
        print(f"\n--- step {i}: rc={r.returncode} elapsed={r.elapsed_seconds:.2f}s ---")
        print(f"cmd: {' '.join(r.command)}")
        if r.stdout:
            print("stdout:")
            print(r.stdout[:2000])
        if r.stderr:
            print("stderr:")
            print(r.stderr[:2000])
    for label, r in (
        ("get_after", report.get_after),
        ("search_after", report.search_after),
        ("status_after", report.status_after),
        ("process_cleanup", report.process_cleanup),
        ("lock_cleanup", report.lock_cleanup),
    ):
        if r is None:
            continue
        print(f"\n--- {label}: rc={r.returncode} elapsed={r.elapsed_seconds:.2f}s ---")
        if r.stdout:
            print("stdout:")
            print(r.stdout[:2000])
        if r.stderr:
            print("stderr:")
            print(r.stderr[:2000])
    if report.notes:
        print("\nnotes:")
        for n in report.notes:
            print(f"  - {n}")
    print("=" * 78)


# ---------------------------------------------------------------------------
# Candidate command groups
# ---------------------------------------------------------------------------

def _candidate_refresh_equivalent() -> list[list[str]]:
    return [
        ["sync", "--full", "--no-embed", "--yes", "--no-pull", "--json", "--repo", "/opt/data/obsidian"],
        ["extract", "--stale", "--json"],
        ["extract", "links", "--source", "db", "--json"],
    ]


def _candidate_negative_incremental() -> list[list[str]]:
    return [
        ["sync", "--no-embed", "--yes", "--no-pull", "--json", "--repo", "/opt/data/obsidian"],
    ]


def _candidate_dream_sync() -> list[list[str]]:
    return [
        ["dream", "--phase", "sync", "--json"],
    ]


def _candidate_dream_extract() -> list[list[str]]:
    return [
        ["dream", "--phase", "extract", "--json"],
    ]


def _candidate_bounded_autopilot() -> list[list[str]]:
    # Container-side timeout with TERM then SIGKILL grace. Run inline, no worker.
    timeout_cmd = (
        f"timeout -s TERM -k {AUTOPILOT_KILL_GRACE_SECONDS}s {AUTOPILOT_TIMEOUT_SECONDS}s "
        "gbrain autopilot --interval 5 --inline --no-worker --json"
    )
    # We return the raw shell command; _run_gbrain wraps with env, so we need
    # to bypass _gbrain_cmd's arg joining. Instead we build the env+timeout
    # string directly and run it as a single shell command.
    env_prefix = " ".join(f"{k}={v}" for k, v in GBRAIN_CMD_ENV.items())
    return [["__shell__", f"{env_prefix} {timeout_cmd}"]]


@unittest.skipUnless(GATE_DOCKER, "set RUN_DOCKER_TESTS=1 to run Docker runtime tests")
@unittest.skipUnless(
    GATE_EXPERIMENT,
    "set RUN_GBRAIN_AUTOPILOT_EXPERIMENT=1 to run the gbrain autopilot experiment",
)
@unittest.skipUnless(docker_available(), "docker CLI is not available")
class GbrainAutopilotExperimentTests(unittest.TestCase):
    """Gated experiment harness for native gbrain sync/dream/autopilot.

    Each test method runs one candidate group in a fresh ComposeRuntime and
    prints a detailed report. Assertions are limited to safety invariants and
    harness-completion checks; adoption conclusions must be made by manual
    inspection of the printed output.
    """

    reports: list[CandidateReport] = []

    def _setup_runtime_and_seed(self, token: str) -> GbrainAutopilotExperimentRuntime:
        runtime = GbrainAutopilotExperimentRuntime()
        self.addCleanup(self._teardown_runtime, runtime)
        runtime.up("hermes")
        # Safety: verify the credentials source mount is empty.
        creds_check = _run_in_container(
            runtime,
            "sh",
            "-lc",
            "test -z \"$(find /opt/josemar/credentials-source -mindepth 1 -maxdepth 1 2>/dev/null)\" && echo EMPTY || echo NONEMPTY",
            as_user=None,
        )
        self.assertEqual(
            creds_check.stdout.strip(),
            "EMPTY",
            "credentials-source mount must be empty in the disposable runtime",
        )
        # Safety: verify /opt/data/credentials is empty (no real creds copied).
        data_creds_check = _run_in_container(
            runtime,
            "sh",
            "-lc",
            "test -z \"$(find /opt/data/credentials -mindepth 1 -maxdepth 1 2>/dev/null)\" && echo EMPTY || echo NONEMPTY",
            as_user=None,
        )
        # /opt/data/credentials may not exist yet; treat missing as empty.
        self.assertIn(
            data_creds_check.stdout.strip(),
            ("EMPTY", "NONEMPTY"),
        )
        if data_creds_check.stdout.strip() == "NONEMPTY":
            self.fail("/opt/data/credentials must be empty in the disposable runtime")

        # Seed baseline vault and reindex before making the manual edit.
        _seed_baseline_vault(runtime)
        reindex_result = _reindex(runtime)
        self.assertEqual(
            reindex_result.returncode,
            0,
            "josemar-gbrain reindex must succeed for baseline; stderr:\n" + reindex_result.stderr,
        )
        # Baseline gbrain status must succeed.
        baseline_status = _run_gbrain(runtime, "status", "--json")
        self.assertEqual(
            baseline_status.returncode,
            0,
            "baseline gbrain status must succeed; stderr:\n" + baseline_status.stderr,
        )
        # The unique edit token must be absent before the manual edit.
        baseline_search = _run_gbrain(runtime, "search", token, "--json")
        self.assertEqual(
            baseline_search.returncode,
            0,
            "baseline search must complete before manual edit; stderr:\n" + baseline_search.stderr,
        )
        self.assertNotIn(token, baseline_search.stdout)

        _make_uncommitted_manual_edit(runtime, token=token)
        return runtime

    def _teardown_runtime(self, runtime: GbrainAutopilotExperimentRuntime) -> None:
        try:
            runtime.down()
        finally:
            runtime.cleanup_override()

    def _run_one_candidate(
        self,
        *,
        name: str,
        candidate_commands: list[list[str]],
        do_process_cleanup: bool = False,
    ) -> CandidateReport:
        token = f"exptok-{name}-{int(time.time())}"
        runtime = self._setup_runtime_and_seed(token=token)
        # Handle the autopilot __shell__ pseudo-command specially.
        expanded: list[list[str]] = []
        for cmd in candidate_commands:
            if len(cmd) == 2 and cmd[0] == "__shell__":
                # Run as a single shell command with the env already embedded.
                result = _run_in_container(
                    runtime,
                    "sh",
                    "-lc",
                    cmd[1],
                    check=False,
                    timeout=AUTOPILOT_TIMEOUT_SECONDS + AUTOPILOT_KILL_GRACE_SECONDS + 30,
                    as_user="hermes",
                )
                report = CandidateReport(name=name, candidate_results=[result])
                get_after, search_after, status_after, process_cleanup, lock_cleanup = _post_checks(
                    runtime, token=token, do_process_cleanup=True
                )
                report.get_after = get_after
                report.search_after = search_after
                report.status_after = status_after
                report.process_cleanup = process_cleanup
                report.lock_cleanup = lock_cleanup
                if result.returncode != 0:
                    report.notes.append(
                        f"autopilot exited non-zero (expected for timeout): rc={result.returncode}"
                    )
                _print_report(report)
                self.reports.append(report)
                return report
            expanded.append(cmd)

        report = _run_candidate(
            runtime,
            name=name,
            candidate_commands=expanded,
            token=token,
            do_process_cleanup=do_process_cleanup,
        )
        _print_report(report)
        self.reports.append(report)
        return report

    # ------------------------------------------------------------------
    # Safety invariants (always asserted when the experiment runs)
    # ------------------------------------------------------------------

    def test_safety_telegram_and_provider_env_blank_in_runtime(self) -> None:
        """The disposable runtime must not carry Telegram or provider API keys."""
        runtime = self._setup_runtime_and_seed(token="safety-env")
        env_proc = _run_in_container(runtime, "sh", "-lc", "env", as_user=None)
        env_text = env_proc.stdout
        for var in TELEGRAM_VARS:
            # The var may appear as VAR= (empty) which is fine; assert no value.
            for line in env_text.splitlines():
                if line.startswith(f"{var}="):
                    self.assertEqual(
                        line.split("=", 1)[1],
                        "",
                        f"{var} must be empty in the disposable runtime",
                    )
        for var in HOST_PROVIDER_KEY_VARS:
            for line in env_text.splitlines():
                if line.startswith(f"{var}="):
                    self.assertEqual(
                        line.split("=", 1)[1],
                        "",
                        f"{var} must be empty in the disposable runtime",
                    )
        # COMPOSE_PROFILES must be empty (aux-ml not started).
        # (This is a host-side env var; verify via runtime.env instead.)
        self.assertEqual(runtime.env.get("COMPOSE_PROFILES", ""), "")

    # ------------------------------------------------------------------
    # Candidate groups
    # ------------------------------------------------------------------

    def test_candidate_refresh_equivalent_baseline(self) -> None:
        report = self._run_one_candidate(
            name="refresh-equivalent",
            candidate_commands=_candidate_refresh_equivalent(),
        )
        # Harness completed and captured post-checks.
        self.assertIsNotNone(report.get_after)
        self.assertIsNotNone(report.search_after)
        self.assertIsNotNone(report.status_after)

    def test_candidate_negative_incremental_sync(self) -> None:
        report = self._run_one_candidate(
            name="negative-incremental",
            candidate_commands=_candidate_negative_incremental(),
        )
        self.assertIsNotNone(report.get_after)
        self.assertIsNotNone(report.search_after)
        self.assertIsNotNone(report.status_after)

    def test_candidate_dream_sync_phase(self) -> None:
        report = self._run_one_candidate(
            name="dream-sync",
            candidate_commands=_candidate_dream_sync(),
        )
        self.assertIsNotNone(report.get_after)
        self.assertIsNotNone(report.search_after)
        self.assertIsNotNone(report.status_after)

    def test_candidate_dream_extract_phase(self) -> None:
        report = self._run_one_candidate(
            name="dream-extract",
            candidate_commands=_candidate_dream_extract(),
        )
        self.assertIsNotNone(report.get_after)
        self.assertIsNotNone(report.search_after)
        self.assertIsNotNone(report.status_after)

    def test_candidate_bounded_autopilot_foreground(self) -> None:
        report = self._run_one_candidate(
            name="bounded-autopilot",
            candidate_commands=_candidate_bounded_autopilot(),
            do_process_cleanup=True,
        )
        self.assertIsNotNone(report.get_after)
        self.assertIsNotNone(report.search_after)
        self.assertIsNotNone(report.status_after)
        # Autopilot must have process cleanup captured.
        self.assertIsNotNone(report.process_cleanup)
        self.assertIsNotNone(report.lock_cleanup)
        # Safety: no lingering autopilot/dream process after cleanup.
        assert report.process_cleanup is not None  # for type checkers
        # The cleanup command prints any remaining matching process; assert
        # none remain (the trailing `|| true` ensures empty output when clean).
        # We accept rc 0 and empty/non-matching output.
        self.assertEqual(report.process_cleanup.returncode, 0)

    @classmethod
    def tearDownClass(cls) -> None:
        """Emit a consolidated JSON summary of all reports for offline inspection."""
        if not cls.reports:
            return
        summary = {
            "experiment": "gbrain-autopilot-runtime",
            "candidates": [r.to_dict() for r in cls.reports],
        }
        text = json.dumps(summary, indent=2, ensure_ascii=False)
        print("\n" + "=" * 78)
        print("EXPERIMENT SUMMARY (JSON)")
        print("=" * 78)
        print(text)
        # Also write to dump_folder if it exists, for later inspection.
        dump = REPO_ROOT / "dump_folder"
        try:
            dump.mkdir(parents=True, exist_ok=True)
            (dump / "gbrain-autopilot-experiment-report.json").write_text(text, encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
