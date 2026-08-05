from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

from .helpers import ComposeRuntime


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
OVERLAY = REPO_ROOT / "docker-compose.browser-control.yml"
EMBED_OVERLAY = REPO_ROOT / "docker-compose.embeddings.yml"
MNEMOSYNE_OVERLAY = REPO_ROOT / "docker-compose.mnemosyne.yml"


def service_block(text: str, service: str) -> str:
    lines = text.splitlines(keepends=True)
    marker = f"  {service}:\n"
    start = next(index for index, line in enumerate(lines) if line == marker)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" "):
            end = index
            break
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            end = index
            break
    return "".join(lines[start:end])


def top_level_block(text: str, header: str) -> str:
    """Return the last top-level `<header>:` block (column 0)."""
    return text.rsplit(f"\n{header}:", 1)[1]


class ComposeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = COMPOSE.read_text(encoding="utf-8")
        self.overlay = OVERLAY.read_text(encoding="utf-8")

    def test_container_names_are_parameterized_for_runtime_test_isolation(self) -> None:
        # browser-tunnel lives in the overlay; the rest in base.
        for service in ["aux-ml", "hermes", "syncthing", "tailscale", "obsidian-backup"]:
            with self.subTest(service=service):
                block = service_block(self.text, service)
                self.assertIn("container_name: ${JOSEMAR_CONTAINER_PREFIX:-josemar}-", block)
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("container_name: ${JOSEMAR_CONTAINER_PREFIX:-josemar}-", block)

    def test_hermes_volume_contract(self) -> None:
        block = service_block(self.text, "hermes")
        self.assertIn("- hermes-data:/opt/data", block)
        self.assertIn("- aux-ml-shared:/shared", block)
        self.assertIn("- obsidian-vault:/opt/data/obsidian", block)
        self.assertNotIn("VAULT_GATEWAY", block)

    def test_aux_ml_shared_volume_is_read_only(self) -> None:
        block = service_block(self.text, "aux-ml")
        self.assertIn("- aux-ml-shared:/shared:ro", block)
        self.assertIn("- AUX_ML_ALLOWED_INPUT_DIRS=${AUX_ML_ALLOWED_INPUT_DIRS:-/shared}", block)

    def test_syncthing_uses_hermes_uid_gid_for_vault_access(self) -> None:
        block = service_block(self.text, "syncthing")
        self.assertIn('user: "${HERMES_UID:-10000}:${HERMES_GID:-10000}"', block)
        self.assertIn("- obsidian-vault:/var/syncthing/data/obsidian", block)

    def test_backup_vault_mount_is_read_only(self) -> None:
        block = service_block(self.text, "obsidian-backup")
        self.assertIn("- obsidian-vault:/data/obsidian:ro", block)
        self.assertIn("- obsidian-backup-state:/state", block)

    def test_public_ports_are_localhost_bound_by_default(self) -> None:
        block = service_block(self.text, "hermes")
        self.assertIn("${HERMES_API_SERVER_BIND_IP:-127.0.0.1}", block)
        self.assertIn("${HERMES_DASHBOARD_BIND_IP:-127.0.0.1}", block)

    def test_runtime_helper_scopes_container_prefix_and_tailscale_hostname(self) -> None:
        runtime = ComposeRuntime()
        self.assertTrue(runtime.project.startswith("josemar-test-"))
        self.assertEqual(runtime.env["JOSEMAR_CONTAINER_PREFIX"], runtime.project)
        self.assertEqual(runtime.env["TAILSCALE_HOSTNAME"], f"{runtime.project}-server")

    def test_gbrain_env_defaults_are_present(self) -> None:
        block = service_block(self.text, "hermes")
        self.assertIn("- GBRAIN_HOME=${GBRAIN_HOME:-/opt/data}", block)
        self.assertIn("- GBRAIN_BRAIN_REPO=${GBRAIN_BRAIN_REPO:-/opt/data/obsidian}", block)
        self.assertIn("- GBRAIN_SCHEMA_PACK=${GBRAIN_SCHEMA_PACK:-josemar}", block)
        self.assertIn("- GBRAIN_SCHEMA_SOURCE_ROOT=${GBRAIN_SCHEMA_SOURCE_ROOT:-/opt/data/.gbrain/schema-packs}", block)
        self.assertIn("- GBRAIN_REFRESH_INTERVAL=${GBRAIN_REFRESH_INTERVAL:-5}", block)

    def test_gbrain_removed_env_vars_absent(self) -> None:
        """Removed gating/bounding env vars must not appear in the hermes service."""
        block = service_block(self.text, "hermes")
        self.assertNotIn("GBRAIN_ENABLED", block)
        self.assertNotIn("GBRAIN_QUERY_TIMEOUT_SECONDS", block)
        self.assertNotIn("GBRAIN_QUERY_MAX_INPUT_CHARS", block)
        self.assertNotIn("GBRAIN_QUERY_MAX_OUTPUT_CHARS", block)
        self.assertNotIn("GBRAIN_QUERY_MAX_LIMIT", block)
        self.assertNotIn("GBRAIN_CONTENT_MAX_CHARS", block)

    def test_gbrain_does_not_add_sidecar_or_volume(self) -> None:
        # No new volume and no new service should be introduced for gbrain.
        self.assertNotIn("gbrain-data:", self.text)
        self.assertNotIn("gbrain:", self.text.split("services:")[1].split("networks:")[0])
        # HERMES_WRITABLE_VOLUMES lives in docker-hermes-init.sh, not compose;
        # ensure .gbrain is not added to any compose writable-volume list.
        self.assertNotIn("HERMES_WRITABLE_VOLUMES", self.text)

    # --- Browser control: true optionality (overlay) ---

    def test_base_has_no_browser_control_network_or_service(self) -> None:
        # The base file must NOT define the browser-control network or the
        # browser-tunnel service. True optionality means base-only deploys are
        # unchanged.
        self.assertNotIn("browser-control:", self.text)
        self.assertNotIn("browser-tunnel:", self.text)
        self.assertNotIn("hermes-browser-tunnel", self.text)
        self.assertNotIn("browser-tunnel-state", self.text)
        self.assertNotIn("browser-tunnel-authorized-keys", self.text)

    def test_base_has_tailscale_serve_config_volume_and_env(self) -> None:
        # The always-present tailscale-serve-config volume and TS_SERVE_CONFIG
        # env live in base so a disabled redeploy writes {} and clears stale
        # tcp:2222.
        block = service_block(self.text, "tailscale")
        self.assertIn("TS_SERVE_CONFIG=/config/tailscale-serve/serve.json", block)
        self.assertIn("tailscale-serve-config:/config/tailscale-serve:ro", block)
        volumes_block = top_level_block(self.text, "volumes")
        self.assertIn("tailscale-serve-config:", volumes_block)

    def test_overlay_defines_browser_control_network_internal(self) -> None:
        networks_block = top_level_block(self.overlay, "networks")
        self.assertIn("browser-control:", networks_block)
        self.assertIn("internal: true", networks_block)
        self.assertIn("subnet: ${BROWSER_CONTROL_SUBNET:-172.31.250.0/29}", networks_block)

    def test_overlay_browser_tunnel_is_profiled(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("profiles:", block)
        self.assertIn("- browser-control", block)

    def test_overlay_browser_tunnel_uses_hermes_network_mode(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("network_mode: service:hermes", block)
        # Must NOT have its own networks: key (incompatible with network_mode).
        self.assertNotIn("\n    networks:", block)

    def test_overlay_browser_tunnel_publishes_no_host_ports(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertNotIn("ports:", block)
        self.assertNotIn("expose:", block)

    def test_overlay_browser_tunnel_hardening(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        self.assertIn("read_only: true", block)
        self.assertIn("no-new-privileges:true", block)
        self.assertIn("cap_drop:", block)
        self.assertIn("- ALL", block)
        # Minimal caps: CHOWN, SETUID, SETGID, SYS_CHROOT. No NET_BIND_SERVICE.
        self.assertIn("- CHOWN", block)
        self.assertIn("- SETUID", block)
        self.assertIn("- SETGID", block)
        self.assertIn("- SYS_CHROOT", block)
        self.assertNotIn("NET_BIND_SERVICE", block)
        # Persistent host key volume.
        self.assertIn("browser-tunnel-state:/var/lib/browser-tunnel", block)
        # Authorized keys from a named volume (not a checkout bind mount).
        self.assertIn("browser-tunnel-authorized-keys:/authorized-keys:ro", block)
        # No bind mount from credentials/ checkout.
        self.assertNotIn("./credentials/", block)

    def test_overlay_browser_tunnel_fixed_constants(self) -> None:
        block = service_block(self.overlay, "browser-tunnel")
        # Only BROWSER_CONTROL_HERMES_IP is configurable; SSH user/port and
        # CDP port are fixed constants in the image.
        self.assertIn("BROWSER_CONTROL_HERMES_IP=${BROWSER_CONTROL_HERMES_IP:-172.31.250.2}", block)
        self.assertNotIn("BROWSER_TUNNEL_SSH_PORT", block)
        self.assertNotIn("BROWSER_TUNNEL_CDP_PORT", block)
        self.assertNotIn("BROWSER_TUNNEL_USER", block)

    def test_overlay_hermes_has_browser_control_alias_and_static_ip(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("hermes-browser-tunnel", block)
        self.assertIn("ipv4_address: ${BROWSER_CONTROL_HERMES_IP:-172.31.250.2}", block)

    def test_overlay_tailscale_has_browser_control_static_ip(self) -> None:
        block = service_block(self.overlay, "tailscale")
        self.assertIn("ipv4_address: ${BROWSER_CONTROL_TAILSCALE_IP:-172.31.250.3}", block)

    def test_overlay_volumes_declared(self) -> None:
        volumes_block = top_level_block(self.overlay, "volumes")
        self.assertIn("browser-tunnel-state:", volumes_block)
        self.assertIn("browser-tunnel-authorized-keys:", volumes_block)

    def test_syncthing_namespace_and_volume_unchanged(self) -> None:
        block = service_block(self.text, "syncthing")
        self.assertIn("network_mode: service:tailscale", block)
        self.assertIn("- syncthing-config:/var/syncthing/config", block)
        self.assertIn("- obsidian-vault:/var/syncthing/data/obsidian", block)

    # --- Embeddings overlay: true optionality ---

    def test_base_has_no_embedding_service_network_volume_or_env(self) -> None:
        # The base file must NOT define the embeddings service, network, volume,
        # or any embedding/Mnemosyne/llama-server env wiring. True optionality
        # means base-only deploys are unchanged.
        self.assertNotIn("embeddings:", self.text)
        self.assertNotIn("embeddings-net", self.text)
        self.assertNotIn("embedding-model-cache", self.text)
        block = service_block(self.text, "hermes")
        self.assertNotIn("EMBEDDING_", block)
        self.assertNotIn("MNEMOSYNE_EMBEDDING", block)
        self.assertNotIn("MNEMOSYNE_EMBEDDINGS_VIA_API", block)
        self.assertNotIn("GBRAIN_EMBEDDING_", block)
        self.assertNotIn("LLAMA_SERVER_BASE_URL", block)


class EmbeddingsOverlayContractTests(unittest.TestCase):
    """Contract tests for docker-compose.embeddings.yml (opt-in overlay)."""

    def setUp(self) -> None:
        self.overlay = EMBED_OVERLAY.read_text(encoding="utf-8")

    def test_overlay_defines_embeddings_service(self) -> None:
        block = service_block(self.overlay, "embeddings")
        self.assertIn("container_name: ${JOSEMAR_CONTAINER_PREFIX:-josemar}-embeddings", block)
        self.assertIn(
            "image: ${EMBEDDING_TEI_IMAGE:-ghcr.io/huggingface/text-embeddings-inference:cpu-1.9}",
            block,
        )

    def test_overlay_embeddings_publishes_no_host_ports(self) -> None:
        block = service_block(self.overlay, "embeddings")
        self.assertNotIn("ports:", block)
        # Only internal service (80) + Prometheus (9000) ports are exposed.
        self.assertIn("- \"80\"", block)
        self.assertIn("- \"9000\"", block)
        # The incorrect 6000 port must NOT be exposed.
        self.assertNotIn("- \"6000\"", block)

    def test_overlay_embeddings_passes_pinned_model_and_revision(self) -> None:
        block = service_block(self.overlay, "embeddings")
        self.assertIn("--model-id", block)
        self.assertIn("${EMBEDDING_MODEL_ID:-intfloat/multilingual-e5-small}", block)
        self.assertIn("--revision", block)
        self.assertIn(
            "${EMBEDDING_MODEL_REVISION:-614241f622f53c4eeff9890bdc4f31cfecc418b3}",
            block,
        )

    def test_overlay_embeddings_bounded_server_settings(self) -> None:
        block = service_block(self.overlay, "embeddings")
        self.assertIn("--max-concurrent-requests", block)
        self.assertIn("${EMBEDDING_MAX_CONCURRENT_REQUESTS:-64}", block)
        self.assertIn("--max-batch-tokens", block)
        self.assertIn("${EMBEDDING_MAX_BATCH_TOKENS:-16384}", block)
        self.assertIn("--max-batch-requests", block)
        self.assertIn("${EMBEDDING_MAX_BATCH_REQUESTS:-8}", block)
        self.assertIn("--max-client-batch-size", block)
        self.assertIn("${EMBEDDING_MAX_CLIENT_BATCH_SIZE:-64}", block)
        self.assertIn("--json-output", block)

    def test_overlay_embeddings_disables_spans_and_configures_prometheus_port(self) -> None:
        block = service_block(self.overlay, "embeddings")
        # TEI 1.9 has --disable-spans; pass it explicitly.
        self.assertIn("--disable-spans", block)
        # Prometheus default port is 9000 (not 6000); configure it explicitly.
        self.assertIn("--prometheus-port", block)
        self.assertIn("${EMBEDDING_PROMETHEUS_PORT:-9000}", block)

    def test_overlay_embeddings_healthcheck_uses_curl_against_health(self) -> None:
        block = service_block(self.overlay, "embeddings")
        self.assertIn("curl", block)
        self.assertIn("http://127.0.0.1/health", block)
        # start_period must be configurable and >= 120s for first download.
        self.assertIn("EMBEDDING_HEALTHCHECK_START_PERIOD", block)
        self.assertIn("start_period:", block)
        self.assertIn("interval:", block)
        self.assertIn("timeout:", block)
        self.assertIn("retries:", block)

    def test_overlay_embeddings_resource_limits_are_top_level(self) -> None:
        block = service_block(self.overlay, "embeddings")
        # Repo operational Compose style: top-level cpus / mem_limit, NOT
        # deploy.resources (which only applies under swarm).
        self.assertIn("cpus: \"${EMBEDDING_CPUS:-2}\"", block)
        self.assertIn("mem_limit: ${EMBEDDING_MEM_LIMIT:-4g}", block)
        # deploy.resources limits must NOT be present (no tested need for both).
        self.assertNotIn("deploy:", block)
        self.assertNotIn("resources:", block)

    def test_overlay_embeddings_cache_volume_mounted_at_data(self) -> None:
        block = service_block(self.overlay, "embeddings")
        self.assertIn("embedding-model-cache:/data", block)

    def test_overlay_embeddings_no_private_or_state_mounts(self) -> None:
        block = service_block(self.overlay, "embeddings")
        # No /shared, Obsidian, credentials, or Hermes state mounts.
        self.assertNotIn("/shared", block)
        self.assertNotIn("obsidian-vault", block)
        self.assertNotIn("./credentials/", block)
        self.assertNotIn("hermes-data", block)
        self.assertNotIn("/opt/data", block)

    def test_overlay_dedicated_network_used_only_by_hermes_and_embeddings(self) -> None:
        networks_block = top_level_block(self.overlay, "networks")
        self.assertIn("embeddings-net:", networks_block)
        self.assertIn("driver: bridge", networks_block)
        # Not marked internal because the first model download needs egress.
        # Check the actual network config (not the comment, which mentions
        # "internal: true" only to explain why it is NOT set).
        net_block = networks_block.split("embeddings-net:", 1)[1].split("volumes:", 1)[0]
        self.assertNotRegex(net_block, r"^\s*internal:\s*true", "network must not be internal")
        # Only hermes and embeddings join embeddings-net; no other service.
        embeddings = service_block(self.overlay, "embeddings")
        self.assertIn("embeddings-net", embeddings)
        hermes = service_block(self.overlay, "hermes")
        self.assertIn("embeddings-net", hermes)
        # aux-ml, syncthing, tailscale, obsidian-backup must not join.
        for svc in ["aux-ml", "syncthing", "tailscale", "obsidian-backup"]:
            self.assertNotIn(f"  {svc}:", self.overlay)

    def test_overlay_cache_volume_declared(self) -> None:
        volumes_block = top_level_block(self.overlay, "volumes")
        self.assertIn("embedding-model-cache:", volumes_block)

    def test_overlay_hermes_wires_mnemosyne_contract(self) -> None:
        block = service_block(self.overlay, "hermes")
        # Verified upstream Mnemosyne env contract.
        self.assertIn("MNEMOSYNE_EMBEDDINGS_VIA_API=true", block)
        # Model derives from the authoritative EMBEDDING_MODEL_ID (no duplicate
        # EMBEDDING_MNEMOSYNE_MODEL knob).
        self.assertIn(
            "MNEMOSYNE_EMBEDDING_MODEL=${EMBEDDING_MODEL_ID:-intfloat/multilingual-e5-small}",
            block,
        )
        # Singular DIM (not plural DIMENSIONS).
        self.assertIn("MNEMOSYNE_EMBEDDING_DIM=${EMBEDDING_MODEL_DIMENSIONS:-384}", block)
        # DOC_PREFIX (not PASSAGE_PREFIX).
        self.assertIn(
            "MNEMOSYNE_EMBEDDING_DOC_PREFIX=${EMBEDDING_PASSAGE_PREFIX:-passage: }", block
        )
        self.assertIn(
            "MNEMOSYNE_EMBEDDING_QUERY_PREFIX=${EMBEDDING_QUERY_PREFIX:-query: }", block
        )
        # API URL derives from the single EMBEDDING_API_URL.
        self.assertIn(
            "MNEMOSYNE_EMBEDDING_API_URL=${EMBEDDING_API_URL:-http://embeddings:80/v1}", block
        )

    def test_overlay_hermes_wires_gbrain_contract(self) -> None:
        block = service_block(self.overlay, "hermes")
        # gbrain provider model is `llama-server:${EMBEDDING_MODEL_ID}` (quoted,
        # derived from the authoritative tuple — no EMBEDDING_GBRAIN_MODEL knob).
        self.assertIn(
            '"GBRAIN_EMBEDDING_MODEL=llama-server:${EMBEDDING_MODEL_ID:-intfloat/multilingual-e5-small}"',
            block,
        )
        self.assertIn("GBRAIN_EMBEDDING_DIMENSIONS=${EMBEDDING_MODEL_DIMENSIONS:-384}", block)
        # LLAMA_SERVER_BASE_URL derives from the single EMBEDDING_API_URL.
        self.assertIn(
            "LLAMA_SERVER_BASE_URL=${EMBEDDING_API_URL:-http://embeddings:80/v1}", block
        )

    def test_overlay_hermes_does_not_define_removed_client_knobs(self) -> None:
        block = service_block(self.overlay, "hermes")
        # Inspect only the environment: entries (not comments), since comments
        # may legitimately mention the removed names while explaining the
        # change.
        env_lines = self._env_entry_lines(block)
        joined = "\n".join(env_lines)
        # Removed: invalid Mnemosyne mode, plural DIMENSIONS, PASSAGE_PREFIX.
        self.assertNotIn("MNEMOSYNE_EMBEDDING_MODE=", joined)
        self.assertNotIn("MNEMOSYNE_EMBEDDING_DIMENSIONS=", joined)
        self.assertNotIn("MNEMOSYNE_EMBEDDING_PASSAGE_PREFIX=", joined)
        # Removed: duplicated independent client-specific model/url knobs.
        self.assertNotIn("EMBEDDING_MNEMOSYNE_MODEL", joined)
        self.assertNotIn("EMBEDDING_MNEMOSYNE_MODE", joined)
        self.assertNotIn("EMBEDDING_MNEMOSYNE_API_URL", joined)
        self.assertNotIn("EMBEDDING_GBRAIN_MODEL", joined)
        self.assertNotIn("EMBEDDING_LLAMA_SERVER_BASE_URL", joined)

    @staticmethod
    def _env_entry_lines(block: str) -> list[str]:
        """Return the raw environment list entries from a service block."""
        lines = block.splitlines()
        env_idx = next(
            (i for i, ln in enumerate(lines) if ln.strip() == "environment:"),
            None,
        )
        if env_idx is None:
            return []
        entries: list[str] = []
        for ln in lines[env_idx + 1:]:
            if ln.startswith("    - "):
                entries.append(ln.strip()[2:])
            elif ln.startswith("    ") and not ln.startswith("      "):
                # mapping form (key: value) — collect too
                if ":" in ln:
                    entries.append(ln.strip())
            else:
                break
        return entries

    def test_overlay_hermes_does_not_enable_mnemosyne_or_gbrain_embeddings(self) -> None:
        block = service_block(self.overlay, "hermes")
        # The overlay must not claim to enable Mnemosyne or gbrain embeddings.
        self.assertNotIn("MNEMOSYNE_ENABLED", block)
        self.assertNotIn("GBRAIN_EMBEDDINGS_ENABLED", block)
        self.assertNotIn("GBRAIN_EMBEDDING_ENABLED", block)
        # Must not alter the wrapper keyword-only behavior.
        self.assertNotIn("GBRAIN_MCP_KEYWORD_ONLY", block)
        self.assertNotIn("search.mcp_keyword_only", block)

    def test_overlay_hermes_depends_on_healthy_embeddings(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("depends_on:", block)
        self.assertIn("embeddings:", block)
        self.assertIn("condition: service_healthy", block)

    def test_overlay_does_not_alter_base_depends_on(self) -> None:
        # The base hermes service has no depends_on; the overlay adding one is
        # acceptable because including the overlay opts in. Ensure the overlay
        # hermes block only depends on embeddings (no base-service reordering).
        block = service_block(self.overlay, "hermes")
        # The only dependency introduced is embeddings.
        self.assertNotIn("- aux-ml", block)
        self.assertNotIn("- syncthing", block)
        self.assertNotIn("- tailscale", block)


class EmbeddingsRenderedComposeTests(unittest.TestCase):
    """Rendered `docker compose config` validates the merged overlay."""

    def setUp(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker not available; rendered compose checks skipped")

    def _render(self, *, with_overlay: bool, env_overrides: dict | None = None) -> dict:
        runtime = ComposeRuntime()
        env = runtime.env.copy()
        # Safe test env values for embedding vars (no secrets). Prefix values
        # carry trailing spaces to prove exact preservation end-to-end.
        env["EMBEDDING_MODEL_ID"] = "intfloat/multilingual-e5-small"
        env["EMBEDDING_MODEL_REVISION"] = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
        env["EMBEDDING_MODEL_DIMENSIONS"] = "384"
        env["EMBEDDING_QUERY_PREFIX"] = "query: "
        env["EMBEDDING_PASSAGE_PREFIX"] = "passage: "
        if env_overrides:
            env.update(env_overrides)
        file_flags = ["-f", "docker-compose.yml"]
        if with_overlay:
            file_flags.extend(["-f", "docker-compose.embeddings.yml"])
        cmd = ["docker", "compose", *file_flags, "-p", runtime.project, "config"]
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, env=env,
            capture_output=True, text=True, check=False, timeout=120,
        )
        if proc.returncode != 0:
            self.fail(
                f"docker compose config failed (rc={proc.returncode})\n"
                f"cmd: {' '.join(cmd)}\nstderr:\n{proc.stderr}"
            )
        import yaml
        return yaml.safe_load(proc.stdout)

    @staticmethod
    def _env_dict(svc: dict) -> dict:
        env = svc["environment"]
        if isinstance(env, dict):
            return env
        return {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env if "=" in e}

    def test_base_render_has_no_embeddings_service(self) -> None:
        data = self._render(with_overlay=False)
        self.assertNotIn("embeddings", data["services"])
        self.assertNotIn("embeddings-net", data["networks"])
        self.assertNotIn("embedding-model-cache", data["volumes"])

    def test_overlay_render_has_embeddings_service_and_network(self) -> None:
        data = self._render(with_overlay=True)
        self.assertIn("embeddings", data["services"])
        self.assertIn("embeddings-net", data["networks"])
        self.assertIn("embedding-model-cache", data["volumes"])

    def test_overlay_render_embeddings_no_host_ports(self) -> None:
        data = self._render(with_overlay=True)
        emb = data["services"]["embeddings"]
        self.assertNotIn("ports", emb)
        self.assertEqual(emb.get("expose"), ["80", "9000"])

    def test_overlay_render_embeddings_pinned_defaults(self) -> None:
        data = self._render(with_overlay=True)
        emb = data["services"]["embeddings"]
        cmd = emb["command"]
        self.assertIn("intfloat/multilingual-e5-small", cmd)
        self.assertIn("614241f622f53c4eeff9890bdc4f31cfecc418b3", cmd)
        self.assertIn("64", cmd)  # max concurrent
        self.assertIn("16384", cmd)  # max batch tokens
        self.assertIn("--json-output", cmd)
        # Explicit disable-spans and prometheus-port 9000.
        self.assertIn("--disable-spans", cmd)
        self.assertIn("--prometheus-port", cmd)
        self.assertIn("9000", cmd)

    def test_overlay_render_embeddings_healthcheck_and_top_level_limits(self) -> None:
        data = self._render(with_overlay=True)
        emb = data["services"]["embeddings"]
        hc = emb["healthcheck"]
        self.assertIn("curl", hc["test"])
        self.assertIn("/health", " ".join(hc["test"]))
        # Top-level runtime limits (not deploy.resources).
        self.assertNotIn("deploy", emb)
        self.assertEqual(str(emb["cpus"]), "2")
        # 4g -> bytes (compose renders mem_limit as a string of bytes).
        self.assertEqual(int(emb["mem_limit"]), 4 * 1024 ** 3)

    def test_overlay_render_embeddings_cache_volume(self) -> None:
        data = self._render(with_overlay=True)
        emb = data["services"]["embeddings"]
        vols = emb["volumes"]
        self.assertTrue(
            any(v.get("source") == "embedding-model-cache" and v.get("target") == "/data"
                for v in vols),
            f"embedding-model-cache:/data not found in {vols}",
        )

    def test_overlay_render_hermes_client_mappings(self) -> None:
        data = self._render(with_overlay=True)
        env = self._env_dict(data["services"]["hermes"])
        # Mnemosyne verified contract.
        self.assertEqual(env["MNEMOSYNE_EMBEDDINGS_VIA_API"], "true")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_MODEL"], "intfloat/multilingual-e5-small")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_DIM"], "384")
        # Trailing spaces preserved exactly.
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_QUERY_PREFIX"], "query: ")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_DOC_PREFIX"], "passage: ")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_API_URL"], "http://embeddings:80/v1")
        # gbrain derived from the authoritative tuple.
        self.assertEqual(
            env["GBRAIN_EMBEDDING_MODEL"], "llama-server:intfloat/multilingual-e5-small"
        )
        self.assertEqual(env["GBRAIN_EMBEDDING_DIMENSIONS"], "384")
        self.assertEqual(env["LLAMA_SERVER_BASE_URL"], "http://embeddings:80/v1")
        # Removed vars must be absent.
        for removed in [
            "MNEMOSYNE_EMBEDDING_MODE",
            "MNEMOSYNE_EMBEDDING_DIMENSIONS",
            "MNEMOSYNE_EMBEDDING_PASSAGE_PREFIX",
        ]:
            self.assertNotIn(removed, env)

    def test_overlay_render_hermes_depends_on_healthy_embeddings(self) -> None:
        data = self._render(with_overlay=True)
        h = data["services"]["hermes"]
        dep = h.get("depends_on", {})
        self.assertIn("embeddings", dep)
        self.assertEqual(dep["embeddings"].get("condition"), "service_healthy")

    def test_overlay_render_dedicated_network_only_hermes_and_embeddings(self) -> None:
        data = self._render(with_overlay=True)
        nets = data["networks"]
        self.assertIn("embeddings-net", nets)
        # Only hermes and embeddings reference embeddings-net.
        members = [
            name for name, svc in data["services"].items()
            if isinstance(svc.get("networks"), dict) and "embeddings-net" in svc["networks"]
        ]
        self.assertEqual(sorted(members), ["embeddings", "hermes"])

    def test_changing_model_id_propagates_to_tei_mnemosyne_and_gbrain(self) -> None:
        # The single authoritative EMBEDDING_MODEL_ID must drive TEI, the
        # Mnemosyne model, and the `llama-server:` gbrain model together.
        data = self._render(
            with_overlay=True,
            env_overrides={"EMBEDDING_MODEL_ID": "BAAI/bge-small-en-v1.5"},
        )
        emb = data["services"]["embeddings"]
        cmd = emb["command"]
        self.assertIn("BAAI/bge-small-en-v1.5", cmd)
        # Default revision no longer matches the new model's pinned rev, but
        # the render must still carry whatever EMBEDDING_MODEL_REVISION is set
        # to (here the default e5 rev, which is fine for a structural test).
        env = self._env_dict(data["services"]["hermes"])
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_MODEL"], "BAAI/bge-small-en-v1.5")
        self.assertEqual(env["GBRAIN_EMBEDDING_MODEL"], "llama-server:BAAI/bge-small-en-v1.5")

    def test_changing_dimensions_propagates_to_mnemosyne_and_gbrain(self) -> None:
        data = self._render(
            with_overlay=True,
            env_overrides={"EMBEDDING_MODEL_DIMENSIONS": "512"},
        )
        env = self._env_dict(data["services"]["hermes"])
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_DIM"], "512")
        self.assertEqual(env["GBRAIN_EMBEDDING_DIMENSIONS"], "512")

    def test_changing_prefixes_propagates_with_trailing_spaces_preserved(self) -> None:
        data = self._render(
            with_overlay=True,
            env_overrides={
                "EMBEDDING_QUERY_PREFIX": "Q: ",
                "EMBEDDING_PASSAGE_PREFIX": "D: ",
            },
        )
        env = self._env_dict(data["services"]["hermes"])
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_QUERY_PREFIX"], "Q: ")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_DOC_PREFIX"], "D: ")

    def test_changing_api_url_propagates_to_both_client_urls(self) -> None:
        data = self._render(
            with_overlay=True,
            env_overrides={"EMBEDDING_API_URL": "http://embeddings:8080/v1"},
        )
        env = self._env_dict(data["services"]["hermes"])
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_API_URL"], "http://embeddings:8080/v1")
        self.assertEqual(env["LLAMA_SERVER_BASE_URL"], "http://embeddings:8080/v1")


# ---------------------------------------------------------------------------
# Mnemosyne opt-in overlay (docker-compose.mnemosyne.yml)
# ---------------------------------------------------------------------------


class MnemosyneOverlayContractTests(unittest.TestCase):
    """Source-contract tests for docker-compose.mnemosyne.yml (opt-in overlay)."""

    def setUp(self) -> None:
        self.assertTrue(MNEMOSYNE_OVERLAY.is_file(), f"missing overlay: {MNEMOSYNE_OVERLAY}")
        self.overlay = MNEMOSYNE_OVERLAY.read_text(encoding="utf-8")

    def test_overlay_defines_no_new_service(self) -> None:
        # The overlay must NOT define a new service. It only attaches hermes to
        # embeddings-net and wires passive Mnemosyne env vars.
        self.assertNotIn("  embeddings:", self.overlay)
        self.assertNotIn("  mnemosyne:", self.overlay)
        # Only the hermes service block may appear.
        self.assertIn("  hermes:", self.overlay)

    def test_overlay_defines_no_host_port_or_new_volume(self) -> None:
        self.assertNotIn("ports:", self.overlay)
        self.assertNotIn("volumes:", self.overlay.split("networks:")[0])
        # No new data volume.
        self.assertNotIn("mnemosyne-data", self.overlay)
        self.assertNotIn("mnemosyne-state", self.overlay)

    def test_overlay_attaches_hermes_to_embeddings_net(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("embeddings-net", block)

    def test_overlay_does_not_define_embeddings_net_network(self) -> None:
        # The network is provided by docker-compose.embeddings.yml; this overlay
        # must NOT redefine it (it requires the embeddings overlay in sequence).
        # The overlay may declare no networks: top-level block at all.
        if "\nnetworks:" in self.overlay:
            networks_block = top_level_block(self.overlay, "networks")
            self.assertNotIn("embeddings-net:", networks_block)
            self.assertNotIn("driver: bridge", networks_block)
        else:
            # No networks block at all is acceptable (hermes attaches to
            # embeddings-net which is defined by the embeddings overlay).
            pass

    def test_overlay_sets_mnemosyne_provider(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("MNEMOSYNE_PROVIDER=mnemosyne", block)

    def test_overlay_sets_mnemosyne_data_dir(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("MNEMOSYNE_DATA_DIR=/opt/data/mnemosyne/data", block)

    def test_overlay_passive_sync_settings(self) -> None:
        block = service_block(self.overlay, "hermes")
        # Validated user-only sync var (env mirror; authoritative value is set
        # in nested runtime config by init).
        self.assertIn("MNEMOSYNE_SYNC_ROLES=user", block)
        # Skip non-primary contexts.
        self.assertIn("MNEMOSYNE_SKIP_CONTEXTS=cron,flush,subagent,background,skill_loop", block)

    def test_overlay_explicit_turn_limits(self) -> None:
        block = service_block(self.overlay, "hermes")
        # Validated turn-limit vars (env mirror; authoritative value is set in
        # nested runtime config by init).
        self.assertIn("MNEMOSYNE_SYNC_TURN_USER_LIMIT=500", block)
        self.assertIn("MNEMOSYNE_SYNC_TURN_ASSISTANT_LIMIT=800", block)

    def test_overlay_disables_auto_sleep_and_reflection(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn("MNEMOSYNE_AUTO_SLEEP_ENABLED=false", block)
        self.assertIn("MNEMOSYNE_REFLECT_MAX_CALLS_PER_SESSION=0", block)
        self.assertIn("MNEMOSYNE_REFLECT_DISABLED_FOR_CRON=true", block)

    def test_overlay_sets_default_scope_global(self) -> None:
        block = service_block(self.overlay, "hermes")
        # Global cross-session capture (env mirror; profile_isolation is set in
        # nested runtime config by init because it has no env var mapping).
        self.assertIn("MNEMOSYNE_DEFAULT_SCOPE=global", block)

    def test_overlay_does_not_define_invalid_passive_vars(self) -> None:
        block = service_block(self.overlay, "hermes")
        # The five invalid names must NOT appear in the overlay.
        self.assertNotIn("MNEMOSYNE_SYNC_USER_ONLY", block)
        self.assertNotIn("MNEMOSYNE_USER_LIMIT", block)
        self.assertNotIn("MNEMOSYNE_ASSISTANT_LIMIT", block)
        self.assertNotIn("MNEMOSYNE_CONSOLIDATION_ENABLED", block)
        self.assertNotIn("MNEMOSYNE_REFLECTION_ENABLED", block)
        # profile_isolation has NO env var mapping; it must NOT be set in the
        # overlay (init sets it in nested runtime config).
        self.assertNotIn("MNEMOSYNE_PROFILE_ISOLATION", block)

    def test_overlay_does_not_duplicate_remote_e5_settings(self) -> None:
        block = service_block(self.overlay, "hermes")
        # Remote E5 settings already flow from the embeddings overlay; this
        # overlay must NOT redefine/duplicate them.
        self.assertNotIn("MNEMOSYNE_EMBEDDINGS_VIA_API", block)
        self.assertNotIn("MNEMOSYNE_EMBEDDING_MODEL", block)
        self.assertNotIn("MNEMOSYNE_EMBEDDING_DIM", block)
        self.assertNotIn("MNEMOSYNE_EMBEDDING_QUERY_PREFIX", block)
        self.assertNotIn("MNEMOSYNE_EMBEDDING_DOC_PREFIX", block)
        self.assertNotIn("MNEMOSYNE_EMBEDDING_API_URL", block)

    def test_overlay_does_not_activate_gbrain_or_set_provider_in_compose(self) -> None:
        block = service_block(self.overlay, "hermes")
        # Inspect only the environment: entries (not comments), since comments
        # may legitimately mention memory.provider while explaining that init
        # sets it at runtime.
        env_lines = EmbeddingsOverlayContractTests._env_entry_lines(block)
        joined = "\n".join(env_lines)
        # Must not set memory.provider in compose (init does that at runtime).
        self.assertNotIn("memory.provider", joined)
        # Must not alter gbrain keyword-only behavior.
        self.assertNotIn("GBRAIN_MCP_KEYWORD_ONLY", joined)
        self.assertNotIn("search.mcp_keyword_only", joined)
        self.assertNotIn("GBRAIN_EMBEDDING_MODEL", joined)

    def test_overlay_does_not_add_depends_on(self) -> None:
        # The overlay must not add depends_on that would alter base compose
        # ordering. The embeddings overlay already adds the healthy dependency.
        block = service_block(self.overlay, "hermes")
        self.assertNotIn("depends_on:", block)

    def test_overlay_does_not_set_mnemosyne_home(self) -> None:
        # MNEMOSYNE_HOME is not the runtime storage override; MNEMOSYNE_DATA_DIR
        # is authoritative. The overlay must not set MNEMOSYNE_HOME.
        block = service_block(self.overlay, "hermes")
        self.assertNotIn("MNEMOSYNE_HOME", block)


class MnemosyneRenderedComposeTests(unittest.TestCase):
    """Rendered `docker compose config` validates the mnemosyne overlay."""

    def setUp(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker not available; rendered compose checks skipped")

    def _render(self, *, overlays: list[Path], env_overrides: dict | None = None) -> dict:
        runtime = ComposeRuntime()
        env = runtime.env.copy()
        env["EMBEDDING_MODEL_ID"] = "intfloat/multilingual-e5-small"
        env["EMBEDDING_MODEL_REVISION"] = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
        env["EMBEDDING_MODEL_DIMENSIONS"] = "384"
        env["EMBEDDING_QUERY_PREFIX"] = "query: "
        env["EMBEDDING_PASSAGE_PREFIX"] = "passage: "
        if env_overrides:
            env.update(env_overrides)
        file_flags = ["-f", "docker-compose.yml"]
        for ov in overlays:
            file_flags.extend(["-f", str(ov)])
        cmd = ["docker", "compose", *file_flags, "-p", runtime.project, "config"]
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, env=env,
            capture_output=True, text=True, check=False, timeout=120,
        )
        if proc.returncode != 0:
            self.fail(
                f"docker compose config failed (rc={proc.returncode})\n"
                f"cmd: {' '.join(cmd)}\nstderr:\n{proc.stderr}"
            )
        import yaml
        return yaml.safe_load(proc.stdout)

    @staticmethod
    def _env_dict(svc: dict) -> dict:
        env = svc["environment"]
        if isinstance(env, dict):
            return env
        return {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env if "=" in e}

    def test_base_render_has_no_mnemosyne_env(self) -> None:
        data = self._render(overlays=[])
        env = self._env_dict(data["services"]["hermes"])
        for key in [
            "MNEMOSYNE_PROVIDER",
            "MNEMOSYNE_DATA_DIR",
            "MNEMOSYNE_SYNC_ROLES",
            "MNEMOSYNE_AUTO_SLEEP_ENABLED",
            "MNEMOSYNE_DEFAULT_SCOPE",
        ]:
            self.assertNotIn(key, env)

    def test_mnemosyne_overlay_requires_embeddings_overlay(self) -> None:
        # Rendering mnemosyne WITHOUT the embeddings overlay must fail because
        # embeddings-net is undefined.
        proc = subprocess.run(
            ["docker", "compose", "-f", "docker-compose.yml",
             "-f", "docker-compose.mnemosyne.yml", "-p", "josemar-test-mnemo-nonet",
             "config"],
            cwd=REPO_ROOT,
            env={**ComposeRuntime().env,
                 "EMBEDDING_MODEL_ID": "intfloat/multilingual-e5-small",
                 "EMBEDDING_MODEL_DIMENSIONS": "384",
                 "EMBEDDING_QUERY_PREFIX": "query: ",
                 "EMBEDDING_PASSAGE_PREFIX": "passage: "},
            capture_output=True, text=True, check=False, timeout=120,
        )
        self.assertNotEqual(proc.returncode, 0,
                            "mnemosyne overlay must require the embeddings overlay "
                            "(embeddings-net undefined without it)")

    def test_mnemosyne_overlay_with_embeddings_renders_cleanly(self) -> None:
        data = self._render(overlays=[EMBED_OVERLAY, MNEMOSYNE_OVERLAY])
        env = self._env_dict(data["services"]["hermes"])
        # Provider and data dir.
        self.assertEqual(env["MNEMOSYNE_PROVIDER"], "mnemosyne")
        self.assertEqual(env["MNEMOSYNE_DATA_DIR"], "/opt/data/mnemosyne/data")
        # Validated passive settings (env mirrors; authoritative nested config
        # is set by init).
        self.assertEqual(env["MNEMOSYNE_SYNC_ROLES"], "user")
        self.assertEqual(env["MNEMOSYNE_SKIP_CONTEXTS"],
                         "cron,flush,subagent,background,skill_loop")
        self.assertEqual(env["MNEMOSYNE_DEFAULT_SCOPE"], "global")
        self.assertEqual(env["MNEMOSYNE_SYNC_TURN_USER_LIMIT"], "500")
        self.assertEqual(env["MNEMOSYNE_SYNC_TURN_ASSISTANT_LIMIT"], "800")
        self.assertEqual(env["MNEMOSYNE_AUTO_SLEEP_ENABLED"], "false")
        self.assertEqual(env["MNEMOSYNE_REFLECT_MAX_CALLS_PER_SESSION"], "0")
        self.assertEqual(env["MNEMOSYNE_REFLECT_DISABLED_FOR_CRON"], "true")
        # The five invalid names + profile_isolation (no env mapping) must NOT
        # be present.
        for invalid in [
            "MNEMOSYNE_SYNC_USER_ONLY",
            "MNEMOSYNE_USER_LIMIT",
            "MNEMOSYNE_ASSISTANT_LIMIT",
            "MNEMOSYNE_CONSOLIDATION_ENABLED",
            "MNEMOSYNE_REFLECTION_ENABLED",
            "MNEMOSYNE_PROFILE_ISOLATION",
        ]:
            self.assertNotIn(invalid, env)
        # Remote E5 settings flow from the embeddings overlay (not duplicated).
        self.assertEqual(env["MNEMOSYNE_EMBEDDINGS_VIA_API"], "true")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_MODEL"], "intfloat/multilingual-e5-small")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_DIM"], "384")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_QUERY_PREFIX"], "query: ")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_DOC_PREFIX"], "passage: ")
        self.assertEqual(env["MNEMOSYNE_EMBEDDING_API_URL"], "http://embeddings:80/v1")
        # No MNEMOSYNE_HOME.
        self.assertNotIn("MNEMOSYNE_HOME", env)

    def test_mnemosyne_overlay_combines_with_browser_control(self) -> None:
        # base + embeddings + browser-control + mnemosyne must render cleanly.
        data = self._render(
            overlays=[OVERLAY, EMBED_OVERLAY, MNEMOSYNE_OVERLAY],
            env_overrides={"COMPOSE_PROFILES": "browser-control"},
        )
        env = self._env_dict(data["services"]["hermes"])
        self.assertEqual(env["MNEMOSYNE_PROVIDER"], "mnemosyne")
        # browser-control wiring still present.
        self.assertIn("browser-control", data["networks"])

    def test_mnemosyne_overlay_no_new_service_or_volume(self) -> None:
        data = self._render(overlays=[EMBED_OVERLAY, MNEMOSYNE_OVERLAY])
        # No new service beyond base + embeddings.
        self.assertIn("embeddings", data["services"])
        self.assertNotIn("mnemosyne", data["services"])
        # No new volume.
        self.assertNotIn("mnemosyne-data", data["volumes"])
        self.assertNotIn("mnemosyne-state", data["volumes"])


if __name__ == "__main__":
    unittest.main()
