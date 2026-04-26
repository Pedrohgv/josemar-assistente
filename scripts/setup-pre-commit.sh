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

echo "Installing pre-commit..."
source "$VENV_DIR/bin/activate"
pip install --upgrade pre-commit

echo "Installing git hooks..."
pre-commit install

echo ""
echo "=== Setup Complete ==="
echo "  Pre-commit hooks installed. Gitleaks + PII guard will run on every commit."
echo "  To skip (emergency only): SKIP=gitleaks,pii-guard git commit -m 'message'"
echo "  To update hooks: source $VENV_DIR/bin/activate && pre-commit run --all-files"
