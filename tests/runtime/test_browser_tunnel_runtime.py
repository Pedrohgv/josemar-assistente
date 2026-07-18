"""Opt-in Docker runtime integration test for the browser-tunnel sidecar.

Skipped by default. Enable with:

  RUN_DOCKER_TESTS=1 RUN_BROWSER_TUNNEL_RUNTIME_TESTS=1 \
  python3 -m unittest tests.runtime.test_browser_tunnel_runtime -v

What it proves (all inside Docker, no production stack/Tailscale/laptop):
  1. The production browser-tunnel image builds and starts sshd under the
     production hardening constraints (read-only root, tmpfs, no-new-privileges,
     cap_drop ALL + CHOWN/SETUID/SETGID/SYS_CHROOT).
  2. A real public-key `ssh -N -R 127.0.0.1:9222:127.0.0.1:9222` authentication
     succeeds against the sidecar from an isolated simulated-laptop container.
  3. Traffic traverses the reverse tunnel: a mock CDP HTTP endpoint in the
     laptop container is reachable from the namespace-owner container at
     127.0.0.1:9222 (proving the sidecar shares the namespace and the reverse
     forward lands in it).
  4. At least one forbidden action fails:
       a. A reverse forward to a disallowed port (127.0.0.1:9999) is rejected.
       b. A command/session channel is denied (MaxSessions 0).

Determinism notes:
  - The simulated laptop uses a pinned repo fixture image
    (tests/runtime/fixtures/browser-tunnel-laptop) built explicitly before
    startup, with openssh-client + python3 installed at BUILD time. No runtime
    `apk add` is performed, so startup does not depend on network package
    access and build failures surface with captured stderr.
  - All Docker resources use collision-resistant names (UUID suffix) so
    concurrent or previous runs cannot interfere.
  - On any readiness/traversal/assertion failure, container state and logs are
    captured before cleanup. Cleanup runs even when setup fails and removes
    containers, networks, volumes, images, and generated dump_folder artifacts.

Does not start Hermes or Tailscale.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
import uuid


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BROWSER_TUNNEL_DIR = os.path.join(REPO_ROOT, "browser-tunnel")
LAPTOP_FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "runtime", "fixtures", "browser-tunnel-laptop")
DUMP_DIR = os.path.join(REPO_ROOT, "dump_folder", "browser-tunnel-runtime")

ALPINE = "alpine:3.20.3"
SSH_PORT = "2222"


def docker_available() -> bool:
    return shutil.which("docker") is not None


def run(cmd: list[str], check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


class BrowserTunnelRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.makedirs(DUMP_DIR, exist_ok=True)

    def setUp(self) -> None:
        if os.getenv("RUN_DOCKER_TESTS") != "1":
            self.skipTest("set RUN_DOCKER_TESTS=1 to run Docker runtime tests")
        if os.getenv("RUN_BROWSER_TUNNEL_RUNTIME_TESTS") != "1":
            self.skipTest("set RUN_BROWSER_TUNNEL_RUNTIME_TESTS=1 to run browser-tunnel runtime tests")
        if not docker_available():
            self.skipTest("docker CLI is not available")

        # Collision-resistant resource names so concurrent/previous runs do not
        # interfere. UUID hex suffix, no hyphens (Docker name charset).
        suffix = uuid.uuid4().hex[:12]
        self.suffix = suffix
        self.net = f"bt-test-{suffix}"
        self.ns_owner = f"bt-ns-{suffix}"
        self.tunnel = f"bt-tunnel-{suffix}"
        self.laptop = f"bt-laptop-{suffix}"
        self.vol_host_keys = f"bt-hostkeys-{suffix}"
        self.vol_auth_keys = f"bt-authkeys-{suffix}"
        self.tunnel_image = f"josemar-browser-tunnel:runtime-test-{suffix}"
        self.laptop_image = f"josemar-bt-laptop-fixture:runtime-test-{suffix}"
        # Try a small set of test-only /29 subnets in private space that are
        # unlikely to collide with the host's existing Docker networks. If one
        # overlaps, try the next. The IPs are derived from the chosen subnet.
        self._subnet_candidates = [
            "172.31.252.0/29",
            "172.31.253.0/29",
            "172.31.254.0/29",
            "172.30.252.0/29",
            "172.29.252.0/29",
        ]
        self.subnet: str | None = None
        self.ns_owner_ip: str | None = None
        self.laptop_ip: str | None = None

        # Generated artifacts under dump_folder.
        self.key = os.path.join(DUMP_DIR, f"laptop_key_{suffix}")
        self.pub = f"{self.key}.pub"
        self.seed_dir = os.path.join(DUMP_DIR, f"seed_{suffix}")

        # Track what was created so tearDown can clean everything.
        self._containers: list[str] = []
        self._networks: list[str] = []
        self._volumes: list[str] = []
        self._images: list[str] = []
        self._artifacts: list[str] = []

    def _diagnostics(self) -> str:
        """Capture container state + logs for failure messages."""
        lines = ["--- diagnostics ---"]
        for name in (self.tunnel, self.laptop, self.ns_owner):
            inspect = run(["docker", "inspect", name], check=False)
            lines.append(f"=== docker inspect {name} (rc={inspect.returncode}) ===")
            if inspect.stdout:
                # Only state/status fields to keep it concise.
                lines.append(inspect.stdout[:800])
            logs = run(["docker", "logs", "--tail", "40", name], check=False)
            lines.append(f"=== docker logs {name} (rc={logs.returncode}) ===")
            lines.append(logs.stdout)
            lines.append(logs.stderr)
        return "\n".join(lines)

    def test_reverse_tunnel_and_forbidden_actions(self) -> None:
        try:
            self._run_test()
        except AssertionError as exc:
            # Surface diagnostics before tearDown wipes everything.
            diag = self._diagnostics()
            raise AssertionError(f"{exc}\n\n{diag}") from exc
        except Exception as exc:
            diag = self._diagnostics()
            raise AssertionError(f"unexpected error: {exc}\n\n{diag}") from exc

    def _run_test(self) -> None:
        # --- 1. Build the production browser-tunnel image. ---
        build = run(["docker", "build", "-t", self.tunnel_image, BROWSER_TUNNEL_DIR], timeout=300)
        self.assertEqual(build.returncode, 0, f"browser-tunnel image build failed:\n{build.stderr}")
        self._images.append(self.tunnel_image)

        # --- 2. Build the simulated-laptop fixture image (no runtime apk add). ---
        build_laptop = run(["docker", "build", "-t", self.laptop_image, LAPTOP_FIXTURE_DIR], timeout=300)
        self.assertEqual(
            build_laptop.returncode, 0,
            f"laptop fixture image build failed:\n{build_laptop.stderr}",
        )
        self._images.append(self.laptop_image)

        # --- 3. Generate a temporary SSH keypair for the "laptop". ---
        for p in (self.key, self.pub):
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.remove(p)
        keygen = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.key, "-C", "bt-test-laptop"])
        self.assertEqual(keygen.returncode, 0, f"ssh-keygen failed:\n{keygen.stderr}")
        # ssh-keygen creates the key with 0600; mounted read-only into laptop.
        self.assertEqual(oct(os.stat(self.key).st_mode & 0o777), "0o600")
        with open(self.pub) as fh:
            pub_text = fh.read().strip()
        self.assertTrue(pub_text.startswith("ssh-ed25519 "), f"unexpected pubkey: {pub_text}")
        self._artifacts.extend([self.key, self.pub])

        # --- 4. Create the test network and authorized-keys/host-keys volumes. ---
        # Try candidate subnets until one does not overlap an existing network.
        net_created = False
        for candidate in self._subnet_candidates:
            result = run(["docker", "network", "create", "--subnet", candidate, self.net], check=False)
            if result.returncode == 0:
                self.subnet = candidate
                # Derive IPs: .2 = namespace-owner, .3 = laptop.
                base = candidate.rsplit(".", 1)[0]
                self.ns_owner_ip = f"{base}.2"
                self.laptop_ip = f"{base}.3"
                net_created = True
                break
        if not net_created:
            raise AssertionError(
                f"could not create a non-overlapping test network; tried {self._subnet_candidates}"
            )
        self._networks.append(self.net)
        # Type narrowing: these are set when net_created is True.
        assert self.ns_owner_ip is not None and self.laptop_ip is not None
        run(["docker", "volume", "create", self.vol_auth_keys])
        self._volumes.append(self.vol_auth_keys)
        run(["docker", "volume", "create", self.vol_host_keys])
        self._volumes.append(self.vol_host_keys)

        # Populate the authorized-keys volume with the laptop public key.
        os.makedirs(self.seed_dir, exist_ok=True)
        self._artifacts.append(self.seed_dir)
        seed_keys = os.path.join(self.seed_dir, "authorized_keys")
        with open(seed_keys, "w") as fh:
            fh.write(pub_text + "\n")
        run([
            "docker", "run", "--rm",
            "-v", f"{self.seed_dir}:/seed:ro",
            "-v", f"{self.vol_auth_keys}:/authorized-keys",
            ALPINE,
            "sh", "-c", "mkdir -p /authorized-keys && cp /seed/authorized_keys /authorized-keys/authorized_keys && chmod 600 /authorized-keys/authorized_keys",
        ])

        # --- 5. Start the namespace-owner container (owns the netns). ---
        run([
            "docker", "run", "-d", "--name", self.ns_owner,
            "--network", self.net, "--ip", self.ns_owner_ip,
            ALPINE, "sleep", "600",
        ])
        self._containers.append(self.ns_owner)

        # --- 6. Start the browser-tunnel sidecar sharing the namespace. ---
        run([
            "docker", "run", "-d", "--name", self.tunnel,
            "--network", f"container:{self.ns_owner}",
            "-e", f"BROWSER_CONTROL_HERMES_IP={self.ns_owner_ip}",
            "--read-only",
            "--tmpfs", "/run:rw,noexec,nosuid,size=1m",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=1m",
            "--tmpfs", "/etc/ssh/runtime:rw,noexec,nosuid,size=1m",
            "--security-opt", "no-new-privileges:true",
            "--cap-drop", "ALL",
            "--cap-add", "CHOWN",
            "--cap-add", "SETUID",
            "--cap-add", "SETGID",
            "--cap-add", "SYS_CHROOT",
            "-v", f"{self.vol_host_keys}:/var/lib/browser-tunnel",
            "-v", f"{self.vol_auth_keys}:/authorized-keys:ro",
            self.tunnel_image,
        ])
        self._containers.append(self.tunnel)

        # Wait for sshd to bind.
        bound = False
        logs = run(["docker", "logs", self.tunnel], check=False)
        for _ in range(40):
            logs = run(["docker", "logs", self.tunnel], check=False)
            if "starting sshd on" in logs.stdout and "Bind to port" not in logs.stdout:
                bound = True
                break
            # If the container exited, stop waiting.
            state = run(["docker", "inspect", "-f", "{{.State.Running}}", self.tunnel], check=False)
            if state.stdout.strip() != "true":
                break
            time.sleep(0.5)
        self.assertTrue(
            bound,
            f"sshd did not bind. container logs:\n{logs.stdout}\n{logs.stderr}",
        )

        # --- 7. Start the simulated-laptop container (fixture image, no apk). ---
        run([
            "docker", "run", "-d", "--name", self.laptop,
            "--network", self.net, "--ip", self.laptop_ip,
            "-v", f"{self.key}:/key:ro",
            self.laptop_image,
        ])
        self._containers.append(self.laptop)

        # Launch the mock CDP HTTP server inside the laptop container.
        run(["docker", "exec", "-d", self.laptop, "python3", "/srv/mock_cdp_server.py"])

        # Wait for the mock CDP endpoint to be up inside the laptop.
        laptop_up = False
        for _ in range(40):
            probe = run([
                "docker", "exec", self.laptop,
                "sh", "-c", "wget -qO- http://127.0.0.1:9222/ 2>/dev/null || true",
            ], check=False)
            if "CDP-MOCK-OK" in probe.stdout:
                laptop_up = True
                break
            time.sleep(0.5)
        self.assertTrue(
            laptop_up,
            "laptop mock CDP HTTP server did not start. "
            f"container logs:\n{run(['docker', 'logs', self.laptop], check=False).stdout}",
        )

        # --- 8. Establish the reverse SSH tunnel from the laptop. ---
        ssh_cmd = (
            "ssh -N "
            "-i /key "
            "-o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null "
            "-o ExitOnForwardFailure=yes "
            "-o ServerAliveInterval=15 "
            "-R 127.0.0.1:9222:127.0.0.1:9222 "
            f"-p {SSH_PORT} tunnel@{self.ns_owner_ip}"
        )
        run(["docker", "exec", "-d", self.laptop, "sh", "-c", ssh_cmd])

        # Wait for the reverse listener to appear in the namespace-owner.
        traversed = False
        for _ in range(40):
            probe = run([
                "docker", "exec", self.ns_owner,
                "sh", "-c", "wget -qO- http://127.0.0.1:9222/ 2>/dev/null || true",
            ], check=False)
            if "CDP-MOCK-OK" in probe.stdout:
                traversed = True
                break
            time.sleep(0.5)
        self.assertTrue(
            traversed,
            "reverse tunnel traffic did not traverse to the laptop mock CDP endpoint",
        )

        # --- 9. Forbidden action (a): reverse forward to a disallowed port. ---
        bad_ssh = (
            "ssh -N "
            "-i /key "
            "-o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null "
            "-o ExitOnForwardFailure=yes "
            "-o ConnectTimeout=5 "
            "-R 127.0.0.1:9999:127.0.0.1:9222 "
            f"-p {SSH_PORT} tunnel@{self.ns_owner_ip}"
        )
        bad = run(["docker", "exec", self.laptop, "sh", "-c", bad_ssh], check=False, timeout=20)
        self.assertNotEqual(
            bad.returncode, 0,
            f"disallowed remote forward port 9999 was accepted. stdout={bad.stdout} stderr={bad.stderr}",
        )

        # --- 10. Forbidden action (b): command/session channel denied. ---
        cmd_ssh = (
            "ssh "
            "-i /key "
            "-o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null "
            "-o ConnectTimeout=5 "
            f"-p {SSH_PORT} tunnel@{self.ns_owner_ip} echo SHOULD-NOT-APPEAR"
        )
        cmd = run(["docker", "exec", self.laptop, "sh", "-c", cmd_ssh], check=False, timeout=20)
        self.assertNotEqual(
            cmd.returncode, 0,
            "command/session channel was accepted (MaxSessions 0 should deny it)",
        )
        self.assertNotIn("SHOULD-NOT-APPEAR", cmd.stdout)

    def tearDown(self) -> None:
        # Clean up containers, network, volumes, images, and generated artifacts.
        for name in self._containers:
            run(["docker", "rm", "-f", name], check=False)
        for name in self._networks:
            run(["docker", "network", "rm", name], check=False)
        for name in self._volumes:
            run(["docker", "volume", "rm", name], check=False)
        for name in self._images:
            run(["docker", "rmi", name], check=False)
        for p in self._artifacts:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()