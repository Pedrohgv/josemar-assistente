"""Fast tests for the Mnemosyne backup-export cron deployment verifier."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER_PATH = REPO_ROOT / "scripts" / "verify_mnemosyne_backup_cron.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_mnemosyne_backup_cron", VERIFIER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MnemosyneBackupCronVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = _load_verifier()
        self.tmp = Path(tempfile.mkdtemp(prefix="mnemosyne-backup-cron-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _valid_job(self) -> dict:
        return {
            "name": "mnemosyne-backup-export",
            "schedule": {"kind": "interval", "minutes": 60},
            "script": "mnemosyne-backup-export.sh",
            "no_agent": True,
            "workdir": "/opt/data",
        }

    def _reason(self, document: Any, expected_interval: Any = 60):
        return self.verifier.validate_jobs_document(document, expected_interval)

    def _write_jobs(self, document) -> Path:
        path = self.tmp / "jobs.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_valid_document_passes(self) -> None:
        self.assertIsNone(self._reason({"jobs": [self._valid_job()]}))

    def test_missing_and_unreadable_jobs_file_fail_safely(self) -> None:
        self.assertEqual(
            self.verifier.validate_jobs_file(self.tmp / "missing.json", 60),
            "jobs_file_unreadable",
        )
        with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
            self.assertEqual(
                self.verifier.validate_jobs_file(self.tmp / "jobs.json", 60),
                "jobs_file_unreadable",
            )

    def test_malformed_json_fails_safely(self) -> None:
        path = self.tmp / "jobs.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(
            self.verifier.validate_jobs_file(path, 60), "jobs_json_malformed"
        )

    def test_non_object_document_fails(self) -> None:
        for document in (None, [], "jobs"):
            with self.subTest(document=document):
                self.assertEqual(self._reason(document), "jobs_document_invalid")

    def test_missing_or_non_list_jobs_fails(self) -> None:
        for document in ({}, {"jobs": None}, {"jobs": {}}):
            with self.subTest(document=document):
                self.assertEqual(self._reason(document), "jobs_list_invalid")

    def test_zero_and_duplicate_owned_jobs_fail(self) -> None:
        self.assertEqual(self._reason({"jobs": []}), "owned_job_missing")
        self.assertEqual(
            self._reason({"jobs": [self._valid_job(), self._valid_job()]}),
            "owned_job_duplicate",
        )

    def test_malformed_owned_job_and_schedule_variants_fail(self) -> None:
        malformed_job = {"name": "mnemosyne-backup-export"}
        self.assertEqual(
            self._reason({"jobs": [malformed_job]}), "schedule_invalid"
        )
        for schedule in (None, [], "every 60m"):
            with self.subTest(schedule=schedule):
                job = self._valid_job()
                job["schedule"] = schedule
                self.assertEqual(self._reason({"jobs": [job]}), "schedule_invalid")

    def test_schedule_kind_minutes_type_and_interval_mismatches_fail(self) -> None:
        job = self._valid_job()
        job["schedule"]["kind"] = "cron"
        self.assertEqual(self._reason({"jobs": [job]}), "schedule_kind_mismatch")

        for minutes in (True, False, "60", 60.0, None):
            with self.subTest(minutes=minutes):
                job = self._valid_job()
                job["schedule"]["minutes"] = minutes
                self.assertEqual(
                    self._reason({"jobs": [job]}), "schedule_minutes_type_invalid"
                )

        job = self._valid_job()
        job["schedule"]["minutes"] = 30
        self.assertEqual(
            self._reason({"jobs": [job]}), "schedule_minutes_mismatch"
        )

    def test_script_no_agent_and_workdir_mismatches_fail(self) -> None:
        for field, value, reason in (
            ("script", "other.sh", "script_mismatch"),
            ("no_agent", False, "no_agent_mismatch"),
            ("no_agent", None, "no_agent_mismatch"),
            ("workdir", "/tmp", "workdir_mismatch"),
        ):
            with self.subTest(field=field, value=value):
                job = self._valid_job()
                job[field] = value
                self.assertEqual(self._reason({"jobs": [job]}), reason)

    def test_unrelated_jobs_do_not_affect_valid_owned_job(self) -> None:
        document = {
            "jobs": [
                {"name": "unrelated", "metadata": "not inspected"},
                "not-a-job",
                self._valid_job(),
            ]
        }
        self.assertIsNone(self._reason(document))

    def test_cli_diagnostic_does_not_leak_document_or_unrelated_metadata(self) -> None:
        secret_marker = "unrelated-private-metadata-marker"
        jobs_file = self._write_jobs(
            {"jobs": [{"name": "unrelated", "metadata": secret_marker}]}
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = self.verifier.main(
                ["--jobs-file", str(jobs_file), "--expected-interval", "60"]
            )
        self.assertEqual(result, 1)
        self.assertEqual(
            output.getvalue().strip(),
            "mnemosyne_backup_cron_verification_failed reason=owned_job_missing",
        )
        self.assertNotIn(secret_marker, output.getvalue())

    def test_invalid_expected_interval_has_bounded_no_traceback_diagnostic(self) -> None:
        for expected_interval in (0, True, None):
            with self.subTest(expected_interval=expected_interval):
                self.assertEqual(
                    self._reason({"jobs": [self._valid_job()]}, expected_interval),
                    "expected_interval_invalid",
                )

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = self.verifier.main(
                ["--jobs-file", str(self.tmp / "missing.json"), "--expected-interval", "zero"]
            )
        self.assertEqual(result, 1)
        self.assertEqual(
            output.getvalue().strip(),
            "mnemosyne_backup_cron_verification_failed reason=expected_interval_invalid",
        )
        self.assertNotIn("Traceback", output.getvalue())
