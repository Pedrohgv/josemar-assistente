"""Opt-in provider-free gbrain core runtime conformance (issue #127 W2b).

Baseline + provenance + activation + provider-free retrieval/tagging. It
covers:

  - pre-start source-state seeding (real template ``.sync-manifest`` +
    canonical ``josemar`` schema pack into the disposable source-agent-state)
  - baseline Hermes-only build/start (no candidate ref, no sidecar profiles)
  - hermes-writable wait and safety checks: empty credentials and disabled
    owned gbrain/vault-recovery jobs
  - synthetic vault init committed as the hermes runtime user, plus a
    deterministic ``#conformance``-tagged note seeded before reindex
  - unconditional final cleanup (``down -v --remove-orphans``)
  - built-image provenance: the canonical ``GBRAIN_REF`` parsed from
    ``Dockerfile.hermes`` and the public/private wrapper paths
  - canonical runtime schema pack byte identity (seeding conformance)
  - ``josemar-gbrain reindex`` success envelope
  - ``gbrain status --json`` valid runtime/schema facts
  - provider-free retrieval/tagging: ``gbrain get`` markdown with the exact
    unique token, ``gbrain search`` TEXT output (never JSON) containing the
    expected slug/token, and ``gbrain tags`` returning the deterministic
    ``#conformance`` association
  - both link sources: the seeded ``[[projects/atlas]]`` wikilink (markdown
    source) and a public manual link, exposed through ``gbrain backlinks`` /
    ``gbrain graph`` and persistent through ``josemar-gbrain refresh``
  - public write contracts: positional capture create/read-back/idempotency,
    ``capture --stdin --slug --source --json`` (TaskNotes-relevant top-level
    ``written`` bool + full body preservation), ``capture --file``
    create/replacement of the same slug, ``put --content`` full-page
    replacement retaining the retained section, and the public
    ``put --stdin`` safety rejection
  - recovery-page lifecycle: create version A, update to B, ``gbrain history``
    discovers a stable revision handle, and ``gbrain revert`` using that
    runtime handle restores the exact A body and leaves the page writable
  - soft delete/restore lifecycle: ``gbrain delete`` hides the page and
    ``gbrain restore`` brings back the exact body
  - direct committed external edit of a fixture Markdown page as the hermes
    runtime user AFTER activation: the public ``get``/``search`` must NOT
    assume the edit before ``josemar-gbrain refresh`` (stale index), and
    after refresh the unique token is visible while an unrelated known page
    survives untouched
  - shared-lock contention: an independent hermes process holds the
    ``/opt/data/.locks/tasknotes.lock`` flock (flock only, no PGLite);
    ``josemar-gbrain refresh`` fails bounded with the ``refresh_lock_busy``
    envelope, and the next refresh succeeds after the holder releases
  - public boundary: the operator-only ``gbrain reindex`` is rejected by the
    public adapter (rc 2, allowlist message) without invoking the private
    native binary — proven by the lock-held rejection returning rc 2 (not
    the lock-busy 75 the native path would hit) and by the rejection
    holding with a free lock too
  - zero-LLM Chronicle smoke: ``timeline``/``day``/``day --week``/``since``/
    ``last-seen``/``on-this-day``/``orient``/``ontology`` all return valid
    results with clearly no synthetic events (empty arrays / explicit empty
    states / null last-seen)
  - a synthetic report persisted under ``dump_folder/gbrain-conformance``
    with command/result metadata only (never environment dumps)

Runtime execution is gated strictly on ``RUN_DOCKER_TESTS=1`` AND
``RUN_GBRAIN_CONFORMANCE=1`` and skips when the docker CLI is absent. Fast
host-side gate/structure tests in this module always run and need no Docker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest
from unittest import mock

from .gbrain_conformance_support import (
    CANONICAL_PACK_SOURCE,
    CONFORMANCE_EMPTY_ENV_KEYS,
    CommandEvidence,
    GbrainConformanceRuntime,
    conformance_report_dir,
    write_report,
)
from .helpers import REPO_ROOT, docker_available


# The pinned gbrain version the canonical GBRAIN_REF builds (docs/gbrain-
# operations.md "Pinned Values"). Asserted as the status --json runtime fact.
PINNED_GBRAIN_VERSION = "0.42.73.2"

# Fixed deployment wrapper paths (Dockerfile.hermes + issue #110 adapter).
PUBLIC_GBRAIN = "/usr/local/bin/gbrain"
OPERATOR_WRAPPER = "/usr/local/bin/josemar-gbrain"
PRIVATE_NATIVE = "/opt/josemar/libexec/gbrain-native"
LOCK_RUNNER = "/opt/josemar/scripts/tasknotes_lock_run.py"
WRAPPER_PATHS = (
    PUBLIC_GBRAIN,
    OPERATOR_WRAPPER,
    PRIVATE_NATIVE,
    LOCK_RUNNER,
)

# Deterministic provider-free retrieval/tagging facts (synthetic vault).
CONFORMANCE_TOKEN = "conformance-token-welcome"
TAGGED_NOTE_SLUG = "notes/conformance-tagged"
CONFORMANCE_TAG = "conformance"

# Deterministic link facts: the seeded wikilink target and the public manual
# link (page C -> projects/atlas).
WIKILINK_TARGET = "projects/atlas"
MANUAL_LINK_SOURCE = "inbox/page-c"
MANUAL_LINK_TYPE = "related"
MANUAL_LINK_CONTEXT = "conformance-manual-link-ctx-42"

# Deterministic public write facts (capture/put contracts).
POSITIONAL_CAPTURE_BODY = "remember to follow up on X"
POSITIONAL_CAPTURE_SLUG = "inbox/custom"
STDIN_CAPTURE_SLUG = "inbox/stdin-note"
STDIN_CAPTURE_BODY = (
    "stdin body line one conformance-stdin\n"
    "line two\n"
    "line three"
)
FILE_CAPTURE_PATH = "/tmp/conformance-file-note.md"
FILE_CAPTURE_SLUG = "inbox/file-note"
FILE_BODY_V1 = "# File note v1\n\nFile body v1 conformance-file-token.\n"
FILE_BODY_V2 = "# File note v2\n\nFile body v2 conformance-file-token-v2.\n"
PUT_SLUG = "inbox/sections"
PUT_CONTENT = (
    "# Sections\n"
    "\n"
    "## Retained Section\n"
    "\n"
    "This section must be retained.\n"
    "\n"
    "## New Section\n"
    "\n"
    "New content token conformance-new-section.\n"
)

# Deterministic recovery-page facts (history/revert lifecycle).
RECOVERY_SLUG = "inbox/recovery-page"
RECOVERY_BODY_A = (
    "# Recovery Page\n"
    "\n"
    "Version A body with unique token conformance-recovery-a.\n"
)
RECOVERY_BODY_B = (
    "# Recovery Page\n"
    "\n"
    "Version B body with unique token conformance-recovery-b.\n"
)
RECOVERY_BODY_C = (
    "# Recovery Page\n"
    "\n"
    "Version C body with unique token conformance-recovery-c.\n"
)

# Deterministic soft delete/restore facts (exact body lifecycle).
SOFT_DELETE_SLUG = "inbox/soft-delete-page"
SOFT_DELETE_BODY = (
    "# Soft Delete Page\n"
    "\n"
    "Exact body to survive delete/restore: conformance-soft-delete-body.\n"
)

# Deterministic external committed-edit facts (direct vault edit after
# activation, reconciled only by ``josemar-gbrain refresh``).
EXTERNAL_EDIT_TOKEN = "conformance-token-external-edit-v2"
EXTERNAL_EDIT_SLUG = "notes/welcome"

# Deterministic shared-lock contention facts (independent flock-only holder).
LOCK_PATH = "/opt/data/.locks/tasknotes.lock"
LOCK_HOLDER_PID = "/opt/data/.locks/conformance-holder.pid"
LOCK_HOLDER_READY = "/opt/data/.locks/conformance-holder.ready"

# Independent flock-only lock holder body, run as the hermes runtime user
# inside the container. It acquires the shared TaskNotes flock with NO
# PGLite/gbrain access (fcntl flock only), records its PID + a ready marker,
# then sleeps so the test can observe the busy lock and release it later.
# Deliberately contains no "gbrain"/"pglite" token (structural guard).
LOCK_HOLDER_SCRIPT = (
    "set -eu\n"
    "mkdir -p /opt/data/.locks\n"
    "rm -f " + LOCK_HOLDER_PID + " " + LOCK_HOLDER_READY + "\n"
    "python3 -c 'import fcntl, os, time\n"
    "fd = os.open(\"" + LOCK_PATH + "\", os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "open(\"" + LOCK_HOLDER_PID + "\", \"w\").write(str(os.getpid()))\n"
    "open(\"" + LOCK_HOLDER_READY + "\", \"w\").write(\"ready\")\n"
    "time.sleep(900)'\n"
)

# Deterministic zero-LLM Chronicle smoke facts (the synthetic vault has no
# chronicle events, so every read must come back empty / explicitly empty).
CHRONICLE_DAY = "2000-01-01"
CHRONICLE_ENTITY = "people/alice"
CHRONICLE_TIMELINE_SLUG = "notes/welcome"

# Conformance matrix: every operation this suite owns, with its
# classification. The report persists an explicit result for each.
CONFORMANCE_MATRIX = {
    "baseline_seed": "core",
    "baseline_build_start": "core",
    "baseline_writable": "core",
    "baseline_credentials": "core",
    "baseline_jobs": "core",
    "baseline_vault": "core",
    "provenance": "core",
    "pack_identity": "core",
    "reindex": "operator_only",
    "status": "core",
    "get": "core",
    "search": "core",
    "tags": "core",
    "backlinks": "core",
    "capture": "core",
    "link": "core",
    "graph": "core",
    "refresh": "operator_only",
    "put": "core",
    "put --stdin": "forbidden",
    "history": "core",
    "revert": "core",
    "delete": "core",
    "restore": "core",
    "external_edit_pre_refresh": "core",
    "external_edit_post_refresh": "core",
    "refresh_lock_busy": "core",
    "public_reindex_rejected": "core",
    "chronicle_timeline": "chronicle_read",
    "chronicle_day": "chronicle_read",
    "chronicle_day_week": "chronicle_read",
    "chronicle_since": "chronicle_read",
    "chronicle_last_seen": "chronicle_read",
    "chronicle_on_this_day": "chronicle_read",
    "chronicle_orient": "chronicle_read",
    "chronicle_ontology": "chronicle_read",
}


def _conformance_enabled() -> bool:
    """Strict gate: RUN_DOCKER_TESTS=1 AND RUN_GBRAIN_CONFORMANCE=1 AND a
    docker CLI is available."""
    return (
        os.getenv("RUN_DOCKER_TESTS") == "1"
        and os.getenv("RUN_GBRAIN_CONFORMANCE") == "1"
        and docker_available()
    )


@unittest.skipUnless(
    _conformance_enabled(),
    "set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_CONFORMANCE=1 with a docker CLI",
)
class GbrainConformanceTestCase(unittest.TestCase):
    """Shared base setup for the gbrain conformance runtime suite.

    Builds/starts the baseline Hermes-only runtime against a disposable
    Compose project, seeds the real template source state BEFORE start, waits
    for the hermes-writable surface, asserts the isolation safety contract
    (empty credentials, disabled owned jobs), initializes the synthetic vault
    as the hermes runtime user, and unconditionally tears the project down
    with ``down -v --remove-orphans``.
    """

    def setUp(self) -> None:
        self._evidence: list[CommandEvidence] = []
        self._matrix: dict[str, str] = {
            op: "pass" if op.startswith("baseline_") else "not_run"
            for op in CONFORMANCE_MATRIX
        }
        self._gbrain_version: str | None = None
        self._report_path: Path | None = None

        self.runtime = GbrainConformanceRuntime()
        # Pre-start source state seeding: real template .sync-manifest +
        # canonical josemar schema pack into the disposable source-agent-state.
        self.runtime.seed_source_state()
        # Baseline Hermes-only build/start (no candidate ref, no sidecars).
        self.runtime.up("hermes", timeout=900)
        # Wait for the exact hermes-writable surface before any exec probe.
        self.runtime.wait_until_hermes_writable(timeout=120)
        # Safety checks: empty credentials + disabled owned jobs.
        self._evidence.append(self._assert_no_credentials())
        self._evidence.append(self.runtime.assert_owned_jobs_disabled())
        # Synthetic vault init committed as the hermes runtime user.
        self._evidence.append(self.runtime.init_synthetic_vault())
        # Deterministic tagged note seeded before reindex (safe hermes command).
        self._evidence.append(self._seed_tagged_note())
        # Deterministic wikilink seeded into the welcome fixture page.
        self._evidence.append(self._seed_welcome_wikilink())

    def tearDown(self) -> None:
        # Unconditional final cleanup: down -v --remove-orphans.
        self.runtime.cleanup()

    # --- safety helpers ---------------------------------------------------

    def _seed_tagged_note(self) -> CommandEvidence:
        """Write a deterministic note carrying the ``#conformance`` tag into
        the vault as the hermes runtime user and commit it, so the later
        reindex indexes the tag association."""
        script = (
            "set -eu\n"
            "cd /opt/data/obsidian\n"
            "cat > notes/conformance-tagged.md <<'MD'\n"
            "---\n"
            "tags: [conformance]\n"
            "---\n"
            "\n"
            "# Conformance Tagged Note\n"
            "\n"
            "A deterministic note tagged #conformance.\n"
            "MD\n"
            "git add notes/conformance-tagged.md\n"
            "git commit -qm 'seed conformance tagged note'\n"
        )
        return self.runtime.run_as_hermes("sh", "-lc", script)

    def _seed_welcome_wikilink(self) -> CommandEvidence:
        """Rewrite the welcome fixture page so it carries the deterministic
        ``[[projects/atlas]]`` wikilink (preserving the unique search token),
        committed as the hermes runtime user before reindex."""
        script = (
            "set -eu\n"
            "cd /opt/data/obsidian\n"
            "cat > notes/welcome.md <<'MD'\n"
            "# Welcome\n"
            "\n"
            "Deterministic conformance note with unique search token: "
            "conformance-token-welcome.\n"
            "\n"
            "Links to [[projects/atlas]].\n"
            "MD\n"
            "git add notes/welcome.md\n"
            "git commit -qm 'seed welcome wikilink'\n"
        )
        return self.runtime.run_as_hermes("sh", "-lc", script)

    def _assert_no_credentials(self) -> CommandEvidence:
        """Assert every conformance-blanked credential env key is empty inside
        the running container (defense in depth on top of the host-side
        sanitizer)."""
        script = (
            "set -eu\n"
            "for k in " + " ".join(CONFORMANCE_EMPTY_ENV_KEYS) + "; do\n"
            "  v=$(printenv \"$k\" 2>/dev/null || true)\n"
            "  if [ -n \"$v\" ]; then\n"
            "    echo \"credential env key $k is non-empty\" >&2\n"
            "    exit 1\n"
            "  fi\n"
            "done\n"
            "echo no-credentials-present\n"
        )
        ev = self.runtime.run_as_hermes("sh", "-lc", script)
        self.assertIn("no-credentials-present", ev.stdout, ev.stderr)
        return ev

    # --- report -----------------------------------------------------------

    def _write_report(self) -> None:
        """Persist the synthetic conformance report under
        ``dump_folder/gbrain-conformance``. Contains command/result metadata
        only (argv, rc, stdout, stderr, elapsed) plus the explicit matrix —
        never the process or runtime environment."""
        metadata = {
            "baseline_ref": self.runtime.baseline_gbrain_ref(),
            "gbrain_version": self._gbrain_version,
            "matrix": self._matrix,
        }
        self._report_path = write_report(
            conformance_report_dir(),
            "gbrain-conformance",
            self._evidence,
            metadata=metadata,
        )


class GbrainConformanceRuntimeTests(GbrainConformanceTestCase):
    """W2b provider-free runtime scenarios (Docker-gated via the base class)."""

    def test_provider_free_core_runtime_conformance(self) -> None:
        try:
            self._scenario_provenance()
            self._scenario_pack_identity()
            self._scenario_reindex()
            self._scenario_status()
            self._scenario_get_search_tags()
            self._scenario_links_backlinks_graph()
            self._scenario_public_write_contracts()
            self._scenario_recovery_history_revert()
            self._scenario_soft_delete_restore()
            self._scenario_external_edit_refresh()
            self._scenario_lock_contention()
            self._scenario_public_boundary()
            self._scenario_chronicle_zero_llm()
        finally:
            self._write_report()

    def _scenario_provenance(self) -> None:
        """Built-image provenance: the canonical baseline ref and the
        public/private wrapper paths."""
        self._matrix["provenance"] = "fail"
        baseline_ref = self.runtime.baseline_gbrain_ref()
        self.assertRegex(baseline_ref, r"^[0-9a-f]{40}$")
        for path in WRAPPER_PATHS:
            ev = self.runtime.run_as_hermes("test", "-x", path)
            self.assertEqual(ev.returncode, 0, f"missing executable: {path}")
            self._evidence.append(ev)
        self._matrix["provenance"] = "pass"

    def _scenario_pack_identity(self) -> None:
        """The runtime canonical schema pack must be byte-identical to the
        template pack seeded through the disposable source-agent-state."""
        self._matrix["pack_identity"] = "fail"
        canonical = CANONICAL_PACK_SOURCE.read_bytes()
        ev = self.runtime.run_as_hermes(
            "cat", "/opt/data/.gbrain/schema-packs/josemar/pack.yaml"
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertEqual(ev.stdout.encode("utf-8"), canonical)
        self._evidence.append(ev)
        self._matrix["pack_identity"] = "pass"

    def _scenario_reindex(self) -> None:
        """Operator activation returns the success JSON envelope, running
        directly against the byte-identical template-seeded canonical pack
        (no runtime pack replacement). The active schema marker — the runtime
        source of truth the public adapter reads — must identify josemar."""
        self._matrix["reindex"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        envelope = json.loads(ev.stdout)
        self.assertIs(envelope.get("success"), True)
        self.assertEqual(envelope.get("action"), "reindex")
        self.assertEqual(envelope.get("schema_pack"), "josemar")
        # The active schema marker (runtime source of truth for the adapter)
        # must identify the josemar pack.
        marker = self.runtime.run_as_hermes(
            "cat", "/opt/data/.gbrain/active-schema-pack"
        )
        self.assertEqual(marker.returncode, 0, marker.stderr)
        self.assertEqual(marker.stdout.strip(), "josemar")
        self._evidence.append(marker)
        self._matrix["reindex"] = "pass"

    def _scenario_status(self) -> None:
        """``gbrain status --json`` reports valid runtime/schema facts: local
        mode, the pinned gbrain version, schema_version 1, and the fixed
        vault path."""
        self._matrix["status"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "status", "--json", timeout=120)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        data = json.loads(ev.stdout)
        self.assertEqual(data.get("mode"), "local")
        self.assertEqual(data.get("schema_version"), 1)
        self.assertEqual(data.get("version"), PINNED_GBRAIN_VERSION)
        sources = data.get("sync", {}).get("sources", [])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].get("local_path"), "/opt/data/obsidian")
        self._gbrain_version = data.get("version")
        self._matrix["status"] = "pass"

    def _scenario_get_search_tags(self) -> None:
        """Provider-free retrieval/tagging: ``get`` returns the exact markdown
        token, ``search`` returns TEXT (never JSON) containing the expected
        slug/token, and ``tags`` returns the deterministic ``#conformance``
        association."""
        # get: markdown with the exact unique token.
        self._matrix["get"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "get", "notes/welcome")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn(CONFORMANCE_TOKEN, ev.stdout)
        self.assertIn("# Welcome", ev.stdout)
        self._evidence.append(ev)
        self._matrix["get"] = "pass"

        # search: TEXT output (the pinned CLI renders search as text lines,
        # never JSON) containing the expected slug and token.
        self._matrix["search"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "search", CONFORMANCE_TOKEN, "--limit", "5"
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("notes/welcome", ev.stdout)
        self.assertIn(CONFORMANCE_TOKEN, ev.stdout)
        self.assertNotIn('"slug"', ev.stdout)
        self._evidence.append(ev)
        self._matrix["search"] = "pass"

        # tags: the deterministic #conformance association.
        self._matrix["tags"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "tags", TAGGED_NOTE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn(CONFORMANCE_TAG, ev.stdout)
        self._evidence.append(ev)
        self._matrix["tags"] = "pass"

    @staticmethod
    def _find_backlink(backlinks: list[dict], from_slug: str) -> dict | None:
        """Find the backlink edge originating from ``from_slug`` (semantic
        lookup, never a full-snapshot assertion)."""
        for edge in backlinks:
            if edge.get("from_slug") == from_slug:
                return edge
        return None

    def _require_backlink(self, backlinks: list[dict], from_slug: str) -> dict:
        """Find the backlink edge from ``from_slug`` or fail the test."""
        edge = self._find_backlink(backlinks, from_slug)
        self.assertIsNotNone(edge, f"missing backlink from {from_slug}")
        assert edge is not None  # narrow for static checkers
        return edge

    def _scenario_links_backlinks_graph(self) -> None:
        """Both link sources: the seeded ``[[projects/atlas]]`` wikilink
        (markdown source) and a public manual link, exposed through
        backlinks/graph and persistent through refresh/reconciliation."""
        # backlinks: the seeded wikilink edge from notes/welcome.
        self._matrix["backlinks"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "backlinks", WIKILINK_TARGET)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        welcome_edge = self._require_backlink(json.loads(ev.stdout), "notes/welcome")
        self.assertEqual(welcome_edge["link_source"], "markdown")
        self.assertEqual(welcome_edge["link_type"], "mentions")
        self._matrix["backlinks"] = "pass"

        # capture: create page C via the public capture command.
        self._matrix["capture"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "capture", "Page C body with token conformance-page-c",
            "--slug", MANUAL_LINK_SOURCE, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        capture = json.loads(ev.stdout)
        self.assertIs(capture.get("written"), True)
        self.assertEqual(capture.get("slug"), MANUAL_LINK_SOURCE)
        self._matrix["capture"] = "pass"

        # link: public manual link C -> projects/atlas.
        self._matrix["link"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "link", MANUAL_LINK_SOURCE, WIKILINK_TARGET,
            "--link-type", MANUAL_LINK_TYPE,
            "--context", MANUAL_LINK_CONTEXT,
            "--link-source", "manual",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        link = json.loads(ev.stdout)
        self.assertEqual(link.get("status"), "ok")
        self._matrix["link"] = "pass"

        # backlinks now semantically includes the manual relation.
        self._matrix["backlinks"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "backlinks", WIKILINK_TARGET)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        backlinks = json.loads(ev.stdout)
        manual_edge = self._require_backlink(backlinks, MANUAL_LINK_SOURCE)
        self.assertEqual(manual_edge["link_source"], "manual")
        self.assertEqual(manual_edge["link_type"], MANUAL_LINK_TYPE)
        self.assertEqual(manual_edge["context"], MANUAL_LINK_CONTEXT)
        self._matrix["backlinks"] = "pass"

        # graph: stable nodes/edge semantics for both link sources.
        self._matrix["graph"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "graph", WIKILINK_TARGET)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        graph = json.loads(ev.stdout)
        slugs = {node.get("slug") for node in graph}
        self.assertIn(WIKILINK_TARGET, slugs)
        self.assertIn("notes/welcome", slugs)
        atlas_node = next(
            node for node in graph if node.get("slug") == WIKILINK_TARGET
        )
        self.assertIn(
            {"to_slug": "notes/welcome", "link_type": "mentions"},
            atlas_node.get("links", []),
        )
        ev = self.runtime.run_as_hermes("gbrain", "graph", MANUAL_LINK_SOURCE)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        graph = json.loads(ev.stdout)
        slugs = {node.get("slug") for node in graph}
        self.assertIn(MANUAL_LINK_SOURCE, slugs)
        self.assertIn(WIKILINK_TARGET, slugs)
        page_c_node = next(
            node for node in graph if node.get("slug") == MANUAL_LINK_SOURCE
        )
        self.assertIn(
            {"to_slug": WIKILINK_TARGET, "link_type": MANUAL_LINK_TYPE},
            page_c_node.get("links", []),
        )
        self._matrix["graph"] = "pass"

        # refresh/reconciliation: the manual edge must still exist.
        self._matrix["refresh"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        refresh = json.loads(ev.stdout)
        self.assertIs(refresh.get("success"), True)
        self.assertEqual(refresh.get("action"), "refresh")
        self._matrix["refresh"] = "pass"

        self._matrix["backlinks"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "backlinks", WIKILINK_TARGET)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        manual_edge = self._require_backlink(json.loads(ev.stdout), MANUAL_LINK_SOURCE)
        self.assertEqual(manual_edge["link_source"], "manual")
        self.assertEqual(manual_edge["link_type"], MANUAL_LINK_TYPE)
        self._matrix["backlinks"] = "pass"

    def _scenario_public_write_contracts(self) -> None:
        """Public write contracts: positional capture create/read-back/
        idempotency, capture --stdin --slug --source --json (TaskNotes-
        relevant top-level ``written`` bool + full body preservation),
        capture --file create then same-slug replacement with the exact full
        body, put --content full-page replacement retaining the retained
        section, and the public ``put --stdin`` safety rejection."""
        # 1. Positional capture: create, read-back, idempotent re-capture.
        self._matrix["capture"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "capture", POSITIONAL_CAPTURE_BODY,
            "--slug", POSITIONAL_CAPTURE_SLUG, "--type", "note", "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        created = json.loads(ev.stdout)
        self.assertIs(created.get("written"), True)
        self.assertEqual(created.get("slug"), POSITIONAL_CAPTURE_SLUG)
        self.assertEqual(created.get("status"), "created_or_updated")
        content_hash = created.get("content_hash")

        ev = self.runtime.run_as_hermes("gbrain", "get", POSITIONAL_CAPTURE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn(POSITIONAL_CAPTURE_BODY, ev.stdout)
        self._evidence.append(ev)

        ev = self.runtime.run_as_hermes(
            "gbrain", "capture", POSITIONAL_CAPTURE_BODY,
            "--slug", POSITIONAL_CAPTURE_SLUG, "--type", "note", "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        again = json.loads(ev.stdout)
        self.assertEqual(again.get("status"), "skipped")
        self.assertEqual(again.get("content_hash"), content_hash)
        self.assertIs(again.get("written"), True)
        self._matrix["capture"] = "pass"

        # 2. capture --stdin --slug --source --json: top-level written bool
        # and full body preservation (TaskNotes-relevant contract).
        self._matrix["capture"] = "fail"
        script = (
            "printf '%s' '" + STDIN_CAPTURE_BODY + "' | "
            "gbrain capture --stdin --slug " + STDIN_CAPTURE_SLUG +
            " --source default --json\n"
        )
        ev = self.runtime.run_as_hermes("sh", "-lc", script)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        stdin_result = json.loads(ev.stdout)
        self.assertIs(stdin_result.get("written"), True)
        self.assertEqual(stdin_result.get("slug"), STDIN_CAPTURE_SLUG)
        ev = self.runtime.run_as_hermes("gbrain", "get", STDIN_CAPTURE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        for line in STDIN_CAPTURE_BODY.split("\n"):
            self.assertIn(line, ev.stdout)
        self._evidence.append(ev)
        self._matrix["capture"] = "pass"

        # 3. capture --file: create, then replace the same slug with the
        # exact full body (hermes-owned disposable runtime file).
        self._matrix["capture"] = "fail"
        ev = self.runtime.run_as_hermes(
            "sh", "-lc",
            "cat > " + FILE_CAPTURE_PATH + " <<'MD'\n" + FILE_BODY_V1 + "MD\n"
            "gbrain capture --file " + FILE_CAPTURE_PATH + " --slug "
            + FILE_CAPTURE_SLUG + " --json\n",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        file_created = json.loads(ev.stdout)
        self.assertIs(file_created.get("written"), True)
        self.assertEqual(file_created.get("slug"), FILE_CAPTURE_SLUG)
        ev = self.runtime.run_as_hermes("gbrain", "get", FILE_CAPTURE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("File body v1 conformance-file-token.", ev.stdout)
        self._evidence.append(ev)

        ev = self.runtime.run_as_hermes(
            "sh", "-lc",
            "cat > " + FILE_CAPTURE_PATH + " <<'MD'\n" + FILE_BODY_V2 + "MD\n"
            "gbrain capture --file " + FILE_CAPTURE_PATH + " --slug "
            + FILE_CAPTURE_SLUG + " --json\n",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        file_replaced = json.loads(ev.stdout)
        self.assertIs(file_replaced.get("written"), True)
        self.assertEqual(file_replaced.get("slug"), FILE_CAPTURE_SLUG)
        ev = self.runtime.run_as_hermes("gbrain", "get", FILE_CAPTURE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("File body v2 conformance-file-token-v2.", ev.stdout)
        self.assertNotIn("File body v1 conformance-file-token.", ev.stdout)
        self._evidence.append(ev)
        self._matrix["capture"] = "pass"

        # 4. put --content: full-page replacement retaining the retained
        # section.
        self._matrix["put"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "put", PUT_SLUG, "--content", PUT_CONTENT,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        put_result = json.loads(ev.stdout)
        self.assertIs(put_result.get("write_through", {}).get("written"), True)
        ev = self.runtime.run_as_hermes("gbrain", "get", PUT_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("## Retained Section", ev.stdout)
        self.assertIn("This section must be retained.", ev.stdout)
        self.assertIn("## New Section", ev.stdout)
        self._evidence.append(ev)
        self._matrix["put"] = "pass"

        # 5. Safety: public put --stdin is rejected by the adapter.
        self._matrix["put --stdin"] = "fail"
        ev = self.runtime.run_as_hermes(
            "sh", "-lc", "printf 'evil body' | gbrain put inbox/evil --stdin",
            check=False,
        )
        self.assertEqual(ev.returncode, 2)
        self.assertIn("not on the agent-facing allowlist", ev.stderr)
        self._evidence.append(ev)
        self._matrix["put --stdin"] = "pass"

    @staticmethod
    def _extract_revision_handle(history_output: str) -> str:
        """Extract a stable revision handle from ``gbrain history`` output.

        The pinned gbrain CLI renders history entries as ``#N`` revision
        handles (e.g. ``#2  2026-08-21T10:19:28  # Recovery Page``), but
        ``gbrain revert`` takes the PLAIN integer version id (``1``), not the
        ``#1`` display form (the ``#1`` form fails with
        ``invalid input syntax for type integer``). We scan for the first
        ``#N`` token and return the plain ``N``.
        """
        match = re.search(r"#(\d+)", history_output)
        if not match:
            raise AssertionError(
                "no stable revision handle (#N) found in gbrain history output"
            )
        return match.group(1)

    def _scenario_recovery_history_revert(self) -> None:
        """Recovery-page lifecycle: create version A, update to B, discover a
        stable revision handle via ``gbrain history``, and ``gbrain revert``
        using that runtime handle restores the exact A body and leaves the
        page writable."""
        # Create version A.
        self._matrix["capture"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "capture", RECOVERY_BODY_A,
            "--slug", RECOVERY_SLUG, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        created = json.loads(ev.stdout)
        self.assertIs(created.get("written"), True)
        self.assertEqual(created.get("slug"), RECOVERY_SLUG)
        self._matrix["capture"] = "pass"

        # Update to version B.
        self._matrix["put"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "put", RECOVERY_SLUG, "--content", RECOVERY_BODY_B,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        put_result = json.loads(ev.stdout)
        self.assertIs(put_result.get("write_through", {}).get("written"), True)
        ev = self.runtime.run_as_hermes("gbrain", "get", RECOVERY_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("conformance-recovery-b", ev.stdout)
        self._evidence.append(ev)
        self._matrix["put"] = "pass"

        # Discover a stable revision handle from history.
        self._matrix["history"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "history", RECOVERY_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        revision = self._extract_revision_handle(ev.stdout)
        self._matrix["history"] = "pass"

        # Revert to the discovered handle restores the exact A body.
        self._matrix["revert"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "revert", RECOVERY_SLUG, revision,
            check=False,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        ev = self.runtime.run_as_hermes("gbrain", "get", RECOVERY_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("conformance-recovery-a", ev.stdout)
        self.assertNotIn("conformance-recovery-b", ev.stdout)
        self._evidence.append(ev)
        self._matrix["revert"] = "pass"

        # The page remains writable after revert: a NEW write (C) must be a
        # real created_or_updated write (re-writing B would be skipped by the
        # idempotency check against the last-written hash).
        self._matrix["put"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "put", RECOVERY_SLUG, "--content", RECOVERY_BODY_C,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        put_result = json.loads(ev.stdout)
        self.assertIs(put_result.get("write_through", {}).get("written"), True)
        self.assertEqual(put_result.get("status"), "created_or_updated")
        self._matrix["put"] = "pass"

    def _scenario_soft_delete_restore(self) -> None:
        """Soft delete/restore lifecycle: ``gbrain delete`` hides the page and
        ``gbrain restore`` brings back the exact body."""
        # Create the page with the exact body.
        self._matrix["capture"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "capture", SOFT_DELETE_BODY,
            "--slug", SOFT_DELETE_SLUG, "--json",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        created = json.loads(ev.stdout)
        self.assertIs(created.get("written"), True)
        self.assertEqual(created.get("slug"), SOFT_DELETE_SLUG)
        self._matrix["capture"] = "pass"

        # Soft delete hides the page.
        self._matrix["delete"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "delete", SOFT_DELETE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        deleted = json.loads(ev.stdout)
        self.assertEqual(deleted.get("status"), "soft_deleted")
        self.assertEqual(deleted.get("slug"), SOFT_DELETE_SLUG)
        # The page is hidden after delete.
        ev = self.runtime.run_as_hermes("gbrain", "get", SOFT_DELETE_SLUG, check=False)
        self.assertEqual(ev.returncode, 1)
        self.assertIn("page_not_found", ev.stderr)
        self._evidence.append(ev)
        self._matrix["delete"] = "pass"

        # Restore brings back the exact body.
        self._matrix["restore"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "restore", SOFT_DELETE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        restored = json.loads(ev.stdout)
        self.assertEqual(restored.get("status"), "restored")
        self.assertEqual(restored.get("slug"), SOFT_DELETE_SLUG)
        ev = self.runtime.run_as_hermes("gbrain", "get", SOFT_DELETE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("conformance-soft-delete-body", ev.stdout)
        self._evidence.append(ev)
        self._matrix["restore"] = "pass"

    def _scenario_external_edit_refresh(self) -> None:
        """Direct committed external edit of a fixture Markdown page as the
        hermes runtime user AFTER activation: the public get/search must NOT
        assume the edit before ``josemar-gbrain refresh`` (stale index), and
        after refresh the unique token is visible while an unrelated known
        page survives untouched."""
        # 1. Direct committed edit of the welcome fixture page (as hermes).
        script = (
            "set -eu\n"
            "cd /opt/data/obsidian\n"
            "cat >> notes/welcome.md <<'MD'\n"
            "\n"
            "External committed edit with unique token: "
            + EXTERNAL_EDIT_TOKEN + ".\n"
            "MD\n"
            "git add notes/welcome.md\n"
            "git commit -qm 'external committed edit (conformance)'\n"
        )
        ev = self.runtime.run_as_hermes("sh", "-lc", script)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)

        # 2. Pre-refresh: the public get/search must NOT assume the edit.
        self._matrix["external_edit_pre_refresh"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "get", EXTERNAL_EDIT_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertNotIn(EXTERNAL_EDIT_TOKEN, ev.stdout)
        self._evidence.append(ev)
        ev = self.runtime.run_as_hermes(
            "gbrain", "search", EXTERNAL_EDIT_TOKEN, "--limit", "5",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertNotIn("notes/welcome", ev.stdout)
        self._evidence.append(ev)
        self._matrix["external_edit_pre_refresh"] = "pass"

        # 3. Operator refresh reconciles the committed edit.
        self._matrix["refresh"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        refresh = json.loads(ev.stdout)
        self.assertIs(refresh.get("success"), True)
        self.assertEqual(refresh.get("action"), "refresh")
        self._matrix["refresh"] = "pass"

        # 4. Post-refresh: the unique token is now visible.
        self._matrix["external_edit_post_refresh"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "get", EXTERNAL_EDIT_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn(EXTERNAL_EDIT_TOKEN, ev.stdout)
        self._evidence.append(ev)
        ev = self.runtime.run_as_hermes(
            "gbrain", "search", EXTERNAL_EDIT_TOKEN, "--limit", "5",
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("notes/welcome", ev.stdout)
        self._evidence.append(ev)
        self._matrix["external_edit_post_refresh"] = "pass"

        # 5. An unrelated known page survives the refresh untouched.
        ev = self.runtime.run_as_hermes("gbrain", "get", TAGGED_NOTE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("A deterministic note tagged #conformance.", ev.stdout)
        self._evidence.append(ev)
        ev = self.runtime.run_as_hermes("gbrain", "tags", TAGGED_NOTE_SLUG)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn(CONFORMANCE_TAG, ev.stdout)
        self._evidence.append(ev)

    def _start_lock_holder(self) -> None:
        """Start an independent hermes process that holds the shared TaskNotes
        flock (flock only, no PGLite) and writes a ready marker + its PID,
        then wait until the lock is actually held."""
        cid = self.runtime.run("ps", "-q", "hermes").stdout.strip()
        self.assertTrue(cid, "hermes container id must resolve")
        proc = subprocess.run(
            [
                "docker", "exec", "-d", cid,
                "su", "-s", "/bin/sh", "hermes", "-c", LOCK_HOLDER_SCRIPT,
            ],
            capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = self.runtime.run_as_hermes(
                "test", "-f", LOCK_HOLDER_READY, check=False, timeout=30,
            )
            if probe.returncode == 0:
                return
            time.sleep(0.5)
        raise AssertionError("lock holder did not acquire the flock in time")

    def _stop_lock_holder(self) -> None:
        """Kill the independent lock holder (releasing the flock) and wait
        until the process is gone."""
        script = (
            "set -eu\n"
            "pid=$(cat " + LOCK_HOLDER_PID + " 2>/dev/null || true)\n"
            "if [ -n \"$pid\" ]; then kill \"$pid\" 2>/dev/null || true; fi\n"
        )
        self.runtime.run_as_hermes("sh", "-lc", script, check=False, timeout=30)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = self.runtime.run_as_hermes(
                "sh", "-lc",
                "pid=$(cat " + LOCK_HOLDER_PID + " 2>/dev/null || true); "
                "if [ -z \"$pid\" ]; then exit 0; fi; "
                "if ! kill -0 \"$pid\" 2>/dev/null; then exit 0; fi; exit 1",
                check=False, timeout=30,
            )
            if probe.returncode == 0:
                return
            time.sleep(0.5)
        raise AssertionError("lock holder did not release the flock in time")

    def _scenario_lock_contention(self) -> None:
        """Shared-lock contention: an independent hermes process holds the
        TaskNotes/gbrain flock (flock only, no PGLite); ``josemar-gbrain
        refresh`` must fail bounded with the lock-busy envelope; after the
        holder releases, the next refresh succeeds."""
        self._start_lock_holder()
        try:
            self._matrix["refresh_lock_busy"] = "fail"
            ev = self.runtime.run_as_hermes(
                "josemar-gbrain", "refresh", check=False, timeout=120,
            )
            self.assertNotEqual(ev.returncode, 0, ev.stdout + ev.stderr)
            self.assertIn("refresh_lock_busy", ev.stdout)
            self._evidence.append(ev)
            self._matrix["refresh_lock_busy"] = "pass"
        finally:
            self._stop_lock_holder()

        self._matrix["refresh"] = "fail"
        ev = self.runtime.run_as_hermes("josemar-gbrain", "refresh", timeout=300)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        refresh = json.loads(ev.stdout)
        self.assertIs(refresh.get("success"), True)
        self.assertEqual(refresh.get("action"), "refresh")
        self._matrix["refresh"] = "pass"

    def _scenario_public_boundary(self) -> None:
        """Public boundary: the operator-only ``gbrain reindex`` must be
        rejected by the public adapter (rc 2, allowlist message) WITHOUT
        invoking the private native binary. Proof: while an independent
        process holds the shared lock, the public rejection still returns
        rc 2 (allowlist) rather than 75 (lock busy) — the native path, which
        would need the lock, is never reached. The rejection is also
        lock-independent: it holds with a free lock too."""
        self._start_lock_holder()
        try:
            self._matrix["public_reindex_rejected"] = "fail"
            ev = self.runtime.run_as_hermes(
                "gbrain", "reindex", check=False, timeout=60,
            )
            self.assertEqual(ev.returncode, 2, ev.stdout + ev.stderr)
            self.assertIn("not on the agent-facing allowlist", ev.stderr)
            self.assertIn(
                "operator-only maintenance runs through josemar-gbrain",
                ev.stderr,
            )
            self.assertNotIn('"success"', ev.stdout)
            self._evidence.append(ev)
            self._matrix["public_reindex_rejected"] = "pass"
        finally:
            self._stop_lock_holder()

        # Lock-independent: still rejected with a free lock.
        self._matrix["public_reindex_rejected"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "reindex", check=False, timeout=60,
        )
        self.assertEqual(ev.returncode, 2, ev.stdout + ev.stderr)
        self.assertIn("not on the agent-facing allowlist", ev.stderr)
        self._evidence.append(ev)
        self._matrix["public_reindex_rejected"] = "pass"

    def _scenario_chronicle_zero_llm(self) -> None:
        """Zero-LLM Chronicle smoke: every chronicle read command returns a
        VALID result with clearly no synthetic events (the synthetic vault
        has no chronicle events, so the outputs must be empty / explicitly
        empty)."""
        # timeline: the per-page timeline renderer prints an explicit empty
        # state, never a fabricated event.
        self._matrix["chronicle_timeline"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "timeline", CHRONICLE_TIMELINE_SLUG,
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self.assertIn("No timeline entries", ev.stdout)
        self._evidence.append(ev)
        self._matrix["chronicle_timeline"] = "pass"

        # day / day --week / since / on-this-day / ontology: empty JSON
        # arrays (no synthetic events).
        for key, command in (
            ("chronicle_day", ("gbrain", "day", CHRONICLE_DAY)),
            ("chronicle_day_week", ("gbrain", "day", CHRONICLE_DAY, "--week")),
            ("chronicle_since", ("gbrain", "since", CHRONICLE_DAY)),
            ("chronicle_on_this_day", ("gbrain", "on-this-day")),
            ("chronicle_ontology", ("gbrain", "ontology", CHRONICLE_ENTITY)),
        ):
            self._matrix[key] = "fail"
            ev = self.runtime.run_as_hermes(*command)
            self.assertEqual(ev.returncode, 0, ev.stderr)
            self.assertEqual(
                json.loads(ev.stdout), [], f"{command} must be empty"
            )
            self._evidence.append(ev)
            self._matrix[key] = "pass"

        # last-seen: no last-seen record (nulls), never a synthetic event.
        self._matrix["chronicle_last_seen"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "last-seen", CHRONICLE_ENTITY)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        last_seen = json.loads(ev.stdout)
        self.assertIsNone(last_seen.get("last_date"))
        self.assertIsNone(last_seen.get("last_event_slug"))
        self._matrix["chronicle_last_seen"] = "pass"

        # orient: the recent timeline must be empty (no synthetic events).
        self._matrix["chronicle_orient"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "orient")
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        orient = json.loads(ev.stdout)
        self.assertEqual(orient.get("recent_timeline"), [])
        self._matrix["chronicle_orient"] = "pass"


class GbrainConformanceGateStructureTests(unittest.TestCase):
    """Fast host-side guards for the conformance gate and module structure.
    No Docker required; these run in the normal fast suite."""

    @staticmethod
    def _module_text() -> str:
        return (REPO_ROOT / "tests" / "runtime" / "test_gbrain_conformance.py").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _docker_available_patch(available: bool):
        """Patch this module's own ``docker_available`` reference.

        Patching the module attribute directly (rather than a dotted import
        path) is robust against double-import under ``discover -s tests``,
        where the module can be imported as both ``runtime.…`` and
        ``tests.runtime.…`` and a dotted target would patch the wrong copy.
        """
        return mock.patch.object(
            sys.modules[__name__], "docker_available", return_value=available
        )

    def test_gate_requires_run_docker_tests(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "", "RUN_GBRAIN_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_conformance_enabled())

    def test_gate_requires_run_gbrain_conformance(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_CONFORMANCE": ""}
        ):
            with self._docker_available_patch(True):
                self.assertFalse(_conformance_enabled())

    def test_gate_requires_docker(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(False):
                self.assertFalse(_conformance_enabled())

    def test_gate_enabled_when_all_conditions_met(self) -> None:
        with mock.patch.dict(
            os.environ, {"RUN_DOCKER_TESTS": "1", "RUN_GBRAIN_CONFORMANCE": "1"}
        ):
            with self._docker_available_patch(True):
                self.assertTrue(_conformance_enabled())

    def test_runtime_class_is_gated_on_both_env_vars(self) -> None:
        text = self._module_text()
        self.assertIn("RUN_DOCKER_TESTS", text)
        self.assertIn("RUN_GBRAIN_CONFORMANCE", text)
        self.assertIn("skipUnless", text)
        self.assertIn('"set RUN_DOCKER_TESTS=1 and RUN_GBRAIN_CONFORMANCE=1 with a docker CLI"', text)

    def test_shared_base_class_defined(self) -> None:
        self.assertTrue(issubclass(GbrainConformanceTestCase, unittest.TestCase))
        self.assertTrue(issubclass(GbrainConformanceRuntimeTests, GbrainConformanceTestCase))

    def test_base_setup_uses_gbrain_conformance_runtime(self) -> None:
        text = self._module_text()
        self.assertIn("GbrainConformanceRuntime()", text)
        self.assertIn("self.runtime.seed_source_state()", text)
        self.assertIn('self.runtime.up("hermes", timeout=900)', text)
        self.assertIn("self.runtime.wait_until_hermes_writable(timeout=120)", text)
        self.assertIn("self.runtime.assert_owned_jobs_disabled()", text)
        self.assertIn("self.runtime.init_synthetic_vault()", text)
        self.assertIn("self.runtime.cleanup()", text)

    def test_cleanup_is_unconditional_down_v(self) -> None:
        text = self._module_text()
        self.assertIn("self.runtime.cleanup()", text)
        # The base ComposeRuntime.down() runs `down -v --remove-orphans`.
        self.assertIn("down -v --remove-orphans", text)

    def test_reindex_runs_against_seeded_pack_without_runtime_mutation(self) -> None:
        """Reindex must run directly against the byte-identical template-seeded
        canonical pack: the runtime scenario class must never replace the
        runtime pack and must not carry a duplicate VALID_JOSEMAR_PACK
        constant. Only the runtime scenario class is inspected so this
        structural test's own assertion strings cannot pollute the check."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertNotIn("VALID_JOSEMAR_PACK", runtime_class)
        self.assertNotIn("schema-packs/josemar/pack.yaml <<", runtime_class)
        self.assertNotIn("> /opt/data/.gbrain/schema-packs/josemar/pack.yaml", runtime_class)
        # The reindex scenario must invoke the operator wrapper directly.
        self.assertIn('self.runtime.run_as_hermes("josemar-gbrain", "reindex", timeout=300)', runtime_class)
        # The active schema marker must be asserted as the runtime source of truth.
        self.assertIn('"/opt/data/.gbrain/active-schema-pack"', runtime_class)

    def test_report_uses_support_without_env_dump(self) -> None:
        text = self._module_text()
        self.assertIn("write_report", text)
        self.assertIn("conformance_report_dir", text)
        # The report must never serialize the process/runtime environment:
        # the report-writing method must not reference os.environ at all.
        report_method = text.split("def _write_report", 1)[1]
        report_method = report_method.split("class ", 1)[0]
        self.assertNotIn("os.environ", report_method)

    def test_conformance_matrix_covers_all_owned_operations(self) -> None:
        self.assertEqual(
            set(CONFORMANCE_MATRIX),
            {
                "baseline_seed",
                "baseline_build_start",
                "baseline_writable",
                "baseline_credentials",
                "baseline_jobs",
                "baseline_vault",
                "provenance",
                "pack_identity",
                "reindex",
                "status",
                "get",
                "search",
                "tags",
                "backlinks",
                "capture",
                "link",
                "graph",
                "refresh",
                "put",
                "put --stdin",
                "history",
                "revert",
                "delete",
                "restore",
                "external_edit_pre_refresh",
                "external_edit_post_refresh",
                "refresh_lock_busy",
                "public_reindex_rejected",
                "chronicle_timeline",
                "chronicle_day",
                "chronicle_day_week",
                "chronicle_since",
                "chronicle_last_seen",
                "chronicle_on_this_day",
                "chronicle_orient",
                "chronicle_ontology",
            },
        )

    def test_get_search_tags_scenarios_present(self) -> None:
        """The runtime class must exercise get/search/tags with the
        deterministic provider-free facts."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn('"gbrain", "get", "notes/welcome"', runtime_class)
        self.assertIn('"gbrain", "search", CONFORMANCE_TOKEN, "--limit", "5"', runtime_class)
        self.assertIn('"gbrain", "tags", TAGGED_NOTE_SLUG', runtime_class)
        self.assertIn("CONFORMANCE_TOKEN", runtime_class)
        self.assertIn("TAGGED_NOTE_SLUG", runtime_class)
        self.assertIn("CONFORMANCE_TAG", runtime_class)

    def test_search_asserts_text_output_not_json(self) -> None:
        """The search scenario must assert TEXT output (the pinned CLI renders
        search as text lines) and must not demand JSON."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn('self.assertIn("notes/welcome", ev.stdout)', runtime_class)
        self.assertIn('self.assertIn(CONFORMANCE_TOKEN, ev.stdout)', runtime_class)
        self.assertIn('self.assertNotIn(\'"slug"\', ev.stdout)', runtime_class)
        # The get/search/tags scenario must not parse its output as JSON.
        scenario = runtime_class.split("def _scenario_get_search_tags", 1)[1]
        scenario = scenario.split("def _find_backlink", 1)[0]
        self.assertNotIn("json.loads", scenario)

    def test_tagged_note_seeded_before_reindex(self) -> None:
        """The tagged note must be seeded in the base setup (before reindex)
        via a safe hermes command, never by mutating support or template."""
        text = self._module_text()
        self.assertIn("self._seed_tagged_note()", text)
        self.assertIn("self.runtime.run_as_hermes(\"sh\", \"-lc\", script)", text)
        self.assertIn("tags: [conformance]", text)
        self.assertIn("notes/conformance-tagged.md", text)

    def test_welcome_wikilink_seeded_before_reindex(self) -> None:
        """The welcome fixture page must carry the deterministic
        [[projects/atlas]] wikilink, seeded in the base setup before reindex
        via a safe hermes command."""
        text = self._module_text()
        self.assertIn("self._seed_welcome_wikilink()", text)
        self.assertIn("[[projects/atlas]]", text)
        self.assertIn("notes/welcome.md", text)
        self.assertIn("conformance-token-welcome", text)

    def test_links_backlinks_graph_scenarios_present(self) -> None:
        """The runtime class must exercise backlinks/link/graph/refresh with
        the deterministic link facts (semantic assertions, no full
        snapshots)."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn('"gbrain", "backlinks", WIKILINK_TARGET', runtime_class)
        self.assertIn('"gbrain", "link", MANUAL_LINK_SOURCE, WIKILINK_TARGET', runtime_class)
        self.assertIn('"--link-source", "manual"', runtime_class)
        self.assertIn('"gbrain", "graph", WIKILINK_TARGET', runtime_class)
        self.assertIn('"gbrain", "graph", MANUAL_LINK_SOURCE', runtime_class)
        self.assertIn('"josemar-gbrain", "refresh", timeout=300', runtime_class)
        self.assertIn("_require_backlink", runtime_class)
        self.assertIn("MANUAL_LINK_CONTEXT", runtime_class)
        # Semantic edge lookup, never a brittle full-snapshot assertion.
        self.assertIn("_find_backlink", runtime_class)
        self.assertNotIn("assertEqual(backlinks,", runtime_class)
        self.assertNotIn("assertEqual(graph,", runtime_class)

    def test_public_write_contracts_scenarios_present(self) -> None:
        """The runtime class must exercise the public write contracts:
        positional capture create/read-back/idempotency, capture --stdin
        --slug --source --json, capture --file create/replacement, and
        put --content retained section."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn('"gbrain", "capture", POSITIONAL_CAPTURE_BODY', runtime_class)
        self.assertIn('"--slug", POSITIONAL_CAPTURE_SLUG, "--type", "note", "--json"', runtime_class)
        self.assertIn("gbrain capture --stdin --slug ", runtime_class)
        self.assertIn("--source default --json", runtime_class)
        self.assertIn("gbrain capture --file ", runtime_class)
        self.assertIn('"gbrain", "put", PUT_SLUG, "--content", PUT_CONTENT', runtime_class)
        self.assertIn("STDIN_CAPTURE_BODY", runtime_class)
        self.assertIn("FILE_BODY_V1", runtime_class)
        self.assertIn("FILE_BODY_V2", runtime_class)
        self.assertIn("PUT_CONTENT", runtime_class)
        # Idempotency: the re-capture must assert the skipped status and the
        # unchanged content hash.
        self.assertIn('again.get("status"), "skipped"', runtime_class)
        self.assertIn('again.get("content_hash"), content_hash', runtime_class)

    def test_put_stdin_rejected_safety(self) -> None:
        """The public put --stdin rejection must be asserted as a safety
        contract (rc 2, allowlist message)."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn("gbrain put inbox/evil --stdin", runtime_class)
        self.assertIn("self.assertEqual(ev.returncode, 2)", runtime_class)
        self.assertIn('"not on the agent-facing allowlist"', runtime_class)
        self.assertIn('self._matrix["put --stdin"]', runtime_class)

    def test_recovery_history_revert_scenario_present(self) -> None:
        """The runtime class must exercise the recovery-page lifecycle:
        create A, update B, discover a stable revision handle via history,
        revert to it restoring the exact A body, and remain writable."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn("_scenario_recovery_history_revert", runtime_class)
        self.assertIn('"gbrain", "history", RECOVERY_SLUG', runtime_class)
        self.assertIn('"gbrain", "revert", RECOVERY_SLUG, revision', runtime_class)
        self.assertIn("_extract_revision_handle", runtime_class)
        self.assertIn("RECOVERY_BODY_A", runtime_class)
        self.assertIn("RECOVERY_BODY_B", runtime_class)
        self.assertIn("RECOVERY_BODY_C", runtime_class)
        self.assertIn("conformance-recovery-a", runtime_class)
        self.assertIn("conformance-recovery-b", runtime_class)
        self.assertIn('self._matrix["history"]', runtime_class)
        self.assertIn('self._matrix["revert"]', runtime_class)
        # The revision handle must be the PLAIN integer id: the pinned CLI
        # rejects the ``#1`` display form (invalid input syntax for integer).
        self.assertIn("return match.group(1)", runtime_class)
        self.assertNotIn("return match.group(0)", runtime_class)
        # The post-revert write must be a genuinely new write (C), not a
        # re-write of B (which the idempotency check would skip).
        self.assertIn('"gbrain", "put", RECOVERY_SLUG, "--content", RECOVERY_BODY_C', runtime_class)
        self.assertIn('put_result.get("status"), "created_or_updated"', runtime_class)

    def test_soft_delete_restore_scenario_present(self) -> None:
        """The runtime class must exercise the soft delete/restore lifecycle
        with the exact body."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn("_scenario_soft_delete_restore", runtime_class)
        self.assertIn('"gbrain", "delete", SOFT_DELETE_SLUG', runtime_class)
        self.assertIn('"gbrain", "restore", SOFT_DELETE_SLUG', runtime_class)
        self.assertIn("SOFT_DELETE_BODY", runtime_class)
        self.assertIn("conformance-soft-delete-body", runtime_class)
        self.assertIn('self._matrix["delete"]', runtime_class)
        self.assertIn('self._matrix["restore"]', runtime_class)

    def test_external_edit_refresh_scenario_present(self) -> None:
        """The runtime class must exercise the direct committed external edit
        after activation: public get/search must NOT assume it before
        refresh, and after refresh the unique token is visible while an
        unrelated known page survives."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn("_scenario_external_edit_refresh", runtime_class)
        self.assertIn("EXTERNAL_EDIT_TOKEN", runtime_class)
        self.assertIn("EXTERNAL_EDIT_SLUG", runtime_class)
        # Pre-refresh: get/search must NOT assume the edit.
        self.assertIn("self.assertNotIn(EXTERNAL_EDIT_TOKEN, ev.stdout)", runtime_class)
        self.assertIn('self.assertNotIn("notes/welcome", ev.stdout)', runtime_class)
        # Post-refresh: the unique token is visible.
        self.assertIn("self.assertIn(EXTERNAL_EDIT_TOKEN, ev.stdout)", runtime_class)
        self.assertIn('self.assertIn("notes/welcome", ev.stdout)', runtime_class)
        self.assertIn('self._matrix["external_edit_pre_refresh"]', runtime_class)
        self.assertIn('self._matrix["external_edit_post_refresh"]', runtime_class)
        # The unrelated known page must survive the refresh.
        self.assertIn('"gbrain", "get", TAGGED_NOTE_SLUG', runtime_class)
        self.assertIn('"gbrain", "tags", TAGGED_NOTE_SLUG', runtime_class)

    def test_lock_contention_scenario_present(self) -> None:
        """The runtime class must exercise shared-lock contention with an
        independent flock-only holder (no PGLite): refresh fails bounded with
        the lock-busy envelope, then succeeds after release."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn("_scenario_lock_contention", runtime_class)
        self.assertIn("_start_lock_holder", runtime_class)
        self.assertIn("_stop_lock_holder", runtime_class)
        self.assertIn("refresh_lock_busy", runtime_class)
        self.assertIn('self._matrix["refresh_lock_busy"]', runtime_class)
        self.assertIn("LOCK_HOLDER_SCRIPT", runtime_class)
        # The holder must be flock-only: no PGLite/gbrain access in the body.
        self.assertIn("fcntl.flock", LOCK_HOLDER_SCRIPT)
        self.assertNotIn("gbrain", LOCK_HOLDER_SCRIPT)
        self.assertNotIn("pglite", LOCK_HOLDER_SCRIPT.lower())

    def test_public_boundary_scenario_present(self) -> None:
        """The runtime class must assert the operator-only ``gbrain reindex``
        is rejected by the public adapter (rc 2, allowlist message) without
        invoking the private native binary."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn("_scenario_public_boundary", runtime_class)
        self.assertIn('"gbrain", "reindex", check=False', runtime_class)
        self.assertIn("self.assertEqual(ev.returncode, 2", runtime_class)
        self.assertIn('"not on the agent-facing allowlist"', runtime_class)
        self.assertIn(
            '"operator-only maintenance runs through josemar-gbrain"',
            runtime_class,
        )
        self.assertIn('self._matrix["public_reindex_rejected"]', runtime_class)

    def test_chronicle_zero_llm_scenario_present(self) -> None:
        """The runtime class must exercise every zero-LLM Chronicle read and
        assert clearly no synthetic events (empty arrays / explicit empty
        states / null last-seen)."""
        text = self._module_text()
        runtime_class = text.split("class GbrainConformanceRuntimeTests", 1)[1]
        runtime_class = runtime_class.split("class GbrainConformanceGateStructureTests", 1)[0]
        self.assertIn("_scenario_chronicle_zero_llm", runtime_class)
        for cmd in (
            "timeline", "day", "since", "last-seen", "on-this-day",
            "orient", "ontology",
        ):
            self.assertIn(f'"gbrain", "{cmd}"', runtime_class)
        self.assertIn("--week", runtime_class)
        self.assertIn("CHRONICLE_DAY", runtime_class)
        self.assertIn("CHRONICLE_ENTITY", runtime_class)
        self.assertIn("CHRONICLE_TIMELINE_SLUG", runtime_class)
        # No synthetic events: empty arrays / explicit empty states.
        self.assertIn("json.loads(ev.stdout), []", runtime_class)
        self.assertIn("No timeline entries", runtime_class)
        self.assertIn("recent_timeline", runtime_class)
        self.assertIn("last_date", runtime_class)
        self.assertIn("last_event_slug", runtime_class)
        # The loop-driven scenario must carry every chronicle matrix key and
        # mark each one fail/pass through the dynamic key.
        for key in (
            "chronicle_timeline", "chronicle_day", "chronicle_day_week",
            "chronicle_since", "chronicle_last_seen", "chronicle_on_this_day",
            "chronicle_orient", "chronicle_ontology",
        ):
            self.assertIn(key, runtime_class)
        self.assertIn('self._matrix[key] = "fail"', runtime_class)
        self.assertIn('self._matrix[key] = "pass"', runtime_class)


if __name__ == "__main__":
    unittest.main()
