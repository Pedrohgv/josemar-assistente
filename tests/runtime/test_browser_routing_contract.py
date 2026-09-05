"""Fast, Docker-free contract tests for the Hermes rev-2 browser routing
(issue #136).

Revision 2 replaces revision 1 in full: no ``use_connected_browser`` flag.
The deterministic ordinary route is Hermes's built-in ``browser_*`` toolset
(``browser.backend: "off"`` + ``cloud_provider: "local"`` keep upstream
``browser_exec`` hidden and the built-in surface local), and a NEW model-
visible tool ``connected_browser_exec`` is registered for the externally
connected operator browser (``browser.connected_cdp_url`` only, fail-closed,
no fallback). Both browser CLIs are provisioned at image build time:
``agent-browser@0.26.0`` plus a pinned full Chrome for Testing
(``/opt/josemar/agent-browser/chrome/chrome``, version 152.0.7977.64, sha256
8b592f066af71f054aab2cc80fc26f73c775c6d44ebb99d16ade924b24756c2e, selected
via ``AGENT_BROWSER_EXECUTABLE_PATH`` — the sanctioned deviation from the
rev-2 base-cache reuse, because runtime evidence proved the base image's
Playwright headless-shell is unusable by agent-browser@0.26.0 and the
agent-browser HOME cache under /opt/data is masked by the runtime volume),
and an isolated
``/opt/josemar/browser-use`` venv with ``browser-use==0.13.8`` +
``browser-harness==0.1.9`` (connected route, fixed CLI path, no uvx).

This suite runs on ordinary ``make test`` and asserts:

  1. Config: the ``browser:`` block is exactly ``backend: "off"`` (quoted),
     ``cloud_provider: "local"``, ``connected_cdp_url``; there is NO global
     ``cdp_url`` key, and the comments state the ordinary route must never
     consume the connected endpoint.
  2. Patch-source shape: ``connected_browser_exec`` registration in the
     ``browser`` toolset (schema code/session/timeout_s, check_fn
     ``is_connected_browser_configured``), the reserved ``__jc_2_`` daemon
     namespace, the route-selector env scrub (``BU_*``
     prefix loop + the provider/LLM credential tuple), the fixed CLI path
     invocation, NO ``use_connected_browser``, no ``uvx``/``_find_cli`` in
     the connected implementation, ``_OWN_TAB_PREAMBLE`` applied, upstream
     ``browser_exec`` body untouched, and the ``model_tools.py`` terminal
     gate extended to ``connected_browser_exec``.
  3. Session mapping: reserved names are deterministic, bounded (<=64),
     PASS the browser-harness ``\\A[A-Za-z0-9_-]{1,64}\\Z`` rule and FAIL the
     Hermes ``_SESSION_RE`` (mechanical disjointness), with no truncation
     collisions. The name is bound to BOTH the configured connected endpoint
     (normalized only by stripping a trailing slash after the existing
     validation) and the public session (NUL-delimited in the digest input):
     the same public session at endpoints A/B has distinct names, and the
     omitted public session is also endpoint-bound (no fixed cross-endpoint
     default).
  4. Functional apply proof: the real patch applies to a minimal
     anchor/mechanics fixture of the pinned upstream shape (including the
     load-bearing upstream symbols asserted by ``assert_upstream_symbols``);
     duplicate apply fails loudly; a missing upstream symbol fails loudly;
     the patched modules compile; a connected nonzero CLI outcome surfaces
     the generic leak-free error with the scrubbed env and the own-tab
     preamble applied. The fixture is a MECHANICS fixture, not a claim of
     complete upstream fidelity — the real Docker image build is the drift
     proof.
  5. Dockerfile contract: exact ``agent-browser@0.26.0`` bake, the exact
     Chrome for Testing URL+version+sha256 in ONE RUN block (sha256sum -c,
     unzip, ``test -x``, version grep, mandatory headless smoke launch,
     tmp cleanup), the exact ``ENV AGENT_BROWSER_EXECUTABLE_PATH=`` line,
     ``/opt/josemar/agent-browser`` and ``/opt/josemar/browser-use`` NOT in
     ``HERMES_WRITABLE_VOLUMES`` and not under ``/opt/data``, and the
     fail-loud patch block py_compiling BOTH ``browser_use_cli.py`` and
     ``model_tools.py``.

Transient artifacts are written only under ``dump_folder/`` and removed on
teardown.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
HERMES_CONFIG = REPO_ROOT / "config" / "hermes-config.yaml"
PATCH_SCRIPT = REPO_ROOT / "scripts" / "patch-hermes-browser-routing.py"
DOCKERFILE = REPO_ROOT / "Dockerfile.hermes"
DOCKER_INIT = REPO_ROOT / "docker-hermes-init.sh"
DUMP_DIR = REPO_ROOT / "dump_folder" / "browser-routing-contract"

# Pinned upstream / browser-harness constraints.
# Public session names: 1-64 chars, first char alphanumeric.
PUBLIC_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# browser-use CLI / browser-harness 0.1.9 BU_NAME: 1-64 of [A-Za-z0-9_-],
# leading underscore valid.
HARNESS_BU_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

# Reserved connected daemon namespace (revision-2 R1 remediation): __jc_2_ + full SHA-256
# URL-safe base64 digest (43 chars) of the NUL-delimited configured connected
# endpoint + public session. Total 50 chars, inside the harness limit. The
# endpoint is normalized only by stripping a trailing slash after the existing
# validation, so the same public session at endpoints A/B has distinct names
# and the omitted public session is also endpoint-bound (no fixed
# cross-endpoint default).
CONNECTED_DAEMON_PREFIX = "__jc_2_"
SAMPLE_SESSION = "route-probe-s"
SAMPLE_ENDPOINT = "http://127.0.0.1:9222"
SAMPLE_DIGEST = "8cXv_M66HQlxret8ilfXkoc0hXKWnuf1v-b_6WtR8w0"
SAMPLE_DAEMON_NAME = CONNECTED_DAEMON_PREFIX + SAMPLE_DIGEST

# The connected CLI constant the patched tool body depends on. The plan
# requires the fixed build-owned CLI to be invoked by absolute path; the
# patch must DEFINE this constant (it is referenced by
# is_connected_browser_configured / connected_browser_exec).
CONNECTED_CLI_DEFINITION = '_CONNECTED_BROWSER_CLI = "/opt/josemar/browser-use/bin/browser-use"'

# Route-selector / provider / LLM keys the connected env scrub must clear
# from the local call env (never os.environ).
SCRUBBED_ENV_KEYS = (
    "BROWSER_CDP_URL",
    "BROWSERBASE_API_KEY",
    "BROWSERBASE_PROJECT_ID",
    "BROWSER_USE_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
    "FIRECRAWL_BROWSER_TTL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


def connected_daemon_name(endpoint: str, public_session: str) -> str:
    """Deterministic reserved BU_NAME for connected sessions (revision-2 R1 remediation).

    Binds the harness name to BOTH the configured connected endpoint
    (normalized only by stripping a trailing slash after the existing
    validation) and the public session (NUL-delimited in the digest input),
    so the same public session at endpoints A/B has distinct names and the
    omitted public session is also endpoint-bound (no fixed cross-endpoint
    default).
    """
    digest = (
        base64.urlsafe_b64encode(
            hashlib.sha256(
                (endpoint.rstrip("/") + "\0" + public_session).encode("utf-8")
            ).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    return CONNECTED_DAEMON_PREFIX + digest


def top_level_block(text: str, key: str) -> str:
    """Return the column-0 ``<key>:`` YAML block from a config file."""
    lines = text.splitlines(keepends=True)
    marker = f"{key}:\n"
    try:
        start = next(index for index, line in enumerate(lines) if line == marker)
    except StopIteration as exc:
        raise AssertionError(f"missing top-level YAML key {key!r}") from exc
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" "):
            end = index
            break
    return "".join(lines[start:end])


def load_patch_module() -> Any:
    """Import scripts/patch-hermes-browser-routing.py in-process."""
    spec = importlib.util.spec_from_file_location(
        "patch_hermes_browser_routing", PATCH_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Minimal anchor/mechanics fixture for the pinned upstream
# tools/browser_use_cli.py shape: it contains EXACTLY the replace_once
# anchors the rev-2 patch expects (in upstream order) plus the load-bearing
# upstream-private symbols asserted by assert_upstream_symbols, and the
# minimal surrounding code needed to stay syntactically valid and runnable.
# It is a MECHANICS fixture, NOT a claim of complete upstream fidelity — the
# real Docker image build (Dockerfile.hermes applies the same patch to the
# real pinned source and py_compiles it) is the authoritative drift proof.
# Upstream browser_exec is present and ends with its result block; the
# patch inserts the connected tool between that return and _HEADER_BASE.
SKELETON_SOURCE = (
    "import json\n"
    "import logging\n"
    "import os\n"
    "import re\n"
    "import shutil\n"
    "import subprocess\n"
    "import time\n"
    "from pathlib import Path\n"
    "from typing import Any, Dict, List, Optional, Tuple\n"
    "\n"
    "from utils import is_truthy_value\n"
    "\n"
    "logger = logging.getLogger(\"skeleton\")\n"
    "\n"
    "_DEFAULT_TIMEOUT_S = 60\n"
    "_MAX_TIMEOUT_S = 600\n"
    "_MIN_TIMEOUT_S = 5\n"
    "_STDERR_CAP_CHARS = 2000\n"
    "_OWN_TAB_PREAMBLE = \"# Own-tab preamble\\n\"\n"
    "_dynamic_schema_overrides = {}\n"
    "\n"
    "# Mechanics hook (test-only): the connected resolver reads this instead\n"
    "# of a real config file so the preflight can be driven against a local\n"
    "# discovery stub. Not part of any patch anchor.\n"
    "_SKELETON_BROWSER_CFG: dict = {}\n"
    "\n"
    "\n"
    "def _read_browser_cfg() -> dict:\n"
    "    return _SKELETON_BROWSER_CFG\n"
    "\n"
    "\n"
    "def _base_subprocess_env() -> dict:\n"
    "    return dict(os.environ)\n"
    "\n"
    "\n"
    "def _workspace_dir(task_id: Optional[str] = None) -> str:\n"
    "    return \"\"\n"
    "\n"
    "\n"
    "def _blocked_url_in_code(code: str) -> Optional[str]:\n"
    "    return None\n"
    "\n"
    "\n"
    "def _find_screenshot(stdout: str, started: float) -> Optional[str]:\n"
    "    return None\n"
    "\n"
    "\n"
    "def _native_screenshot_result(result: dict, screenshot: str) -> Optional[dict]:\n"
    "    return None\n"
    "\n"
    "\n"
    "# Cloud daemon names become the BU_NAME env var\n"
    '_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")\n'
    "\n"
    "# Mechanics hook (test-only): the connected CLI constant. The host test\n"
    "# env has no /opt/josemar/browser-use, so point at a guaranteed-existing\n"
    "# executable (/bin/sh) to keep the CLI-presence check reachable; the CLI\n"
    "# invocation itself is always faked in these tests.\n"
    "_CONNECTED_BROWSER_CLI = \"/bin/sh\"\n"
    "\n"
    "\n"
    "def is_browser_use_cli_mode() -> bool:\n"
    "    return False\n"
    "\n"
    "\n"
    "def _dynamic_schema_overrides_fn() -> dict:\n"
    "    return {}\n"
    "\n"
    "\n"
    "def browser_exec(\n"
    "    code: str,\n"
    '    session: str = "",\n'
    "    timeout_s: int = _DEFAULT_TIMEOUT_S,\n"
    "    task_id: Optional[str] = None,\n"
    "):\n"
    "    env = _base_subprocess_env()\n"
    "    if session:\n"
    "        env[\"BU_NAME\"] = session\n"
    "    proc = subprocess.run(\n"
    "        [\"echo\", code],\n"
    "        input=code,\n"
    "        capture_output=True,\n"
    "        text=True,\n"
    "        timeout=timeout_s,\n"
    "        env=env,\n"
    "    )\n"
    "    result = {\n"
    '        "success": proc.returncode == 0,\n'
    '        "exit_code": proc.returncode,\n'
    '        "output": proc.stdout,\n'
    "    }\n"
    "    return tool_result(result)\n"
    "\n"
    "\n"
    "# The tool description is the CLI's skill, fetched from browser-use skill\n"
    '_HEADER_BASE = (\n'
    '    "skeleton header",\n'
    ")\n"
    "\n"
    "\n"
    "BROWSER_EXEC_SCHEMA = {\n"
    '    "name": "browser_exec",\n'
    '    "parameters": {"type": "object", "properties": {}, "required": ["code"]},\n'
    "}\n"
    "\n"
    "\n"
    "from tools.registry import registry\n"
    "\n"
    "\n"
    "registry.register(\n"
    '    name="browser_exec",\n'
    '    toolset="browser",\n'
    "    schema=BROWSER_EXEC_SCHEMA,\n"
    "    handler=lambda args, **kw: browser_exec(\n"
    '        code=args.get("code", ""),\n'
    "    ),\n"
    "    check_fn=is_browser_use_cli_mode,\n"
    "    dynamic_schema_overrides=_dynamic_schema_overrides,\n"
    '    emoji="🌐",\n'
    ")\n"
)


# Mechanics stub for `utils` (the pinned module imports is_truthy_value).
UTILS_SOURCE = (
    "def is_truthy_value(value) -> bool:\n"
    "    return bool(value)\n"
)


# Minimal anchor/mechanics fixture for the pinned upstream model_tools.py
# session-level terminal gate (the rev-2 patch extends it to
# connected_browser_exec).
SKELETON_MODEL_TOOLS_SOURCE = (
    "def _filter_tools(filtered_tools: list, available_tool_names: set):\n"
    "    # browser_exec (Browser Use mode) runs arbitrary Python on the host via\n"
    "    # the browser-use CLI subprocess.  A session whose toolset selection\n"
    "    # excludes the terminal surface (e.g. a messaging platform configured\n"
    "    # without terminal access) must not regain host code execution through\n"
    "    # the browser toolset — that would silently widen the operator's chosen\n"
    "    # security posture.  Session-level gate, NOT a check_fn: check_fn results\n"
    "    # are TTL-cached process-wide while one gateway process serves many\n"
    "    # sessions with different toolset configs.\n"
    '    if "browser_exec" in available_tool_names and "terminal" not in available_tool_names:\n'
    "        filtered_tools = [\n"
    "            td for td in filtered_tools\n"
    '            if td.get("function", {}).get("name") != "browser_exec"\n'
    "        ]\n"
    '        available_tool_names.discard("browser_exec")\n'
    "    return filtered_tools, available_tool_names\n"
)


# Mechanics stub for `tools.registry` (the patched module imports
# `from tools.registry import tool_error, tool_result` / `registry`).
TOOLS_REGISTRY_SOURCE = (
    "from __future__ import annotations\n"
    "\n"
    "\n"
    "def tool_error(message: str) -> dict:\n"
    '    return {"error": message}\n'
    "\n"
    "\n"
    "def tool_result(result: dict) -> dict:\n"
    "    return result\n"
    "\n"
    "\n"
    "class _Registry:\n"
    "    def __init__(self) -> None:\n"
    "        self._tools: dict = {}\n"
    "\n"
    "    def register(self, **kwargs) -> None:\n"
    '        self._tools[kwargs.get("name", "?")] = kwargs\n'
    "\n"
    "    def registered_names(self) -> set:\n"
    "        return set(self._tools)\n"
    "\n"
    "\n"
    "registry = _Registry()\n"
)


class BrowserRoutingConfigContractTests(unittest.TestCase):
    """hermes-config.yaml: rev-2 browser block, no global cdp_url."""

    def setUp(self) -> None:
        self.text = HERMES_CONFIG.read_text(encoding="utf-8")
        self.browser_block = top_level_block(self.text, "browser")

    def test_config_browser_block_rev2_invariants(self) -> None:
        self.assertIn('backend: "off"', self.browser_block)
        self.assertIn("cloud_provider: \"local\"", self.browser_block)
        self.assertIn(
            'connected_cdp_url: "http://127.0.0.1:9222"',
            self.browser_block,
        )

    def test_config_has_no_global_cdp_url_key(self) -> None:
        for line in self.browser_block.splitlines():
            self.assertNotRegex(
                line, r"^\s*cdp_url:",
                f"global browser.cdp_url must not be defined: {line!r}",
            )

    def test_config_comment_ordinary_route_never_consumes_connected(self) -> None:
        self.assertIn(
            "must never consume",
            self.text,
            "the config comments must state the ordinary route must never "
            "consume the connected endpoint",
        )


class BrowserRoutingPatchSourceContractTests(unittest.TestCase):
    """scripts/patch-hermes-browser-routing.py (rev-2) source shape."""

    def setUp(self) -> None:
        self.text = PATCH_SCRIPT.read_text(encoding="utf-8")

    def test_patch_importable_and_exposes_apply_patches(self) -> None:
        module = load_patch_module()
        self.assertTrue(callable(module.apply_patches))
        self.assertTrue(callable(module.replace_once))
        self.assertTrue(callable(module.assert_upstream_symbols))
        self.assertEqual(module.BROWSER_USE_CLI_PATH.name, "browser_use_cli.py")
        self.assertEqual(module.MODEL_TOOLS_PATH.name, "model_tools.py")

    def test_patch_docstring_pins_v2026_8_31_provenance(self) -> None:
        # Issue #156 W4 reconciliation: the patcher docstring must pin the
        # exact current provenance — Hermes v2026.8.31 and the pinned
        # upstream commit SHA. (The retired v2026.8.18 pin carried a
        # 10-digit numeric run that needed a .pii-allowlist exception;
        # this pin has no such run, so no exception is needed.)
        docstring = self.text.split('"""', 2)[1]
        self.assertIn("Hermes v2026.8.31", docstring)
        self.assertIn("29112bef099274229cadff79cdff7bf7b99c4b77", docstring)

    def test_patch_uses_fail_loud_replace_once(self) -> None:
        self.assertIn("def replace_once", self.text)
        self.assertIn("raise RuntimeError", self.text)
        self.assertIn("Expected snippet not found", self.text)

    def test_patch_assert_upstream_symbols_has_eleven_needles(self) -> None:
        module = load_patch_module()
        needles = module.UPSTREAM_SYMBOL_NEEDLES
        self.assertEqual(len(needles), 11)
        for needle in (
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
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, needles)

    def test_patch_registers_connected_tool_in_browser_toolset(self) -> None:
        self.assertIn('name="connected_browser_exec"', self.text)
        self.assertIn('toolset="browser"', self.text)
        self.assertIn("def connected_browser_exec(", self.text)
        self.assertIn("def is_connected_browser_configured(", self.text)
        self.assertIn("def _resolve_connected_cdp(", self.text)
        self.assertIn("check_fn=is_connected_browser_configured", self.text)

    def test_patch_schema_code_session_timeout_s(self) -> None:
        self.assertIn("CONNECTED_BROWSER_EXEC_SCHEMA = {", self.text)
        self.assertIn('"required": ["code"],', self.text)
        self.assertIn('"session": {', self.text)
        self.assertIn('"timeout_s": {', self.text)
        # The check_fn must not probe the network during schema assembly.
        self.assertIn("NO network/CDP probe during schema assembly", self.text)

    def test_patch_reserved_daemon_constants(self) -> None:
        self.assertIn('_CONNECTED_DAEMON_PREFIX = "__jc_2_"', self.text)
        self.assertIn("def _connected_daemon_name(", self.text)
        self.assertIn("def _connected_daemon_digest(", self.text)

    def test_patch_env_scrub_prefix_loop_and_tuple(self) -> None:
        self.assertIn('if key.startswith("BU_"):', self.text)
        self.assertIn("env.pop(key, None)", self.text)
        for key in SCRUBBED_ENV_KEYS:
            with self.subTest(env_key=key):
                self.assertIn(f'"{key}",', self.text)
        # Only the preflighted ws and the reserved name are injected.
        self.assertIn('env["BU_NAME"] = _connected_daemon_name(endpoint, session)', self.text)
        self.assertIn('env["BU_CDP_WS"] = ws_url', self.text)
        # Scrub is local-call-env only.
        self.assertIn("never os.environ", self.text)

    def test_patch_defines_connected_cli_constant(self) -> None:
        # The connected tool invokes the fixed build-owned CLI by absolute
        # path; the constant MUST be defined in the patch (referenced by
        # is_connected_browser_configured and connected_browser_exec).
        self.assertIn(CONNECTED_CLI_DEFINITION, self.text)

    def test_patch_connected_invocation_is_fixed_path_no_uvx(self) -> None:
        # The connected subprocess argv is the fixed constant — no uvx, no
        # _find_cli() command discovery in the connected implementation.
        self.assertIn("            [_CONNECTED_BROWSER_CLI],", self.text)
        connected_section = self.text.split(
            "# connected_browser_exec: the externally connected operator browser", 1
        )[1].split("# The tool description is the CLI's skill", 1)[0]
        self.assertNotIn("uvx", connected_section)
        self.assertNotIn("_find_cli", connected_section)

    def test_patch_own_tab_preamble_applied(self) -> None:
        self.assertIn("code = _OWN_TAB_PREAMBLE + code", self.text)

    def test_patch_no_use_connected_browser_flag(self) -> None:
        # Revision 2 removes the revision-1 flag entirely. The module
        # docstring legitimately narrates the removal, so scope the absence
        # assertion to the code after the docstring.
        code = self.text.split('"""', 2)[2]
        self.assertNotIn("use_connected_browser", code)

    def _decoded_replace_once_olds(self) -> list[str]:
        """Decode the OLD anchor strings of every replace_once call.

        The patch file stores anchors as Python string literals (escaped
        newlines/apostrophes); decoding via ast gives the exact text the
        patch will search for in the target file.
        """
        import ast

        tree = ast.parse(self.text)
        olds: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "replace_once":
                old_arg = node.args[1]
                if isinstance(old_arg, ast.Constant) and isinstance(old_arg.value, str):
                    olds.append(old_arg.value)
        return olds

    def test_patch_upstream_browser_exec_untouched(self) -> None:
        # The connected tool is inserted AFTER browser_exec's final
        # `return tool_result(result)` and BEFORE _HEADER_BASE; the decoded
        # anchor proves the insertion point, and no rev-1 route boolean
        # exists in the code (the docstring narrates the removal).
        insertion_anchor = (
            "    return tool_result(result)\n"
            "\n"
            "\n"
            "# The tool description is the CLI's skill, fetched from browser-use skill\n"
            "_HEADER_BASE = (\n"
        )
        self.assertTrue(
            any(insertion_anchor in old for old in self._decoded_replace_once_olds()),
            "the patch must insert the connected tool between browser_exec's "
            "final return and _HEADER_BASE (browser_exec body untouched)",
        )
        code = self.text.split('"""', 2)[2]
        self.assertNotIn("use_connected_browser", code)
        self.assertNotIn("backend_err = _resolve_backend_cdp", code)

    def test_patch_model_tools_terminal_gate_extended(self) -> None:
        self.assertIn(
            'or "connected_browser_exec" in available_tool_names',
            self.text,
        )
        self.assertIn(
            'available_tool_names.discard("connected_browser_exec")',
            self.text,
        )
        self.assertIn('not in ("browser_exec", "connected_browser_exec")', self.text)

    def test_patch_normalizes_loopback_ws_for_remote_endpoint(self) -> None:
        # Council correction: Chrome's /json/version echoes a loopback ws
        # authority even when reached via a non-loopback (bridge/remote)
        # host; the preflight must rewrite it to the configured endpoint
        # authority ONLY when the configured host is non-loopback.
        self.assertIn("def _normalize_connected_ws_url(", self.text)
        self.assertIn("from urllib.parse import urlparse, urlunparse", self.text)
        self.assertIn("ws_url = _normalize_connected_ws_url(ws_url, endpoint)", self.text)
        # Production loopback layout must stay untouched by the rewrite rule.
        self.assertIn('if ws_host not in ("127.0.0.1", "localhost", "::1"):', self.text)
        self.assertIn('if endpoint_host in ("127.0.0.1", "localhost", "::1"):', self.text)


class BrowserRoutingSessionMappingTests(unittest.TestCase):
    """Reserved connected BU_NAME mapping: deterministic, bounded,
    harness-valid, mechanically disjoint from public session names, and bound
    to BOTH the configured connected endpoint and the public session."""

    def test_named_session_digest_formula_and_length(self) -> None:
        name = connected_daemon_name(SAMPLE_ENDPOINT, SAMPLE_SESSION)
        self.assertEqual(name, SAMPLE_DAEMON_NAME)
        self.assertEqual(len(SAMPLE_DIGEST), 43)
        self.assertEqual(len(name), 50)
        self.assertLessEqual(len(name), 64)

    def test_reserved_names_fail_public_session_re(self) -> None:
        # _SESSION_RE requires an ALPHANUMERIC first character; the reserved
        # underscore-leading namespace is mechanically disjoint from every
        # valid public session name (and from every normal upstream daemon
        # BU_NAME, which equals the public session verbatim).
        for public in ("", "a", "session-1", "a" * 64):
            internal = connected_daemon_name(SAMPLE_ENDPOINT, public)
            with self.subTest(public=public[:16] or "<empty>", internal=internal):
                self.assertIsNone(PUBLIC_SESSION_RE.match(internal))

    def test_reserved_names_pass_harness_rule(self) -> None:
        for name in (
            connected_daemon_name(SAMPLE_ENDPOINT, ""),
            connected_daemon_name(SAMPLE_ENDPOINT, SAMPLE_SESSION),
        ):
            self.assertTrue(name.startswith("_"))
            self.assertRegex(name, HARNESS_BU_NAME_RE)

    def test_64_char_sessions_map_distinct_without_truncation(self) -> None:
        prefix = "a" * 60
        first = connected_daemon_name(SAMPLE_ENDPOINT, prefix + "XY")
        second = connected_daemon_name(SAMPLE_ENDPOINT, prefix + "XZ")
        self.assertNotEqual(first, second, "same-prefix sessions must never collide")
        self.assertRegex(first, HARNESS_BU_NAME_RE)
        self.assertRegex(second, HARNESS_BU_NAME_RE)

    def test_mapping_is_bounded_and_deterministic(self) -> None:
        self.assertEqual(
            connected_daemon_name(SAMPLE_ENDPOINT, SAMPLE_SESSION),
            connected_daemon_name(SAMPLE_ENDPOINT, SAMPLE_SESSION),
        )

    def test_same_session_at_different_endpoints_has_distinct_names(self) -> None:
        # The chosen invariant: the connected BU_NAME must deterministically
        # include BOTH the configured connected endpoint and the public
        # session, so the same public session at endpoints A/B has distinct
        # harness names (no cross-endpoint daemon/state collision).
        endpoint_b = "http://fixture-b:9222"
        name_a = connected_daemon_name(SAMPLE_ENDPOINT, SAMPLE_SESSION)
        name_b = connected_daemon_name(endpoint_b, SAMPLE_SESSION)
        self.assertNotEqual(name_a, name_b)
        self.assertRegex(name_a, HARNESS_BU_NAME_RE)
        self.assertRegex(name_b, HARNESS_BU_NAME_RE)

    def test_omitted_session_is_endpoint_bound(self) -> None:
        # No fixed cross-endpoint default: the omitted public session must
        # also be endpoint-bound, so endpoints A/B get distinct names.
        endpoint_b = "http://fixture-b:9222"
        default_a = connected_daemon_name(SAMPLE_ENDPOINT, "")
        default_b = connected_daemon_name(endpoint_b, "")
        self.assertNotEqual(default_a, default_b)
        self.assertRegex(default_a, HARNESS_BU_NAME_RE)
        self.assertRegex(default_b, HARNESS_BU_NAME_RE)

    def test_endpoint_normalization_strips_only_trailing_slash(self) -> None:
        # The endpoint is normalized only by stripping a trailing slash after
        # the existing validation; a trailing slash must not change the name.
        self.assertEqual(
            connected_daemon_name("http://127.0.0.1:9222/", SAMPLE_SESSION),
            connected_daemon_name("http://127.0.0.1:9222", SAMPLE_SESSION),
        )


class BrowserRoutingDockerfileContractTests(unittest.TestCase):
    """Dockerfile.hermes: deterministic dual CLI provisioning + fail-loud
    patch block + writable-volume boundary."""

    def setUp(self) -> None:
        self.text = DOCKERFILE.read_text(encoding="utf-8")

    def test_agent_browser_baked_with_version_assertion(self) -> None:
        self.assertIn(
            "RUN npm install -g --no-audit --fetch-retries=5 agent-browser@0.26.0",
            self.text,
        )
        self.assertIn('test -n "$(command -v agent-browser)"', self.text)
        self.assertIn(
            "agent-browser --version 2>&1 | grep -Eq '^agent-browser v?0\\.26\\.0$|^v?0\\.26\\.0$'",
            self.text,
        )

    def test_chrome_for_testing_baked_in_one_fail_loud_run(self) -> None:
        """The pinned Chrome for Testing bake: URL + version + sha256 co-located
        in ONE RUN block (a version bump cannot silently skip the checksum
        bump), with sha256sum -c, unzip of the WHOLE tree, test -x, version
        grep, the mandatory headless smoke launch, and tmp cleanup."""
        cft_version = "152.0.7977.64"
        cft_sha256 = "8b592f066af71f054aab2cc80fc26f73c775c6d44ebb99d16ade924b24756c2e"
        cft_url = (
            "https://storage.googleapis.com/chrome-for-testing-public/"
            f"{cft_version}/linux64/chrome-linux64.zip"
        )
        # The URL, version, and sha256 must live in the SAME RUN block: take
        # the slice from the RUN that contains the URL up to the next blank
        # line and assert everything is inside it.
        url_pos = self.text.find(cft_url)
        self.assertNotEqual(url_pos, -1, "CfT download URL missing")
        run_start = self.text.rfind("RUN ", 0, url_pos)
        run_end = self.text.find("\n\n", url_pos)
        self.assertNotEqual(run_start, -1)
        self.assertNotEqual(run_end, -1)
        block = self.text[run_start:run_end]
        self.assertIn(cft_version, block, "CfT version not in the same RUN block")
        self.assertIn(cft_sha256, block, "CfT sha256 not in the same RUN block")
        self.assertIn("sha256sum -c", block)
        self.assertIn("unzip -q", block)
        self.assertIn("chown -R root:root /opt/josemar/agent-browser/chrome", block)
        self.assertIn("chmod -R 0755 /opt/josemar/agent-browser/chrome", block)
        self.assertIn("test -x /opt/josemar/agent-browser/chrome/chrome", block)
        self.assertIn("chrome --version 2>&1", block)
        self.assertIn("grep -Eq '152\\.0\\.7977\\.64'", block)
        self.assertIn(
            "--headless --no-sandbox", block,
            "mandatory headless smoke launch missing",
        )
        self.assertIn("--dump-dom about:blank", block)
        self.assertIn("rm -rf /tmp/cft-bake", block)
        # The env points at the chrome binary inside the baked tree.
        self.assertIn(
            "ENV AGENT_BROWSER_EXECUTABLE_PATH=/opt/josemar/agent-browser/chrome/chrome",
            self.text,
        )
        # curl is installed in the same block (final stage previously lacked it).
        self.assertIn("apt-get install -y --no-install-recommends", block)
        self.assertIn("curl \\", block)
        # No surviving base-cache reuse claim in the Dockerfile.
        self.assertNotIn("/opt/hermes/.playwright", self.text)

    def test_chrome_for_testing_not_in_writable_volumes(self) -> None:
        init_text = DOCKER_INIT.read_text(encoding="utf-8")
        self.assertNotIn(
            "/opt/josemar/agent-browser", init_text,
            "the ordinary-browser Chrome tree must stay root-owned; the init "
            "allowlist must never chown it",
        )
        self.assertNotIn(
            "/opt/data/agent-browser", self.text,
            "the baked Chrome tree must not live under $HERMES_HOME",
        )
        # AGENT_BROWSER_EXECUTABLE_PATH is set ONCE, as an ENV in the
        # Dockerfile (single source; not also set in compose).
        self.assertEqual(
            self.text.count("AGENT_BROWSER_EXECUTABLE_PATH="), 1,
            "AGENT_BROWSER_EXECUTABLE_PATH must be set exactly once (ENV line)",
        )

    def test_browser_use_venv_baked_with_exact_versions(self) -> None:
        self.assertIn(
            "RUN /opt/hermes/.venv/bin/python3 -m venv /opt/josemar/browser-use",
            self.text,
        )
        self.assertIn("browser-use==0.13.8", self.text)
        self.assertIn("browser-harness==0.1.9", self.text)
        self.assertIn("test -x /opt/josemar/browser-use/bin/browser-use", self.text)
        self.assertIn("pip show browser-use", self.text)
        self.assertIn("'^Version: 0\\.13\\.8$'", self.text)
        self.assertIn("pip show browser-harness", self.text)
        self.assertIn("'^Version: 0\\.1\\.9$'", self.text)

    def test_browser_use_env_not_in_writable_volumes(self) -> None:
        init_text = DOCKER_INIT.read_text(encoding="utf-8")
        self.assertIn("HERMES_WRITABLE_VOLUMES=", init_text)
        self.assertNotIn(
            "/opt/josemar/browser-use", init_text,
            "the connected CLI env must stay root-owned; the init allowlist "
            "must never chown it",
        )
        self.assertNotIn(
            "/opt/josemar/agent-browser", init_text,
            "the baked Chrome tree must stay root-owned; the init allowlist "
            "must never chown it",
        )
        # And neither is under $HERMES_HOME (/opt/data) in the Dockerfile.
        self.assertNotIn(
            "/opt/data/browser-use", self.text,
            "the build-time tool env must not live under $HERMES_HOME",
        )
        self.assertNotIn(
            "/opt/data/agent-browser", self.text,
            "the baked Chrome tree must not live under $HERMES_HOME",
        )

    def test_patch_block_fail_loud_py_compiles_both_modules(self) -> None:
        self.assertIn(
            "COPY scripts/patch-hermes-browser-routing.py /tmp/patch-hermes-browser-routing.py",
            self.text,
        )
        self.assertIn(
            "RUN python3 /tmp/patch-hermes-browser-routing.py \\\n"
            "    && /opt/hermes/.venv/bin/python3 -m py_compile \\\n"
            "        /opt/hermes/tools/browser_use_cli.py \\\n"
            "        /opt/hermes/model_tools.py \\\n"
            "    && rm /tmp/patch-hermes-browser-routing.py",
            self.text,
        )

    def test_ordering_install_before_patch(self) -> None:
        venv_pos = self.text.find("RUN /opt/hermes/.venv/bin/python3 -m venv /opt/josemar/browser-use")
        patch_pos = self.text.find("COPY scripts/patch-hermes-browser-routing.py")
        self.assertNotEqual(venv_pos, -1, "browser-use venv install missing")
        self.assertNotEqual(patch_pos, -1, "patch COPY missing")
        self.assertLess(venv_pos, patch_pos, "the connected CLI must be baked before the patch")


class BrowserRoutingPatchApplyFunctionalTests(unittest.TestCase):
    """Apply the real rev-2 patch to the anchor/mechanics fixtures and prove
    the fail-loud contract and the connected tool wiring end to end."""

    @classmethod
    def setUpClass(cls) -> None:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="skeleton-", dir=DUMP_DIR))
        cls.module = load_patch_module()
        cls.buc_path = cls.tmpdir / "browser_use_cli.py"
        cls.mt_path = cls.tmpdir / "model_tools.py"
        tools_pkg = cls.tmpdir / "tools"
        tools_pkg.mkdir(exist_ok=True)
        (tools_pkg / "__init__.py").write_text("", encoding="utf-8")
        (tools_pkg / "registry.py").write_text(TOOLS_REGISTRY_SOURCE, encoding="utf-8")
        cls.buc_path.write_text(SKELETON_SOURCE, encoding="utf-8")
        cls.mt_path.write_text(SKELETON_MODEL_TOOLS_SOURCE, encoding="utf-8")
        (cls.tmpdir / "utils.py").write_text(UTILS_SOURCE, encoding="utf-8")
        cls.module.apply_patches(cls.buc_path, cls.mt_path)
        cls.patched_text = cls.buc_path.read_text(encoding="utf-8")
        cls.patched_mt_text = cls.mt_path.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(DUMP_DIR, ignore_errors=True)

    def test_patch_applies_to_pinned_shape(self) -> None:
        self.assertNotEqual(self.patched_text, SKELETON_SOURCE)
        self.assertGreater(len(self.patched_text), len(SKELETON_SOURCE))

    def test_patched_text_contains_connected_wiring(self) -> None:
        for needle in (
            "def connected_browser_exec(",
            "def _resolve_connected_cdp(",
            "def is_connected_browser_configured(",
            '_CONNECTED_DAEMON_PREFIX = "__jc_2_"',
            'name="connected_browser_exec",',
            'toolset="browser",',
            "CONNECTED_BROWSER_EXEC_SCHEMA = {",
            '"required": ["code"],',
            "code = _OWN_TAB_PREAMBLE + code",
            "env = _base_subprocess_env()",
            "env[\"BU_CDP_WS\"] = ws_url",
            "env[\"BU_NAME\"] = _connected_daemon_name(endpoint, session)",
            "check_fn=is_connected_browser_configured,",
            'subprocess.run(\n            [_CONNECTED_BROWSER_CLI],',
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.patched_text)
        self.assertNotIn("use_connected_browser", self.patched_text)

    def test_patched_model_tools_gate_extended(self) -> None:
        self.assertIn(
            'or "connected_browser_exec" in available_tool_names',
            self.patched_mt_text,
        )
        self.assertIn(
            'available_tool_names.discard("connected_browser_exec")',
            self.patched_mt_text,
        )

    def test_patched_modules_compile(self) -> None:
        py_compile.compile(str(self.buc_path), doraise=True)
        py_compile.compile(str(self.mt_path), doraise=True)

    def test_second_apply_fails_loudly(self) -> None:
        with self.assertRaises(RuntimeError):
            self.module.apply_patches(self.buc_path, self.mt_path)

    def test_missing_upstream_symbol_fails_loudly(self) -> None:
        # A skeleton without `def _read_browser_cfg(` must abort via
        # assert_upstream_symbols (not silently).
        broken = self.tmpdir / "broken_browser_use_cli.py"
        broken.write_text(
            SKELETON_SOURCE.replace("def _read_browser_cfg(", "def _read_browser_cfg_x("),
            encoding="utf-8",
        )
        broken_mt = self.tmpdir / "broken_model_tools.py"
        broken_mt.write_text(SKELETON_MODEL_TOOLS_SOURCE, encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            self.module.apply_patches(broken, broken_mt)
        self.assertIn("Load-bearing upstream symbols missing", str(ctx.exception))

    def _load_patched(self, tag: str) -> Any:
        """Load the patched skeleton module fresh under a unique name, with
        the temp dir (and its tools package) importable."""
        name = f"browser_use_cli_patched_{tag}"
        sys.modules.pop(name, None)
        sys.path.insert(0, str(self.tmpdir))
        spec = importlib.util.spec_from_file_location(name, self.buc_path)
        assert spec is not None and spec.loader is not None
        patched = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(patched)
        return patched

    def _start_discovery_server(self) -> tuple:
        """In-process CDP discovery stub on an ephemeral port."""
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        holder: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = _json.dumps({
                    "webSocketDebuggerUrl": holder["ws"],
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        holder["ws"] = f"ws://127.0.0.1:{port}/devtools/browser/skeleton"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, holder["ws"]

    @staticmethod
    def _fake_cli(returncode: int, stderr: str = "", calls: list | None = None,
                  timeout_error: bool = False):
        """Patch subprocess.run with a controlled fake CLI outcome."""

        class FakeProc:
            stdout = "fake-cli-stdout"

            @property
            def returncode(self):
                return returncode

            @property
            def stderr(self):
                return stderr

            def communicate(self, *a, **k):
                return self.stdout, stderr

            def wait(self, *a, **k):
                return returncode

        def fake_run(*args, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            if timeout_error:
                raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 1))
            return FakeProc()

        return mock.patch.object(subprocess, "run", fake_run)

    def test_connected_nonzero_cli_failure_is_generic_and_leak_free(self) -> None:
        server, ws_url = self._start_discovery_server()
        self.addCleanup(server.shutdown)
        patched = self._load_patched("conn_nonzero")
        patched._SKELETON_BROWSER_CFG["connected_cdp_url"] = (
            f"http://127.0.0.1:{server.server_address[1]}"
        )
        # Ambient route selectors and provider/LLM keys must be scrubbed from
        # the local call env (never os.environ).
        ambient = {
            "BU_CDP_URL": "http://127.0.0.1:9",
            "BU_AUTOSPAWN": "1",
            "BROWSER_CDP_URL": "http://127.0.0.1:9",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "OPENAI_API_KEY": "sk-open-secret",
            "BROWSERBASE_API_KEY": "bb-secret",
        }

        fake_stderr = "ws://127.0.0.1:1/secret-endpoint\npassword=supersecret\n"
        calls: list = []
        with mock.patch.dict(os.environ, ambient, clear=False), \
                self._fake_cli(returncode=1, stderr=fake_stderr, calls=calls):
            out = patched.connected_browser_exec(
                "print(1)", session=SAMPLE_SESSION, timeout_s=30,
            )
        text = str(out)
        self.assertIn("Connected browser exec failed", text)
        for leak in ("secret-endpoint", "supersecret", "ws://", "127.0.0.1"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text)
        self.assertEqual(len(calls), 1, "connected failure must not retry/fall back")
        env = calls[0]["env"]
        for key in ambient:
            with self.subTest(scrubbed=key):
                self.assertNotIn(key, env)
        self.assertEqual(env.get("BU_CDP_WS"), ws_url)
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertEqual(env.get("BU_NAME"), connected_daemon_name(endpoint, SAMPLE_SESSION))
        # Own-tab preamble must be prepended to the piped code.
        self.assertTrue(str(calls[0]["input"]).startswith("# Own-tab preamble"))

    def test_connected_timeout_failure_is_generic(self) -> None:
        server, _ws = self._start_discovery_server()
        self.addCleanup(server.shutdown)
        patched = self._load_patched("conn_timeout")
        patched._SKELETON_BROWSER_CFG["connected_cdp_url"] = (
            f"http://127.0.0.1:{server.server_address[1]}"
        )
        with self._fake_cli(returncode=0, timeout_error=True):
            out = patched.connected_browser_exec(
                "import time; time.sleep(300)", session="", timeout_s=5,
            )
        text = str(out)
        self.assertIn("timed out after", text)
        self.assertNotIn("ws://", text)

    def test_connected_success_with_preflight_and_fake_cli(self) -> None:
        server, ws_url = self._start_discovery_server()
        self.addCleanup(server.shutdown)
        patched = self._load_patched("conn_ok")
        patched._SKELETON_BROWSER_CFG["connected_cdp_url"] = (
            f"http://127.0.0.1:{server.server_address[1]}"
        )
        calls: list = []
        with self._fake_cli(returncode=0, calls=calls):
            out = patched.connected_browser_exec(
                "print(1)", session=SAMPLE_SESSION, timeout_s=30,
            )
        self.assertIsInstance(out, dict)
        self.assertTrue(out.get("success") is True, str(out))
        self.assertEqual(len(calls), 1)
        env = calls[0]["env"]
        self.assertEqual(env.get("BU_CDP_WS"), ws_url)
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertEqual(env.get("BU_NAME"), connected_daemon_name(endpoint, SAMPLE_SESSION))

    def test_connected_unconfigured_fails_closed_generic(self) -> None:
        patched = self._load_patched("conn_unconfigured")
        # _SKELETON_BROWSER_CFG is empty: no endpoint configured.
        out = patched.connected_browser_exec("print(1)", session=SAMPLE_SESSION, timeout_s=30)
        text = str(out)
        self.assertIn("Connected browser is not configured", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("ws://", text)

    def test_normalize_connected_ws_url_rewrites_loopback_for_remote_endpoint(self) -> None:
        """A loopback-reported ws authority is rewritten to the configured
        non-loopback (bridge/remote) endpoint authority, preserving the
        ws/wss scheme and path/query."""
        patched = self._load_patched("norm_remote")
        fn = patched._normalize_connected_ws_url
        self.assertEqual(
            fn("ws://127.0.0.1:9222/devtools/browser/x?q=1", "http://fixture:9222"),
            "ws://fixture:9222/devtools/browser/x?q=1",
        )
        self.assertEqual(
            fn("wss://localhost:9223/p?q=1#f", "https://fixture:9223"),
            "wss://fixture:9223/p?q=1#f",
        )
        self.assertEqual(
            fn("ws://[::1]:9222/x", "http://fixture:9222"),
            "ws://fixture:9222/x",
        )
        # Default port when the endpoint omits it.
        self.assertEqual(
            fn("ws://127.0.0.1/p", "http://fixture"),
            "ws://fixture:80/p",
        )

    def test_normalize_connected_ws_url_never_touches_loopback_production(self) -> None:
        """The production 127.0.0.1:9222 layout is unchanged; non-loopback
        reports are never rewritten; malformed inputs degrade safely."""
        patched = self._load_patched("norm_loopback")
        fn = patched._normalize_connected_ws_url
        # Loopback report + loopback endpoint (production layout): unchanged.
        self.assertEqual(
            fn("ws://127.0.0.1:9222/devtools/browser/x", "http://127.0.0.1:9222"),
            "ws://127.0.0.1:9222/devtools/browser/x",
        )
        # Non-loopback report: never rewritten.
        self.assertEqual(
            fn("ws://fixture:9222/devtools/browser/x", "http://fixture:9222"),
            "ws://fixture:9222/devtools/browser/x",
        )
        # Malformed inputs degrade to the unchanged URL (caller validation
        # already rejected non-ws schemes; nothing is logged).
        self.assertEqual(fn("not a url", "http://fixture:9222"), "not a url")
        self.assertEqual(fn("ws://127.0.0.1:9222/x", "not a url"), "ws://127.0.0.1:9222/x")
        self.assertEqual(fn("", "http://fixture:9222"), "")


class BrowserRoutingPortProbeContractTests(unittest.TestCase):
    """The runtime listener probes parse /proc/net/tcp hex ports: 9222 must
    be probed as 0x2406 (0x2404 is 9220 — a silent false negative)."""

    def test_9222_port_hex_is_2406(self) -> None:
        self.assertEqual(hex(9222), "0x2406")

    def test_runtime_listener_probes_use_2406_not_2404(self) -> None:
        runtime_src = (REPO_ROOT / "tests" / "runtime" / "test_browser_routing_runtime.py").read_text(encoding="utf-8")
        self.assertIn(":2406", runtime_src)
        self.assertNotIn(":2404", runtime_src)


if __name__ == "__main__":
    unittest.main()
