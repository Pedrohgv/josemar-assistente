"""Opt-in Docker runtime integration test for the browser-tunnel sidecar.

Skipped by default. Enable with:

  RUN_DOCKER_TESTS=1 RUN_BROWSER_TUNNEL_RUNTIME_TESTS=1 \
  python3 -m unittest tests.runtime.test_browser_tunnel_runtime -v

The pure subnet-allocation unit tests at the bottom of this file run
unconditionally (they need no Docker).

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
  - The test network subnet is collision-aware (select_test_subnet): the IPAM
    subnets of every existing Docker network are enumerated, the historical
    172.29-31 /29 candidates are preferred when they are free, and otherwise
    the less-contested 10.200.0.0/16 space is scanned in ascending /29 order
    for the first block that overlaps nothing. Create-time retries
    re-enumerate on every attempt so a concurrent run that wins a race
    becomes visible and is skipped; docker stderr from every failed attempt
    is included in the final allocation error.
  - On any readiness/traversal/assertion failure, container state and logs are
    captured before cleanup. Cleanup runs even when setup fails and removes
    containers, networks, volumes, images, and generated dump_folder artifacts.

Does not start Hermes or Tailscale.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import time
import unittest
import uuid
from collections.abc import Sequence


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


# --- Collision-aware test-network subnet allocation --------------------------
#
# Hosts commonly run foreign Docker networks in 172.17-31.0.0/16 (compose
# stacks, CI runners, this repo's own test stacks), so a hard-coded test
# subnet in that space reliably collides. Strategy: enumerate the IPAM
# subnets of every existing Docker network, keep the historical 172.29-31
# candidates when they are free, otherwise deterministically scan the
# less-contested 10.200.0.0/16 space in ascending /29 order for the first
# block that overlaps nothing. Create-time retries (with a fresh enumeration
# each attempt) handle races against concurrent runs.

ALLOCATION_ATTEMPTS = 5
PREFERRED_TEST_SUBNETS = [
    "172.31.252.0/29",
    "172.31.253.0/29",
    "172.31.254.0/29",
    "172.30.252.0/29",
    "172.29.252.0/29",
]
TEST_SCAN_SPACE = "10.200.0.0/16"
TEST_SUBNET_PREFIX = 29


class SubnetAllocationError(Exception):
    """Raised when no non-overlapping test subnet can be selected."""


def _parse_ipam_subnets(inspect_json: str) -> list[ipaddress.IPv4Network]:
    """Parse ``docker network inspect`` JSON into the IPv4 IPAM subnets.

    IPv6 subnets and malformed/absent entries are skipped so one odd network
    can never break allocation. Accepts both the array and single-object
    forms docker emits.
    """
    try:
        networks = json.loads(inspect_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid `docker network inspect` JSON: {exc}") from exc
    if isinstance(networks, dict):
        networks = [networks]
    subnets: list[ipaddress.IPv4Network] = []
    for network in networks:
        for config in (network.get("IPAM") or {}).get("Config") or []:
            subnet_text = config.get("Subnet")
            if not subnet_text:
                continue
            try:
                subnet = ipaddress.ip_network(subnet_text, strict=False)
            except ValueError:
                continue
            if subnet.version == 4:
                subnets.append(subnet)
    return subnets


def _enumerate_existing_subnets() -> list[ipaddress.IPv4Network]:
    """IPAM IPv4 subnets of every existing Docker network (for overlap checks).

    Fails loudly with docker stderr only when the daemon cannot be enumerated
    at all; a network that cannot be inspected individually is skipped (the
    create-time retries still race against it).
    """
    listed = run(["docker", "network", "ls", "-q"], check=False)
    if listed.returncode != 0:
        raise AssertionError(f"docker network ls failed:\n{listed.stderr}")
    network_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not network_ids:
        return []
    inspected = run(["docker", "network", "inspect", *network_ids], check=False)
    if inspected.returncode == 0:
        return _parse_ipam_subnets(inspected.stdout)
    # Batch inspect can fail when one network is uninspectable (e.g. swarm);
    # fall back to per-network inspection and keep whatever parses.
    subnets: list[ipaddress.IPv4Network] = []
    failures = 0
    one = inspected
    for network_id in network_ids:
        one = run(["docker", "network", "inspect", network_id], check=False)
        if one.returncode == 0:
            subnets.extend(_parse_ipam_subnets(one.stdout))
        else:
            failures += 1
    if not subnets and failures == len(network_ids):
        raise AssertionError(
            "docker network inspect failed for every network "
            f"(batch stderr:\n{inspected.stderr}\n"
            f"last per-network stderr:\n{one.stderr})"
        )
    return subnets


def select_test_subnet(
    existing_subnets: Sequence[ipaddress.IPv4Network],
    *,
    preferred: Sequence[str] = PREFERRED_TEST_SUBNETS,
    scan_space: str = TEST_SCAN_SPACE,
) -> str:
    """Deterministically pick a /29 test subnet overlapping no existing one.

    The historical 172.29-31 candidates are checked first so hosts where
    those ranges are free keep the old behavior; otherwise ``scan_space`` is
    scanned in ascending /29 order and the first free block is returned.
    Deterministic for a given set of existing subnets.
    """
    existing = list(existing_subnets)

    def overlaps(candidate: ipaddress.IPv4Network) -> bool:
        return any(candidate.overlaps(other) for other in existing)

    for candidate in preferred:
        subnet = ipaddress.ip_network(candidate)
        if not isinstance(subnet, ipaddress.IPv4Network):
            raise SubnetAllocationError(f"preferred subnet {candidate!r} is not IPv4")
        if not overlaps(subnet):
            return candidate
    scan = ipaddress.ip_network(scan_space)
    if not isinstance(scan, ipaddress.IPv4Network):
        raise SubnetAllocationError(f"scan space {scan_space!r} is not IPv4")
    for subnet in scan.subnets(new_prefix=TEST_SUBNET_PREFIX):
        if not overlaps(subnet):
            return str(subnet)
    raise SubnetAllocationError(
        "no non-overlapping /29 test subnet: every block in "
        f"{list(preferred)} and {scan_space} overlaps one of "
        f"{len(existing)} existing Docker network subnet(s)"
    )


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
        # The test subnet is chosen at network-create time by
        # select_test_subnet() (collision-aware against every existing Docker
        # network's IPAM subnets; see PREFERRED_TEST_SUBNETS / TEST_SCAN_SPACE).
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
        # Collision-aware allocation: enumerate the IPAM subnets of every
        # existing Docker network, prefer the historical 172.29-31 candidates
        # when free, then deterministically scan the less-contested
        # 10.200.0.0/16 space for a /29 block overlapping nothing. Each retry
        # re-enumerates so a concurrent run that won the previous race is
        # visible and skipped; docker stderr from every attempt is kept for
        # the final error.
        create_errors: list[str] = []
        net_created = False
        for attempt in range(1, ALLOCATION_ATTEMPTS + 1):
            candidate = select_test_subnet(_enumerate_existing_subnets())
            result = run(
                ["docker", "network", "create", "--subnet", candidate, self.net],
                check=False,
            )
            if result.returncode == 0:
                self.subnet = candidate
                # Derive IPs: .2 = namespace-owner, .3 = laptop.
                base = candidate.rsplit(".", 1)[0]
                self.ns_owner_ip = f"{base}.2"
                self.laptop_ip = f"{base}.3"
                net_created = True
                break
            create_errors.append(
                f"attempt {attempt} (subnet {candidate}): {result.stderr.strip()}"
            )
        if not net_created:
            raise AssertionError(
                "could not create a non-overlapping test network after "
                f"{ALLOCATION_ATTEMPTS} attempts:\n" + "\n".join(create_errors)
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


class SubnetAllocationUnitTests(unittest.TestCase):
    """Pure unit coverage for the collision-aware subnet helpers.

    No Docker required; these run even when the runtime tests are skipped.
    """

    def test_parse_ipam_subnets_extracts_ipv4_and_skips_noise(self) -> None:
        payload = json.dumps([
            {
                "Name": "bridge",
                "IPAM": {
                    "Driver": "default",
                    "Config": [
                        {"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"},
                        {"Subnet": "fd00::/64", "Gateway": "fd00::1"},
                    ],
                },
            },
            {"Name": "host", "IPAM": {"Driver": "default", "Config": []}},
            {
                "Name": "custom",
                "IPAM": {
                    "Driver": "default",
                    "Config": [
                        {"Subnet": "10.200.0.0/29"},
                        {"Subnet": "not-a-subnet"},
                        {"Subnet": ""},
                        {},
                    ],
                },
            },
            {"Name": "no-ipam"},
        ])
        self.assertEqual(
            _parse_ipam_subnets(payload),
            [
                ipaddress.IPv4Network("172.17.0.0/16"),
                ipaddress.IPv4Network("10.200.0.0/29"),
            ],
        )

    def test_parse_ipam_subnets_single_object_and_bad_json(self) -> None:
        self.assertEqual(
            _parse_ipam_subnets(json.dumps({
                "Name": "only",
                "IPAM": {"Config": [{"Subnet": "10.200.0.8/29"}]},
            })),
            [ipaddress.IPv4Network("10.200.0.8/29")],
        )
        with self.assertRaises(ValueError):
            _parse_ipam_subnets("not json")

    def test_preferred_candidates_used_when_free(self) -> None:
        self.assertEqual(select_test_subnet([]), "172.31.252.0/29")
        self.assertEqual(
            select_test_subnet([ipaddress.IPv4Network("172.31.252.0/29")]),
            "172.31.253.0/29",
        )
        # Unrelated networks never block the preferred candidates.
        self.assertEqual(
            select_test_subnet([
                ipaddress.IPv4Network("172.17.0.0/16"),
                ipaddress.IPv4Network("192.168.16.0/20"),
            ]),
            "172.31.252.0/29",
        )

    def test_scan_space_used_when_all_preferred_overlap(self) -> None:
        foreign = [
            ipaddress.IPv4Network("172.29.0.0/16"),
            ipaddress.IPv4Network("172.30.0.0/16"),
            ipaddress.IPv4Network("172.31.0.0/16"),
        ]
        self.assertEqual(select_test_subnet(foreign), "10.200.0.0/29")

    def test_scan_skips_occupied_blocks_deterministically(self) -> None:
        foreign = [
            ipaddress.IPv4Network("172.29.0.0/16"),
            ipaddress.IPv4Network("172.30.0.0/16"),
            ipaddress.IPv4Network("172.31.0.0/16"),
            ipaddress.IPv4Network("10.200.0.0/29"),
        ]
        self.assertEqual(select_test_subnet(foreign), "10.200.0.8/29")

    def test_scan_exhaustion_raises(self) -> None:
        foreign = [
            ipaddress.IPv4Network("172.29.0.0/16"),
            ipaddress.IPv4Network("172.30.0.0/16"),
            ipaddress.IPv4Network("172.31.0.0/16"),
            ipaddress.IPv4Network("10.200.0.0/16"),
        ]
        with self.assertRaises(SubnetAllocationError):
            select_test_subnet(foreign)


if __name__ == "__main__":
    unittest.main()