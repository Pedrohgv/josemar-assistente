from __future__ import annotations

import os
import unittest

from .helpers import ComposeRuntime, docker_available


@unittest.skipUnless(os.getenv("RUN_DOCKER_TESTS") == "1", "set RUN_DOCKER_TESTS=1 to run Docker runtime tests")
@unittest.skipUnless(
    os.getenv("RUN_AUX_ML_RUNTIME_TESTS") == "1",
    "set RUN_AUX_ML_RUNTIME_TESTS=1 to run aux-ml runtime tests",
)
@unittest.skipUnless(docker_available(), "docker CLI is not available")
class AuxMLRuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = ComposeRuntime(include_aux_ml=True)

    def tearDown(self) -> None:
        self.runtime.down()

    def test_aux_ml_can_read_shared_volume_but_not_write_it(self) -> None:
        self.runtime.up("hermes", "aux-ml")

        self.runtime.exec("hermes", "sh", "-lc", "printf shared-ok > /shared/runtime-contract.txt")

        read_process = self.runtime.exec("aux-ml", "sh", "-lc", "cat /shared/runtime-contract.txt")
        self.assertEqual(read_process.stdout.strip(), "shared-ok")

        write_process = self.runtime.exec(
            "aux-ml",
            "sh",
            "-lc",
            "touch /shared/should-not-write",
            check=False,
        )
        self.assertNotEqual(write_process.returncode, 0)

        env_process = self.runtime.exec("aux-ml", "sh", "-lc", "env")
        self.assertIn("AUX_ML_ALLOWED_INPUT_DIRS=/shared", env_process.stdout)
        self.assertIn("AUX_ML_MODEL_REGISTRY=/app/config/models.yaml", env_process.stdout)


if __name__ == "__main__":
    unittest.main()
