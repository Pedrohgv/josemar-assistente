"""Contract tests for the repo-owned browser-control skill.

The skill is instruction-only (SKILL.md + SETUP.md, no executable) and is
baked into the Hermes image so it is always registered, regardless of
whether the browser-control Compose overlay is enabled. The overlay gates the
tunnel sidecar and network, not the skill. These tests enforce the agreed
design: valid frontmatter with required tools, generic operator-agnostic
wording in SKILL.md (operator-specific setup content lives in SETUP.md),
the native workflow/safety/recovery guidance, and image-baked registration.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - environment-dependent
    yaml = None  # type: ignore

from .helpers import ComposeRuntime


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills-factory" / "browser-control"
SKILL_MD = SKILL_DIR / "SKILL.md"
SETUP_MD = SKILL_DIR / "SETUP.md"
DOCKERFILE = REPO_ROOT / "Dockerfile.hermes"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
OVERLAY = REPO_ROOT / "docker-compose.browser-control.yml"
DOCS = REPO_ROOT / "docs" / "browser-control.md"
SKILL_TARGET = "/opt/josemar/skills/browser-control"
SKILL_SOURCE = "./skills-factory/browser-control"


def service_block(text: str, service: str) -> str:
    """Return the indented `<service>:` block from a compose file."""
    lines = text.splitlines(keepends=True)
    marker = f"  {service}:\n"
    start = next(i for i, ln in enumerate(lines) if ln == marker)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln and not ln.startswith(" "):
            end = i
            break
        if ln.startswith("  ") and not ln.startswith("    ") and ln.strip().endswith(":"):
            end = i
            break
    return "".join(lines[start:end])


def parse_frontmatter(text: str) -> dict:
    assert yaml is not None, "PyYAML required"
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    parts = text.split("---\n", 2)
    assert len(parts) >= 3, "SKILL.md must have closing frontmatter delimiter"
    data = yaml.safe_load(parts[1])
    assert isinstance(data, dict), "frontmatter must parse to a mapping"
    return data


# Operator-specific / unsupported strings that must not leak into the
# LLM-facing SKILL.md (runtime-driving guidance). Operator-specific setup
# content lives in SETUP.md, which has its own content contract. Includes
# the unsupported `browser_select` tool (pinned Hermes exposes
# navigate/snapshot/click/type/scroll/back/press/get_images/
# vision/console/cdp/dialog, not select).
FORBIDDEN_SKILL_STRINGS = [
    "Josemar Browser", "josemar-browser-control", ".josemar-chrome-profile",
    "BROWSER_TUNNEL_AUTHORIZED_KEY", "BROWSER_CONTROL_ENABLED",
    "127.0.0.1:9222", "curl -s http://127.0.0.1:9222", "/json/version",
    "webSocketDebuggerUrl", "Tailscale", "tailscale", "SSH tunnel",
    "ssh -R", "ssh -N", "ExitOnForwardFailure", "GitHub", "Pedro",
    "josemar-server", "google-chrome", "--user-data-dir",
    "--remote-debugging-port", "0.0.0.0", "browser_select",
]

# Required substrings in the SKILL.md body, grouped by concern. Each entry is
# (label, needle) so failures name the missing concept, not just the string.
REQUIRED_SKILL_STRINGS = [
    ("headful external browser", "externally connected"),
    ("headful external browser", "headful"),
    ("web_search preference", "web_search"),
    ("web_search preference", "read-only"),
    ("snapshot-first workflow", "browser_snapshot"),
    ("snapshot-first workflow", "snapshot first"),
    ("snapshot-first workflow", "refs"),
    ("snapshot-first workflow", "re-snapshot"),
    ("snapshot-first workflow", "verify"),
    ("connection-failure recovery", "operator"),
    ("connection-failure recovery", "reopen"),
    ("connection-failure recovery", "retry"),
    ("prompt-injection defense", "prompt-injection"),
    ("prompt-injection defense", "untrusted"),
    ("prompt-injection defense", "only the user"),
    ("credential/auth boundaries", "password"),
    ("credential/auth boundaries", "2fa"),
    ("credential/auth boundaries", "payment"),
    ("no session termination", "do not close"),
    ("no session termination", "session"),
    ("task scoping", "scope"),
    ("task scoping", "unrelated"),
    ("instruction-only/repo-owned", "instruction-only"),
    ("soft-gate: always registered", "always registered"),
    ("soft-gate: overlay gates tunnel not skill", "overlay gates"),
    ("setup pointer", "setup.md"),
]

# Required substrings in SETUP.md. Operator-specific setup content that is
# forbidden in SKILL.md must be present here.
REQUIRED_SETUP_STRINGS = [
    ("ssh keypair generation", "ssh-keygen"),
    ("ssh keypair path", "josemar_browser_tunnel"),
    ("authorized key secret", "BROWSER_TUNNEL_AUTHORIZED_KEY"),
    ("overlay enablement variable", "BROWSER_CONTROL_ENABLED"),
    ("tailscale ACL", "tailscale"),
    ("tailscale ACL dst port", "2222"),
    ("chrome version requirement", "136"),
    ("linux mint launcher", "josemar-browser-control"),
    ("dedicated chrome profile", ".josemar-chrome-profile"),
    ("cdp verification", "/json/version"),
    ("disable/rollback", "BROWSER_CONTROL_ENABLED=false"),
    ("dedicated profile warning", "dedicated"),
]

# SETUP.md must not duplicate the runtime-driving workflow guidance that
# belongs in SKILL.md. Keep setup focused on first-time enablement.
FORBIDDEN_SETUP_STRINGS = [
    "snapshot first",
    "re-snapshot",
    "prompt-injection",
]

# Affirmative headless phrasings the skill must not use (negation like
# "not a headless scraper" is fine).
FORBIDDEN_HEADLESS_PHRASES = [
    "use the headless", "drive headless", "run headless", "launch headless",
]

# False claims the skill must not make: the skill is always registered
# (baked into the image), so claims that it disappears when the overlay is off
# are wrong. The overlay gates the tunnel sidecar, not the skill.
FORBIDDEN_SKILL_CLAIMS = [
    "browser_* tools are not available",
    "browser_* tools are unavailable",
    "documentation-only",
    "not registered",
    "is not registered",
    "are not registered",
]


class BrowserControlSkillContractTests(unittest.TestCase):
    """Frontmatter, content, and image-mount contract for the skill."""

    def setUp(self) -> None:
        self.assertTrue(SKILL_MD.is_file(), f"missing skill file: {SKILL_MD}")
        self.text = SKILL_MD.read_text(encoding="utf-8")
        self.lower = self.text.lower()

    def test_skill_dir_has_skill_md_and_setup_md(self) -> None:
        self.assertEqual(
            sorted(p.name for p in SKILL_DIR.iterdir()),
            ["SETUP.md", "SKILL.md"],
        )

    def test_setup_md_exists(self) -> None:
        self.assertTrue(SETUP_MD.is_file(), f"missing setup file: {SETUP_MD}")

    def test_frontmatter_fields_and_required_tools(self) -> None:
        if yaml is None:
            self.skipTest("PyYAML not available")
        data = parse_frontmatter(self.text)
        self.assertEqual(data.get("name"), "browser-control")
        self.assertEqual(data.get("version"), "1.1.0")
        desc = data.get("description")
        self.assertIsInstance(desc, str)
        assert isinstance(desc, str)
        self.assertTrue(desc.strip(), "description must be non-empty")
        self.assertNotIn("categories", data, "use metadata.hermes.requires_tools, not categories")
        hermes = data.get("metadata", {}).get("hermes", {})
        requires = hermes.get("requires_tools")
        self.assertIsInstance(requires, list, "metadata.hermes.requires_tools must be a list")
        assert isinstance(requires, list)
        self.assertIn("browser_navigate", requires)
        self.assertIn("browser_snapshot", requires)

    def test_skill_body_is_generic_and_accurate(self) -> None:
        for needle in FORBIDDEN_SKILL_STRINGS:
            with self.subTest(kind="forbidden string", needle=needle):
                self.assertNotIn(
                    needle.lower(), self.lower,
                    f"operator-specific/unsupported string must not appear: {needle!r}",
                )
        for phrase in FORBIDDEN_HEADLESS_PHRASES:
            with self.subTest(kind="forbidden headless phrase", phrase=phrase):
                self.assertNotIn(phrase, self.lower)
        for claim in FORBIDDEN_SKILL_CLAIMS:
            with self.subTest(kind="false claim", claim=claim):
                self.assertNotIn(claim, self.lower)
        # Recovery must ask the operator, not shell in.
        self.assertNotIn("ssh ", self.lower)
        self.assertNotIn("curl ", self.lower)

    def test_skill_body_teaches_required_concepts(self) -> None:
        for label, needle in REQUIRED_SKILL_STRINGS:
            with self.subTest(concept=label, needle=needle):
                self.assertIn(
                    needle, self.lower,
                    f"missing required concept {label!r}: {needle!r}",
                )

    def test_skill_baked_into_image(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        # The browser-control skill is baked into the image (not overlay-
        # mounted) so it is always registered and can guide first-time setup
        # and self-diagnose when the overlay is disabled.
        self.assertIn(
            "COPY skills-factory/browser-control /opt/josemar/skills/browser-control",
            dockerfile,
        )
        # Other repo-owned skills are still baked in.
        for skill in ("gbrain", "aux-ml", "workspace-sync"):
            with self.subTest(skill=skill):
                self.assertIn(
                    f"COPY skills-factory/{skill} /opt/josemar/skills/{skill}",
                    dockerfile,
                )


class BrowserControlSetupMdContractTests(unittest.TestCase):
    """SETUP.md holds operator-specific first-time setup content."""

    def setUp(self) -> None:
        self.assertTrue(SETUP_MD.is_file(), f"missing setup file: {SETUP_MD}")
        self.text = SETUP_MD.read_text(encoding="utf-8")
        self.lower = self.text.lower()

    def test_setup_md_teaches_required_concepts(self) -> None:
        for label, needle in REQUIRED_SETUP_STRINGS:
            with self.subTest(concept=label, needle=needle):
                self.assertIn(
                    needle.lower(), self.lower,
                    f"missing required setup concept {label!r}: {needle!r}",
                )

    def test_setup_md_does_not_duplicate_runtime_guidance(self) -> None:
        for needle in FORBIDDEN_SETUP_STRINGS:
            with self.subTest(kind="forbidden in setup", needle=needle):
                self.assertNotIn(
                    needle.lower(), self.lower,
                    f"runtime-driving guidance must stay in SKILL.md, not SETUP.md: {needle!r}",
                )


class BrowserControlComposeMountTests(unittest.TestCase):
    """Skill is baked into the image; the overlay must NOT bind-mount it."""

    def setUp(self) -> None:
        self.base = BASE_COMPOSE.read_text(encoding="utf-8")
        self.overlay = OVERLAY.read_text(encoding="utf-8")

    def test_base_and_overlay_exclude_skill_bind_mount(self) -> None:
        # No bind mount of the skill source in either base or overlay.
        self.assertNotIn(f"{SKILL_SOURCE}:", self.base)
        self.assertNotIn(f"{SKILL_SOURCE}:", self.overlay)
        self.assertNotIn(f"{SKILL_TARGET}:ro", self.overlay)
        self.assertNotIn(f"{SKILL_TARGET}:rw", self.overlay)

    def test_overlay_browser_tunnel_does_not_mount_skill(self) -> None:
        tunnel = service_block(self.overlay, "browser-tunnel")
        self.assertNotIn(SKILL_SOURCE, tunnel)
        self.assertNotIn(SKILL_TARGET, tunnel)


class BrowserControlRenderedComposeTests(unittest.TestCase):
    """Rendered `docker compose config` proves neither base nor overlay
    bind-mounts the skill (it is baked into the image)."""

    def setUp(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker not available; rendered compose checks skipped")

    def _render(self, *, with_overlay: bool) -> str:
        runtime = ComposeRuntime()
        env = runtime.env.copy()
        file_flags = ["-f", "docker-compose.yml"]
        if with_overlay:
            file_flags.extend(["-f", "docker-compose.browser-control.yml"])
            profiles = env.get("COMPOSE_PROFILES", "")
            env["COMPOSE_PROFILES"] = (
                "browser-control" if not profiles else f"{profiles},browser-control"
            )
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
        return proc.stdout

    def test_rendered_base_and_overlay_exclude_skill_bind_mount(self) -> None:
        base = self._render(with_overlay=False)
        self.assertNotIn(SKILL_SOURCE, base)
        self.assertNotIn(SKILL_TARGET, base)

        overlay = self._render(with_overlay=True)
        # The skill is baked into the image; the overlay must not bind-mount
        # the skill source directory.
        self.assertNotIn(SKILL_SOURCE, overlay)
        self.assertNotIn(SKILL_TARGET, overlay)


class BrowserControlDocsAccuracyTests(unittest.TestCase):
    """Docs must reflect that the skill is baked in (always registered) and
    that the overlay gates the tunnel sidecar, not the skill."""

    def setUp(self) -> None:
        self.assertTrue(DOCS.is_file(), f"missing docs: {DOCS}")
        self.docs = DOCS.read_text(encoding="utf-8").lower()

    def test_docs_accurate_about_disabled_deploy(self) -> None:
        self.assertNotIn(
            "will not attempt browser actions", self.docs,
            "overlay gates the tunnel sidecar, not built-in browser tools",
        )
        self.assertNotIn(
            "not registered", self.docs,
            "the skill is baked into the image and always registered; docs must not claim it disappears when the overlay is off",
        )
        self.assertIn(
            "baked", self.docs,
            "docs should state the repo-owned skill is baked into the image",
        )


if __name__ == "__main__":
    unittest.main()