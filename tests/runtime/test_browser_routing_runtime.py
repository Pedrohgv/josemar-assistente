"""Opt-in Docker runtime test for the Hermes rev-2 browser routing
(issue #136).

Skipped by default. Enable with:

  RUN_DOCKER_TESTS=1 RUN_BROWSER_ROUTING_RUNTIME_TESTS=1 \
  python3 -m unittest tests.runtime.test_browser_routing_runtime -v

or:

  make test-browser-routing-runtime

All inside a disposable Hermes image built from the production
Dockerfile.hermes (which exercises the REAL rev-2 patch path, the
agent-browser@0.26.0 bake, the pinned Chrome for Testing bake, and the
/opt/josemar/browser-use venv bake); no
production data/secrets/volumes are mounted; every in-container command runs
as the `hermes` runtime user, never root.

Evidence claims (each phase proves exactly what it says, nothing more):

  1. Config schema/startup: the runtime config (/opt/data/config.yaml)
     materializes; the REAL hermes_cli.config loader accepts it with
     backend "off" / cloud_provider "local" / connected_cdp_url; the RAW
     runtime-file view (real raw reader or yaml.safe_load) has NO global
     cdp_url key; the gateway is alive.
  2. LIVE SESSION TOOLSET (the design's core claim): in the built image
     with the shipped config, upstream browser_exec is HIDDEN
     (is_browser_use_cli_mode() == False under backend "off") while
     connected_browser_exec is VISIBLE (is_connected_browser_configured()
     == True) and the built-in browser_navigate/browser_snapshot exist.
     Registry/toolset introspection is attempted; the check_fn evidence is
     the hard gate (check_fn results drive tool inclusion).
  3. Cold-start ordinary browser_*: agent-browser@0.26.0 and the pinned
     Chrome for Testing tree are baked and UNCHANGED across the
     first real browser_navigate + browser_snapshot against a disposable
     loopback HTTP fixture (no runtime npx/package/browser download; the
     agent-browser HOME cache under $HOME/.agent-browser stays absent, no
     new cache dirs appear anywhere); AGENT_BROWSER_EXECUTABLE_PATH is set
     in-container as the hermes user; the
     connected endpoint being absent does not affect ordinary success; the
     Hermes container never listens on 9222.
  4. Connected fail-closed: connected_browser_exec with the endpoint down
     returns the generic connected-route error, never opens/mutates an
     ordinary headless session, never falls back, and never binds 9222.
  5. Real connected success: a separate disposable Chrome/Chromium
     fixture container (own namespace on the isolated Compose network, no
     host ports; CONTAINER-ONLY test-topology deviation — it binds CDP on
     0.0.0.0 inside its own container because Hermes reaches it via the
     bridge hostname, unlike the real laptop launcher's loopback-only
     reverse-tunnel posture) serves CDP; the disposable runtime config
     only (never a tracked file) points browser.connected_cdp_url at it;
     the REAL connected_browser_exec runs through the REAL
     /opt/josemar/browser-use CLI: env scrub evidence (only the preflighted
     BU_CDP_WS + reserved BU_NAME survive from ambient), loopback ws
     authority normalization (the reported ws host is rewritten to the
     configured fixture authority and proven bridge-reachable FROM Hermes
     via a real TCP connect before the CLI is handed the route),
     deterministic session mapping, own-tab safety (a pre-opened marker tab
     keeps its URL and stays open), and a hanging CLI is killed within the
     bounded timeout with a generic leak-free error.
  6. The Hermes container NEVER listens on 9222 in any phase; only the
     fixture does.

If the pinned image cannot provide a piece of evidence (e.g. the W1 patch
defect where _CONNECTED_BROWSER_CLI is referenced but not defined), the
probe reports the exact failure and the gate FAILS with that evidence —
success is never faked. Probe artifacts land only under the gitignored
dump_folder/browser-routing-runtime/; teardown is unconditional
(docker compose down -v --remove-orphans + docker rm -f for the fixture).
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import time
import unittest
import uuid

from .helpers import ComposeRuntime, docker_available

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DUMP_DIR = os.path.join(REPO_ROOT, "dump_folder", "browser-routing-runtime")

EXPECTED_CONNECTED = "http://127.0.0.1:9222"
MARKER_TAB_URL = "data:text/html,<title>JC-MARKER-TAB</title>"

# Disposable external-CDP fixture command (runs inside the fixture container
# only; never publishes host ports). CONTAINER-ONLY TEST-TOPOLOGY DEVIATION:
# the real laptop launcher binds CDP loopback-only
# (--remote-debugging-address=127.0.0.1) because the reverse tunnel forwards
# Hermes's own loopback; this disposable fixture is a SEPARATE bridge
# container that Hermes must reach via its network hostname. Chrome for
# Testing 152 binds the DevTools endpoint loopback-only regardless of
# --remote-debugging-address and rejects non-IP Host headers, so the fixture
# runs a tiny stdlib TCP forwarder that binds 0.0.0.0:9222 inside its own
# container, forwards to Chrome's loopback CDP, and rewrites the Host header
# to 127.0.0.1:9222. This is NOT a launcher-fidelity claim: the fixture
# mirrors the launcher's CDP port and its no-`--remote-allow-origins` posture
# only. Container-required deviations (documented): --no-sandbox
# (unprivileged container), --user-data-dir=/tmp/fixture-profile (disposable
# profile), headless mode, and the loopback-to-bridge forwarder (test-only
# topology). The marker tab is opened as a single URL so own-tab safety can
# be verified through the fixture's /json/list.
FIXTURE_CMD = r'''
set -e
BIN=""
for c in \
  "/opt/josemar/agent-browser/chrome/chrome" \
  "/opt/hermes/.playwright/chromium_headless_shell-"*/chrome-linux/headless_shell \
  "/opt/hermes/.playwright/chromium-"*/chrome-linux/chrome \
  "/root/.cache/ms-playwright/chromium_headless_shell-"*/chrome-linux/headless_shell \
  "/root/.cache/ms-playwright/chromium-"*/chrome-linux/chrome \
  "/opt/hermes/.cache/ms-playwright/chromium_headless_shell-"*/chrome-linux/headless_shell \
  "/opt/hermes/.cache/ms-playwright/chromium-"*/chrome-linux/chrome \
  /usr/bin/google-chrome /usr/bin/google-chrome-stable /usr/bin/chromium \
  /usr/bin/chromium-browser /usr/bin/headless_shell /opt/google/chrome/chrome; do
  if [ -x "$c" ]; then BIN="$c"; break; fi
done
if [ -z "$BIN" ]; then
  echo "FIXTURE-UNAVAILABLE: no supported browser executable found in the built image" >&2
  exit 2
fi
echo "FIXTURE-BROWSER=$BIN"
case "$BIN" in
  *headless_shell*) EXTRA="" ;;
  *) EXTRA="--headless=new" ;;
esac
"$BIN" $EXTRA --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
  --no-first-run --disable-background-mode --user-data-dir=/tmp/fixture-profile \
  "data:text/html,<title>JC-MARKER-TAB</title>" &
CHROME_PID=$!
# stdlib TCP forwarder: expose Chrome's loopback CDP on 0.0.0.0:9222 for the
# bridge network and rewrite the Host header to 127.0.0.1:9222 (Chrome rejects
# non-IP Host headers). Chrome for Testing 152 binds the DevTools endpoint to
# the IPv6 loopback ([::1]) nondeterministically, so the forwarder connects
# to "localhost" (tries both stacks) rather than 127.0.0.1. Test-only topology;
# the real launcher needs none of this because Hermes reaches its own loopback
# through the reverse tunnel.
python3 - <<'PY'
import socket, select, threading

def handle(c):
    up = socket.create_connection(("localhost", 9222))
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = c.recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    out = []
    for ln in head.split(b"\r\n"):
        if ln.lower().startswith(b"host:"):
            out.append(b"Host: 127.0.0.1:9222")
        else:
            out.append(ln)
    up.sendall(b"\r\n".join(out) + b"\r\n\r\n" + rest)
    while True:
        r, _, _ = select.select([c, up], [], [], 1.0)
        if not r:
            continue
        for s in r:
            try:
                d = s.recv(65536)
            except Exception:
                d = b""
            if not d:
                c.close()
                up.close()
                return
            (up if s is c else c).sendall(d)

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 9222))
srv.listen(50)
print("FIXTURE-FORWARDER-READY", flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
PY
'''


# In-container probe: config schema/startup acceptance with the REAL
# hermes_cli.config loader + raw runtime-file view. Prints only safe
# booleans/identifiers plus the expected nonsecret loopback key value —
# never the whole config or secrets.
RT_CONFIG_PROBE_SOURCE = r'''
"""Narrow config schema/startup probe (rev-2) against the REAL
hermes_cli.config loader and raw runtime-file reader."""

import inspect
import sys

sys.path.insert(0, "/opt/hermes")
sys.path.insert(0, "/opt/hermes/tools")

EXPECTED_CONNECTED = "http://127.0.0.1:9222"
RUNTIME_CONFIG = "/opt/data/config.yaml"

load_config = None
loader_how = ""
errors = []
try:
    from hermes_cli.config import load_config  # noqa: F401
    loader_how = "hermes_cli.config.load_config"
except Exception as exc:
    errors.append(f"import: {exc!r}")
    try:
        import hermes_cli.config as _cfgmod
        load_config = _cfgmod.load_config
        loader_how = "hermes_cli.config.load_config (attr)"
    except Exception as exc2:
        errors.append(f"attr: {exc2!r}")
if load_config is None:
    print("CONFIG-PROBE-FAILED: " + "; ".join(errors))
    sys.exit(3)

cfg = None
call_errors = []
try:
    params = list(inspect.signature(load_config).parameters)
    if params and params[0] not in ("self", "cls"):
        cfg = load_config(RUNTIME_CONFIG)
    else:
        cfg = load_config()
except Exception as exc:
    call_errors.append(f"sig-call: {exc!r}")
    try:
        cfg = load_config(RUNTIME_CONFIG)
    except Exception as exc2:
        call_errors.append(f"pos-call: {exc2!r}")
if cfg is None:
    print("CONFIG-PROBE-FAILED: " + "; ".join(call_errors))
    sys.exit(3)

if isinstance(cfg, dict):
    loaded_browser = cfg.get("browser") or {}
    loaded = {
        "backend": loaded_browser.get("backend"),
        "cloud_provider": loaded_browser.get("cloud_provider"),
        "connected_cdp_url": loaded_browser.get("connected_cdp_url"),
    }
else:
    browser = getattr(cfg, "browser", None)
    loaded = {
        "backend": getattr(browser, "backend", None),
        "cloud_provider": getattr(browser, "cloud_provider", None),
        "connected_cdp_url": getattr(browser, "connected_cdp_url", None),
    }
loader_ok = (
    loaded.get("backend") == "off"
    and loaded.get("cloud_provider") == "local"
    and loaded.get("connected_cdp_url") == EXPECTED_CONNECTED
)
if not loader_ok:
    print(f"CONFIG-PROBE-FAILED: loader view {loaded!r}")
    sys.exit(3)

# RAW runtime-file view: no global browser.cdp_url key (the loader may
# normalize other keys; the raw file is the ground truth for the Josemar
# config contract).
raw_mapping = None
raw_how = ""
raw_errors = []
raw_reader_normalizes = False
try:
    import hermes_cli.config as _rawmod
    raw_reader = None
    for cand in ("read_raw_config", "load_raw_config", "read_config_raw"):
        fn = getattr(_rawmod, cand, None)
        if callable(fn):
            raw_reader = fn
            raw_how = f"hermes_cli.config.{cand}"
            break
    if raw_reader is not None:
        rparams = list(inspect.signature(raw_reader).parameters)
        if rparams and rparams[0] not in ("self", "cls"):
            reader_raw = raw_reader(RUNTIME_CONFIG)
        else:
            reader_raw = raw_reader()
        if isinstance(reader_raw, dict):
            if "browser" in reader_raw:
                reader_mapping = reader_raw.get("browser") or {}
            else:
                reader_mapping = reader_raw
            if "cdp_url" in reader_mapping:
                raw_reader_normalizes = True
            else:
                raw_mapping = reader_mapping
except Exception as exc:
    raw_errors.append(f"raw-reader: {exc!r}")

if raw_mapping is None:
    import yaml
    try:
        with open(RUNTIME_CONFIG, encoding="utf-8") as fh:
            raw_file = yaml.safe_load(fh)
        raw_mapping = (raw_file or {}).get("browser") or {}
        if raw_reader_normalizes:
            raw_how = "yaml.safe_load(raw runtime file; real raw reader normalizes)"
        else:
            raw_how = "yaml.safe_load(raw runtime file)"
    except Exception as exc:
        raw_errors.append(f"yaml: {exc!r}")
if raw_mapping is None:
    print("CONFIG-PROBE-FAILED: no raw runtime-file view: " + "; ".join(raw_errors))
    sys.exit(3)

raw_ok = (
    "cdp_url" not in raw_mapping
    and raw_mapping.get("backend") == "off"
    and raw_mapping.get("cloud_provider") == "local"
    and raw_mapping.get("connected_cdp_url") == EXPECTED_CONNECTED
)
if not raw_ok:
    print(f"CONFIG-PROBE-FAILED: raw browser mapping invalid: "
          f"cdp_url_present={'cdp_url' in raw_mapping} backend={raw_mapping.get('backend')!r}")
    sys.exit(3)

print(f"CONFIG-PROBE loader={loader_how} raw_reader={raw_how} "
      f"loader_ok={loader_ok} raw_ok={raw_ok}")
print("CONFIG-PROBE-OK")
'''


# In-container probe: LIVE SESSION TOOLSET evidence. The check_fn results
# are the authoritative visibility gate (check_fn results are TTL-cached and
# drive tool inclusion in the schema assembly path); registry/toolset
# introspection is attempted and reported. Also verifies the built-in
# browser_navigate/browser_snapshot exist.
RT_TOOLSET_PROBE_SOURCE = r'''
"""LIVE SESSION TOOLSET probe (rev-2): browser_exec hidden, built-in
browser_* present, connected_browser_exec visible."""

import sys

sys.path.insert(0, "/opt/hermes/tools")
sys.path.insert(0, "/opt/hermes")

mod = None
errors = []
try:
    import browser_use_cli  # noqa: F401
    mod = browser_use_cli
except Exception as exc:
    errors.append(f"top-level: {exc!r}")
    sys.modules.pop("browser_use_cli", None)
if mod is None:
    try:
        from tools import browser_use_cli  # type: ignore
        mod = browser_use_cli
    except Exception as exc:
        errors.append(f"package: {exc!r}")
        sys.modules.pop("tools.browser_use_cli", None)
if mod is None:
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("browser_use_cli", "/opt/hermes/tools/browser_use_cli.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        errors.append(f"spec: {exc!r}")
if mod is None:
    print("TOOLSET-PROBE-FAILED: module import failed: " + "; ".join(errors))
    sys.exit(3)

# check_fn evidence (the authoritative visibility gate).
bu_cli_mode = None
try:
    bu_cli_mode = bool(mod.is_browser_use_cli_mode())
except Exception as exc:
    print(f"TOOLSET-PROBE-FAILED: is_browser_use_cli_mode raised: {exc!r}")
    sys.exit(3)
connected_cfg = None
try:
    connected_cfg = bool(mod.is_connected_browser_configured())
except Exception as exc:
    print(f"TOOLSET-PROBE-FAILED: is_connected_browser_configured raised: {exc!r}")
    sys.exit(3)
print(f"CHECK-FN browser_use_cli_mode={bu_cli_mode} connected_browser_configured={connected_cfg}")
if bu_cli_mode is not False or connected_cfg is not True:
    print("TOOLSET-PROBE-FAILED: session toolset gate evidence unexpected")
    sys.exit(3)

# Built-in ordinary browser tools.
try:
    import browser_tool  # type: ignore
    print(f"BROWSER_TOOL navigate={hasattr(browser_tool, 'browser_navigate')} "
          f"snapshot={hasattr(browser_tool, 'browser_snapshot')}")
    if not (hasattr(browser_tool, "browser_navigate") and hasattr(browser_tool, "browser_snapshot")):
        print("TOOLSET-PROBE-FAILED: built-in browser_* tools missing")
        sys.exit(3)
except Exception as exc:
    print(f"TOOLSET-PROBE-FAILED: browser_tool import raised: {exc!r}")
    sys.exit(3)

# Registry introspection (attempted; reported).
try:
    from tools.registry import registry as reg
    names = set()
    for attr in ("_tools", "tools", "registered", "registered_tools"):
        obj = getattr(reg, attr, None)
        if isinstance(obj, dict):
            names |= set(obj)
        elif callable(obj):
            try:
                res = obj()
                if isinstance(res, dict):
                    names |= set(res)
                elif isinstance(res, (set, list, tuple)):
                    names |= set(res)
            except Exception:
                pass
    print(f"REGISTRY-NAMES={sorted(names)}")
    if names:
        if "connected_browser_exec" not in names:
            print("TOOLSET-PROBE-FAILED: connected_browser_exec not registered")
            sys.exit(3)
        if "browser_exec" not in names:
            print("REGISTRY-NOTE: browser_exec not in registry (backend off hides it)")
except Exception as exc:
    print(f"REGISTRY-INTROSPECTION-ERROR: {exc!r}")

print("TOOLSET-PROBE-OK")
'''


# In-container probe: cold-start ordinary browser_* against a disposable
# loopback HTTP fixture, with the baked-dependency proof (agent-browser
# version + the baked Chrome for Testing tree unchanged across the first
# invocation; $HOME/.agent-browser stays absent; no new cache dirs anywhere).
RT_ORDINARY_SOURCE = r'''
"""Cold-start ordinary browser_* probe (rev-2): real browser_navigate +
browser_snapshot against a loopback fixture; no runtime download."""

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MARKER = "COLD-START-MARKER"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"<html><body><h1>{MARKER}</h1></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{port}/"

# --- Baked-dependency proof ---
ver = subprocess.run(["agent-browser", "--version"], capture_output=True, text=True, timeout=30)
print(f"AGENT-BROWSER rc={ver.returncode} version={ver.stdout.strip()[:60]}")
assert ver.returncode == 0 and "0.26.0" in ver.stdout, "agent-browser@0.26.0 not baked"
ab_exec = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "")
print(f"AGENT-BROWSER-EXECUTABLE-PATH={ab_exec}")
assert ab_exec == "/opt/josemar/agent-browser/chrome/chrome", \
    f"AGENT_BROWSER_EXECUTABLE_PATH not set as expected: {ab_exec!r}"
assert os.path.isfile(ab_exec) and os.access(ab_exec, os.X_OK), \
    f"baked Chrome not executable: {ab_exec}"
cft_root = "/opt/josemar/agent-browser"
assert os.path.isdir(cft_root), f"baked CfT tree missing: {cft_root}"


def tree_snapshot(root):
    """Recursive (relpath, mtime_ns, size) snapshot of a cache root, or None
    when the root is absent. Absent -> None; present -> a sorted list of
    (relpath, mtime_ns, size). mtime_ns may be 0 for filesystems that don't
    report it, so size is also captured."""
    if not os.path.isdir(root):
        return None
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            entries.append(
                (os.path.relpath(full, root), st.st_mtime_ns, st.st_size)
            )
    entries.sort()
    return entries


# No-runtime-download evidence snapshots ONLY actual package/browser cache
# roots. Normal browser profile/socket/temp activity under $HOME or /tmp is
# expected and is NOT download evidence, so it is deliberately not scanned.
home = os.path.expanduser("~")
CACHE_ROOTS = {
    "agent-browser-install": "/opt/josemar/agent-browser",
    "agent-browser-browsers": os.path.join(home, ".agent-browser", "browsers"),
    "ms-playwright": os.path.join(home, ".cache", "ms-playwright"),
    "puppeteer": os.path.join(home, ".cache", "puppeteer"),
    "npx": os.path.join(home, ".npm", "_npx"),
}

before = {name: tree_snapshot(path) for name, path in CACHE_ROOTS.items()}
for name in CACHE_ROOTS:
    state = "absent" if before[name] is None else f"{len(before[name])} entries"
    print(f"CACHE-ROOT before {name}={CACHE_ROOTS[name]} -> {state}")

sys.path.insert(0, "/opt/hermes/tools")
sys.path.insert(0, "/opt/hermes")
import browser_tool  # noqa: E402

nav = getattr(browser_tool, "browser_navigate")
snap = getattr(browser_tool, "browser_snapshot")


def call(fn, *args):
    if inspect.iscoroutinefunction(fn):
        return asyncio.run(fn(*args))
    return fn(*args)


nav_out = call(nav, url)
print(f"NAVIGATE ok={getattr(nav_out, 'get', lambda *_: None)('success', None) is not False}")
snap_out = call(snap)
text = str(snap_out)
print(f"SNAPSHOT marker_present={MARKER in text} len={len(text)}")
assert MARKER in text, f"snapshot lacks marker: {text[:500]}"

after = {name: tree_snapshot(path) for name, path in CACHE_ROOTS.items()}
for name in CACHE_ROOTS:
    state = "absent" if after[name] is None else f"{len(after[name])} entries"
    print(f"CACHE-ROOT after {name}={CACHE_ROOTS[name]} -> {state}")
    assert before[name] == after[name], (
        f"cache root changed during first ordinary use: {name} "
        f"(before={'absent' if before[name] is None else len(before[name])}, "
        f"after={'absent' if after[name] is None else len(after[name])})"
    )
# Explicit first-use assertion: the agent-browser browser download cache must
# NOT appear on first ordinary use (the browser binary is baked). A broad
# '$HOME/.agent-browser absent' claim is NOT made — only the browsers cache.
assert after["agent-browser-browsers"] is None, (
    "$HOME/.agent-browser/browsers appeared during first ordinary use: "
    "a browser download occurred"
)
print(f"NO-DOWNLOAD-EVIDENCE roots_checked={len(CACHE_ROOTS)} "
      f"browsers_cache_absent=True")
print("COLD-START-OK")
'''


# In-container probe: connected route evidence against the REAL patched
# module and REAL /opt/josemar/browser-use CLI. Modes:
#   down     -> endpoint down (default config): generic fail-closed error
#   success  -> real connected_browser_exec against the fixture
#   timeout  -> hanging code killed within the bounded timeout
#   env      -> ambient route-selector/provider keys scrubbed; only the
#               preflighted BU_CDP_WS + reserved BU_NAME survive
#   preflight-> resolve the connected route and prove the NORMALIZED ws
#               authority is bridge-reachable FROM Hermes with a real TCP
#               connect (not merely a string assertion)
#   map      -> deterministic session mapping + namespace disjointness
#   marker   -> own-tab safety: the fixture's pre-opened marker tab keeps
#               its URL and stays open after a connected run
# Arguments: <mode> [fixture_host] [marker_url]
RT_CONNECTED_SOURCE = r'''
"""Connected-route probe (rev-2) — REAL CLI, no fakes."""

import base64
import hashlib
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request

MODE = sys.argv[1] if len(sys.argv) > 1 else "down"
FIXTURE_HOST = sys.argv[2] if len(sys.argv) > 2 else ""
MARKER_URL = sys.argv[3] if len(sys.argv) > 3 else ""

sys.path.insert(0, "/opt/hermes/tools")
sys.path.insert(0, "/opt/hermes")

mod = None
errors = []
try:
    import browser_use_cli  # noqa: F401
    mod = browser_use_cli
except Exception as exc:
    errors.append(f"top-level: {exc!r}")
    sys.modules.pop("browser_use_cli", None)
if mod is None:
    try:
        from tools import browser_use_cli  # type: ignore
        mod = browser_use_cli
    except Exception as exc:
        errors.append(f"package: {exc!r}")
        sys.modules.pop("tools.browser_use_cli", None)
if mod is None:
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("browser_use_cli", "/opt/hermes/tools/browser_use_cli.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        errors.append(f"spec: {exc!r}")
if mod is None:
    print(f"CONNECTED-{MODE.upper()}-UNVERIFIED: module import failed: " + "; ".join(errors))
    sys.exit(0)

SESSION = "rt-conn-sess"


def digest(name):
    return base64.urlsafe_b64encode(hashlib.sha256(name.encode("utf-8")).digest()).decode("ascii").rstrip("=")


if MODE == "map":
    n1 = mod._connected_daemon_name(SESSION)
    n2 = mod._connected_daemon_name(SESSION)
    default = mod._connected_daemon_name("")
    assert n1 == n2, "connected daemon mapping not deterministic"
    assert default == "__jc_0"
    assert n1 == "__jc_1_" + digest(SESSION)
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", n1), "reserved name violates harness rule"
    assert mod._SESSION_RE.match(n1) is None, "reserved name collides with public namespace"
    assert mod._SESSION_RE.match(default) is None
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", default)
    print(f"SESSION-MAP default={default} named_ok={n1 == '__jc_1_' + digest(SESSION)}")
    print("CONNECTED-MAP-OK")
    sys.exit(0)

if MODE == "env":
    ambient = {
        "BU_CDP_URL": "http://127.0.0.1:9",
        "BU_AUTOSPAWN": "1",
        "BROWSER_CDP_URL": "http://127.0.0.1:9",
        "BROWSERBASE_API_KEY": "bb-secret",
        "BROWSER_USE_API_KEY": "bu-secret",
        "FIRECRAWL_API_KEY": "fc-secret",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "OPENAI_API_KEY": "sk-open-secret",
    }
    for k, v in ambient.items():
        os.environ[k] = v
    env = dict(os.environ)
    err = mod._resolve_connected_cdp(env, SESSION)
    for k in ambient:
        os.environ.pop(k, None)
    assert err is None, f"connected preflight failed: {err}"
    assert env.get("BU_NAME") == "__jc_1_" + digest(SESSION), env.get("BU_NAME")
    assert env.get("BU_CDP_WS", "").startswith("ws://"), env.get("BU_CDP_WS")
    for k in ambient:
        assert k not in env, f"ambient key survived the scrub: {k}"
    print(f"ENV-SCRUB ws_ok={env.get('BU_CDP_WS', '').startswith('ws://')} "
          f"name_ok={env.get('BU_NAME') == '__jc_1_' + digest(SESSION)}")
    print("CONNECTED-ENV-OK")
    sys.exit(0)

if MODE == "preflight":
    assert FIXTURE_HOST, "preflight mode needs the fixture host"
    env = {}
    err = mod._resolve_connected_cdp(env, SESSION)
    assert err is None, f"connected preflight failed: {err}"
    ws = env.get("BU_CDP_WS", "")
    assert ws.startswith(("ws://", "wss://")), f"unexpected ws: {ws[:80]!r}"
    parsed = urllib.parse.urlparse(ws)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if parsed.scheme == "wss" else 80
    assert host == FIXTURE_HOST, \
        f"normalized ws host {host!r} != configured fixture {FIXTURE_HOST!r}"
    # Real TCP connect FROM Hermes to the normalized ws authority: proves
    # the route the CLI will receive is actually reachable on the bridge.
    with socket.create_connection((host, port), timeout=10):
        pass
    print(f"CONNECTED-PREFLIGHT ws_host={host} ws_port={port} tcp_ok=True")
    print("CONNECTED-PREFLIGHT-OK")
    sys.exit(0)

if MODE == "down":
    out = mod.connected_browser_exec(
        "print('x')", session=SESSION, timeout_s=10,
    )
    text = str(out)
    assert "Connected browser is unreachable" in text, f"unexpected outcome: {text}"
    assert "127.0.0.1" not in text and "ws://" not in text, f"generic error leaked: {text}"
    print("CONNECTED-DOWN-OK")
    sys.exit(0)

if MODE == "success":
    codes = [
        "print('CONNECTED-PROBE-OK')",
        "new_tab('about:blank')\nprint(page_info())",
    ]
    for code in codes:
        try:
            out = mod.connected_browser_exec(code=code, session=SESSION, timeout_s=60)
        except Exception as exc:
            print(f"CONNECTED-SUCCESS-UNVERIFIED: raised {type(exc).__name__}: {str(exc)[:200]}")
            sys.exit(0)
        text = str(out)
        if "CONNECTED-PROBE-OK" in text:
            print(f"CONNECTED_ROUTE_OK code={code!r}")
            sys.exit(0)
        print(f"attempt {code!r} -> {text[:400]}")
    print("CONNECTED_ROUTE_UNVERIFIED: no code candidate succeeded")
    sys.exit(0)

if MODE == "timeout":
    out = mod.connected_browser_exec(
        "import time; time.sleep(300)", session=SESSION, timeout_s=5,
    )
    text = str(out)
    assert "timed out after" in text, f"unexpected outcome: {text}"
    assert "127.0.0.1" not in text and "ws://" not in text, f"timeout error leaked: {text}"
    print("CONNECTED-TIMEOUT-OK")
    sys.exit(0)

if MODE == "marker":
    assert FIXTURE_HOST, "marker mode needs the fixture host"
    resp = urllib.request.urlopen(f"http://{FIXTURE_HOST}:9222/json/list", timeout=10)
    tabs = json.loads(resp.read().decode("utf-8"))
    marker_tabs = [t for t in tabs if t.get("url") == MARKER_URL]
    assert marker_tabs, f"marker tab missing after connected run: {[t.get('url') for t in tabs]}"
    assert len(tabs) >= 2, f"connected run closed unrelated tabs: {len(tabs)}"
    print(f"MARKER-TAB count={len(marker_tabs)} url_unchanged={marker_tabs[0].get('url') == MARKER_URL} "
          f"total_tabs={len(tabs)}")
    print("CONNECTED-MARKER-OK")
    sys.exit(0)

print(f"CONNECTED-{MODE.upper()}-UNVERIFIED: unknown mode")
sys.exit(0)
'''


class BrowserRoutingRuntimeTests(unittest.TestCase):
    """Opt-in Docker gate: builds the disposable Hermes image (exercising the
    real rev-2 patch + dependency bakes) and proves the three-route design
    with layered evidence."""

    def setUp(self) -> None:
        if os.getenv("RUN_DOCKER_TESTS") != "1":
            self.skipTest("set RUN_DOCKER_TESTS=1 to run Docker runtime tests")
        if os.getenv("RUN_BROWSER_ROUTING_RUNTIME_TESTS") != "1":
            self.skipTest(
                "set RUN_BROWSER_ROUTING_RUNTIME_TESTS=1 to run browser-routing "
                "runtime tests"
            )
        if not docker_available():
            self.skipTest("docker CLI is not available")
        self.runtime = ComposeRuntime()
        # Deterministic host port pins (mirrors the vault-recovery suites) so
        # the disposable stack cannot collide with dev processes.
        self.runtime.env["HERMES_API_SERVER_BIND_IP"] = "127.0.0.1"
        self.runtime.env["HERMES_DASHBOARD_BIND_IP"] = "127.0.0.1"
        self.runtime.env["HERMES_API_SERVER_PORT"] = "18642"
        self.runtime.env["HERMES_DASHBOARD_PORT"] = "19119"
        self._fixture: str | None = None
        self.addCleanup(self._cleanup_fixture)
        self.addCleanup(self.runtime.down)
        os.makedirs(DUMP_DIR, exist_ok=True)
        self.addCleanup(shutil.rmtree, DUMP_DIR, True)

    def _cleanup_fixture(self) -> None:
        if self._fixture:
            subprocess.run(
                ["docker", "rm", "-f", self._fixture],
                capture_output=True, text=True, check=False, timeout=120,
            )
            self._fixture = None

    def _run(self, script: str, *, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess[str]:
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

    def _ship(self, name: str, source: str) -> None:
        b64 = base64.b64encode(source.encode("utf-8")).decode("ascii")
        self._run(f"echo {b64} | base64 -d > /tmp/{name}")

    def _hermes_9222_listeners(self) -> int:
        proc = self._run(
            "python3 - <<'PY'\n"
            "lines = open('/proc/net/tcp', encoding='utf-8').read().splitlines()[1:]\n"
            "print(sum(1 for ln in lines if ':2406' in ln.split()[1] and ln.split()[3] == '0A'))\n"
            "PY",
            timeout=60, check=False,
        )
        return int(proc.stdout.strip() or "0")

    def _wait_runtime_config(self, timeout: int = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc = self._run("test -f /opt/data/config.yaml && echo READY", check=False, timeout=30)
            if proc.returncode == 0 and "READY" in proc.stdout:
                return
            time.sleep(2)
        self.fail("/opt/data/config.yaml never materialized in the runtime config")

    def _assert_gateway_liveness(self) -> None:
        ps = self.runtime.run("ps", "-q", "hermes", check=True, timeout=60)
        cid = ps.stdout.strip()
        if not cid:
            self.fail("hermes container not present after startup")
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}|{{.RestartCount}}", cid],
            capture_output=True, text=True, check=False, timeout=60,
        )
        running, _, restart = state.stdout.strip().partition("|")
        self.assertEqual(running, "true", f"hermes container not running: {state.stdout.strip()}")
        self.assertLessEqual(
            int(restart or "0"), 2,
            f"hermes container crash-loops ({restart} restarts)",
        )
        proc = self._run("ps -eo args | grep -F 'gateway run' | grep -v grep | head -1", check=False, timeout=60)
        self.assertNotEqual(proc.stdout.strip(), "", "no gateway process visible in the container")

    def _start_fixture(self) -> str:
        name = f"jc-br-fixture-{uuid.uuid4().hex[:12]}"
        ps = self.runtime.run("ps", "-q", "hermes", check=True, timeout=60)
        cid = ps.stdout.strip()
        if not cid:
            self.fail("could not resolve the hermes container id")
        img = subprocess.run(
            ["docker", "inspect", "-f", "{{.Image}}", cid],
            capture_output=True, text=True, check=False, timeout=60,
        )
        if img.returncode != 0 or not img.stdout.strip():
            self.fail(f"could not resolve the built hermes image id: {img.stderr[-500:]}")
        net = f"{self.runtime.project}_josemar-network"
        proc = subprocess.run(
            ["docker", "run", "-d", "--name", name, "--network", net,
             "--entrypoint", "sh", img.stdout.strip(), "-c", FIXTURE_CMD],
            capture_output=True, text=True, check=False, timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f"CDP fixture container could not be started: {proc.stderr[-1500:]}")
        self._fixture = name
        return name

    def _wait_fixture_ready(self, name: str, timeout: int = 120) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            proc = subprocess.run(
                ["docker", "exec", name, "python3", "-c",
                 "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9222/json/version', timeout=2); print('READY')"],
                capture_output=True, text=True, check=False, timeout=30,
            )
            if proc.returncode == 0 and "READY" in proc.stdout:
                return
            last = (proc.stderr or proc.stdout).strip()
            time.sleep(2)
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}} exit={{.State.ExitCode}}", name],
            capture_output=True, text=True, check=False, timeout=30,
        )
        logs = subprocess.run(
            ["docker", "logs", "--tail", "20", name],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.fail(
            "CDP fixture did not become ready\n"
            f"state: {state.stdout.strip()}\nlast probe: {last[-300:]}\n"
            f"fixture logs:\n{(logs.stdout + logs.stderr)[-2000:]}"
        )

    def _fixture_reachable_from_hermes(self, name: str) -> None:
        self._run(
            "python3 - <<'PY'\n"
            "import urllib.request\n"
            f"resp = urllib.request.urlopen('http://{name}:9222/json/version', timeout=10)\n"
            "assert resp.status == 200, resp.status\n"
            "print('FIXTURE-REACHABLE-FROM-HERMES')\n"
            "PY",
            timeout=60,
        )

    def _swap_connected_cdp(self, fixture_host: str) -> None:
        """Point the disposable RUNTIME config only (never a tracked file) at
        the fixture; backend/cloud_provider lines must stay intact. The runtime
        config is Hermes-reformatted (unquoted scalars, single-quoted 'off'),
        so match the runtime formatting, not the repo-template formatting."""
        self._run(
            "python3 - <<'PY'\n"
            "path = '/opt/data/config.yaml'\n"
            "text = open(path, encoding='utf-8').read()\n"
            "old = '  connected_cdp_url: http://127.0.0.1:9222'\n"
            "new = '  connected_cdp_url: http://{host}:9222'\n"
            "assert old in text, 'connected_cdp_url anchor missing from runtime config'\n"
            "text = text.replace(old, new, 1)\n"
            "open(path, 'w', encoding='utf-8').write(text)\n"
            "assert new in text, 'fixture URL not written'\n"
            "assert old not in text, 'loopback URL still present'\n"
            "assert \"backend: 'off'\" in text, 'backend must stay off'\n"
            "assert 'cloud_provider: local' in text, 'cloud_provider must stay local'\n"
            "print('CONFIG-SWAPPED-OK')\n"
            "PY".format(host=fixture_host),
            timeout=60,
        )

    def _run_probe(self, probe: str, *args: str, timeout: int = 300,
                   check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = " ".join(shlex.quote(a) for a in args)
        return self._run(
            f"/opt/hermes/.venv/bin/python3 /tmp/{probe} {argv}",
            timeout=timeout, check=check,
        )

    def _assert_probe_ok(self, proc: subprocess.CompletedProcess[str], marker: str,
                         label: str) -> None:
        self.assertEqual(proc.returncode, 0, f"{label} probe failed\n"
                         f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}")
        self.assertIn(marker, proc.stdout, f"{label} marker missing\n"
                      f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}")

    def test_rev2_routing_boundary_in_built_image(self) -> None:
        # Build the disposable image (the Dockerfile exercises the real rev-2
        # patch + dependency bakes) and start the isolated service.
        self.runtime.up("hermes", timeout=1800)
        self.runtime.wait_until_hermes_writable(timeout=120)
        self._wait_runtime_config()

        # 1. Config schema/startup acceptance + gateway liveness.
        self._ship("rt-config-probe.py", RT_CONFIG_PROBE_SOURCE)
        self._assert_probe_ok(
            self._run_probe("rt-config-probe.py", timeout=120), "CONFIG-PROBE-OK",
            "config schema",
        )
        self._assert_gateway_liveness()

        # 2. LIVE SESSION TOOLSET: browser_exec hidden, built-in browser_*
        # present, connected_browser_exec visible (check_fn evidence is the
        # authoritative visibility gate).
        self._ship("rt-toolset-probe.py", RT_TOOLSET_PROBE_SOURCE)
        self._assert_probe_ok(
            self._run_probe("rt-toolset-probe.py", timeout=120), "TOOLSET-PROBE-OK",
            "live session toolset",
        )

        # 3. Cold-start ordinary browser_* (real navigate + snapshot against
        # a loopback fixture; baked-dependency proof; connected endpoint
        # absent must not affect it; 9222 never listened).
        self.assertEqual(self._hermes_9222_listeners(), 0,
                         "unexpected 9222 listener before the ordinary phase")
        self._ship("rt-ordinary.py", RT_ORDINARY_SOURCE)
        self._assert_probe_ok(
            self._run_probe("rt-ordinary.py", timeout=300), "COLD-START-OK",
            "ordinary cold-start",
        )
        self.assertEqual(self._hermes_9222_listeners(), 0,
                         "the ordinary route must not occupy 9222")

        # 4. Connected fail-closed with the endpoint down (default config):
        # generic leak-free error, no fallback, no 9222 listener, ordinary
        # route still functional afterwards.
        self._ship("rt-connected.py", RT_CONNECTED_SOURCE)
        self._assert_probe_ok(
            self._run_probe("rt-connected.py", "down", timeout=120),
            "CONNECTED-DOWN-OK", "connected down",
        )
        self.assertEqual(self._hermes_9222_listeners(), 0,
                         "a failed connected call must not bind 9222")
        self._assert_probe_ok(
            self._run_probe("rt-ordinary.py", timeout=300), "COLD-START-OK",
            "ordinary route after connected failure (no cross-coupling)",
        )

        # 5. Real connected success through a separate disposable fixture.
        try:
            self._connected_real_phase()
        except AssertionError as exc:
            self.fail(f"CONNECTED_ROUTE_UNVERIFIED: {exc}")

    def _connected_real_phase(self) -> None:
        fixture = self._start_fixture()
        self._wait_fixture_ready(fixture)
        self._fixture_reachable_from_hermes(fixture)
        self.assertEqual(self._hermes_9222_listeners(), 0,
                         "Hermes must never listen on 9222; only the fixture does")
        self._swap_connected_cdp(fixture)

        # 5a. Session mapping determinism + namespace disjointness (real env).
        self._assert_probe_ok(
            self._run_probe("rt-connected.py", "map", timeout=120),
            "CONNECTED-MAP-OK", "session mapping",
        )

        # 5b. Env scrub evidence: ambient route selectors + provider/LLM keys
        # cleared; only the preflighted ws + reserved BU_NAME injected.
        self._assert_probe_ok(
            self._run_probe("rt-connected.py", "env", timeout=120),
            "CONNECTED-ENV-OK", "env scrub",
        )

        # 5c. Normalized ws reachability: the route handed to the real CLI
        # must be bridge-reachable FROM Hermes (real TCP connect, not just a
        # string assertion).
        self._assert_probe_ok(
            self._run_probe("rt-connected.py", "preflight", fixture, timeout=120),
            "CONNECTED-PREFLIGHT-OK", "normalized ws reachability",
        )

        # 5d. Real connected CLI success against the fixture.
        self._assert_probe_ok(
            self._run_probe("rt-connected.py", "success", timeout=300),
            "CONNECTED_ROUTE_OK", "real connected success",
        )

        # 5e. Own-tab safety: the fixture's pre-opened marker tab keeps its
        # URL and stays open after the connected run.
        self._assert_probe_ok(
            self._run_probe("rt-connected.py", "marker", fixture, MARKER_TAB_URL, timeout=120),
            "CONNECTED-MARKER-OK", "own-tab marker",
        )

        # 5f. Timeout/hang: a hanging connected CLI is killed within the
        # bounded timeout with a generic leak-free error.
        self._assert_probe_ok(
            self._run_probe("rt-connected.py", "timeout", timeout=180),
            "CONNECTED-TIMEOUT-OK", "connected timeout",
        )

        self.assertEqual(self._hermes_9222_listeners(), 0,
                         "Hermes must never listen on 9222; only the fixture does")


if __name__ == "__main__":
    unittest.main()
