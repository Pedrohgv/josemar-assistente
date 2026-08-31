"""Contract tests for the repo-owned browser-control skill.

The skill is instruction-only and baked into the Hermes image so it is always
registered regardless of whether the optional browser-control Compose overlay
is enabled. Routine runtime-driving guidance stays in SKILL.md; operator-side
first-time setup lives under references/setup.md and is loaded on demand.
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
REFERENCES_DIR = SKILL_DIR / "references"
SETUP_MD = REFERENCES_DIR / "setup.md"
LEGACY_SETUP_MD = SKILL_DIR / "SETUP.md"
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


# Operator-specific / unsupported strings must not leak into the always-loaded
# runtime-driving SKILL.md. Setup details belong in references/setup.md.
FORBIDDEN_SKILL_STRINGS = [
    "Josemar Browser", "josemar-browser-control", ".josemar-chrome-profile",
    "BROWSER_TUNNEL_AUTHORIZED_KEY", "BROWSER_CONTROL_ENABLED",
    "127.0.0.1:9222", "curl -s http://127.0.0.1:9222", "/json/version",
    "webSocketDebuggerUrl", "Tailscale", "tailscale", "SSH tunnel",
    "ssh -R", "ssh -N", "ExitOnForwardFailure", "GitHub", "Pedro",
    "josemar-server", "google-chrome", "--user-data-dir",
    "--remote-debugging-port", "0.0.0.0", "browser_select",
    "use_connected_browser", "not part of the active tool surface",
]

REQUIRED_SKILL_STRINGS = [
    ("three-route rule", "three intentionally distinct"),
    ("three-route rule", "different browsers with different state"),
    ("three-route rule", "search/extraction"),
    ("three-route rule", "connected_browser_exec"),
    ("three-route rule", "browser_*"),
    ("web_search preference", "web_search"),
    ("web_search preference", "read-only"),
    ("ordinary route tool vocabulary", "browser_snapshot"),
    ("ordinary route snapshot workflow", "snapshot"),
    ("ordinary route snapshot workflow", "re-snapshot"),
    ("ordinary route snapshot workflow", "refs"),
    ("no first-use download", "first-use"),
    ("fail-closed connected route", "fails closed"),
    ("fail-closed connected route", "fall back to"),
    ("connected session vocabulary", "session"),
    ("connection-failure recovery", "operator-controlled"),
    ("connection-failure recovery", "reopen"),
    ("connection-failure recovery", "retry"),
    ("prompt-injection defense", "prompt-injection"),
    ("prompt-injection defense", "untrusted"),
    ("prompt-injection defense", "only the user"),
    ("credential/auth boundaries", "password"),
    ("credential/auth boundaries", "2fa"),
    ("credential/auth boundaries", "payment"),
    ("credential/auth boundaries", "captcha"),
    ("consequential confirmation", "consequential"),
    ("consequential confirmation", "confirm with the user"),
    ("no session termination", "do not close"),
    ("task scoping", "scope"),
    ("task scoping", "unrelated"),
    ("dedicated profile", "dedicated"),
    ("dedicated profile", "profile"),
    ("setup pointer", "references/setup.md"),
    ("setup loader", 'skill_view("browser-control", file_path="references/setup.md")'),
]

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

FORBIDDEN_SETUP_STRINGS = [
    "snapshot first",
    "re-snapshot",
    "prompt-injection",
]

FORBIDDEN_HEADLESS_PHRASES = [
    "use the headless", "drive headless", "run headless", "launch headless",
]

FORBIDDEN_SKILL_CLAIMS = [
    "browser_* tools are not available",
    "browser_* tools are unavailable",
    "documentation-only",
    "not registered",
    "is not registered",
    "are not registered",
]


class BrowserControlSkillContractTests(unittest.TestCase):
    """Frontmatter, content, modular layout, and image-bake contract."""

    def setUp(self) -> None:
        self.assertTrue(SKILL_MD.is_file(), f"missing skill file: {SKILL_MD}")
        self.text = SKILL_MD.read_text(encoding="utf-8")
        self.lower = self.text.lower()

    def test_skill_layout_uses_references_directory(self) -> None:
        self.assertFalse(
            LEGACY_SETUP_MD.exists(),
            "operator setup must live under references/, not as sibling SETUP.md",
        )
        self.assertTrue(REFERENCES_DIR.is_dir(), f"missing references directory: {REFERENCES_DIR}")
        self.assertTrue(SETUP_MD.is_file(), f"missing setup reference: {SETUP_MD}")

    def test_frontmatter_fields_and_required_tools(self) -> None:
        if yaml is None:
            self.skipTest("PyYAML not available")
        data = parse_frontmatter(self.text)
        self.assertEqual(data.get("name"), "browser-control")
        self.assertEqual(data.get("version"), "3.0.0")
        desc = data.get("description")
        self.assertIsInstance(desc, str)
        assert isinstance(desc, str)
        self.assertTrue(desc.strip(), "description must be non-empty")
        self.assertNotIn("categories", data, "use metadata.hermes.requires_tools, not categories")
        hermes = data.get("metadata", {}).get("hermes", {})
        requires = hermes.get("requires_tools")
        self.assertIsInstance(requires, list, "metadata.hermes.requires_tools must be a list")
        assert isinstance(requires, list)
        self.assertIn("connected_browser_exec", requires)
        self.assertNotIn("browser_exec", requires)
        self.assertNotIn("browser_navigate", requires)
        self.assertNotIn("browser_snapshot", requires)

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
        self.assertNotIn("ssh ", self.lower)
        self.assertNotIn("curl ", self.lower)

    def test_skill_body_teaches_required_concepts(self) -> None:
        for label, needle in REQUIRED_SKILL_STRINGS:
            with self.subTest(concept=label, needle=needle):
                self.assertIn(
                    needle.lower(), self.lower,
                    f"missing required concept {label!r}: {needle!r}",
                )

    def test_skill_baked_into_image(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn(
            "COPY skills-factory/browser-control /opt/josemar/skills/browser-control",
            dockerfile,
        )
        for skill in ("gbrain", "aux-ml", "workspace-sync"):
            with self.subTest(skill=skill):
                self.assertIn(
                    f"COPY skills-factory/{skill} /opt/josemar/skills/{skill}",
                    dockerfile,
                )


class BrowserControlSetupReferenceContractTests(unittest.TestCase):
    """The on-demand setup reference owns operator-specific enablement content."""

    def setUp(self) -> None:
        self.assertTrue(SETUP_MD.is_file(), f"missing setup reference: {SETUP_MD}")
        self.text = SETUP_MD.read_text(encoding="utf-8")
        self.lower = self.text.lower()

    def test_setup_reference_teaches_required_concepts(self) -> None:
        for label, needle in REQUIRED_SETUP_STRINGS:
            with self.subTest(concept=label, needle=needle):
                self.assertIn(
                    needle.lower(), self.lower,
                    f"missing required setup concept {label!r}: {needle!r}",
                )

    def test_setup_reference_does_not_duplicate_runtime_guidance(self) -> None:
        for needle in FORBIDDEN_SETUP_STRINGS:
            with self.subTest(kind="forbidden in setup reference", needle=needle):
                self.assertNotIn(
                    needle.lower(), self.lower,
                    f"runtime-driving guidance must stay in SKILL.md, not setup reference: {needle!r}",
                )


class BrowserControlComposeMountTests(unittest.TestCase):
    """Skill is baked into the image; the overlay must not bind-mount it."""

    def setUp(self) -> None:
        self.base = BASE_COMPOSE.read_text(encoding="utf-8")
        self.overlay = OVERLAY.read_text(encoding="utf-8")

    def test_base_and_overlay_exclude_skill_bind_mount(self) -> None:
        self.assertNotIn(f"{SKILL_SOURCE}:", self.base)
        self.assertNotIn(f"{SKILL_SOURCE}:", self.overlay)
        self.assertNotIn(f"{SKILL_TARGET}:ro", self.overlay)
        self.assertNotIn(f"{SKILL_TARGET}:rw", self.overlay)

    def test_overlay_browser_tunnel_does_not_mount_skill(self) -> None:
        tunnel = service_block(self.overlay, "browser-tunnel")
        self.assertNotIn(SKILL_SOURCE, tunnel)
        self.assertNotIn(SKILL_TARGET, tunnel)


class BrowserControlRenderedComposeTests(unittest.TestCase):
    """Rendered Compose must also exclude a browser-control skill bind mount."""

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
            cmd,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
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
        self.assertNotIn(SKILL_SOURCE, overlay)
        self.assertNotIn(SKILL_TARGET, overlay)


class BrowserControlDocsAccuracyTests(unittest.TestCase):
    """Operator docs must reflect skill registration and separate connected route."""

    def setUp(self) -> None:
        self.assertTrue(DOCS.is_file(), f"missing docs: {DOCS}")
        self.docs = DOCS.read_text(encoding="utf-8").lower()

    def test_docs_accurate_about_disabled_deploy(self) -> None:
        self.assertNotIn(
            "will not attempt browser actions",
            self.docs,
            "overlay gates the tunnel sidecar, not built-in browser tools",
        )
        self.assertNotIn(
            "not registered",
            self.docs,
            "the skill is baked into the image and always registered",
        )
        self.assertIn("baked", self.docs)
        self.assertIn("connected_browser_exec", self.docs)


if __name__ == "__main__":
    unittest.main()
