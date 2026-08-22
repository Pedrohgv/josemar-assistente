"""Reusable core gbrain conformance scenarios (issue #127 W2b, PR #129).

This module is NOT a test module. It hosts the ``CoreScenarioMixin``: the
provider-free core operation scenarios (and their deterministic facts) that
the core runtime suite runs, and that the candidate upgrade suite can reuse
against a candidate image. Keeping the scenarios in a narrow mixin — instead
of methods on the Docker-gated test class — is what makes them reusable
without duplicating them (PR #129 MAJOR finding: the upgrade suite must
rerun the supported-operation matrix against the candidate rather than
duplicating the scenarios).

The mixin is deliberately narrow: it contains ONLY the scenario methods and
their deterministic facts. It does NOT contain the base runtime setup
(``GbrainConformanceTestCase``), the report writer, or the fast structural
guards — those stay in ``test_gbrain_conformance.py``.

Contract with any host test class that mixes this in (the core runtime class
and, later, the candidate upgrade runtime class):
  - ``self.runtime``: a ``GbrainConformanceRuntime`` (``run_as_hermes``,
    ``run``, ...)
  - ``self._matrix``: ``dict[str, str]`` keyed by ``CONFORMANCE_MATRIX``
  - ``self._evidence``: ``list[CommandEvidence]``
  - ``self._gbrain_version``: ``str | None`` (set by the status scenario)
  - ``self._schema_status_classification``: ``str`` (set by the schema-status
    probe scenario; the host report persists and cites it)
  - ``self._report_path``: ``Path | None``
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unittest
from pathlib import Path

from .gbrain_conformance_support import CommandEvidence, GbrainConformanceRuntime


# The pinned gbrain version the canonical GBRAIN_REF builds (docs/gbrain-
# operations.md "Pinned Values"). Asserted as the status --json runtime fact.
PINNED_GBRAIN_VERSION = "0.46.26.0"

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

# Deterministic doctor / sources / schema-status probe / type-inference
# facts (PR #129 MAJOR finding: these classified supported surfaces must have
# real runtime coverage).
DOCTOR_CORE_CHECKS = ("connection", "jsonb_integrity", "schema_version", "pgvector")

# Stable expected schema fact for a FIXED ``gbrain schema-status`` (PR #129
# re-review): the schema version and the canonical active pack — the same
# deterministic runtime facts the status scenario asserts. A real upstream
# fix is classified ``fixed`` only when the command succeeds AND the output
# carries both tokens (robust to JSON and text rendering).
SCHEMA_STATUS_EXPECTED_FACTS = ("schema_version", "josemar")

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
    "doctor": "core",
    "sources_list": "core",
    "schema_status_probe": "probe_unavailable",
    "type_inference": "core",
    "chronicle_timeline": "chronicle_read",
    "chronicle_day": "chronicle_read",
    "chronicle_day_week": "chronicle_read",
    "chronicle_since": "chronicle_read",
    "chronicle_last_seen": "chronicle_read",
    "chronicle_on_this_day": "chronicle_read",
    "chronicle_orient": "chronicle_read",
    "chronicle_ontology": "chronicle_read",
}


def _classify_schema_status_probe(ev: CommandEvidence) -> str:
    """Pure PR #129 re-review oracle for the ``gbrain schema-status`` probe.

    Report-only classification of the ACTUAL behavior, so a real upstream
    fix is recorded as ``fixed`` instead of rejecting the run:

      - ``fixed``: the command succeeds and the output carries the stable
        expected schema fact (``SCHEMA_STATUS_EXPECTED_FACTS``).
      - ``present``: the exact current failure signature — the pinned
        native CLI reports ``Unknown command``.
      - ``changed_failure_mode``: any other failure, or a success that does
        not deliver the stable expected schema fact.
    """
    if ev.returncode == 0:
        output = ev.stdout + ev.stderr
        if all(fact in output for fact in SCHEMA_STATUS_EXPECTED_FACTS):
            return "fixed"
        return "changed_failure_mode"
    if "Unknown command" in ev.stderr:
        return "present"
    return "changed_failure_mode"


class CoreScenarioMixin(unittest.TestCase):
    """Provider-free core operation scenarios (issue #127 W2b).

    Inherits ``unittest.TestCase`` so the scenario methods can use the
    standard assertion helpers; it defines no ``test_*`` methods of its own
    and is always mixed into a host runtime test class (which supplies
    ``self.runtime``, ``self._matrix``, ``self._evidence`` and the base
    setup). Every scenario records its outcome in ``self._matrix`` (fail ->
    pass), except the schema-status probe, which records its
    fixed/present/changed_failure_mode/inconclusive classification instead
    (PR #129 re-review: report-only, never a hard assertion). Every scenario
    appends complete ``CommandEvidence`` to ``self._evidence``. The
    scenarios are written against the mixin contract above so the same
    methods can run against the baseline runtime (core suite) and, later,
    against a candidate image (upgrade suite).
    """

    # Host-provided attributes (set by the host test class's setUp; see the
    # module docstring contract). Declared here so static checkers know the
    # scenario methods can rely on them.
    runtime: GbrainConformanceRuntime
    _matrix: dict[str, str]
    _evidence: list[CommandEvidence]
    _gbrain_version: str | None
    _schema_status_classification: str
    _report_path: Path | None

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
        from .gbrain_conformance_support import CANONICAL_PACK_SOURCE

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

    def _scenario_doctor(self) -> None:
        """``gbrain doctor --json`` returns a valid health report: the core
        checks (connection, jsonb_integrity, schema_version, pgvector) are ok
        and the expected no-embedding warning is present (the base deploy
        runs keyword-only, docs/gbrain-operations.md "Doctor Warns in
        No-Embedding Mode")."""
        self._matrix["doctor"] = "fail"
        ev = self.runtime.run_as_hermes("gbrain", "doctor", "--json", timeout=120)
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        report = json.loads(ev.stdout)
        checks = {c.get("name"): c.get("status") for c in report.get("checks", [])}
        for check in DOCTOR_CORE_CHECKS:
            self.assertEqual(
                checks.get(check), "ok", f"doctor check {check}: {report}"
            )
        self.assertEqual(
            checks.get("embeddings"),
            "warn",
            f"doctor must warn about missing embeddings in the base deploy: {report}",
        )
        self._matrix["doctor"] = "pass"

    def _scenario_sources_list(self) -> None:
        """``gbrain sources list --json`` (the read-only sources surface)
        reports exactly one registered source resolving to the vault path."""
        self._matrix["sources_list"] = "fail"
        ev = self.runtime.run_as_hermes(
            "gbrain", "sources", "list", "--json", timeout=120
        )
        self.assertEqual(ev.returncode, 0, ev.stderr)
        self._evidence.append(ev)
        data = json.loads(ev.stdout)
        sources = data.get("sources", [])
        self.assertEqual(len(sources), 1, data)
        self.assertEqual(sources[0].get("local_path"), "/opt/data/obsidian")
        self._matrix["sources_list"] = "pass"

    def _scenario_schema_status_probe(self) -> None:
        """``gbrain schema-status`` probe (PR #129 re-review): report-only
        classification, never a hard assertion. The agent-facing spelling is
        allowlisted by the public adapter but the pinned native CLI has no
        such command (known discrepancy, classified probe_unavailable). The
        probe records the ACTUAL classification so a real upstream fix is
        recorded as ``fixed`` instead of rejecting the candidate rerun:

          - ``fixed``: success returning the stable expected schema fact
          - ``present``: the exact current ``Unknown command`` failure
          - ``changed_failure_mode``: any other failure (or a success
            without the stable expected fact)
          - ``inconclusive``: only when the harness/setup cannot establish
            the probe (an exception running it)

        The classification is recorded in the matrix and exposed through
        ``self._schema_status_classification`` so the host report persists
        and cites it (baseline in the core suite; baseline and candidate in
        the upgrade suite)."""
        classification = self._probe_schema_status()
        self._matrix["schema_status_probe"] = classification
        self._schema_status_classification = classification

    def _probe_schema_status(self) -> str:
        """Run the probe and classify it. ``inconclusive`` ONLY when the
        harness/setup cannot establish the probe (an exception running the
        command); every completed probe classifies into
        fixed/present/changed_failure_mode."""
        try:
            ev = self.runtime.run_as_hermes(
                "gbrain", "schema-status", check=False, timeout=60,
            )
        except Exception:
            return "inconclusive"
        self._evidence.append(ev)
        return _classify_schema_status_probe(ev)

    def _scenario_type_inference(self) -> None:
        """Path-prefix type inference: the seeded pages carry the inferred
        types (people/ -> person, projects/ -> project, notes/ -> note)."""
        for slug, expected_type in (
            ("people/alice", "person"),
            ("projects/atlas", "project"),
            ("notes/welcome", "note"),
        ):
            self._matrix["type_inference"] = "fail"
            ev = self.runtime.run_as_hermes("gbrain", "get", slug)
            self.assertEqual(ev.returncode, 0, ev.stderr)
            self.assertIn(f"type: {expected_type}", ev.stdout)
            self._evidence.append(ev)
            self._matrix["type_inference"] = "pass"

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
