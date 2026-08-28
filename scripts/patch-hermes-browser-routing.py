#!/usr/bin/env python3
"""Patch pinned Hermes v2026.8.18 for the Josemar connected-browser route
(issue #136, revision 2).

Pinned source identity: nousresearch/hermes-agent:v2026.8.18, commit
``e624e9fde561e1add9388384012b295fde669ade``.

Revision 2 replaces revision 1 in full. The revision-1 ``use_connected_browser``
flag on ``browser_exec`` is REMOVED — upstream ``browser_exec`` is left
byte-identical. The deterministic ordinary route is Hermes's built-in
``browser_*`` toolset (``browser.backend: "off"`` + ``cloud_provider: "local"``
in the Josemar config keep upstream ``browser_exec`` hidden and the built-in
surface local), and a NEW model-visible tool ``connected_browser_exec`` is
registered for the externally connected operator browser:

  - Registered in the ``browser`` toolset (coexists with the built-in
    ``browser_*`` tools; ``get_toolset``/``resolve_toolset`` merge
    registry-registered tools into the static toolset definition, so no
    toolsets.py edit is needed).
  - Schema: ``code`` (required), optional ``session`` and ``timeout_s``,
    following the pinned Browser Use code-execution contract. The description
    states it controls the optional externally connected operator browser for
    existing authenticated/session-dependent state and is NOT the default
    research/browser route.
  - check_fn ``is_connected_browser_configured`` inspects static config
    (``browser.connected_cdp_url`` present in the raw config) and executable
    presence (``/opt/josemar/browser-use/bin/browser-use``) ONLY — no
    network/CDP probe during schema assembly, so an offline laptop leaves the
    tool visible and invocation returns actionable connection guidance.
  - Connected routing: reads ONLY ``browser.connected_cdp_url`` (raw config,
    never the ``_get_cdp_override`` chain / ``BROWSER_CDP_URL`` / global
    ``browser.cdp_url``), preflights it via a plain ``/json/version`` GET
    (never spawns or binds a browser), requires a valid
    ``webSocketDebuggerUrl``, and injects that exact websocket as the ONLY
    ``BU_CDP_WS`` plus the reserved ``BU_NAME`` in the per-call env. A
    loopback-reported websocket authority (``127.0.0.1``/``localhost``/
    ``[::1]``) is normalized to the configured endpoint authority ONLY when
    the configured endpoint host is non-loopback (remote/bridge CDP
    endpoint); the production ``127.0.0.1:9222`` layout is untouched.
    ``os.environ`` is never touched. The normal provider/CDP auto-resolution
    is never invoked.
  - Connected env scrubbing is route-selector scrubbing, not secret
    scrubbing: the local call env is cleared of route selectors (``BU_*``
    incl. ``BU_CDP_WS``/``BU_CDP_URL``/``BU_AUTOSPAWN``, and
    ``BROWSER_CDP_URL``) and browser-provider/LLM credential keys
    (Browserbase/Browser Use/Firecrawl keys, ``ANTHROPIC_API_KEY``,
    ``OPENAI_API_KEY``). The subprocess env otherwise inherits the container
    environment — other secrets (e.g. ``DEEPSEEK_API_KEY``, ``GBRAIN_*``,
    ``MNEMOSYNE_*``) remain visible to the model-sent code. That is
    acceptable because ``connected_browser_exec`` is held behind the same
    session-level terminal gate as ``browser_exec``; the terminal gate is
    the controlling boundary, not the env scrub.
  - Invokes the build-owned CLI at the ABSOLUTE path
    ``/opt/josemar/browser-use/bin/browser-use`` — never ``_find_cli()``'s
    uvx fallback.
  - Preserves the pinned shared-CDP own-tab/ownership preamble
    (``_OWN_TAB_PREAMBLE``) so the task does not enumerate or commandeer
    unrelated tabs in the operator's browser.
  - Missing/malformed/unreachable/disappearing connected endpoint produces a
    connected-browser-specific generic failure (no endpoint/websocket/stderr
    leakage) with NO fallback to ordinary ``browser_*``, Browser Use cloud,
    Browserbase, or another browser. A connected subprocess failure is also
    surfaced generically.
  - Session isolation: public ``session`` stays validated by the pinned
    ``_SESSION_RE`` and is echoed back as the result's public ``session``.
    Connected sessions map deterministically into a reserved UNDERSCORE-
    LEADING ``BU_NAME`` namespace — ``__jc_0`` for the omitted public session
    (distinct connected default), ``__jc_1_`` + the 43-char URL-safe base64
    of the full SHA-256 digest of the public session for named sessions
    (total 50 <= 64). The internal ``BU_NAME`` is consumed only by
    browser-harness (rule ``\\A[A-Za-z0-9_-]{1,64}\\Z``, leading underscore
    allowed) and never round-trips through Hermes ``_SESSION_RE`` (only the
    public ``session`` argument is validated there). Because ``_SESSION_RE``
    requires an ALPHANUMERIC first character
    (``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$``), the reserved underscore-leading
    namespace is MECHANICALLY disjoint from every valid public session name —
    and therefore from every normal upstream daemon BU_NAME (the ordinary
    route's BU_NAME equals the public session verbatim) — with zero runtime
    guards, so no cross-route daemon/state collision is possible.
  - model_tools.py: the pinned session-level terminal gate (literal-name
    list) is extended so sessions without the ``terminal`` surface cannot
    regain host code execution through ``connected_browser_exec`` either.
  - Fail-loud upstream-symbol assertions: the connected tool body depends on
    upstream-private symbols (``_base_subprocess_env``, ``_MIN_TIMEOUT_S``,
    ``_OWN_TAB_PREAMBLE``, ``_read_browser_cfg``, ``_workspace_dir``,
    ``_blocked_url_in_code``, ``_STDERR_CAP_CHARS``, ``_MAX_TIMEOUT_S``,
    ``_DEFAULT_TIMEOUT_S``, ``_find_screenshot``,
    ``_native_screenshot_result``) that are not covered by the replace_once
    anchors. ``py_compile`` catches only syntax; a renamed upstream symbol
    would fail at runtime. The patch therefore asserts every load-bearing
    symbol's definition needle against the PRISTINE source before any
    replacement and aborts the build loudly if any is missing.

Fail-fast contract (mirrors ``patch-hermes-dashboard-profile-name.py`` and
``patch-hermes-skills-config.py``):

  - Each ``replace_once`` raises if the expected snippet is missing (source
    shape changed upstream).
  - A duplicate application raises because the first anchor is already
    replaced.
  - The upstream-symbol assertion runs first and fails the build if any
    load-bearing private symbol is missing from the pristine source.
"""

from __future__ import annotations

import sys
from pathlib import Path

BROWSER_USE_CLI_PATH = Path("/opt/hermes/tools/browser_use_cli.py")
MODEL_TOOLS_PATH = Path("/opt/hermes/model_tools.py")

# Load-bearing upstream-private symbols the inserted connected tool body
# depends on but that are NOT covered by the replace_once anchors. Each entry
# is a definition needle checked against the PRISTINE source (before any
# replacement, so the inserted code cannot mask a renamed/missing upstream
# symbol). py_compile only catches syntax; a renamed symbol would fail at
# runtime, so the build must fail here instead.
UPSTREAM_SYMBOL_NEEDLES: tuple[str, ...] = (
    "def _base_subprocess_env(",
    "_MIN_TIMEOUT_S = ",
    "_OWN_TAB_PREAMBLE = ",
    "def _read_browser_cfg(",
    "def _workspace_dir(",
    "def _blocked_url_in_code(",
    "_STDERR_CAP_CHARS = ",
    "_MAX_TIMEOUT_S = ",
    "_DEFAULT_TIMEOUT_S = ",
    "def _find_screenshot(",
    "def _native_screenshot_result(",
)


def assert_upstream_symbols(path: Path) -> None:
    """Fail loudly if any load-bearing upstream symbol is missing.

    Runs on the pristine source before any replacement so the inserted
    connected tool body cannot mask a renamed/missing upstream symbol.
    """
    text = path.read_text(encoding="utf-8")
    missing = [needle for needle in UPSTREAM_SYMBOL_NEEDLES if needle not in text]
    if missing:
        raise RuntimeError(
            f"Load-bearing upstream symbols missing in {path}: {missing}. "
            "The pinned source shape changed; re-verify before building."
        )


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Expected snippet not found in {path}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def apply_patches(browser_use_cli_path: Path, model_tools_path: Path) -> None:
    # 0. Fail-loud upstream-symbol assertions on the pristine source (B4):
    #    the connected tool body depends on these private symbols; a renamed
    #    upstream symbol would otherwise fail at runtime, not at build time.
    assert_upstream_symbols(browser_use_cli_path)
    # --- tools/browser_use_cli.py -----------------------------------------
    # 1. Imports: base64 + hashlib (daemon digest), urllib.request +
    #    urlparse (connected CDP preflight). Stdlib only, like the rest of
    #    the module.
    replace_once(
        browser_use_cli_path,
        'import json\n'
        'import logging\n'
        'import os\n'
        'import re\n'
        'import shutil\n'
        'import subprocess\n'
        'import time\n'
        'from pathlib import Path\n'
        'from typing import Any, Dict, List, Optional, Tuple\n'
        '\n'
        'from utils import is_truthy_value\n',
        'import base64\n'
        'import hashlib\n'
        'import json\n'
        'import logging\n'
        'import os\n'
        'import re\n'
        'import shutil\n'
        'import subprocess\n'
        'import time\n'
        'import urllib.request\n'
        'from pathlib import Path\n'
        'from typing import Any, Dict, List, Optional, Tuple\n'
        'from urllib.parse import urlparse, urlunparse\n'
        '\n'
        'from utils import is_truthy_value\n',
    )

# 2. Reserved connected daemon namespace constants + digest helpers,
    #    right after the public session regex. Council finding (rev-2
    #    correction): the internal BU_NAME is consumed only by
    #    browser-harness (\A[A-Za-z0-9_-]{1,64}\Z, leading underscore
    #    allowed); it never round-trips through Hermes _SESSION_RE (only the
    #    public `session` argument is validated there, and it is echoed back
    #    as the result's public session). Underscore-leading names are
    #    therefore MECHANICALLY disjoint from every _SESSION_RE-valid public
    #    session name — and from every normal upstream daemon BU_NAME (which
    #    equals the public session verbatim) — with zero runtime guards.
    replace_once(
        browser_use_cli_path,
        '# Cloud daemon names become the BU_NAME env var\n'
        '_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")\n',
        '# Cloud daemon names become the BU_NAME env var\n'
        '_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")\n'
        '\n'
        '# Reserved BU_NAME daemon namespace for connected_browser_exec\n'
        '# (issue #136). The internal BU_NAME is consumed only by\n'
        '# browser-harness (rule \\A[A-Za-z0-9_-]{1,64}\\Z, leading underscore\n'
        '# allowed) and never round-trips through Hermes _SESSION_RE — only\n'
        '# the public `session` argument is validated there, and it is echoed\n'
        '# back as the result\'s public session. Because _SESSION_RE requires\n'
        '# an ALPHANUMERIC first character (^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$),\n'
        '# the reserved underscore-leading namespace is MECHANICALLY disjoint\n'
        '# from every valid public session name — and therefore from every\n'
        '# normal upstream daemon BU_NAME (the ordinary route\'s BU_NAME equals\n'
        '# the public session verbatim) — with zero runtime guards:\n'
        '#   public session omitted -> __jc_0 (distinct connected default)\n'
        '#   public session "foo"   -> __jc_1_<43-char URL-safe base64 of the\n'
        '#                             full SHA-256 digest of "foo">\n'
        '# Named total length is 7 + 43 = 50 (<= 64), deterministic, and\n'
        '# collision-safe against truncated/same-prefix public names.\n'
        '# Build-owned Browser Use CLI (absolute path; never _find_cli()/uvx).\n'
        '_CONNECTED_BROWSER_CLI = "/opt/josemar/browser-use/bin/browser-use"\n'
        '_CONNECTED_DAEMON_DEFAULT = "__jc_0"\n'
        '_CONNECTED_DAEMON_NAMED_PREFIX = "__jc_1_"\n'
        '\n'
        '\n'
        'def _connected_daemon_digest(public_session: str) -> str:\n'
        '    """Full SHA-256 digest of a public session, URL-safe base64 (43 chars)."""\n'
        '    return (\n'
        '        base64.urlsafe_b64encode(\n'
        '            hashlib.sha256(public_session.encode("utf-8")).digest()\n'
        '        )\n'
        '        .decode("ascii")\n'
        '        .rstrip("=")\n'
        '    )\n'
        '\n'
        '\n'
        'def _connected_daemon_name(public_session: str) -> str:\n'
        '    """Reserved, deterministic BU_NAME for a connected session."""\n'
        '    if public_session:\n'
        '        return _CONNECTED_DAEMON_NAMED_PREFIX + _connected_daemon_digest(public_session)\n'
        '    return _CONNECTED_DAEMON_DEFAULT\n',
    )

    # 3. The connected tool implementation, inserted between browser_exec
    #    and _HEADER_BASE. browser_exec itself is NOT modified.
    replace_once(
        browser_use_cli_path,
        '    return tool_result(result)\n'
        '\n'
        '\n'
        '# The tool description is the CLI\'s skill, fetched from browser-use skill\n'
        '_HEADER_BASE = (\n',
        '    return tool_result(result)\n'
        '\n'
        '\n'
        '# ---------------------------------------------------------------------------\n'
        '# connected_browser_exec: the externally connected operator browser\n'
        '# (issue #136). SEPARATE from browser_exec: it drives ONLY the operator\n'
        '# browser exposed via browser.connected_cdp_url (browser-control overlay /\n'
        '# laptop tunnel), fails closed when that route is unavailable, and NEVER\n'
        '# falls back to the ordinary server browser, Browser Use cloud,\n'
        '# Browserbase, or any other browser. The ordinary server-headless route is\n'
        '# the built-in browser_* toolset (browser.backend: "off" keeps upstream\n'
        '# browser_exec hidden).\n'
        '# ---------------------------------------------------------------------------\n'
        '_CONNECTED_PREFLIGHT_TIMEOUT_S = 10\n'
        '\n'
        '\n'
        'def _connected_cdp_endpoint() -> str:\n'
        '    """Return the configured connected endpoint (Josemar-owned key).\n'
        '\n'
        '    Reads ONLY ``browser.connected_cdp_url`` straight from the raw config\n'
        '    file (via ``_read_browser_cfg``) — never the ``_get_cdp_override``\n'
        '    chain (BROWSER_CDP_URL env / global ``browser.cdp_url``), never\n'
        '    providers, never local Chrome. Returns "" when absent or not a\n'
        '    nonempty string.\n'
        '    """\n'
        '    try:\n'
        '        cfg = _read_browser_cfg()\n'
        '    except Exception:\n'
        '        cfg = {}\n'
        '    endpoint = cfg.get("connected_cdp_url")\n'
        '    if not isinstance(endpoint, str):\n'
        '        return ""\n'
        '    return endpoint.strip()\n'
        '\n'
        '\n'
        'def is_connected_browser_configured() -> bool:\n'
        '    """Static availability for check_fn: config key + build-owned CLI present.\n'
        '\n'
        '    NO network/CDP probe during schema assembly: an offline laptop must\n'
        '    leave the tool visible so invocation can return actionable connection\n'
        '    guidance. Both inputs are stable process-wide, so the TTL-cached\n'
        '    check_fn result stays valid.\n'
        '    """\n'
        '    if not Path(_CONNECTED_BROWSER_CLI).is_file():\n'
        '        return False\n'
        '    try:\n'
        '        return bool(_connected_cdp_endpoint())\n'
        '    except Exception:\n'
        '        return False\n'
        '\n'
        '\n'
        'def _resolve_connected_cdp(env: dict, session: str = "") -> Optional[str]:\n'
        '    """Preflight the connected endpoint and prepare the per-call env.\n'
        '\n'
        '    On success env carries ONLY the preflighted ``BU_CDP_WS`` plus the\n'
        '    reserved ``BU_NAME``; route selectors and browser-provider/LLM\n'
        '    credential keys are cleared first (local call env only —\n'
        '    ``os.environ`` is never touched). This is route-selector\n'
        '    scrubbing, not secret scrubbing: the subprocess env otherwise\n'
        '    inherits the container environment; the session-level terminal\n'
        '    gate (model_tools.py) is the controlling boundary for the\n'
        '    model-sent code. Returns a generic connected-route error string\n'
        '    on any failure; the error never contains the endpoint, the CDP\n'
        '    response, or session data, and nothing is logged here.\n'
        '    """\n'
        '    endpoint = _connected_cdp_endpoint()\n'
        '    if not endpoint:\n'
        '        return (\n'
        '            "Connected browser is not configured: set browser.connected_cdp_url "\n'
        '            "in config.yaml and restart Hermes."\n'
        '        )\n'
        '    try:\n'
        '        parsed = urlparse(endpoint)\n'
        '        if parsed.scheme not in ("http", "https") or not parsed.netloc:\n'
        '            return (\n'
        '                "Connected browser configuration is invalid: the configured "\n'
        '                "endpoint is not an HTTP(S) CDP discovery URL. Fix "\n'
        '                "browser.connected_cdp_url in config.yaml and restart Hermes."\n'
        '            )\n'
        '    except Exception:\n'
        '        return (\n'
        '            "Connected browser configuration is invalid: the configured "\n'
        '            "endpoint could not be parsed. Fix browser.connected_cdp_url in "\n'
        '            "config.yaml and restart Hermes."\n'
        '        )\n'
        '    try:\n'
        '        with urllib.request.urlopen(\n'
        '            endpoint.rstrip("/") + "/json/version",\n'
        '            timeout=_CONNECTED_PREFLIGHT_TIMEOUT_S,\n'
        '        ) as resp:\n'
        '            payload = json.loads(resp.read().decode("utf-8", errors="replace"))\n'
        '        ws_url = payload.get("webSocketDebuggerUrl")\n'
        '        if not isinstance(ws_url, str):\n'
        '            ws_url = ""\n'
        '        ws_url = ws_url.strip()\n'
        '        ws_parsed = urlparse(ws_url)\n'
        '        if ws_parsed.scheme not in ("ws", "wss") or not ws_parsed.netloc:\n'
        '            return (\n'
        '                "Connected browser is unreachable: its CDP discovery endpoint "\n'
        '                "did not report a usable debugger websocket. Verify the "\n'
        '                "connected browser is running and retry."\n'
        '            )\n'
        '    except Exception:\n'
        '        return (\n'
        '            "Connected browser is unreachable: its CDP discovery endpoint did "\n'
        '            "not respond with a usable debugger websocket. Verify the "\n'
        '            "connected browser is running and retry."\n'
        '        )\n'
        '    # Loopback ws authority normalization: Chrome\'s /json/version echoes\n'
        '    # a loopback websocket authority (127.0.0.1/localhost/[::1]) even when\n'
        '    # the discovery endpoint was reached via a non-loopback\n'
        '    # (remote/bridge) host. Rewrite the returned websocket authority to\n'
        '    # the configured endpoint authority in that case ONLY, so the CLI\n'
        '    # never targets this host\'s loopback for a remote/bridge endpoint.\n'
        '    # The production config is 127.0.0.1:9222 (loopback), so this is a\n'
        '    # no-op there; nothing is logged here.\n'
        '    ws_url = _normalize_connected_ws_url(ws_url, endpoint)\n'
        '    # Clear ALL conflicting route selectors and provider/credential keys\n'
        '    # from this local call env (never os.environ): ambient BU_* (BU_CDP_WS,\n'
        '    # BU_CDP_URL, BU_AUTOSPAWN, ...), BROWSER_CDP_URL, and the browser/LLM\n'
        '    # provider keys the sanitization boundary may pass through. The normal\n'
        '    # provider/CDP auto-resolution is never invoked; the connected endpoint\n'
        '    # is forced.\n'
        '    for key in list(env):\n'
        '        if key.startswith("BU_"):\n'
        '            env.pop(key, None)\n'
        '    for key in (\n'
        '        "BROWSER_CDP_URL",\n'
        '        "BROWSERBASE_API_KEY",\n'
        '        "BROWSERBASE_PROJECT_ID",\n'
        '        "BROWSER_USE_API_KEY",\n'
        '        "FIRECRAWL_API_KEY",\n'
        '        "FIRECRAWL_API_URL",\n'
        '        "FIRECRAWL_BROWSER_TTL",\n'
        '        "ANTHROPIC_API_KEY",\n'
        '        "OPENAI_API_KEY",\n'
        '    ):\n'
        '        env.pop(key, None)\n'
        '    env["BU_NAME"] = _connected_daemon_name(session)\n'
        '    env["BU_CDP_WS"] = ws_url\n'
        '    return None\n'
        '\n'
        '\n'
        'def _normalize_connected_ws_url(ws_url: str, endpoint: str) -> str:\n'
        '    """Rewrite a loopback-reported CDP websocket authority to the\n'
        '    configured endpoint authority when the endpoint is non-loopback.\n'
        '\n'
        '    Chrome\'s /json/version echoes a loopback websocket authority\n'
        '    (127.0.0.1/localhost/[::1]) even when the discovery endpoint was\n'
        '    reached via a non-loopback host (e.g. a remote/bridge CDP endpoint).\n'
        '    Injecting that URL verbatim would make the CLI target THIS host\'s\n'
        '    loopback. When the configured endpoint host is itself loopback (the\n'
        '    production 127.0.0.1:9222 layout), the report is correct and the URL\n'
        '    is returned unchanged. The ws/wss scheme and the path/query are\n'
        '    preserved; malformed inputs are returned unchanged (the caller\'s\n'
        '    validation already rejected non-ws schemes). Nothing is logged;\n'
        '    any error degrades to the unchanged URL.\n'
        '    """\n'
        '    try:\n'
        '        ws_parsed = urlparse(ws_url)\n'
        '        endpoint_parsed = urlparse(endpoint)\n'
        '        if ws_parsed.scheme not in ("ws", "wss") or not ws_parsed.netloc:\n'
        '            return ws_url\n'
        '        if endpoint_parsed.scheme not in ("http", "https") or not endpoint_parsed.netloc:\n'
        '            return ws_url\n'
        '        ws_host = (ws_parsed.hostname or "").lower()\n'
        '        endpoint_host = (endpoint_parsed.hostname or "").lower()\n'
        '        if ws_host not in ("127.0.0.1", "localhost", "::1"):\n'
        '            return ws_url\n'
        '        if endpoint_host in ("127.0.0.1", "localhost", "::1"):\n'
        '            return ws_url\n'
        '        endpoint_port = endpoint_parsed.port\n'
        '        if endpoint_port is None:\n'
        '            endpoint_port = 443 if endpoint_parsed.scheme == "https" else 80\n'
        '        if ":" in endpoint_host and not endpoint_host.startswith("["):\n'
        '            netloc = f"[{endpoint_host}]:{endpoint_port}"\n'
        '        else:\n'
        '            netloc = f"{endpoint_host}:{endpoint_port}"\n'
        '        return urlunparse(\n'
        '            (ws_parsed.scheme, netloc, ws_parsed.path,\n'
        '             ws_parsed.params, ws_parsed.query, ws_parsed.fragment)\n'
        '        )\n'
        '    except Exception:\n'
        '        return ws_url\n'
        '\n'
        '\n'
        'def connected_browser_exec(\n'
        '    code: str,\n'
        '    session: str = "",\n'
        '    timeout_s: int = _DEFAULT_TIMEOUT_S,\n'
        '    task_id: Optional[str] = None,\n'
        '):\n'
        '    """Run Python code through the browser-use CLI against the connected browser."""\n'
        '    from tools.registry import tool_error, tool_result\n'
        '\n'
        '    if not code or not code.strip():\n'
        '        return tool_error("No code provided. Pass Python that uses the pre-imported helpers, e.g. new_tab(\\"https://example.com\\") then print(page_info()).")\n'
        '\n'
        '    blocked = _blocked_url_in_code(code)\n'
        '    if blocked:\n'
        '        return tool_error(blocked)\n'
        '\n'
        '    if not Path(_CONNECTED_BROWSER_CLI).is_file():\n'
        '        return tool_error(\n'
        '            "Connected browser CLI is not available in this image "\n'
        '            "(/opt/josemar/browser-use/bin/browser-use is missing). Rebuild "\n'
        '            "the image with the pinned browser-use environment."\n'
        '        )\n'
        '\n'
        '    env = _base_subprocess_env()\n'
        '    if session:\n'
        '        if not _SESSION_RE.match(session):\n'
        '            return tool_error(\n'
        '                f"Invalid session name {session!r}: use 1-64 letters, digits, "\n'
        '                "dashes, or underscores (e.g. \'r7k2\')."\n'
        '            )\n'
        '\n'
        '    connected_err = _resolve_connected_cdp(env, session)\n'
        '    if connected_err:\n'
        '        return tool_error(connected_err)\n'
        '\n'
        '    # Shared external browser: pin this daemon to a tab it created before\n'
        '    # running the model\'s code (the same protection browser_exec applies on\n'
        '    # shared CDP browsers), so the task does not enumerate or commandeer\n'
        '    # unrelated tabs. The connected route always runs under a reserved\n'
        '    # BU_NAME, so the preamble always applies.\n'
        '    code = _OWN_TAB_PREAMBLE + code\n'
        '\n'
        '    workspace = _workspace_dir(task_id)\n'
        '    if workspace:\n'
        '        env["BH_AGENT_WORKSPACE"] = workspace\n'
        '\n'
        '    try:\n'
        '        timeout = max(_MIN_TIMEOUT_S, min(int(timeout_s), _MAX_TIMEOUT_S))\n'
        '    except (TypeError, ValueError):\n'
        '        timeout = _DEFAULT_TIMEOUT_S\n'
        '\n'
        '    popen_extra: dict = {}\n'
        '    if os.name == "nt":\n'
        '        try:\n'
        '            from hermes_cli._subprocess_compat import windows_hide_flags\n'
        '\n'
        '            popen_extra["creationflags"] = windows_hide_flags()\n'
        '            _si = subprocess.STARTUPINFO()\n'
        '            _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW\n'
        '            popen_extra["startupinfo"] = _si\n'
        '        except Exception as e:\n'
        '            logger.debug("Windows hide-flags unavailable: %s", e)\n'
        '\n'
        '    started = time.time()\n'
        '    try:\n'
        '        proc = subprocess.run(\n'
        '            [_CONNECTED_BROWSER_CLI],\n'
        '            input=code,\n'
        '            capture_output=True,\n'
        '            text=True,\n'
        '            timeout=timeout,\n'
        '            env=env,\n'
        '            **popen_extra,\n'
        '        )\n'
        '    except subprocess.TimeoutExpired:\n'
        '        return tool_error(\n'
        '            f"Connected browser exec timed out after {timeout}s. The daemon may "\n'
        '            "still be working; retry with a larger timeout_s (max "\n'
        '            f"{_MAX_TIMEOUT_S}), or split the work into several calls that "\n'
        '            "append to workspace files — anything already written to the "\n'
        '            "workspace is preserved."\n'
        '        )\n'
        '    except OSError as e:\n'
        '        return tool_error(f"Failed to launch the connected browser CLI: {e}")\n'
        '\n'
        '    if proc.returncode != 0:\n'
        '        # Generic connected-route failure: the CLI stderr may echo the\n'
        '        # endpoint or websocket it attached to. NO fallback to the ordinary\n'
        '        # server browser, Browser Use cloud, Browserbase, or another browser.\n'
        '        return tool_error(\n'
        '            "Connected browser exec failed: the remote browser did not "\n'
        '            "complete the request. Verify the connected browser is running "\n'
        '            "and retry. No fallback to the ordinary server browser or a "\n'
        '            "cloud browser is performed."\n'
        '        )\n'
        '\n'
        '    result = {\n'
        '        "success": True,\n'
        '        "exit_code": proc.returncode,\n'
        '        "output": proc.stdout,\n'
        '    }\n'
        '    if workspace:\n'
        '        result["workspace"] = workspace\n'
        '    if session:\n'
        '        result["session"] = session\n'
        '    stderr = (proc.stderr or "").strip()\n'
        '    if stderr:\n'
        '        if len(stderr) > _STDERR_CAP_CHARS:\n'
        '            stderr = stderr[:_STDERR_CAP_CHARS] + "\\n… (stderr truncated)"\n'
        '        result["stderr"] = stderr\n'
        '\n'
        '    screenshot = _find_screenshot(proc.stdout, started)\n'
        '    if screenshot:\n'
        '        result["screenshot_path"] = screenshot\n'
        '        native = _native_screenshot_result(result, screenshot)\n'
        '        if native is not None:\n'
        '            return native\n'
        '    return tool_result(result)\n'
        '\n'
        '\n'
        'CONNECTED_BROWSER_EXEC_SCHEMA = {\n'
        '    "name": "connected_browser_exec",\n'
        '    "description": (\n'
        '        "Controls the optional externally connected operator browser. Drives "\n'
        '        "ONLY the browser exposed via browser.connected_cdp_url (the "\n'
        '        "browser-control overlay / laptop tunnel); it is intended for work "\n'
        '        "that needs the operator\'s existing authenticated/session-dependent "\n'
        '        "state and should be used only when the operator asks for their "\n'
        '        "browser or session state matters. It is NOT the default "\n'
        '        "research/browser route: for ordinary interactive/rendered web work "\n'
        '        "use the built-in browser_* tools, and for simple public facts "\n'
        '        "prefer search/extraction tools. The connected browser must be "\n'
        '        "running and reachable; when it is not, calls fail closed with "\n'
        '        "guidance and never fall back to the server browser or a cloud "\n'
        '        "browser. The `code` argument is piped verbatim to the browser-use "\n'
        '        "CLI on stdin and executed as full Python (standard library "\n'
        '        "available) with the CLI\'s pre-imported browser helpers; stdout "\n'
        '        "comes back in the result. STATE: the connected browser session and "\n'
        '        "the workspace persist across calls; Python variables do NOT (each "\n'
        '        "call is a fresh interpreter). Pass session=<name> (never BU_NAME "\n'
        '        "env syntax) for an isolated named session and reuse the same name "\n'
        '        "on every related call."\n'
        '    ),\n'
        '    "parameters": {\n'
        '        "type": "object",\n'
        '        "properties": {\n'
        '            "code": {\n'
        '                "type": "string",\n'
        '                "description": "Python code to execute using the pre-imported browser helpers. Use print(...) for any data you need back.",\n'
        '            },\n'
        '            "session": {\n'
        '                "type": "string",\n'
        '                "description": "Named isolated connected-browser session: each name gets its own harness daemon pinned to a tab it created, so concurrent tasks don\'t clobber each other. Omit for the shared connected default session. Reuse the same name across calls to keep working in that session.",\n'
        '            },\n'
        '            "timeout_s": {\n'
        '                "type": "integer",\n'
        '                "description": f"Max seconds to wait for the code to finish (default {_DEFAULT_TIMEOUT_S}, max {_MAX_TIMEOUT_S}).",\n'
        '                "default": _DEFAULT_TIMEOUT_S,\n'
        '            },\n'
        '        },\n'
        '        "required": ["code"],\n'
        '    },\n'
        '}\n'
        '\n'
        '\n'
        '# The tool description is the CLI\'s skill, fetched from browser-use skill\n'
        '_HEADER_BASE = (\n',
    )

    # 4. Register connected_browser_exec AFTER the existing browser_exec
    #    registration (the ``from tools.registry import registry`` import sits
    #    just above it). Toolset "browser" makes it coexist with the built-in
    #    browser_* tools; resolve_toolset() merges registry registrations into
    #    the static toolset definition, so no toolsets.py edit is needed.
    replace_once(
        browser_use_cli_path,
        '    check_fn=is_browser_use_cli_mode,\n'
        '    dynamic_schema_overrides=_dynamic_schema_overrides,\n'
        '    emoji="🌐",\n'
        ')\n',
        '    check_fn=is_browser_use_cli_mode,\n'
        '    dynamic_schema_overrides=_dynamic_schema_overrides,\n'
        '    emoji="🌐",\n'
        ')\n'
        '\n'
        '\n'
        'registry.register(\n'
        '    name="connected_browser_exec",\n'
        '    toolset="browser",\n'
        '    schema=CONNECTED_BROWSER_EXEC_SCHEMA,\n'
        '    handler=lambda args, **kw: connected_browser_exec(\n'
        '        code=args.get("code", ""),\n'
        '        session=args.get("session", "") or "",\n'
        '        timeout_s=args.get("timeout_s", _DEFAULT_TIMEOUT_S),\n'
        '        task_id=kw.get("task_id"),\n'
        '    ),\n'
        '    check_fn=is_connected_browser_configured,\n'
        '    emoji="🌐",\n'
        ')\n',
    )

    # --- model_tools.py ----------------------------------------------------
    # 5. Session-level terminal gate: connected_browser_exec runs arbitrary
    #    Python on the host via the browser-use CLI subprocess, exactly like
    #    browser_exec, so it must be held behind the same literal-name gate —
    #    a session whose toolset selection excludes the terminal surface must
    #    not regain host code execution through the browser toolset.
    replace_once(
        model_tools_path,
        '    # browser_exec (Browser Use mode) runs arbitrary Python on the host via\n'
        '    # the browser-use CLI subprocess.  A session whose toolset selection\n'
        '    # excludes the terminal surface (e.g. a messaging platform configured\n'
        '    # without terminal access) must not regain host code execution through\n'
        '    # the browser toolset — that would silently widen the operator\'s chosen\n'
        '    # security posture.  Session-level gate, NOT a check_fn: check_fn results\n'
        '    # are TTL-cached process-wide while one gateway process serves many\n'
        '    # sessions with different toolset configs.\n'
        '    if "browser_exec" in available_tool_names and "terminal" not in available_tool_names:\n'
        '        filtered_tools = [\n'
        '            td for td in filtered_tools\n'
        '            if td.get("function", {}).get("name") != "browser_exec"\n'
        '        ]\n'
        '        available_tool_names.discard("browser_exec")\n',
        '    # browser_exec (Browser Use mode) and connected_browser_exec run\n'
        '    # arbitrary Python on the host via the browser-use CLI subprocess.  A\n'
        '    # session whose toolset selection excludes the terminal surface (e.g. a\n'
        '    # messaging platform configured without terminal access) must not regain\n'
        '    # host code execution through the browser toolset — that would silently\n'
        '    # widen the operator\'s chosen security posture.  Session-level gate, NOT\n'
        '    # a check_fn: check_fn results are TTL-cached process-wide while one\n'
        '    # gateway process serves many sessions with different toolset configs.\n'
        '    if (\n'
        '        "browser_exec" in available_tool_names\n'
        '        or "connected_browser_exec" in available_tool_names\n'
        '    ) and "terminal" not in available_tool_names:\n'
        '        filtered_tools = [\n'
        '            td for td in filtered_tools\n'
        '            if td.get("function", {}).get("name")\n'
        '            not in ("browser_exec", "connected_browser_exec")\n'
        '        ]\n'
        '        available_tool_names.discard("browser_exec")\n'
        '        available_tool_names.discard("connected_browser_exec")\n',
    )


def main() -> None:
    browser_use_cli_path = Path(sys.argv[1]) if len(sys.argv) > 1 else BROWSER_USE_CLI_PATH
    model_tools_path = Path(sys.argv[2]) if len(sys.argv) > 2 else MODEL_TOOLS_PATH
    apply_patches(browser_use_cli_path, model_tools_path)
    print(
        "Patched Hermes connected-browser routing (issue #136 rev2): "
        f"{browser_use_cli_path}, {model_tools_path}"
    )


if __name__ == "__main__":
    main()