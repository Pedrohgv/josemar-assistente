from __future__ import annotations

import os
import unittest

from .helpers import (
    AUX_ML_MODEL_SHA256_BUILD_ARGS,
    ComposeRuntime,
    REPO_ROOT,
    aux_ml_model_sha256_env,
    docker_available,
)


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
        # `up -d` returns when the containers START; the hermes init chowns
        # the root-owned /shared volume asynchronously during cont-init, so
        # wait for the writable state before the hermes write probe (the
        # aux-ml build above also gives the init time on a cold build, but
        # a cached build can still race it).
        self.runtime.wait_until_hermes_writable()

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


class AuxMLBuildArgIsolationTests(unittest.TestCase):
    """No-docker regression: ComposeRuntime(include_aux_ml=True) pins the
    aux-ml image build-arg SHA256 values to the repo's LOCAL model files
    (the build context), so the Dockerfile's checksum verification matches
    the files actually present instead of the download-source defaults —
    a local file that differs from the compose default must never break the
    gated test build (and a missing file stays unpinned so the default
    download path still applies)."""

    def test_aux_ml_env_pins_local_model_hashes(self) -> None:
        runtime = ComposeRuntime(include_aux_ml=True)
        pinned = aux_ml_model_sha256_env()
        for filename, key in AUX_ML_MODEL_SHA256_BUILD_ARGS.items():
            path = REPO_ROOT / "aux-ml" / "models" / filename
            if path.is_file():
                self.assertEqual(
                    runtime.env.get(key), pinned[key], filename
                )
                # The pinned value must be a real 64-hex sha256.
                self.assertRegex(runtime.env[key], r"^[0-9a-f]{64}$", filename)
            else:
                # Missing file: the arg is left to the compose default.
                self.assertNotIn(key, runtime.env, filename)


if __name__ == "__main__":
    unittest.main()
