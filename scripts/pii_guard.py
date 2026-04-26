#!/usr/bin/env python3
"""PII guard for git diffs (staged or commit range)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_BR_RE = re.compile(
    r"(?<!\d)(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?(?:9\d{4}|\d{4})[-\s]?\d{4}(?!\d)"
)
CPF_RE = re.compile(r"(?<!\d)(?:\d{3}[.\s-]?\d{3}[.\s-]?\d{3}[.\s-]?\d{2})(?!\d)")
CNPJ_RE = re.compile(r"(?<!\d)(?:\d{2}[.\s-]?\d{3}[.\s-]?\d{3}[/\s-]?\d{4}[.\s-]?\d{2})(?!\d)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

EXAMPLE_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "acme.example",
}

SEVERITY_RANK = {"none": 0, "medium": 1, "high": 2}


@dataclass
class Finding:
    kind: str
    severity: str
    file_path: str
    line_number: int
    value: str
    line_text: str


def _run_git_diff(*, staged: bool, base: str | None, head: str | None, three_dot: bool) -> str:
    cmd = ["git", "diff", "--no-color", "--unified=0"]
    if staged:
        cmd.append("--cached")
    if base and head:
        separator = "..." if three_dot else ".."
        cmd.append(f"{base}{separator}{head}")

    completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git diff failed")
    return completed.stdout


def _parse_added_lines(diff_text: str) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    current_file = ""
    new_line_number = 0

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[len("+++ b/") :]
            continue

        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw_line)
            if match:
                new_line_number = int(match.group(1))
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            findings.append((current_file, new_line_number, raw_line[1:]))
            new_line_number += 1
            continue

        if raw_line.startswith(" "):
            new_line_number += 1

    return findings


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _valid_cpf(value: str) -> bool:
    digits = _digits(value)
    if len(digits) != 11:
        return False
    if len(set(digits)) == 1:
        return False

    weights_1 = list(range(10, 1, -1))
    total_1 = sum(int(digits[i]) * weights_1[i] for i in range(9))
    dv1 = (total_1 * 10 % 11) % 10
    if dv1 != int(digits[9]):
        return False

    weights_2 = list(range(11, 1, -1))
    total_2 = sum(int(digits[i]) * weights_2[i] for i in range(10))
    dv2 = (total_2 * 10 % 11) % 10
    return dv2 == int(digits[10])


def _valid_cnpj(value: str) -> bool:
    digits = _digits(value)
    if len(digits) != 14:
        return False
    if len(set(digits)) == 1:
        return False

    weights_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    total_1 = sum(int(digits[i]) * weights_1[i] for i in range(12))
    rem_1 = total_1 % 11
    dv1 = 0 if rem_1 < 2 else 11 - rem_1
    if dv1 != int(digits[12]):
        return False

    weights_2 = [6] + weights_1
    total_2 = sum(int(digits[i]) * weights_2[i] for i in range(13))
    rem_2 = total_2 % 11
    dv2 = 0 if rem_2 < 2 else 11 - rem_2
    return dv2 == int(digits[13])


def _luhn_valid(value: str) -> bool:
    digits = _digits(value)
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, ch in enumerate(digits):
        number = int(ch)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        checksum += number
    return checksum % 10 == 0


def _is_example_email(value: str) -> bool:
    _, _, domain = value.lower().rpartition("@")
    return domain in EXAMPLE_EMAIL_DOMAINS


def _load_allowlist(path: Path) -> list[re.Pattern[str]]:
    if not path.exists():
        return []

    patterns: list[re.Pattern[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        patterns.append(re.compile(raw))
    return patterns


def _matches_allowlist(finding: Finding, allow_patterns: list[re.Pattern[str]]) -> bool:
    if not allow_patterns:
        return False
    subject = f"{finding.file_path}:{finding.line_number}:{finding.kind}:{finding.value}:{finding.line_text}"
    return any(pattern.search(subject) for pattern in allow_patterns)


def _collect_findings(added_lines: list[tuple[str, int, str]]) -> list[Finding]:
    findings: list[Finding] = []

    for file_path, line_number, line_text in added_lines:
        if not file_path or file_path == "/dev/null":
            continue

        for match in EMAIL_RE.finditer(line_text):
            value = match.group(0)
            if _is_example_email(value):
                continue
            findings.append(Finding("email", "medium", file_path, line_number, value, line_text.strip()))

        for match in PHONE_BR_RE.finditer(line_text):
            value = match.group(0)
            if len(_digits(value)) < 10:
                continue
            findings.append(Finding("phone", "medium", file_path, line_number, value, line_text.strip()))

        for match in CPF_RE.finditer(line_text):
            value = match.group(0)
            if _valid_cpf(value):
                findings.append(Finding("cpf", "high", file_path, line_number, value, line_text.strip()))

        for match in CNPJ_RE.finditer(line_text):
            value = match.group(0)
            if _valid_cnpj(value):
                findings.append(Finding("cnpj", "high", file_path, line_number, value, line_text.strip()))

        for match in CARD_RE.finditer(line_text):
            value = match.group(0)
            if _luhn_valid(value):
                findings.append(Finding("credit_card", "high", file_path, line_number, value, line_text.strip()))

    deduped: dict[tuple[str, int, str, str], Finding] = {}
    for item in findings:
        key = (item.file_path, item.line_number, item.kind, item.value)
        deduped[key] = item
    return sorted(deduped.values(), key=lambda item: (item.file_path, item.line_number, item.kind, item.value))


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan added git diff lines for potential PII.")
    parser.add_argument("--staged", action="store_true", help="Scan staged diff (`git diff --cached`).")
    parser.add_argument("--base", help="Base git ref/sha for range scan.")
    parser.add_argument("--head", help="Head git ref/sha for range scan.")
    parser.add_argument("--three-dot", action="store_true", help="Use three-dot diff (base...head).")
    parser.add_argument(
        "--fail-on",
        choices=["none", "medium", "high"],
        default="high",
        help="Fail when finding severity is at or above this threshold.",
    )
    parser.add_argument(
        "--allowlist",
        default=".pii-allowlist",
        help="Path to regex allowlist file.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args()

    if (args.base and not args.head) or (args.head and not args.base):
        parser.error("--base and --head must be provided together")

    try:
        diff_text = _run_git_diff(
            staged=args.staged,
            base=args.base,
            head=args.head,
            three_dot=args.three_dot,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    added_lines = _parse_added_lines(diff_text)
    findings = _collect_findings(added_lines)
    allow_patterns = _load_allowlist(Path(args.allowlist))
    filtered = [item for item in findings if not _matches_allowlist(item, allow_patterns)]

    threshold = SEVERITY_RANK[args.fail_on]
    failing = [item for item in filtered if SEVERITY_RANK[item.severity] >= threshold]

    if args.json:
        payload = {
            "scanned_added_lines": len(added_lines),
            "findings": [item.__dict__ for item in filtered],
            "failing": [item.__dict__ for item in failing],
            "fail_on": args.fail_on,
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(f"PII guard scanned {len(added_lines)} added line(s)")
        if not filtered:
            print("PII guard: no findings")
        else:
            print(f"PII guard: {len(filtered)} finding(s)")
            for item in filtered:
                flag = "BLOCK" if item in failing else "WARN"
                print(
                    f"[{flag}] {item.severity.upper():6} {item.kind:11} "
                    f"{item.file_path}:{item.line_number} -> {item.value}"
                )

    if failing:
        print(
            "PII guard blocked this change. Remove/redact data or add an explicit regex exception in .pii-allowlist.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
