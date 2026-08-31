from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "fast-tests.yml"


class FastTestsWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_failure_summary_preserves_make_verify_status(self):
        self.assertIn('make verify 2>&1 | tee "$verify_log"', self.workflow)
        self.assertIn('verify_status=${PIPESTATUS[0]}', self.workflow)
        self.assertIn('exit "$verify_status"', self.workflow)

    def test_failure_summary_lists_only_unittest_headers(self):
        self.assertIn('if (( verify_status != 0 )); then', self.workflow)
        self.assertIn('=== Failed unittest cases ===', self.workflow)
        self.assertIn("grep -E '^(FAIL|ERROR): '", self.workflow)
        self.assertIn('no unittest FAIL:/ERROR: headers were found', self.workflow)

    def test_verification_capture_is_runner_local_and_cleaned_up(self):
        self.assertIn('verify_log="$RUNNER_TEMP/fast-tests-verify.log"', self.workflow)
        self.assertIn("trap 'rm -f \"$verify_log\"' EXIT", self.workflow)
        self.assertNotIn("upload-artifact", self.workflow)


if __name__ == "__main__":
    unittest.main()
