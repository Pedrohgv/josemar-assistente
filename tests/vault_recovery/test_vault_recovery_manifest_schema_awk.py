"""Regression tests for the shell manifest-schema validator
(scripts/vault-recovery-manifest-schema.awk) — ERE interval expressions.

PR #117 regression: the validator's patterns used POSIX ERE interval
expressions (``{64}``, ``{8}``, ``{12}``, ``{3,4}``). Old mawk (1.3.3, the
default on the CI base images) does NOT support interval expressions: it
treats the braces as LITERAL characters, so ``match("...", "^[0-9a-f]{64}$")``
never matched and EVERY valid manifest was rejected (the uploader/recover
gates then refused everything). The fix replaced every interval with an
explicit length/slice check + a single-repeatable class, preserving
byte-for-byte parity with the authoritative Python validator
(``vault_recovery_core.validate_manifest_schema``) — including the exact
generation-token field widths (8 date digits, T, 12 time digits, Z, '-',
8 hex suffix, total 31 chars).

This module pins the fix three ways:

1. ``test_no_ere_interval_expressions_in_source`` — a STATIC guard that the
   validator source contains no interval expression (comments stripped).
   This is the portable-awk property and fails on ANY awk host if an
   interval is ever reintroduced, regardless of the local awk's support.
2. ``test_awk_validator_parity_with_python`` — a behavioral battery run
   under the host ``awk`` (and under ``busybox awk`` when available — the
   pinned rclone image ships busybox awk only), asserting accept/reject
   parity with the Python validator on every case, including the exact
   field-width traps the interval-free rewrite must still reject.
3. ``test_valid_manifest_accepted_under_interval_literal_semantics`` — when
   the host awk actually exhibits old-mawk literal-brace semantics (probed
   at runtime), the battery IS exercised under that semantics: the exact
   CI failure mode. On modern awks the probe skips; the static guard and
   the parity battery still cover the property there.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCHEMA_AWK = SCRIPTS_DIR / "vault-recovery-manifest-schema.awk"
CORE_MODULE = SCRIPTS_DIR / "vault_recovery_core.py"

# A POSIX ERE interval expression ({n}, {n,m}, {n,}) — what old mawk 1.3.3
# treats as literal braces.
_INTERVAL_RE = re.compile(r"\{[0-9]+(,[0-9]*)?\}")


def _import_core():
    """Import the exporter core from source (repo convention: no package
    install required, no reliance on sys.path)."""
    spec = importlib.util.spec_from_file_location("vault_recovery_core", str(CORE_MODULE))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _import_core()


def _valid_manifest() -> dict:
    """A schema-version-1 manifest that satisfies every strict check
    (mirror of test_vault_recovery_core._valid_manifest)."""
    return {
        "schema_version": 1,
        "generation_id": "20260802T012247123456Z-a1b2c3d4",
        "created_at_utc": "2026-08-02T01:22:47Z",
        "phase": 1,
        "remote": {"uploaded": False, "note": "test"},
        "sources": {
            "gbrain_state_dir": "/opt/data/.gbrain",
            "vault_dir": "/opt/data/obsidian",
        },
        "trees": {
            ".gbrain": {
                "entries": 2, "dirs": 1, "files": 1, "bytes": 10,
                "root_mode": "0o700",
                "scan_digest": "0" * 64, "staged_digest": "0" * 64,
                "entries_file": ".gbrain.entries.txt",
                "entries_digest": "0" * 64,
            },
            "vault": {
                "entries": 2, "dirs": 1, "files": 1, "bytes": 10,
                "root_mode": "0o755",
                "scan_digest": "0" * 64, "staged_digest": "0" * 64,
                "entries_file": "vault.entries.txt",
                "entries_digest": "0" * 64,
            },
        },
        "doctor": {
            "report_schema_version": 2,
            "report_status": "healthy",
            "required_checks": {name: "ok" for name in core.REQUIRED_DOCTOR_CHECKS},
            "check_counts": {"ok": 5, "warn": 0, "fail": 0},
        },
        "convergence": {
            "attempts": 1, "max_attempts": 3,
            "source_scan_a_digest": "0" * 64, "source_scan_b_digest": "0" * 64,
        },
        "exporter": {"version": "2", "python": "3.12"},
    }


def _manifest_cases() -> list[tuple[str, dict]]:
    """The parity battery: (label, manifest). Every mutation targets the
    three interval-rewritten checks (hex64 digests, generation id, root
    mode) plus the valid baseline."""
    cases = [("valid baseline", _valid_manifest())]

    # --- generation_id: exact token field widths (31 chars total) ---
    # Same total length as a valid id, but 9 date digits + 11 time digits:
    # the interval-free rewrite must NOT weaken into a bare ^[0-9]+T[0-9]+Z-
    # shape — the Python validator rejects it, so must the awk twin.
    g = _valid_manifest()
    g["generation_id"] = "202608023T01224712345Z-a1b2c3d4"
    cases.append(("generation_id: shifted digit widths, still 31 chars", g))
    for label, gid in [
        ("7-digit date part", "2026080T012247123456Z-a1b2c3d4"),
        ("9-digit date part", "202608023T012247123456Z-a1b2c3d4"),
        ("11-digit time part", "20260802T01224712345Z-a1b2c3d4"),
        ("13-digit time part", "20260802T0122471234567Z-a1b2c3d4"),
        ("7-hex suffix", "20260802T012247123456Z-a1b2c3d"),
        ("9-hex suffix", "20260802T012247123456Z-a1b2c3d4f"),
        ("uppercase hex suffix", "20260802T012247123456Z-A1B2C3D4"),
        ("non-hex suffix char", "20260802T012247123456Z-a1b2c3dz"),
        ("wrong separator ':'", "20260802T012247123456Z:a1b2c3d4"),
        ("lowercase 't'", "20260802t012247123456Z-a1b2c3d4"),
        ("missing dash", "20260802T012247123456Za1b2c3d4"),
    ]:
        m = _valid_manifest()
        m["generation_id"] = gid
        cases.append((f"generation_id: {label}", m))

    # --- root_mode: "0o" plus exactly 3 or 4 octal digits ---
    for mode, expect_accept in [
        ("0o700", True),
        ("0o7000", True),
        ("0o0700", True),   # still exactly 4 digits
        ("0o70", False),    # too short
        ("0o70000", False),  # too long
        ("0o800", False),   # 8 is not an octal digit
        ("0o", False),
        ("0o70a", False),
        ("700", False),     # missing prefix
    ]:
        m = _valid_manifest()
        m["trees"]["vault"]["root_mode"] = mode
        cases.append((f"root_mode {mode!r} (accept={expect_accept})", m))

    # --- hex64 digests (scan/staged/entries + convergence digests) ---
    for label, digest in [
        ("64 hex digits", "0" * 64),
        ("63 hex digits", "0" * 63),
        ("65 hex digits", "0" * 65),
        ("non-hex char", "0" * 63 + "g"),
        ("uppercase hex", "A" * 64),
    ]:
        m = _valid_manifest()
        m["trees"][".gbrain"]["scan_digest"] = digest
        cases.append((f"scan_digest: {label}", m))
        m = _valid_manifest()
        m["convergence"]["source_scan_b_digest"] = digest
        cases.append((f"convergence digest: {label}", m))

    return cases


def _python_verdict(manifest: dict) -> str:
    try:
        core.validate_manifest_schema(manifest)
        return "accept"
    except core.VaultRecoveryError:
        return "reject"


def _awk_verdict(argv: list[str], manifest: dict) -> str:
    """Run the shell validator on a temp-file manifest (production usage:
    the uploader/recover scripts pass the manifest path). Returns the
    verdict: "accept" (exit 0) or "reject" (exit 1)."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(json.dumps(manifest, indent=2, sort_keys=True))
        path = fh.name
    try:
        proc = subprocess.run(
            [*argv, "-f", str(SCHEMA_AWK), path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    # Exit 0 = accept, 1 = reject (diagnostic on stderr). Anything else is a
    # validator harness failure (e.g. a syntax error in the awk program) and
    # must not masquerade as a verdict.
    if proc.returncode not in (0, 1):
        raise AssertionError(
            f"awk validator exited {proc.returncode} (not 0/1): "
            f"{proc.stderr.strip()!r}"
        )
    return "accept" if proc.returncode == 0 else "reject"


class ManifestSchemaAwkIntervalRegressionTests(unittest.TestCase):
    """The three guards for the PR #117 fix (old mawk, no ERE intervals)."""

    # ------------------------------------------------------------------
    # Static guard: the portable-awk property, enforceable on ANY host.
    # ------------------------------------------------------------------

    def test_no_ere_interval_expressions_in_source(self) -> None:
        """The validator must never use ERE interval expressions: old mawk
        1.3.3 treats the braces as literals and every match fails. Comments
        (which legitimately document the Python twin's interval regexes)
        are stripped before scanning."""
        source = SCHEMA_AWK.read_text(encoding="utf-8")
        code_lines = [
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        ]
        for lineno, line in enumerate(code_lines, start=1):
            self.assertIsNone(
                _INTERVAL_RE.search(line),
                f"{SCHEMA_AWK.name}:{lineno}: ERE interval expression in "
                f"code (old mawk 1.3.3 treats {{n}} as literal braces): {line!r}",
            )

    # ------------------------------------------------------------------
    # Behavioral battery: exact parity with the Python validator.
    # ------------------------------------------------------------------

    def _assert_battery_parity(self, interp: list[str], label: str) -> None:
        for name, manifest in _manifest_cases():
            with self.subTest(interpreter=label, case=name):
                python = _python_verdict(manifest)
                awk = _awk_verdict(interp, manifest)
                self.assertEqual(
                    awk, python,
                    f"{label}: awk verdict {awk!r} != python verdict {python!r} "
                    f"for case {name!r}",
                )

    def test_awk_validator_parity_with_python(self) -> None:
        self._assert_battery_parity(["awk"], "awk")

    def test_awk_validator_parity_under_busybox_awk(self) -> None:
        """The pinned rclone image runs busybox awk only — the validator
        must behave identically there too."""
        busybox = shutil.which("busybox")
        if busybox is None:
            self.skipTest("busybox not installed")
        probe = subprocess.run(
            [busybox, "awk", "BEGIN { print 1 }"],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "1":
            self.skipTest("busybox lacks the awk applet")
        self._assert_battery_parity([busybox, "awk"], "busybox awk")

    # ------------------------------------------------------------------
    # Interval-literal semantics: the actual CI failure mode, when the
    # host awk exhibits it (old mawk). Modern awks skip; the static guard
    # and the parity battery cover the property on those hosts.
    # ------------------------------------------------------------------

    def test_valid_manifest_accepted_under_interval_literal_semantics(self) -> None:
        """Probe: with interval support, ``^b{2}$`` matches only ``bb``
        (so ``match("b{2}", "^b{2}$")`` is 0); old mawk 1.3.3 matches the
        LITERAL braces (result 1). Under literal semantics the validator
        must still accept a fully valid manifest — the exact PR #117
        failure (old code rejected everything because ``{64}`` never
        matched)."""
        probe = subprocess.run(
            ["awk", 'BEGIN { exit (match("b{2}", "^b{2}$") == 1 ? 0 : 1) }'],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            self.skipTest(
                "host awk supports ERE intervals (no interval-literal "
                "semantics to exercise; static guard + parity battery cover it)"
            )
        verdict = _awk_verdict(["awk"], _valid_manifest())
        self.assertEqual(
            verdict, "accept",
            "valid manifest rejected under interval-literal (old mawk) "
            "semantics: the validator still depends on ERE intervals",
        )


if __name__ == "__main__":
    unittest.main()
