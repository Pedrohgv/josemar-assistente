"""Opt-in provider-gated gbrain Dream cycle-start recovery conformance gate
(issue #126/#67, #4390).

Runs the REAL gbrain Dream synthesize lifecycle at the EXACT v0.46.26
candidate commit (``GBRAIN_DREAM_RECOVERY_CANDIDATE_REF``, an exact 40-hex
SHA validated before any Docker invocation; the image is built with
``--build-arg GBRAIN_REF=<ref>`` against the same disposable conformance
runtime) with a deterministic synthetic Anthropic-compatible mock, proving
the v0.46.26 automatic recovery mechanism:

  - baseline isolation contract (empty credentials, disabled owned jobs,
    synthetic vault) with the plain Hermes-only runtime (no overlays)
  - minimal core activation (``josemar-gbrain reindex``)
  - a deterministic loopback Anthropic-compatible mock (the fixture in
    ``tests/runtime/fixtures/gbrain_dream_mock.py``) started as the
    ``hermes`` runtime user inside the container, driven by the supported
    ``ANTHROPIC_BASE_URL`` env override with a non-secret fake key — no
    production provider credentials, no external network
  - synthesize/triage configured with short bounded timings: child
    subagent timeout 10s (well below the inline drain's 30s claim lock,
    ``INLINE_LOCK_MS``) and per-child wait timeout 8s, serial PGLite-safe
    inline handling; exactly one qualifying synthetic transcript seeded
  - run EXACTLY ``gbrain dream --phase synthesize --json`` as the native
    binary (``/opt/josemar/libexec/gbrain-native``) as ``hermes`` under
    the shared TaskNotes lock (``/opt/josemar/scripts/tasknotes_lock_run.py``
    + ``/opt/data/.locks/tasknotes.lock``); the public adapter is never
    used for dream
  - interruption: the mock triage returns a valid high-score verdict and
    the mock synthesis returns a valid one-page JSON but delays the FIRST
    synthesis call long enough for the test to SIGKILL the parent after
    the real private ``dream-inline-*`` child has been claimed; then the
    test proves the global lock is released/reacquirable and no live
    parent/native process remains
  - automatic recovery: an IMMEDIATE identical invocation is refused with
    the supported ``skipped: cycle_already_running`` report (the dead
    parent's cycle lock is younger than the 60s holder-takeover grace);
    then, after the stranded row's owner lease
    (``private_queue_lease_until``, observed via ``gbrain jobs get``)
    lapses — bounded, no manual cancellation, no ``jobs retry``, no DB
    writes — ONE identical rerun automatically reconciles the provably
    orphaned private queue at Dream cycle start: the stranded row is
    cancelled with the machine-readable reason
    ``private_queue_reconciled: cycle startup recovery: orphaned
    dream-inline private queue`` (observable via ``gbrain jobs get``), and
    the same input completes: the page is written and visible through the
    supported public ``gbrain get`` surface; private queue/idempotency
    state is inspected through the supported ``gbrain jobs list/get
    --json`` surface

Honest scope: #4390/v0.46.26 incorporates the #4361/#4332 terminal-path
lifecycle upstream. This gate claims EXACTLY automatic PGLite Dream
cycle-start recovery of orphaned private child work at the next
invocation's cycle start; it does not claim mid-cycle live healing of the
interrupted invocation itself. A candidate build failure because the
canonical local patch no longer applies is an upgrade incompatibility to
record, not a harness failure.

Runtime execution is gated strictly on ``RUN_DOCKER_TESTS=1`` AND
``RUN_GBRAIN_DREAM_RECOVERY=1`` AND a non-empty
``GBRAIN_DREAM_RECOVERY_CANDIDATE_REF`` and skips when the docker CLI is
absent. Fast host-side gate/mock/safety structure tests in this module
always run and need no Docker.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from .gbrain_conformance_support import (
    CONFORMANCE_EMPTY_ENV_KEYS,
    CommandEvidence,
    GbrainConformanceRuntime,
    conformance_report_dir,
    normalize_candidate_ref,
    write_report,
)
from .helpers import REPO_ROOT, docker_available


# Fixed deployment paths (same constants the core conformance module asserts).
PRIVATE_NATIVE = "/opt/josemar/libexec/gbrain-native"
LOCK_RUNNER = "/opt/josemar/scripts/tasknotes_lock_run.py"
LOCK_PATH = "/opt/data/.locks/tasknotes.lock"
PYTHON_BIN = "/opt/hermes/.venv/bin/python3"

# In-container mock facts: loopback-only port, script/log/pid/marker paths
# under the hermes-writable /opt/data surface. The mock is the fixture file
# tests/runtime/fixtures/gbrain_dream_mock.py, written into the container
# byte-exact (base64) and started detached as hermes.
DREAM_MOCK_PORT = 8766
DREAM_MOCK_SCRIPT = "/opt/data/gbrain-dream-mock.py"
DREAM_MOCK_LOG = "/opt/data/dream-mock-requests.log"
DREAM_MOCK_PID = "/opt/data/dream-mock.pid"
DREAM_MOCK_TRIAGE_JSON = "/opt/data/dream-mock-triage.json"
DREAM_MOCK_PAGE_JSON = "/opt/data/dream-mock-page.json"
DREAM_MOCK_FIRST_SEEN = "/opt/data/dream-mock-synthesis-first-seen"
DREAM_MOCK_BASE_URL = "http://127.0.0.1:" + str(DREAM_MOCK_PORT) + "/v1"

# Supported provider override: ANTHROPIC_BASE_URL plus a NON-SECRET fake
# key (test-only literal; never a real credential, never inherited).
FAKE_ANTHROPIC_API_KEY = "sk-ant-dream-recovery-test"

# Deterministic mock model ids. The triage model id carries "triage" and the
# synthesis model id does not, so the mock dispatches on the request's model
# field. Both are unknown ids: resolveModel/resolveAlias return them
# verbatim, and the synthesize fan-out prefixes claude-* ids with
# anthropic: for the subagent queue validator.
DREAM_TRIAGE_MODEL = "claude-dream-triage-test"
DREAM_SYNTH_MODEL = "claude-dream-synth-test"

# Short bounded timings (issue #126 gate). DREAM_SUBAGENT_TIMEOUT_MS (10s)
# is well below the inline drain's 30s claim lock (INLINE_LOCK_MS) and the
# per-child wait (8s) bounds each run's wait on a child, so a killed run can
# never wedge the automatic recovery rerun: it reconciles and completes
# inside DREAM_RETRY_WALL_CLOCK_LIMIT_S (60s) instead of hanging.
DREAM_SUBAGENT_TIMEOUT_MS = 10000
DREAM_SUBAGENT_WAIT_TIMEOUT_MS = 8000
DREAM_SYNTH_DELAY_MS = 30000  # mock delay for the FIRST synthesis call (kill window)
DREAM_RETRY_WALL_CLOCK_LIMIT_S = 60

# v0.46.26 automatic cycle-start recovery (issue #4390): the owner lease of
# an inline PGLite dream-inline-* queue is DEFAULT_PRIVATE_QUEUE_LEASE_MS
# (600s) from claim and is the queue-orphan criterion — the stranded row's
# private_queue_lease_until (observed via the supported `gbrain jobs get`
# surface) must lapse before the cycle-start recovery classifies the queue
# provably orphaned. This constant bounds that observable wait (fails
# loudly if the lease never lapses); it is NOT a blind 300/320s sleep.
DREAM_OWNER_LEASE_EXPIRY_WAIT_S = 720

# The EXACT v0.46.26 candidate commit the gate builds (issue #4390): the
# image is built with --build-arg GBRAIN_REF=<ref> against the same
# disposable conformance runtime. The ref must be an exact 40-hex SHA,
# validated before any Docker invocation (never branches/tags).
GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV = "GBRAIN_DREAM_RECOVERY_CANDIDATE_REF"

# The deterministic synthetic corpus: exactly one qualifying transcript.
DREAM_CORPUS_DIR = "/opt/data/transcripts-dream"
DREAM_TRANSCRIPT_PATH = DREAM_CORPUS_DIR + "/2026-08-20-session.txt"
DREAM_TRANSCRIPT_CONTENT = (
    "# Conversation Session 2026-08-20\n"
    "\n"
    "Alice and Bob discussed the automatic cycle-start recovery design for the "
    "dream synthesis gate. Alice articulated a durable thesis: an interrupted "
    "inline synthesis run strands its private child work, and the next identical "
    "invocation must reconcile the orphaned private queue at cycle start and "
    "complete the same input instead of waiting out a lease or asking the "
    "operator to cancel. Bob noted the upstream lifecycle now incorporates the "
    "terminal-path wave, so the honest contract is automatic cycle-start "
    "reconciliation, never a silent mid-cycle claim.\n"
    "\n"
    "Reflection: the durable signal here is the distinction between visible "
    "stranding and automatic reconciliation - a knowledge brain should recover "
    "the work it failed to finish.\n"
)

# Vault-side dream allow-list (filing-rules ladder rung 2: the engine-
# resolved brain repo). Mirrors the bundled globs so the mock's oneshot
# page slug is authorized and remapped by the default 'wiki' output root.
DREAM_FILING_RULES_JSON = (
    "{\n"
    '  "dream_synthesize_paths": {\n'
    '    "description": "Test-seeded allow-list for the issue #126 dream recovery gate.",\n'
    '    "globs": [\n'
    '      "wiki/personal/reflections/*",\n'
    '      "wiki/originals/*",\n'
    '      "wiki/personal/patterns/*",\n'
    '      "wiki/people/*",\n'
    '      "dream-cycle-summaries/*"\n'
    "    ]\n"
    "  }\n"
    "}\n"
)

# The deterministic triage verdict: a high score (0.9 >= default threshold
# 0.5) with the optional lenient fields the judge tolerates.
DREAM_TRIAGE_JSON = {
    "score": 0.9,
    "content_type": "reflection",
    "segments": [
        {
            "quote": "an interrupted inline synthesis run must strand its private child work visibly",
            "note": "bounded retry thesis",
        }
    ],
    "entities": ["alice"],
    "reasons": ["synthetic reflection with durable signal"],
}


def _dream_transcript_hash6() -> str:
    """sha256(transcript content) prefix — the pinned synthesize phase's
    content hash (``contentHash``) and the CDX-9 oneshot slug suffix for a
    single-chunk transcript. Computable host-side because the transcript is
    written byte-exact."""
    return hashlib.sha256(DREAM_TRANSCRIPT_CONTENT.encode("utf-8")).hexdigest()[:6]


def _dream_page_slug() -> str:
    """The deterministic one-page synthesis slug: inside the oneshot-only
    task shape (``wiki/originals/``) and ending with the CDX-9 hash suffix."""
    return "wiki/originals/dream-recovery-" + _dream_transcript_hash6()


def _parse_utc_ms(value: object) -> float:
    """Parse a jobs JSON UTC timestamp (``2026-08-22T01:37:23.216Z``) to
    epoch milliseconds — the observable lease-expiry wait compares it
    against the host clock (same host as the container)."""
    text = str(value).strip()
    dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt.timestamp() * 1000


# The deterministic one-page synthesis JSON the mock returns (after its
# first-call delay). Valid under the pinned oneshot contract: slug obeys the
# allow-list + task shape + hash suffix, body carries a wikilink
# ([[notes/welcome]] resolves in the synthetic vault; the cold-brain
# relaxation also accepts syntactic presence since link_manifest is off).
DREAM_PAGE_JSON = {
    "pages": [
        {
            "slug": _dream_page_slug(),
            "title": "Dream Recovery Synthesis",
            "type": "note",
            "body": (
                "# Dream Recovery Synthesis\n"
                "\n"
                "Deterministic one-page synthesis output for the issue #126/#67 "
                "dream cycle-start recovery gate.\n"
                "\n"
                "Key reflection: automatic cycle-start reconciliation recovers "
                "stranded dream-inline child work.\n"
                "\n"
                "See [[notes/welcome]] for the synthetic vault baseline.\n"
            ),
        }
    ],
    "skipped": False,
    "skip_reason": None,
}

# DB-plane dream config written through the supported operator path
# (``gbrain config set``, native under the shared lock — engine.getConfig
# reads the DB plane for dream.*/models.* keys). Short bounded timings plus
# deterministic corpus/triage/model wiring; cooldown disabled and triage
# threshold explicit so every rerun is deterministic.
DREAM_CONFIG_KEYS = [
    ("dream.synthesize.enabled", "true"),
    ("dream.synthesize.session_corpus_dir", DREAM_CORPUS_DIR),
    ("dream.synthesize.min_chars", "100"),
    ("dream.synthesize.cooldown_hours", "0"),
    ("dream.synthesize.subagent_timeout_ms", str(DREAM_SUBAGENT_TIMEOUT_MS)),
    ("dream.synthesize.subagent_wait_timeout_ms", str(DREAM_SUBAGENT_WAIT_TIMEOUT_MS)),
    ("dream.synthesize.inline_concurrency", "1"),
    ("dream.synthesize.link_manifest", "false"),
    ("dream.synthesize.mode", "oneshot"),
    ("dream.triage.threshold", "0.5"),
    ("models.dream.synthesize", DREAM_SYNTH_MODEL),
    ("models.dream.triage", DREAM_TRIAGE_MODEL),
]

# Dream gate matrix: every operation this gate owns, with its classification.
DREAM_RECOVERY_MATRIX = {
    "baseline_seed": "core",
    "baseline_build_start": "core",
    "baseline_writable": "core",
    "baseline_credentials": "core",
    "baseline_jobs": "core",
    "baseline_vault": "core",
    "reindex": "operator_only",
    "mock_health": "core",
    "dream_config": "operator_only",
    "corpus_seed": "core",
    "dream_interrupt_kill": "provider_gated",
    "lock_released": "core",
    "no_live_process": "core",
    "jobs_state_stranded": "operator_only",
    "recovery_immediate_refused": "provider_gated",
    "recovery_private_queue_reconciled": "provider_gated",
    "recover_automatic_completion": "provider_gated",
    "page_written": "core",
    "mock_called": "provider_gated",
}


def _dream_recovery_candidate_ref() -> str:
    """The EXACT v0.46.26 candidate ref (exact 40-hex Git SHA), validated
    before any Docker invocation; invalid/empty values fail closed."""
    return normalize_candidate_ref(
        os.getenv(GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV, "")
    )


def _dream_recovery_enabled() -> bool:
    """Strict gate: RUN_DOCKER_TESTS=1 AND RUN_GBRAIN_DREAM_RECOVERY=1 AND a
    candidate ref is set AND a docker CLI is available."""
    return (
        os.getenv("RUN_DOCKER_TESTS") == "1"
        and os.getenv("RUN_GBRAIN_DREAM_RECOVERY") == "1"
        and bool(os.getenv(GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV, "").strip())
        and docker_available()
    )


@unittest.skipUnless(
    _dream_recovery_enabled(),
    "set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_DREAM_RECOVERY=1 with a docker CLI",
)
class GbrainDreamRecoveryTestCase(unittest.TestCase):
    """Shared base setup for the provider-gated Dream cycle-start recovery
    conformance suite.

    Builds/starts the EXACT v0.46.26 candidate Hermes-only runtime against
    a disposable Compose project (no overlays), seeds the real template
    source state BEFORE start, waits for the hermes-writable surface,
    asserts the isolation safety contract (empty credentials, disabled
    owned jobs), initializes the synthetic vault as the hermes runtime
    user, runs minimal core activation (``josemar-gbrain reindex``), and
    unconditionally tears the project down with ``down -v --remove-orphans``.
    """

    def setUp(self) -> None:
        self._evidence: list[CommandEvidence] = []
        self._matrix: dict[str, str] = {
            op: "pass" if op.startswith("baseline_") else "not_run"
            for op in DREAM_RECOVERY_MATRIX
        }
        self._gbrain_version: str | None = None
        self._report_path: Path | None = None

        self.runtime = GbrainConformanceRuntime()
        # Pre-start source state seeding: real template .sync-manifest +
        # canonical josemar schema pack into the disposable source-agent-state.
        self.runtime.seed_source_state()
        # EXACT v0.46.26 candidate build (--build-arg GBRAIN_REF=<ref> against
        # the same disposable conformance runtime), then start WITHOUT an
        # implicit rebuild. A build failure because the canonical local patch
        # no longer applies is an upgrade incompatibility to record, not a
        # harness failure.
        self._candidate_ref = _dream_recovery_candidate_ref()
        self.runtime.build_candidate(self._candidate_ref, "hermes", timeout=900)
        self.runtime.start("hermes", timeout=900)
        # Wait for the exact hermes-writable surface before any exec probe.
        self.runtime.wait_until_hermes_writable(timeout=120)
        # Safety checks: empty credentials (incl. the provider override env)
        # + disabled owned jobs.
        self._evidence.append(self._assert_no_credentials())
        self._evidence.append(self.runtime.assert_owned_jobs_disabled())
        # Synthetic vault init committed as the hermes runtime user.
        self._evidence.append(self.runtime.init_synthetic_vault())
        # Minimal core activation: PGLite init + sync.repo_path config +
        # vault sync (the filing-rules ladder rung 2 and the page universe
        # the oneshot validation reads both depend on it).
        self._matrix["reindex"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "reindex")
        self.assertEqual(envelope.get("schema_pack"), "josemar")
        status = self.runtime.run_as_hermes("gbrain", "status", "--json", timeout=120)
        self.assertEqual(status.returncode, 0, status.stderr)
        self._evidence.append(status)
        self._gbrain_version = json.loads(status.stdout).get("version")
        self._matrix["reindex"] = "pass"

    def tearDown(self) -> None:
        # Unconditional final cleanup: down -v --remove-orphans.
        self.runtime.cleanup()

    # --- safety helpers ---------------------------------------------------

    def _assert_no_credentials(self) -> CommandEvidence:
        """Assert every conformance-blanked credential env key — plus the
        provider override env (ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY, which
        only ever ride a per-invocation ``env`` prefix, never the runtime
        environment) — is empty inside the running container."""
        keys = list(CONFORMANCE_EMPTY_ENV_KEYS) + [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
        ]
        script = (
            "set -eu\n"
            "for k in " + " ".join(keys) + "; do\n"
            "  v=$(printenv \"$k\" 2>/dev/null || true)\n"
            "  if [ -n \"$v\" ]; then\n"
            "    echo \"credential env key $k is non-empty\" >&2\n"
            "    exit 1\n"
            "  fi\n"
            "done\n"
            "echo no-credentials-present\n"
        )
        ev = self.runtime.run_as_hermes("sh", "-lc", script)
        self.assertIn("no-credentials-present", ev.stdout, ev.stderr)
        return ev

    # --- safe internal/operator path (native under the shared lock) -------

    def _run_native_under_lock(
        self, *args: str, timeout: int = 120
    ) -> CommandEvidence:
        """Run a native gbrain command as the hermes runtime user under the
        shared TaskNotes lock via the lock runner — the same safe
        internal/operator mechanism the operator wrapper and the public
        adapter use. The canonical gbrain env is exported explicitly (never
        caller-controlled). The public adapter is deliberately NOT used for
        operator-only ``config``/``jobs``/``dream`` work."""
        return self.runtime.run_as_hermes(
            "env",
            "GBRAIN_HOME=/opt/data",
            "GBRAIN_BRAIN_REPO=/opt/data/obsidian",
            "GBRAIN_SCHEMA_PACK=josemar",
            "GBRAIN_SKIP_STARTUP_HOOKS=1",
            PYTHON_BIN, "-I",
            LOCK_RUNNER,
            "--lock-path", LOCK_PATH,
            "--lock-timeout", "30",
            "--timeout", str(timeout),
            "--",
            PRIVATE_NATIVE, *args,
            timeout=timeout + 60,
        )

    def _run_dream_native(self, *args: str, timeout: int = 90) -> CommandEvidence:
        """Run a native dream invocation as hermes under the shared lock with
        the provider override env (ANTHROPIC_BASE_URL -> the in-container
        mock + the non-secret fake key). This is the ONLY place provider
        env reaches a gbrain process."""
        return self.runtime.run_as_hermes(
            "env",
            "GBRAIN_HOME=/opt/data",
            "GBRAIN_BRAIN_REPO=/opt/data/obsidian",
            "GBRAIN_SCHEMA_PACK=josemar",
            "GBRAIN_SKIP_STARTUP_HOOKS=1",
            "ANTHROPIC_BASE_URL=" + DREAM_MOCK_BASE_URL,
            "ANTHROPIC_API_KEY=" + FAKE_ANTHROPIC_API_KEY,
            PYTHON_BIN, "-I",
            LOCK_RUNNER,
            "--lock-path", LOCK_PATH,
            "--lock-timeout", "30",
            "--timeout", str(timeout),
            "--",
            PRIVATE_NATIVE, *args,
            timeout=timeout + 60,
        )

    def _jobs_list(self) -> list[dict]:
        ev = self._run_native_under_lock("jobs", "list", "--json", timeout=60)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        return json.loads(ev.stdout)

    def _jobs_get(self, job_id: int) -> dict:
        ev = self._run_native_under_lock(
            "jobs", "get", str(job_id), "--json", timeout=60
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        return json.loads(ev.stdout)

    # --- in-container mock lifecycle --------------------------------------

    def _start_mock(self) -> None:
        """Write the mock fixture + deterministic payload files as hermes
        (byte-exact via base64), start the mock detached as hermes inside
        the container, and wait until it serves health over loopback."""
        cid = self.runtime.run("ps", "-q", "hermes").stdout.strip()
        self.assertTrue(cid, "hermes container id must resolve")
        fixture = (
            REPO_ROOT / "tests" / "runtime" / "fixtures" / "gbrain_dream_mock.py"
        ).read_text(encoding="utf-8")
        script_b64 = base64.b64encode(fixture.encode("utf-8")).decode("ascii")
        triage_b64 = base64.b64encode(
            json.dumps(DREAM_TRIAGE_JSON).encode("utf-8")
        ).decode("ascii")
        page_b64 = base64.b64encode(
            json.dumps(DREAM_PAGE_JSON).encode("utf-8")
        ).decode("ascii")
        write = self.runtime.run_as_hermes(
            "sh", "-lc",
            "set -eu; "
            "mkdir -p /opt/data; "
            "rm -f " + DREAM_MOCK_LOG + " " + DREAM_MOCK_FIRST_SEEN + "; "
            + PYTHON_BIN + " -c 'import base64,sys;open(sys.argv[1],\"wb\").write(base64.b64decode(sys.argv[2]))' "
            + DREAM_MOCK_SCRIPT + " " + script_b64 + "; "
            + PYTHON_BIN + " -c 'import base64,sys;open(sys.argv[1],\"wb\").write(base64.b64decode(sys.argv[2]))' "
            + DREAM_MOCK_TRIAGE_JSON + " " + triage_b64 + "; "
            + PYTHON_BIN + " -c 'import base64,sys;open(sys.argv[1],\"wb\").write(base64.b64decode(sys.argv[2]))' "
            + DREAM_MOCK_PAGE_JSON + " " + page_b64,
            timeout=60,
        )
        self.assertEqual(write.returncode, 0, write.stderr)
        self._evidence.append(write)
        proc = subprocess.run(
            [
                "docker", "exec", "-d", cid,
                "su", "-s", "/bin/sh", "hermes", "-c",
                "python3 " + DREAM_MOCK_SCRIPT + " "
                + str(DREAM_MOCK_PORT) + " " + DREAM_MOCK_LOG + " "
                + DREAM_MOCK_PID + " " + DREAM_MOCK_TRIAGE_JSON + " "
                + DREAM_MOCK_PAGE_JSON + " " + str(DREAM_SYNTH_DELAY_MS) + " "
                + DREAM_MOCK_FIRST_SEEN,
            ],
            capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = self.runtime.run_as_hermes(
                "curl", "-fsS",
                "http://127.0.0.1:" + str(DREAM_MOCK_PORT) + "/v1/models",
                check=False, timeout=30,
            )
            if probe.returncode == 0:
                self._evidence.append(probe)
                return
            time.sleep(0.5)
        raise AssertionError("dream mock did not become healthy in time")

    def _stop_mock(self) -> None:
        """Kill the in-container mock (releasing its port) as hermes."""
        script = (
            "set -eu\n"
            "pid=$(cat " + DREAM_MOCK_PID + " 2>/dev/null || true)\n"
            "if [ -n \"$pid\" ]; then kill \"$pid\" 2>/dev/null || true; fi\n"
        )
        self.runtime.run_as_hermes("sh", "-lc", script, check=False, timeout=30)

    def _count_mock_calls(self) -> tuple[int, int]:
        """(triage, synthesis) request counts from the mock's request log."""
        ev = self.runtime.run_as_hermes("cat", DREAM_MOCK_LOG, timeout=60)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        triage = 0
        synthesis = 0
        for line in ev.stdout.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("phase") == "triage":
                triage += 1
            elif record.get("phase") == "synthesis":
                synthesis += 1
        return triage, synthesis

    # --- corpus + vault allow-list seeding --------------------------------

    def _seed_dream_state(self) -> None:
        """Seed the deterministic corpus (exactly one qualifying transcript,
        byte-exact via base64) and the vault-side dream allow-list
        (filing-rules ladder rung 2)."""
        self._matrix["corpus_seed"] = "fail"
        transcript_b64 = base64.b64encode(
            DREAM_TRANSCRIPT_CONTENT.encode("utf-8")
        ).decode("ascii")
        script = (
            "set -eu\n"
            "mkdir -p " + DREAM_CORPUS_DIR + " /opt/data/obsidian/skills\n"
            + PYTHON_BIN + " -c 'import base64,sys;open(sys.argv[1],\"wb\").write(base64.b64decode(sys.argv[2]))' "
            + DREAM_TRANSCRIPT_PATH + " " + transcript_b64 + "\n"
            "cat > /opt/data/obsidian/skills/_brain-filing-rules.json <<'JSON'\n"
            + DREAM_FILING_RULES_JSON
            + "JSON\n"
        )
        ev = self.runtime.run_as_hermes("sh", "-lc", script, timeout=60)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self._matrix["corpus_seed"] = "pass"

    def _scenario_dream_config(self) -> None:
        """Write the DB-plane dream config through the supported operator
        path (``gbrain config set``, native under the shared lock): short
        bounded subagent/wait timings, serial PGLite-safe inline handling,
        deterministic models/triage, cooldown off."""
        self._matrix["dream_config"] = "fail"
        for key, value in DREAM_CONFIG_KEYS:
            ev = self._run_native_under_lock(
                "config", "set", key, value, timeout=60
            )
            self.assertEqual(ev.returncode, 0, f"{key}={value}: {ev.stderr}")
            self._evidence.append(ev)
        self._matrix["dream_config"] = "pass"

    # --- interruption helpers ---------------------------------------------

    def _launch_dream_background(self, log_path: str) -> None:
        """Launch ``gbrain dream --phase synthesize --json`` DETACHED inside
        the container: the shell writes its own PID (the lock-runner PID
        after exec) to /opt/data/dream-run.pid, then execs the lock runner
        around the native binary with the provider override env. All as the
        hermes runtime user."""
        cid = self.runtime.run("ps", "-q", "hermes").stdout.strip()
        self.assertTrue(cid, "hermes container id must resolve")
        inner = (
            "echo $$ > /opt/data/dream-run.pid; "
            "exec env GBRAIN_HOME=/opt/data GBRAIN_BRAIN_REPO=/opt/data/obsidian "
            "GBRAIN_SCHEMA_PACK=josemar GBRAIN_SKIP_STARTUP_HOOKS=1 "
            "ANTHROPIC_BASE_URL=" + DREAM_MOCK_BASE_URL + " "
            "ANTHROPIC_API_KEY=" + FAKE_ANTHROPIC_API_KEY + " "
            + PYTHON_BIN + " -I " + LOCK_RUNNER + " "
            "--lock-path " + LOCK_PATH + " --lock-timeout 30 --timeout 120 -- "
            + PRIVATE_NATIVE + " dream --phase synthesize --json "
            "> " + log_path + " 2>&1"
        )
        proc = subprocess.run(
            [
                "docker", "exec", "-d", cid,
                "su", "-s", "/bin/sh", "hermes", "-c", inner,
            ],
            capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _wait_for_synthesis_claim(self, timeout: int = 90) -> None:
        """Wait until the mock reports the FIRST synthesis request — the
        inline-drained subagent child has been claimed and is in flight."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            probe = self.runtime.run_as_hermes(
                "test", "-f", DREAM_MOCK_FIRST_SEEN, check=False, timeout=30
            )
            if probe.returncode == 0:
                self._evidence.append(probe)
                return
            time.sleep(0.5)
        raise AssertionError(
            "the synthesis subagent was never claimed (no mock synthesis request)"
        )

    def _sigkill_dream_parent(self) -> CommandEvidence:
        """Controlled SIGKILL of the dream parent (the bun process running
        the native CLI). The inline-drained subagent child dies with it; the
        lock runner then reaps and releases the flock. The kill pattern is
        built from shell fragments so the probe script cannot match itself."""
        script = (
            "set -eu\n"
            "b=bun\n"
            "pid=$(ps -u hermes -o pid= -o args= | grep \"/usr/local/bin/$b\" "
            "| grep -v grep | awk '{print $1}' | head -1)\n"
            "test -n \"$pid\" || { echo 'no dream parent process found' >&2; exit 1; }\n"
            "kill -9 \"$pid\"\n"
            "echo sigkilled-$pid\n"
        )
        ev = self.runtime.run_as_hermes("sh", "-lc", script, timeout=60)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        return ev

    def _wait_runner_cleared(self, timeout: int = 20) -> None:
        """Wait until the lock-runner process (whose PID the background
        launcher recorded) has exited — the flock is then provably free."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            probe = self.runtime.run_as_hermes(
                "sh", "-lc",
                "kill -0 \"$(cat /opt/data/dream-run.pid)\" 2>/dev/null "
                "&& echo alive || echo cleared",
                check=False, timeout=30,
            )
            if "cleared" in probe.stdout:
                self._evidence.append(probe)
                return
            time.sleep(0.5)
        raise AssertionError(
            "the lock-runner process did not exit after the parent SIGKILL"
        )

    def _assert_no_live_dream_process(self) -> CommandEvidence:
        """Assert no live parent/native process remains: no bun-running-the-
        CLI process and no lock-runner process (the mock server itself is a
        python3 process and never matches)."""
        script = (
            "set -eu\n"
            "b=bun\n"
            "l=tasknotes_lock_run\n"
            "out=$(ps -u hermes -o pid= -o args= "
            "| grep -E \"/usr/local/bin/$b|$l\" | grep -v grep || true)\n"
            "if [ -n \"$out\" ]; then "
            "echo \"live dream process remains: $out\" >&2; exit 1; fi\n"
            "echo no-live-dream-process\n"
        )
        ev = self.runtime.run_as_hermes("sh", "-lc", script, timeout=60)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        return ev

    # --- report -----------------------------------------------------------

    def _write_report(self) -> None:
        """Persist the synthetic conformance report under
        ``dump_folder/gbrain-conformance``. Contains command/result metadata
        only (argv, rc, stdout, stderr, elapsed) plus the explicit matrix —
        never the process or runtime environment."""
        metadata = {
            "baseline_ref": self.runtime.baseline_gbrain_ref(),
            "candidate_ref": self._candidate_ref,
            "gbrain_version": self._gbrain_version,
            "matrix": self._matrix,
        }
        self._report_path = write_report(
            conformance_report_dir(),
            "gbrain-dream-recovery",
            self._evidence,
            metadata=metadata,
        )


class GbrainDreamRecoveryRuntimeTests(GbrainDreamRecoveryTestCase):
    """Provider-gated Dream cycle-start recovery lifecycle (Docker-gated via
    the base class): SIGKILL mid-synthesize -> stranded private child ->
    immediate identical rerun is refused with ``cycle_already_running``; after
    bounded owner-lease expiry, one rerun reconciles the orphaned queue
    (``private_queue_reconciled:``) and completes the same input/page."""

    def test_dream_interruption_automatic_recovery_gate(self) -> None:
        try:
            self._scenario_dream_config()
            self._seed_dream_state()
            self._start_mock()
            try:
                self._scenario_mock_health()
                stranded_id, stranded_key = self._scenario_interrupt()
                self._scenario_automatic_recovery(stranded_id, stranded_key)
                self._scenario_mock_evidence()
            finally:
                self._stop_mock()
        finally:
            self._write_report()

    def _scenario_mock_health(self) -> None:
        """The in-container mock serves the Anthropic-compatible models
        surface over loopback."""
        self._matrix["mock_health"] = "fail"
        ev = self.runtime.run_as_hermes(
            "curl", "-fsS",
            "http://127.0.0.1:" + str(DREAM_MOCK_PORT) + "/v1/models",
            timeout=60,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self._matrix["mock_health"] = "pass"

    def _scenario_interrupt(self) -> tuple[int, str]:
        """Run EXACTLY ``gbrain dream --phase synthesize --json`` (native
        under the shared lock) in the background; wait until the real
        private ``dream-inline-*`` child has been claimed (the mock reports
        the first synthesis request); SIGKILL the parent; prove the global
        lock is released/reacquirable and no live parent/native process
        remains; return the stranded child's (id, idempotency_key)."""
        self._matrix["dream_interrupt_kill"] = "fail"
        self._matrix["lock_released"] = "fail"
        self._matrix["no_live_process"] = "fail"
        self._matrix["jobs_state_stranded"] = "fail"
        self._launch_dream_background("/opt/data/dream-run-1.log")
        self._wait_for_synthesis_claim(timeout=90)
        kill_ev = self._sigkill_dream_parent()
        self.assertIn("sigkilled-", kill_ev.stdout, kill_ev.stdout)
        self._matrix["dream_interrupt_kill"] = "pass"
        self._wait_runner_cleared(timeout=20)
        self._assert_no_live_dream_process()
        self._matrix["no_live_process"] = "pass"
        # Lock released/reacquirable: jobs list acquires the shared lock
        # within its 30s wait and reads the queue — proof the flock was
        # released with the dead parent.
        jobs = self._jobs_list()
        self._matrix["lock_released"] = "pass"
        subagent_rows = [j for j in jobs if j.get("name") == "subagent"]
        self.assertEqual(
            len(subagent_rows), 1, "exactly one stranded synthesis child"
        )
        row = subagent_rows[0]
        self.assertEqual(row["status"], "active")
        self.assertTrue(
            str(row["queue"]).startswith("dream-inline-"), row["queue"]
        )
        self.assertTrue(
            str(row["idempotency_key"]).startswith("dream:synth-v2:"),
            row["idempotency_key"],
        )
        self.assertEqual(row["timeout_ms"], DREAM_SUBAGENT_TIMEOUT_MS)
        self._matrix["jobs_state_stranded"] = "pass"
        return int(row["id"]), str(row["idempotency_key"])

    def _scenario_automatic_recovery(
        self, stranded_id: int, stranded_key: str
    ) -> None:
        """Prove the v0.46.26 automatic cycle-start recovery (issue #4390):

        1. The immediate identical rerun is REFUSED with a supported
           ``skipped: cycle_already_running`` report: the dead parent's
           cycle lock is younger than the 60s holder-takeover grace
           (``HOLDER_TAKEOVER_GRACE_MS``) — observable dead-holder lock
           evidence, no state change.
        2. The stranded row's owner lease (``private_queue_lease_until``
           via the supported ``gbrain jobs get`` surface) is the
           queue-orphan criterion; the gate polls it (bounded by
           ``DREAM_OWNER_LEASE_EXPIRY_WAIT_S``) instead of any manual
           cancellation or ``jobs retry``.
        3. ONE identical rerun then automatically reconciles the provably
           orphaned private queue at Dream cycle start: the stranded row is
           cancelled with the machine-readable reason family
           ``private_queue_reconciled: cycle startup recovery: orphaned
           dream-inline private queue`` — observable via ``gbrain jobs
           get`` — and the same input completes: the page is written and
           visible through the supported public ``gbrain get`` surface."""
        self._matrix["recovery_immediate_refused"] = "fail"
        self._matrix["recovery_private_queue_reconciled"] = "fail"
        self._matrix["recover_automatic_completion"] = "fail"
        self._matrix["page_written"] = "fail"

        # 1. Immediate identical rerun: dead-holder cycle lock too young.
        ev = self._run_dream_native(
            "dream", "--phase", "synthesize", "--json", timeout=90
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        immediate = json.loads(ev.stdout)
        self.assertEqual(
            immediate.get("status"), "skipped",
            "the immediate rerun must be refused while the dead parent's "
            "cycle lock is younger than the holder-takeover grace",
        )
        self.assertEqual(immediate.get("reason"), "cycle_already_running")
        self._matrix["recovery_immediate_refused"] = "pass"

        # 2. Observable bounded wait: poll the stranded row's owner lease
        # (private_queue_lease_until) until it lapses — the queue-orphan
        # criterion. Never a blind 300/320s sleep, never manual cancel.
        got = self._jobs_get(stranded_id)
        lease_until_ms = _parse_utc_ms(got.get("private_queue_lease_until"))
        deadline = time.monotonic() + DREAM_OWNER_LEASE_EXPIRY_WAIT_S
        while (
            time.monotonic() < deadline
            and time.time() * 1000 < lease_until_ms
        ):
            time.sleep(10)
            self._jobs_get(stranded_id)
        self.assertGreaterEqual(
            time.time() * 1000,
            lease_until_ms,
            "the stranded row's owner lease must lapse inside the bounded "
            f"wait ({DREAM_OWNER_LEASE_EXPIRY_WAIT_S}s)",
        )

        # 3. The single recovery rerun: dead-holder cycle lock auto-taken
        # over, cycle-start recovery reconciles the orphaned queue, a fresh
        # child completes the same input inside the strict bounded
        # wall-time.
        start = time.monotonic()
        ev = self._run_dream_native(
            "dream", "--phase", "synthesize", "--json", timeout=90
        )
        elapsed = time.monotonic() - start
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertLess(
            elapsed,
            DREAM_RETRY_WALL_CLOCK_LIMIT_S,
            f"automatic recovery must finish inside "
            f"{DREAM_RETRY_WALL_CLOCK_LIMIT_S}s, took {elapsed:.1f}s",
        )
        self._evidence.append(ev)
        report = json.loads(ev.stdout)
        phase = report["phases"][0]
        self.assertEqual(phase["status"], "ok")
        details = phase["details"]
        self.assertEqual(details["transcripts_processed"], 1)
        self.assertEqual(details["pages_written"], 1)
        self.assertEqual(details["children_submitted"], 1)
        outcomes = details["child_outcomes"]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(
            outcomes[0]["status"], "completed",
            "after automatic cycle-start reconciliation the rerun must "
            "complete the same input normally",
        )
        self.assertEqual(details["triage"]["cache_hits"], 1)
        self._matrix["recover_automatic_completion"] = "pass"

        # 4. Observable recovery reason via the supported jobs get surface:
        # the stranded row was cancelled by the cycle-start recovery with
        # the machine-readable private_queue_reconciled reason family.
        got = self._jobs_get(stranded_id)
        self.assertEqual(got["status"], "cancelled")
        self.assertTrue(
            str(got.get("error_text") or "").startswith(
                "private_queue_reconciled: cycle startup recovery:"
            ),
            f"recovery reason missing: {got.get('error_text')!r}",
        )
        self._matrix["recovery_private_queue_reconciled"] = "pass"

        # 5. Private queue state: no live/waiting rows remain; the input is
        # completed under the SAME idempotency key on a fresh row.
        jobs = self._jobs_list()
        subagent_rows = [j for j in jobs if j.get("name") == "subagent"]
        self.assertTrue(subagent_rows, "at least one subagent row remains")
        for row in subagent_rows:
            self.assertNotIn(
                row["status"], ("active", "waiting"),
                f"no row may stay active/waiting after automatic recovery: "
                f"#{row['id']} status={row['status']}",
            )
        completed = [r for r in subagent_rows if r.get("status") == "completed"]
        self.assertTrue(completed, "the input must complete on the queue")
        self.assertTrue(
            any(r.get("idempotency_key") == stranded_key for r in completed),
            "the completed row must reuse the same idempotency key",
        )
        completed_row = completed[0]
        got_completed = self._jobs_get(int(completed_row["id"]))
        self.assertEqual(got_completed["status"], "completed")
        self.assertTrue(str(got_completed["queue"]).startswith("dream-inline-"))
        self.assertIsNotNone(got_completed["finished_at"])

        # 6. The synthesized page is written and visible through the
        # supported public read surface (plus the vault markdown
        # reverse-write).
        get_ev = self.runtime.run_as_hermes(
            "gbrain", "get", _dream_page_slug(), timeout=60
        )
        self.assertEqual(get_ev.returncode, 0, get_ev.stderr)
        self.assertIn("Dream Recovery Synthesis", get_ev.stdout)
        self._evidence.append(get_ev)
        self._matrix["page_written"] = "pass"

    def _scenario_mock_evidence(self) -> None:
        """The judge + subagent must have called the in-container mock
        through the real LLM path: exactly ONE triage request (verdict
        cached on the recovery rerun) and exactly TWO synthesis requests
        (the delayed one that was killed, then the instant one from the
        automatically recovered rerun)."""
        self._matrix["mock_called"] = "fail"
        triage, synthesis = self._count_mock_calls()
        self.assertEqual(triage, 1, "the triage judge must be called exactly once")
        self.assertEqual(
            synthesis, 2,
            "the synthesis subagent must be called twice: the killed run "
            "and the automatic-recovery run",
        )
        self._matrix["mock_called"] = "pass"


class GbrainDreamRecoveryGateStructureTests(unittest.TestCase):
    """Fast host-side guards for the dream recovery gate: the env gate, the
    exact synthesize invocation, locked native execution, no direct DB
    writes, the explicit bounded retry/cancel path, cleanup, and the mock
    contract. No Docker required; these run in the normal fast suite."""

    @staticmethod
    def _module_text() -> str:
        return (
            REPO_ROOT / "tests" / "runtime" / "test_gbrain_dream_recovery.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _runtime_class_text() -> str:
        """The runtime portion of the module: the shared base test case class
        plus the Docker-gated scenario class (everything before the fast gate
        structure tests)."""
        text = GbrainDreamRecoveryGateStructureTests._module_text()
        runtime_class = text.split("class GbrainDreamRecoveryTestCase", 1)[1]
        return runtime_class.split(
            "class GbrainDreamRecoveryGateStructureTests", 1
        )[0]

    @staticmethod
    def _fixture_text() -> str:
        return (
            REPO_ROOT / "tests" / "runtime" / "fixtures" / "gbrain_dream_mock.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _docker_available_patch(available: bool):
        """Patch this module's own ``docker_available`` reference (robust
        against double-import under ``discover -s tests``)."""
        return mock.patch.object(
            sys.modules[__name__], "docker_available", return_value=available
        )

    # --- gate env vars ----------------------------------------------------

    def test_gate_requires_run_docker_tests(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "",
                "RUN_GBRAIN_DREAM_RECOVERY": "1",
                GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV: "0" * 40,
            },
            clear=True,
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_dream_recovery_enabled())

    def test_gate_requires_run_gbrain_dream_recovery(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_DREAM_RECOVERY": "",
                GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV: "0" * 40,
            },
            clear=True,
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_dream_recovery_enabled())

    def test_gate_requires_candidate_ref(self) -> None:
        """The gate must require the exact v0.46.26 candidate ref: without
        it the suite cannot prove the version it claims. ``clear=True`` so a
        candidate ref inherited from the invoking Make target is actually
        removed inside this subtest (and restored afterwards)."""
        with mock.patch.dict(
            os.environ,
            {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_DREAM_RECOVERY": "1"},
            clear=True,
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_dream_recovery_enabled())

    def test_gate_requires_docker(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_DREAM_RECOVERY": "1",
                GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV: "0" * 40,
            },
            clear=True,
        ):
            with self._docker_available_patch(False):
                self.assertFalse(_dream_recovery_enabled())

    def test_gate_enabled_when_all_conditions_met(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RUN_DOCKER_TESTS": "1",
                "RUN_GBRAIN_DREAM_RECOVERY": "1",
                GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV: "0" * 40,
            },
            clear=True,
        ):
            with self._docker_available_patch(True):
                self.assertTrue(_dream_recovery_enabled())

    def test_candidate_ref_is_validated_fail_closed(self) -> None:
        """The candidate ref must be validated as an exact 40-hex SHA
        (reusing the conformance support machinery) BEFORE any Docker
        invocation; empty and malformed values fail closed."""
        with mock.patch.dict(
            os.environ,
            {GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV: ""},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                _dream_recovery_candidate_ref()
        with mock.patch.dict(
            os.environ,
            {GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV: "not-a-sha"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                _dream_recovery_candidate_ref()
        with mock.patch.dict(
            os.environ,
            {GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV: "A" * 40},
            clear=True,
        ):
            self.assertEqual(_dream_recovery_candidate_ref(), "a" * 40)

    def test_runtime_class_is_gated_on_all_conditions(self) -> None:
        text = self._module_text()
        self.assertIn("RUN_DOCKER_TESTS", text)
        self.assertIn("RUN_GBRAIN_DREAM_RECOVERY", text)
        self.assertIn(GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV, text)
        self.assertIn("skipUnless", text)
        self.assertIn(
            '"set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_DREAM_RECOVERY=1 with a docker CLI"',
            text,
        )

    # --- exact synthesize invocation --------------------------------------

    def test_exact_synthesize_invocation(self) -> None:
        """The gate must run EXACTLY ``gbrain dream --phase synthesize
        --json`` (native under the lock) and never the public adapter."""
        runtime_class = self._runtime_class_text()
        self.assertIn('"dream", "--phase", "synthesize", "--json"', runtime_class)
        self.assertIn("PRIVATE_NATIVE, *args", runtime_class)
        self.assertNotIn('"gbrain", "dream"', runtime_class)
        self.assertNotIn("gbrain-chat-run", runtime_class)

    # --- locked native execution ------------------------------------------

    def test_locked_native_execution_as_hermes(self) -> None:
        """Every native invocation must run as the hermes runtime user under
        the shared TaskNotes lock via the lock runner (issue #110); the
        public adapter must never be used for operator-only dream/jobs."""
        runtime_class = self._runtime_class_text()
        self.assertIn("LOCK_RUNNER", runtime_class)
        self.assertIn("PRIVATE_NATIVE", runtime_class)
        self.assertIn("tasknotes_lock_run.py", LOCK_RUNNER)
        self.assertIn("gbrain-native", PRIVATE_NATIVE)
        self.assertIn("--lock-path", runtime_class)
        self.assertIn("LOCK_PATH", runtime_class)
        self.assertIn("--lock-timeout", runtime_class)
        self.assertIn("run_as_hermes", runtime_class)
        self.assertIn('"su", "-s", "/bin/sh", "hermes"', runtime_class)
        self.assertNotIn("run_in_container", runtime_class)

    def test_provider_override_uses_anthropic_base_url(self) -> None:
        """The provider override must use the supported ANTHROPIC_BASE_URL
        env + a NON-SECRET fake key (never a real credential literal)."""
        runtime_class = self._runtime_class_text()
        module_text = self._module_text()
        self.assertIn("ANTHROPIC_BASE_URL=", runtime_class)
        self.assertIn("ANTHROPIC_API_KEY=", runtime_class)
        self.assertIn("FAKE_ANTHROPIC_API_KEY", runtime_class)
        self.assertIn(FAKE_ANTHROPIC_API_KEY, module_text)
        self.assertIn("127.0.0.1", DREAM_MOCK_BASE_URL)
        self.assertNotIn("litellm", runtime_class)

    def test_no_public_adapter_for_dream(self) -> None:
        runtime_class = self._runtime_class_text()
        self.assertNotIn('"gbrain", "dream"', runtime_class)
        self.assertNotIn('"gbrain", "config"', runtime_class)
        self.assertNotIn('"gbrain", "jobs"', runtime_class)

    # --- no direct DB writes ----------------------------------------------

    def test_no_direct_pglite_writes(self) -> None:
        """The runtime must never write the PGLite database directly: all
        writes go through gbrain commands (native config/jobs/dream under
        the lock, public put/get reads). PGLite mentions in prose are about
        the pinned CLI's own serial inline behavior, never a test-side
        engine handle."""
        runtime_class = self._runtime_class_text()
        self.assertNotIn("sqlite", runtime_class)
        self.assertNotIn("executeRaw", runtime_class)
        self.assertNotIn("self.engine", runtime_class)
        self.assertNotIn("pglite-engine", runtime_class)
        self.assertNotIn("postgres-engine", runtime_class)

    # --- automatic recovery path (no lease wait, no cancel) ---------------

    def test_bounded_subagent_timings_below_inline_lock(self) -> None:
        """The subagent timeout (10s) must be well below the inline drain's
        30s claim lock, and the wait timeout (~8s) must bound the runs."""
        self.assertLess(DREAM_SUBAGENT_TIMEOUT_MS, 30000)
        self.assertLess(DREAM_SUBAGENT_WAIT_TIMEOUT_MS, DREAM_SUBAGENT_TIMEOUT_MS)
        runtime_class = self._runtime_class_text()
        module_text = self._module_text()
        self.assertIn("DREAM_SUBAGENT_TIMEOUT_MS", runtime_class)
        self.assertIn("DREAM_SUBAGENT_WAIT_TIMEOUT_MS", module_text)
        self.assertIn("dream.synthesize.subagent_timeout_ms", module_text)
        self.assertIn(str(DREAM_SUBAGENT_TIMEOUT_MS), module_text)
        self.assertIn("dream.synthesize.subagent_wait_timeout_ms", module_text)
        self.assertIn(str(DREAM_SUBAGENT_WAIT_TIMEOUT_MS), module_text)
        self.assertIn("dream.synthesize.inline_concurrency", module_text)

    def test_automatic_recovery_no_cancel_no_lease_wait(self) -> None:
        """The gate must prove #4390/v0.46.26 automatic cycle-start
        recovery: the immediate rerun observes the dead-holder cycle lock
        (``cycle_already_running``), the recovery reason comes from the
        supported ``jobs get`` surface (``private_queue_reconciled:``
        reason family), and NEVER ``jobs cancel`` / ``jobs retry`` / a
        blind 300/320s lease-wait constant."""
        runtime_class = self._runtime_class_text()
        module_text = self._module_text()
        self.assertIn("DREAM_RETRY_WALL_CLOCK_LIMIT_S", runtime_class)
        self.assertIn(
            "assertLess(\n            elapsed,\n            DREAM_RETRY_WALL_CLOCK_LIMIT_S",
            runtime_class,
        )
        self.assertIn("cycle_already_running", runtime_class)
        self.assertIn("private_queue_reconciled", runtime_class)
        self.assertIn("private_queue_lease_until", runtime_class)
        self.assertIn("_jobs_get", runtime_class)
        self.assertNotIn("DREAM_STRANDED_RETRY_WAIT_S", runtime_class)
        self.assertNotIn("remaining_wait", runtime_class)
        self.assertNotIn('"jobs", "cancel"', runtime_class)
        self.assertNotIn('"jobs", "retry"', runtime_class)
        # The gate must never claim mid-cycle live healing.
        self.assertNotIn("mid-cycle live healing", module_text.split("Honest")[0])

    def test_owner_lease_wait_is_bounded_and_observable(self) -> None:
        """The wait before the recovery rerun is the observable owner-lease
        expiry (``private_queue_lease_until`` via jobs get), bounded by
        ``DREAM_OWNER_LEASE_EXPIRY_WAIT_S`` — never a blind 300/320s
        constant and never a lease unbounded wait."""
        self.assertGreaterEqual(DREAM_OWNER_LEASE_EXPIRY_WAIT_S, 600)
        self.assertLess(DREAM_OWNER_LEASE_EXPIRY_WAIT_S, 900)
        runtime_class = self._runtime_class_text()
        self.assertIn("DREAM_OWNER_LEASE_EXPIRY_WAIT_S", runtime_class)
        self.assertIn("private_queue_lease_until", runtime_class)
        self.assertIn("_parse_utc_ms", runtime_class)

    def test_recovery_wall_clock_limit_constant(self) -> None:
        self.assertEqual(DREAM_RETRY_WALL_CLOCK_LIMIT_S, 60)
        self.assertLess(DREAM_RETRY_WALL_CLOCK_LIMIT_S, 60 * 60)
        # The recovery rerun itself (after the observable lease-expiry wait)
        # must land inside the 60s wall-clock bound.
        self.assertIn(
            "DREAM_RETRY_WALL_CLOCK_LIMIT_S", self._runtime_class_text()
        )

    # --- exact v0.46.26 candidate build -----------------------------------

    def test_exact_candidate_build(self) -> None:
        """The runtime must build the EXACT v0.46.26 candidate commit
        (--build-arg GBRAIN_REF=<ref>) and start WITHOUT an implicit
        rebuild; the ref is validated as an exact 40-hex SHA before any
        Docker invocation."""
        runtime_class = self._runtime_class_text()
        module_text = self._module_text()
        self.assertIn("build_candidate", runtime_class)
        self.assertIn('self.runtime.start("hermes", timeout=900)', runtime_class)
        self.assertIn("normalize_candidate_ref", module_text)
        self.assertIn(GBRAIN_DREAM_RECOVERY_CANDIDATE_REF_ENV, module_text)
        self.assertIn("40-hex", module_text)
        self.assertIn("GBRAIN_REF=<ref>", module_text)

    def test_claim_surface_is_v04626_automatic_recovery(self) -> None:
        """Every claim surface must name the exact v0.46.26 candidate and
        #4390 (incorporating the #4361/#4332 lifecycle) and claim exactly
        automatic PGLite Dream cycle-start recovery."""
        module_text = self._module_text()
        self.assertIn("v0.46.26", module_text)
        self.assertIn("#4390", module_text)
        self.assertIn("#4361", module_text)
        self.assertIn("#4332", module_text)
        self.assertIn("cycle-start", module_text)
        self.assertIn("private_queue_reconciled", module_text)
        self.assertIn("gbrain jobs cancel", module_text)

    # --- deterministic transcript / page contract -------------------------

    def test_transcript_is_qualifying(self) -> None:
        """The seeded transcript must qualify for the synthesize phase: at
        least the configured 100-char floor, no excluded vocabulary
        (medical/therapy), no dream_generated marker, single chunk."""
        content = DREAM_TRANSCRIPT_CONTENT
        self.assertGreaterEqual(len(content), 100)
        self.assertNotIn("medical", content.lower())
        self.assertNotIn("therapy", content.lower())
        self.assertNotIn("dream_generated", content.lower())
        self.assertLess(len(content), 100000 * 3.5)  # single chunk

    def test_page_slug_ends_with_transcript_hash_suffix(self) -> None:
        """The deterministic one-page slug must carry the CDX-9 suffix: the
        first 6 hex chars of sha256(transcript content) — the pinned
        contentHash the subagent validator enforces."""
        expected = "wiki/originals/dream-recovery-" + _dream_transcript_hash6()
        self.assertEqual(_dream_page_slug(), expected)
        self.assertTrue(_dream_page_slug().endswith(
            "-" + _dream_transcript_hash6()
        ))

    def test_oneshot_page_json_is_valid(self) -> None:
        """The mock's one-page JSON must satisfy the pinned oneshot contract:
        pages array with slug/title/type/body, skipped=false, body with a
        wikilink, slug inside the oneshot task shape."""
        payload = DREAM_PAGE_JSON
        self.assertIs(payload["skipped"], False)
        pages = payload["pages"]
        self.assertEqual(len(pages), 1)
        page = pages[0]
        self.assertEqual(page["slug"], _dream_page_slug())
        self.assertRegex(page["slug"], r"^wiki/originals/[a-z0-9/-]+$")
        self.assertTrue(page["slug"].startswith("wiki/originals/"))
        self.assertTrue(page["title"].strip())
        self.assertEqual(page["type"], "note")
        self.assertIn("[[", page["body"])
        self.assertIn("[[notes/welcome]]", page["body"])

    def test_triage_json_is_high_score(self) -> None:
        """The triage verdict must be a finite score in [0,1] above the
        configured 0.5 threshold."""
        score = DREAM_TRIAGE_JSON["score"]
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0.5)
        self.assertLessEqual(score, 1.0)

    def test_filing_rules_seeded_in_vault(self) -> None:
        runtime_class = self._runtime_class_text()
        module_text = self._module_text()
        self.assertIn("_brain-filing-rules.json", runtime_class)
        self.assertIn("DREAM_FILING_RULES_JSON", runtime_class)
        self.assertIn("dream_synthesize_paths", module_text)
        self.assertIn("wiki/originals/*", module_text)

    # --- mock contract ----------------------------------------------------

    def test_mock_binds_loopback_only_and_is_credential_free(self) -> None:
        """The mock must bind 127.0.0.1 only and carry no credential
        material or external host."""
        text = self._fixture_text()
        self.assertIn('("127.0.0.1", port)', text)
        self.assertNotIn("0.0.0.0", text)
        for secret in (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "api_key",
            "Authorization", "Bearer", "sk-ant-",
        ):
            self.assertNotIn(secret, text)
        self.assertNotIn("https://", text)

    def test_mock_serves_anthropic_messages_api(self) -> None:
        """The mock must answer the Anthropic Messages API: /v1/messages
        POSTs returning the message envelope with content blocks and an
        end_turn stop_reason (the gateway's anthropic recipe parses that)."""
        text = self._fixture_text()
        self.assertIn("/v1/messages", text)
        self.assertIn('"type": "message"', text)
        self.assertIn('"content": [{"type": "text", "text": text}]', text)
        self.assertIn('"stop_reason": "end_turn"', text)
        self.assertIn('"role": "assistant"', text)
        self.assertIn("usage", text)

    def test_mock_delays_first_synthesis_and_marks_claim(self) -> None:
        """The FIRST synthesis request must wait DELAY_MS and write the
        first-seen marker so the test can SIGKILL the parent after child
        claim; later synthesis requests answer immediately."""
        text = self._fixture_text()
        self.assertIn("synth_calls", text)
        self.assertIn("time.sleep(delay_ms / 1000.0)", text)
        self.assertIn("marker_path", text)
        self.assertIn("first-synthesis-request", text)
        self.assertIn('"triage" in model', text)

    def test_mock_logs_every_request(self) -> None:
        text = self._fixture_text()
        self.assertIn("log_path", text)
        self.assertIn('"phase":', text)
        self.assertIn('"seq":', text)

    # --- report + cleanup -------------------------------------------------

    def test_cleanup_is_unconditional_down_v(self) -> None:
        text = self._module_text()
        self.assertIn("self.runtime.cleanup()", text)
        self.assertIn("down -v --remove-orphans", text)

    def test_report_uses_support_without_env_dump(self) -> None:
        text = self._module_text()
        self.assertIn("write_report", text)
        self.assertIn("conformance_report_dir", text)
        report_method = text.split("def _write_report", 1)[1]
        report_method = report_method.split("class ", 1)[0]
        self.assertNotIn("os.environ", report_method)

    def test_matrix_covers_all_owned_operations(self) -> None:
        self.assertEqual(
            set(DREAM_RECOVERY_MATRIX),
            {
                "baseline_seed",
                "baseline_build_start",
                "baseline_writable",
                "baseline_credentials",
                "baseline_jobs",
                "baseline_vault",
                "reindex",
                "mock_health",
                "dream_config",
                "corpus_seed",
                "dream_interrupt_kill",
                "lock_released",
                "no_live_process",
                "jobs_state_stranded",
                "recovery_immediate_refused",
                "recovery_private_queue_reconciled",
                "recover_automatic_completion",
                "page_written",
                "mock_called",
            },
        )


if __name__ == "__main__":
    unittest.main()
