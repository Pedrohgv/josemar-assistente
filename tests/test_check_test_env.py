"""Unit tests for scripts/check-test-env.py (issue #91).

The drift check is the mechanism behind the "detect dependency drift before a
commit" acceptance criterion, so it must be regression-tested. We exercise the
pure core (``check_versions``) with an injected fake ``version_lookup`` so the
tests run without a venv and without importing any third-party package.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-test-env.py"


def _load_module():
    """Import scripts/check_test_env.py without a scripts package (repo convention)."""
    spec = importlib.util.spec_from_file_location("check_test_env", str(SCRIPT_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()
check_versions = MOD.check_versions
parse_requirements = MOD.parse_requirements


def _write(tmp: str, content: str) -> Path:
    path = Path(tmp) / "requirements-test.txt"
    path.write_text(content, encoding="utf-8")
    return path


class ParseRequirementsTests(unittest.TestCase):
    def test_parses_strict_pins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "mcp==1.28.1\nhttpx==0.28.1\n")
            self.assertEqual(
                parse_requirements(path), [("mcp", "1.28.1"), ("httpx", "0.28.1")]
            )

    def test_ignores_comments_and_blanks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                "# header comment\n\nmcp==1.28.1  # inline comment\n\nhttpx==0.28.1\n",
            )
            self.assertEqual(
                parse_requirements(path), [("mcp", "1.28.1"), ("httpx", "0.28.1")]
            )

    def test_rejects_unpinned_line(self) -> None:
        # A future `>=`/`~=`/`-r` line must fail loudly, not be silently
        # skipped, so an unpinned dependency cannot escape drift detection.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "mcp==1.28.1\nmcp>=1.28\n")
            with self.assertRaises(ValueError) as ctx:
                parse_requirements(path)
            self.assertIn("mcp>=1.28", str(ctx.exception))
            self.assertIn("expected 'name==version'", str(ctx.exception))


class CheckVersionsTests(unittest.TestCase):
    def _lookup(self, versions: dict[str, str]):
        def lookup(name: str) -> str:
            # Replicate importlib.metadata's PEP 503 name normalization
            # (lowercase; runs of `-`/`_`/`.` collapse to `-`).
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if normalized not in versions:
                raise importlib.metadata.PackageNotFoundError(name)
            return versions[normalized]

        return lookup

    def test_clean_pass_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "mcp==1.28.1\nhttpx==0.28.1\n")
            code, messages = check_versions(
                path, self._lookup({"mcp": "1.28.1", "httpx": "0.28.1"})
            )
            self.assertEqual(code, 0)
            self.assertTrue(any("all test-environment pins match" in m for m in messages))

    def test_stale_pin_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "mcp==1.28.1\n")
            code, messages = check_versions(path, self._lookup({"mcp": "1.26.0"}))
            self.assertEqual(code, 1)
            self.assertTrue(any("expected 1.28.1, found 1.26.0" in m for m in messages))

    def test_over_version_drift_returns_one(self) -> None:
        # Someone pip-installs mcp 2.0.0 locally: must be caught too.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "mcp==1.28.1\n")
            code, messages = check_versions(path, self._lookup({"mcp": "2.0.0"}))
            self.assertEqual(code, 1)
            self.assertTrue(any("expected 1.28.1, found 2.0.0" in m for m in messages))

    def test_missing_package_returns_one_with_actionable_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "mcp==1.28.1\n")
            code, messages = check_versions(path, self._lookup({}))
            self.assertEqual(code, 1)
            self.assertTrue(any("NOT INSTALLED: mcp" in m for m in messages))
            self.assertTrue(any("scripts/setup-pre-commit.sh" in m for m in messages))

    def test_malformed_line_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "mcp>=1.28\n")
            code, messages = check_versions(path, self._lookup({}))
            self.assertEqual(code, 2)
            self.assertTrue(any("expected 'name==version'" in m for m in messages))

    def test_missing_requirements_file_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does-not-exist.txt"
            code, messages = check_versions(path, self._lookup({}))
            self.assertEqual(code, 2)
            self.assertTrue(messages)

    def test_name_normalization_via_importlib_metadata(self) -> None:
        # PyYAML is normalized to `pyyaml` by importlib.metadata (PEP 503).
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "PyYAML==6.0.3\n")
            code, messages = check_versions(path, self._lookup({"pyyaml": "6.0.3"}))
            self.assertEqual(code, 0)


class MainEntryPointTests(unittest.TestCase):
    """Exercise the real CLI entry point (argv, default path, exit codes).

    Uses a subprocess with the same interpreter the pre-commit hook invokes, so
    the real importlib.metadata lookup and exit-code mapping are covered. The
    pin is drift-immune: it resolves a live version rather than a literal mcp
    version, so the test can never fail confusingly on a legitimate upgrade.
    """

    def _run(self, *args: str, cwd: str | None = None) -> int:
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            capture_output=True,
            cwd=cwd,
        ).returncode

    def test_clean_manifest_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, f"pip=={importlib.metadata.version('pip')}\n")
            self.assertEqual(self._run(str(path)), 0)

    def test_stale_pin_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "pip==0.0.0\n")
            self.assertEqual(self._run(str(path)), 1)

    def test_missing_manifest_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(str(Path(tmp) / "nope.txt")), 2)

    def test_too_many_args_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run("a.txt", "b.txt"), 2)


if __name__ == "__main__":
    unittest.main()
