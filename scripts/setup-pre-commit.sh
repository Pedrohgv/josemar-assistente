#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$REPO_ROOT/venv"

echo "=== Pre-commit Setup ==="

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
else
    echo "Using existing virtual environment at $VENV_DIR"
fi

echo "Installing tracked test requirements (authoritative: requirements-test.txt)..."
source "$VENV_DIR/bin/activate"
# requirements-test.txt is the single reproducible manifest for the local
# fast-test/pre-commit environment. It pins pre-commit and the fast-test
# dependencies (mcp, httpx, httpx-sse, pydantic, pydantic-settings, PyYAML,
# sqlite-vec). This venv is NOT a production Docker dependency (Dockerfile.hermes
# and aux-ml/Dockerfile install their own production requirements). Do not
# upgrade an unpinned pre-commit separately — the pinned version here wins.
if [ -f "$REPO_ROOT/requirements-test.txt" ]; then
    pip install -r "$REPO_ROOT/requirements-test.txt"
else
    echo "ERROR: requirements-test.txt not found at $REPO_ROOT/requirements-test.txt" >&2
    exit 1
fi

echo "Installing git hooks..."
pre-commit install

echo ""
echo "=== Setup Complete ==="
echo "  Pre-commit hooks installed. Gitleaks, PII guard, and fast tests will run on every commit."
echo "  To skip (emergency only): SKIP=gitleaks,pii-guard,josemar-fast-tests git commit -m 'message'"
echo "  To update hooks: source $VENV_DIR/bin/activate && pre-commit run --all-files"
