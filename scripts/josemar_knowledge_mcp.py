#!/usr/bin/env python3
"""Bounded stdio MCP surface exposing curated read-only gbrain tools plus a
user-approved ``josemar_chat`` tool that calls the local Hermes API.

This server is launched as a forced SSH command by the supervised sshd inside the Hermes container
sidecar (see ``docker-compose.josemar-mcp.yml``). It is exposed privately via
Tailscale Serve TCP (never Funnel). The remote MCP client is treated as
user-equivalent. The server offers two distinct classes of tools:

  - **Curated read-only gbrain tools** (no vault write access):
    - ``vault_search(query, max_results=...)``  -> bounded ``gbrain search``
    - ``vault_get(path)``                       -> bounded ``gbrain get``
    - ``project_context()``                     -> allowlisted gbrain reads of
                                                   project-context/memory notes
  - **``josemar_chat(prompt, ...)``** — a full trusted/user-equivalent
    capability: a bounded POST to the internal Hermes OpenAI-compatible API
    (``/v1/chat/completions``). This can trigger Hermes tools and incur model
    cost. It is intentionally NOT read-only; it is the remote equivalent of
    talking to Josemar directly.

No shell, arbitrary command, port forwarding, or direct API exposure is
offered to the remote client. ``gbrain`` is invoked directly with the
deployment env; only bounded read commands are allowed. ``josemar_chat``
uses ``API_SERVER_KEY`` injected only server-side (never sent to the client).

Stdout is reserved for MCP protocol traffic; all diagnostics go to stderr.
Structured output and errors avoid logging sensitive prompt/content.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError


LOGGER = logging.getLogger("josemar-knowledge-mcp")

mcp = FastMCP(
    "josemar-knowledge",
    instructions=(
        "Curated read-only access to the Josemar Obsidian vault via gbrain "
        "(vault_search, vault_get, project_context), plus josemar_chat — a "
        "full trusted/user-equivalent capability that calls the local Hermes "
        "API and can trigger Hermes tools and incur model cost. The vault "
        "tools are read-only; josemar_chat is intentionally not read-only. "
        "No shell, arbitrary command, port forwarding, or direct API "
        "exposure is available."
    ),
    log_level="WARNING",
)


# ---------------------------------------------------------------------------
# Bounds and constants
# ---------------------------------------------------------------------------

SEARCH_MAX_RESULTS = 50
SEARCH_DEFAULT_MAX_RESULTS = 10

# Hard cap on a single gbrain subprocess stdout (bytes). Matches the
# tasknotes_mcp_core MAX_OUTPUT posture: prevents a runaway page/search from
# exhausting memory.
MAX_OUTPUT_BYTES = 2 * 1024 * 1024

# Subprocess timeout for gbrain reads (seconds). Non-mutating reads must not
# disrupt writers; a bounded timeout keeps the server responsive.
GBRAIN_TIMEOUT = 60.0

# josemar_chat bounds.
CHAT_MAX_PROMPT_CHARS = 16000
CHAT_MAX_TOKENS = 2048
CHAT_TIMEOUT = 120.0

# Allowlisted project-context/memory note slugs. These are read via
# ``gbrain get`` only; no write access is invented. The list is intentionally
# short and explicit so a remote client cannot read arbitrary notes through
# this tool (use vault_get for general reads, which is still read-only).
PROJECT_CONTEXT_SLUGS = (
    "inbox/josemar-project-context",
    "inbox/josemar-memory",
)

# Allowlisted gbrain subcommands. Anything else is rejected before exec.
ALLOWED_GBRAIN_SUBCOMMANDS = frozenset({"search", "get"})

# Slug/path validation: gbrain slugs are lowercase kebab/path segments. We
# accept a conservative subset and reject path traversal, absolute paths,
# parent references, symlinks-as-string, and control characters.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationError(ToolError):
    """A user-facing validation error (safe to echo the message)."""


def _validate_max_results(max_results: int) -> int:
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ValidationError("max_results must be an integer")
    if max_results < 1 or max_results > SEARCH_MAX_RESULTS:
        raise ValidationError(
            f"max_results must be between 1 and {SEARCH_MAX_RESULTS}"
        )
    return max_results


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValidationError("query must be a string")
    stripped = query.strip()
    if not stripped:
        raise ValidationError("query must not be empty")
    if len(stripped) > 2000:
        raise ValidationError("query must be at most 2000 characters")
    # Reject control characters (newlines allowed but collapsed).
    if any(ord(ch) < 0x20 and ch not in "\n\r\t" for ch in stripped):
        raise ValidationError("query contains control characters")
    return " ".join(stripped.split())


def _validate_path(path: str) -> str:
    if not isinstance(path, str):
        raise ValidationError("path must be a string")
    stripped = path.strip()
    if not stripped:
        raise ValidationError("path must not be empty")
    if len(stripped) > 512:
        raise ValidationError("path must be at most 512 characters")
    # Reject absolute paths, parent traversal, and any control characters.
    if stripped.startswith("/") or stripped.startswith("~"):
        raise ValidationError("path must be a relative vault slug")
    if ".." in stripped.split("/"):
        raise ValidationError("path must not contain parent references")
    if any(ord(ch) < 0x20 for ch in stripped):
        raise ValidationError("path contains control characters")
    if not _SLUG_RE.match(stripped):
        raise ValidationError(
            "path must match lowercase kebab/path segments "
            "(a-z0-9 and . _ / - only)"
        )
    return stripped


def _validate_prompt(prompt: str) -> str:
    if not isinstance(prompt, str):
        raise ValidationError("prompt must be a string")
    stripped = prompt.strip()
    if not stripped:
        raise ValidationError("prompt must not be empty")
    if len(stripped) > CHAT_MAX_PROMPT_CHARS:
        raise ValidationError(
            f"prompt must be at most {CHAT_MAX_PROMPT_CHARS} characters"
        )
    return stripped


# ---------------------------------------------------------------------------
# gbrain subprocess (bounded, allowlisted, deployment env)
# ---------------------------------------------------------------------------


def _gbrain_bin() -> str:
    return os.environ.get("JOSEMAR_MCP_GBRAIN_BIN", "/usr/local/bin/gbrain")


def _gbrain_env() -> dict[str, str]:
    """Build a minimal deployment env for gbrain subprocesses.

    Only HOME, PATH, LANG, LC_ALL, TZ, GBRAIN_HOME, GBRAIN_BRAIN_REPO,
    GBRAIN_SCHEMA_PACK, and GBRAIN_SKIP_STARTUP_HOOKS=1 are set. No provider
    API credentials are inherited. This mirrors the tasknotes_mcp_core
    posture.
    """
    return {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "TZ": os.environ.get("TZ", "UTC"),
        "GBRAIN_HOME": os.environ.get("GBRAIN_HOME", "/opt/data"),
        "GBRAIN_BRAIN_REPO": os.environ.get(
            "GBRAIN_BRAIN_REPO", "/opt/data/obsidian"
        ),
        "GBRAIN_SCHEMA_PACK": os.environ.get("GBRAIN_SCHEMA_PACK", "josemar"),
        "GBRAIN_SKIP_STARTUP_HOOKS": "1",
    }


def _run_gbrain(argv: list[str]) -> str:
    """Run an allowlisted gbrain command with bounded I/O and timeout.

    Returns stdout text. Raises ``ToolError`` on any failure. Never logs
    content. The argv is validated to start with an allowlisted subcommand.
    """
    if len(argv) < 2 or argv[1] not in ALLOWED_GBRAIN_SUBCOMMANDS:
        raise ToolError("disallowed gbrain subcommand")
    try:
        proc = subprocess.run(
            argv,
            env=_gbrain_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GBRAIN_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError("gbrain read timed out") from exc
    except FileNotFoundError as exc:
        raise ToolError("gbrain binary not found") from exc
    except OSError as exc:
        raise ToolError(f"gbrain failed to start: {exc}") from exc

    stdout = proc.stdout.decode("utf-8", errors="replace")
    if len(stdout.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES:
        raise ToolError("gbrain output exceeded size bound")
    if proc.returncode != 0:
        # Never echo stderr content (may contain diagnostics). Surface a
        # generic, sanitized failure.
        stderr_tail = proc.stderr.decode("utf-8", errors="replace").strip()
        sanitized = stderr_tail[:120].replace("\n", " ")
        raise ToolError(f"gbrain failed (rc={proc.returncode}): {sanitized}")
    return stdout


def _parse_json_loose(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Lock coordination (exclusive serialization for gbrain subprocesses)
# ---------------------------------------------------------------------------


def _lock_path() -> Path:
    return Path(
        os.environ.get("JOSEMAR_MCP_LOCK_DIR", "/opt/data/.locks")
    ) / "tasknotes.lock"


class _GbrainLock:
    """Acquire an exclusive (``LOCK_EX``) advisory lock with a bounded wait.

    gbrain's PGLite has its own internal locking, but the remote side must
    serialize its own gbrain invocations so they do not race with each other
    or with Hermes-side writers (tasknotes mutations, the refresh cron,
    embed-backfill). The lock file is ``/opt/data/.locks/tasknotes.lock``,
    shared with tasknotes mutations (which also take an exclusive lock), the
    refresh cron, and embed-backfill. An exclusive lock here means the
    sidecar's gbrain reads serialize against all those writers — this is
    correct because gbrain search/get may touch PGLite state (WAL, lock
    files) and must not race with concurrent writers.

    Fail-closed: if the lock cannot be acquired within the timeout (e.g. a
    writer holds the lock), raises ``ToolError`` with a generic "vault busy"
    message. The read is NOT performed without the lock — a hard failure is
    preferable to an uncoordinated read that could race with a writer or
    corrupt PGLite state.

    Used as a context manager so the lock is held only for the duration of
    the gbrain subprocess, not around validation or result normalization.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def __enter__(self) -> "_GbrainLock":
        import fcntl

        path = _lock_path()
        timeout = float(os.environ.get("JOSEMAR_MCP_LOCK_TIMEOUT", "10"))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            LOGGER.warning("cannot open lock file: %s", exc)
            raise ToolError("vault busy: lock unavailable") from exc
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    LOGGER.warning("lock acquisition timed out after %ss", timeout)
                    raise ToolError("vault busy: lock timed out") from None
                time.sleep(0.1)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            import fcntl

            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None


# ---------------------------------------------------------------------------
# gbrain read helpers (parsing only; subprocess invocation is in the tool
# functions so the exclusive lock is held only during the subprocess)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# josemar_chat helper (internal Hermes API, server-side key)
# ---------------------------------------------------------------------------


def _chat_endpoint() -> str:
    # The MCP server runs inside the Hermes container, so the API is reached
    # on loopback — not over the Docker network.
    host = os.environ.get("JOSEMAR_MCP_CHAT_HOST", "localhost")
    port = os.environ.get("JOSEMAR_MCP_CHAT_PORT", "8642")
    return f"http://{host}:{port}/v1/chat/completions"


def _chat_api_key() -> str:
    key = os.environ.get("API_SERVER_KEY", "")
    if not key:
        raise ToolError("chat API key is not configured server-side")
    return key


def _chat_model() -> str:
    return os.environ.get("JOSEMAR_MCP_CHAT_MODEL", "Josemar")


def _post_chat(prompt: str, max_tokens: int) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": _chat_model(),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib_request.Request(
        _chat_endpoint(),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_chat_api_key()}",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=CHAT_TIMEOUT) as resp:
            body = resp.read()
    except urllib_error.HTTPError as exc:
        # Never include the API key or full response body in errors.
        raise ToolError(f"chat API returned HTTP {exc.code}") from exc
    except urllib_error.URLError as exc:
        raise ToolError("chat API unreachable on internal network") from exc
    except TimeoutError as exc:
        raise ToolError("chat API timed out") from exc
    if len(body) > MAX_OUTPUT_BYTES:
        raise ToolError("chat API response exceeded size bound")
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ToolError("chat API returned non-JSON") from exc
    if not isinstance(data, dict):
        raise ToolError("chat API returned non-object")
    # Extract only the assistant message content; never echo the key.
    choices = data.get("choices", [])
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message", {})
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str):
                    return {
                        "content": content,
                        "model": str(data.get("model", "")) or "",
                        "usage": data.get("usage", {}),
                    }
    return {"content": "", "model": str(data.get("model", "")) or "", "usage": {}}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(structured_output=True)
def vault_search(query: str, max_results: int = SEARCH_DEFAULT_MAX_RESULTS) -> list[dict[str, Any]]:
    """Search the Josemar Obsidian vault via gbrain (read-only, keyword or
    hybrid depending on deployment). Returns bounded metadata snippets
    (slug, title, type, score, snippet); never full page content. Use
    ``vault_get`` to read a specific page.
    """
    q = _validate_query(query)
    n = _validate_max_results(max_results)
    argv = [_gbrain_bin(), "search", q, "--limit", str(n), "--json"]
    with _GbrainLock():
        stdout = _run_gbrain(argv)
    return _parse_search_results(stdout, n)


def _parse_search_results(stdout: str, max_results: int) -> list[dict[str, Any]]:
    data = _parse_json_loose(stdout)
    if data is None:
        return []
    if isinstance(data, list):
        results = data
    elif isinstance(data, dict):
        results = (
            data.get("results")
            or data.get("pages")
            or data.get("items")
            or []
        )
    else:
        return []
    if not isinstance(results, list):
        return []
    out: list[dict[str, Any]] = []
    for item in results[:max_results]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "slug": str(item.get("slug", item.get("path", ""))) or "",
                "title": str(item.get("title", "")) or "",
                "type": str(item.get("type", "")) or "",
                "score": item.get("score"),
                "snippet": str(item.get("snippet", item.get("summary", ""))) or "",
            }
        )
    return out


@mcp.tool(structured_output=True)
def vault_get(path: str) -> dict[str, Any]:
    """Read one vault page by slug/path (read-only). Returns the page title,
    type, tags, frontmatter, and compiled content. Path traversal and
    absolute paths are rejected.
    """
    p = _validate_path(path)
    argv = [_gbrain_bin(), "get", p, "--json"]
    with _GbrainLock():
        stdout = _run_gbrain(argv)
    return _parse_get_result(stdout)


def _parse_get_result(stdout: str) -> dict[str, Any]:
    data = _parse_json_loose(stdout)
    if data is None:
        raise ToolError("gbrain get returned non-JSON output")
    if not isinstance(data, dict):
        raise ToolError("gbrain get returned non-object")
    if "error" in data and data.get("error") == "page_not_found":
        raise ToolError("page not found")
    return {
        "slug": str(data.get("slug", data.get("path", ""))) or "",
        "title": str(data.get("title", "")) or "",
        "type": str(data.get("type", "")) or "",
        "tags": list(data.get("tags", []) or []),
        "frontmatter": data.get("frontmatter", {}) or {},
        "content": str(data.get("compiled_truth", data.get("content", ""))) or "",
    }


@mcp.tool(structured_output=True)
def project_context() -> dict[str, Any]:
    """Return curated project-context/memory notes via allowlisted gbrain
    reads only. No write access is available. The allowlist is fixed in the
    server source; a remote client cannot expand it. Missing notes are
    reported as ``null`` rather than failing the whole call.
    """
    out: dict[str, Any] = {}
    # Acquire the exclusive lock once for all allowlisted reads; hold it only
    # for the duration of the gbrain subprocesses, not around result
    # normalization.
    with _GbrainLock():
        for slug in PROJECT_CONTEXT_SLUGS:
            argv = [_gbrain_bin(), "get", slug, "--json"]
            try:
                stdout = _run_gbrain(argv)
                out[slug] = _parse_get_result(stdout)
            except ToolError:
                out[slug] = None
    return out


@mcp.tool(structured_output=True)
def josemar_chat(
    prompt: str,
    max_tokens: int = CHAT_MAX_TOKENS,
) -> dict[str, Any]:
    """Send a bounded prompt to the local Josemar Hermes API
    (``/v1/chat/completions``) on the internal Docker network.

    This is a **full trusted/user-equivalent capability**, not a read-only
    tool. It is the remote equivalent of talking to Josemar directly: it can
    trigger Hermes tools (which may read/write the vault, send messages,
    run skills, etc.) and incur model cost on every call. It is distinct
    from the curated read-only gbrain tools (vault_search, vault_get,
    project_context), which cannot mutate state and do not invoke the
    model.

    The API key is injected server-side and never returned to the client.
    The operator is responsible for prompts sent through this tool.
    """
    p = _validate_prompt(prompt)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
        raise ValidationError("max_tokens must be an integer")
    if max_tokens < 1 or max_tokens > CHAT_MAX_TOKENS:
        raise ValidationError(
            f"max_tokens must be between 1 and {CHAT_MAX_TOKENS}"
        )
    return _post_chat(p, max_tokens)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the server over stdio. Stdout is reserved for MCP traffic."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()