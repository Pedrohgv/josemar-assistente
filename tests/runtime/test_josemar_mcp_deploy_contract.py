"""Source/structure contract tests for the josemar-mcp deploy integration in
`.github/workflows/deploy-to-home-server.yml` and the josemar-mcp compose
overlay.

Pure-source: parse the workflow YAML and overlay text without executing
GitHub Actions or Docker. Covers:

  - default-off / backward compatibility (no josemar-mcp overlays when unset),
  - strict boolean validation for JOSEMAR_MCP_ENABLED,
  - required JOSEMAR_MCP_AUTHORIZED_KEY secret + single-line SSH key check,
  - subnet/IP validation with ipaddress (distinct subnet from browser-control),
  - HERMES_API_SERVER_ENABLED + HERMES_API_SERVER_KEY prerequisite when enabled,
  - COMPOSE_FILE ordering (base; browser-control; josemar-mcp; embeddings;
    mnemosyne; backup last),
  - combined Tailscale Serve config (tcp:2222 + tcp:2223 when both enabled;
    no Funnel),
  - atomic named-volume seeding for josemar-mcp-authorized-keys,
  - maximal fail-closed teardown includes josemar-mcp overlay + profile,
  - enabled/disabled verification steps (sidecar running, tcp:2223 target,
    no Funnel, forced-command wrapper + MCP server import; absent + tcp:2223
    cleared when disabled).
"""

from __future__ import annotations

import unittest
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-to-home-server.yml"
OVERLAY = REPO_ROOT / "docker-compose.josemar-mcp.yml"
BROWSER_OVERLAY = REPO_ROOT / "docker-compose.browser-control.yml"


def _step_text(workflow: dict, name: str) -> str:
    steps = workflow["jobs"]["deploy"]["steps"]
    for step in steps:
        if step.get("name") == name:
            run = step.get("run")
            assert run is not None, f"step {name!r} has no run block"
            return run
    raise AssertionError(f"workflow step {name!r} not found")


def _step_names(workflow: dict) -> list[str]:
    return [step.get("name", "") for step in workflow["jobs"]["deploy"]["steps"]]


class JosemarMcpDeployWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        assert yaml is not None, "PyYAML is required for these contract tests"
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.text = WORKFLOW.read_text(encoding="utf-8")

    # --- default off / backward compatibility ---

    def test_josemar_mcp_default_is_false(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn('JOSEMAR_MCP_ENABLED="$JOSEMAR_MCP_ENABLED_INPUT"', validate)
        self.assertIn('if [ -z "$JOSEMAR_MCP_ENABLED" ]; then', validate)
        self.assertIn('JOSEMAR_MCP_ENABLED="false"', validate)

    def test_josemar_mcp_strict_boolean(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn(
            "ERROR: JOSEMAR_MCP_ENABLED must be 'true' or 'false'",
            validate,
        )

    # --- required secret + key validation ---

    def test_josemar_mcp_requires_authorized_key_when_enabled(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn(
            "ERROR: JOSEMAR_MCP_AUTHORIZED_KEY secret is required when JOSEMAR_MCP_ENABLED=true",
            validate,
        )
        self.assertIn("single-line SSH public key", validate)
        # The OpenSSH public key regex check is present.
        self.assertIn("ssh-(rsa|ed25519|dss)", validate)

    # --- subnet/IP validation (distinct from browser-control) ---

    def test_josemar_mcp_subnet_validation_uses_ipaddress(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        self.assertIn("import ipaddress", validate)
        self.assertIn("172.31.251.0/29", validate)
        # Must reference josemar-mcp-specific env names.
        self.assertIn("JOSEMAR_MCP_SUBNET", validate)
        self.assertIn("JOSEMAR_MCP_HERMES_IP", validate)

    def test_josemar_mcp_subnet_distinct_from_browser_control(self) -> None:
        validate = _step_text(self.workflow, "Validate required repository variables")
        # The josemar-mcp python validation block must default to the distinct
        # subnet. Extract just the python heredoc for the JM block.
        jm_block = validate.split("Josemar MCP is optional")[1]
        py_block = jm_block.split("python3 - <<'PY'")[1].split("PY")[0]
        self.assertIn("172.31.251.0/29", py_block)
        # The JM python block must not default to the browser-control subnet.
        self.assertNotIn('"172.31.250.0/29"', py_block)
        # Must use JOSEMAR_MCP_HERMES_IP.
        self.assertIn("JOSEMAR_MCP_HERMES_IP", py_block)

    # --- HERMES_API_SERVER prerequisite ---

    def test_josemar_mcp_requires_api_server_enabled(self) -> None:
        env_step = _step_text(self.workflow, "Create .env file")
        self.assertIn(
            "JOSEMAR_MCP_ENABLED=true requires HERMES_API_SERVER_ENABLED=true",
            env_step,
        )
        self.assertIn(
            "JOSEMAR_MCP_ENABLED=true requires HERMES_API_SERVER_KEY secret",
            env_step,
        )

    # --- COMPOSE_FILE ordering ---

    def test_josemar_mcp_overlay_after_browser_control_before_embeddings(self) -> None:
        derive = _step_text(self.workflow, "Derive compose file and validate config")
        bc_idx = derive.index("docker-compose.browser-control.yml")
        jm_idx = derive.index("docker-compose.josemar-mcp.yml")
        case_idx = derive.index('case "${MNEMOSYNE_DEPLOY_MODE:-off}" in')
        self.assertLess(bc_idx, jm_idx)
        self.assertLess(jm_idx, case_idx)
        self.assertIn('"${JOSEMAR_MCP_ENABLED}" = "true"', derive)

    # --- combined Tailscale Serve config ---

    def test_serve_config_combines_both_features(self) -> None:
        populate = _step_text(
            self.workflow,
            "Populate browser-control and josemar-mcp persistent volumes",
        )
        # The python assembler references both BC and JM flags + IPs.
        self.assertIn("bc_on", populate)
        self.assertIn("jm_on", populate)
        self.assertIn("2222", populate)
        self.assertIn("2223", populate)
        # The python assembler must not write a Funnel config. Check the
        # python heredoc body (not comments).
        py_block = populate.split("python3 - ")[1].split("<<'PY'")[1].split("PY")[0]
        self.assertNotIn("Funnel", py_block)
        self.assertNotIn("funnel", py_block)

    def test_serve_config_empty_when_neither_enabled(self) -> None:
        populate = _step_text(
            self.workflow,
            "Populate browser-control and josemar-mcp persistent volumes",
        )
        # When neither is enabled, cfg is {}.
        self.assertIn('cfg = {"TCP": tcp} if tcp else {}', populate)

    # --- atomic named-volume seeding ---

    def test_josemar_mcp_authorized_keys_atomic_seed(self) -> None:
        populate = _step_text(
            self.workflow,
            "Populate browser-control and josemar-mcp persistent volumes",
        )
        self.assertIn("josemar-mcp-authorized-keys", populate)
        # Atomic copy pattern: .new + chmod + mv.
        self.assertIn("jm_authorized_keys", populate)
        self.assertIn("authorized_keys.new", populate)
        self.assertIn("mv /authorized-keys/authorized_keys.new", populate)

    def test_josemar_mcp_authorized_keys_cleared_when_disabled(self) -> None:
        populate = _step_text(
            self.workflow,
            "Populate browser-control and josemar-mcp persistent volumes",
        )
        self.assertIn("JM_AUTH_KEYS_VOLUME", populate)
        self.assertIn("rm -f /authorized-keys/authorized_keys", populate)

    # --- maximal fail-closed teardown ---

    def test_teardown_includes_josemar_mcp_overlay(self) -> None:
        stop = _step_text(self.workflow, "Stop existing services")
        superset = stop.split("Tearing down any prior overlay-enabled stack")[1].split(
            "Tearing down with selected config"
        )[0]
        self.assertIn("docker-compose.josemar-mcp.yml", superset)
        # No --profile josemar-mcp needed (no separate service).
        self.assertNotIn("--profile josemar-mcp", superset)

    # --- verification steps ---

    def test_enabled_verification_step_present(self) -> None:
        names = _step_names(self.workflow)
        self.assertIn("Verify josemar-mcp sshd (only when enabled)", names)

    def test_disabled_verification_step_present(self) -> None:
        names = _step_names(self.workflow)
        self.assertIn("Verify josemar-mcp is absent (only when disabled)", names)

    def test_enabled_verification_checks_no_funnel(self) -> None:
        step = _step_text(self.workflow, "Verify josemar-mcp sshd (only when enabled)")
        self.assertIn("sshd", step)
        self.assertIn("2223", step)
        self.assertIn("AllowFunnel", step)
        # Uses the JM_HERMES_IP env var.
        self.assertIn("JM_HERMES_IP", step)
        # Verifies sshd process is running INSIDE the hermes container.
        self.assertIn("pgrep", step)
        self.assertIn("hermes", step)
        # Verifies the forced-command wrapper and MCP server are present in
        # the hermes container.
        self.assertIn("josemar-knowledge-mcp-forced", step)
        self.assertIn("josemar_knowledge_mcp.py", step)
        # Uses import check (not py_compile) with PYTHONDONTWRITEBYTECODE=1.
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", step)
        self.assertIn("importlib.util", step)
        self.assertNotIn("py_compile", step)
        # Verifies the josemar-mcp skill is baked into the image.
        self.assertIn("josemar-mcp/SKILL.md", step)
        # Verifies the authorized-keys volume is mounted.
        self.assertIn("josemar-mcp-authorized-keys", step)
        # Verifies the sshd config template and init script are present.
        self.assertIn("sshd_config.template", step)
        self.assertIn("02-josemar-mcp-sshd", step)
        # Verifies the sshd host key was generated.
        self.assertIn("/var/lib/josemar-mcp-hostkeys/ssh_host_ed25519_key", step)
        # Verifies the Hermes gateway is running as non-root.
        self.assertIn("GATEWAY_UID", step)
        self.assertIn("non-root", step)
        # Verifies the sshd process is running as root.
        self.assertIn("SSHD_UID", step)
        self.assertIn("root", step)

    def test_disabled_verification_checks_tcp_2223_absent(self) -> None:
        step = _step_text(self.workflow, "Verify josemar-mcp is absent (only when disabled)")
        self.assertIn("2223", step)
        self.assertIn("stale Tailscale Serve tcp:2223", step)
        # Checks no sshd process inside hermes.
        self.assertIn("pgrep", step)
        self.assertIn("hermes", step)

    def test_disabled_verification_requires_valid_serve_json(self) -> None:
        step = _step_text(self.workflow, "Verify josemar-mcp is absent (only when disabled)")
        # Must require valid JSON (parse with python), not accept empty.
        self.assertIn("json.loads", step)
        self.assertIn("invalid JSON", step)
        self.assertIn("empty response", step)
        # Must fail (not warn) if no valid response after retries.
        self.assertIn("ERROR: could not obtain a valid Tailscale Serve status", step)

    def test_both_disabled_clear_step_checks_both_ports(self) -> None:
        step = _step_text(
            self.workflow, "Verify Tailscale Serve is cleared (only when both disabled)"
        )
        self.assertIn("2222", step)
        self.assertIn("2223", step)

    def test_both_disabled_clear_step_requires_valid_serve_json(self) -> None:
        step = _step_text(
            self.workflow, "Verify Tailscale Serve is cleared (only when both disabled)"
        )
        self.assertIn("json.loads", step)
        self.assertIn("invalid JSON", step)
        self.assertIn("empty response", step)
        self.assertIn("ERROR: could not obtain a valid Tailscale Serve status", step)

    # --- env writing ---

    def test_env_writes_josemar_mcp_vars_only_when_enabled(self) -> None:
        env_step = _step_text(self.workflow, "Create .env file")
        self.assertIn("write_env JOSEMAR_MCP_ENABLED", env_step)
        self.assertIn("write_env JOSEMAR_MCP_SUBNET", env_step)
        self.assertIn("write_env JOSEMAR_MCP_HERMES_IP", env_step)

    def test_env_does_not_add_josemar_mcp_profile(self) -> None:
        env_step = _step_text(self.workflow, "Create .env file")
        # No COMPOSE_PROFILES addition for josemar-mcp — the overlay only
        # modifies existing services, no profile-gated service.
        self.assertNotIn("COMPOSE_PROFILES_VALUE=\"${COMPOSE_PROFILES_VALUE},josemar-mcp\"", env_step)
        self.assertNotIn("COMPOSE_PROFILES_VALUE=\"josemar-mcp\"", env_step)


class JosemarMcpOverlayContractTests(unittest.TestCase):
    def test_overlay_distinct_subnet(self) -> None:
        text = OVERLAY.read_text(encoding="utf-8")
        browser = BROWSER_OVERLAY.read_text(encoding="utf-8")
        self.assertIn("172.31.251.0/29", text)
        self.assertIn("172.31.250.0/29", browser)

    def test_overlay_no_funnel(self) -> None:
        text = OVERLAY.read_text(encoding="utf-8")
        # The actual YAML config (non-comment lines) must not enable Funnel.
        code_lines = [
            line for line in text.splitlines() if not line.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        self.assertNotIn("Funnel", code)
        self.assertNotIn("funnel", code)

    def test_overlay_internal_network(self) -> None:
        text = OVERLAY.read_text(encoding="utf-8")
        self.assertIn("internal: true", text)


if __name__ == "__main__":
    unittest.main()
