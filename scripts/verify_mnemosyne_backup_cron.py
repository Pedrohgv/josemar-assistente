#!/usr/bin/env python3
"""Verify the deployed Mnemosyne backup-export cron contract.

This is the deployment reader for the cron job written by
``install_mnemosyne_backup_export_cron``. Diagnostics are intentionally
bounded: never emit cron documents or arbitrary job metadata.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


OWNED_JOB_NAME = "mnemosyne-backup-export"
EXPECTED_SCRIPT = "mnemosyne-backup-export.sh"
EXPECTED_WORKDIR = "/opt/data"


def validate_jobs_document(document: Any, expected_interval: Any) -> str | None:
    """Return a bounded failure reason, or ``None`` when the contract holds."""
    if (
        not isinstance(expected_interval, int)
        or isinstance(expected_interval, bool)
        or expected_interval <= 0
    ):
        return "expected_interval_invalid"
    if not isinstance(document, dict):
        return "jobs_document_invalid"

    jobs = document.get("jobs")
    if not isinstance(jobs, list):
        return "jobs_list_invalid"

    owned_jobs = [
        job
        for job in jobs
        if isinstance(job, dict) and job.get("name") == OWNED_JOB_NAME
    ]
    if not owned_jobs:
        return "owned_job_missing"
    if len(owned_jobs) != 1:
        return "owned_job_duplicate"

    job = owned_jobs[0]
    schedule = job.get("schedule")
    if not isinstance(schedule, dict):
        return "schedule_invalid"
    if schedule.get("kind") != "interval":
        return "schedule_kind_mismatch"

    minutes = schedule.get("minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        return "schedule_minutes_type_invalid"
    if minutes != expected_interval:
        return "schedule_minutes_mismatch"
    if job.get("script") != EXPECTED_SCRIPT:
        return "script_mismatch"
    if job.get("no_agent") is not True:
        return "no_agent_mismatch"
    if job.get("workdir") != EXPECTED_WORKDIR:
        return "workdir_mismatch"
    return None


def validate_jobs_file(jobs_file: Path, expected_interval: Any) -> str | None:
    """Read and validate a jobs file without exposing its contents on failure."""
    try:
        text = jobs_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "jobs_file_unreadable"
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return "jobs_json_malformed"
    return validate_jobs_document(document, expected_interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the Mnemosyne backup-export cron contract."
    )
    parser.add_argument("--jobs-file", required=True)
    parser.add_argument("--expected-interval", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        expected_interval = int(args.expected_interval)
    except ValueError:
        reason = "expected_interval_invalid"
    else:
        reason = validate_jobs_file(Path(args.jobs_file), expected_interval)

    if reason is not None:
        print(f"mnemosyne_backup_cron_verification_failed reason={reason}")
        return 1
    print("mnemosyne_backup_cron_verification_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
