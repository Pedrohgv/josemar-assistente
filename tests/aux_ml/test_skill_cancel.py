from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills-factory" / "aux-ml" / "aux-ml"


def load_skill_module():
    loader = importlib.machinery.SourceFileLoader("aux_ml_skill_cancel_test", str(SKILL_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Could not load aux-ml skill module")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SkillCancelActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = load_skill_module()
        self.skill.AUX_ML_ENABLED = True

    def test_cancel_job_action_returns_cancelled_true_on_success(self) -> None:
        response_payload = {
            "job_id": "job-123",
            "status": "cancelled",
            "cancelled": True,
            "message": "Queued job cancelled.",
        }
        with patch.object(self.skill, "_http_json", return_value=response_payload) as mock_http:
            result = self.skill.action_cancel_job({"job_id": "job-123"})

        self.assertTrue(result["success"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["job_id"], "job-123")
        mock_http.assert_called_once_with("POST", "/jobs/job-123/cancel", timeout=30)

    def test_cancel_job_action_returns_cancelled_true_on_cancelling(self) -> None:
        response_payload = {
            "job_id": "job-123",
            "status": "cancelling",
            "cancelled": True,
            "message": "Running job cancellation requested.",
        }
        with patch.object(self.skill, "_http_json", return_value=response_payload) as mock_http:
            result = self.skill.action_cancel_job({"job_id": "job-123"})

        self.assertTrue(result["success"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["status"], "cancelling")
        mock_http.assert_called_once_with("POST", "/jobs/job-123/cancel", timeout=30)

    def test_cancel_job_action_returns_cancelled_false_when_not_cancellable(self) -> None:
        response_payload = {
            "job_id": "job-123",
            "status": "succeeded",
            "cancelled": False,
            "message": "Job is not cancellable in status 'succeeded'.",
        }
        with patch.object(self.skill, "_http_json", return_value=response_payload):
            result = self.skill.action_cancel_job({"job_id": "job-123"})

        self.assertFalse(result["success"])
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["status"], "succeeded")

    def test_cancel_job_action_missing_job_id(self) -> None:
        result = self.skill.action_cancel_job({})

        self.assertFalse(result["success"])
        self.assertIn("job_id", result["error"])

    def test_cancel_job_action_handles_http_error(self) -> None:
        with patch.object(self.skill, "_http_json", side_effect=RuntimeError("Aux ML HTTP 404: not found")):
            result = self.skill.action_cancel_job({"job_id": "missing"})

        self.assertFalse(result["success"])
        self.assertIn("404", result["error"])
        self.assertEqual(result["job_id"], "missing")

    def test_cancel_job_is_registered_in_actions(self) -> None:
        self.assertIn("cancel_job", self.skill.ACTIONS)
        self.assertEqual(self.skill.ACTIONS["cancel_job"], self.skill.action_cancel_job)


class SkillWaitForJobCancelledTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = load_skill_module()
        self.skill.AUX_ML_ENABLED = True
        self.skill.AUX_ML_POLL_INTERVAL_SECONDS = 0.01

    def test_wait_for_job_returns_cancelled_status(self) -> None:
        cancelled_job = {"status": "cancelled", "job_id": "job-1", "error": None}
        with patch.object(self.skill, "_job_status", return_value=cancelled_job):
            result = self.skill._wait_for_job("job-1", timeout_seconds=5)

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error"], "Job was cancelled")

    def test_wait_for_job_action_propagates_cancelled(self) -> None:
        cancelled_job = {"status": "cancelled", "job_id": "job-1", "error": None}
        with patch.object(self.skill, "_job_status", return_value=cancelled_job):
            result = self.skill.action_wait_for_job({"job_id": "job-1", "timeout_seconds": 5})

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()