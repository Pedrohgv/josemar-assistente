#!/usr/bin/env python3
"""Verify the local test venv matches requirements-test.txt (issue #91).

The pre-commit hooks run the fast-test suite against the repository venv
(venv/). If that venv drifts from the pinned requirements-test.txt manifest
(e.g. a stale pin, a missing package, or an over-version install), the suite
fails late with a raw traceback. This script fails fast, before the suite runs,
with actionable setup guidance.

Stdlib-only by design: it must run even when the venv is broken or missing
dependencies, so it cannot rely on third-party packages (e.g. packaging).

Exit codes:
  0  all pinned packages are installed at the pinned version
  1  dependency drift or a missing package (actionable guidance printed)
  2  configuration/parse error (missing requirements file, malformed pin line)
"""

from __future__ import annotations

import importlib.metadata
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_PATH = REPO_ROOT / "requirements-test.txt"

# Strict `name==version` pin. Distribution names follow PEP 503; versions may
# include letters, digits, dots, underscores, plus signs, and local labels.
PIN_RE = re.compile(r"^([A-Za-z0-9._-]+)==([A-Za-z0-9._+!~-]+)$")


def parse_requirements(path: Path) -> list[tuple[str, str]]:
    """Parse a strict ``name==version`` manifest into (name, version) pairs.

    Full-line ``#`` comments and inline ``#`` trailing comments are allowed.
    Any other non-empty content line that is not a strict ``name==version`` pin
    is a hard error: silently skipping it would let a future unpinned
    dependency escape drift detection entirely.
    """
    pins: list[tuple[str, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = PIN_RE.match(line)
        if not match:
            raise ValueError(
                f"{path}:{lineno}: expected 'name==version', got: {raw!r}"
            )
        pins.append((match.group(1), match.group(2)))
    return pins


def check_versions(
    requirements_path: Path,
    version_lookup=importlib.metadata.version,
) -> tuple[int, list[str]]:
    """Compare installed versions against the manifest.

    ``version_lookup`` is injectable for tests. Returns (exit_code, messages).
    """
    try:
        pins = parse_requirements(requirements_path)
    except (OSError, ValueError) as exc:
        return 2, [str(exc)]

    problems = 0
    messages: list[str] = []
    for name, expected in pins:
        try:
            found = version_lookup(name)
        except importlib.metadata.PackageNotFoundError:
            problems += 1
            messages.append(f"NOT INSTALLED: {name} (expected {expected})")
            continue
        if found != expected:
            problems += 1
            messages.append(f"MISMATCH: {name} expected {expected}, found {found}")

    if problems:
        messages.append(
            "Reconcile with: scripts/setup-pre-commit.sh "
            "(or source venv/bin/activate && pip install -r requirements-test.txt). "
            "Emergency override: SKIP=test-env-drift pre-commit run --all-files"
        )
        return 1, messages
    return 0, ["all test-environment pins match requirements-test.txt"]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Optional manifest path (defaults to the repo's requirements-test.txt).
    # Kept simple: at most one positional argument.
    if len(args) > 1:
        print("usage: check-test-env.py [requirements-test.txt]", file=sys.stderr)
        return 2
    path = Path(args[0]) if args else REQUIREMENTS_PATH
    code, messages = check_versions(path)
    for msg in messages:
        print(msg, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
