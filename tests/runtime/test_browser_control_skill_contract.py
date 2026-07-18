"""Contract tests for the repo-owned browser-control skill.

The skill is instruction-only (a SKILL.md with no executable) and is registered
only by the optional docker-compose.browser-control.yml overlay via a read-only
bind mount on the `hermes` service. These tests enforce the agreed design:
valid frontmatter with required tools, generic operator-agnostic wording, the
native workflow/safety/recovery guidance, and overlay-only read-only mounting.
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
# LLM-facing skill. Includes the unsupported `browser_select` tool (pinned
# Hermes exposes navigate/snapshot/click/type/scroll/back/press/get_images/
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

# Required substrings in the skill body, grouped by concern. Each entry is
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
    ("overlay gates guidance not tools", "not registered"),
]

# Affirmative headless phrasings the skill must not use (negation like
# "not a headless scraper" is fine).
FORBIDDEN_HEADLESS_PHRASES = [
    "use the headless", "drive headless", "run headless", "launch headless",
]

# False claims the skill must not make: the overlay gates registration of this
# repo-owned guidance, not Hermes's built-in browser tool schemas.
FORBIDDEN_SKILL_CLAIMS = [
    "browser_* tools are not available",
    "browser_* tools are unavailable",
    "documentation-only",
]


class BrowserControlSkillContractTests(unittest.TestCase):
    """Frontmatter, content, and image-mount contract for the skill."""

    def setUp(self) -> None:
        self.assertTrue(SKILL_MD.is_file(), f"missing skill file: {SKILL_MD}")
        self.text = SKILL_MD.read_text(encoding="utf-8")
        self.lower = self.text.lower()

    def test_skill_dir_has_only_skill_md(self) -> None:
        self.assertEqual(
            sorted(p.name for p in SKILL_DIR.iterdir()),
            ["SKILL.md"],
        )

    def test_frontmatter_fields_and_required_tools(self) -> None:
        if yaml is None:
            self.skipTest("PyYAML not available")
        data = parse_frontmatter(self.text)
        self.assertEqual(data.get("name"), "browser-control")
        self.assertEqual(data.get("version"), "1.0.0")
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

    def test_skill_not_baked_into_image(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertNotIn("COPY skills-factory/browser-control", dockerfile)
        self.assertNotIn("skills/browser-control/browser-control", dockerfile)
        # Other repo-owned skills are still baked in.
        for skill in ("gbrain", "aux-ml", "workspace-sync"):
            with self.subTest(skill=skill):
                self.assertIn(
                    f"COPY skills-factory/{skill} /opt/josemar/skills/{skill}",
                    dockerfile,
                )


class BrowserControlComposeMountTests(unittest.TestCase):
    """Skill is mounted only by the overlay, read-only, never by base/image."""

    def setUp(self) -> None:
        self.base = BASE_COMPOSE.read_text(encoding="utf-8")
        self.overlay = OVERLAY.read_text(encoding="utf-8")

    def test_base_and_image_exclude_skill(self) -> None:
        self.assertNotIn(SKILL_SOURCE, self.base)
        self.assertNotIn(SKILL_TARGET, self.base)

    def test_overlay_hermes_mounts_skill_read_only(self) -> None:
        block = service_block(self.overlay, "hermes")
        self.assertIn(f"{SKILL_SOURCE}:{SKILL_TARGET}:ro", block)
        # browser-tunnel sidecar must not mount the skill.
        tunnel = service_block(self.overlay, "browser-tunnel")
        self.assertNotIn(SKILL_SOURCE, tunnel)
        self.assertNotIn(SKILL_TARGET, tunnel)


class BrowserControlRenderedComposeTests(unittest.TestCase):
    """Rendered `docker compose config` proves base excludes and overlay :ro."""

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

    def test_rendered_base_excludes_and_overlay_includes_read_only(self) -> None:
        base = self._render(with_overlay=False)
        self.assertNotIn(SKILL_SOURCE, base)
        self.assertNotIn(SKILL_TARGET, base)

        overlay = self._render(with_overlay=True)
        self.assertIn("skills-factory/browser-control", overlay)
        self.assertIn(SKILL_TARGET, overlay)
        # `docker compose config` normalizes bind mounts to {source, target,
        # read_only}; assert read_only: true near the skill target.
        idx = overlay.find(f"target: {SKILL_TARGET}")
        self.assertGreater(idx, -1, "skill target not found in rendered overlay")
        self.assertIn("read_only: true", overlay[idx - 200 : idx + 200])
        self.assertNotIn(f"{SKILL_TARGET}:rw", overlay)


class BrowserControlDocsAccuracyTests(unittest.TestCase):
    """Docs must not claim built-in browser tools vanish when control is off."""

    def setUp(self) -> None:
        self.assertTrue(DOCS.is_file(), f"missing docs: {DOCS}")
        self.docs = DOCS.read_text(encoding="utf-8").lower()

    def test_docs_accurate_about_disabled_deploy(self) -> None:
        self.assertNotIn(
            "will not attempt browser actions", self.docs,
            "overlay gates this repo-owned skill/guidance, not built-in browser tools",
        )
        self.assertIn(
            "not registered", self.docs,
            "docs should state the repo-owned skill/guidance is not registered when off",
        )


if __name__ == "__main__":
    unittest.main()