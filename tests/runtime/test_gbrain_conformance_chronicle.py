"""Opt-in provider-gated gbrain Chronicle conformance (issue #127 W5).

Runs the REAL Chronicle event-extraction lifecycle against the pinned gbrain
build (``GBRAIN_REF`` in ``Dockerfile.hermes``) inside the disposable
conformance runtime, with NO external LLM/provider credentials and NO
external network: the LLM judge is served by a synthetic, credential-free,
OpenAI-compatible LiteLLM mock HTTP server started as the ``hermes`` runtime
user INSIDE the Hermes container (``127.0.0.1`` only), returning a
deterministic JSON event reply. Scope:

  - baseline isolation contract (empty credentials, disabled owned jobs,
    synthetic vault) with the plain Hermes-only runtime (no overlays)
  - minimal core activation (``josemar-gbrain reindex``)
  - in-container mock lifecycle: script written as hermes, server started
    detached as hermes, health probed over ``127.0.0.1``, request log kept
    for evidence, mock killed before teardown
  - provider configuration through the SAFE INTERNAL/OPERATOR path only:
    the native binary (``/opt/josemar/libexec/gbrain-native``) run as hermes
    under the shared TaskNotes lock via the lock runner — the public adapter
    rejects ``config``/``jobs`` as operator-only and is never used for them
    (``chat_model`` = ``litellm:conformance-mock``, ``provider_base_urls.
    litellm`` = the in-container mock URL; the litellm recipe needs no API
    key, so the mock is credential-free)
  - a synthetic qualifying meeting page (``meetings/<date>-conformance``,
    ``type: meeting``, ``date`` + ``attendees`` frontmatter, body >= 80
    chars) created through the public ``put`` write path
  - the real ``chronicle_extract`` job submitted through the real inline
    job path (``jobs submit chronicle_extract --follow``, native under the
    lock): the judge calls the mock and at least one deterministic event is
    written (event page + timeline projection)
  - semantic read behavior on a deterministic date/entity: ``timeline``,
    ``day``, ``day --week``, ``since``, ``last-seen``, ``on-this-day``
    (empty: no synthetic prior-year events), ``orient``
  - lock/non-root/no-direct-PGLite-write constraints: every native
    invocation runs as hermes under the shared lock; all writes go through
    gbrain commands; the mock only writes its own request log
  - a synthetic report under ``dump_folder/gbrain-conformance`` with
    command/result metadata only (never environment dumps)

Runtime execution is gated strictly on ``RUN_DOCKER_TESTS=1`` AND
``RUN_GBRAIN_CHRONICLE_CONFORMANCE=1`` and skips when the docker CLI is
absent. Fast host-side gate/mock/safety structure tests in this module
always run and need no Docker.
"""

from __future__ import annotations

from datetime import datetime
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
    write_report,
)
from .helpers import REPO_ROOT, docker_available


# Fixed deployment paths (same constants the core conformance module asserts).
PRIVATE_NATIVE = "/opt/josemar/libexec/gbrain-native"
LOCK_RUNNER = "/opt/josemar/scripts/tasknotes_lock_run.py"
LOCK_PATH = "/opt/data/.locks/tasknotes.lock"
PYTHON_BIN = "/opt/hermes/.venv/bin/python3"

# In-container mock facts: loopback-only port, script/log/pid paths under the
# hermes-writable /opt/data surface.
CHRONICLE_MOCK_PORT = 8765
CHRONICLE_MOCK_SCRIPT = "/opt/data/chronicle-mock-server.py"
CHRONICLE_MOCK_LOG = "/opt/data/chronicle-mock-requests.log"
CHRONICLE_MOCK_PID = "/opt/data/chronicle-mock.pid"
CHRONICLE_MOCK_MODEL = "conformance-mock"
CHRONICLE_MOCK_BASE_URL = "http://127.0.0.1:" + str(CHRONICLE_MOCK_PORT) + "/v1"

# Deterministic synthetic entity + event facts (the synthetic vault's person
# page and a fixed one-clause summary the mock judge returns).
CHRONICLE_ENTITY = "people/alice"
CHRONICLE_EVENT_WHAT = "Conformance meeting decision: adopt the synthetic plan"
CHRONICLE_EVENT_KIND = "decision"

# The synthetic qualifying meeting page body (>= 80 chars for chronicle
# eligibility). The frontmatter date is interpolated at runtime.
CHRONICLE_MEETING_BODY = (
    "# Conformance Meeting\n"
    "\n"
    "A deterministic synthetic meeting page for the provider-gated chronicle\n"
    "conformance gate. This meeting discusses the synthetic plan and reaches\n"
    "a decision that the mock judge extracts into a timeline event.\n"
)


def _mock_events_for(date: str) -> list[dict]:
    """The deterministic synthetic event set the mock judge returns for the
    given date. Mirrors the pinned CLI's ``ChronicleEventProposal`` shape
    (when/who/what/where/kind) so the host-side safety test can validate it
    against the same PARSE BARRIER rules without Docker."""
    return [
        {
            "when": date,
            "who": [CHRONICLE_ENTITY],
            "what": CHRONICLE_EVENT_WHAT,
            "where": None,
            "kind": CHRONICLE_EVENT_KIND,
        }
    ]


# The in-container mock server source. ``__EVENTS_JSON__`` is replaced with
# the deterministic events payload at write time. Binds 127.0.0.1 only; no
# credentials, no external network; every request is logged for evidence.
MOCK_SERVER_SCRIPT = '''#!/usr/bin/env python3
"""Synthetic credential-free OpenAI-compatible LiteLLM mock (issue #127
Chronicle gate). Serves a deterministic chat completion carrying the fixed
synthetic event set; logs every request. Binds 127.0.0.1 only; no
credentials, no external network."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
LOG_PATH = sys.argv[2]
PID_PATH = sys.argv[3]
EVENTS = __EVENTS_JSON__


class Handler(BaseHTTPRequestHandler):
    def _log(self, body):
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"path": self.path, "body": body.decode("utf-8", "replace")}) + "\\n")
        except OSError:
            pass

    def do_GET(self):
        self._log(b"")
        self._respond({"object": "list", "data": [{"id": "conformance-mock", "object": "model"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self._log(body)
        self._respond({
            "id": "chatcmpl-conformance-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "conformance-mock",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(EVENTS)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def _respond(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


with open(PID_PATH, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''

# W5 conformance matrix: every operation this increment owns, with its
# classification. The report persists an explicit result for each.
CHRONICLE_CONFORMANCE_MATRIX = {
    "baseline_seed": "core",
    "baseline_build_start": "core",
    "baseline_writable": "core",
    "baseline_credentials": "core",
    "baseline_jobs": "core",
    "baseline_vault": "core",
    "reindex": "operator_only",
    "mock_health": "core",
    "provider_config": "operator_only",
    "meeting_create": "core",
    "chronicle_extract": "provider_gated",
    "mock_called": "provider_gated",
    "timeline": "chronicle_read",
    "day": "chronicle_read",
    "day_week": "chronicle_read",
    "since": "chronicle_read",
    "last_seen": "chronicle_read",
    "on_this_day": "chronicle_read",
    "orient": "chronicle_read",
}


def _chronicle_conformance_enabled() -> bool:
    """Strict gate: RUN_DOCKER_TESTS=1 AND RUN_GBRAIN_CHRONICLE_CONFORMANCE=1
    AND a docker CLI is available."""
    return (
        os.getenv("RUN_DOCKER_TESTS") == "1"
        and os.getenv("RUN_GBRAIN_CHRONICLE_CONFORMANCE") == "1"
        and docker_available()
    )


@unittest.skipUnless(
    _chronicle_conformance_enabled(),
    "set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_CHRONICLE_CONFORMANCE=1 with a docker CLI",
)
class GbrainChronicleConformanceTestCase(unittest.TestCase):
    """Shared base setup for the provider-gated Chronicle conformance suite.

    Builds/starts the baseline Hermes-only runtime against a disposable
    Compose project (no overlays), seeds the real template source state
    BEFORE start, waits for the hermes-writable surface, asserts the
    isolation safety contract (empty credentials, disabled owned jobs),
    initializes the synthetic vault as the hermes runtime user, and
    unconditionally tears the project down with ``down -v --remove-orphans``.
    """

    def setUp(self) -> None:
        self._evidence: list[CommandEvidence] = []
        self._matrix: dict[str, str] = {
            op: "pass" if op.startswith("baseline_") else "not_run"
            for op in CHRONICLE_CONFORMANCE_MATRIX
        }
        self._gbrain_version: str | None = None
        self._report_path: Path | None = None
        # Deterministic per-run event date: the CONTAINER's local date (the
        # same calendar the database's ``current_date``-based reads see), so
        # the semantic reads are robust across timezone edges.
        self._event_date: str = ""
        self._meeting_slug: str = ""

        self.runtime = GbrainConformanceRuntime()
        # Pre-start source state seeding: real template .sync-manifest +
        # canonical josemar schema pack into the disposable source-agent-state.
        self.runtime.seed_source_state()
        # Baseline Hermes-only build/start (no candidate ref, no sidecars).
        self.runtime.up("hermes", timeout=900)
        # Wait for the exact hermes-writable surface before any exec probe.
        self.runtime.wait_until_hermes_writable(timeout=120)
        # Safety checks: empty credentials + disabled owned jobs.
        self._evidence.append(self._assert_no_credentials())
        self._evidence.append(self.runtime.assert_owned_jobs_disabled())
        # Synthetic vault init committed as the hermes runtime user.
        self._evidence.append(self.runtime.init_synthetic_vault())
        # Deterministic event date from the container's own calendar.
        date_ev = self.runtime.run_as_hermes("date", "+%F")
        self.assertEqual(date_ev.returncode, 0, date_ev.stderr)
        self._event_date = date_ev.stdout.strip()
        self.assertRegex(self._event_date, r"^\d{4}-\d{2}-\d{2}$")
        self._meeting_slug = "meetings/" + self._event_date + "-conformance"

    def tearDown(self) -> None:
        # Unconditional final cleanup: down -v --remove-orphans.
        self.runtime.cleanup()

    # --- safety helpers ---------------------------------------------------

    def _assert_no_credentials(self) -> CommandEvidence:
        """Assert every conformance-blanked credential env key is empty inside
        the running container (defense in depth on top of the host-side
        sanitizer)."""
        script = (
            "set -eu\n"
            "for k in " + " ".join(CONFORMANCE_EMPTY_ENV_KEYS) + "; do\n"
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

    def _run_under_lock(self, *command: str, timeout: int = 180) -> CommandEvidence:
        """Run an arbitrary command as the hermes runtime user under the
        shared TaskNotes lock via the lock runner — the same safe
        internal/operator mechanism the operator wrapper and the public
        adapter use. The canonical gbrain env is exported explicitly (never
        caller-controlled)."""
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
            *command,
            timeout=timeout + 60,
        )

    def _run_native_under_lock(self, *args: str, timeout: int = 180) -> CommandEvidence:
        """Run a native gbrain command as the hermes runtime user under the
        shared TaskNotes lock. The public adapter is deliberately NOT used
        here: it rejects ``config``/``jobs`` as operator-only."""
        return self._run_under_lock(PRIVATE_NATIVE, *args, timeout=timeout)

    def _set_provider_file_plane(self) -> CommandEvidence:
        """Persist the litellm provider in the FILE plane (``config.json``)
        as hermes under the lock — the supported operator surface the
        gateway actually reads. CLI invocations build the gateway from
        ``loadConfig()`` (file/env plane only): ``gbrain config set`` writes
        the DB plane, which the gateway never reads for ``chat_model`` or
        ``provider_base_urls`` (a known silent no-op), so the file plane is
        the correct operator path. Uses the same atomic temp+rename,
        fail-closed pattern the operator wrapper uses for its own file-plane
        writes."""
        snippet = (
            "import json, os, tempfile\n"
            "path = \"/opt/data/.gbrain/config.json\"\n"
            "cfg = {}\n"
            "if os.path.exists(path):\n"
            "    with open(path, encoding=\"utf-8\") as fh:\n"
            "        cfg = json.load(fh)\n"
            "cfg[\"chat_model\"] = \"litellm:" + CHRONICLE_MOCK_MODEL + "\"\n"
            "cfg.setdefault(\"provider_base_urls\", {})[\"litellm\"] = "
            "\"" + CHRONICLE_MOCK_BASE_URL + "\"\n"
            "fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), "
            "prefix=\"config.json.\", suffix=\".tmp\")\n"
            "with os.fdopen(fd, \"w\", encoding=\"utf-8\") as fh:\n"
            "    json.dump(cfg, fh, indent=2)\n"
            "    fh.write(\"\\n\")\n"
            "os.replace(tmp, path)\n"
        )
        return self._run_under_lock(PYTHON_BIN, "-c", snippet)

    # --- in-container mock lifecycle --------------------------------------

    def _start_mock(self) -> None:
        """Write the mock server script as hermes, start it detached as
        hermes inside the container, and wait until it serves health over
        loopback."""
        cid = self.runtime.run("ps", "-q", "hermes").stdout.strip()
        self.assertTrue(cid, "hermes container id must resolve")
        # repr() (not json.dumps): the payload is embedded as a Python
        # literal in the mock source, so None must render as None, not null.
        script = MOCK_SERVER_SCRIPT.replace(
            "__EVENTS_JSON__", repr(_mock_events_for(self._event_date))
        )
        write = self.runtime.run_as_hermes(
            "sh", "-lc",
            "mkdir -p /opt/data && cat > " + CHRONICLE_MOCK_SCRIPT + " <<'PY'\n"
            + script + "PY\n",
        )
        self.assertEqual(write.returncode, 0, write.stderr)
        self._evidence.append(write)
        proc = subprocess.run(
            [
                "docker", "exec", "-d", cid,
                "su", "-s", "/bin/sh", "hermes", "-c",
                "python3 " + CHRONICLE_MOCK_SCRIPT + " "
                + str(CHRONICLE_MOCK_PORT) + " " + CHRONICLE_MOCK_LOG + " "
                + CHRONICLE_MOCK_PID,
            ],
            capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = self.runtime.run_as_hermes(
                "curl", "-fsS",
                "http://127.0.0.1:" + str(CHRONICLE_MOCK_PORT) + "/v1/models",
                check=False, timeout=30,
            )
            if probe.returncode == 0:
                self._evidence.append(probe)
                return
            time.sleep(0.5)
        raise AssertionError("chronicle mock did not become healthy in time")

    def _stop_mock(self) -> None:
        """Kill the in-container mock (releasing its port) as hermes."""
        script = (
            "set -eu\n"
            "pid=$(cat " + CHRONICLE_MOCK_PID + " 2>/dev/null || true)\n"
            "if [ -n \"$pid\" ]; then kill \"$pid\" 2>/dev/null || true; fi\n"
        )
        self.runtime.run_as_hermes("sh", "-lc", script, check=False, timeout=30)

    # --- report -----------------------------------------------------------

    def _write_report(self) -> None:
        """Persist the synthetic conformance report under
        ``dump_folder/gbrain-conformance``. Contains command/result metadata
        only (argv, rc, stdout, stderr, elapsed) plus the explicit matrix —
        never the process or runtime environment."""
        metadata = {
            "baseline_ref": self.runtime.baseline_gbrain_ref(),
            "gbrain_version": self._gbrain_version,
            "matrix": self._matrix,
        }
        self._report_path = write_report(
            conformance_report_dir(),
            "gbrain-conformance-chronicle",
            self._evidence,
            metadata=metadata,
        )


class GbrainChronicleConformanceRuntimeTests(GbrainChronicleConformanceTestCase):
    """W5 provider-gated Chronicle lifecycle scenarios (Docker-gated via the
    base class)."""

    def test_chronicle_provider_gated_conformance(self) -> None:
        try:
            self._scenario_reindex()
            self._start_mock()
            try:
                self._scenario_mock_health()
                self._scenario_provider_config()
                self._scenario_meeting_create()
                self._scenario_chronicle_extract()
                self._scenario_semantic_reads()
                self._scenario_mock_evidence()
            finally:
                self._stop_mock()
        finally:
            self._write_report()

    def _scenario_reindex(self) -> None:
        """Operator activation returns the success JSON envelope; the runtime
        gbrain version is captured for the report."""
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

    def _scenario_mock_health(self) -> None:
        """The in-container mock serves the OpenAI-compatible models surface
        over loopback (the same endpoint the judge's chat call will hit)."""
        self._matrix["mock_health"] = "fail"
        ev = self.runtime.run_as_hermes(
            "curl", "-fsS",
            "http://127.0.0.1:" + str(CHRONICLE_MOCK_PORT) + "/v1/models",
            timeout=60,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self._matrix["mock_health"] = "pass"

    def _scenario_provider_config(self) -> None:
        """Configure the actual supported litellm chat provider through the
        safe internal/operator path (native binary as hermes under the shared
        lock): ``chat_model`` = ``litellm:conformance-mock`` (file plane) and
        ``provider_base_urls.litellm`` = the in-container mock URL. The
        litellm recipe declares no required auth, so the mock is
        credential-free."""
        self._matrix["provider_config"] = "fail"
        # The gateway is built from the FILE plane (loadConfig) for CLI
        # invocations, so both keys are persisted there atomically as hermes
        # under the lock (the DB-plane `config set` is a silent no-op for
        # these keys).
        ev = self._set_provider_file_plane()
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        # File-plane proof: both keys persisted for every later invocation.
        cfg = self.runtime.run_as_hermes("cat", "/opt/data/.gbrain/config.json")
        self.assertEqual(cfg.returncode, 0, cfg.stderr)
        self._evidence.append(cfg)
        data = json.loads(cfg.stdout)
        self.assertEqual(data.get("chat_model"), "litellm:" + CHRONICLE_MOCK_MODEL)
        self.assertEqual(
            data.get("provider_base_urls", {}).get("litellm"),
            CHRONICLE_MOCK_BASE_URL,
        )
        self._matrix["provider_config"] = "pass"

    def _scenario_meeting_create(self) -> None:
        """Create the synthetic qualifying meeting page through the public
        ``put`` write path: ``type: meeting`` + ``date`` + ``attendees``
        frontmatter and a body >= 80 chars (chronicle eligibility)."""
        self._matrix["meeting_create"] = "fail"
        content = (
            "---\n"
            "type: meeting\n"
            "date: " + self._event_date + "\n"
            "attendees: [" + CHRONICLE_ENTITY + "]\n"
            "---\n"
            "\n"
            + CHRONICLE_MEETING_BODY
        )
        self.assertGreaterEqual(len(CHRONICLE_MEETING_BODY), 80)
        ev = self.runtime.run_as_hermes(
            "gbrain", "put", self._meeting_slug, "--content", content,
            timeout=120,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self._matrix["meeting_create"] = "pass"

    def _scenario_chronicle_extract(self) -> None:
        """Submit the real ``chronicle_extract`` job through the real inline
        job path (``jobs submit chronicle_extract --follow``, native under
        the lock): the judge calls the in-container mock and at least one
        deterministic event is written."""
        self._matrix["chronicle_extract"] = "fail"
        params = json.dumps({"slug": self._meeting_slug})
        ev = self._run_native_under_lock(
            "jobs", "submit", "chronicle_extract",
            "--params", params, "--follow",
            timeout=300,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self.assertIn('"status":"extracted"', ev.stdout)
        self.assertIn('"events_written":1', ev.stdout)
        self._matrix["chronicle_extract"] = "pass"

    def _scenario_semantic_reads(self) -> None:
        """Semantic read behavior on the deterministic date/entity: the
        projected event is visible through timeline/day/day --week/since/
        last-seen/orient, while on-this-day stays empty (no synthetic
        prior-year events)."""
        date = self._event_date
        meeting_slug = self._meeting_slug

        # timeline: the meeting page's own timeline shows the projected event.
        self._matrix["timeline"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "timeline", meeting_slug)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn(CHRONICLE_EVENT_WHAT, ev.stdout)
        self.assertIn(date, ev.stdout)
        self._evidence.append(ev)
        self._matrix["timeline"] = "pass"

        # day / day --week / since: the event row is returned.
        for key, command in (
            ("day", ("gbrain", "day", date)),
            ("day_week", ("gbrain", "day", date, "--week")),
            ("since", ("gbrain", "since", date)),
        ):
            self._matrix[key] = "fail"
            ev = self.runtime.run_as_hermes(*command)
            self.assertEqual(ev.returncode, 0, ev.stderr)
            self._evidence.append(ev)
            rows = json.loads(ev.stdout)
            self.assertTrue(rows, f"{command} must return the event")
            row = rows[0]
            self.assertEqual(row.get("summary"), CHRONICLE_EVENT_WHAT)
            self.assertEqual(row.get("page_slug"), meeting_slug)
            self.assertTrue(
                str(row.get("event_slug", "")).startswith("life/events/" + date + "-"),
                f"unexpected event_slug: {row.get('event_slug')!r}",
            )
            self._matrix[key] = "pass"

        # last-seen: the synthetic entity was seen at the event.
        self._matrix["last_seen"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "last-seen", CHRONICLE_ENTITY)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        last_seen = json.loads(ev.stdout)
        self.assertEqual(last_seen.get("last_date"), date)
        self.assertTrue(
            str(last_seen.get("last_event_slug", "")).startswith("life/events/"),
            f"unexpected last_event_slug: {last_seen.get('last_event_slug')!r}",
        )
        self._matrix["last_seen"] = "pass"

        # on-this-day: prior years only — no synthetic prior-year events.
        self._matrix["on_this_day"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "on-this-day")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        self.assertEqual(json.loads(ev.stdout), [])
        self._matrix["on_this_day"] = "pass"

        # orient: the recent timeline includes the event.
        self._matrix["orient"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "orient")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        orient = json.loads(ev.stdout)
        recent = orient.get("recent_timeline", [])
        self.assertTrue(recent, "orient must include the event in the recent timeline")
        self.assertTrue(
            str(recent[0].get("event_slug", "")).startswith("life/events/" + date + "-"),
            f"unexpected recent event_slug: {recent[0].get('event_slug')!r}",
        )
        self._matrix["orient"] = "pass"

    def _scenario_mock_evidence(self) -> None:
        """The judge must have actually called the in-container mock (real
        LLM path, no shortcut, no external network): the request log records
        at least one POST to the chat completions endpoint."""
        self._matrix["mock_called"] = "fail"
        ev = self.runtime.run_as_hermes("cat", CHRONICLE_MOCK_LOG, timeout=60)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        posts = [ln for ln in ev.stdout.splitlines() if "chat/completions" in ln]
        self.assertTrue(posts, "the mock must have received a chat completion request")
        self._matrix["mock_called"] = "pass"


class GbrainChronicleConformanceGateStructureTests(unittest.TestCase):
    """Fast host-side guards for the provider-gated Chronicle gate, the mock
    response safety, and the lock/non-root/no-direct-write constraints. No
    Docker required; these run in the normal fast suite."""

    @staticmethod
    def _module_text() -> str:
        return (
            REPO_ROOT / "tests" / "runtime" / "test_gbrain_conformance_chronicle.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _runtime_class_text() -> str:
        """The runtime portion of the module: the shared base case class plus
        the Docker-gated scenario class (everything before the fast gate
        structure tests)."""
        text = GbrainChronicleConformanceGateStructureTests._module_text()
        runtime_class = text.split("class GbrainChronicleConformanceTestCase", 1)[1]
        return runtime_class.split("class GbrainChronicleConformanceGateStructureTests", 1)[0]

    @staticmethod
    def _docker_available_patch(available: bool):
        """Patch this module's own ``docker_available`` reference.

        Patching the module attribute directly (rather than a dotted import
        path) is robust against double-import under ``discover -s tests``,
        where the module can be imported as both ``runtime.…`` and
        ``tests.runtime.…`` and a dotted target would patch the wrong copy.
        """
        return mock.patch.object(
            sys.modules[__name__], "docker_available", return_value=available
        )

    def test_gate_requires_run_docker_tests(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "", "RUN_GBRAIN_CHRONICLE_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_chronicle_conformance_enabled())

    def test_gate_requires_run_gbrain_chronicle_conformance(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_CHRONICLE_CONFORMANCE": ""}
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_chronicle_conformance_enabled())

    def test_gate_requires_docker(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_CHRONICLE_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(False):
                self.assertFalse(_chronicle_conformance_enabled())

    def test_gate_enabled_when_all_conditions_met(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_CHRONICLE_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(True):
                self.assertTrue(_chronicle_conformance_enabled())

    def test_runtime_class_is_gated_on_both_env_vars(self) -> None:
        text = self._module_text()
        self.assertIn("RUN_DOCKER_TESTS", text)
        self.assertIn("RUN_GBRAIN_CHRONICLE_CONFORMANCE", text)
        self.assertIn("skipUnless", text)
        self.assertIn(
            '"set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_CHRONICLE_CONFORMANCE=1 with a docker CLI"',
            text,
        )

    # --- mock response safety --------------------------------------------

    def test_mock_response_is_deterministic_valid_proposal(self) -> None:
        """The deterministic mock reply must be a valid ChronicleEventProposal
        under the pinned CLI's PARSE BARRIER rules (parseable when, non-empty
        what, who array of strings, kind string)."""
        events = _mock_events_for("2026-08-21")
        self.assertEqual(len(events), 1)
        proposal = events[0]
        self.assertIsInstance(proposal.get("when"), str)
        self.assertRegex(proposal["when"], r"^\d{4}-\d{2}-\d{2}$")
        datetime.fromisoformat(proposal["when"])  # must not raise
        self.assertIsInstance(proposal.get("what"), str)
        self.assertTrue(proposal["what"].strip())
        self.assertIsInstance(proposal.get("who"), list)
        self.assertTrue(all(isinstance(w, str) for w in proposal["who"]))
        self.assertIsInstance(proposal.get("kind"), str)
        self.assertIn(proposal.get("where"), (None, ""))
        # The event set is deterministic: same date -> same payload.
        self.assertEqual(_mock_events_for("2026-08-21"), events)

    def test_mock_binds_loopback_only_and_is_credential_free(self) -> None:
        """The mock must bind 127.0.0.1 only and carry no credential material
        or external host: no API keys, no provider env names, no outbound
        URL."""
        text = MOCK_SERVER_SCRIPT
        self.assertIn('("127.0.0.1", PORT)', text)
        self.assertNotIn("0.0.0.0", text)
        for secret in (
            "OPENAI_API_KEY", "LITELLM_API_KEY", "ANTHROPIC_API_KEY",
            "api_key", "Authorization", "Bearer",
        ):
            self.assertNotIn(secret, text)
        self.assertNotIn("http://", text)

    def test_mock_reply_is_openai_chat_completion_shape(self) -> None:
        """The mock's POST reply must be an OpenAI chat completion carrying
        the deterministic events as the assistant message content with a
        stop finish reason (the judge parses that content as a JSON array)."""
        text = MOCK_SERVER_SCRIPT
        self.assertIn('"object": "chat.completion"', text)
        self.assertIn('"choices"', text)
        self.assertIn('"message"', text)
        self.assertIn('"content"', text)
        self.assertIn('"finish_reason"', text)
        self.assertIn('"stop"', text)
        self.assertIn("json.dumps(EVENTS)", text)

    # --- safe internal/operator path + lock/non-root constraints ----------

    def test_provider_configured_via_native_operator_path_under_lock(self) -> None:
        """The provider must be configured through the safe internal/operator
        path: the native binary run as hermes under the shared TaskNotes lock
        via the lock runner. The public adapter must never be used for
        operator-only config."""
        runtime_class = self._runtime_class_text()
        # The runtime drives the native binary through the lock runner
        # constants; the constants themselves must point at the fixed
        # deployment paths.
        self.assertIn("LOCK_RUNNER", runtime_class)
        self.assertIn("PRIVATE_NATIVE", runtime_class)
        self.assertIn("tasknotes_lock_run.py", LOCK_RUNNER)
        self.assertIn("gbrain-native", PRIVATE_NATIVE)
        self.assertIn("--lock-path", runtime_class)
        # Both gateway keys (chat_model + provider_base_urls.litellm) are
        # persisted in the FILE plane (config.json) under the lock — the
        # gateway is built from loadConfig() for CLI invocations, so the
        # DB-plane `config set` would be a silent no-op for them.
        self.assertIn("_set_provider_file_plane", runtime_class)
        self.assertIn("config.json", runtime_class)
        self.assertIn("provider_base_urls", runtime_class)
        self.assertIn("litellm:" + CHRONICLE_MOCK_MODEL, runtime_class)
        self.assertIn("CHRONICLE_MOCK_BASE_URL", runtime_class)
        # The base-URL constant itself must point at the loopback mock.
        self.assertIn("127.0.0.1", CHRONICLE_MOCK_BASE_URL)
        self.assertNotIn('"gbrain", "config"', runtime_class)

    def test_chronicle_extract_uses_real_inline_job_path(self) -> None:
        """The extraction must go through the real inline job path
        (``jobs submit chronicle_extract --follow``), never a shortcut."""
        runtime_class = self._runtime_class_text()
        self.assertIn('"jobs", "submit", "chronicle_extract"', runtime_class)
        self.assertIn("--follow", runtime_class)
        self.assertIn('"status":"extracted"', runtime_class)
        self.assertIn('"events_written":1', runtime_class)

    def test_operator_invocations_run_as_hermes_never_root(self) -> None:
        """Every in-container invocation must run as the hermes runtime user
        (issue #110): the native operator path uses run_as_hermes and the
        mock is started via ``su -s /bin/sh hermes``."""
        runtime_class = self._runtime_class_text()
        self.assertIn("run_as_hermes", runtime_class)
        self.assertIn('"su", "-s", "/bin/sh", "hermes"', runtime_class)
        self.assertNotIn("run_in_container", runtime_class)

    def test_no_direct_pglite_writes(self) -> None:
        """The runtime must never write the PGLite database directly: all
        writes go through gbrain commands (public put, native config/jobs
        under the lock)."""
        runtime_class = self._runtime_class_text()
        self.assertNotIn("sqlite", runtime_class)
        self.assertNotIn("pglite", runtime_class.lower())

    def test_semantic_reads_covered(self) -> None:
        """The runtime must exercise every semantic read on the deterministic
        date/entity."""
        runtime_class = self._runtime_class_text()
        for cmd in ("timeline", "day", "since", "last-seen", "on-this-day", "orient"):
            self.assertIn(f'"gbrain", "{cmd}"', runtime_class)
        self.assertIn("--week", runtime_class)
        self.assertIn("CHRONICLE_ENTITY", runtime_class)
        self.assertIn("CHRONICLE_EVENT_WHAT", runtime_class)

    def test_on_this_day_asserts_no_synthetic_prior_year_events(self) -> None:
        """on-this-day must assert an EMPTY result: the deterministic event
        is dated today, so no synthetic prior-year events may appear."""
        runtime_class = self._runtime_class_text()
        self.assertIn("json.loads(ev.stdout), []", runtime_class)

    def test_meeting_page_is_qualifying(self) -> None:
        """The synthetic meeting body must satisfy the pinned CLI's chronicle
        eligibility floor (>= 80 chars) and carry the meeting type."""
        self.assertGreaterEqual(len(CHRONICLE_MEETING_BODY), 80)
        runtime_class = self._runtime_class_text()
        self.assertIn("type: meeting", runtime_class)
        self.assertIn("attendees: [", runtime_class)

    # --- report + cleanup -------------------------------------------------

    def test_report_uses_support_without_env_dump(self) -> None:
        text = self._module_text()
        self.assertIn("write_report", text)
        self.assertIn("conformance_report_dir", text)
        # The report must never serialize the process/runtime environment:
        # the report-writing method must not reference os.environ at all.
        report_method = text.split("def _write_report", 1)[1]
        report_method = report_method.split("class ", 1)[0]
        self.assertNotIn("os.environ", report_method)

    def test_cleanup_is_unconditional_down_v(self) -> None:
        text = self._module_text()
        self.assertIn("self.runtime.cleanup()", text)
        # The base ComposeRuntime.down() runs `down -v --remove-orphans`.
        self.assertIn("down -v --remove-orphans", text)

    def test_matrix_covers_all_owned_operations(self) -> None:
        self.assertEqual(
            set(CHRONICLE_CONFORMANCE_MATRIX),
            {
                "baseline_seed",
                "baseline_build_start",
                "baseline_writable",
                "baseline_credentials",
                "baseline_jobs",
                "baseline_vault",
                "reindex",
                "mock_health",
                "provider_config",
                "meeting_create",
                "chronicle_extract",
                "mock_called",
                "timeline",
                "day",
                "day_week",
                "since",
                "last_seen",
                "on_this_day",
                "orient",
            },
        )


if __name__ == "__main__":
    unittest.main()
