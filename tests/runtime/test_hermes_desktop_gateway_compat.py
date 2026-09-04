"""Opt-in Docker runtime gate: Hermes Desktop Remote gateway compatibility
for the Hermes v0.21.0 candidate (issue #156 W3).

Skipped by default. Enable with:

  RUN_DOCKER_TESTS=1 RUN_HERMES_DESKTOP_GATEWAY_COMPAT_TESTS=1 \
  python3 -m unittest tests.runtime.test_hermes_desktop_gateway_compat -v

or:

  make test-hermes-desktop-gateway-compat

WHAT IT PROVES (each phase against the real candidate image built by this
test's own Compose project):

  1. Public status/readiness: ``/api/health`` and ``/api/status`` answer
     without any token and report the exact candidate version (0.21.0).
  2. Auth rejection: a wrong REST session token gets HTTP 401 on a gated
     endpoint, and a wrong WebSocket token gets the ``/api/ws`` upgrade
     rejected (v0.21 loopback token mode; ``HERMES_DASHBOARD_INSECURE`` is
     explicitly pinned to "0" because it no longer bypasses v0.21 auth).
  3. Auth acceptance: the valid REST ``X-Hermes-Session-Token`` header and
     the valid WS ``?token=`` query credential (with loopback Origin) are
     accepted; the WS speaks newline-delimited JSON-RPC and emits
     ``gateway.ready`` on accept.
  4. Profile surface: ``/api/profiles`` lists exactly ONE profile — the
     public display name ``Josemar`` with ``is_default`` true and the
     canonical base HERMES_HOME path — and no duplicate ``default`` row;
     REST session rows keep stamping the canonical ``default`` profile.
  5. Session lifecycle over WS: ``session.create`` returns the live
     ``session_id`` plus the durable ``stored_session_id``; the session is
     NOT durable before its first prompt (REST list must not contain it).
  6. Streamed REAL-agent turn: ``prompt.submit`` (after ``session.create``)
     with a deterministic prompt drives the normal v0.21 real-agent path —
     the session pins ``model``/``provider`` resolving to a disposable
     ``custom_providers`` OpenAI-compatible entry (target-supported safe
     seam) whose base_url is a loopback fake provider started INSIDE the
     container as ``hermes`` (no external network, no real credentials).
     The turn returns ``{"status": "streaming"}`` and emits
     ``message.start``, >= 1 streamed ``message.delta``, and the terminal
     settled ``message.complete`` with status ``complete`` and the fake's
     deterministic assistant content. Turn settlement is judged ONLY by
     ``message.complete``, never ``session.info``. The mock's request log
     proves the agent actually hit the loopback fake over
     ``chat/completions``.
  7. Durable transcript BEFORE restart: ``/api/sessions/<stored>/messages``
     contains the deterministic user turn and the deterministic assistant
     turn (the normal real-agent persistence path via the agent's
     ``_persist_session`` contract), and the session row appears in
     ``/api/sessions``.
  8. Reconnect: a fresh WS connection ``session.resume``es the session by
     its stored id and gets the transcript back (live history).
  9. Restart durability: the container is stopped and restarted while the
     disposable named volume is preserved; after the dashboard is ready
     again, the session row (id/title/profile stamp) persists, the REST
     messages endpoint STILL contains the deterministic user + assistant
     transcript, and WS ``session.resume`` returns that same history.

The fake provider and its config wiring are disposable test-local state:
the provider entry is appended to the RUNTIME config file inside the
disposable volume (never a tracked file) and re-applied after the restart
because the container init re-materializes the runtime config from the
template on every start; the fake provider script lives on the disposable
volume and is restarted after the container restart before the resume.

ISOLATION: ``ComposeRuntime`` + the test-isolation overlay replace the
repo's agent-state/credentials bind mounts with disposable empty dirs; all
dashboard credentials are synthetic (ComposeRuntime-generated); Telegram,
workspace sync, and hosted-provider credentials are blanked; the dashboard
binds container-loopback (127.0.0.1) and every protocol client and the fake
provider run INSIDE the candidate container as the ``hermes`` runtime user
using the image's own Hermes venv (which upstream's ISO harness uses for
``websockets``), so no host port is ever used and no new host-side Python
package is needed.

Cleanup is unconditional for project state: ``ComposeRuntime.down`` runs
``docker compose down -v --remove-orphans`` for the disposable project
(state, network, volumes) and removes the disposable mount dirs even when
a phase fails; the fake provider process is killed before teardown. Client
evidence logs are retained under the gitignored
``dump_folder/hermes-desktop-gateway-compat/`` (never committed).
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import shlex
import subprocess
import time
import unittest

from .helpers import ComposeRuntime, docker_available

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DUMP_DIR = os.path.join(REPO_ROOT, "dump_folder", "hermes-desktop-gateway-compat")

# ---------------------------------------------------------------------------
# Gate variables. The Docker runtime gate below is skipped unless BOTH are
# set; the fast structural suite above it always runs on default discovery.
# ---------------------------------------------------------------------------
DOCKER_GATE_ENV_VAR = "RUN_DOCKER_TESTS"
MODULE_GATE_ENV_VAR = "RUN_HERMES_DESKTOP_GATEWAY_COMPAT_TESTS"

# ---------------------------------------------------------------------------
# Pinned v0.21 token-mode protocol surface exercised by the in-container
# client (structurally asserted by the fast suite; proven live by the gate).
# ---------------------------------------------------------------------------
EXPECTED_CANDIDATE_VERSION = "0.21.0"
WS_PATH = "/api/ws"
WS_TOKEN_QUERY_PARAM = "token"
REST_SESSION_TOKEN_HEADER = "X-Hermes-Session-Token"
PUBLIC_PROFILE_DISPLAY_NAME = "Josemar"
CANONICAL_DEFAULT_PROFILE = "default"
EXPECTED_BASE_HERMES_HOME = "/opt/data"

# Host-publish port pins only (mirrors the other runtime gates so the
# disposable stack cannot collide with dev/production listeners). The
# protocol client and the fake provider are container-internal.
DASHBOARD_PORT = "19443"
API_SERVER_PORT = "18643"

SESSION_TITLE = "w3-desktop-gateway-compat"
SESSION_SOURCE = "w3-desktop-gateway-compat"

# ---------------------------------------------------------------------------
# Disposable loopback fake OpenAI-compatible provider (test-local seam).
# The provider entry is target-supported ``custom_providers`` config appended
# to the RUNTIME config inside the disposable volume; the fake serves
# deterministic content on container loopback only.
# ---------------------------------------------------------------------------
FAKE_PROVIDER_PORT = "8123"
FAKE_PROVIDER_SCRIPT_PATH = "/opt/data/w3-fake-provider.py"
FAKE_PROVIDER_LOG_PATH = "/opt/data/w3-fake-provider-requests.log"
FAKE_PROVIDER_PID_PATH = "/opt/data/w3-fake-provider.pid"
FAKE_PROVIDER_NAME = "w3fake"
FAKE_PROVIDER_MODEL = "w3-mock-model"
FAKE_PROVIDER_BASE_URL = "http://127.0.0.1:8123/v1"
FAKE_PROVIDER_API_KEY = "w3-synthetic-key"
FAKE_PROVIDER_MARKER = "W3-FAKE-PROVIDER-OK"
FAKE_PROVIDER_PROMPT = "W3 deterministic provider turn: reply with your fixed content."
RUNTIME_CONFIG_PATH = "/opt/data/config.yaml"

IN_CONTAINER_CLIENT_PATH = "/tmp/w3_compat_client.py"

CONFIG_PROVIDER_BLOCK = (
    "\n# W3 disposable test-only provider (issue #156; loopback fake, no real\n"
    "# credentials, never a tracked file — the container init re-materializes\n"
    "# the runtime config from the template on every start).\n"
    "custom_providers:\n"
    f"- name: {FAKE_PROVIDER_NAME}\n"
    f"  base_url: {FAKE_PROVIDER_BASE_URL}\n"
    f"  api_key: {FAKE_PROVIDER_API_KEY}\n"
    "  api_mode: chat_completions\n"
    f"  model: {FAKE_PROVIDER_MODEL}\n"
)

# In-container dashboard readiness probe (runs as hermes, Hermes venv).
HEALTH_PROBE_SCRIPT = (
    "/opt/hermes/.venv/bin/python3 - <<'PYEOF'\n"
    "import sys\n"
    "import urllib.request\n"
    "try:\n"
    f"    resp = urllib.request.urlopen('http://127.0.0.1:{DASHBOARD_PORT}/api/health', timeout=5)\n"
    "    body = resp.read().decode()\n"
    "    print('HEALTH-OK', resp.status, body[:200])\n"
    "except Exception as exc:\n"
    "    print('HEALTH-WAIT', exc)\n"
    "    sys.exit(1)\n"
    "PYEOF"
)

# Append the disposable custom_providers entry to the RUNTIME config
# (test-local state on the disposable volume; idempotent).
CONFIG_WRITE_SCRIPT = (
    "export HOME=/opt/data HERMES_HOME=/opt/data\n"
    "/opt/hermes/.venv/bin/python3 - <<'PYEOF'\n"
    f"path = {RUNTIME_CONFIG_PATH!r}\n"
    "text = open(path, encoding='utf-8').read()\n"
    "if 'custom_providers:' not in text:\n"
    f"    text += {CONFIG_PROVIDER_BLOCK!r}\n"
    "    open(path, 'w', encoding='utf-8').write(text)\n"
    "assert 'custom_providers:' in open(path, encoding='utf-8').read()\n"
    "print('W3-CONFIG-OK')\n"
    "PYEOF"
)

# Prove the target resolves the named custom provider to the loopback fake
# BEFORE the turn (real hermes_cli resolution path, in the candidate venv).
PROVIDER_RESOLUTION_PROBE = (
    "export HOME=/opt/data HERMES_HOME=/opt/data\n"
    "/opt/hermes/.venv/bin/python3 - <<'PYEOF'\n"
    "from hermes_cli.runtime_provider import _get_named_custom_provider\n"
    f"entry = _get_named_custom_provider({FAKE_PROVIDER_NAME!r})\n"
    f"assert entry and entry.get('base_url') == {FAKE_PROVIDER_BASE_URL!r}, entry\n"
    f"assert entry.get('api_mode') == 'chat_completions', entry\n"
    "print('W3-PROVIDER-RESOLVES', entry.get('name'), entry.get('base_url'), entry.get('api_mode'))\n"
    "PYEOF"
)

# Prove the agent actually hit the loopback fake (request log on the
# disposable volume; the log contains only request paths, stream flags, and
# POST markers — never credentials or environment).
MOCK_EVIDENCE_PROBE = (
    "test -f " + FAKE_PROVIDER_LOG_PATH + " && "
    "grep -c 'post stream=True' " + FAKE_PROVIDER_LOG_PATH + " && "
    "echo W3-MOCK-EVIDENCE-OK"
)

# Disposable loopback fake OpenAI-compatible provider (runs INSIDE the
# container as hermes; binds 127.0.0.1 only; deterministic content; no
# credentials, no external network).
W3_FAKE_PROVIDER_SCRIPT = r'''
"""W3 disposable loopback fake OpenAI-compatible provider (test-only).

Threaded server: the OpenAI SDK (httpx) holds keep-alive connections open,
which would block a single-threaded HTTPServer's accept loop forever."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
LOG_PATH = sys.argv[2]
PID_PATH = sys.argv[3]
MODEL = "w3-mock-model"
CONTENT = "W3-FAKE-PROVIDER-OK deterministic-turn"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _log(self, extra):
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"path": self.path, "extra": extra}) + "\n")
        except OSError:
            pass

    def _json_response(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._log("get")
        self._json_response({
            "object": "list",
            "data": [{"id": MODEL, "object": "model", "owned_by": "w3-test"}],
        })

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        try:
            request = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            request = {}
        wants_stream = bool(request.get("stream"))
        self._log("post stream=%s" % wants_stream)
        if not wants_stream:
            self._json_response({
                "id": "chatcmpl-w3", "object": "chat.completion", "created": 0,
                "model": MODEL,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": CONTENT},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        pieces = ["W3-FAKE-", "PROVIDER-OK ", "deterministic-turn"]
        for index, piece in enumerate(pieces):
            chunk = {
                "id": "chatcmpl-w3", "object": "chat.completion.chunk",
                "created": 0, "model": MODEL,
                "choices": [{
                    "index": 0,
                    "delta": {"content": piece},
                    "finish_reason": "stop" if index == len(pieces) - 1 else None,
                }],
            }
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, fmt, *args):
        pass


with open(PID_PATH, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))


class FakeProviderServer(ThreadingHTTPServer):
    daemon_threads = True


FakeProviderServer(("127.0.0.1", PORT), Handler).serve_forever()
'''


# ---------------------------------------------------------------------------
# The in-container protocol client. Self-contained stdlib + the Hermes venv's
# websockets package (the same interpreter upstream's ISO harness uses).
# Modes: rest-auth / ws-auth / turn / resume-live / post-restart.
# Each prints a final `W3-<MODE>-OK {json evidence}` line on success.
# ---------------------------------------------------------------------------
W3_CLIENT_SOURCE = r'''
"""W3 Desktop Remote gateway protocol client (in-container, hermes venv)."""

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request

REST_SESSION_TOKEN_HEADER = "X-Hermes-Session-Token"
WS_PATH = "/api/ws"
WS_TOKEN_QUERY_PARAM = "token"

EXPECTED_VERSION = "0.21.0"
PUBLIC_PROFILE = "Josemar"
CANONICAL_DEFAULT = "default"
BASE_HOME = "/opt/data"
SESSION_TITLE = "w3-desktop-gateway-compat"
SESSION_SOURCE = "w3-desktop-gateway-compat"
FAKE_PROVIDER_MARKER = "W3-FAKE-PROVIDER-OK"


def rest(port, path, token=None, timeout=30):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
    if token is not None:
        req.add_header(REST_SESSION_TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode() or "{}")
        except Exception:
            body = {}
        return exc.code, body


def message_text(message):
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content)


class GatewayWS:
    """Newline-delimited JSON-RPC over the candidate's /api/ws.

    Every received frame is appended to ``self.frames``; lookups scan by
    index so a turn thread racing the RPC response (message.start can be
    written before the prompt.submit response) is never lost.
    """

    def __init__(self, port, token):
        from websockets.asyncio.client import connect  # noqa: PLC0415

        self._connect_factory = connect
        self.url = "ws://127.0.0.1:%d%s?%s=%s" % (
            port, WS_PATH, WS_TOKEN_QUERY_PARAM, token,
        )
        self.origin = "http://127.0.0.1:%d" % port
        self.ws = None
        self._buf = b""
        self._id = 0
        self.frames = []

    async def connect(self, timeout=20):
        self.ws = await self._connect_factory(
            self.url,
            additional_headers={"Origin": self.origin},
            open_timeout=timeout,
            max_size=None,
        )

    async def close(self):
        if self.ws is not None:
            await self.ws.close()

    async def _pump(self, timeout):
        raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        self._buf += raw if isinstance(raw, bytes) else raw.encode()
        # The candidate sends one JSON frame per WS text message (send_text,
        # no trailing newline; newline splitting only matters if a sender
        # ever batches). Accept both framings: complete newline-terminated
        # lines, plus a trailing residue once it parses as whole JSON.
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line = self._buf[:newline].strip()
                self._buf = self._buf[newline + 1:]
                if line:
                    self.frames.append(json.loads(line.decode()))
                continue
            residue = self._buf.strip()
            if not residue:
                self._buf = b""
                return
            try:
                frame = json.loads(residue.decode())
            except (ValueError, UnicodeDecodeError):
                return  # partial frame; wait for the remainder
            self.frames.append(frame)
            self._buf = b""
            return

    async def scan(self, start, predicate, timeout):
        """Scan frames from ``start`` for ``predicate(frame)``; return
        ``(match_index, frame)``. Pumps the socket while waiting."""
        deadline = time.monotonic() + timeout
        index = start
        while True:
            while index < len(self.frames):
                frame = self.frames[index]
                if predicate(frame):
                    return index, frame
                index += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ws scan timeout after %ss" % timeout)
            await self._pump(remaining)

    async def wait_event(self, start, types, timeout):
        def pred(frame):
            if frame.get("method") != "event":
                return False
            return (not types) or (frame.get("params") or {}).get("type") in types

        _, frame = await self.scan(start, pred, timeout)
        return (frame.get("params") or {}).get("type")

    async def rpc(self, start, method, params, timeout=45):
        self._id += 1
        rid = "w3-%d" % self._id
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        ) + "\n"
        await self.ws.send(frame)

        def pred(obj):
            return obj.get("id") == rid

        _, response = await self.scan(start, pred, timeout)
        return response


async def ws_probe_reject(port, token):
    """A wrong token must not yield a working /api/ws connection."""
    bad = GatewayWS(port, token + "-deliberately-wrong")
    evidence = {"rejected": False}
    try:
        await bad.connect(timeout=15)
    except Exception as exc:
        code = None
        response = getattr(exc, "response", None)
        if response is not None:
            code = getattr(response, "status_code", None)
            if code is None:
                code = getattr(response, "status", None)
        close_frame = getattr(exc, "rcvd", None)
        if close_frame is not None:
            code = getattr(close_frame, "code", code)
        evidence = {
            "rejected": True,
            "how": type(exc).__name__,
            "code": code,
        }
        return evidence
    try:
        # Connected at TCP level: the server must still not admit the
        # client — either close (4401) or never emit gateway.ready.
        try:
            await bad.wait_event(0, ["gateway.ready"], timeout=5)
            evidence["how"] = "gateway.ready emitted on bad token"
        except TimeoutError:
            evidence["rejected"] = True
            evidence["how"] = "no gateway.ready within 5s"
        except Exception as exc:
            evidence["rejected"] = True
            evidence["how"] = "closed: %s" % type(exc).__name__
        return evidence
    finally:
        await bad.close()


def mode_rest_auth(port, token):
    evidence = {}
    status, body = rest(port, "/api/health")
    assert status == 200, ("health status", status, body)
    assert body.get("ok") is True, ("health ok", body)
    assert body.get("version") == EXPECTED_VERSION, ("health version", body)
    assert body.get("auth_required") is False, ("health auth_required", body)
    evidence["health"] = {"version": body.get("version"), "auth_required": body.get("auth_required")}

    status, body = rest(port, "/api/status")
    assert status == 200, ("status status", status)
    assert body.get("version") == EXPECTED_VERSION, ("status version", body)
    evidence["status_version"] = body.get("version")

    # Wrong REST token must be rejected on a gated endpoint.
    status, body = rest(port, "/api/sessions?limit=10", token="definitely-not-the-token")
    assert status == 401, ("bad token status", status, body)

    # Valid REST header must authenticate.
    status, body = rest(port, "/api/sessions?limit=100", token=token)
    assert status == 200, ("sessions status", status, body)
    assert isinstance(body.get("sessions"), list), ("sessions shape", body)

    status, body = rest(port, "/api/profiles", token=token)
    assert status == 200, ("profiles status", status, body)
    profiles = body.get("profiles") or []
    assert len(profiles) == 1, ("expected exactly one profile", profiles)
    base = profiles[0]
    assert base.get("name") == PUBLIC_PROFILE, ("public display name", base)
    assert base.get("is_default") is True, ("base profile default flag", base)
    assert base.get("path") == BASE_HOME, ("canonical base home", base)
    assert not any(
        p.get("name") == CANONICAL_DEFAULT for p in profiles
    ), ("duplicate canonical default row", profiles)
    evidence["profiles"] = [
        {"name": p.get("name"), "is_default": p.get("is_default"), "path": p.get("path")}
        for p in profiles
    ]

    print("W3-REST-AUTH-OK " + json.dumps(evidence))


async def mode_ws_auth(port, token):
    evidence = {}
    bad = await ws_probe_reject(port, token)
    assert bad.get("rejected") is True, ("bad WS token accepted", bad)
    evidence["bad_token"] = bad

    good = GatewayWS(port, token)
    await good.connect()
    try:
        ready_type = await good.wait_event(0, ["gateway.ready"], timeout=20)
        evidence["gateway_ready"] = ready_type
    finally:
        await good.close()
    print("W3-WS-AUTH-OK " + json.dumps(evidence))


async def mode_turn(port, token, prompt, model, provider):
    evidence = {}
    client = GatewayWS(port, token)
    await client.connect()
    try:
        ready_index, _ = await client.scan(
            0, lambda f: f.get("method") == "event", timeout=20
        )
        ready_type = (client.frames[ready_index].get("params") or {}).get("type")
        assert ready_type == "gateway.ready", ("first event", ready_type)

        created = await client.rpc(
            ready_index + 1,
            "session.create",
            {
                "cols": 80,
                "source": SESSION_SOURCE,
                "title": SESSION_TITLE,
                "model": model,
                "provider": provider,
            },
            timeout=60,
        )
        create_index, _ = await client.scan(
            ready_index + 1, lambda f: f.get("id") == created.get("id"), timeout=5
        )
        result = created.get("result") or {}
        sid = result.get("session_id")
        stored_key = result.get("stored_session_id")
        assert sid and stored_key, ("session.create", created)
        evidence["session_id"] = sid
        evidence["stored_session_id"] = stored_key

        # A created session is NOT durable before its first prompt.
        status, body = rest(port, "/api/sessions?limit=100", token=token)
        assert status == 200, ("pre-prompt sessions status", status)
        ids = [row.get("id") for row in body.get("sessions") or []]
        assert stored_key not in ids, ("row existed before first prompt", ids)
        evidence["pre_prompt_row_absent"] = True

        submitted = await client.rpc(
            create_index + 1,
            "prompt.submit",
            {"session_id": sid, "text": prompt},
            timeout=60,
        )
        submit_result = submitted.get("result") or {}
        assert submit_result.get("status") == "streaming", ("prompt.submit", submitted)
        evidence["submit_status"] = "streaming"

        # Collect this turn's streamed events. The turn thread may write
        # message.start before the prompt.submit response frame lands, so
        # scan from just after session.create's response instead of the
        # submit response; the settle judgement is the terminal
        # message.complete only (never session.info).
        scan_from = create_index + 1
        saw_start = False
        deltas = []
        complete = None
        deadline = time.monotonic() + 180

        def _is_event(frame):
            return frame.get("method") == "event"

        while complete is None and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            scan_from, frame = await client.scan(scan_from, _is_event, max(0.05, remaining))
            params = frame.get("params") or {}
            if params.get("session_id") != sid:
                scan_from += 1
                continue
            etype = params.get("type")
            scan_from += 1
            if etype == "message.start":
                saw_start = True
            elif etype == "message.delta":
                deltas.append((params.get("payload") or {}).get("text") or "")
            elif etype == "message.complete":
                complete = params.get("payload") or {}
            elif etype == "error":
                raise AssertionError("turn error event: %r" % (params.get("payload"),))
        if complete is None:
            raise TimeoutError("turn did not complete within 180s")

        assert saw_start, "message.start missing"
        assert len(deltas) >= 1, "no message.delta received"
        assert FAKE_PROVIDER_MARKER in "".join(deltas), (
            "deltas do not carry the deterministic provider content",
            deltas[:5],
        )
        final_text = complete.get("text") or ""
        assert complete.get("status") == "complete", ("complete status", complete)
        assert FAKE_PROVIDER_MARKER in final_text, ("final text", final_text[:200])
        evidence["deltas"] = len(deltas)
        evidence["complete_status"] = complete.get("status")
        evidence["final_text_prefix"] = final_text[:64]

        # Durable transcript BEFORE restart (real-agent persistence path):
        # the user turn and the assistant turn must both be present.
        status, body = rest(port, "/api/sessions?limit=100", token=token)
        assert status == 200, ("post-turn sessions status", status)
        row = next(
            (r for r in body.get("sessions") or [] if r.get("id") == stored_key), None
        )
        assert row is not None, "session row missing after first prompt"
        evidence["row_profile"] = row.get("profile")

        mstatus, mbody = rest(port, "/api/sessions/%s/messages" % stored_key, token=token)
        assert mstatus == 200, ("messages status", mstatus, mbody)
        assert mbody.get("session_id") == stored_key, ("messages session_id", mbody)
        messages = mbody.get("messages") or []
        user_texts = [
            message_text(m) for m in messages if m.get("role") == "user"
        ]
        assistant_texts = [
            message_text(m) for m in messages if m.get("role") == "assistant"
        ]
        assert any(prompt in text for text in user_texts), (
            "deterministic user turn not persisted", user_texts[:2],
        )
        assert any(FAKE_PROVIDER_MARKER in text for text in assistant_texts), (
            "deterministic assistant turn not persisted", assistant_texts[:2],
        )
        evidence["rest_message_count"] = len(messages)
        evidence["persisted_user_turn"] = True
        evidence["persisted_assistant_turn"] = True
    finally:
        await client.close()
    print("W3-TURN-OK " + json.dumps(evidence))
    return evidence


async def mode_resume_live(port, token, stored_key):
    evidence = {}
    client = GatewayWS(port, token)
    await client.connect()
    try:
        ready_index, _ = await client.scan(
            0, lambda f: f.get("method") == "event", timeout=20
        )
        resumed = await client.rpc(
            ready_index + 1,
            "session.resume",
            {"session_id": stored_key, "cols": 80},
            timeout=60,
        )
        result = resumed.get("result") or {}
        assert result.get("resumed") == stored_key, ("resume", resumed)
        messages_text = json.dumps(result.get("messages") or [])
        assert FAKE_PROVIDER_MARKER in messages_text, (
            "live resume lost the streamed transcript",
            messages_text[:300],
        )
        evidence["resumed"] = result.get("resumed")
        evidence["message_count"] = result.get("message_count")
    finally:
        await client.close()
    print("W3-RESUME-LIVE-OK " + json.dumps(evidence))


async def mode_post_restart(port, token, stored_key, prompt):
    evidence = {}
    status, body = rest(port, "/api/sessions?limit=100", token=token)
    assert status == 200, ("post-restart sessions status", status)
    row = next(
        (r for r in body.get("sessions") or [] if r.get("id") == stored_key), None
    )
    assert row is not None, "session row did not persist across restart"
    assert row.get("title") == SESSION_TITLE, ("durable title", row.get("title"))
    assert row.get("profile") == CANONICAL_DEFAULT, (
        "canonical default profile stamp", row.get("profile"),
    )
    evidence["row"] = {
        "id": row.get("id"),
        "title": row.get("title"),
        "profile": row.get("profile"),
        "is_default_profile": row.get("is_default_profile"),
    }

    # The persisted transcript must survive the real restart.
    mstatus, mbody = rest(port, "/api/sessions/%s/messages" % stored_key, token=token)
    assert mstatus == 200, ("post-restart messages status", mstatus, mbody)
    assert mbody.get("session_id") == stored_key, ("messages session_id", mbody)
    messages = mbody.get("messages") or []
    user_texts = [
        message_text(m) for m in messages if m.get("role") == "user"
    ]
    assistant_texts = [
        message_text(m) for m in messages if m.get("role") == "assistant"
    ]
    assert any(prompt in text for text in user_texts), (
        "user turn lost across restart", user_texts[:2],
    )
    assert any(FAKE_PROVIDER_MARKER in text for text in assistant_texts), (
        "assistant turn lost across restart", assistant_texts[:2],
    )
    evidence["rest_message_count"] = len(messages)

    client = GatewayWS(port, token)
    await client.connect()
    try:
        ready_index, _ = await client.scan(
            0, lambda f: f.get("method") == "event", timeout=20
        )
        resumed = await client.rpc(
            ready_index + 1,
            "session.resume",
            {"session_id": stored_key, "cols": 80},
            timeout=90,
        )
        result = resumed.get("result") or {}
        assert result.get("resumed") == stored_key, ("post-restart resume", resumed)
        assert result.get("session_key") == stored_key, (
            "resume session_key", result,
        )
        assert result.get("status") == "idle", ("resume status", result)
        messages_text = json.dumps(result.get("messages") or [])
        assert FAKE_PROVIDER_MARKER in messages_text, (
            "post-restart resume lost the durable transcript",
            messages_text[:300],
        )
        evidence["resumed"] = result.get("resumed")
        evidence["resume_message_count"] = result.get("message_count")
    finally:
        await client.close()
    print("W3-POST-RESTART-OK " + json.dumps(evidence))


def main():
    mode = sys.argv[1]
    port = int(sys.argv[2])
    token = sys.argv[3]
    extra = sys.argv[4:]
    if mode == "rest-auth":
        mode_rest_auth(port, token)
    elif mode == "ws-auth":
        asyncio.run(mode_ws_auth(port, token))
    elif mode == "turn":
        asyncio.run(mode_turn(port, token, extra[0], extra[1], extra[2]))
    elif mode == "resume-live":
        asyncio.run(mode_resume_live(port, token, extra[0]))
    elif mode == "post-restart":
        asyncio.run(mode_post_restart(port, token, extra[0], extra[1]))
    else:
        raise AssertionError("unknown mode: %s" % mode)


if __name__ == "__main__":
    main()
'''


def _module_source() -> str:
    with open(__file__, encoding="utf-8") as handle:
        return handle.read()


class HermesDesktopGatewayCompatContractTests(unittest.TestCase):
    """Fast structural suite (no Docker): pins the gate wiring, the exact
    v0.21 token-mode protocol constants, and the loopback/credential-free
    fake-provider seam. Runs on ordinary ``make test`` so discovery stays
    guarded even if the runtime class is refactored."""

    def test_gate_variables_are_the_documented_env_names(self) -> None:
        self.assertEqual(DOCKER_GATE_ENV_VAR, "RUN_DOCKER_TESTS")
        self.assertEqual(MODULE_GATE_ENV_VAR, "RUN_HERMES_DESKTOP_GATEWAY_COMPAT_TESTS")

    def test_runtime_gate_checks_both_variables_before_docker(self) -> None:
        source = _module_source()
        self.assertIn(
            'os.getenv(DOCKER_GATE_ENV_VAR) != "1"',
            source,
            "runtime gate must consult RUN_DOCKER_TESTS",
        )
        self.assertIn(
            'os.getenv(MODULE_GATE_ENV_VAR) != "1"',
            source,
            "runtime gate must consult its dedicated module variable",
        )
        setUp_source = inspect.getsource(
            HermesDesktopGatewayCompatTests.setUp
        )
        self.assertIn('os.getenv(DOCKER_GATE_ENV_VAR) != "1"', setUp_source)
        self.assertIn('os.getenv(MODULE_GATE_ENV_VAR) != "1"', setUp_source)
        self.assertIn("docker_available()", setUp_source)
        self.assertIn("skipTest(", setUp_source)

    def test_protocol_constants_match_v021_token_mode(self) -> None:
        self.assertEqual(WS_PATH, "/api/ws")
        self.assertEqual(WS_TOKEN_QUERY_PARAM, "token")
        self.assertEqual(REST_SESSION_TOKEN_HEADER, "X-Hermes-Session-Token")
        self.assertEqual(EXPECTED_CANDIDATE_VERSION, "0.21.0")
        self.assertEqual(PUBLIC_PROFILE_DISPLAY_NAME, "Josemar")
        self.assertEqual(CANONICAL_DEFAULT_PROFILE, "default")
        self.assertEqual(EXPECTED_BASE_HERMES_HOME, "/opt/data")
        self.assertIn(WS_PATH, W3_CLIENT_SOURCE)
        self.assertIn(REST_SESSION_TOKEN_HEADER, W3_CLIENT_SOURCE)

    def test_fake_provider_seam_is_loopback_and_credential_free(self) -> None:
        # The provider seam is target-supported `custom_providers` config with
        # a container-internal loopback base_url and a synthetic throwaway key.
        self.assertTrue(FAKE_PROVIDER_BASE_URL.startswith("http://127.0.0.1:"))
        self.assertEqual(FAKE_PROVIDER_NAME, "w3fake")
        self.assertEqual(FAKE_PROVIDER_API_KEY, "w3-synthetic-key")
        env_key = os.environ.get("OPENAI_API_KEY")
        if env_key:
            self.assertNotIn(env_key, FAKE_PROVIDER_API_KEY)
        self.assertIn("chat_completions", CONFIG_PROVIDER_BLOCK)
        self.assertIn("custom_providers:", CONFIG_PROVIDER_BLOCK)
        # The prompt/marker are deterministic constants, not user data.
        self.assertIsInstance(FAKE_PROVIDER_PROMPT, str)
        self.assertIn(FAKE_PROVIDER_MARKER, W3_FAKE_PROVIDER_SCRIPT)
        # The fake binds container loopback only, never contacts elsewhere,
        # and is threaded so the SDK's keep-alive connections cannot block
        # its accept loop.
        self.assertIn('FakeProviderServer(("127.0.0.1", PORT), Handler)', W3_FAKE_PROVIDER_SCRIPT)
        self.assertIn("ThreadingHTTPServer", W3_FAKE_PROVIDER_SCRIPT)
        self.assertIn("data: [DONE]", W3_FAKE_PROVIDER_SCRIPT)

    def test_isolation_and_cleanup_wiring_is_present(self) -> None:
        source = _module_source()
        self.assertIn("ComposeRuntime()", source)
        self.assertIn("addCleanup(self.runtime.down)", source)
        self.assertIn('"HERMES_DASHBOARD_HOST"', source)
        self.assertIn('"127.0.0.1"', source)
        self.assertIn('"HERMES_DASHBOARD_INSECURE"', source)
        self.assertIn('"HERMES_DASHBOARD"', source)
        # The provider entry is applied to the RUNTIME config only (test-local
        # state on the disposable volume) and proven through the real
        # hermes_cli resolution path before the turn.
        self.assertIn("W3-CONFIG-OK", source)
        self.assertIn("W3-PROVIDER-RESOLVES", source)
        self.assertIn("W3-MOCK-EVIDENCE-OK", source)
        self.assertIn(RUNTIME_CONFIG_PATH, source)


class HermesDesktopGatewayCompatTests(unittest.TestCase):
    """Opt-in Docker gate: builds the real v0.21 candidate image via this
    test's disposable Compose project and proves the Desktop Remote
    token-auth REST/WS/session/stream/persist/restart protocol inside the
    container as the hermes runtime user, driving the normal real-agent
    persistence path through a disposable loopback OpenAI-compatible fake."""

    def setUp(self) -> None:
        if os.getenv(DOCKER_GATE_ENV_VAR) != "1":
            self.skipTest(f"set {DOCKER_GATE_ENV_VAR}=1 to run Docker runtime tests")
        if os.getenv(MODULE_GATE_ENV_VAR) != "1":
            self.skipTest(
                f"set {MODULE_GATE_ENV_VAR}=1 to run the Hermes Desktop gateway "
                "compat gate"
            )
        if not docker_available():
            self.skipTest("docker CLI is not available")
        self.runtime = ComposeRuntime()
        # Explicitly enable the dashboard, keep it container-loopback, and
        # pin host-publish ports away from dev/production listeners.
        # HERMES_DASHBOARD_INSECURE is pinned to "0" on purpose: v0.21 no
        # longer lets it bypass dashboard auth, and this gate must never
        # depend on it — the loopback bind itself keeps token mode active.
        self.runtime.env["HERMES_DASHBOARD"] = "1"
        self.runtime.env["HERMES_DASHBOARD_HOST"] = "127.0.0.1"
        self.runtime.env["HERMES_DASHBOARD_INSECURE"] = "0"
        self.runtime.env["HERMES_DASHBOARD_PORT"] = DASHBOARD_PORT
        self.runtime.env["HERMES_DASHBOARD_BIND_IP"] = "127.0.0.1"
        self.runtime.env["HERMES_API_SERVER_PORT"] = API_SERVER_PORT
        self.runtime.env["HERMES_API_SERVER_BIND_IP"] = "127.0.0.1"
        # The session token is the synthetic ComposeRuntime-generated value.
        self.session_token = self.runtime.env["HERMES_DASHBOARD_SESSION_TOKEN"]
        self._client_shipped = False
        self._mock_started = False
        self.addCleanup(self.runtime.down)
        self.addCleanup(self._stop_fake_provider)
        # Evidence dir is created for client logs; it is gitignored and left
        # in place (including after failures) for post-run inspection.
        os.makedirs(DUMP_DIR, exist_ok=True)

    # -- in-container plumbing (all steps run as the hermes runtime user) ----

    def _hermes_exec(self, script: str, *, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a command inside the container as the hermes runtime user."""
        proc = self.runtime.exec(
            "hermes", "su", "-s", "/bin/sh", "hermes", "-c", script,
            check=False, timeout=timeout,
        )
        if check and proc.returncode != 0:
            self.fail(
                f"hermes command failed ({proc.returncode}): {script}\n"
                f"stdout: {proc.stdout[-3000:]}\nstderr: {proc.stderr[-3000:]}"
            )
        return proc

    def _ship_payload(self, name: str, source: str, target_path: str) -> None:
        encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
        self._hermes_exec(f"echo {encoded} | base64 -d > {target_path}")

    def _ship_client(self) -> None:
        if self._client_shipped:
            return
        self._ship_payload("client", W3_CLIENT_SOURCE, IN_CONTAINER_CLIENT_PATH)
        self._client_shipped = True

    def _wait_dashboard_ready(self, timeout: int = 240) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            proc = self._hermes_exec(HEALTH_PROBE_SCRIPT, timeout=60, check=False)
            if proc.returncode == 0 and "HEALTH-OK" in proc.stdout:
                return
            last = (proc.stdout or proc.stderr).strip()[-300:]
            time.sleep(3)
        self.fail(f"dashboard never became ready on 127.0.0.1:{DASHBOARD_PORT}: {last}")

    # -- disposable loopback fake provider lifecycle ---------------------------

    def _apply_provider_config(self) -> None:
        """Append the disposable custom_providers entry to the RUNTIME config
        (test-local state on the disposable volume; idempotent)."""
        proc = self._hermes_exec(CONFIG_WRITE_SCRIPT, timeout=120)
        self.assertIn("W3-CONFIG-OK", proc.stdout)

    def _assert_provider_resolves(self) -> None:
        """Prove the real hermes_cli resolution path maps the named provider
        to the loopback fake before the turn."""
        proc = self._hermes_exec(PROVIDER_RESOLUTION_PROBE, timeout=180)
        self.assertIn("W3-PROVIDER-RESOLVES", proc.stdout)

    def _start_fake_provider(self) -> None:
        """Write the fake provider onto the disposable volume and start it
        detached as hermes inside the container, then wait for health."""
        self._ship_payload(
            "fake-provider", W3_FAKE_PROVIDER_SCRIPT, FAKE_PROVIDER_SCRIPT_PATH,
        )
        proc = self.runtime.run(
            "exec", "-T", "-d", "hermes",
            "su", "-s", "/bin/sh", "hermes", "-c",
            "/opt/hermes/.venv/bin/python3 " + FAKE_PROVIDER_SCRIPT_PATH
            + " " + FAKE_PROVIDER_PORT
            + " " + FAKE_PROVIDER_LOG_PATH
            + " " + FAKE_PROVIDER_PID_PATH,
            check=False, timeout=120,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"fake provider start failed: {proc.stderr[-1000:]}",
        )
        self._mock_started = True
        deadline = time.monotonic() + 60
        probe = (
            "/opt/hermes/.venv/bin/python3 - <<'PYEOF'\n"
            "import urllib.request\n"
            f"resp = urllib.request.urlopen('{FAKE_PROVIDER_BASE_URL}/models', timeout=3)\n"
            "assert resp.status == 200, resp.status\n"
            "print('W3-FAKE-PROVIDER-READY')\n"
            "PYEOF"
        )
        while time.monotonic() < deadline:
            ready = self._hermes_exec(probe, timeout=60, check=False)
            if ready.returncode == 0 and "W3-FAKE-PROVIDER-READY" in ready.stdout:
                return
            time.sleep(1)
        self.fail("loopback fake provider did not become healthy in time")

    def _stop_fake_provider(self) -> None:
        """Kill the in-container fake provider (releasing its port) as hermes."""
        if not self._mock_started:
            return
        self._mock_started = False
        script = (
            "set -eu\n"
            f"pid=$(cat {FAKE_PROVIDER_PID_PATH} 2>/dev/null || true)\n"
            'if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi\n'
        )
        self.runtime.exec(
            "hermes", "su", "-s", "/bin/sh", "hermes", "-c", script,
            check=False, timeout=60,
        )

    def _assert_mock_hit(self) -> None:
        """Prove the agent's turn actually hit the loopback fake provider."""
        proc = self._hermes_exec(MOCK_EVIDENCE_PROBE, timeout=60)
        self.assertIn("W3-MOCK-EVIDENCE-OK", proc.stdout)

    # -- client driver ---------------------------------------------------------

    def _run_client(self, mode: str, *args: str, timeout: int = 300) -> dict:
        """Ship (once) and run the in-container client; return evidence.

        Client output is always persisted under the gitignored dump dir so
        failures leave real evidence behind."""
        self._ship_client()
        argv = " ".join(shlex.quote(part) for part in (mode, DASHBOARD_PORT, self.session_token) + args)
        proc = self._hermes_exec(
            f"/opt/hermes/.venv/bin/python3 {IN_CONTAINER_CLIENT_PATH} {argv}",
            timeout=timeout,
            check=False,
        )
        self._save_artifact(mode, proc.stdout, proc.stderr)
        marker = f"W3-{mode.upper()}-OK"
        if proc.returncode != 0 or marker not in proc.stdout:
            self.fail(
                f"{mode} phase failed (rc={proc.returncode})\n"
                f"stdout: {proc.stdout[-4000:]}\nstderr: {proc.stderr[-4000:]}"
            )
        evidence_line = next(
            line for line in reversed(proc.stdout.splitlines())
            if line.startswith(marker)
        )
        return json.loads(evidence_line[len(marker):].strip() or "{}")

    def _save_artifact(self, name: str, stdout: str, stderr: str) -> None:
        path = os.path.join(DUMP_DIR, f"{name}.log")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(stdout)
            if stderr:
                handle.write("\n--- stderr ---\n" + stderr)

    def _restart_hermes_preserving_state(self) -> None:
        """Stop and restart the hermes service; the disposable named volume
        is preserved (only ComposeRuntime.down removes project volumes)."""
        self.runtime.stop("hermes", timeout=300)
        self.runtime.start("hermes", timeout=600)

    # -- the gate -------------------------------------------------------------

    def test_v021_desktop_gateway_compat_lifecycle(self) -> None:
        """Full Desktop Remote token-auth lifecycle against the built
        candidate image: public status/readiness + version, token rejection
        and acceptance (REST + WS), profile surface, streamed REAL-agent
        turn over the disposable loopback provider with durable persisted
        transcript, reconnect rediscovers the session, and persisted
        transcript + resume across a real container restart."""
        # Build the real candidate image and start the isolated stack.
        self.runtime.up("hermes", timeout=1800)
        self.runtime.wait_until_hermes_writable(timeout=180)
        self._wait_dashboard_ready()

        # Wire the disposable provider seam: runtime-config entry (test-local
        # state) + in-container loopback fake + real resolution proof.
        self._apply_provider_config()
        self._assert_provider_resolves()
        self._start_fake_provider()

        # 1+2+3+4: public status/readiness/version, bad/good REST + WS auth,
        # and the public-profile surface.
        self._run_client("rest-auth", timeout=180)
        self._run_client("ws-auth", timeout=120)

        # 5+6+7: session create (not durable pre-prompt), streamed REAL-agent
        # turn over the loopback fake, durable user+assistant transcript.
        turn_evidence = self._run_client(
            "turn",
            FAKE_PROVIDER_PROMPT,
            FAKE_PROVIDER_MODEL,
            FAKE_PROVIDER_NAME,
            timeout=300,
        )
        stored_key = turn_evidence["stored_session_id"]
        self._assert_mock_hit()

        # 8: disconnect/reconnect — a fresh WS connection rediscovers the
        # session by its stored id and receives the transcript.
        self._run_client("resume-live", stored_key, timeout=180)

        # 9: real gateway/container restart preserving the disposable
        # volume; re-apply the test-local provider state (the container init
        # re-materializes the runtime config from the template) and restart
        # the in-container fake; then durable row + REST transcript + WS
        # resume history.
        self._restart_hermes_preserving_state()
        self.runtime.wait_until_hermes_writable(timeout=180)
        self._wait_dashboard_ready()
        self._apply_provider_config()
        self._start_fake_provider()
        post_evidence = self._run_client(
            "post-restart", stored_key, FAKE_PROVIDER_PROMPT, timeout=240,
        )
        self.assertEqual(post_evidence["row"]["id"], stored_key)
        self.assertEqual(post_evidence["row"]["title"], SESSION_TITLE)
        self.assertGreaterEqual(post_evidence["rest_message_count"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
