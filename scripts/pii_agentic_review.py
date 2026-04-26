#!/usr/bin/env python3
"""Optional LLM-powered privacy review for added git diff lines."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request


SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _run_git_diff(base: str, head: str, three_dot: bool) -> str:
    sep = "..." if three_dot else ".."
    cmd = ["git", "diff", "--no-color", "--unified=0", f"{base}{sep}{head}"]
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git diff failed")
    return completed.stdout


def _parse_added_lines(diff_text: str) -> list[dict]:
    results: list[dict] = []
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
            results.append(
                {
                    "file_path": current_file,
                    "line_number": new_line_number,
                    "line_text": raw_line[1:][:600],
                }
            )
            new_line_number += 1
            continue

        if raw_line.startswith(" "):
            new_line_number += 1

    return results


def _extract_json_block(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {}

    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def _call_llm_review(lines: list[dict], *, model: str, base_url: str, api_key: str, timeout: int) -> dict:
    endpoint = base_url.rstrip("/") + "/chat/completions"

    system_prompt = (
        "You are a privacy/security reviewer. Analyze only the provided git added lines for likely PII exposure. "
        "Return strict JSON with this schema: "
        '{"summary":"string","findings":[{"file_path":"string","line_number":number,'
        '"severity":"low|medium|high","category":"string","reason":"string","suggestion":"string"}]}. '
        "Mark high only for strong evidence of real personal/sensitive data (CPF/CNPJ/document IDs, personal contacts, "
        "billing data, private credentials pasted as text)."
    )

    user_prompt = {
        "task": "Review added lines for privacy risk",
        "constraints": [
            "Only report findings tied to provided file_path and line_number.",
            "Ignore obvious placeholders/tests/docs examples unless they look real.",
            "Prefer precision over recall.",
        ],
        "added_lines": lines,
    }

    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=True)},
        ],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM API HTTP {exc.code}: {details[:800]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM API connection error: {exc}") from exc

    parsed = _extract_json_block(body)
    choices = parsed.get("choices", []) if isinstance(parsed, dict) else []
    if not choices:
        raise RuntimeError("LLM API returned no choices")

    content = choices[0].get("message", {}).get("content", "")
    result = _extract_json_block(content)
    if not isinstance(result, dict):
        return {}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM-powered privacy review for git added lines.")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--three-dot", action="store_true")
    parser.add_argument("--fail-on", choices=["none", "low", "medium", "high"], default="high")
    parser.add_argument("--max-lines", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    api_key = os.environ.get("PII_REVIEW_API_KEY", "").strip()
    model = os.environ.get("PII_REVIEW_MODEL", "gpt-4o-mini").strip()
    base_url = os.environ.get("PII_REVIEW_BASE_URL", "https://api.openai.com/v1").strip()

    if not api_key:
        print("Agentic privacy review skipped: PII_REVIEW_API_KEY not set")
        return 0

    try:
        diff = _run_git_diff(args.base, args.head, args.three_dot)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    added_lines = _parse_added_lines(diff)
    if not added_lines:
        print("Agentic privacy review: no added lines")
        return 0

    clipped = added_lines[: max(1, args.max_lines)]
    if len(added_lines) > len(clipped):
        print(f"Agentic privacy review: clipped {len(added_lines)} lines to {len(clipped)}")

    try:
        review = _call_llm_review(
            clipped,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=max(5, args.timeout_seconds),
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    findings = review.get("findings", []) if isinstance(review, dict) else []
    if not isinstance(findings, list):
        findings = []

    valid_findings = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "low")).lower().strip()
        if severity not in SEVERITY_RANK:
            severity = "low"
        valid_findings.append(
            {
                "file_path": str(item.get("file_path", "")).strip(),
                "line_number": int(item.get("line_number", 0) or 0),
                "severity": severity,
                "category": str(item.get("category", "unknown")).strip(),
                "reason": str(item.get("reason", "")).strip(),
                "suggestion": str(item.get("suggestion", "")).strip(),
            }
        )

    threshold = SEVERITY_RANK[args.fail_on]
    failing = [item for item in valid_findings if SEVERITY_RANK[item["severity"]] >= threshold]

    summary = ""
    if isinstance(review, dict):
        summary = str(review.get("summary", "")).strip()

    print(f"Agentic privacy review analyzed {len(clipped)} added line(s)")
    if summary:
        print(f"Summary: {summary}")
    if not valid_findings:
        print("Agentic privacy review: no findings")
    else:
        print(f"Agentic privacy review: {len(valid_findings)} finding(s)")
        for item in valid_findings:
            flag = "BLOCK" if item in failing else "WARN"
            print(
                f"[{flag}] {item['severity'].upper():6} {item['category']:12} "
                f"{item['file_path']}:{item['line_number']} - {item['reason']}"
            )

    if args.output_json:
        payload = {
            "summary": summary,
            "findings": valid_findings,
            "failing": failing,
            "fail_on": args.fail_on,
            "model": model,
        }
        with open(args.output_json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)

    if failing:
        print("Agentic privacy review blocked this change based on configured severity threshold.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
