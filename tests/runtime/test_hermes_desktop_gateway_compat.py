"""Opt-in Docker runtime gate: Hermes Desktop Remote gateway compatibility
for the Hermes v0.21.0 candidate (issue #156 W3, revision 2 R1).

Skipped by default. Enable with:

  RUN_DOCKER_TESTS=1 RUN_HERMES_DESKTOP_GATEWAY_COMPAT_TESTS=1 \
  python3 -m unittest tests.runtime.test_hermes_desktop_gateway_compat -v

or:

  make test-hermes-desktop-gateway-compat

This gate is the PRODUCTION-EQUIVALENT gated-dashboard protocol proof: the
dashboard is explicitly enabled on a NON-loopback container bind
(``HERMES_DASHBOARD_HOST=0.0.0.0``, ``HERMES_DASHBOARD_INSECURE=0``), which
engages the v0.21 dashboard auth gate with the bundled synthetic-credential
basic provider. The legacy static session token still exists (ComposeRuntime
requires it for interpolation) but is proven INERT: REST calls carrying it
are rejected and a ``?token=`` WebSocket upgrade never reaches
``gateway.ready``. Every accepted credential follows the production shape:
password login -> private cookie jar -> single-use 30s ``?ticket=`` WS
upgrades, minted fresh per connection.

WHAT IT PROVES (each phase against the real candidate image built by this
test's own Compose project):

  1. Public status/readiness: ``/api/health`` and ``/api/status`` answer
     without credentials and report the exact candidate version (0.21.0)
     with ``auth_required: true``; ``/api/status`` advertises the ``basic``
     auth provider.
  2. Basic discovery: public ``/api/auth/providers`` advertises
     ``{name: "basic", supports_password: true}``.
  3. Gate enforcement: a protected REST endpoint rejects BOTH the
     no-credential request AND the static session token (header and bearer
     forms, HTTP 401); a ``?token=`` WebSocket upgrade never reaches
     ``gateway.ready``.
  4. Wrong password: ``POST /auth/password-login`` with a wrong password is
     rejected with the generic 401.
  5. Basic login + cookie session: the valid synthetic username/password
     pair logs in, the private 0600 cookie jar inside the disposable
     hermes-data volume persists the session, ``/api/auth/me`` reports
     ``provider: "basic"``, cookie-authenticated ``/api/profiles`` and
     ``/api/sessions`` succeed, and ``/api/profiles`` lists exactly ONE
     profile — the public display name ``Josemar`` over the canonical base
     HERMES_HOME with ``is_default`` true and no duplicate ``default`` row.
  6. Streamed REAL-agent turn over a fresh ticket: ``POST
     /api/auth/ws-ticket`` mints a 30s single-use ticket; the ``?ticket=``
     WS upgrade reaches ``gateway.ready``; ``session.create`` returns the
     live ``session_id`` plus the durable ``stored_session_id`` and the
     session is NOT durable before its first prompt; ``prompt.submit``
     drives the normal real-agent path over a disposable loopback
     ``custom_providers`` OpenAI-compatible fake (no external network, no
     real credentials); the turn emits ``message.start``, streamed
     ``message.delta`` frames, and the terminal settled
     ``message.complete`` with the fake's deterministic content
     (settlement judged ONLY by ``message.complete``, never
     ``session.info``); the mock's request log proves the agent hit the
     loopback ``chat/completions`` endpoint; the durable transcript is in
     REST ``/api/sessions/<stored>/messages`` (deterministic user + assistant
     turns).
  7. Reconnect: a NEW ticket minted from the SAME cookie session reconnects
     and ``session.resume`` returns the durable transcript.
  8. Real recreation: ``runtime.recreate("hermes")`` (compose
     ``up -d --force-recreate --no-build``) replaces the container while
     preserving the disposable volume; only the disposable provider runtime
     config is re-applied and the in-container fake restarted.
  9. Session survives recreation WITHOUT a new login: the pre-recreation
     cookie jar still authenticates ``/api/auth/me`` (provider basic —
     stateless HMAC sessions with the unchanged synthetic secret survive
     container recreation), the REST transcript persists, and a NEW
     post-recreate ticket reconnects and ``session.resume`` returns the
     same durable transcript.

The fake provider and its config wiring are disposable test-local state:
the provider entry is appended to the RUNTIME config file inside the
disposable volume (never a tracked file) and re-applied after the
recreation because the container init re-materializes the runtime config
from the template on every start; the fake provider script lives on the
disposable volume and is restarted after the recreation.

ISOLATION: ``ComposeRuntime`` + the test-isolation overlay replace the
repo's agent-state/credentials bind mounts with disposable empty dirs; all
dashboard credentials are synthetic values (basic username/password/secret
and the inert static session token); Telegram, workspace sync, and
hosted-provider credentials are blanked; the protocol clients and the fake
provider run INSIDE the candidate container as the ``hermes`` runtime user
using the image's own Hermes venv (which upstream's ISO harness uses for
``websockets``), so no host port is ever used and no new host-side Python
package is needed. Credentials, cookies, and tickets are never printed;
diagnostics carry statuses, ids, and deterministic markers only.

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
# Pinned v0.21 gated-dashboard protocol surface exercised by the in-container
# client (structurally asserted by the fast suite; proven live by the gate).
# ---------------------------------------------------------------------------
EXPECTED_CANDIDATE_VERSION = "0.21.0"
WS_PATH = "/api/ws"
WS_TOKEN_QUERY_PARAM = "token"
WS_TICKET_QUERY_PARAM = "ticket"
REST_SESSION_TOKEN_HEADER = "X-Hermes-Session-Token"
AUTH_PROVIDERS_ROUTE = "/api/auth/providers"
PASSWORD_LOGIN_ROUTE = "/auth/password-login"
AUTH_ME_ROUTE = "/api/auth/me"
WS_TICKET_ROUTE = "/api/auth/ws-ticket"
BASIC_PROVIDER_NAME = "basic"
EXPECTED_TICKET_TTL_SECONDS = 30
PUBLIC_PROFILE_DISPLAY_NAME = "Josemar"
CANONICAL_DEFAULT_PROFILE = "default"
EXPECTED_BASE_HERMES_HOME = "/opt/data"

# Synthetic gated-dashboard credentials (test-only; never real secrets).
BASIC_USERNAME = "w3gate-admin"
BASIC_PASSWORD = "w3gate-synthetic-password"
BASIC_SECRET = "w3gate-synthetic-secret-alpha-bravo-charlie"

# Host-publish port pins only (mirrors the other runtime gates so the
# disposable stack cannot collide with dev/production listeners). The
# protocol client and the fake provider are container-internal.
DASHBOARD_PORT = "19443"
API_SERVER_PORT = "18643"

SESSION_TITLE = "w3-desktop-gateway-compat"
SESSION_SOURCE = "w3-desktop-gateway-compat"

# Private 0600 cookie jar inside the disposable hermes-data volume: written
# by the login phase, reused (no new login) after the real recreation.
COOKIE_JAR_PATH = "/opt/data/.w3-gate/cookies.txt"

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

IN_CONTAINER_CLIENT_PATH = "/opt/data/.w3-gate/w3_compat_client.py"

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
# disposable volume; the log contains only request paths and stream flags —
# never credentials or environment).
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
# Gated mode only: password login -> private 0600 cookie jar -> fresh 30s
# single-use ?ticket= per WebSocket connection. The legacy static session
# token appears ONLY in inertness probes and is never an accepted credential.
#
# Modes: rest-auth / turn / resume-live / post-restart.
# Each prints a final `W3-<MODE>-OK {json evidence}` line on success.
# ---------------------------------------------------------------------------
W3_CLIENT_SOURCE = r'''
"""W3 Desktop Remote gateway protocol client (in-container, hermes venv).

Gated mode: password login -> private 0600 cookie jar -> fresh single-use
?ticket= per WS connection. Static session tokens are probe-only."""

import asyncio
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REST_SESSION_TOKEN_HEADER = "X-Hermes-Session-Token"
WS_PATH = "/api/ws"
WS_TOKEN_QUERY_PARAM = "token"
WS_TICKET_QUERY_PARAM = "ticket"
AUTH_PROVIDERS_ROUTE = "/api/auth/providers"
PASSWORD_LOGIN_ROUTE = "/auth/password-login"
AUTH_ME_ROUTE = "/api/auth/me"
WS_TICKET_ROUTE = "/api/auth/ws-ticket"
BASIC_PROVIDER_NAME = "basic"
EXPECTED_TICKET_TTL_SECONDS = 30

EXPECTED_VERSION = "0.21.0"
PUBLIC_PROFILE = "Josemar"
CANONICAL_DEFAULT = "default"
BASE_HOME = "/opt/data"
SESSION_TITLE = "w3-desktop-gateway-compat"
SESSION_SOURCE = "w3-desktop-gateway-compat"
FAKE_PROVIDER_MARKER = "W3-FAKE-PROVIDER-OK"

PORT = 0  # set from argv in main()


def make_jar(jar_path=None):
    if jar_path:
        return http.cookiejar.MozillaCookieJar(jar_path)
    return http.cookiejar.CookieJar()


def load_jar(jar_path):
    jar = make_jar(jar_path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def save_jar(jar, jar_path):
    os.makedirs(os.path.dirname(jar_path), mode=0o700, exist_ok=True)
    jar.save(ignore_discard=True, ignore_expires=True)
    os.chmod(jar_path, 0o600)


def make_opener(jar):
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def request_json(opener, method, path, body=None, extra_headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if extra_headers:
        for key, value in extra_headers.items():
            req.add_header(key, value)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode() or "{}")
        except Exception:
            parsed = {}
        return exc.code, parsed


def message_text(message):
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content)


def mint_ticket(opener):
    """Mint one fresh 30s single-use WS ticket (never printed)."""
    status, body = request_json(opener, "POST", WS_TICKET_ROUTE, body={})
    assert status == 200, ("ws-ticket status", status)
    assert body.get("ttl_seconds") == EXPECTED_TICKET_TTL_SECONDS, ("ttl", body)
    ticket = body.get("ticket")
    assert isinstance(ticket, str) and len(ticket) > 10, ("ticket shape", body)
    return ticket


def assert_transcript(messages, prompt):
    user_texts = [message_text(m) for m in messages if m.get("role") == "user"]
    assistant_texts = [message_text(m) for m in messages if m.get("role") == "assistant"]
    assert any(prompt in text for text in user_texts), (
        "deterministic user turn missing", user_texts[:2],
    )
    assert any(FAKE_PROVIDER_MARKER in text for text in assistant_texts), (
        "deterministic assistant turn missing", assistant_texts[:2],
    )


class GatewayWS:
    """Newline-delimited JSON-RPC over the candidate's /api/ws.

    ``query_name``/``query_value`` is the WS credential: gated mode accepts
    a fresh single-use ``?ticket=`` minted from the cookie session; the
    legacy ``?token=`` form is probe-only and must never reach ready. Every
    received frame is appended to ``self.frames``; lookups scan by index so
    a turn thread racing the RPC response (message.start can be written
    before the prompt.submit response) is never lost.
    """

    def __init__(self, port, query_name, query_value):
        from websockets.asyncio.client import connect  # noqa: PLC0415

        self._connect_factory = connect
        self.url = "ws://127.0.0.1:%d%s?%s=%s" % (
            port, WS_PATH, query_name,
            urllib.parse.quote(query_value, safe=""),
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


async def ws_probe_never_reaches_ready(port, query_name, query_value):
    """The given WS credential must not yield a working gateway connection."""
    bad = GatewayWS(port, query_name, query_value)
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
        return {"rejected": True, "how": type(exc).__name__, "code": code}
    try:
        # Connected at TCP level: the server must still not admit the
        # client — either close or never emit gateway.ready. The exact
        # close code is deliberately not asserted.
        try:
            await bad.scan(
                0,
                lambda f: f.get("method") == "event"
                and (f.get("params") or {}).get("type") == "gateway.ready",
                timeout=5,
            )
            return {"rejected": False, "how": "gateway.ready emitted"}
        except TimeoutError:
            return {"rejected": True, "how": "no gateway.ready within 5s"}
        except Exception as exc:
            return {"rejected": True, "how": "closed: %s" % type(exc).__name__}
    finally:
        await bad.close()


def mode_rest_auth(port, static_token, username, password, jar_path):
    evidence = {}
    bare = make_opener(make_jar())

    # 1. Public status/readiness: gated mode engaged, exact candidate version.
    status, body = request_json(bare, "GET", "/api/health")
    assert status == 200, ("health status", status, body)
    assert body.get("ok") is True, ("health ok", body)
    assert body.get("version") == EXPECTED_VERSION, ("health version", body)
    assert body.get("auth_required") is True, ("health auth_required", body)
    evidence["health"] = {
        "version": body.get("version"),
        "auth_required": body.get("auth_required"),
    }

    status, body = request_json(bare, "GET", "/api/status")
    assert status == 200, ("status status", status)
    assert body.get("version") == EXPECTED_VERSION, ("status version", body)
    assert body.get("auth_required") is True, ("status auth_required", body)
    assert BASIC_PROVIDER_NAME in (body.get("auth_providers") or []), (
        "status auth_providers", body.get("auth_providers"),
    )
    evidence["status_auth_providers"] = body.get("auth_providers")

    # 2. Basic discovery: password capability publicly advertised.
    status, body = request_json(bare, "GET", AUTH_PROVIDERS_ROUTE)
    assert status == 200, ("providers status", status, body)
    providers = body.get("providers") or []
    basic = next(
        (p for p in providers if p.get("name") == BASIC_PROVIDER_NAME), None
    )
    assert basic is not None, ("basic provider missing", providers)
    assert basic.get("supports_password") is True, ("supports_password", basic)
    evidence["auth_providers"] = [
        {"name": p.get("name"), "supports_password": p.get("supports_password")}
        for p in providers
    ]

    # 3. Gate enforcement: protected REST rejects no-credential AND the
    # static session token (header and bearer forms).
    status, body = request_json(bare, "GET", "/api/sessions?limit=10")
    assert status == 401, ("no-credential status", status, body)
    assert body.get("error") == "unauthenticated", ("401 envelope", body)
    status, body = request_json(
        bare, "GET", "/api/sessions?limit=10",
        extra_headers={REST_SESSION_TOKEN_HEADER: static_token},
    )
    assert status == 401, ("static token header status", status, body)
    status, body = request_json(
        bare, "GET", "/api/sessions?limit=10",
        extra_headers={"Authorization": "Bearer %s" % static_token},
    )
    assert status == 401, ("static token bearer status", status, body)
    evidence["static_token_rest_inert"] = True

    # Static-token WS upgrade never reaches ready (close code not asserted).
    ws_probe = asyncio.run(
        ws_probe_never_reaches_ready(port, WS_TOKEN_QUERY_PARAM, static_token)
    )
    assert ws_probe.get("rejected") is True, ("static token WS reached ready", ws_probe)
    evidence["static_token_ws_inert"] = ws_probe

    # 4. Wrong password: generic rejection (credential material not printed).
    wrong_status, wrong_body = request_json(
        make_opener(make_jar()), "POST", PASSWORD_LOGIN_ROUTE,
        body={"provider": BASIC_PROVIDER_NAME, "username": username,
              "password": password + "-deliberately-wrong", "next": "/"},
    )
    assert wrong_status == 401, ("wrong password status", wrong_status)
    assert wrong_body.get("detail"), ("generic detail", wrong_body)
    evidence["wrong_password_rejected"] = True

    # 5. Valid basic login -> private 0600 cookie jar -> authenticated REST.
    jar = make_jar(jar_path)
    opener = make_opener(jar)
    status, body = request_json(
        opener, "POST", PASSWORD_LOGIN_ROUTE,
        body={"provider": BASIC_PROVIDER_NAME, "username": username,
              "password": password, "next": "/"},
    )
    assert status == 200, ("login status", status, body)
    assert body.get("ok") is True, ("login ok", body)
    save_jar(jar, jar_path)
    evidence["login_ok"] = True

    status, body = request_json(opener, "GET", AUTH_ME_ROUTE)
    assert status == 200, ("auth me status", status, body)
    assert body.get("provider") == BASIC_PROVIDER_NAME, ("auth me provider", body)
    evidence["auth_me_provider"] = body.get("provider")

    status, body = request_json(opener, "GET", "/api/sessions?limit=100")
    assert status == 200, ("cookie sessions status", status, body)
    assert isinstance(body.get("sessions"), list), ("sessions shape", body)

    status, body = request_json(opener, "GET", "/api/profiles")
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


async def mode_turn(port, jar_path, prompt, model, provider):
    evidence = {}
    opener = make_opener(load_jar(jar_path))

    # Fresh single-use ticket minted from the cookie session, immediately
    # traded for the WS upgrade.
    ticket = mint_ticket(opener)
    client = GatewayWS(port, WS_TICKET_QUERY_PARAM, ticket)
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
        status, body = request_json(
            opener, "GET", "/api/sessions?limit=100",
        )
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

        # Durable transcript BEFORE recreation (real-agent persistence path):
        # the user turn and the assistant turn must both be present.
        status, body = request_json(opener, "GET", "/api/sessions?limit=100")
        assert status == 200, ("post-turn sessions status", status)
        row = next(
            (r for r in body.get("sessions") or [] if r.get("id") == stored_key), None
        )
        assert row is not None, "session row missing after first prompt"
        evidence["row_profile"] = row.get("profile")

        mstatus, mbody = request_json(
            opener, "GET", "/api/sessions/%s/messages" % stored_key,
        )
        assert mstatus == 200, ("messages status", mstatus, mbody)
        assert mbody.get("session_id") == stored_key, ("messages session_id", mbody)
        messages = mbody.get("messages") or []
        assert_transcript(messages, prompt)
        evidence["rest_message_count"] = len(messages)
        evidence["persisted_user_turn"] = True
        evidence["persisted_assistant_turn"] = True
    finally:
        await client.close()
    print("W3-TURN-OK " + json.dumps(evidence))
    return evidence


async def mode_resume_live(port, jar_path, stored_key):
    evidence = {}
    opener = make_opener(load_jar(jar_path))

    # Phase 7: a NEW ticket from the SAME cookie session.
    ticket = mint_ticket(opener)
    client = GatewayWS(port, WS_TICKET_QUERY_PARAM, ticket)
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
            "live resume lost the durable transcript",
            messages_text[:300],
        )
        evidence["resumed"] = result.get("resumed")
        evidence["message_count"] = result.get("message_count")
    finally:
        await client.close()
    print("W3-RESUME-LIVE-OK " + json.dumps(evidence))


async def mode_post_restart(port, jar_path, stored_key, prompt):
    evidence = {}
    # Phase 9: the PRE-RECREATION cookie jar still authenticates without a
    # new password login (stateless HMAC session, stable synthetic secret).
    opener = make_opener(load_jar(jar_path))
    status, body = request_json(opener, "GET", AUTH_ME_ROUTE)
    assert status == 200, ("post-recreate auth me status", status, body)
    assert body.get("provider") == BASIC_PROVIDER_NAME, (
        "post-recreate auth me provider", body,
    )
    evidence["auth_me_provider"] = body.get("provider")

    status, body = request_json(opener, "GET", "/api/sessions?limit=100")
    assert status == 200, ("post-recreation sessions status", status)
    row = next(
        (r for r in body.get("sessions") or [] if r.get("id") == stored_key), None
    )
    assert row is not None, "session row did not persist across recreation"
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

    # The persisted transcript must survive the real recreation.
    mstatus, mbody = request_json(
        opener, "GET", "/api/sessions/%s/messages" % stored_key,
    )
    assert mstatus == 200, ("post-recreation messages status", mstatus, mbody)
    assert mbody.get("session_id") == stored_key, ("messages session_id", mbody)
    messages = mbody.get("messages") or []
    assert_transcript(messages, prompt)
    evidence["rest_message_count"] = len(messages)

    # NEW post-recreate ticket; reconnect; same durable transcript on resume.
    ticket = mint_ticket(opener)
    client = GatewayWS(port, WS_TICKET_QUERY_PARAM, ticket)
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
        assert result.get("resumed") == stored_key, ("post-recreate resume", resumed)
        assert result.get("session_key") == stored_key, (
            "resume session_key", result,
        )
        assert result.get("status") == "idle", ("resume status", result)
        messages_text = json.dumps(result.get("messages") or [])
        assert FAKE_PROVIDER_MARKER in messages_text, (
            "post-recreate resume lost the durable transcript",
            messages_text[:300],
        )
        evidence["resumed"] = result.get("resumed")
        evidence["resume_message_count"] = result.get("message_count")
    finally:
        await client.close()
    print("W3-POST-RESTART-OK " + json.dumps(evidence))


def main():
    global PORT
    PORT = int(sys.argv[2])
    mode = sys.argv[1]
    if mode == "rest-auth":
        mode_rest_auth(PORT, sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])
    elif mode == "turn":
        asyncio.run(mode_turn(PORT, sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]))
    elif mode == "resume-live":
        asyncio.run(mode_resume_live(PORT, sys.argv[3], sys.argv[4]))
    elif mode == "post-restart":
        asyncio.run(mode_post_restart(PORT, sys.argv[3], sys.argv[4], sys.argv[5]))
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
    v0.21 gated basic/cookie/ticket protocol constants, the recreation
    lifecycle, and the loopback/credential-free fake-provider seam. Runs on
    ordinary ``make test`` so discovery stays guarded even if the runtime
    class is refactored."""

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

    def test_protocol_constants_match_v021_gated_mode(self) -> None:
        self.assertEqual(AUTH_PROVIDERS_ROUTE, "/api/auth/providers")
        self.assertEqual(PASSWORD_LOGIN_ROUTE, "/auth/password-login")
        self.assertEqual(AUTH_ME_ROUTE, "/api/auth/me")
        self.assertEqual(WS_TICKET_ROUTE, "/api/auth/ws-ticket")
        self.assertEqual(BASIC_PROVIDER_NAME, "basic")
        self.assertEqual(EXPECTED_TICKET_TTL_SECONDS, 30)
        self.assertEqual(EXPECTED_CANDIDATE_VERSION, "0.21.0")
        self.assertEqual(WS_TICKET_QUERY_PARAM, "ticket")
        self.assertEqual(PUBLIC_PROFILE_DISPLAY_NAME, "Josemar")
        self.assertEqual(CANONICAL_DEFAULT_PROFILE, "default")
        self.assertEqual(EXPECTED_BASE_HERMES_HOME, "/opt/data")
        for needle in (
            PASSWORD_LOGIN_ROUTE, AUTH_PROVIDERS_ROUTE, AUTH_ME_ROUTE,
            WS_TICKET_ROUTE, "gateway.ready",
        ):
            self.assertIn(needle, W3_CLIENT_SOURCE)

    def test_gated_design_replaces_static_token_acceptance(self) -> None:
        """Token-mode is NOT the acceptance protocol: the static session
        token appears only in inertness probes; every accepted WS credential
        is a freshly minted ?ticket= from the cookie session; the lifecycle
        uses a real force-recreate and a private 0600 cookie jar."""
        source = _module_source()
        client = W3_CLIENT_SOURCE
        # Accepted WS credentials are tickets, minted fresh per connection.
        self.assertEqual(client.count("GatewayWS(port, WS_TICKET_QUERY_PARAM, ticket)"), 3)
        # The static token is constructed exactly once — the inertness probe
        # (the WS credential itself is a GatewayWS constructor parameter, so
        # the token never appears as a hard-coded accepted credential).
        self.assertEqual(
            client.count("ws_probe_never_reaches_ready(port, WS_TOKEN_QUERY_PARAM"), 1
        )
        self.assertIn("async def ws_probe_never_reaches_ready(port, query_name, query_value)", client)
        # Cookie jar: private 0600 file inside the disposable volume,
        # reused after recreation without a new login.
        self.assertIn("os.chmod(jar_path, 0o600)", client)
        self.assertEqual(COOKIE_JAR_PATH, "/opt/data/.w3-gate/cookies.txt")
        self.assertIn("load_jar(jar_path)", client)
        # Real recreation lifecycle (force-recreate, no build, volumes kept).
        self.assertIn('self.runtime.recreate("hermes")', source)
        # Gated bind: explicitly non-loopback inside the container.
        setUp_source = inspect.getsource(HermesDesktopGatewayCompatTests.setUp)
        self.assertIn('"HERMES_DASHBOARD_HOST"', setUp_source)
        self.assertIn('"0.0.0.0"', setUp_source)
        self.assertIn('"HERMES_DASHBOARD_INSECURE"', setUp_source)
        self.assertIn('"HERMES_DASHBOARD"', setUp_source)
        # Synthetic basic credentials are explicit, never host-derived.
        self.assertIn("BASIC_USERNAME", setUp_source)
        self.assertIn("BASIC_PASSWORD", setUp_source)
        self.assertIn("BASIC_SECRET", setUp_source)
        self.assertGreaterEqual(len(BASIC_SECRET), 16)

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
        self.assertIn("addCleanup(self._stop_fake_provider)", source)
        # The provider entry is applied to the RUNTIME config only (test-local
        # state on the disposable volume) and proven through the real
        # hermes_cli resolution path before the turn.
        self.assertIn("W3-CONFIG-OK", source)
        self.assertIn("W3-PROVIDER-RESOLVES", source)
        self.assertIn("W3-MOCK-EVIDENCE-OK", source)
        self.assertIn(RUNTIME_CONFIG_PATH, source)


class HermesDesktopGatewayCompatTests(unittest.TestCase):
    """Opt-in Docker gate: builds the real v0.21 candidate image via this
    test's disposable Compose project and proves the PRODUCTION-EQUIVALENT
    gated Desktop Remote protocol (basic login -> private cookie jar ->
    single-use WS tickets) inside the container as the hermes runtime user,
    driving the normal real-agent persistence path through a disposable
    loopback OpenAI-compatible fake, including transcript + cookie-session
    durability across a real force-recreate."""

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
        # PRODUCTION-EQUIVALENT gated dashboard: explicitly enabled on a
        # non-loopback container bind so the v0.21 auth gate engages with the
        # bundled basic provider (synthetic credentials only). INSECURE is
        # pinned to "0" — it does not bypass auth and must never be relied on.
        # The static session token still exists (compose interpolation needs
        # it) and is proven INERT by the gate itself.
        self.runtime.env["HERMES_DASHBOARD"] = "1"
        self.runtime.env["HERMES_DASHBOARD_HOST"] = "0.0.0.0"
        self.runtime.env["HERMES_DASHBOARD_INSECURE"] = "0"
        self.runtime.env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] = BASIC_USERNAME
        self.runtime.env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] = BASIC_PASSWORD
        self.runtime.env["HERMES_DASHBOARD_BASIC_AUTH_SECRET"] = BASIC_SECRET
        self.runtime.env["HERMES_DASHBOARD_PORT"] = DASHBOARD_PORT
        self.runtime.env["HERMES_DASHBOARD_BIND_IP"] = "127.0.0.1"
        self.runtime.env["HERMES_API_SERVER_PORT"] = API_SERVER_PORT
        self.runtime.env["HERMES_API_SERVER_BIND_IP"] = "127.0.0.1"
        # The static session token is the synthetic ComposeRuntime-generated
        # value; the gate proves it is inert in gated mode.
        self.static_session_token = self.runtime.env["HERMES_DASHBOARD_SESSION_TOKEN"]
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
        directory = os.path.dirname(target_path)
        self._hermes_exec(
            f"mkdir -p {shlex.quote(directory)} && "
            f"echo {encoded} | base64 -d > {shlex.quote(target_path)}"
        )

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
        failures leave real evidence behind. Arguments are positional and
        quoted; credentials/cookies/tickets are never printed by the client."""
        self._ship_client()
        argv = " ".join(shlex.quote(part) for part in (mode, DASHBOARD_PORT) + args)
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

    # -- the gate -------------------------------------------------------------

    def test_v021_desktop_gateway_compat_lifecycle(self) -> None:
        """Full gated Desktop Remote lifecycle against the built candidate
        image: public status/readiness + version with the gate engaged,
        basic discovery, static-token inertness (REST + WS), wrong-password
        rejection, basic login with a private 0600 cookie jar, fresh
        single-use ticket WS upgrades, streamed REAL-agent turn over the
        disposable loopback provider with durable persisted transcript,
        reconnect resume, and cookie-session + transcript durability across
        a real force-recreate."""
        # Build the real candidate image and start the isolated stack.
        self.runtime.up("hermes", timeout=1800)
        self.runtime.wait_until_hermes_writable(timeout=180)
        self._wait_dashboard_ready()

        # Wire the disposable provider seam: runtime-config entry (test-local
        # state) + in-container loopback fake + real resolution proof.
        self._apply_provider_config()
        self._assert_provider_resolves()
        self._start_fake_provider()

        # Phases 1-5: public status/readiness with the gate engaged, basic
        # discovery, static-token inertness (REST + WS), wrong-password
        # rejection, basic login -> private 0600 cookie jar, authenticated
        # REST, public Josemar over canonical default.
        self._run_client(
            "rest-auth",
            self.static_session_token,
            BASIC_USERNAME,
            BASIC_PASSWORD,
            COOKIE_JAR_PATH,
            timeout=240,
        )

        # Phase 6: fresh ticket -> gateway.ready -> session create (not
        # durable pre-prompt) -> streamed REAL-agent turn over the loopback
        # fake -> durable user+assistant transcript.
        turn_evidence = self._run_client(
            "turn",
            COOKIE_JAR_PATH,
            FAKE_PROVIDER_PROMPT,
            FAKE_PROVIDER_MODEL,
            FAKE_PROVIDER_NAME,
            timeout=300,
        )
        stored_key = turn_evidence["stored_session_id"]
        self._assert_mock_hit()

        # Phase 7: a NEW ticket from the SAME cookie session; reconnect and
        # resume the durable transcript.
        self._run_client("resume-live", COOKIE_JAR_PATH, stored_key, timeout=180)

        # Phase 8: REAL recreation (compose up -d --force-recreate --no-build;
        # disposable volume preserved), then only the disposable provider
        # runtime config is re-applied and the fake restarted.
        self.runtime.recreate("hermes", timeout=600)
        self.runtime.wait_until_hermes_writable(timeout=180)
        self._wait_dashboard_ready()
        self._apply_provider_config()
        self._start_fake_provider()

        # Phase 9: pre-recreation cookie jar still authenticates /api/auth/me
        # without a new login; durable transcript via REST; NEW post-recreate
        # ticket reconnects and session.resume returns the same transcript.
        post_evidence = self._run_client(
            "post-restart", COOKIE_JAR_PATH, stored_key,
            FAKE_PROVIDER_PROMPT, timeout=240,
        )
        self.assertEqual(post_evidence["row"]["id"], stored_key)
        self.assertEqual(post_evidence["row"]["title"], SESSION_TITLE)
        self.assertGreaterEqual(post_evidence["rest_message_count"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
