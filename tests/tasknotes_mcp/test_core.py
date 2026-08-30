"""Focused tests for the TaskNotes MCP core engine.

These tests use temporary Git vaults and a faithful fake gbrain executable
that mirrors the pinned gbrain contract (top-level type/title/tags, string
timeline, write-through provenance injection on disk, date normalization,
source routing). They cover the real TaskNotes 4.11.1 schema, gbrain source
verification/routing, mutation pre-put guards, faithful page/document model
and reconstruction, strong semantic verification, consolidated post-put
handling, completion date defaults, Git hardening, input bounds, bounded
subprocess I/O, race-safe no-follow reads, shared locking for gbrain reads,
and strict listing limits/filtering.
"""

from __future__ import annotations

import copy
import dataclasses
import errno
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_PATH = REPO_ROOT / "scripts" / "tasknotes_mcp_core.py"


def _load_core():
    spec = importlib.util.spec_from_file_location("tasknotes_mcp_core", CORE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["tasknotes_mcp_core"] = module
    spec.loader.exec_module(module)
    return module


def _has_yaml() -> bool:
    try:
        import yaml  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Real TaskNotes 4.11.1 fixtures
# ---------------------------------------------------------------------------

# Real-shaped manifest (matches the observed live plugin).
REAL_MANIFEST = {
    "id": "tasknotes",
    "name": "TaskNotes",
    "version": "4.11.1",
    "minAppVersion": "1.12.2",
    "description": "Note-based task management.",
    "author": "Callum Alpass",
    "authorUrl": "https://github.com/callumalpass",
    "isDesktopOnly": False,
}

# Real-shaped data.json with the observed schema: customStatuses,
# defaultTaskStatus, customPriorities, defaultTaskPriority, direct-string
# fieldMapping (including archiveTag), taskIdentificationMethod=tag.
REAL_PROFILE_DATA = {
    "tasksFolder": "tasks",
    "moveArchivedTasks": False,
    "archiveFolder": "tasks/archive",
    "taskTag": "task",
    "taskIdentificationMethod": "tag",
    "hideIdentifyingTagsInCards": False,
    "hideIdentifyingTagsMode": "all",
    "taskPropertyName": "",
    "taskPropertyValue": "",
    "excludedFolders": "",
    "defaultTaskPriority": "normal",
    "defaultTaskStatus": "open",
    "taskFilenameFormat": "zettel",
    "storeTitleInFilename": False,
    "customFilenameTemplate": "{{title}}",
    "fieldMapping": {
        "title": "title",
        "status": "status",
        "priority": "priority",
        "due": "due",
        "scheduled": "scheduled",
        "contexts": "contexts",
        "projects": "projects",
        "timeEstimate": "timeEstimate",
        "completedDate": "completedDate",
        "dateCreated": "dateCreated",
        "dateModified": "dateModified",
        "recurrence": "recurrence",
        "recurrenceAnchor": "recurrence_anchor",
        "recurrenceParent": "recurrence_parent",
        "occurrenceDate": "occurrence_date",
        "occurrenceMaterialization": "occurrence_materialization",
        "occurrenceNextTrigger": "occurrence_next_trigger",
        "occurrenceTemplate": "occurrence_template",
        "occurrencePastHorizon": "occurrence_past_horizon",
        "occurrenceFutureHorizon": "occurrence_future_horizon",
        "archiveTag": "archived",
        "timeEntries": "timeEntries",
        "completeInstances": "complete_instances",
        "skippedInstances": "skipped_instances",
        "blockedBy": "blockedBy",
        "pomodoros": "pomodoros",
        "icsEventId": "icsEventId",
        "icsEventTag": "ics_event",
        "googleCalendarEventId": "googleCalendarEventId",
        "googleCalendarExceptionEventId": "googleCalendarExceptionEventId",
        "googleCalendarExceptionOriginalScheduled": "googleCalendarExceptionOriginalScheduled",
        "googleCalendarMovedOriginalDates": "googleCalendarMovedOriginalDates",
        "reminders": "reminders",
        "sortOrder": "tasknotes_manual_order",
    },
    "customStatuses": [
        {"id": "none", "value": "none", "label": "None", "color": "#cccccc",
         "isCompleted": False, "excludeFromCycle": False, "order": 0,
         "autoArchive": False, "autoArchiveDelay": 5},
        {"id": "open", "value": "open", "label": "Open", "color": "#808080",
         "isCompleted": False, "excludeFromCycle": False, "order": 1,
         "autoArchive": False, "autoArchiveDelay": 5},
        {"id": "in-progress", "value": "in-progress", "label": "In progress", "color": "#0066cc",
         "isCompleted": False, "excludeFromCycle": False, "order": 2,
         "autoArchive": False, "autoArchiveDelay": 5},
        {"id": "done", "value": "done", "label": "Done", "color": "#00aa00",
         "isCompleted": True, "excludeFromCycle": False, "order": 3,
         "autoArchive": False, "autoArchiveDelay": 5},
    ],
    "customPriorities": [
        {"id": "none", "value": "none", "label": "None", "color": "#cccccc", "weight": 0},
        {"id": "low", "value": "low", "label": "Low", "color": "#00aa00", "weight": 1},
        {"id": "normal", "value": "normal", "label": "Normal", "color": "#ffaa00", "weight": 2},
        {"id": "high", "value": "high", "label": "High", "color": "#ff0000", "weight": 3},
    ],
    "userFields": [
        {"id": "pipeline_stage", "key": "pipeline_stage", "type": "text",
         "label": "Pipeline stage"},
        {"id": "tags_extra", "key": "tags_extra", "type": "list",
         "label": "Extra tags"},
        {"id": "effort_hours", "key": "effort_hours", "type": "number",
         "label": "Effort hours"},
        {"id": "blocked", "key": "blocked", "type": "boolean",
         "label": "Blocked"},
        {"id": "review_date", "key": "review_date", "type": "date",
         "label": "Review date"},
        {"id": "planned_week", "key": "planned_week", "type": "date",
         "label": "Planned week"},
        {"id": "related", "key": "related", "type": "link",
         "label": "Related note"},
        {"id": "team", "key": "team", "type": "enum",
         "label": "Team", "options": ["engineering", "design", "product", "marketing"]},
    ],
}


def _write_profile(vault: Path, manifest: Optional[dict] = None, data: Optional[dict] = None) -> None:
    plugin_dir = vault / ".obsidian/plugins/tasknotes"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "manifest.json").write_text(
        json.dumps(manifest or REAL_MANIFEST), encoding="utf-8"
    )
    (plugin_dir / "data.json").write_text(
        json.dumps(data or REAL_PROFILE_DATA), encoding="utf-8"
    )


def _init_git_repo(vault: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"], cwd=str(vault), check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@local"], cwd=str(vault),
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=str(vault),
        check=True, capture_output=True,
    )
    (vault / ".placeholder").write_text("# placeholder\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"], cwd=str(vault), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=str(vault),
        check=True, capture_output=True,
    )


def _make_vault(tmpdir: Path, name: str = "vault") -> Path:
    vault = tmpdir / name
    vault.mkdir()
    _write_profile(vault)
    (vault / "tasks").mkdir()
    _init_git_repo(vault)
    return vault


# ---------------------------------------------------------------------------
# Faithful fake gbrain executable
# ---------------------------------------------------------------------------


def _write_fake_gbrain(tmpdir: Path, behavior: Optional[dict] = None) -> Path:
    """Write a faithful fake gbrain executable.

    Mirrors the pinned gbrain contract:
      - sources list --json returns {"sources": [{"id", "name", "local_path", ...}]}
      - call --source <id> get_page <json> returns a page dict with
        top-level type/title/tags, frontmatter WITHOUT structural fields,
        string compiled_truth, string timeline
      - capture --stdin --slug <slug> --source <id> --json reads markdown
        stdin, parses frontmatter, stores the page, writes the disk file
        WITH provenance injection (ingested_via/ingested_at/source_kind
        and captured_via/captured_at) and date normalization, returns
        {"written": true|false, ...}
      - sync --source <id> ... returns {"status": "ok"}

    behavior overrides (all optional):
      - "sources": list of source dicts (default: one matching vault)
      - "capture_fail": set of slugs that fail write-through (written=false)
      - "capture_nonzero": set of slugs where capture exits nonzero
      - "capture_invalid_json": set of slugs where capture writes invalid JSON
      - "capture_timeout": set of slugs where capture sleeps past timeout
      - "get_not_found": set of slugs where get_page returns page_not_found
      - "sync_fail": bool
      - "no_source": bool (sources list returns empty)
      - "ambiguous_source": bool (two sources match vault)
    """
    bin_dir = tmpdir / "bin"
    bin_dir.mkdir(exist_ok=True)
    gbrain_path = bin_dir / "gbrain"
    state_path = tmpdir / "gbrain_state.json"
    log_path = tmpdir / "gbrain_calls.log"
    behavior = behavior or {}
    state_path.write_text(json.dumps({"behavior": behavior, "pages": {}}), encoding="utf-8")
    script = (
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(f'''
        import json, os, sys, time, datetime
        STATE = {str(state_path)!r}
        LOG = {str(log_path)!r}
        with open(STATE) as f:
            state = json.load(f)
        behavior = state.get("behavior", {{}})
        pages = state.setdefault("pages", {{}})
        argv = sys.argv[1:]
        stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
        with open(LOG, "a") as f:
            f.write(json.dumps({{"argv": argv, "stdin_len": len(stdin)}}) + "\\n")
        cmd = argv[0] if argv else ""

        def normalize_date(v):
            # gbrain normalizes bare dates to ISO timestamps on disk.
            if isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-":
                try:
                    datetime.date.fromisoformat(v)
                    return v + "T00:00:00.000Z"
                except Exception:
                    return v
            return v

        if cmd == "sources" and len(argv) >= 2 and argv[1] == "list":
            sources = behavior.get("sources")
            if sources is None:
                # Default: one source matching the vault.
                vault = behavior.get("vault", "")
                sources = [{{"id": "default", "name": "default", "local_path": vault,
                            "federated": False, "page_count": 0, "last_sync_at": None}}]
            if behavior.get("no_source"):
                sources = []
            if behavior.get("ambiguous_source"):
                vault = behavior.get("vault", "")
                sources = [
                    {{"id": "s1", "name": "s1", "local_path": vault, "federated": False,
                      "page_count": 0, "last_sync_at": None}},
                    {{"id": "s2", "name": "s2", "local_path": vault, "federated": False,
                      "page_count": 0, "last_sync_at": None}},
                ]
            print(json.dumps({{"sources": sources}}))
            sys.exit(0)

        if cmd == "call" and "get_page" in argv:
            # argv: call --source <id> get_page <json>
            source_id = None
            if "--source" in argv:
                idx = argv.index("--source")
                source_id = argv[idx + 1] if idx + 1 < len(argv) else None
            payload_arg = argv[-1]
            payload = json.loads(payload_arg)
            slug = payload.get("slug", "")
            if slug in behavior.get("get_not_found", []):
                print(json.dumps({{"error": "page_not_found", "message": "not found"}}))
                sys.exit(1)
            page = pages.get(slug)
            if page is None:
                print(json.dumps({{"error": "page_not_found", "message": "not found"}}))
                sys.exit(1)
            # Return faithful shape: top-level type/title/tags, frontmatter
            # WITHOUT structural fields, string timeline.
            fm = {{k: v for k, v in page["frontmatter"].items()
                   if k not in ("type", "title", "tags", "slug")}}
            # Normalize dates in returned frontmatter (gbrain get normalizes too).
            for k, v in list(fm.items()):
                fm[k] = normalize_date(v)
            out = {{
                "type": page.get("type", "note"),
                "title": page.get("title", ""),
                "tags": page.get("tags", []),
                "frontmatter": fm,
                "compiled_truth": page.get("compiled_truth", ""),
                "timeline": page.get("timeline", ""),
            }}
            print(json.dumps(out))
            sys.exit(0)

        if cmd == "capture" and "--stdin" in argv:
            # argv: capture --stdin --slug <slug> --source <id> --json
            slug = None
            source_id = None
            if "--slug" in argv:
                idx = argv.index("--slug")
                slug = argv[idx + 1] if idx + 1 < len(argv) else None
            if "--source" in argv:
                idx = argv.index("--source")
                source_id = argv[idx + 1] if idx + 1 < len(argv) else None
            if slug in behavior.get("capture_timeout", []):
                time.sleep(30)
                sys.exit(0)
            if slug in behavior.get("capture_nonzero", []):
                sys.stderr.write("fake gbrain capture error\\n")
                sys.exit(1)
            if slug in behavior.get("capture_invalid_json", []):
                print("not valid json{{")
                sys.exit(0)
            if slug in behavior.get("capture_fail", []):
                print(json.dumps({{"written": False, "error": "EACCES"}}))
                sys.exit(0)
            # Parse markdown.
            fm = {{}}
            body = ""
            timeline = ""
            if stdin.startswith("---\\n"):
                end = stdin.index("\\n---\\n", 4)
                fm_text = stdin[4:end]
                body = stdin[end + 5:]
                import yaml
                fm = yaml.safe_load(fm_text) or {{}}
            # Split body at timeline sentinel.
            if "<!-- timeline -->" in body:
                idx = body.index("<!-- timeline -->")
                compiled = body[:idx].rstrip("\\n")
                timeline = body[idx + len("<!-- timeline -->"):].lstrip("\\n")
            else:
                compiled = body.rstrip("\\n")
            # Extract top-level fields.
            ptype = fm.pop("type", "note")
            title = fm.pop("title", "")
            tags = fm.pop("tags", [])
            fm.pop("slug", None)
            # The on-disk write-through preserves the frontmatter the
            # adapter sent verbatim (gbrain capture --stdin is write-
            # through). Keep a copy of the raw frontmatter for disk.
            disk_fm_raw = dict(fm)
            # Normalize dates for the in-memory DB state only: gbrain
            # get_page returns bare dates normalized to midnight UTC.
            for k, v in list(fm.items()):
                fm[k] = normalize_date(v)
            # Store page (in-memory DB state, faithful get_page shape).
            pages[slug] = {{
                "type": ptype,
                "title": title,
                "tags": tags,
                "frontmatter": fm,
                "compiled_truth": compiled,
                "timeline": timeline,
            }}
            # Write disk file WITH provenance injection.
            vault = behavior.get("vault")
            if vault:
                p = os.path.join(vault, slug + ".md")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                disk_fm = dict(disk_fm_raw)
                disk_fm["type"] = ptype
                disk_fm["title"] = title
                # Provenance injection (operations.ts put_page + capture).
                disk_fm["ingested_via"] = "put_page"
                disk_fm["ingested_at"] = "2026-07-18T00:00:00.000Z"
                disk_fm["source_kind"] = "put_page"
                disk_fm["captured_via"] = "capture_stdin"
                disk_fm["captured_at"] = "2026-07-18T00:00:00.000Z"
                disk_fm["tags"] = tags
                import yaml
                fm_text = yaml.safe_dump(disk_fm, default_flow_style=False, sort_keys=False, allow_unicode=True)
                disk_content = "---\\n" + fm_text + "---\\n\\n"
                if compiled:
                    disk_content += compiled + "\\n"
                if timeline:
                    disk_content += "\\n<!-- timeline -->\\n\\n" + timeline + "\\n"
                with open(p, "w") as fobj:
                    fobj.write(disk_content)
            with open(STATE, "w") as fobj:
                json.dump(state, fobj)
            print(json.dumps({{"written": True, "slug": slug}}))
            sys.exit(0)

        if cmd == "delete" and len(argv) >= 2:
            slug = argv[1]
            if "--source" in argv:
                pass  # accepted, ignored in fake
            if slug in behavior.get("delete_fail", []):
                sys.stderr.write("fake gbrain delete error\\n")
                sys.exit(1)
            page = pages.pop(slug, None)
            if page is None:
                # Page not found in DB. Gbrain returns the same shape as the
                # real delete_page op: page_not_found error.
                print(json.dumps({{"error": "page_not_found", "message": "not found"}}))
                sys.exit(1)
            with open(STATE, "w") as fobj:
                json.dump(state, fobj)
            print(json.dumps({{"status": "soft_deleted", "slug": slug,
                               "recoverable_until": "now + 72h via restore_page"}}))
            sys.exit(0)

        if cmd == "sync":
            if behavior.get("sync_fail"):
                sys.stderr.write("sync failed\\n")
                sys.exit(1)
            # Import current markdown files so this fake models the freshness
            # guarantee provided by preflight commit + gbrain sync.
            vault = behavior.get("vault")
            if vault:
                for root, _dirs, files in os.walk(vault):
                    if ".git" in root.split(os.sep):
                        continue
                    for filename in files:
                        if not filename.endswith(".md"):
                            continue
                        path = os.path.join(root, filename)
                        rel = os.path.relpath(path, vault).replace(os.sep, "/")
                        slug = rel[:-3]
                        text = open(path, encoding="utf-8").read()
                        fm = {{}}
                        body = text
                        if text.startswith("---\\n") and "\\n---\\n" in text[4:]:
                            end = text.index("\\n---\\n", 4)
                            import yaml
                            fm = yaml.safe_load(text[4:end]) or {{}}
                            body = text[end + 5:]
                        timeline = ""
                        if "<!-- timeline -->" in body:
                            idx = body.index("<!-- timeline -->")
                            compiled = body[:idx].strip("\\n")
                            timeline = body[idx + len("<!-- timeline -->"):].strip("\\n")
                        else:
                            compiled = body.strip("\\n")
                        ptype = fm.pop("type", "note")
                        title = fm.pop("title", "")
                        tags = fm.pop("tags", [])
                        for key in ("slug", "ingested_via", "ingested_at", "source_kind",
                                    "captured_via", "captured_at"):
                            fm.pop(key, None)
                        for key, value in list(fm.items()):
                            fm[key] = normalize_date(value)
                        pages[slug] = {{
                            "type": ptype,
                            "title": title,
                            "tags": tags,
                            "frontmatter": fm,
                            "compiled_truth": compiled,
                            "timeline": timeline,
                        }}
                with open(STATE, "w") as fobj:
                    json.dump(state, fobj)
            print(json.dumps({{"status": "ok"}}))
            sys.exit(0)

        print(json.dumps({{"error": "unknown_command", "cmd": cmd}}))
        sys.exit(1)
    ''')
    )
    gbrain_path.write_text(script, encoding="utf-8")
    gbrain_path.chmod(0o755)
    return gbrain_path


def _set_behavior(tmpdir: Path, behavior: dict) -> None:
    state_path = tmpdir / "gbrain_state.json"
    state_path.write_text(json.dumps({"behavior": behavior, "pages": {}}), encoding="utf-8")


def _read_calls(tmpdir: Path) -> List[dict]:
    log_path = tmpdir / "gbrain_calls.log"
    if not log_path.exists():
        return []
    calls = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            calls.append(json.loads(line))
    return calls


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def _make_engine(core, tmpdir: Path, behavior: Optional[dict] = None):
    vault = _make_vault(tmpdir)
    behavior = behavior or {}
    behavior["vault"] = str(vault)
    gbrain_bin = _write_fake_gbrain(tmpdir, behavior)
    gbrain_home = tmpdir / "gbrain_home"
    gbrain_home.mkdir()
    lock_dir = tmpdir / "locks"
    engine = core.TaskNotesEngine(
        vault=vault,
        gbrain_bin=str(gbrain_bin),
        gbrain_home=gbrain_home,
        lock_dir=lock_dir,
        lock_timeout=2.0,
        tz="UTC",
    )
    return engine, vault, gbrain_bin


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class ProfileTests(unittest.TestCase):
    """Real TaskNotes 4.11.1 profile compatibility and drift tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.vault = _make_vault(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_valid_real_profile(self) -> None:
        profile = self.core.load_profile(self.vault, self.vault)
        self.assertEqual(profile.version, "4.11.1")
        self.assertEqual(profile.tasks_folder, "tasks")
        self.assertEqual(profile.task_tag, "task")
        self.assertEqual(profile.archive_tag, "archived")
        self.assertEqual(profile.completed_status, "done")
        self.assertEqual(profile.default_status, "open")
        self.assertEqual(profile.default_priority, "normal")
        self.assertEqual(profile.mappings["title"], "title")
        self.assertEqual(profile.mappings["completedDate"], "completedDate")
        self.assertEqual(profile.mappings["archiveTag"], "archived")
        self.assertTrue(profile.profile_hash)

    def test_wrong_version_rejected(self) -> None:
        m = copy.deepcopy(REAL_MANIFEST)
        m["version"] = "4.11.0"
        _write_profile(self.vault, manifest=m)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_task_identification_must_be_tag(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["taskIdentificationMethod"] = "property"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_store_title_in_filename_accepted(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["storeTitleInFilename"] = True
        _write_profile(self.vault, data=d)
        profile = self.core.load_profile(self.vault, self.vault)
        self.assertEqual(profile.version, "4.11.1")

    def test_custom_filename_format_accepted(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["taskFilenameFormat"] = "custom"
        _write_profile(self.vault, data=d)
        profile = self.core.load_profile(self.vault, self.vault)
        self.assertEqual(profile.version, "4.11.1")

    def test_move_archived_tasks_true_accepted_with_archive_folder(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["moveArchivedTasks"] = True
        d["archiveFolder"] = "tasks/archive"
        _write_profile(self.vault, data=d)
        (self.vault / "tasks" / "archive").mkdir(exist_ok=True)
        profile = self.core.load_profile(self.vault, self.vault)
        self.assertTrue(profile.move_archived_tasks)
        self.assertEqual(profile.archive_folder, "tasks/archive")

    def test_move_archived_tasks_true_rejected_without_archive_folder(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["moveArchivedTasks"] = True
        del d["archiveFolder"]
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_move_archived_tasks_false_ignores_archive_folder(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["moveArchivedTasks"] = False
        _write_profile(self.vault, data=d)
        profile = self.core.load_profile(self.vault, self.vault)
        self.assertFalse(profile.move_archived_tasks)
        self.assertIsNone(profile.archive_folder)

    def test_uppercase_tasks_folder_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["tasksFolder"] = "Tasks"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_absolute_tasks_folder_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["tasksFolder"] = "/etc"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_traversal_tasks_folder_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["tasksFolder"] = "tasks/../etc"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_backslash_tasks_folder_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["tasksFolder"] = "tasks\\evil"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_missing_tasks_folder_rejected(self) -> None:
        shutil.rmtree(self.vault / "tasks")
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_symlinked_tasks_folder_component_rejected(self) -> None:
        outside = self.tmpdir / "outside"
        (outside / "tasks").mkdir(parents=True)
        (self.vault / "nested").symlink_to(outside, target_is_directory=True)
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["tasksFolder"] = "nested/tasks"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_symlinked_profile_file_rejected(self) -> None:
        manifest = self.vault / ".obsidian" / "plugins" / "tasknotes" / "manifest.json"
        outside = self.tmpdir / "outside-manifest.json"
        outside.write_text(json.dumps(REAL_MANIFEST), encoding="utf-8")
        manifest.unlink()
        manifest.symlink_to(outside)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_no_completed_status_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        for s in d["customStatuses"]:
            s["isCompleted"] = False
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_two_completed_statuses_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["customStatuses"][0]["isCompleted"] = True
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_duplicate_status_ids_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["customStatuses"][0]["id"] = "open"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_duplicate_status_values_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["customStatuses"][0]["value"] = "open"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_default_status_not_in_set_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["defaultTaskStatus"] = "bogus"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_default_priority_not_in_set_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["defaultTaskPriority"] = "bogus"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_duplicate_priority_ids_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["customPriorities"][0]["id"] = "low"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_missing_mapping_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        del d["fieldMapping"]["completedDate"]
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_duplicate_mapping_values_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["fieldMapping"]["completedDate"] = "title"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_mapping_collides_with_reserved_key_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["fieldMapping"]["status"] = "type"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_mapping_collides_with_tags_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["fieldMapping"]["status"] = "tags"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_mapping_collides_with_provenance_key_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["fieldMapping"]["status"] = "ingested_via"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_non_title_mapping_collides_with_canonical_title_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["fieldMapping"]["title"] = "name"
        d["fieldMapping"]["status"] = "title"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_missing_archive_tag_mapping_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        del d["fieldMapping"]["archiveTag"]
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_archive_tag_same_as_task_tag_rejected(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["fieldMapping"]["archiveTag"] = "task"
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, self.vault)

    def test_vault_not_brain_repo_rejected(self) -> None:
        other = self.tmpdir / "other_vault"
        other.mkdir()
        with self.assertRaises(self.core.ProfileIncompatible):
            self.core.load_profile(self.vault, other)

    def test_no_git_head_rejected(self) -> None:
        nohead = self.tmpdir / "nohead"
        nohead.mkdir()
        _write_profile(nohead)
        (nohead / "tasks").mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(nohead), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@l"], cwd=str(nohead), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(nohead), check=True, capture_output=True)
        with self.assertRaises(self.core.GitError):
            self.core.load_profile(nohead, nohead)

    def test_merge_state_rejected(self) -> None:
        (self.vault / ".git" / "MERGE_HEAD").write_text("abc\n", encoding="utf-8")
        with self.assertRaises(self.core.GitError):
            self.core.load_profile(self.vault, self.vault)

    def test_rebase_state_rejected(self) -> None:
        (self.vault / ".git" / "rebase-merge").mkdir()
        with self.assertRaises(self.core.GitError):
            self.core.load_profile(self.vault, self.vault)

    def test_profile_hash_stable(self) -> None:
        p1 = self.core.load_profile(self.vault, self.vault)
        p2 = self.core.load_profile(self.vault, self.vault)
        self.assertEqual(p1.profile_hash, p2.profile_hash)

    def test_profile_drift_detected(self) -> None:
        p1 = self.core.load_profile(self.vault, self.vault)
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["defaultTaskStatus"] = "none"
        _write_profile(self.vault, data=d)
        p2 = self.core.load_profile(self.vault, self.vault)
        self.assertNotEqual(p1.profile_hash, p2.profile_hash)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class PathTests(unittest.TestCase):
    """Slug/path/symlink/control rejection tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.vault = _make_vault(self.tmpdir)
        self.profile = self.core.load_profile(self.vault, self.vault)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_valid_slug(self) -> None:
        self.assertEqual(self.core.validate_slug("20260718t143000"), "20260718t143000")
        self.assertEqual(self.core.validate_slug("a-b_c"), "a-b_c")

    def test_uppercase_slug_rejected(self) -> None:
        with self.assertRaises(self.core.PathError):
            self.core.validate_slug("MyTask")

    def test_absolute_slug_rejected(self) -> None:
        with self.assertRaises(self.core.PathError):
            self.core.validate_slug("/etc/evil")

    def test_traversal_slug_rejected(self) -> None:
        with self.assertRaises(self.core.PathError):
            self.core.validate_slug("..")
        with self.assertRaises(self.core.PathError):
            self.core.validate_slug("a..b")

    def test_backslash_slug_rejected(self) -> None:
        with self.assertRaises(self.core.PathError):
            self.core.validate_slug("a\\b")

    def test_slash_in_slug_rejected(self) -> None:
        # MVP: no nested slugs.
        with self.assertRaises(self.core.PathError):
            self.core.validate_slug("a/b")

    def test_control_char_slug_rejected(self) -> None:
        with self.assertRaises(self.core.PathError):
            self.core.validate_slug("a\x00b")

    def test_empty_slug_rejected(self) -> None:
        with self.assertRaises(self.core.PathError):
            self.core.validate_slug("")

    def test_oversized_slug_rejected(self) -> None:
        with self.assertRaises(self.core.PathError):
            self.core.validate_slug("a" * 201)

    def test_slugify_title_basic(self) -> None:
        self.assertEqual(self.core.slugify_title("Buy Groceries"), "buy-groceries")
        self.assertEqual(self.core.slugify_title("Review Q3 Report!"), "review-q3-report")
        self.assertEqual(self.core.slugify_title("  trim  "), "trim")
        self.assertEqual(self.core.slugify_title("multiple   spaces"), "multiple-spaces")

    def test_slugify_title_empty(self) -> None:
        self.assertEqual(self.core.slugify_title("日本語"), "")
        self.assertEqual(self.core.slugify_title("!!!"), "")
        self.assertEqual(self.core.slugify_title(""), "")

    def test_generate_slug_format(self) -> None:
        slug = self.core.generate_slug("Buy Groceries", tz="UTC")
        # Format: YYYY-MM-DD-HHmmss-buy-groceries
        self.assertRegex(slug, r"^\d{4}-\d{2}-\d{2}-\d{6}-buy-groceries$")
        # Must pass validation
        self.core.validate_slug(slug)

    def test_generate_slug_no_alphanumeric_title(self) -> None:
        slug = self.core.generate_slug("日本語", tz="UTC")
        self.assertRegex(slug, r"^\d{4}-\d{2}-\d{2}-\d{6}$")
        self.core.validate_slug(slug)

    def test_generate_slug_long_title_truncated(self) -> None:
        long_title = "a" * 300
        slug = self.core.generate_slug(long_title, tz="UTC")
        self.assertLessEqual(len(slug), self.core.MAX_SLUG_LEN)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class GitTests(unittest.TestCase):
    """Git command order and hardening tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.vault = _make_vault(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _git_env(self):
        return self.core._build_git_env()

    def test_preflight_commit_no_op_when_clean(self) -> None:
        committed = self.core.git_preflight_commit(self.vault, self._git_env())
        self.assertFalse(committed)

    def test_preflight_commit_stages_and_commits_pending_edit(self) -> None:
        (self.vault / "tasks" / "manual.md").write_text("---\ntitle: Manual\ntags:\n  - task\n---\nbody\n", encoding="utf-8")
        committed = self.core.git_preflight_commit(self.vault, self._git_env())
        self.assertTrue(committed)
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.vault), capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "")

    def test_preflight_commit_uses_generic_message(self) -> None:
        (self.vault / "tasks" / "manual.md").write_text("x", encoding="utf-8")
        self.core.git_preflight_commit(self.vault, self._git_env())
        r = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=str(self.vault), capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), self.core.PREFLIGHT_COMMIT_MSG)

    def test_postwrite_commit_targets_only_path(self) -> None:
        target = self.vault / "tasks" / "target.md"
        target.write_text("---\ntitle: T\ntags:\n  - task\n---\nbody\n", encoding="utf-8")
        (self.vault / "tasks" / "unrelated.md").write_text("y", encoding="utf-8")
        committed = self.core.git_commit_target(self.vault, target, self._git_env())
        self.assertTrue(committed)
        self.assertTrue(self.core.git_target_clean(self.vault, target, self._git_env()))
        r = subprocess.run(["git", "status", "--porcelain", "--", "tasks/unrelated.md"], cwd=str(self.vault), capture_output=True, text=True)
        self.assertIn("unrelated.md", r.stdout)

    def test_postwrite_commit_no_op_when_clean(self) -> None:
        target = self.vault / "tasks" / "target.md"
        target.write_text("x", encoding="utf-8")
        self.core.git_commit_target(self.vault, target, self._git_env())
        committed = self.core.git_commit_target(self.vault, target, self._git_env())
        self.assertFalse(committed)

    def test_git_disables_hooks(self) -> None:
        self.assertIn("core.hooksPath=/dev/null", self.core._GIT_BASE_ARGS)

    def test_git_disables_signing(self) -> None:
        self.assertIn("commit.gpgsign=false", self.core._GIT_BASE_ARGS)

    def test_git_disables_gc(self) -> None:
        self.assertIn("gc.auto=0", self.core._GIT_BASE_ARGS)

    def test_git_disables_maintenance(self) -> None:
        self.assertIn("maintenance.auto=false", self.core._GIT_BASE_ARGS)

    def test_git_env_no_credentials(self) -> None:
        env = self.core._build_git_env()
        for key in env:
            self.assertFalse(
                any(s in key.upper() for s in ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL", "API")),
                f"unexpected credential-like env var: {key}",
            )

    def test_git_state_rejects_unmerged(self) -> None:
        (self.vault / "tasks" / "conflict.md").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.vault), check=True, capture_output=True)
        (self.vault / ".git" / "MERGE_HEAD").write_text("abc\n", encoding="utf-8")
        with self.assertRaises(self.core.GitError):
            self.core.check_git_state(self.vault, self._git_env())

    def test_preflight_uses_maintenance_disable(self) -> None:
        # Verify the git base args are applied to preflight commits by
        # inspecting that a commit succeeds with hooks disabled (a hook
        # that would fail is not triggered).
        hook_dir = self.vault / ".git" / "hooks"
        hook_dir.mkdir(exist_ok=True)
        bad_hook = hook_dir / "pre-commit"
        bad_hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        bad_hook.chmod(0o755)
        (self.vault / "tasks" / "manual.md").write_text("x", encoding="utf-8")
        # Should succeed despite the failing hook because hooksPath=/dev/null.
        committed = self.core.git_preflight_commit(self.vault, self._git_env())
        self.assertTrue(committed)

    def test_postwrite_uses_maintenance_disable(self) -> None:
        hook_dir = self.vault / ".git" / "hooks"
        hook_dir.mkdir(exist_ok=True)
        bad_hook = hook_dir / "pre-commit"
        bad_hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        bad_hook.chmod(0o755)
        target = self.vault / "tasks" / "target.md"
        target.write_text("x", encoding="utf-8")
        committed = self.core.git_commit_target(self.vault, target, self._git_env())
        self.assertTrue(committed)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class SubprocessTests(unittest.TestCase):
    """Subprocess hardening tests (bounded I/O, timeout cleanup, env)."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_gbrain_env_excludes_credentials(self) -> None:
        inherited = {
            "LLAMA_SERVER_BASE_URL": "http://llama:8080",
            "GBRAIN_EMBEDDING_MODEL_REVISION": "revision-1",
            "GBRAIN_EMBEDDING_MODEL": "nomic-embed-text",
            "GBRAIN_EMBEDDING_DIMENSIONS": "768",
            "OPENAI_API_KEY": "secret",
            "TELEGRAM_BOT_TOKEN": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            env = self.core._build_gbrain_env(self.tmpdir, self.tmpdir)

        self.assertEqual(
            set(env),
            {
                "HOME", "PATH", "LANG", "LC_ALL", "TZ", "GBRAIN_HOME",
                "GBRAIN_BRAIN_REPO", "GBRAIN_SKIP_STARTUP_HOOKS",
                "LLAMA_SERVER_BASE_URL", "GBRAIN_EMBEDDING_MODEL_REVISION",
                "GBRAIN_EMBEDDING_MODEL", "GBRAIN_EMBEDDING_DIMENSIONS",
            },
        )
        self.assertEqual(env["LLAMA_SERVER_BASE_URL"], "http://llama:8080")
        self.assertEqual(env["GBRAIN_EMBEDDING_MODEL_REVISION"], "revision-1")
        self.assertEqual(env["GBRAIN_EMBEDDING_MODEL"], "nomic-embed-text")
        self.assertEqual(env["GBRAIN_EMBEDDING_DIMENSIONS"], "768")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)

    def test_gbrain_env_minimal_keys(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            env = self.core._build_gbrain_env(self.tmpdir, self.tmpdir)
        expected = {"HOME", "PATH", "LANG", "LC_ALL", "TZ", "GBRAIN_HOME",
                    "GBRAIN_BRAIN_REPO", "GBRAIN_SKIP_STARTUP_HOOKS"}
        self.assertEqual(set(env.keys()), expected)

    def test_gbrain_env_skip_startup_hooks_always_one(self) -> None:
        """Issue #112: _build_gbrain_env must assign
        GBRAIN_SKIP_STARTUP_HOOKS="1" (never inherit it), so a hostile caller
        environment cannot re-enable gbrain startup hooks through the private
        launcher."""
        for hostile in ("0", ""):
            with self.subTest(caller_value=hostile):
                with mock.patch.dict(
                    os.environ, {"GBRAIN_SKIP_STARTUP_HOOKS": hostile}, clear=True
                ):
                    env = self.core._build_gbrain_env(self.tmpdir, self.tmpdir)
                self.assertEqual(env["GBRAIN_SKIP_STARTUP_HOOKS"], "1")

    def test_subprocess_timeout_kills_process_group(self) -> None:
        script = self.tmpdir / "sleep.py"
        script.write_text(
            "import time, sys, os\n"
            "print('child pid', os.getpid(), file=sys.stderr)\n"
            "sys.stderr.flush()\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        with self.assertRaises(self.core.SubprocessError):
            self.core.run_subprocess([sys.executable, str(script)], timeout=0.5)

    def test_subprocess_oversized_output_raises_no_survivor(self) -> None:
        # Generate output exceeding the hard memory cap.
        script = self.tmpdir / "big.py"
        script.write_text(
            "import sys\n"
            "sys.stdout.write('x' * (1024 * 1024))\n"  # 1 MB >> 64 KB cap
            "sys.stdout.flush()\n",
            encoding="utf-8",
        )
        with self.assertRaises(self.core.SubprocessError):
            self.core.run_subprocess([sys.executable, str(script)], timeout=5)

    def test_subprocess_oversized_stderr_raises(self) -> None:
        script = self.tmpdir / "bigerr.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write('x' * (1024 * 1024))\n"
            "sys.stderr.flush()\n",
            encoding="utf-8",
        )
        with self.assertRaises(self.core.SubprocessError):
            self.core.run_subprocess([sys.executable, str(script)], timeout=5)

    def test_subprocess_redacts_errors(self) -> None:
        big = "x" * (self.core.MAX_OUTPUT + 1000)
        capped = self.core._redact(big)
        self.assertLessEqual(len(capped.encode("utf-8")), self.core.MAX_OUTPUT + 50)
        self.assertIn("[truncated]", capped)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class LockTests(unittest.TestCase):
    """Lock serialization tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.lock_path = self.tmpdir / "locks" / "test.lock"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_lock_acquires_and_releases(self) -> None:
        with self.core.Lock(self.lock_path, timeout=1.0):
            pass
        with self.core.Lock(self.lock_path, timeout=1.0):
            pass

    def test_lock_serializes_concurrent_holders(self) -> None:
        held = []

        def hold():
            with self.core.Lock(self.lock_path, timeout=2.0):
                held.append(time.time())
                time.sleep(0.3)

        threads = [threading.Thread(target=hold) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(held), 2)
        self.assertGreaterEqual(held[1] - held[0], 0.25)

    def test_lock_timeout_raises(self) -> None:
        holder_started = threading.Event()

        def hold():
            with self.core.Lock(self.lock_path, timeout=2.0):
                holder_started.set()
                time.sleep(1.0)

        t = threading.Thread(target=hold)
        t.start()
        holder_started.wait()
        try:
            with self.core.Lock(self.lock_path, timeout=0.2):
                pass
            self.fail("expected lock timeout")
        except self.core.CoreError:
            pass
        t.join()

    def test_lock_rejects_symlink_file(self) -> None:
        self.lock_path.parent.mkdir(parents=True)
        outside = self.tmpdir / "outside.lock"
        outside.write_text("", encoding="utf-8")
        self.lock_path.symlink_to(outside)
        with self.assertRaises(self.core.CoreError):
            with self.core.Lock(self.lock_path, timeout=0.1):
                pass


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class SourceRoutingTests(unittest.TestCase):
    """gbrain source verification and routing tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.engine, self.vault, self.gbrain_bin = _make_engine(self.core, self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_verify_source_returns_profile_with_source_id(self) -> None:
        with self.core.Lock(self.engine.lock_path, timeout=2.0):
            profile = self.engine.load_profile()
            verified = self.engine._verify_source(profile)
        self.assertEqual(verified.source_id, "default")

    def test_no_source_rejected(self) -> None:
        _set_behavior(self.tmpdir, {"vault": str(self.vault), "no_source": True})
        with self.core.Lock(self.engine.lock_path, timeout=2.0):
            profile = self.engine.load_profile()
            with self.assertRaises(self.core.GbrainError):
                self.engine._verify_source(profile)

    def test_ambiguous_source_rejected(self) -> None:
        _set_behavior(self.tmpdir, {"vault": str(self.vault), "ambiguous_source": True})
        with self.core.Lock(self.engine.lock_path, timeout=2.0):
            profile = self.engine.load_profile()
            with self.assertRaises(self.core.GbrainError):
                self.engine._verify_source(profile)

    def test_mismatched_source_rejected(self) -> None:
        _set_behavior(self.tmpdir, {
            "vault": str(self.vault),
            "sources": [{"id": "other", "name": "other", "local_path": "/some/other/path",
                         "federated": False, "page_count": 0, "last_sync_at": None}],
        })
        with self.core.Lock(self.engine.lock_path, timeout=2.0):
            profile = self.engine.load_profile()
            with self.assertRaises(self.core.GbrainError):
                self.engine._verify_source(profile)

    def test_no_source_no_git_side_effects(self) -> None:
        # When source verification fails, no sync/capture should run.
        _set_behavior(self.tmpdir, {"vault": str(self.vault), "no_source": True})
        with self.assertRaises(self.core.GbrainError):
            self.engine.create("t1", "T", body="b")
        calls = _read_calls(self.tmpdir)
        cmds = [c["argv"][0] if c["argv"] else "" for c in calls]
        self.assertIn("sources", cmds)
        self.assertNotIn("sync", cmds)
        self.assertNotIn("capture", cmds)

    def test_get_routes_with_source(self) -> None:
        self.engine.create("t1", "T", body="b")
        calls_before = _read_calls(self.tmpdir)
        # Clear log for get call inspection.
        (self.tmpdir / "gbrain_calls.log").write_text("", encoding="utf-8")
        self.engine.get("t1")
        calls = _read_calls(self.tmpdir)
        # The get call must include --source.
        get_call = [c for c in calls if c["argv"][:2] == ["call", "--source"]]
        self.assertTrue(get_call, "get must route with --source")
        self.assertIn("default", get_call[0]["argv"])

    def test_capture_routes_with_source(self) -> None:
        self.engine.create("t1", "T", body="b")
        calls = _read_calls(self.tmpdir)
        capture_calls = [c for c in calls if c["argv"][0] == "capture"]
        self.assertTrue(capture_calls)
        argv = capture_calls[0]["argv"]
        # Exact documented command path and required flags.
        self.assertEqual(argv[1], "--stdin")
        self.assertIn("--slug", argv)
        self.assertEqual(argv[argv.index("--slug") + 1], "tasks/t1")
        self.assertIn("--source", argv)
        self.assertEqual(argv[argv.index("--source") + 1], "default")
        self.assertIn("--json", argv)

    def test_capture_sends_body_through_stdin_not_argv(self) -> None:
        unique_body = "UNIQUE_STDIN_BODY_MARKER_42"
        self.engine.create("t1", "T", body=unique_body)
        calls = _read_calls(self.tmpdir)
        capture_calls = [c for c in calls if c["argv"][0] == "capture"]
        self.assertTrue(capture_calls)
        # The full body must travel through stdin, never argv.
        argv_joined = " ".join(capture_calls[0]["argv"])
        self.assertNotIn(unique_body, argv_joined)
        # stdin_len recorded by the fake must be non-trivial (frontmatter +
        # body), proving the body was piped through stdin.
        self.assertGreater(capture_calls[0]["stdin_len"], len(unique_body))
        # The body must be present on disk after capture write-through.
        disk_text = (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        self.assertIn(unique_body, disk_text)

    def test_capture_top_level_written_false_is_hard_failure(self) -> None:
        _set_behavior(self.tmpdir, {"vault": str(self.vault), "capture_fail": ["tasks/fail"]})
        result = self.engine.create("fail", "T", body="b")
        # A structured written:false must remain a hard failure before any
        # post-write Git operation: recovery_required, no commit attempted.
        self.assertEqual(result.state, self.core.RECOVERY_REQUIRED)
        # No git commit for the failed slug (no applied_and_committed).
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(self.vault),
            capture_output=True, text=True,
        )
        self.assertNotIn("tasknotes-mcp: task update", log.stdout)

    def test_large_multibyte_body_rejected_before_mutation(self) -> None:
        # A body whose UTF-8 byte length exceeds MAX_MARKDOWN_LEN must be
        # rejected up front (ValidationError) before any gbrain side effect.
        # Use a 4-byte-per-char sequence so character count stays under the
        # body character cap while the constructed markdown exceeds the byte
        # cap. Each emoji is 4 UTF-8 bytes.
        char_cap = self.core.MAX_BODY_LEN
        # Build a body just under the character cap but whose byte length
        # pushes the constructed markdown past MAX_MARKDOWN_LEN bytes.
        emoji = "\U0001F600"  # 4 bytes per char
        body = emoji * char_cap  # 100_000 chars => 400_000 bytes
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", body=body)
        # No capture subprocess should have been invoked.
        calls = _read_calls(self.tmpdir)
        capture_calls = [c for c in calls if c["argv"][0] == "capture"]
        self.assertEqual(capture_calls, [])

    def test_sync_routes_with_source(self) -> None:
        self.engine.create("t1", "T", body="b")
        calls = _read_calls(self.tmpdir)
        sync_calls = [c for c in calls if c["argv"][0] == "sync"]
        self.assertTrue(sync_calls)
        self.assertIn("--source", sync_calls[0]["argv"])

    def test_incremental_sync_accepts_pinned_human_success_output(self) -> None:
        with mock.patch(
            "tasknotes_mcp_core.run_subprocess",
            return_value=self.core.SubprocessResult(
                returncode=0,
                stdout="Already up to date.\n",
                stderr="",
            ),
        ):
            result = self.core.gbrain_sync_incremental(
                self.engine.gbrain_bin,
                self.engine._gbrain_env,
                self.vault,
                "default",
            )
        self.assertEqual(result, {})

    def test_full_sync_accepts_pinned_human_success_output(self) -> None:
        with mock.patch(
            "tasknotes_mcp_core.run_subprocess",
            return_value=self.core.SubprocessResult(
                returncode=0,
                stdout="First sync complete. Checkpoint: deadbeef\n",
                stderr="",
            ),
        ):
            result = self.core.gbrain_sync_full(
                self.engine.gbrain_bin,
                self.engine._gbrain_env,
                self.vault,
                "default",
            )
        self.assertEqual(result, {})

    def test_get_page_maps_structured_missing_page_to_typed_error(self) -> None:
        with mock.patch(
            "tasknotes_mcp_core.run_subprocess",
            return_value=self.core.SubprocessResult(
                returncode=1,
                stdout='{"error":"page_not_found"}\n',
                stderr="",
            ),
        ):
            with self.assertRaises(self.core.GbrainPageNotFound):
                self.core.gbrain_get_page(
                    self.engine.gbrain_bin,
                    self.engine._gbrain_env,
                    "tasks/missing",
                    "default",
                )

    def test_get_page_maps_pinned_missing_stderr_to_typed_error(self) -> None:
        with mock.patch(
            "tasknotes_mcp_core.run_subprocess",
            return_value=self.core.SubprocessResult(
                returncode=1,
                stdout="",
                stderr="Page not found: tasks/missing\n",
            ),
        ):
            with self.assertRaises(self.core.GbrainPageNotFound):
                self.core.gbrain_get_page(
                    self.engine.gbrain_bin,
                    self.engine._gbrain_env,
                    "tasks/missing",
                    "default",
                )


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class ReconstructionTests(unittest.TestCase):
    """Faithful page/document model and reconstruction tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.vault = _make_vault(self.tmpdir)
        self.profile = self.core.load_profile(self.vault, self.vault)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_decode_page_strips_structural_from_frontmatter(self) -> None:
        page = {
            "type": "note",
            "title": "T",
            "tags": ["task"],
            "frontmatter": {"title": "T", "status": "open", "custom": "x"},
            "compiled_truth": "body",
            "timeline": "",
        }
        decoded = self.core.decode_page(page)
        self.assertNotIn("title", decoded["frontmatter"])
        self.assertNotIn("type", decoded["frontmatter"])
        self.assertNotIn("tags", decoded["frontmatter"])
        self.assertEqual(decoded["frontmatter"]["status"], "open")
        self.assertEqual(decoded["frontmatter"]["custom"], "x")

    def test_decode_page_rejects_non_string_title(self) -> None:
        page = {"type": "note", "title": 123, "tags": [], "frontmatter": {},
                "compiled_truth": "", "timeline": ""}
        with self.assertRaises(self.core.GbrainError):
            self.core.decode_page(page)

    def test_decode_page_rejects_non_list_tags(self) -> None:
        page = {"type": "note", "title": "T", "tags": "task", "frontmatter": {},
                "compiled_truth": "", "timeline": ""}
        with self.assertRaises(self.core.GbrainError):
            self.core.decode_page(page)

    def test_decode_page_rejects_non_string_timeline(self) -> None:
        page = {"type": "note", "title": "T", "tags": [], "frontmatter": {},
                "compiled_truth": "", "timeline": []}
        with self.assertRaises(self.core.GbrainError):
            self.core.decode_page(page)

    def test_reconstruct_preserves_unknown_frontmatter(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open", "priority": "normal", "custom_field": "keep"},
            "compiled_truth": "body", "timeline": "",
        }
        md = self.core.reconstruct_markdown(page, self.profile, {"status": "in-progress"})
        fm, body = self.core._parse_frontmatter(md)
        self.assertEqual(fm["status"], "in-progress")
        self.assertEqual(fm["custom_field"], "keep")
        self.assertIn("body", body)

    def test_reconstruct_preserves_body_and_timeline(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open"},
            "compiled_truth": "my body",
            "timeline": "timeline event 1\ntimeline event 2",
        }
        md = self.core.reconstruct_markdown(page, self.profile, {})
        self.assertIn("my body", md)
        self.assertIn("<!-- timeline -->", md)
        self.assertIn("timeline event 1", md)
        self.assertIn("timeline event 2", md)

    def test_reconstruct_emits_pinned_timeline_sentinel(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open"},
            "compiled_truth": "body",
            "timeline": "event",
        }
        md = self.core.reconstruct_markdown(page, self.profile, {})
        # Exactly one sentinel, no closing marker.
        self.assertEqual(md.count("<!-- timeline -->"), 1)
        self.assertNotIn("<!-- /timeline -->", md)
        self.assertNotIn("--- timeline ---", md)

    def test_reconstruct_no_timeline_no_sentinel(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open"},
            "compiled_truth": "body", "timeline": "",
        }
        md = self.core.reconstruct_markdown(page, self.profile, {})
        self.assertNotIn("<!-- timeline -->", md)

    def test_reconstruct_sets_mapped_fields(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open"},
            "compiled_truth": "", "timeline": "",
        }
        md = self.core.reconstruct_markdown(
            page, self.profile,
            {"status": "done", "priority": "high", "due": "2026-07-25"},
        )
        fm, _ = self.core._parse_frontmatter(md)
        self.assertEqual(fm["status"], "done")
        self.assertEqual(fm["priority"], "high")
        self.assertEqual(fm["due"], "2026-07-25")

    def test_reconstruct_none_removes_field(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open", "due": "2026-07-25"},
            "compiled_truth": "", "timeline": "",
        }
        md = self.core.reconstruct_markdown(page, self.profile, {"due": None})
        fm, _ = self.core._parse_frontmatter(md)
        self.assertNotIn("due", fm)

    def test_reconstruct_body_override_replaces_body(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open", "custom_field": "keep"},
            "compiled_truth": "old body", "timeline": "",
        }
        md = self.core.reconstruct_markdown(
            page, self.profile, {}, body_override="new body"
        )
        fm, body = self.core._parse_frontmatter(md)
        self.assertEqual(fm["status"], "open")
        self.assertEqual(fm["custom_field"], "keep")
        self.assertIn("new body", body)
        self.assertNotIn("old body", md)

    def test_reconstruct_body_override_empty_clears_body(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open"},
            "compiled_truth": "old body", "timeline": "",
        }
        md = self.core.reconstruct_markdown(
            page, self.profile, {}, body_override=""
        )
        fm, body = self.core._parse_frontmatter(md)
        self.assertEqual(fm["status"], "open")
        self.assertNotIn("old body", body)

    def test_reconstruct_body_override_none_preserves_body(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open"},
            "compiled_truth": "keep me", "timeline": "",
        }
        md = self.core.reconstruct_markdown(
            page, self.profile, {}, body_override=None
        )
        self.assertIn("keep me", md)

    def test_reconstruct_body_override_preserves_timeline(self) -> None:
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {"status": "open"},
            "compiled_truth": "old body",
            "timeline": "timeline event 1\ntimeline event 2",
        }
        md = self.core.reconstruct_markdown(
            page, self.profile, {}, body_override="new body"
        )
        self.assertIn("new body", md)
        self.assertIn("<!-- timeline -->", md)
        self.assertIn("timeline event 1", md)
        self.assertIn("timeline event 2", md)

    def test_reconstruct_denormalizes_bare_date_from_gbrain(self) -> None:
        """gbrain returns bare dates as ``YYYY-MM-DDT00:00:00.000Z``.

        The write path must serialize them back as plain ``YYYY-MM-DD``
        so disk frontmatter stays canonical, while still applying status
        and completedDate updates.
        """
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {
                "status": "open",
                "scheduled": "2026-08-10T00:00:00.000Z",
                "due": "2026-09-01T00:00:00.000Z",
            },
            "compiled_truth": "body", "timeline": "",
        }
        md = self.core.reconstruct_markdown(
            page, self.profile,
            {"status": "done", "completedDate": "2026-08-10"},
        )
        fm, _ = self.core._parse_frontmatter(md)
        # Preserved date fields collapse to plain YYYY-MM-DD.
        self.assertEqual(str(fm["scheduled"]), "2026-08-10")
        self.assertEqual(str(fm["due"]), "2026-09-01")
        # Updated fields are applied.
        self.assertEqual(fm["status"], "done")
        self.assertEqual(str(fm["completedDate"]), "2026-08-10")
        # No normalized timestamp form leaks into the serialized markdown.
        self.assertNotIn("T00:00:00.000Z", md)

    def test_reconstruct_preserves_true_datetime(self) -> None:
        """True datetimes (non-midnight-UTC) are preserved verbatim."""
        page = {
            "type": "note", "title": "T", "tags": ["task"],
            "frontmatter": {
                "status": "open",
                "scheduled": "2026-08-10T13:45:00.000Z",
            },
            "compiled_truth": "body", "timeline": "",
        }
        md = self.core.reconstruct_markdown(page, self.profile, {})
        fm, _ = self.core._parse_frontmatter(md)
        self.assertEqual(str(fm["scheduled"]), "2026-08-10T13:45:00.000Z")

    def test_build_create_markdown_injects_task_tag(self) -> None:
        md = self.core.build_create_markdown(
            self.profile, "Title", "open", "normal", None, None, None, None, "body"
        )
        fm, _ = self.core._parse_frontmatter(md)
        self.assertIn("task", fm["tags"])

    def test_build_create_markdown_preserves_extra_tags(self) -> None:
        md = self.core.build_create_markdown(
            self.profile, "Title", "open", "normal", None, None, None, ["custom"], "body"
        )
        fm, _ = self.core._parse_frontmatter(md)
        self.assertIn("task", fm["tags"])
        self.assertIn("custom", fm["tags"])

    def test_build_create_markdown_writes_recurrence(self) -> None:
        md = self.core.build_create_markdown(
            self.profile, "Title", "open", "normal", None, None, None, None, "body",
            recurrence="FREQ=WEEKLY;BYDAY=MO,WE,FR",
        )
        fm, _ = self.core._parse_frontmatter(md)
        self.assertEqual(fm["recurrence"], "FREQ=WEEKLY;BYDAY=MO,WE,FR")

    def test_build_create_markdown_omits_recurrence_when_none(self) -> None:
        md = self.core.build_create_markdown(
            self.profile, "Title", "open", "normal", None, None, None, None, "body"
        )
        fm, _ = self.core._parse_frontmatter(md)
        self.assertNotIn("recurrence", fm)

    def test_build_create_markdown_recurrence_requires_profile_mapping(self) -> None:
        # Build a profile without a recurrence mapping by removing it.
        d = copy.deepcopy(REAL_PROFILE_DATA)
        del d["fieldMapping"]["recurrence"]
        _write_profile(self.vault, data=d)
        profile = self.core.load_profile(self.vault, self.vault)
        with self.assertRaises(self.core.ValidationError):
            self.core.build_create_markdown(
                profile, "Title", "open", "normal", None, None, None, None, "body",
                recurrence="FREQ=DAILY",
            )

    def test_profile_extracts_recurrence_mapping(self) -> None:
        # The real profile data declares recurrence in fieldMapping.
        self.assertEqual(self.profile.mappings.get("recurrence"), "recurrence")

    def test_profile_without_recurrence_mapping(self) -> None:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        del d["fieldMapping"]["recurrence"]
        _write_profile(self.vault, data=d)
        profile = self.core.load_profile(self.vault, self.vault)
        self.assertNotIn("recurrence", profile.mappings)

    def test_split_body_timeline_at_sentinel(self) -> None:
        body = "line1\nline2\n\n<!-- timeline -->\nevent1\nevent2"
        compiled, timeline = self.core._split_body_timeline(body)
        self.assertEqual(compiled, "line1\nline2\n")
        self.assertEqual(timeline, "event1\nevent2")

    def test_split_body_no_sentinel(self) -> None:
        body = "line1\nline2"
        compiled, timeline = self.core._split_body_timeline(body)
        self.assertEqual(compiled, body)
        self.assertEqual(timeline, "")


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class ListingTests(unittest.TestCase):
    """Race-safe no-follow listing tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.vault = _make_vault(self.tmpdir)
        self.profile = self.core.load_profile(self.vault, self.vault)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_task(self, slug: str, fm: dict, body: str = "body") -> None:
        import yaml
        path = self.vault / "tasks" / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_text = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False)
        path.write_text(f"---\n{fm_text}---\n{body}\n", encoding="utf-8")

    def test_list_returns_modeled_fields(self) -> None:
        self._write_task("task1", {"title": "T1", "status": "open", "priority": "normal", "tags": ["task"]})
        results = self.core.list_tasks(self.vault, self.profile)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["slug"], "task1")
        self.assertEqual(results[0]["status"], "open")
        self.assertEqual(results[0]["priority"], "normal")
        self.assertEqual(results[0]["title"], "T1")
        self.assertIn("task", results[0]["tags"])

    def test_list_filters_by_task_tag(self) -> None:
        self._write_task("task1", {"title": "T1", "tags": ["task"]})
        self._write_task("note1", {"title": "N1", "tags": ["other"]})
        results = self.core.list_tasks(self.vault, self.profile)
        slugs = [r["slug"] for r in results]
        self.assertIn("task1", slugs)
        self.assertNotIn("note1", slugs)

    def test_list_skips_non_md_files(self) -> None:
        (self.vault / "tasks" / "notes.txt").write_text("x", encoding="utf-8")
        results = self.core.list_tasks(self.vault, self.profile)
        self.assertEqual(results, [])

    def test_list_skips_symlinks(self) -> None:
        outside = self.tmpdir / "outside.md"
        outside.write_text("---\ntitle: T\ntags:\n  - task\n---\nbody\n", encoding="utf-8")
        link = self.vault / "tasks" / "link.md"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlink not supported")
        results = self.core.list_tasks(self.vault, self.profile)
        self.assertEqual(results, [])

    def test_list_skips_oversized_files(self) -> None:
        big = self.vault / "tasks" / "big.md"
        big.write_text("---\ntitle: T\ntags:\n  - task\n---\n" + "x" * (self.core.LIST_MAX_FILE_SIZE + 1), encoding="utf-8")
        results = self.core.list_tasks(self.vault, self.profile, max_size=1024)
        self.assertEqual(results, [])

    def test_list_bounds_results(self) -> None:
        for i in range(5):
            self._write_task(f"task{i}", {"title": f"T{i}", "status": "open", "tags": ["task"]})
        results = self.core.list_tasks(self.vault, self.profile, max_results=3)
        self.assertEqual(len(results), 3)

    def test_list_empty_folder(self) -> None:
        results = self.core.list_tasks(self.vault, self.profile)
        self.assertEqual(results, [])

    def test_list_skips_malformed_frontmatter(self) -> None:
        path = self.vault / "tasks" / "bad.md"
        path.write_text("---\n: invalid: : :\n---\nbody\n", encoding="utf-8")
        results = self.core.list_tasks(self.vault, self.profile)
        self.assertEqual(results, [])

    def test_list_filter_by_status(self) -> None:
        self._write_task("t1", {"title": "T1", "status": "open", "priority": "normal", "tags": ["task"]})
        self._write_task("t2", {"title": "T2", "status": "in-progress", "priority": "normal", "tags": ["task"]})
        results = self.core.list_tasks(self.vault, self.profile, status="open")
        slugs = [r["slug"] for r in results]
        self.assertEqual(slugs, ["t1"])

    def test_list_filter_by_priority(self) -> None:
        self._write_task("t1", {"title": "T1", "status": "open", "priority": "normal", "tags": ["task"]})
        self._write_task("t2", {"title": "T2", "status": "open", "priority": "high", "tags": ["task"]})
        results = self.core.list_tasks(self.vault, self.profile, priority="high")
        slugs = [r["slug"] for r in results]
        self.assertEqual(slugs, ["t2"])

    def test_list_filter_by_tag(self) -> None:
        self._write_task("t1", {"title": "T1", "status": "open", "tags": ["task", "urgent"]})
        self._write_task("t2", {"title": "T2", "status": "open", "tags": ["task"]})
        results = self.core.list_tasks(self.vault, self.profile, tag="urgent")
        slugs = [r["slug"] for r in results]
        self.assertEqual(slugs, ["t1"])

    def test_list_filter_archived_true(self) -> None:
        self._write_task("t1", {"title": "T1", "status": "open", "tags": ["task"]})
        self._write_task("t2", {"title": "T2", "status": "open", "tags": ["task", "archived"]})
        results = self.core.list_tasks(self.vault, self.profile, archived=True)
        slugs = [r["slug"] for r in results]
        self.assertEqual(slugs, ["t2"])

    def test_list_filter_archived_false(self) -> None:
        self._write_task("t1", {"title": "T1", "status": "open", "tags": ["task"]})
        self._write_task("t2", {"title": "T2", "status": "open", "tags": ["task", "archived"]})
        results = self.core.list_tasks(self.vault, self.profile, archived=False)
        slugs = [r["slug"] for r in results]
        self.assertEqual(slugs, ["t1"])

    def test_list_filter_archived_none_returns_all(self) -> None:
        self._write_task("t1", {"title": "T1", "status": "open", "tags": ["task"]})
        self._write_task("t2", {"title": "T2", "status": "open", "tags": ["task", "archived"]})
        results = self.core.list_tasks(self.vault, self.profile, archived=None)
        slugs = sorted(r["slug"] for r in results)
        self.assertEqual(slugs, ["t1", "t2"])

    def test_list_combined_filters(self) -> None:
        self._write_task("t1", {"title": "T1", "status": "open", "priority": "normal", "tags": ["task", "urgent"]})
        self._write_task("t2", {"title": "T2", "status": "open", "priority": "high", "tags": ["task", "urgent"]})
        self._write_task("t3", {"title": "T3", "status": "in-progress", "priority": "normal", "tags": ["task", "urgent"]})
        self._write_task("t4", {"title": "T4", "status": "open", "priority": "normal", "tags": ["task"]})
        results = self.core.list_tasks(
            self.vault, self.profile, status="open", priority="normal", tag="urgent"
        )
        slugs = [r["slug"] for r in results]
        self.assertEqual(slugs, ["t1"])

    def test_list_filter_returns_empty_when_no_match(self) -> None:
        self._write_task("t1", {"title": "T1", "status": "open", "tags": ["task"]})
        results = self.core.list_tasks(self.vault, self.profile, status="done")
        self.assertEqual(results, [])

    def test_list_filter_via_engine(self) -> None:
        self._write_task("t1", {"title": "T1", "status": "open", "tags": ["task"]})
        self._write_task("t2", {"title": "T2", "status": "in-progress", "tags": ["task"]})
        results = self.core.list_tasks(self.vault, self.profile, status="open")
        slugs = [r["slug"] for r in results]
        self.assertEqual(slugs, ["t1"])


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class NoFollowReadTests(unittest.TestCase):
    """Race-safe no-follow read tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.vault = _make_vault(self.tmpdir)
        self.profile = self.core.load_profile(self.vault, self.vault)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_file_no_follow_rejects_symlink(self) -> None:
        outside = self.tmpdir / "outside.md"
        outside.write_text("x", encoding="utf-8")
        link = self.vault / "tasks" / "link.md"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlink not supported")
        with self.assertRaises(self.core.PathError):
            self.core.read_file_no_follow(link)

    def test_target_exists_no_follow_detects_symlink(self) -> None:
        outside = self.tmpdir / "outside.md"
        outside.write_text("x", encoding="utf-8")
        link = self.vault / "tasks" / "link.md"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlink not supported")
        # A symlink counts as "exists" for the no-follow check.
        self.assertTrue(self.core.target_exists_no_follow(link))

    def test_list_dir_no_follow_rejects_symlink_dir(self) -> None:
        outside = self.tmpdir / "outside_dir"
        outside.mkdir()
        link = self.vault / "tasks" / "linkdir"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlink not supported")
        with self.assertRaises(self.core.PathError):
            self.core.list_dir_no_follow(link)

    def test_semantic_from_disk_rejects_symlink(self) -> None:
        outside = self.tmpdir / "outside.md"
        outside.write_text("---\ntitle: T\ntags:\n  - task\n---\nbody\n", encoding="utf-8")
        link = self.vault / "tasks" / "link.md"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlink not supported")
        with self.assertRaises(self.core.PathError):
            self.core.semantic_from_disk(self.vault, self.profile, "link")

    def test_semantic_from_disk_requires_task_tag(self) -> None:
        path = self.vault / "tasks" / "notask.md"
        path.write_text("---\ntitle: T\ntags:\n  - other\n---\nbody\n", encoding="utf-8")
        with self.assertRaises(self.core.CoreError):
            self.core.semantic_from_disk(self.vault, self.profile, "notask")

    def test_semantic_from_disk_rejects_non_string_tags(self) -> None:
        path = self.vault / "tasks" / "bad-tags.md"
        path.write_text(
            "---\ntitle: T\ntags:\n  - task\n  - 42\n---\nbody\n",
            encoding="utf-8",
        )
        with self.assertRaises(self.core.CoreError):
            self.core.semantic_from_disk(self.vault, self.profile, "bad-tags")


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class EngineOperationTests(unittest.TestCase):
    """Engine operation tests with faithful fake gbrain."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.engine, self.vault, self.gbrain_bin = _make_engine(self.core, self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_success(self) -> None:
        result = self.engine.create("t1", "My Task", status="open", priority="normal", body="body")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.slug, "t1")
        self.assertIsNotNone(result.commit_id)

    def test_create_rejects_completed_status(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", status="done")

    def test_create_rejects_archive_tag(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", tags=["archived"])

    def test_create_injects_task_tag(self) -> None:
        self.engine.create("t1", "T", body="b")
        target = self.vault / "tasks" / "t1.md"
        self.assertTrue(target.exists())
        fm, _ = self.core._parse_frontmatter(target.read_text(encoding="utf-8"))
        self.assertIn("task", fm["tags"])

    def test_create_invalid_status_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", status="bogus")

    def test_create_invalid_priority_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", priority="bogus")

    def test_create_invalid_date_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", due="not-a-date")
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", due="2026-13-45")

    def test_create_oversized_title_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "x" * 501)

    def test_create_oversized_body_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", body="x" * (self.core.MAX_BODY_LEN + 1))

    def test_create_rejects_existing_disk_target(self) -> None:
        # Pre-create the target file on disk.
        target = self.vault / "tasks" / "t1.md"
        target.write_text("---\ntitle: T\ntags:\n  - task\n---\nbody\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.vault), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=str(self.vault), check=True, capture_output=True)
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", body="b")

    def test_create_rejects_existing_db_page(self) -> None:
        # First create succeeds.
        self.engine.create("t1", "T", body="b")
        # Second create on same slug must fail (DB page exists).
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T2", body="b2")

    def test_create_rejects_symlink_target(self) -> None:
        outside = self.tmpdir / "outside.md"
        outside.write_text("x", encoding="utf-8")
        link = self.vault / "tasks" / "t1.md"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlink not supported")
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", body="b")

    def test_create_writes_recurrence_to_frontmatter(self) -> None:
        self.engine.create(
            "t1", "T", body="b", recurrence="FREQ=MONTHLY;BYMONTHDAY=15"
        )
        target = self.vault / "tasks" / "t1.md"
        fm, _ = self.core._parse_frontmatter(target.read_text(encoding="utf-8"))
        self.assertEqual(fm["recurrence"], "FREQ=MONTHLY;BYMONTHDAY=15")

    def test_create_without_recurrence_omits_field(self) -> None:
        self.engine.create("t1", "T", body="b")
        target = self.vault / "tasks" / "t1.md"
        fm, _ = self.core._parse_frontmatter(target.read_text(encoding="utf-8"))
        self.assertNotIn("recurrence", fm)

    def test_create_rejects_empty_recurrence(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", body="b", recurrence="")

    def test_create_rejects_control_chars_in_recurrence(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", body="b", recurrence="FREQ=DAILY\n\x00")

    def test_create_rejects_oversized_recurrence(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", body="b", recurrence="x" * (self.core.MAX_RECURRENCE_LEN + 1))

    def test_create_rejects_non_string_recurrence(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", body="b", recurrence=123)

    def test_create_recurrence_requires_profile_mapping(self) -> None:
        # Rewrite the profile without a recurrence mapping.
        d = copy.deepcopy(REAL_PROFILE_DATA)
        del d["fieldMapping"]["recurrence"]
        _write_profile(self.vault, data=d)
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", body="b", recurrence="FREQ=DAILY")

    def test_get_returns_modeled_fields(self) -> None:
        self.engine.create("t1", "T", body="b")
        result = self.engine.get("t1")
        self.assertEqual(result["slug"], "t1")
        self.assertEqual(result["title"], "T")
        self.assertIn("status", result)
        self.assertIn("body", result)
        self.assertIn("timeline", result)

    def test_list_via_engine(self) -> None:
        self.engine.create("t1", "T1", body="b")
        self.engine.create("t2", "T2", body="b")
        results = self.engine.list()
        slugs = {r["slug"] for r in results}
        self.assertIn("t1", slugs)
        self.assertIn("t2", slugs)

    def test_list_via_engine_with_status_filter(self) -> None:
        self.engine.create("t1", "T1", status="open", body="b")
        self.engine.create("t2", "T2", status="in-progress", body="b")
        results = self.engine.list(status="open")
        slugs = {r["slug"] for r in results}
        self.assertEqual(slugs, {"t1"})

    def test_list_via_engine_with_tag_filter(self) -> None:
        self.engine.create("t1", "T1", tags=["urgent"], body="b")
        self.engine.create("t2", "T2", body="b")
        results = self.engine.list(tag="urgent")
        slugs = {r["slug"] for r in results}
        self.assertEqual(slugs, {"t1"})

    def test_list_via_engine_with_archived_filter(self) -> None:
        self.engine.create("t1", "T1", body="b")
        self.engine.create("t2", "T2", body="b")
        self.engine.archive("t2")
        archived = self.engine.list(archived=True)
        non_archived = self.engine.list(archived=False)
        self.assertEqual({r["slug"] for r in archived}, {"t2"})
        self.assertEqual({r["slug"] for r in non_archived}, {"t1"})

    def test_update_success(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        result = self.engine.update("t1", status="in-progress", priority="high")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)

    def test_update_rejects_completed_status(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.update("t1", status="done")

    def test_update_rejects_completed_task(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        self.engine.complete("t1")
        with self.assertRaises(self.core.ValidationError):
            self.engine.update("t1", status="in-progress")

    def test_update_no_op_when_no_fields(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        result = self.engine.update("t1")
        self.assertEqual(result.state, self.core.NOT_APPLIED)

    def test_update_invalid_date_rejected(self) -> None:
        self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.update("t1", due="not-a-date")

    def test_update_can_explicitly_clear_optional_fields(self) -> None:
        self.engine.create(
            "t1",
            "T",
            due="2026-07-25",
            scheduled="2026-07-20",
            projects=["[[Project]]"],
            body="b",
        )
        result = self.engine.update(
            "t1", clear_due=True, clear_scheduled=True, clear_projects=True
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertNotIn("due", fm)
        self.assertNotIn("scheduled", fm)
        self.assertNotIn("projects", fm)

    def test_update_rejects_set_and_clear_together(self) -> None:
        self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.update("t1", due="2026-07-25", clear_due=True)

    def test_update_body_only_replaces_body(self) -> None:
        self.engine.create("t1", "T", status="open", body="original body")
        result = self.engine.update("t1", body="replaced body")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fetched = self.engine.get("t1")
        self.assertEqual(fetched["body"].strip(), "replaced body")
        # Status and title untouched.
        self.assertEqual(fetched["status"], "open")
        self.assertEqual(fetched["title"], "T")

    def test_update_body_empty_string_clears_body(self) -> None:
        self.engine.create("t1", "T", status="open", body="original body")
        result = self.engine.update("t1", body="")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fetched = self.engine.get("t1")
        self.assertEqual(fetched["body"].strip(), "")

    def test_update_body_none_preserves_existing_body(self) -> None:
        self.engine.create("t1", "T", status="open", body="keep me")
        result = self.engine.update("t1", status="in-progress")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fetched = self.engine.get("t1")
        self.assertEqual(fetched["body"].strip(), "keep me")
        self.assertEqual(fetched["status"], "in-progress")

    def test_update_body_only_is_not_no_op(self) -> None:
        # A body-only update must not hit the no-op guard.
        self.engine.create("t1", "T", status="open", body="original")
        result = self.engine.update("t1", body="changed")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)

    def test_update_body_empty_only_is_not_no_op(self) -> None:
        self.engine.create("t1", "T", status="open", body="original")
        result = self.engine.update("t1", body="")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)

    def test_update_combined_body_and_metadata(self) -> None:
        self.engine.create(
            "t1", "T", status="open", priority="normal",
            due="2026-07-25", body="original body",
        )
        result = self.engine.update(
            "t1", status="in-progress", priority="high", body="new body"
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fetched = self.engine.get("t1")
        self.assertEqual(fetched["status"], "in-progress")
        self.assertEqual(fetched["priority"], "high")
        self.assertEqual(fetched["body"].strip(), "new body")
        self.assertEqual(str(fetched["due"])[:10], "2026-07-25")

    def test_update_oversized_body_rejected(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.update("t1", body="x" * (self.core.MAX_BODY_LEN + 1))

    def test_update_body_preserves_frontmatter_and_timeline(self) -> None:
        self.engine.create(
            "t1", "T", status="open", body="original body",
            custom_fields={"pipeline_stage": "drafting"},
        )
        # Complete to seed a timeline event, then reopen is not allowed; so
        # just verify a body update preserves frontmatter (incl. custom field)
        # and the existing timeline (if any) verbatim.
        result = self.engine.update("t1", body="new body")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        disk_text = (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        fm, body = self.core._parse_frontmatter(disk_text)
        self.assertEqual(fm["status"], "open")
        self.assertEqual(fm["pipeline_stage"], "drafting")
        self.assertIn("new body", body)
        # Title remains in frontmatter (title edits are unsupported).
        self.assertEqual(fm["title"], "T")

    def test_complete_success_sets_completion_date(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        result = self.engine.complete("t1", completion_date="2026-07-18")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter((self.vault / "tasks" / "t1.md").read_text(encoding="utf-8"))
        self.assertEqual(fm["status"], "done")
        self.assertEqual(str(fm["completedDate"])[:10], "2026-07-18")

    def test_complete_defaults_to_today(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        result = self.engine.complete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter((self.vault / "tasks" / "t1.md").read_text(encoding="utf-8"))
        # Pinned gbrain normalizes bare dates to midnight UTC timestamps.
        self.assertRegex(str(fm["completedDate"]), r"^\d{4}-\d{2}-\d{2}")

    def test_complete_invalid_date_rejected(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.complete("t1", completion_date="not-a-date")

    def test_complete_idempotent_preserves_date(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        self.engine.complete("t1", completion_date="2026-07-18")
        result = self.engine.complete("t1", completion_date="2026-07-19")
        self.assertEqual(result.state, self.core.NOT_APPLIED)
        fm, _ = self.core._parse_frontmatter((self.vault / "tasks" / "t1.md").read_text(encoding="utf-8"))
        self.assertEqual(str(fm["completedDate"])[:10], "2026-07-18")

    def test_complete_retains_plain_scheduled_date_on_disk(self) -> None:
        """Regression for issue #107: completing a task with a bare
        ``scheduled`` date must retain exactly that plain ``YYYY-MM-DD``
        on disk, not the gbrain-normalized ``YYYY-MM-DDT00:00:00.000Z``
        form returned by ``get_page``.
        """
        self.engine.create(
            "t1", "T", status="open", scheduled="2026-08-10", body="b"
        )
        result = self.engine.complete("t1", completion_date="2026-08-10")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        disk_text = (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        fm, _ = self.core._parse_frontmatter(disk_text)
        # The scheduled date is preserved as the exact plain bare date.
        self.assertEqual(str(fm["scheduled"]), "2026-08-10")
        # No normalized timestamp form leaks onto disk for scheduled.
        self.assertNotIn("scheduled: 2026-08-10T00:00:00.000Z", disk_text)
        # Status/completedDate are updated as required.
        self.assertEqual(fm["status"], "done")
        self.assertEqual(str(fm["completedDate"]), "2026-08-10")

    def test_archive_success(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        result = self.engine.archive("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter((self.vault / "tasks" / "t1.md").read_text(encoding="utf-8"))
        self.assertIn("archived", fm["tags"])

    def test_archive_idempotent(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        self.engine.archive("t1")
        result = self.engine.archive("t1")
        self.assertEqual(result.state, self.core.NOT_APPLIED)

    def test_delete_success(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        target = self.vault / "tasks" / "t1.md"
        self.assertTrue(target.exists())

        result = self.engine.delete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertIsNotNone(result.commit_id)

        # File must be gone from disk.
        self.assertFalse(target.exists())
        # Idempotent delete verifies both gbrain and disk agree on absence.
        result2 = self.engine.delete("t1")
        self.assertEqual(result2.state, self.core.NOT_APPLIED)
        self.assertEqual(result2.detail, "task already deleted")

    def test_delete_idempotent(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        self.engine.delete("t1")
        result = self.engine.delete("t1")
        self.assertEqual(result.state, self.core.NOT_APPLIED)
        self.assertEqual(result.detail, "task already deleted")

    def test_delete_nonexistent(self) -> None:
        result = self.engine.delete("t1")
        self.assertEqual(result.state, self.core.NOT_APPLIED)
        self.assertEqual(result.detail, "task already deleted")

    def test_delete_dirty_target_refused(self) -> None:
        self.engine.create("t1", "T", status="open", body="b")
        target = self.vault / "tasks" / "t1.md"
        target.write_text(target.read_text(encoding="utf-8") + "\ndirty edit\n", encoding="utf-8")
        with self.assertRaises(self.core.ValidationError):
            self.engine.delete("t1")
        # File must still exist.
        self.assertTrue(target.exists())

    def test_delete_archived_task(self) -> None:
        """Delete also works on archived tasks (in the archive folder)."""
        self.engine.create("t1", "T", status="open", body="b")
        self.engine.archive("t1")
        result = self.engine.delete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        # Original file must be gone.
        target = self.vault / "tasks" / "t1.md"
        self.assertFalse(target.exists())

    def test_delete_gbrain_failure(self) -> None:
        behavior = {"delete_fail": ["tasks/t1"]}
        fail_tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.addCleanup(lambda: shutil.rmtree(fail_tmpdir, ignore_errors=True))
        engine, vault, gbrain_bin = _make_engine(self.core, fail_tmpdir, behavior)
        engine.create("t1", "T", status="open", body="b")
        with self.assertRaises(self.core.GbrainError):
            engine.delete("t1")

    def test_preflight_commits_pending_manual_edit(self) -> None:
        (self.vault / "tasks" / "manual.md").write_text("---\ntitle: Manual\ntags:\n  - task\n---\nbody\n", encoding="utf-8")
        self.engine.create("t1", "T", body="b")
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.vault), capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "")

    def test_unrelated_edit_remains_pending(self) -> None:
        self.engine.create("t1", "T", body="b")
        (self.vault / "tasks" / "unrelated.md").write_text("x", encoding="utf-8")
        r = subprocess.run(["git", "status", "--porcelain", "--", "tasks/unrelated.md"], cwd=str(self.vault), capture_output=True, text=True)
        self.assertIn("unrelated.md", r.stdout)

    def test_gbrain_sync_called_before_capture(self) -> None:
        self.engine.create("t1", "T", body="b")
        calls = _read_calls(self.tmpdir)
        cmds = [c["argv"][0] if c["argv"] else "" for c in calls]
        self.assertIn("sync", cmds)
        sync_idx = cmds.index("sync")
        capture_idx = cmds.index("capture")
        self.assertLess(sync_idx, capture_idx)

    def test_get_takes_lock(self) -> None:
        # get invokes gbrain, so it must take the lock. Verify by holding the
        # lock from another thread and observing get blocks until released.
        self.engine.create("t1", "T", body="b")
        holder_released = threading.Event()
        get_completed = threading.Event()
        get_started = threading.Event()

        def hold():
            with self.core.Lock(self.engine.lock_path, timeout=2.0):
                # Hold until get has started waiting, then release.
                get_started.wait(timeout=1.0)
                time.sleep(0.2)
                holder_released.set()

        def do_get():
            # Wait until holder has the lock.
            time.sleep(0.1)
            get_started.set()
            self.engine.get("t1")
            get_completed.set()

        t = threading.Thread(target=hold)
        gt = threading.Thread(target=do_get)
        t.start()
        gt.start()
        gt.join(timeout=3.0)
        t.join(timeout=3.0)
        self.assertTrue(get_completed.is_set(), "get should complete after lock released")


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class PreCaptureGuardTests(unittest.TestCase):
    """Mutation pre-capture guard tests with race hooks."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.engine, self.vault, self.gbrain_bin = _make_engine(self.core, self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_update_imports_and_preserves_preexisting_manual_edit(self) -> None:
        self.engine.create("t1", "T", body="b")
        # A manual edit that exists before the operation is committed and
        # imported by preflight sync, then preserved by the mutation.
        target = self.vault / "tasks" / "t1.md"
        target.write_text(target.read_text(encoding="utf-8") + "\nmanual edit\n", encoding="utf-8")
        result = self.engine.update("t1", status="in-progress")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertIn("manual edit", target.read_text(encoding="utf-8"))

    def test_update_profile_drift_before_capture_refused(self) -> None:
        self.engine.create("t1", "T", body="b")
        # Install a race hook that modifies the profile after get/reconstruct.
        def hook(eng, slug, profile):
            d = copy.deepcopy(REAL_PROFILE_DATA)
            d["defaultTaskStatus"] = "none"
            _write_profile(eng.vault, data=d)
        self.engine._pre_capture_hook = hook
        with self.assertRaises(self.core.ProfileIncompatible):
            self.engine.update("t1", status="in-progress")
        # No capture for the update.
        calls = _read_calls(self.tmpdir)
        capture_calls = [c for c in calls if c["argv"][0] == "capture"]
        self.assertEqual(len(capture_calls), 1)  # only the create

    def test_update_target_modified_after_get_refused(self) -> None:
        self.engine.create("t1", "T", body="b")
        # Race hook that dirties the target after get/reconstruct.
        def hook(eng, slug, profile):
            target = eng.vault / "tasks" / f"{slug}.md"
            target.write_text(target.read_text(encoding="utf-8") + "\nrace\n", encoding="utf-8")
        self.engine._pre_capture_hook = hook
        with self.assertRaises(self.core.ValidationError):
            self.engine.update("t1", status="in-progress")
        calls = _read_calls(self.tmpdir)
        capture_calls = [c for c in calls if c["argv"][0] == "capture"]
        self.assertEqual(len(capture_calls), 1)

    def test_complete_dirty_target_refused(self) -> None:
        self.engine.create("t1", "T", body="b")
        def hook(eng, slug, profile):
            target = eng.vault / "tasks" / f"{slug}.md"
            target.write_text(target.read_text(encoding="utf-8") + "\nrace\n", encoding="utf-8")
        self.engine._pre_capture_hook = hook
        with self.assertRaises(self.core.ValidationError):
            self.engine.complete("t1")
        calls = _read_calls(self.tmpdir)
        capture_calls = [c for c in calls if c["argv"][0] == "capture"]
        self.assertEqual(len(capture_calls), 1)

    def test_archive_dirty_target_refused(self) -> None:
        self.engine.create("t1", "T", body="b")
        def hook(eng, slug, profile):
            target = eng.vault / "tasks" / f"{slug}.md"
            target.write_text(target.read_text(encoding="utf-8") + "\nrace\n", encoding="utf-8")
        self.engine._pre_capture_hook = hook
        with self.assertRaises(self.core.ValidationError):
            self.engine.archive("t1")
        calls = _read_calls(self.tmpdir)
        capture_calls = [c for c in calls if c["argv"][0] == "capture"]
        self.assertEqual(len(capture_calls), 1)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class FailureRecoveryTests(unittest.TestCase):
    """Consolidated post-capture failure recovery and marker tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.engine, self.vault, self.gbrain_bin = _make_engine(self.core, self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_written_false_triggers_recovery(self) -> None:
        _set_behavior(self.tmpdir, {"vault": str(self.vault), "capture_fail": ["tasks/fail"]})
        result = self.engine.create("fail", "T", body="b")
        # Recovery: full sync + read-back. The file does not exist (write
        # failed), so read-back fails => recovery marker + recovery_required.
        self.assertEqual(result.state, self.core.RECOVERY_REQUIRED)
        self.assertTrue(self.engine.recovery_marker.exists())

    def test_capture_nonzero_returns_structured_outcome(self) -> None:
        _set_behavior(self.tmpdir, {"vault": str(self.vault), "capture_nonzero": ["tasks/t1"]})
        result = self.engine.create("t1", "T", body="b")
        # Capture invocation failed after starting; reconcile. No page in DB,
        # no disk file => recovery_required.
        self.assertEqual(result.state, self.core.RECOVERY_REQUIRED)

    def test_capture_invalid_json_returns_structured_outcome(self) -> None:
        _set_behavior(self.tmpdir, {"vault": str(self.vault), "capture_invalid_json": ["tasks/t1"]})
        result = self.engine.create("t1", "T", body="b")
        self.assertEqual(result.state, self.core.RECOVERY_REQUIRED)

    def test_capture_timeout_returns_structured_outcome(self) -> None:
        with mock.patch(
            "tasknotes_mcp_core.gbrain_capture",
            side_effect=self.core.SubprocessError("subprocess timed out"),
        ):
            result = self.engine.create("t1", "T", body="b")
        self.assertIn(result.state, (self.core.RECOVERY_REQUIRED,))

    def test_unexpected_capture_error_returns_structured_outcome(self) -> None:
        with mock.patch(
            "tasknotes_mcp_core.gbrain_capture",
            side_effect=RuntimeError("unexpected"),
        ):
            result = self.engine.create("t1", "T", body="b")
        self.assertEqual(result.state, self.core.RECOVERY_REQUIRED)

    def test_commit_failure_returns_applied_uncommitted(self) -> None:
        with mock.patch("tasknotes_mcp_core.git_commit_target", side_effect=self.core.GitError("commit failed")):
            result = self.engine.create("t1", "T", body="b")
        self.assertEqual(result.state, self.core.APPLIED_UNCOMMITTED)

    def test_unexpected_post_commit_error_returns_applied_uncommitted(self) -> None:
        with mock.patch(
            "tasknotes_mcp_core.git_head_id", side_effect=RuntimeError("unexpected")
        ):
            result = self.engine.create("t1", "T", body="b")
        self.assertEqual(result.state, self.core.APPLIED_UNCOMMITTED)

    def test_unexpected_recovery_error_returns_recovery_required(self) -> None:
        _set_behavior(self.tmpdir, {"vault": str(self.vault), "capture_fail": ["tasks/fail"]})
        with mock.patch(
            "tasknotes_mcp_core.gbrain_sync_full",
            side_effect=RuntimeError("unexpected"),
        ):
            result = self.engine.create("fail", "T", body="b")
        self.assertEqual(result.state, self.core.RECOVERY_REQUIRED)

    def test_recovery_marker_blocks_future_mutations(self) -> None:
        self.engine.recovery_marker.parent.mkdir(parents=True, exist_ok=True)
        self.engine.recovery_marker.write_text("x", encoding="utf-8")
        with self.assertRaises(self.core.RecoveryRequired):
            self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.RecoveryRequired):
            self.engine.update("t1", status="in-progress")
        with self.assertRaises(self.core.RecoveryRequired):
            self.engine.complete("t1")
        with self.assertRaises(self.core.RecoveryRequired):
            self.engine.archive("t1")

    def test_marker_write_failure_still_returns_recovery_required(self) -> None:
        _set_behavior(self.tmpdir, {"vault": str(self.vault), "capture_fail": ["tasks/fail"]})
        # Simulate an I/O failure only when the engine tries to persist the
        # recovery marker. The marker must not exist before the operation,
        # otherwise the fail-closed preflight correctly blocks the mutation.
        real_open = os.open

        def fail_marker_open(path, *args, **kwargs):
            if Path(path) == self.engine.recovery_marker:
                raise OSError("read-only")
            return real_open(path, *args, **kwargs)

        with mock.patch("tasknotes_mcp_core.os.open", side_effect=fail_marker_open):
            result = self.engine.create("fail", "T", body="b")
        self.assertEqual(result.state, self.core.RECOVERY_REQUIRED)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class SemanticVerificationTests(unittest.TestCase):
    """Strong semantic read-back verification tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.engine, self.vault, self.gbrain_bin = _make_engine(self.core, self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_semantic_documents_agree_on_create(self) -> None:
        self.engine.create("t1", "T", status="open", priority="normal", body="body")
        # After create, gbrain and disk should agree semantically.
        profile = self.engine.load_profile()
        gbrain_slug = self.core.resolve_gbrain_slug(profile, "t1")
        page = self.core.gbrain_get_page(self.engine.gbrain_bin, self.engine._gbrain_env, gbrain_slug, "default")
        gbrain_doc = self.core.semantic_from_gbrain(page, profile)
        disk_doc = self.core.semantic_from_disk(self.vault, profile, "t1")
        self.assertTrue(self.core.semantic_documents_agree(gbrain_doc, disk_doc, profile))

    def test_semantic_excludes_provenance(self) -> None:
        self.engine.create("t1", "T", body="b")
        # Disk has provenance keys; gbrain get_page does not. Semantic
        # comparison must exclude them.
        disk_text = (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        self.assertIn("ingested_via", disk_text)
        # capture provenance is also injected on disk and must be excluded.
        self.assertIn("captured_via", disk_text)
        profile = self.engine.load_profile()
        disk_doc = self.core.semantic_from_disk(self.vault, profile, "t1")
        self.assertNotIn("ingested_via", disk_doc.frontmatter)
        self.assertNotIn("ingested_at", disk_doc.frontmatter)
        self.assertNotIn("source_kind", disk_doc.frontmatter)
        self.assertNotIn("captured_via", disk_doc.frontmatter)
        self.assertNotIn("captured_at", disk_doc.frontmatter)

    def test_semantic_preserves_unknown_frontmatter(self) -> None:
        # Create a task with a custom field via the fake gbrain by writing
        # the disk file directly is not allowed; instead use create with
        # extra tags and verify they survive.
        self.engine.create("t1", "T", tags=["custom-tag"], body="b")
        profile = self.engine.load_profile()
        gbrain_slug = self.core.resolve_gbrain_slug(profile, "t1")
        page = self.core.gbrain_get_page(self.engine.gbrain_bin, self.engine._gbrain_env, gbrain_slug, "default")
        gbrain_doc = self.core.semantic_from_gbrain(page, profile)
        disk_doc = self.core.semantic_from_disk(self.vault, profile, "t1")
        self.assertIn("custom-tag", gbrain_doc.tags)
        self.assertIn("custom-tag", disk_doc.tags)
        self.assertTrue(self.core.semantic_documents_agree(gbrain_doc, disk_doc, profile))

    def test_semantic_preserves_body_and_timeline(self) -> None:
        # Create a task, then verify body is preserved on disk.
        self.engine.create("t1", "T", body="my unique body text")
        disk_text = (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        self.assertIn("my unique body text", disk_text)

    def test_create_body_preserved_in_immediate_readback(self) -> None:
        # Body must be preserved immediately after create, not only later.
        unique = "immediate readback body marker"
        result = self.engine.create("t1", "T", body=unique)
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fetched = self.engine.get("t1")
        self.assertEqual(fetched["body"].strip(), unique)

    def test_update_preserves_body_through_capture(self) -> None:
        # An update must not drop the body through the capture write-through.
        self.engine.create("t1", "T", body="original body kept")
        result = self.engine.update("t1", status="in-progress")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fetched = self.engine.get("t1")
        self.assertEqual(fetched["body"].strip(), "original body kept")
        self.assertEqual(fetched["status"], "in-progress")


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class DirtyTargetTests(unittest.TestCase):
    """Dirty target refusal tests (mutation-path, not helper-only)."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.vault = _make_vault(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_git_target_clean_true_when_committed(self) -> None:
        target = self.vault / "tasks" / "t.md"
        target.write_text("x", encoding="utf-8")
        self.core.git_commit_target(self.vault, target, self.core._build_git_env())
        self.assertTrue(self.core.git_target_clean(self.vault, target, self.core._build_git_env()))

    def test_git_target_clean_false_when_dirty(self) -> None:
        target = self.vault / "tasks" / "t.md"
        target.write_text("x", encoding="utf-8")
        self.core.git_commit_target(self.vault, target, self.core._build_git_env())
        target.write_text("y", encoding="utf-8")
        self.assertFalse(self.core.git_target_clean(self.vault, target, self.core._build_git_env()))


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class CustomFieldsTests(unittest.TestCase):
    """Custom user field validation and create/update tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.engine, self.vault, self.gbrain_bin = _make_engine(self.core, self.tmpdir)
        self.profile = self.engine.load_profile()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_profile_loads_user_fields(self) -> None:
        keys = {uf["key"] for uf in self.profile.user_fields}
        self.assertIn("pipeline_stage", keys)
        self.assertIn("tags_extra", keys)
        self.assertIn("team", keys)
        types = {uf["key"]: uf["type"] for uf in self.profile.user_fields}
        self.assertEqual(types["pipeline_stage"], "text")
        self.assertEqual(types["tags_extra"], "list")
        self.assertEqual(types["effort_hours"], "number")
        self.assertEqual(types["blocked"], "boolean")
        self.assertEqual(types["review_date"], "date")
        self.assertEqual(types["related"], "link")
        self.assertEqual(types["team"], "enum")
        # Verify enum field carries options.
        team_field = {uf["key"]: uf for uf in self.profile.user_fields}["team"]
        self.assertIn("options", team_field)
        self.assertEqual(
            team_field["options"], ["engineering", "design", "product", "marketing"]
        )

    def test_create_writes_custom_fields_to_frontmatter(self) -> None:
        result = self.engine.create(
            "t1", "T", body="b",
            custom_fields={"pipeline_stage": "drafting", "tags_extra": ["x", "y"]},
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["pipeline_stage"], "drafting")
        self.assertEqual(fm["tags_extra"], ["x", "y"])

    def test_update_updates_custom_fields(self) -> None:
        self.engine.create(
            "t1", "T", body="b",
            custom_fields={"pipeline_stage": "drafting"},
        )
        result = self.engine.update(
            "t1", custom_fields={"pipeline_stage": "review"},
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["pipeline_stage"], "review")

    def test_update_none_clears_custom_field(self) -> None:
        self.engine.create(
            "t1", "T", body="b",
            custom_fields={"pipeline_stage": "drafting"},
        )
        result = self.engine.update(
            "t1", custom_fields={"pipeline_stage": None},
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertNotIn("pipeline_stage", fm)

    def test_create_rejects_unknown_custom_field(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b",
                custom_fields={"not_a_field": "x"},
            )

    def test_update_rejects_unknown_custom_field(self) -> None:
        self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.update(
                "t1", custom_fields={"not_a_field": "x"},
            )

    def test_create_rejects_wrong_type_for_number(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b",
                custom_fields={"effort_hours": "not-a-number"},
            )

    def test_create_rejects_wrong_type_for_boolean(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b",
                custom_fields={"blocked": "yes"},
            )

    def test_create_rejects_wrong_type_for_list(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b",
                custom_fields={"tags_extra": "not-a-list"},
            )

    def test_create_rejects_wrong_type_for_date(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b",
                custom_fields={"review_date": "not-a-date"},
            )

    def test_create_rejects_wrong_type_for_text(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b",
                custom_fields={"pipeline_stage": 123},
            )

    def test_create_accepts_number_and_boolean(self) -> None:
        result = self.engine.create(
            "t1", "T", body="b",
            custom_fields={"effort_hours": 3.5, "blocked": True},
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["effort_hours"], 3.5)
        self.assertIs(fm["blocked"], True)

    def test_create_accepts_date_and_link(self) -> None:
        result = self.engine.create(
            "t1", "T", body="b",
            custom_fields={"review_date": "2026-07-20", "related": "[[Note]]"},
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(str(fm["review_date"])[:10], "2026-07-20")
        self.assertEqual(fm["related"], "[[Note]]")

    def test_create_accepts_valid_enum_value(self) -> None:
        result = self.engine.create(
            "t1", "T", body="b",
            custom_fields={"team": "engineering"},
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["team"], "engineering")

    def test_create_rejects_invalid_enum_value(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b",
                custom_fields={"team": "legal"},
            )

    def test_create_rejects_enum_value_wrong_type(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b",
                custom_fields={"team": 123},
            )

    def test_update_accepts_valid_enum_value(self) -> None:
        self.engine.create(
            "t1", "T", body="b",
            custom_fields={"team": "engineering"},
        )
        result = self.engine.update(
            "t1", custom_fields={"team": "design"},
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["team"], "design")

    def test_update_rejects_invalid_enum_value(self) -> None:
        self.engine.create(
            "t1", "T", body="b",
            custom_fields={"team": "engineering"},
        )
        with self.assertRaises(self.core.ValidationError):
            self.engine.update(
                "t1", custom_fields={"team": "legal"},
            )

    def test_update_none_clears_enum_field(self) -> None:
        self.engine.create(
            "t1", "T", body="b",
            custom_fields={"team": "engineering"},
        )
        result = self.engine.update(
            "t1", custom_fields={"team": None},
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertNotIn("team", fm)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class PlannedWeekTests(unittest.TestCase):
    """Semantic week-planning model (issue #128).

    Covers the three planning states (Backlog / week-planned / day-
    scheduled), Monday-only validation, the reserved custom-field key,
    the profile ``userFields`` date prerequisite, create/update
    transitions and conflicts, scheduled-wins normalization of manually
    inconsistent pairs across mutation paths, idempotency, unrelated
    custom-field preservation, and structured get/list exposure.
    """

    MONDAY = "2026-08-24"
    NEXT_MONDAY = "2026-08-31"
    TUESDAY = "2026-08-25"

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.engine, self.vault, self.gbrain_bin = _make_engine(self.core, self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _disk_fm(self, slug: str) -> dict:
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / f"{slug}.md").read_text(encoding="utf-8")
        )
        return fm

    def _rewrite_disk_fm(self, slug: str, extra: dict) -> None:
        """Hand-edit task frontmatter (simulates a manual Obsidian edit)."""
        import yaml
        path = self.vault / "tasks" / f"{slug}.md"
        fm, body = self.core._parse_frontmatter(path.read_text(encoding="utf-8"))
        fm.update(extra)
        fm_text = yaml.safe_dump(
            fm, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
        path.write_text(f"---\n{fm_text}---\n{body}", encoding="utf-8")

    def _profile_without_planned_week(self) -> dict:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        d["userFields"] = [
            uf for uf in d["userFields"] if uf["key"] != "planned_week"
        ]
        return d

    def _profile_with_wrong_planned_week_type(self) -> dict:
        d = copy.deepcopy(REAL_PROFILE_DATA)
        for uf in d["userFields"]:
            if uf["key"] == "planned_week":
                uf["type"] = "text"
        return d

    def _gbrain_cmds(self) -> List[str]:
        return [c["argv"][0] if c["argv"] else "" for c in _read_calls(self.tmpdir)]

    # -- create states -----------------------------------------------------

    def test_create_backlog_omits_planning_fields(self) -> None:
        result = self.engine.create("t1", "T", body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertNotIn("scheduled", fm)
        self.assertNotIn("planned_week", fm)

    def test_create_day_scheduled_writes_scheduled_only(self) -> None:
        self.engine.create("t1", "T", scheduled="2026-09-01", body="b")
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["scheduled"]), "2026-09-01")
        self.assertNotIn("planned_week", fm)

    def test_create_week_planned_writes_monday_planned_week(self) -> None:
        self.engine.create("t1", "T", planned_week=self.MONDAY, body="b")
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["planned_week"]), self.MONDAY)
        self.assertNotIn("scheduled", fm)

    def test_create_rejects_both_planning_targets(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", scheduled="2026-09-01", planned_week=self.MONDAY, body="b"
            )
        self.assertNotIn("capture", self._gbrain_cmds())

    def test_create_rejects_non_monday_planned_week(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", planned_week=self.TUESDAY, body="b")

    def test_create_rejects_invalid_planned_week_date(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", planned_week="not-a-date", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", planned_week="2026-13-45", body="b")

    # -- profile prerequisite -----------------------------------------------

    def test_create_requires_profile_planned_week_field(self) -> None:
        _write_profile(self.vault, data=self._profile_without_planned_week())
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", planned_week=self.MONDAY, body="b")
        cmds = self._gbrain_cmds()
        self.assertNotIn("sync", cmds)
        self.assertNotIn("capture", cmds)

    def test_create_requires_date_type_for_planned_week(self) -> None:
        _write_profile(self.vault, data=self._profile_with_wrong_planned_week_type())
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "T", planned_week=self.MONDAY, body="b")

    def test_update_requires_profile_planned_week_field(self) -> None:
        self.engine.create("t1", "T", body="b")
        _write_profile(self.vault, data=self._profile_without_planned_week())
        with self.assertRaises(self.core.ValidationError):
            self.engine.update("t1", planned_week=self.MONDAY)

    # -- reserved custom-field key ------------------------------------------

    def test_custom_fields_reject_reserved_planned_week_key(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b", custom_fields={"planned_week": self.MONDAY}
            )
        with self.assertRaises(self.core.ValidationError):
            self.engine.create(
                "t1", "T", body="b", custom_fields={"planned_week": None}
            )
        self.engine.create("t2", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.update(
                "t2", custom_fields={"planned_week": self.MONDAY}
            )
        with self.assertRaises(self.core.ValidationError):
            self.engine.update("t2", custom_fields={"planned_week": None})

    # -- update transitions --------------------------------------------------

    def test_update_sets_week_planned_from_backlog(self) -> None:
        self.engine.create("t1", "T", body="b")
        result = self.engine.update("t1", planned_week=self.MONDAY)
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["planned_week"]), self.MONDAY)
        self.assertNotIn("scheduled", fm)

    def test_update_scheduled_clears_planned_week(self) -> None:
        self.engine.create("t1", "T", planned_week=self.MONDAY, body="b")
        result = self.engine.update("t1", scheduled="2026-09-01")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["scheduled"]), "2026-09-01")
        self.assertNotIn("planned_week", fm)

    def test_update_planned_week_clears_scheduled(self) -> None:
        self.engine.create("t1", "T", scheduled="2026-09-01", body="b")
        result = self.engine.update("t1", planned_week=self.MONDAY)
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["planned_week"]), self.MONDAY)
        self.assertNotIn("scheduled", fm)

    def test_update_clear_scheduled_removes_both_targets(self) -> None:
        self.engine.create("t1", "T", planned_week=self.MONDAY, body="b")
        result = self.engine.update("t1", clear_scheduled=True)
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertNotIn("scheduled", fm)
        self.assertNotIn("planned_week", fm)

    def test_update_clear_planned_week_yields_backlog(self) -> None:
        self.engine.create("t1", "T", planned_week=self.MONDAY, body="b")
        result = self.engine.update("t1", clear_planned_week=True)
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertNotIn("planned_week", fm)
        self.assertNotIn("scheduled", fm)

    def test_update_clear_planned_week_retains_manual_scheduled(self) -> None:
        self.engine.create("t1", "T", scheduled="2026-09-01", body="b")
        self._rewrite_disk_fm("t1", {"planned_week": self.MONDAY})
        result = self.engine.update("t1", clear_planned_week=True)
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["scheduled"]), "2026-09-01")
        self.assertNotIn("planned_week", fm)

    def test_update_rejects_contradictory_planning_combinations(self) -> None:
        self.engine.create("t1", "T", body="b")
        captures_before = self._gbrain_cmds().count("capture")
        cases = [
            {"planned_week": self.MONDAY, "clear_planned_week": True},
            {"scheduled": "2026-09-01", "planned_week": self.MONDAY},
            {"clear_scheduled": True, "planned_week": self.MONDAY},
            {"clear_planned_week": True, "scheduled": "2026-09-01"},
        ]
        for kwargs in cases:
            with self.subTest(**kwargs):
                with self.assertRaises(self.core.ValidationError):
                    self.engine.update("t1", **kwargs)
        self.assertEqual(self._gbrain_cmds().count("capture"), captures_before)

    def test_update_rejects_non_monday_planned_week(self) -> None:
        self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.update("t1", planned_week=self.TUESDAY)

    def test_update_planned_week_idempotent(self) -> None:
        self.engine.create("t1", "T", planned_week=self.MONDAY, body="b")
        result = self.engine.update("t1", planned_week=self.MONDAY)
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["planned_week"]), self.MONDAY)
        self.assertNotIn("scheduled", fm)
        result = self.engine.update("t1", planned_week=self.NEXT_MONDAY)
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["planned_week"]), self.NEXT_MONDAY)
        self.assertNotIn("scheduled", fm)

    # -- scheduled-wins normalization on rewrites -----------------------------

    def test_unrelated_update_normalizes_manual_inconsistent_pair(self) -> None:
        self.engine.create("t1", "T", scheduled="2026-09-01", body="b")
        self._rewrite_disk_fm("t1", {"planned_week": self.MONDAY})
        result = self.engine.update("t1", priority="high")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["scheduled"]), "2026-09-01")
        self.assertNotIn("planned_week", fm)
        self.assertEqual(fm["priority"], "high")

    def test_complete_normalizes_manual_inconsistent_pair(self) -> None:
        self.engine.create("t1", "T", scheduled="2026-09-01", body="b")
        self._rewrite_disk_fm("t1", {"planned_week": self.MONDAY})
        result = self.engine.complete("t1", completion_date="2026-09-01")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(fm["status"], "done")
        self.assertEqual(str(fm["scheduled"]), "2026-09-01")
        self.assertNotIn("planned_week", fm)

    def test_normalization_works_without_profile_planned_week_definition(self) -> None:
        _write_profile(self.vault, data=self._profile_without_planned_week())
        self.engine.create("t1", "T", scheduled="2026-09-01", body="b")
        self._rewrite_disk_fm("t1", {"planned_week": self.MONDAY})
        result = self.engine.update("t1", priority="high")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(str(fm["scheduled"]), "2026-09-01")
        self.assertNotIn("planned_week", fm)

    # -- unrelated custom fields and structured output ------------------------

    def test_planned_week_preserves_unrelated_custom_fields(self) -> None:
        self.engine.create(
            "t1", "T", body="b", custom_fields={"pipeline_stage": "drafting"}
        )
        result = self.engine.update("t1", planned_week=self.MONDAY)
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm = self._disk_fm("t1")
        self.assertEqual(fm["pipeline_stage"], "drafting")
        self.assertEqual(str(fm["planned_week"]), self.MONDAY)

    def test_get_exposes_planned_week_only_when_present(self) -> None:
        self.engine.create("week", "W", planned_week=self.MONDAY, body="b")
        self.engine.create("day", "D", scheduled="2026-09-01", body="b")
        self.engine.create("backlog", "B", body="b")
        fetched = self.engine.get("week")
        self.assertIn("planned_week", fetched)
        self.assertEqual(str(fetched["planned_week"])[:10], self.MONDAY)
        self.assertNotIn("planned_week", self.engine.get("day"))
        self.assertNotIn("planned_week", self.engine.get("backlog"))

    def test_list_exposes_planned_week_only_when_present(self) -> None:
        self.engine.create("week", "W", planned_week=self.MONDAY, body="b")
        self.engine.create("day", "D", scheduled="2026-09-01", body="b")
        self.engine.create("backlog", "B", body="b")
        rows = {r["slug"]: r for r in self.engine.list()}
        self.assertIn("planned_week", rows["week"])
        self.assertEqual(str(rows["week"]["planned_week"])[:10], self.MONDAY)
        self.assertNotIn("planned_week", rows["day"])
        self.assertNotIn("planned_week", rows["backlog"])

    def test_structured_outputs_hide_unrelated_user_fields(self) -> None:
        self.engine.create(
            "t1", "T", planned_week=self.MONDAY, body="b",
            custom_fields={"review_date": "2026-09-01"},
        )
        fetched = self.engine.get("t1")
        self.assertNotIn("review_date", fetched)
        row = next(r for r in self.engine.list() if r["slug"] == "t1")
        self.assertNotIn("review_date", row)
        self.assertIn("planned_week", row)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class TagMutationTests(unittest.TestCase):
    """add_tag / remove_tag engine method tests."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_"))
        self.engine, self.vault, self.gbrain_bin = _make_engine(self.core, self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_tag_adds_custom_tag(self) -> None:
        self.engine.create("t1", "T", body="b")
        result = self.engine.add_tag("t1", "urgent")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertIn("urgent", fm["tags"])

    def test_add_tag_idempotent(self) -> None:
        self.engine.create("t1", "T", body="b")
        self.engine.add_tag("t1", "urgent")
        result = self.engine.add_tag("t1", "urgent")
        self.assertEqual(result.state, self.core.NOT_APPLIED)

    def test_add_tag_rejects_task_identification_tag(self) -> None:
        self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.add_tag("t1", "task")

    def test_add_tag_rejects_archive_tag(self) -> None:
        self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.add_tag("t1", "archived")

    def test_add_tag_rejects_invalid_tag_format(self) -> None:
        self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.add_tag("t1", "has space")
        with self.assertRaises(self.core.ValidationError):
            self.engine.add_tag("t1", "")

    def test_remove_tag_removes_custom_tag(self) -> None:
        self.engine.create("t1", "T", body="b", tags=["urgent"])
        result = self.engine.remove_tag("t1", "urgent")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertNotIn("urgent", fm["tags"])

    def test_remove_tag_idempotent(self) -> None:
        self.engine.create("t1", "T", body="b", tags=["urgent"])
        self.engine.remove_tag("t1", "urgent")
        result = self.engine.remove_tag("t1", "urgent")
        self.assertEqual(result.state, self.core.NOT_APPLIED)

    def test_remove_tag_rejects_task_identification_tag(self) -> None:
        self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.remove_tag("t1", "task")

    def test_remove_tag_rejects_archive_tag(self) -> None:
        self.engine.create("t1", "T", body="b")
        with self.assertRaises(self.core.ValidationError):
            self.engine.remove_tag("t1", "archived")


# ---------------------------------------------------------------------------
# Daily Notes primitives (issue #139, W1a)
# ---------------------------------------------------------------------------


def _make_plain_vault(tmpdir: Path, name: str = "vault") -> Path:
    """Bare vault directory with .obsidian (no git, no TaskNotes profile)."""
    vault = tmpdir / name
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    return vault


def _write_daily_config(vault: Path, config: dict) -> None:
    (vault / ".obsidian" / "daily-notes.json").write_text(
        json.dumps(config), encoding="utf-8"
    )


class DailyNotesConfigTests(unittest.TestCase):
    """Strict lazy reader for .obsidian/daily-notes.json."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_cfg_"))
        self.vault = _make_plain_vault(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_config_file_fails_closed(self) -> None:
        with self.assertRaises(self.core.CoreError):
            self.core.load_daily_notes_config(self.vault)

    def test_missing_obsidian_dir_fails_closed(self) -> None:
        vault = self.tmpdir / "bare"
        vault.mkdir()
        with self.assertRaises(self.core.CoreError):
            self.core.load_daily_notes_config(vault)

    def test_valid_config_parsed(self) -> None:
        (self.vault / "journal").mkdir()
        (self.vault / "templates").mkdir()
        (self.vault / "templates" / "daily.md").write_text(
            "# {{date}}\n", encoding="utf-8"
        )
        _write_daily_config(self.vault, {
            "folder": "journal",
            "format": "YYYY/MM/DD",
            "template": "templates/daily.md",
        })
        cfg = self.core.load_daily_notes_config(self.vault)
        self.assertEqual(cfg.folder, "journal")
        self.assertEqual(cfg.format, "YYYY/MM/DD")
        self.assertEqual(cfg.template, "templates/daily.md")

    def test_empty_and_null_values_fall_back_to_defaults(self) -> None:
        _write_daily_config(self.vault, {
            "folder": "",
            "format": None,
            "template": None,
        })
        cfg = self.core.load_daily_notes_config(self.vault)
        self.assertEqual(cfg.folder, "")
        self.assertEqual(cfg.format, "YYYY-MM-DD")
        self.assertIsNone(cfg.template)

    def test_unknown_keys_ignored(self) -> None:
        _write_daily_config(self.vault, {
            "folder": "journal",
            "autorun": True,
            "somethingElse": {"nested": 1},
        })
        cfg = self.core.load_daily_notes_config(self.vault)
        self.assertEqual(cfg.folder, "journal")

    def test_malformed_json_rejected(self) -> None:
        (self.vault / ".obsidian" / "daily-notes.json").write_text(
            "{not json", encoding="utf-8"
        )
        with self.assertRaises(self.core.ValidationError):
            self.core.load_daily_notes_config(self.vault)

    def test_non_object_root_rejected(self) -> None:
        (self.vault / ".obsidian" / "daily-notes.json").write_text(
            '["not", "an", "object"]', encoding="utf-8"
        )
        with self.assertRaises(self.core.ValidationError):
            self.core.load_daily_notes_config(self.vault)

    def test_folder_absolute_rejected(self) -> None:
        _write_daily_config(self.vault, {"folder": "/etc"})
        with self.assertRaises(self.core.PathError):
            self.core.load_daily_notes_config(self.vault)

    def test_folder_traversal_rejected(self) -> None:
        for folder in ("..", "a/../b", "."):
            _write_daily_config(self.vault, {"folder": folder})
            with self.assertRaises(self.core.PathError):
                self.core.load_daily_notes_config(self.vault)

    def test_folder_empty_segments_rejected(self) -> None:
        for folder in ("a//b", "a/", "/a"):
            _write_daily_config(self.vault, {"folder": folder})
            with self.assertRaises(self.core.PathError):
                self.core.load_daily_notes_config(self.vault)

    def test_folder_backslash_rejected(self) -> None:
        _write_daily_config(self.vault, {"folder": "a\\b"})
        with self.assertRaises(self.core.PathError):
            self.core.load_daily_notes_config(self.vault)

    def test_folder_control_char_rejected(self) -> None:
        _write_daily_config(self.vault, {"folder": "a\x0bb"})
        with self.assertRaises(self.core.PathError):
            self.core.load_daily_notes_config(self.vault)

    def test_folder_non_string_rejected(self) -> None:
        _write_daily_config(self.vault, {"folder": 5})
        with self.assertRaises(self.core.ValidationError):
            self.core.load_daily_notes_config(self.vault)

    def test_folder_missing_dir_accepted(self) -> None:
        _write_daily_config(self.vault, {"folder": "does-not-exist-yet"})
        cfg = self.core.load_daily_notes_config(self.vault)
        self.assertEqual(cfg.folder, "does-not-exist-yet")

    def test_folder_existing_dir_accepted(self) -> None:
        (self.vault / "journal").mkdir()
        _write_daily_config(self.vault, {"folder": "journal"})
        cfg = self.core.load_daily_notes_config(self.vault)
        self.assertEqual(cfg.folder, "journal")

    def test_folder_symlink_component_rejected(self) -> None:
        (self.vault / "real-dir").mkdir()
        os.symlink("real-dir", str(self.vault / "alias"))
        _write_daily_config(self.vault, {"folder": "alias"})
        with self.assertRaises(self.core.PathError):
            self.core.load_daily_notes_config(self.vault)

    def test_format_non_string_rejected(self) -> None:
        _write_daily_config(self.vault, {"format": ["YYYY"]})
        with self.assertRaises(self.core.ValidationError):
            self.core.load_daily_notes_config(self.vault)

    def test_format_unsupported_syntax_rejected(self) -> None:
        for fmt in ("YYYY MMM", "YYYY-MM-DD-dddd", "woop"):
            _write_daily_config(self.vault, {"format": fmt})
            with self.assertRaises(self.core.ValidationError):
                self.core.load_daily_notes_config(self.vault)

    def test_template_non_string_rejected(self) -> None:
        _write_daily_config(self.vault, {"template": 42})
        with self.assertRaises(self.core.ValidationError):
            self.core.load_daily_notes_config(self.vault)

    def test_template_unsafe_paths_rejected(self) -> None:
        for template in ("/tmp/t.md", "a\\b.md", "../evil.md", "a//b.md", "notes.txt"):
            _write_daily_config(self.vault, {"template": template})
            with self.assertRaises(self.core.PathError):
                self.core.load_daily_notes_config(self.vault)

    def test_template_missing_file_accepted_at_load(self) -> None:
        _write_daily_config(self.vault, {"template": "templates/daily.md"})
        cfg = self.core.load_daily_notes_config(self.vault)
        self.assertEqual(cfg.template, "templates/daily.md")

    def test_template_symlink_rejected(self) -> None:
        (self.vault / "templates").mkdir()
        (self.vault / "templates" / "real.md").write_text("x", encoding="utf-8")
        os.symlink("real.md", str(self.vault / "templates" / "alias.md"))
        _write_daily_config(self.vault, {"template": "templates/alias.md"})
        with self.assertRaises(self.core.PathError):
            self.core.load_daily_notes_config(self.vault)


class DailyNotesConfigVarianceTests(unittest.TestCase):
    """D13 core config variance: folder roots, nesting, formats, rejections.

    Covers the config-shape matrix the projection must survive: root/
    named/nested folders, a non-directory component on the configured
    path, the documented format subset (including same-target monthly
    composition), and locale/week/traversal rejection.
    """

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_var_"))
        self.vault = _make_plain_vault(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- root folder variance -------------------------------------------------

    def test_folder_key_missing_defaults_to_vault_root(self) -> None:
        _write_daily_config(self.vault, {"format": "YYYY-MM-DD"})
        cfg = self.core.load_daily_notes_config(self.vault)
        self.assertEqual(cfg.folder, "")
        path = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")
        self.assertEqual(path, self.vault / "2026-08-28.md")

    # -- named and nested folders ----------------------------------------------

    def test_single_named_folder_accepted(self) -> None:
        _write_daily_config(self.vault, {"folder": "Daily"})
        cfg = self.core.load_daily_notes_config(self.vault)
        self.assertEqual(cfg.folder, "Daily")
        path = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")
        self.assertEqual(path, self.vault / "Daily" / "2026-08-28.md")

    def test_nested_folder_path_accepted(self) -> None:
        _write_daily_config(self.vault, {"folder": "Journal/Daily"})
        cfg = self.core.load_daily_notes_config(self.vault)
        self.assertEqual(cfg.folder, "Journal/Daily")
        path = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")
        self.assertEqual(
            path, self.vault / "Journal" / "Daily" / "2026-08-28.md"
        )

    def test_non_directory_folder_component_rejected(self) -> None:
        # An existing regular file occupies a configured folder component;
        # the strict loader rejects it fail-closed (symlink components are
        # covered by DailyNotesConfigTests).
        (self.vault / "Journal").write_text("not a dir\n", encoding="utf-8")
        _write_daily_config(self.vault, {"folder": "Journal/Daily"})
        with self.assertRaises(self.core.PathError):
            self.core.load_daily_notes_config(self.vault)

    # -- documented format subset ------------------------------------------------

    def test_documented_formats_resolve_expected_filenames(self) -> None:
        cases = {
            "YYYY-MM-DD": "2026-08-28.md",
            "DD.MM.YY": "28.08.26.md",
            "YYYY_M_D": "2026_8_28.md",
            "YYYY/MM/DD": "2026/08/28.md",
        }
        for fmt, expected in cases.items():
            cfg = self.core.DailyNotesConfig(folder="journal", format=fmt)
            path = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")
            self.assertEqual(
                path,
                self.vault.joinpath(*("journal", *expected.split("/"))),
                msg=f"format {fmt!r}",
            )

    def test_monthly_format_resolves_same_target_for_sibling_days(self) -> None:
        # Same-target basis (R4): YYYY-MM maps sibling days of one month
        # onto a single note path, so a D1 -> D2 transition composes to
        # exactly one ensure.
        cfg = self.core.DailyNotesConfig(folder="journal", format="YYYY-MM")
        d1 = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-27")
        d2 = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")
        self.assertEqual(d1, d2)
        self.assertEqual(d1, self.vault / "journal" / "2026-08.md")
        steps = self.core._daily_link_plan("2026-08-27", "2026-08-28")
        composed = self.core._compose_daily_link_plan_by_target(
            self.vault, cfg, steps
        )
        self.assertEqual(
            composed,
            [(self.core.DAILY_PROJECTION_OP_ENSURE, "2026-08-28")],
        )

    # -- unsupported locale/week/traversal rejection ------------------------------

    def test_locale_and_week_tokens_rejected(self) -> None:
        for fmt in ("MMMM", "MMM YYYY", "wo", "YYYY [w] wo", "dddd DD MM"):
            _write_daily_config(self.vault, {"format": fmt})
            with self.assertRaises(self.core.ValidationError):
                self.core.load_daily_notes_config(self.vault)

    def test_traversal_format_rejected_at_resolution(self) -> None:
        # The format literal set permits '.', so a traversal-shaped
        # format is rejected at path resolution: the resolved relative
        # path must never contain '..' segments.
        cfg = self.core.DailyNotesConfig(folder="journal", format="YYYY/../../MM")
        with self.assertRaises(self.core.PathError):
            self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")


class DailyNoteTemplateReadTests(unittest.TestCase):
    """No-follow bounded template reading."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_read_"))
        self.vault = _make_plain_vault(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_none_when_unconfigured(self) -> None:
        cfg = self.core.DailyNotesConfig()
        self.assertIsNone(self.core.read_daily_note_template(self.vault, cfg))

    def test_reads_template_content(self) -> None:
        (self.vault / "templates").mkdir()
        (self.vault / "templates" / "daily.md").write_text(
            "# {{date}}\n## Tasks\n", encoding="utf-8"
        )
        cfg = self.core.DailyNotesConfig(template="templates/daily.md")
        self.assertEqual(
            self.core.read_daily_note_template(self.vault, cfg),
            "# {{date}}\n## Tasks\n",
        )

    def test_missing_template_file_rejected_at_read(self) -> None:
        cfg = self.core.DailyNotesConfig(template="templates/ghost.md")
        with self.assertRaises(self.core.PathError):
            self.core.read_daily_note_template(self.vault, cfg)

    def test_symlinked_template_rejected_at_read(self) -> None:
        (self.vault / "templates").mkdir()
        (self.vault / "templates" / "real.md").write_text("x", encoding="utf-8")
        os.symlink("real.md", str(self.vault / "templates" / "alias.md"))
        cfg = self.core.DailyNotesConfig(template="templates/alias.md")
        with self.assertRaises(self.core.PathError):
            self.core.read_daily_note_template(self.vault, cfg)


class DailyNoteFormatTests(unittest.TestCase):
    """Deterministic date formatter (strict token subset)."""

    def setUp(self) -> None:
        self.core = _load_core()

    def test_default_format(self) -> None:
        self.assertEqual(
            self.core.format_daily_note_date("2026-08-28", "YYYY-MM-DD"),
            "2026-08-28",
        )

    def test_each_token(self) -> None:
        cases = {
            "YYYY": "2026",
            "YY": "26",
            "MM": "08",
            "M": "8",
            "DD": "28",
            "D": "28",
        }
        for token, expected in cases.items():
            self.assertEqual(
                self.core.format_daily_note_date("2026-08-28", token), expected
            )

    def test_yy_zero_padding(self) -> None:
        self.assertEqual(
            self.core.format_daily_note_date("2005-01-05", "YY"), "05"
        )
        self.assertEqual(
            self.core.format_daily_note_date("2005-01-05", "M/D/YY"), "1/5/05"
        )

    def test_combined_tokens_and_separators(self) -> None:
        self.assertEqual(
            self.core.format_daily_note_date("2026-08-28", "M.D.YY"), "8.28.26"
        )
        self.assertEqual(
            self.core.format_daily_note_date("2026-08-28", "YYYY_MM"), "2026_08"
        )

    def test_nested_path_format(self) -> None:
        self.assertEqual(
            self.core.format_daily_note_date("2026-08-28", "YYYY/MM/DD"),
            "2026/08/28",
        )

    def test_unsupported_alphabetic_rejected(self) -> None:
        for fmt in (
            "YYYY-MM-DD-dddd",
            "MMM",
            "DDDD",
            "YYYYMM",
            "YYYY woop",
            "x",
            "YYYYwMM",
        ):
            with self.assertRaises(self.core.ValidationError):
                self.core.format_daily_note_date("2026-08-28", fmt)

    def test_unsafe_literals_rejected(self) -> None:
        for fmt in ("YYYY MM", "YYYY=MM", "YYYY(MM)", "YYYY\\MM"):
            with self.assertRaises(self.core.ValidationError):
                self.core.format_daily_note_date("2026-08-28", fmt)

    def test_empty_format_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.format_daily_note_date("2026-08-28", "")

    def test_format_requires_token(self) -> None:
        for fmt in ("-", "--", "..."):
            with self.assertRaises(self.core.ValidationError):
                self.core.format_daily_note_date("2026-08-28", fmt)

    def test_invalid_date_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.format_daily_note_date("2026-2-28", "YYYY-MM-DD")
        with self.assertRaises(self.core.ValidationError):
            self.core.format_daily_note_date("not-a-date", "YYYY")

    def test_oversized_format_rejected(self) -> None:
        fmt = "YYYY-MM-" * 20
        with self.assertRaises(self.core.ValidationError):
            self.core.format_daily_note_date("2026-08-28", fmt)


class DailyNotePathTests(unittest.TestCase):
    """Vault-confined <folder>/<formatted date>.md resolution."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_path_"))
        self.vault = _make_plain_vault(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_resolve_root_folder(self) -> None:
        cfg = self.core.DailyNotesConfig()
        path = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")
        self.assertEqual(path, self.vault / "2026-08-28.md")

    def test_resolve_with_folder(self) -> None:
        cfg = self.core.DailyNotesConfig(folder="journal")
        path = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")
        self.assertEqual(path, self.vault / "journal" / "2026-08-28.md")

    def test_resolve_nested_format(self) -> None:
        cfg = self.core.DailyNotesConfig(folder="journal", format="YYYY/MM/DD")
        path = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")
        self.assertEqual(path, self.vault / "journal" / "2026" / "08" / "28.md")

    def test_resolve_vault_confined(self) -> None:
        cfg = self.core.DailyNotesConfig(folder="journal", format="YYYY/MM/DD")
        path = self.core.resolve_daily_note_path(self.vault, cfg, "2026-08-28")
        self.assertTrue(str(path).startswith(str(self.vault)))

    def test_resolve_invalid_date_rejected(self) -> None:
        cfg = self.core.DailyNotesConfig()
        with self.assertRaises(self.core.ValidationError):
            self.core.resolve_daily_note_path(self.vault, cfg, "28-08-2026")


class DailyNoteTemplateRenderTests(unittest.TestCase):
    """Template rendering ({{date}}, {{title}}, {{date:FORMAT}} only)."""

    def setUp(self) -> None:
        self.core = _load_core()

    def test_render_date_and_title(self) -> None:
        out = self.core.render_daily_note_template(
            "# {{date}}\n\n{{title}}\n", date="2026-08-28", title="Day 1"
        )
        self.assertEqual(out, "# 2026-08-28\n\nDay 1\n")

    def test_render_date_with_format(self) -> None:
        out = self.core.render_daily_note_template(
            "{{date:YYYY/MM/DD}}", date="2026-08-28", title="T"
        )
        self.assertEqual(out, "2026/08/28")

    def test_unsupported_expressions_rejected(self) -> None:
        for template in ("{{time}}", "{{date:dddd}}", "{{foo}}"):
            with self.assertRaises(self.core.ValidationError):
                self.core.render_daily_note_template(
                    template, date="2026-08-28", title="T"
                )

    def test_no_code_execution(self) -> None:
        for template in ("{{ 7*7 }}", "{{ self.__init__ }}", "{{ date.format() }}"):
            with self.assertRaises(self.core.ValidationError):
                self.core.render_daily_note_template(
                    template, date="2026-08-28", title="T"
                )

    def test_literal_braces_preserved(self) -> None:
        out = self.core.render_daily_note_template(
            "{x} {{date}} }", date="2026-08-28", title="T"
        )
        self.assertEqual(out, "{x} 2026-08-28 }")

    def test_oversized_template_rejected(self) -> None:
        template = "x" * (self.core.MAX_BODY_LEN + 1)
        with self.assertRaises(self.core.ValidationError):
            self.core.render_daily_note_template(
                template, date="2026-08-28", title="T"
            )

    def test_default_body_shape(self) -> None:
        body = self.core.build_default_daily_note_body("2026-08-28")
        self.assertEqual(body, "# 2026-08-28\n\n## Tasks\n")
        start, end = self.core.find_tasks_section(body)
        self.assertEqual(body[start:end], "## Tasks\n")

    def test_default_body_rejects_invalid_date(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.build_default_daily_note_body("2026-08-28T00:00")

    def test_rendered_body_tasks_requirement_enforced(self) -> None:
        rendered = self.core.render_daily_note_template(
            "# {{date}}\n\n## Tasks\n", date="2026-08-28", title="T"
        )
        self.core.find_tasks_section(rendered)  # must not raise
        missing = self.core.render_daily_note_template(
            "# {{date}}\n", date="2026-08-28", title="T"
        )
        with self.assertRaises(self.core.ValidationError):
            self.core.find_tasks_section(missing)


class DailyNoteFrontmatterTests(unittest.TestCase):
    """Empty/null top-level date/title normalization."""

    def setUp(self) -> None:
        self.core = _load_core()

    def test_empty_strings_filled(self) -> None:
        out = self.core.normalize_daily_note_frontmatter(
            {"date": "", "title": ""}, date="2026-08-28", title_stem="2026-08-28"
        )
        self.assertEqual(out["date"], "2026-08-28")
        self.assertEqual(out["title"], "2026-08-28")

    def test_nulls_filled(self) -> None:
        out = self.core.normalize_daily_note_frontmatter(
            {"date": None, "title": None}, date="2026-08-28", title_stem="stem"
        )
        self.assertEqual(out["date"], "2026-08-28")
        self.assertEqual(out["title"], "stem")

    def test_blank_strings_filled(self) -> None:
        out = self.core.normalize_daily_note_frontmatter(
            {"date": "   ", "title": ""}, date="2026-08-28", title_stem="stem"
        )
        self.assertEqual(out["date"], "2026-08-28")
        self.assertEqual(out["title"], "stem")

    def test_nonempty_values_unchanged(self) -> None:
        marker = object()
        out = self.core.normalize_daily_note_frontmatter(
            {"date": "2020-01-01", "title": "My Day", "extra": marker},
            date="2026-08-28",
            title_stem="stem",
        )
        self.assertEqual(out["date"], "2020-01-01")
        self.assertEqual(out["title"], "My Day")
        self.assertIs(out["extra"], marker)

    def test_missing_keys_unchanged(self) -> None:
        out = self.core.normalize_daily_note_frontmatter(
            {"tags": ["task"]}, date="2026-08-28", title_stem="stem"
        )
        self.assertEqual(out, {"tags": ["task"]})

    def test_non_mapping_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.normalize_daily_note_frontmatter(
                ["not", "a", "mapping"], date="2026-08-28", title_stem="stem"
            )

    def test_invalid_date_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.normalize_daily_note_frontmatter(
                {}, date="2026-13-01", title_stem="stem"
            )


class DailyTasksSectionTests(unittest.TestCase):
    """find_tasks_section and the structural add/remove transformer."""

    def setUp(self) -> None:
        self.core = _load_core()

    # -- find_tasks_section ------------------------------------------------

    def test_find_section_offsets(self) -> None:
        body = "# Day\n\n## Tasks\n- [[a]]\n\n## Notes\nx"
        start, end = self.core.find_tasks_section(body)
        self.assertEqual(body[start:end], "## Tasks\n- [[a]]\n\n")

    def test_find_section_to_eof(self) -> None:
        body = "## Tasks\n- [[a]]\n"
        start, end = self.core.find_tasks_section(body)
        self.assertEqual((start, end), (0, len(body)))

    def test_h1_h3_do_not_end_section(self) -> None:
        body = "## Tasks\ncontent\n# Not H2\n### Not H2 either\nmore\n## Next\n"
        start, end = self.core.find_tasks_section(body)
        self.assertTrue(body[start:end].startswith("## Tasks"))
        self.assertTrue(body[start:end].endswith("more\n"))

    def test_zero_tasks_sections_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.find_tasks_section("# Day\n\n## Notes\n")

    def test_two_tasks_sections_rejected(self) -> None:
        body = "## Tasks\na\n## Mid\n## Tasks\nb\n"
        with self.assertRaises(self.core.ValidationError):
            self.core.find_tasks_section(body)

    def test_indented_tasks_heading_not_matched(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.find_tasks_section("  ## Tasks\n")

    def test_trailing_whitespace_heading_matched(self) -> None:
        start, _ = self.core.find_tasks_section("## Tasks  \nbody\n")
        self.assertEqual(start, 0)

    def test_oversized_body_rejected(self) -> None:
        body = "## Tasks\n" + "x" * (self.core.MAX_BODY_LEN + 1)
        with self.assertRaises(self.core.ValidationError):
            self.core.find_tasks_section(body)

    # -- add_daily_note_task_link ------------------------------------------

    def test_add_appends_to_empty_section(self) -> None:
        body = "## Tasks\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(new_body, "## Tasks\n- [[task-1]]\n")

    def test_add_appends_before_next_h2(self) -> None:
        body = "# Day\n\n## Tasks\n\n## Notes\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(
            new_body, "# Day\n\n## Tasks\n\n- [[task-1]]\n## Notes\n"
        )

    def test_add_appends_at_eof_without_trailing_newline(self) -> None:
        body = "# Day\n## Tasks"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(new_body, "# Day\n## Tasks\n- [[task-1]]\n")

    def test_add_preserves_bytes_outside_section(self) -> None:
        prefix = "# Day\n\nintro prose\n"
        suffix = "\n## Notes\nkeep me\n"
        body = prefix + "## Tasks\n- [[other|Other]]\n" + suffix
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertTrue(new_body.startswith(prefix))
        self.assertTrue(new_body.endswith(suffix))
        self.assertIn("- [[other|Other]]", new_body)

    def test_add_bare_link_is_already_canonical(self) -> None:
        # Revision 3 W1: the bare form IS the canonical line, so an
        # existing exact bare bullet link is a no-op.
        body = "## Tasks\n- [[task-1]]\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertFalse(changed)
        self.assertEqual(new_body, body)

    def test_add_normalizes_aliased_link_to_bare(self) -> None:
        # Revision 3 W1: a prior ``[[slug|alias]]`` form is recognized by
        # exact slug and normalized to the bare canonical line.
        body = "## Tasks\n- [[task-1|Stale Display]]\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(new_body, "## Tasks\n- [[task-1]]\n")

    def test_add_normalizes_alt_bullet_and_stale_display(self) -> None:
        body = "## Tasks\n* [[task-1|Old Title]]\n"
        new_body, _ = self.core.add_daily_note_task_link(body, slug="task-1")
        self.assertEqual(new_body, "## Tasks\n- [[task-1]]\n")

    def test_add_preserves_indent_of_existing_link(self) -> None:
        body = "## Tasks\n  - [[task-1|Old]]\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(new_body, "## Tasks\n  - [[task-1]]\n")

    def test_add_preserves_indent_of_existing_bare_link(self) -> None:
        body = "## Tasks\n\t- [[task-1]]\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertFalse(changed)
        self.assertEqual(new_body, body)

    def test_add_dedupes_multiple_occurrences(self) -> None:
        # All exact-slug forms (bare and aliased) dedupe onto one bare
        # canonical line; the first occurrence survives in place.
        body = "## Tasks\n- [[task-1]]\n- [[task-1|Again]]\ntext\n- [[task-1|Third]]\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(
            new_body, "## Tasks\n- [[task-1]]\ntext\n"
        )

    def test_add_idempotent(self) -> None:
        body = "## Tasks\n- [[task-1]]\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertFalse(changed)
        self.assertEqual(new_body, body)

    def test_add_ignores_similar_slugs(self) -> None:
        body = "## Tasks\n- [[task-1x|Similar]]\n- [[task-12]]\n- [[ task-1 ]]\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertIn("- [[task-1x|Similar]]", new_body)
        self.assertIn("- [[task-12]]", new_body)
        self.assertIn("- [[ task-1 ]]", new_body)
        self.assertIn("\n- [[task-1]]\n", new_body)

    def test_add_ignores_prose_mention(self) -> None:
        body = "## Tasks\nsee [[task-1]] for details\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertIn("see [[task-1]] for details", new_body)
        self.assertTrue(new_body.endswith("- [[task-1]]\n"))

    def test_add_ignores_checkbox_line(self) -> None:
        body = "## Tasks\n- [ ] [[task-1]]\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertIn("- [ ] [[task-1]]", new_body)
        self.assertTrue(new_body.endswith("- [[task-1]]\n"))

    def test_add_preserves_outside_section_slugs(self) -> None:
        body = "- [[task-1]] before\n\n## Tasks\n\n## Notes\n- [[task-1]] after\n"
        new_body, changed = self.core.add_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertTrue(new_body.startswith("- [[task-1]] before\n"))
        self.assertTrue(new_body.endswith("## Notes\n- [[task-1]] after\n"))

    def test_add_rejects_invalid_slug(self) -> None:
        with self.assertRaises(self.core.PathError):
            self.core.add_daily_note_task_link("## Tasks\n", slug="Bad Slug")

    def test_add_requires_tasks_section(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.add_daily_note_task_link("# no tasks here\n", slug="task-1")

    # -- remove_daily_note_task_link ---------------------------------------

    def test_remove_removes_every_exact_slug_form(self) -> None:
        # Revision 3 W1: remove deletes every exact-slug bullet-only
        # form — bare and aliased alike.
        body = "## Tasks\n- [[task-1]]\n- [[task-1|T]]\n- [[other|Other]]\n"
        new_body, changed = self.core.remove_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(new_body, "## Tasks\n- [[other|Other]]\n")

    def test_remove_preserves_similar_slugs_and_prose(self) -> None:
        body = (
            "## Tasks\n- [[task-1|T]]\nsee [[task-1]] prose\n"
            "- [[task-1x|Similar]]\n"
        )
        new_body, changed = self.core.remove_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(
            new_body, "## Tasks\nsee [[task-1]] prose\n- [[task-1x|Similar]]\n"
        )

    def test_remove_preserves_outside_section(self) -> None:
        body = "- [[task-1]] before\n\n## Tasks\n- [[task-1|T]]\n\n## Notes\n- [[task-1]] after\n"
        new_body, changed = self.core.remove_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(
            new_body,
            "- [[task-1]] before\n\n## Tasks\n\n## Notes\n- [[task-1]] after\n",
        )

    def test_remove_idempotent(self) -> None:
        body = "## Tasks\n- [[other|Other]]\n"
        new_body, changed = self.core.remove_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertFalse(changed)
        self.assertEqual(new_body, body)

    def test_remove_last_line_without_trailing_newline(self) -> None:
        body = "## Tasks\n- [[task-1|T]]"
        new_body, changed = self.core.remove_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        self.assertEqual(new_body, "## Tasks")

    def test_remove_consecutive_dups_at_eof(self) -> None:
        body = "## Tasks\n- [[task-1|A]]\n- [[task-1|B]]"
        new_body, changed = self.core.remove_daily_note_task_link(
            body, slug="task-1"
        )
        self.assertTrue(changed)
        # Both bullet lines and the inner newline are removed; the heading
        # line keeps its own trailing newline.
        self.assertEqual(new_body, "## Tasks\n")

    def test_remove_requires_tasks_section(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.remove_daily_note_task_link("# nothing\n", slug="task-1")

    def test_remove_rejects_invalid_slug(self) -> None:
        with self.assertRaises(self.core.PathError):
            self.core.remove_daily_note_task_link("## Tasks\n", slug="../x")


class MutationResultDailyFieldsTests(unittest.TestCase):
    """Backward-compatible daily link fields and serialization behavior."""

    def setUp(self) -> None:
        self.core = _load_core()

    def _mutation_dict(self, result):
        """Mirror the MCP server serialization (drop None values)."""
        return {
            key: value
            for key, value in dataclasses.asdict(result).items()
            if value is not None
        }

    def test_daily_fields_default_none(self) -> None:
        result = self.core.MutationResult(state="applied", slug="t1")
        self.assertIsNone(result.daily_link_state)
        self.assertIsNone(result.daily_link_detail)
        self.assertIsNone(result.daily_link_dates)

    def test_backward_compat_positional(self) -> None:
        result = self.core.MutationResult("s", "t1", "abc", "detail")
        self.assertEqual(result.state, "s")
        self.assertEqual(result.slug, "t1")
        self.assertEqual(result.commit_id, "abc")
        self.assertEqual(result.detail, "detail")
        self.assertIsNone(result.daily_link_state)

    def test_daily_fields_set(self) -> None:
        result = self.core.MutationResult(
            state="applied",
            slug="t1",
            daily_link_state="applied_and_committed",
            daily_link_detail="daily note updated",
            daily_link_dates=["2026-08-28"],
        )
        self.assertEqual(result.daily_link_state, "applied_and_committed")
        self.assertEqual(result.daily_link_detail, "daily note updated")
        self.assertEqual(result.daily_link_dates, ["2026-08-28"])

    def test_daily_link_dates_supports_multiple(self) -> None:
        result = self.core.MutationResult(
            state="applied",
            slug="t1",
            daily_link_dates=["2026-08-27", "2026-08-28"],
        )
        self.assertEqual(result.daily_link_dates, ["2026-08-27", "2026-08-28"])

    def test_asdict_includes_daily_fields(self) -> None:
        result = self.core.MutationResult(state="s", slug="t1")
        raw = dataclasses.asdict(result)
        self.assertIn("daily_link_state", raw)
        self.assertIn("daily_link_detail", raw)
        self.assertIn("daily_link_dates", raw)

    def test_serialization_drops_none_and_keeps_set(self) -> None:
        unset = self._mutation_dict(
            self.core.MutationResult(state="s", slug="t1")
        )
        self.assertNotIn("daily_link_state", unset)
        self.assertNotIn("daily_link_detail", unset)
        self.assertNotIn("daily_link_dates", unset)
        set_result = self._mutation_dict(
            self.core.MutationResult(
                state="s",
                slug="t1",
                daily_link_state="not_applied",
                daily_link_dates=["2026-08-27", "2026-08-28"],
            )
        )
        self.assertEqual(set_result["daily_link_state"], "not_applied")
        self.assertEqual(
            set_result["daily_link_dates"], ["2026-08-27", "2026-08-28"]
        )
        self.assertNotIn("daily_link_detail", set_result)
        json.dumps(set_result)  # must remain JSON-serializable

    def test_legacy_results_unaffected(self) -> None:
        result = self.core.MutationResult(
            state=self.core.APPLIED_AND_COMMITTED,
            slug="t1",
            commit_id="abc",
        )
        as_dict = self._mutation_dict(result)
        self.assertEqual(
            as_dict,
            {"state": "applied_and_committed", "slug": "t1", "commit_id": "abc"},
        )


# ---------------------------------------------------------------------------
# Daily Notes projection preparation/persistence (issue #139, W1b)
# ---------------------------------------------------------------------------


def _make_plain_git_vault(tmpdir: Path, name: str = "vault") -> Path:
    """Plain daily-notes vault backed by a Git repo (no TaskNotes profile)."""
    vault = _make_plain_vault(tmpdir, name)
    _init_git_repo(vault)
    return vault


def _write_note(path: Path, text: str, mode: Optional[int] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)
    return path


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class DailyProjectionPrepareTests(unittest.TestCase):
    """prepare_daily_note_projection: pre-read, transform, classify."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_prep_"))
        self.vault = _make_plain_vault(self.tmpdir)
        self.cfg = self.core.DailyNotesConfig(folder="journal")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ensure_missing_note_creates_from_default_body(self) -> None:
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_CREATE)
        self.assertIsNone(proj.fingerprint)
        self.assertIsNotNone(proj.content)
        content = proj.content.decode("utf-8")
        self.assertEqual(
            content, "# 2026-08-28\n\n## Tasks\n- [[task-1]]\n"
        )
        self.assertFalse((self.vault / "journal" / "2026-08-28.md").exists())

    def test_ensure_missing_note_uses_template_and_normalizes_frontmatter(self) -> None:
        (self.vault / "templates").mkdir()
        (self.vault / "templates" / "daily.md").write_text(
            "---\ndate: \ntitle: \ntags:\n  - day\n---\n# {{date}}\n\n## Tasks\n",
            encoding="utf-8",
        )
        cfg = self.core.DailyNotesConfig(
            folder="journal", template="templates/daily.md"
        )
        proj = self.core.prepare_daily_note_projection(
            self.vault, cfg, "ensure", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_CREATE)
        content = proj.content.decode("utf-8")
        fm, body = self.core._parse_frontmatter(content)
        self.assertEqual(fm["date"], "2026-08-28")
        self.assertEqual(fm["title"], "2026-08-28")
        self.assertEqual(fm["tags"], ["day"])
        self.assertIn("- [[task-1]]", body)

    def test_ensure_template_missing_tasks_section_rejected_before_write(self) -> None:
        (self.vault / "templates").mkdir()
        (self.vault / "templates" / "daily.md").write_text(
            "# {{date}}\n\nno tasks here\n", encoding="utf-8"
        )
        cfg = self.core.DailyNotesConfig(
            folder="journal", template="templates/daily.md"
        )
        with self.assertRaises(self.core.ValidationError):
            self.core.prepare_daily_note_projection(
                self.vault, cfg, "ensure", "2026-08-28", slug="task-1",
            )
        self.assertFalse((self.vault / "journal").exists())

    def test_ensure_existing_note_replaces_preserving_outside_bytes(self) -> None:
        original = "# Day\n\nintro\n\n## Tasks\n\n## Notes\nkeep me\n"
        _write_note(self.vault / "journal" / "2026-08-28.md", original)
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_REPLACE)
        self.assertIsNotNone(proj.fingerprint)
        content = proj.content.decode("utf-8")
        self.assertTrue(content.startswith("# Day\n\nintro\n\n## Tasks\n"))
        self.assertTrue(content.endswith("## Notes\nkeep me\n"))
        self.assertIn("- [[task-1]]\n## Notes", content)

    def test_existing_note_empty_frontmatter_value_not_normalized(self) -> None:
        # R2 (issue #140): existing notes are never normalized or
        # reserialized; the empty ``date`` value is preserved verbatim.
        original = "---\ndate: \"\"\n---\n## Tasks\n"
        _write_note(self.vault / "journal" / "2026-08-28.md", original)
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_REPLACE)
        content = proj.content.decode("utf-8")
        self.assertTrue(content.startswith('---\ndate: ""\n---\n'))
        fm, _body = self.core._parse_frontmatter(content)
        self.assertEqual(fm["date"], "")

    def test_existing_note_frontmatter_bytes_preserved_verbatim(self) -> None:
        # R2 (issue #140): with null/empty identity fields, unrelated
        # keys, comments, and hand formatting, every byte outside the
        # '## Tasks' section is byte-identical after projection.
        original = (
            "---\n"
            "# editor formatting must survive\n"
            "title: \n"
            "date: ~\n"
            "layout: compact\n"
            "custom_key: keep me\n"
            "tags:\n"
            "  - daily\n"
            "---\n"
            "\n"
            "intro prose\n"
            "\n"
            "## Tasks\n"
            "- [[other|O]]\n"
            "\n"
            "## Notes\n"
            "keep   this\n"
        )
        note = self.vault / "journal" / "2026-08-28.md"
        _write_note(note, original)
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_REPLACE)
        content = proj.content.decode("utf-8")
        start, end = self.core.find_tasks_section(original)
        # Bytes outside the '## Tasks' section are byte-identical: the
        # only change is the bare canonical link line inserted at the
        # section end; prefix, frontmatter, and suffix are untouched.
        self.assertEqual(content[:start], original[:start])
        self.assertEqual(content[:end], original[:end])
        self.assertEqual(content[end:], "- [[task-1]]\n" + original[end:])
        # No reserialization: comment, empty title, null date survive.
        self.assertIn("# editor formatting must survive", content)
        self.assertIn("title: \n", content)
        self.assertIn("date: ~\n", content)
        # The on-disk bytes match the same contract after apply.
        outcome = self.core.apply_daily_note_projection(self.vault, self.cfg, proj)
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_APPLIED)
        on_disk = note.read_text(encoding="utf-8")
        self.assertEqual(on_disk[:start], original[:start])
        self.assertEqual(on_disk[:end], original[:end])
        self.assertEqual(on_disk[end:], "- [[task-1]]\n" + original[end:])

    def test_ensure_canonical_note_is_noop(self) -> None:
        _write_note(
            self.vault / "journal" / "2026-08-28.md",
            "## Tasks\n- [[task-1]]\n",
        )
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_NONE)
        self.assertIsNone(proj.content)
        self.assertIsNotNone(proj.fingerprint)

    def test_ensure_existing_aliased_link_prepares_bare_normalization(self) -> None:
        # Revision 3 W1: a prior aliased form is owned by exact slug and
        # the prepared bytes normalize it to the bare canonical line.
        _write_note(
            self.vault / "journal" / "2026-08-28.md",
            "## Tasks\n- [[task-1|Stale]]\n",
        )
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_REPLACE)
        content = proj.content.decode("utf-8")
        self.assertEqual(content, "## Tasks\n- [[task-1]]\n")

    def test_remove_missing_note_is_idempotent_noop(self) -> None:
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "remove", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_NONE)
        self.assertIsNone(proj.content)
        self.assertFalse((self.vault / "journal").exists())

    def test_remove_existing_note_removes_link_only(self) -> None:
        _write_note(
            self.vault / "journal" / "2026-08-28.md",
            "# Day\n\n## Tasks\n- [[task-1|T]]\n- [[other|O]]\n\n## Notes\nx\n",
        )
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "remove", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_REPLACE)
        content = proj.content.decode("utf-8")
        self.assertNotIn("[[task-1", content)
        self.assertIn("- [[other|O]]", content)
        self.assertIn("## Notes\nx", content)

    def test_remove_existing_without_link_is_noop(self) -> None:
        _write_note(
            self.vault / "journal" / "2026-08-28.md", "## Tasks\n- [[other|O]]\n"
        )
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "remove", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_NONE)

    def test_existing_note_with_two_tasks_sections_rejected(self) -> None:
        _write_note(
            self.vault / "journal" / "2026-08-28.md",
            "## Tasks\na\n## Mid\n## Tasks\nb\n",
        )
        with self.assertRaises(self.core.ValidationError):
            self.core.prepare_daily_note_projection(
                self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
            )

    def test_oversized_existing_note_rejected_bounded(self) -> None:
        big = "## Tasks\n" + "x" * (self.core.DAILY_NOTES_MAX_FILE_SIZE + 1)
        _write_note(self.vault / "journal" / "2026-08-28.md", big)
        with self.assertRaises(self.core.CoreError):
            self.core.prepare_daily_note_projection(
                self.vault, self.cfg, "remove", "2026-08-28", slug="task-1",
            )

    def test_invalid_inputs_rejected(self) -> None:
        for date in ("28-08-2026", "not-a-date"):
            with self.assertRaises(self.core.ValidationError):
                self.core.prepare_daily_note_projection(
                    self.vault, self.cfg, "ensure", date, slug="task-1",
                )
        with self.assertRaises(self.core.PathError):
            self.core.prepare_daily_note_projection(
                self.vault, self.cfg, "ensure", "2026-08-28",
                slug="Bad Slug",
            )
        with self.assertRaises(self.core.ValidationError):
            self.core.prepare_daily_note_projection(
                self.vault, self.cfg, "rename", "2026-08-28", slug="task-1",
            )

    def test_symlinked_note_rejected(self) -> None:
        (self.vault / "journal").mkdir()
        (self.vault / "journal" / "real.md").write_text("## Tasks\n", encoding="utf-8")
        os.symlink("real.md", str(self.vault / "journal" / "2026-08-28.md"))
        with self.assertRaises(self.core.PathError):
            self.core.prepare_daily_note_projection(
                self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
            )

    def test_symlinked_folder_component_rejected(self) -> None:
        (self.vault / "real-dir").mkdir()
        os.symlink("real-dir", str(self.vault / "journal"))
        with self.assertRaises(self.core.PathError):
            self.core.prepare_daily_note_projection(
                self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
            )


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class DailyProjectionWriterTests(unittest.TestCase):
    """apply_daily_note_projection: atomic replace, modes, cleanup, races."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_apply_"))
        self.vault = _make_plain_vault(self.tmpdir)
        self.cfg = self.core.DailyNotesConfig(folder="journal")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _note(self, name: str = "2026-08-28.md") -> Path:
        return self.vault / "journal" / name

    def test_apply_create_writes_file_with_content_and_mode(self) -> None:
        mask = os.umask(0o022)
        os.umask(mask)
        try:
            proj = self.core.prepare_daily_note_projection(
                self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
            )
            outcome = self.core.apply_daily_note_projection(
                self.vault, self.cfg, proj
            )
        finally:
            os.umask(mask)
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_APPLIED)
        self.assertTrue(outcome.created)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(
            self._note().read_text(encoding="utf-8"),
            "# 2026-08-28\n\n## Tasks\n- [[task-1]]\n",
        )
        expected_mode = 0o644 & ~mask
        self.assertEqual(os.stat(self._note()).st_mode & 0o777, expected_mode)

    def test_apply_replace_preserves_existing_mode(self) -> None:
        _write_note(
            self._note(), "## Tasks\n- [[task-1|Old]]\n", mode=0o640
        )
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        outcome = self.core.apply_daily_note_projection(self.vault, self.cfg, proj)
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_APPLIED)
        self.assertFalse(outcome.created)
        self.assertTrue(outcome.changed)
        self.assertEqual(os.stat(self._note()).st_mode & 0o777, 0o640)
        self.assertIn("- [[task-1]]", self._note().read_text(encoding="utf-8"))

    def test_apply_create_missing_parents_safely(self) -> None:
        cfg = self.core.DailyNotesConfig(folder="journal", format="YYYY/MM/DD")
        proj = self.core.prepare_daily_note_projection(
            self.vault, cfg, "ensure", "2026-08-28", slug="task-1",
        )
        outcome = self.core.apply_daily_note_projection(self.vault, cfg, proj)
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_APPLIED)
        self.assertTrue(
            (self.vault / "journal" / "2026" / "08" / "28.md").is_dir() is False
        )
        self.assertTrue((self.vault / "journal" / "2026" / "08" / "28.md").exists())

    def test_apply_create_nested_folder_parents_safely(self) -> None:
        # D13 variance: a multi-segment configured folder creates every
        # missing parent on first projection.
        cfg = self.core.DailyNotesConfig(folder="Journal/Daily")
        proj = self.core.prepare_daily_note_projection(
            self.vault, cfg, "ensure", "2026-08-28", slug="task-1",
        )
        outcome = self.core.apply_daily_note_projection(self.vault, cfg, proj)
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_APPLIED)
        note = self.vault / "Journal" / "Daily" / "2026-08-28.md"
        self.assertTrue(note.exists())
        self.assertIn("- [[task-1]]", note.read_text(encoding="utf-8"))

    def test_apply_parent_component_is_file_rejected(self) -> None:
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        # A regular file occupies the configured folder between
        # prepare and apply; the writer must reject it (no clobber).
        (self.vault / "journal").write_text("not a dir\n", encoding="utf-8")
        with self.assertRaises(self.core.PathError):
            self.core.apply_daily_note_projection(self.vault, self.cfg, proj)
        self.assertFalse((self.vault / "journal" / "2026-08-28.md").exists())

    def test_apply_parent_component_symlink_rejected(self) -> None:
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        # A symlink swap occupies the configured folder between
        # prepare and apply; the writer must not traverse it.
        (self.vault / "real-dir").mkdir()
        os.symlink("real-dir", str(self.vault / "journal"))
        with self.assertRaises(self.core.PathError):
            self.core.apply_daily_note_projection(self.vault, self.cfg, proj)
        self.assertFalse((self.vault / "journal" / "2026-08-28.md").exists())

    def test_apply_remove_missing_note_writes_nothing(self) -> None:
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "remove", "2026-08-28", slug="task-1",
        )
        outcome = self.core.apply_daily_note_projection(self.vault, self.cfg, proj)
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_NOT_APPLIED)
        self.assertEqual(outcome.attempts, 1)
        self.assertFalse(self._note().exists())
        self.assertFalse((self.vault / "journal").exists())

    def test_apply_canonical_ensure_is_not_applied(self) -> None:
        _write_note(self._note(), "## Tasks\n- [[task-1]]\n")
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        before = os.stat(self._note()).st_mtime_ns
        outcome = self.core.apply_daily_note_projection(self.vault, self.cfg, proj)
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_NOT_APPLIED)
        self.assertEqual(os.stat(self._note()).st_mtime_ns, before)

    def test_none_projection_drift_race_recomputes_and_applies(self) -> None:
        _write_note(self._note(), "# Day\n\n## Tasks\n- [[other|O]]\n")
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "remove", "2026-08-28", slug="task-1",
        )
        self.assertEqual(proj.kind, self.core.DAILY_NOTE_PROJECTION_NONE)
        # A concurrent editor re-adds the link between prepare and apply.
        _write_note(
            self._note(), "# Day\n\n## Tasks\n- [[other|O]]\n- [[task-1|T]]\n"
        )
        outcome = self.core.apply_daily_note_projection(self.vault, self.cfg, proj)
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_APPLIED)
        self.assertEqual(outcome.attempts, 2)
        content = self._note().read_text(encoding="utf-8")
        self.assertNotIn("[[task-1", content)
        self.assertIn("- [[other|O]]", content)

    def test_temp_cleanup_on_failure(self) -> None:
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )

        def boom() -> None:
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.core.apply_daily_note_projection(
                self.vault, self.cfg, proj, _final_check_hook=boom
            )
        entries = list((self.vault / "journal").iterdir())
        self.assertEqual(entries, [])

    def test_apply_rejects_replace_projection_without_content(self) -> None:
        broken = self.core.DailyNoteProjection(
            operation="ensure", date="2026-08-28", slug="task-1",
            target_relative="journal/2026-08-28.md",
            kind=self.core.DAILY_NOTE_PROJECTION_REPLACE,
            content=None, fingerprint=None,
        )
        with self.assertRaises(self.core.ValidationError):
            self.core.apply_daily_note_projection(self.vault, self.cfg, broken)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class DailyProjectionRaceTests(unittest.TestCase):
    """Conflict/race/retry: injected single race and persistent race."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_race_"))
        self.vault = _make_plain_vault(self.tmpdir)
        self.cfg = self.core.DailyNotesConfig(folder="journal")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _note(self) -> Path:
        return self.vault / "journal" / "2026-08-28.md"

    def test_single_injected_race_recomputes_and_succeeds(self) -> None:
        _write_note(
            self._note(),
            "# Day\n\n## Tasks\n- [[task-1|Old]]\n\n## Notes\nkeep\n",
        )
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        fired = {"n": 0}

        def single_race() -> None:
            if fired["n"] == 0:
                fired["n"] += 1
                # Concurrent editor modifies the note after prepare.
                self._note().write_text(
                    "# Day\n\n## Tasks\n- [[task-1|Old]]\n\n## Notes\nuser edit\n",
                    encoding="utf-8",
                )

        outcome = self.core.apply_daily_note_projection(
            self.vault, self.cfg, proj, _final_check_hook=single_race
        )
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_APPLIED)
        self.assertEqual(outcome.attempts, 2)
        content = self._note().read_text(encoding="utf-8")
        # The recomputed transformation preserved the racing edit.
        self.assertIn("user edit", content)
        self.assertIn("- [[task-1]]", content)
        self.assertIn("## Notes", content)

    def test_persistent_race_conflicts_without_overwrite(self) -> None:
        _write_note(
            self._note(), "# v0\n\n## Tasks\n- [[task-1|Old]]\n"
        )
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        counter = {"n": 0}

        def persistent_race() -> None:
            counter["n"] += 1
            self._note().write_text(
                f"# v{counter['n']}\n\n## Tasks\n- [[task-1|Old]]\n",
                encoding="utf-8",
            )

        outcome = self.core.apply_daily_note_projection(
            self.vault, self.cfg, proj, _final_check_hook=persistent_race
        )
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_CONFLICT)
        self.assertEqual(outcome.attempts, self.core.DAILY_PROJECTION_MAX_ATTEMPTS)
        # The racing content survived; our bytes were never applied.
        self.assertEqual(
            self._note().read_text(encoding="utf-8"),
            f"# v{counter['n']}\n\n## Tasks\n- [[task-1|Old]]\n",
        )
        self.assertNotIn("- [[task-1]]\n", self._note().read_text(encoding="utf-8"))
        # Temp files are cleaned up after the conflict.
        self.assertEqual(
            [p.name for p in (self.vault / "journal").iterdir()],
            ["2026-08-28.md"],
        )

    def test_create_race_recomputes_to_replace_and_applies(self) -> None:
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        fired = {"n": 0}

        def create_race() -> None:
            if fired["n"] == 0:
                fired["n"] += 1
                # A concurrent creator materializes the note first.
                _write_note(
                    self._note(), "# Mine\n\n## Tasks\n- [[task-1|User]]\n"
                )

        outcome = self.core.apply_daily_note_projection(
            self.vault, self.cfg, proj, _final_check_hook=create_race
        )
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_APPLIED)
        self.assertEqual(outcome.attempts, 2)
        self.assertFalse(outcome.created)  # second attempt was a replace
        content = self._note().read_text(encoding="utf-8")
        self.assertIn("# Mine", content)
        self.assertIn("- [[task-1]]", content)

    def test_create_race_at_publication_boundary_never_clobbers(self) -> None:
        # R1 (issue #140): a competing creator that materializes the
        # target AFTER the final absence check but BEFORE publication is
        # never overwritten; its bytes survive and the intended link
        # outcome is recomputed safely on the retry.
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        competing = "# Mine\n\n## Tasks\n- [[task-1|User]]\n"
        fired = {"n": 0}
        real_check = self.core._daily_entry_exists

        def boundary_race(parent_fd: int, name: str) -> bool:
            if fired["n"] == 0:
                fired["n"] += 1
                _write_note(self._note(), competing)
                return False  # stale absence answer past the check
            return real_check(parent_fd, name)

        with mock.patch.object(self.core, "_daily_entry_exists", boundary_race):
            outcome = self.core.apply_daily_note_projection(
                self.vault, self.cfg, proj
            )
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_APPLIED)
        self.assertEqual(outcome.attempts, 2)
        self.assertFalse(outcome.created)  # retry was a replace, not a clobber
        content = self._note().read_text(encoding="utf-8")
        self.assertIn("# Mine", content)  # competing bytes survived
        self.assertIn("- [[task-1]]", content)  # link outcome recomputed

    def test_persistent_publication_eexist_conflicts_without_write(self) -> None:
        # R1 (issue #140): a persistent EEXIST at the publication
        # boundary exhausts the bounded retries into a conflict and never
        # writes (or clobbers) anything.
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )

        def eexist_link(src, dst, **kwargs):
            raise FileExistsError(errno.EEXIST, "injected publication race")

        with mock.patch.object(os, "link", eexist_link):
            outcome = self.core.apply_daily_note_projection(
                self.vault, self.cfg, proj
            )
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_CONFLICT)
        self.assertEqual(outcome.attempts, self.core.DAILY_PROJECTION_MAX_ATTEMPTS)
        self.assertFalse(self._note().exists())
        self.assertEqual(
            [p.name for p in (self.vault / "journal").iterdir()], []
        )

    def test_persistent_create_race_conflicts_without_overwrite(self) -> None:
        proj = self.core.prepare_daily_note_projection(
            self.vault, self.cfg, "ensure", "2026-08-28", slug="task-1",
        )
        counter = {"n": 0}

        def persistent_create_race() -> None:
            counter["n"] += 1
            _write_note(
                self._note(),
                f"# v{counter['n']}\n\n## Tasks\n- [[task-1|User]]\n",
            )

        outcome = self.core.apply_daily_note_projection(
            self.vault, self.cfg, proj, _final_check_hook=persistent_create_race
        )
        self.assertEqual(outcome.state, self.core.DAILY_PROJECTION_CONFLICT)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(
            self._note().read_text(encoding="utf-8"),
            f"# v{counter['n']}\n\n## Tasks\n- [[task-1|User]]\n",
        )
        self.assertNotIn("- [[task-1]]\n", self._note().read_text(encoding="utf-8"))


class DailyProjectionGitTests(unittest.TestCase):
    """Bounded explicit multi-target staging + content-free commit."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_git_"))
        self.vault = _make_plain_git_vault(self.tmpdir)
        self.git_env = self.core._build_git_env()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=str(self.vault),
            capture_output=True, text=True, check=True,
        )
        return result.stdout

    def _write_daily_note(self, date: str) -> Path:
        note = self.vault / "journal" / f"{date}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"# {date}\n\n## Tasks\n", encoding="utf-8")
        return note

    def test_commits_only_provided_targets(self) -> None:
        note1 = self._write_daily_note("2026-08-27")
        note2 = self._write_daily_note("2026-08-28")
        unrelated = self.vault / "unrelated.md"
        unrelated.write_text("dirty\n", encoding="utf-8")
        committed = self.core.git_commit_daily_projection_targets(
            self.vault, [note1, note2], self.git_env
        )
        self.assertTrue(committed)
        status = self._git("status", "--porcelain")
        self.assertIn("unrelated.md", status)
        self.assertNotIn("2026-08-27.md", status)
        self.assertNotIn("2026-08-28.md", status)
        names = set(self._git("show", "--name-only", "--pretty=format:", "HEAD").split())
        self.assertEqual(names, {"journal/2026-08-27.md", "journal/2026-08-28.md"})
        subject = self._git("log", "-1", "--pretty=%s").strip()
        self.assertEqual(subject, self.core.DAILY_PROJECTION_COMMIT_MSG)

    def test_accepts_relative_and_absolute_paths(self) -> None:
        note1 = self._write_daily_note("2026-08-27")
        note2 = self._write_daily_note("2026-08-28")
        committed = self.core.git_commit_daily_projection_targets(
            self.vault, [note1, Path("journal/2026-08-28.md")], self.git_env
        )
        self.assertTrue(committed)
        self.assertTrue(
            self.core.git_target_clean(self.vault, note2, self.git_env)
        )
        _ = note1  # both staged exactly once

    def test_nothing_to_commit_returns_false(self) -> None:
        note = self._write_daily_note("2026-08-27")
        self.core.git_commit_daily_projection_targets(self.vault, [note], self.git_env)
        committed = self.core.git_commit_daily_projection_targets(
            self.vault, [note], self.git_env
        )
        self.assertFalse(committed)

    def test_empty_targets_returns_false(self) -> None:
        self.assertFalse(
            self.core.git_commit_daily_projection_targets(
                self.vault, [], self.git_env
            )
        )

    def test_target_count_bound(self) -> None:
        targets = [
            Path(f"journal/{i}.md")
            for i in range(self.core.MAX_DAILY_PROJECTION_TARGETS + 1)
        ]
        with self.assertRaises(self.core.ValidationError):
            self.core.git_commit_daily_projection_targets(
                self.vault, targets, self.git_env
            )

    def test_unconfined_paths_rejected(self) -> None:
        for bad in ("../evil.md", "a\\b.md", "/etc/passwd"):
            with self.assertRaises(self.core.PathError):
                self.core.git_commit_daily_projection_targets(
                    self.vault, [Path(bad)], self.git_env
                )

    def test_duplicate_targets_single_commit(self) -> None:
        note = self._write_daily_note("2026-08-27")
        committed = self.core.git_commit_daily_projection_targets(
            self.vault, [note, note], self.git_env
        )
        self.assertTrue(committed)
        self.assertEqual(int(self._git("rev-list", "--count", "HEAD")), 2)


# ---------------------------------------------------------------------------
# Daily Notes link integration (issue #139, W2: engine lifecycle)
# ---------------------------------------------------------------------------

_D1 = "2026-08-27"
_D2 = "2026-08-28"


def _make_daily_engine(
    core,
    tmpdir: Path,
    behavior: Optional[dict] = None,
    *,
    daily_links_enabled: bool = True,
    reconcile_enabled: bool = False,
    reconcile_cursor_path: Optional[Path] = None,
    reconcile_pending_path: Optional[Path] = None,
):
    """Engine vault with a valid Daily Notes config + journal folder."""
    vault = _make_vault(tmpdir)
    _write_daily_config(vault, {"folder": "journal"})
    (vault / "journal").mkdir()
    subprocess.run(["git", "add", "-A"], cwd=str(vault), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "daily setup"], cwd=str(vault), check=True, capture_output=True)
    behavior = dict(behavior or {})
    behavior["vault"] = str(vault)
    gbrain_bin = _write_fake_gbrain(tmpdir, behavior)
    gbrain_home = tmpdir / "gbrain_home"
    gbrain_home.mkdir()
    lock_dir = tmpdir / "locks"
    engine = core.TaskNotesEngine(
        vault=vault,
        gbrain_bin=str(gbrain_bin),
        gbrain_home=gbrain_home,
        lock_dir=lock_dir,
        lock_timeout=2.0,
        tz="UTC",
        daily_links_enabled=daily_links_enabled,
        reconcile_enabled=reconcile_enabled,
        reconcile_cursor_path=reconcile_cursor_path,
        reconcile_pending_path=reconcile_pending_path,
    )
    return engine, vault, gbrain_bin


def _git_log_with_subjects(vault: Path, count: int = 8) -> Dict[str, str]:
    """Map commit subject -> hash for the last ``count`` commits."""
    result = subprocess.run(
        ["git", "log", f"-n{count}", "--pretty=%H %s"],
        cwd=str(vault), capture_output=True, text=True, check=True,
    )
    out: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class DailyLinkIntegrationTests(unittest.TestCase):
    """Scheduled-driven Daily Notes projection in create/update/delete."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_link_"))
        self.engine, self.vault, self.gbrain_bin = _make_daily_engine(
            self.core, self.tmpdir
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def _note(self, date: str) -> Path:
        return self.vault / "journal" / f"{date}.md"

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(self.vault),
            capture_output=True, text=True, check=True,
        ).stdout

    def _no_recovery_marker(self) -> None:
        self.assertFalse(
            (self.tmpdir / "locks" / "tasknotes-recovery.marker").exists()
        )

    def _patch_apply(self, wrapper):
        return mock.patch.object(
            self.core, "apply_daily_note_projection", wrapper
        )

    # -- ctor safety ---------------------------------------------------------

    def test_ctor_rejects_non_boolean_flag(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.TaskNotesEngine(
                vault=self.vault,
                gbrain_bin=str(self.gbrain_bin),
                gbrain_home=self.tmpdir / "gbrain_home",
                lock_dir=self.tmpdir / "locks",
                daily_links_enabled="yes",
            )

    # -- disabled mode -------------------------------------------------------

    def test_disabled_zero_daily_config_reads_and_unchanged_results(self) -> None:
        # Separate vault so setUp's engine stays untouched.
        sub = self.tmpdir / "disabled"
        sub.mkdir()
        engine, vault, _ = _make_daily_engine(
            self.core, sub, daily_links_enabled=False
        )
        # Invalid config on disk proves it is never read.
        (vault / ".obsidian" / "daily-notes.json").write_text(
            "{not json", encoding="utf-8"
        )
        read_counter = {"n": 0}
        real_load = self.core.load_daily_notes_config

        def counting_load(v):
            read_counter["n"] += 1
            return real_load(v)

        with mock.patch.object(self.core, "load_daily_notes_config", counting_load):
            created = engine.create("t1", "My Task", scheduled=_D1, body="b")
            updated = engine.update("t1", scheduled=_D2, body="b2")
            deleted = engine.delete("t1")
        self.assertEqual(read_counter["n"], 0)
        for result in (created, updated, deleted):
            self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
            self.assertIsNone(result.daily_link_state)
            self.assertIsNone(result.daily_link_detail)
            self.assertIsNone(result.daily_link_dates)
        self.assertFalse((vault / "journal" / f"{_D1}.md").exists())
        self.assertFalse((vault / "journal" / f"{_D2}.md").exists())

    def test_enabled_backlog_create_does_not_load_config(self) -> None:
        read_counter = {"n": 0}
        real_load = self.core.load_daily_notes_config

        def counting_load(v):
            read_counter["n"] += 1
            return real_load(v)

        with mock.patch.object(self.core, "load_daily_notes_config", counting_load):
            result = self.engine.create("t1", "My Task", body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_NOT_APPLICABLE)
        self.assertIsNone(result.daily_link_dates)
        self.assertEqual(read_counter["n"], 0)

    # -- transition matrix: create -------------------------------------------

    def test_create_scheduled_ensures_link(self) -> None:
        result = self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        self.assertIsNone(result.daily_link_detail)
        self.assertEqual(result.daily_link_dates, [_D1])
        content = self._note(_D1).read_text(encoding="utf-8")
        self.assertIn("- [[t1]]", content)
        self.assertEqual(self._git("status", "--porcelain").strip(), "")
        syncs = [c for c in _read_calls(self.tmpdir) if c["argv"][0] == "sync"]
        self.assertGreaterEqual(len(syncs), 2)  # preflight + post-commit

    def test_create_backlog_no_link(self) -> None:
        result = self.engine.create("t1", "My Task", body="b")
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_NOT_APPLICABLE)
        self.assertFalse(self._note(_D1).exists())

    def test_create_planned_week_no_link(self) -> None:
        result = self.engine.create(
            "t1", "My Task", planned_week="2026-08-31", body="b"
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_NOT_APPLICABLE)
        self.assertFalse(self._note(_D1).exists())
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["planned_week"], "2026-08-31")

    # -- transition matrix: update -------------------------------------------

    def test_update_backlog_to_scheduled_ensures(self) -> None:
        self.engine.create("t1", "My Task", body="b")
        result = self.engine.update("t1", scheduled=_D1, body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        self.assertEqual(result.daily_link_dates, [_D1])
        self.assertIn("- [[t1]]", self._note(_D1).read_text(encoding="utf-8"))
        self.assertEqual(self._git("status", "--porcelain").strip(), "")

    def test_update_scheduled_d1_to_d2_ensures_then_removes(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        result = self.engine.update("t1", scheduled=_D2, body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        # Ensure D2 first, then remove D1.
        self.assertEqual(result.daily_link_dates, [_D2, _D1])
        self.assertIn("- [[t1]]", self._note(_D2).read_text(encoding="utf-8"))
        d1_content = self._note(_D1).read_text(encoding="utf-8")
        self.assertNotIn("[[t1", d1_content)
        self.assertIn("## Tasks", d1_content)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["scheduled"], _D2)
        self.assertEqual(self._git("status", "--porcelain").strip(), "")

    def test_update_scheduled_to_backlog_removes_link(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        result = self.engine.update("t1", clear_scheduled=True, body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        self.assertEqual(result.daily_link_dates, [_D1])
        self.assertNotIn("[[t1", self._note(_D1).read_text(encoding="utf-8"))

    def test_update_scheduled_to_week_removes_link(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        result = self.engine.update("t1", planned_week="2026-08-31", body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        self.assertEqual(result.daily_link_dates, [_D1])
        self.assertNotIn("[[t1", self._note(_D1).read_text(encoding="utf-8"))
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertNotIn("scheduled", fm)
        self.assertEqual(fm["planned_week"], "2026-08-31")

    def test_update_same_day_is_no_unnecessary_write_and_no_duplicate(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        before = self._note(_D1).stat().st_mtime_ns
        result = self.engine.update("t1", scheduled=_D1, body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_NOT_APPLIED)
        self.assertEqual(result.daily_link_dates, [_D1])
        self.assertEqual(self._note(_D1).stat().st_mtime_ns, before)
        content = self._note(_D1).read_text(encoding="utf-8")
        self.assertEqual(content.count("- [[t1]]"), 1)
        self.assertEqual(self._git("status", "--porcelain").strip(), "")

    def test_same_resolved_target_transition_emits_single_ensure(self) -> None:
        # R4 (issue #140): plans compose by resolved target path. With a
        # monthly format, D1 -> D2 resolves to the same note, so the
        # transition emits exactly one ensure and never an ensure
        # followed by a remove of the same link.
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertTrue(self._note(_D1).exists())
        # The format change between operations is also the R3 freshness
        # regression: the second operation must use the new format.
        _write_daily_config(
            self.vault, {"folder": "journal", "format": "YYYY-MM"}
        )
        result = self.engine.update("t1", scheduled=_D2, body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        # Exactly one projection step (the ensure); no paired remove.
        self.assertEqual(result.daily_link_dates, [_D2])
        monthly = self.vault / "journal" / "2026-08.md"
        self.assertTrue(monthly.exists())
        content = monthly.read_text(encoding="utf-8")
        self.assertEqual(content.count("- [[t1]]"), 1)
        self._no_recovery_marker()

    def test_update_non_scheduling_does_not_project(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        before = self._note(_D1).stat().st_mtime_ns
        result = self.engine.update("t1", priority="high")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_NOT_APPLICABLE)
        self.assertIsNone(result.daily_link_dates)
        self.assertEqual(self._note(_D1).stat().st_mtime_ns, before)
        self.assertIn("- [[t1]]", self._note(_D1).read_text(encoding="utf-8"))

    # -- scheduled is the sole source -----------------------------------------

    def test_due_only_change_does_not_project(self) -> None:
        self.engine.create("t1", "My Task", body="b")
        before_counts = {"reads": None}
        result = self.engine.update("t1", due=_D1)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_NOT_APPLICABLE)
        self.assertFalse(self._note(_D1).exists())
        _ = before_counts

    def test_recurrence_projects_only_current_schedule(self) -> None:
        result = self.engine.create(
            "t1", "My Task", scheduled=_D1,
            recurrence="FREQ=DAILY;INTERVAL=1", body="b",
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_dates, [_D1])
        self.assertEqual(
            [p.name for p in (self.vault / "journal").iterdir()],
            [f"{_D1}.md"],
        )

    def test_complete_archive_and_tags_do_not_project(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        before = self._note(_D1).stat().st_mtime_ns
        completed = self.engine.complete("t1")
        self.assertIsNone(completed.daily_link_state)
        self.assertEqual(self._note(_D1).stat().st_mtime_ns, before)
        archived = self.engine.archive("t1")
        self.assertIsNone(archived.daily_link_state)
        self.assertEqual(self._note(_D1).stat().st_mtime_ns, before)
        tagged = self.engine.add_tag("t1", "custom")
        self.assertIsNone(tagged.daily_link_state)
        self.assertEqual(self._note(_D1).stat().st_mtime_ns, before)
        untagged = self.engine.remove_tag("t1", "custom")
        self.assertIsNone(untagged.daily_link_state)
        self.assertEqual(self._note(_D1).stat().st_mtime_ns, before)
        self.assertIn("- [[t1]]", self._note(_D1).read_text(encoding="utf-8"))

    # -- title-free projection (revision 3 W1) --------------------------------

    def test_create_metacharacter_title_projects_bare_line(self) -> None:
        # R6: any valid task title is accepted; the projected line is the
        # bare canonical form and never carries the title.
        result = self.engine.create("t1", "Review [draft]", scheduled=_D1, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        content = self._note(_D1).read_text(encoding="utf-8")
        self.assertIn("- [[t1]]", content)
        self.assertNotIn("[[t1|", content)
        self.assertNotIn("Review [draft]", content)
        # Task Markdown/title semantics are untouched.
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["title"], "Review [draft]")
        self.assertEqual(self._git("status", "--porcelain").strip(), "")

    def test_pipe_title_projects_bare_line_on_create_and_reschedule(self) -> None:
        result = self.engine.create("t1", "Compare A | B", scheduled=_D1, body="b")
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        self.assertIn(
            "- [[t1]]",
            self._note(_D1).read_text(encoding="utf-8"),
        )
        moved = self.engine.update("t1", scheduled=_D2, body="b2")
        self.assertEqual(moved.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(moved.daily_link_state, self.core.DAILY_LINK_APPLIED)
        self.assertEqual(moved.daily_link_dates, [_D2, _D1])
        # The D2 ensure writes the bare canonical line...
        self.assertIn(
            "- [[t1]]",
            self._note(_D2).read_text(encoding="utf-8"),
        )
        # ...and the D1 removal found the link by exact slug.
        d1_content = self._note(_D1).read_text(encoding="utf-8")
        self.assertNotIn("[[t1", d1_content)
        self.assertIn("## Tasks", d1_content)
        fm, _ = self.core._parse_frontmatter(
            (self.vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["title"], "Compare A | B")

    def test_ensure_is_idempotent_single_bare_link(self) -> None:
        self.engine.create("t1", "Review [draft]", scheduled=_D1, body="b")
        content = self._note(_D1).read_text(encoding="utf-8")
        self.assertEqual(content.count("- [[t1]]"), 1)
        before = self._note(_D1).stat().st_mtime_ns
        result = self.engine.update(
            "t1", scheduled=_D1, priority="high", body="b2"
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_NOT_APPLIED)
        self.assertEqual(self._note(_D1).stat().st_mtime_ns, before)
        again = self._note(_D1).read_text(encoding="utf-8")
        self.assertEqual(again.count("- [[t1]]"), 1)
        self.assertNotIn("[[t1|", again)
        self.assertEqual(self._git("status", "--porcelain").strip(), "")

    def test_projection_preserves_unrelated_lines(self) -> None:
        _write_note(
            self._note(_D1),
            "# Day\n\nprose with [[t1]] raw link\n\n"
            "## Tasks\n- [[other|Other]]\n\n## Notes\nkeep\n",
        )
        result = self.engine.create("t1", "Review [draft]", scheduled=_D1, body="b")
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        content = self._note(_D1).read_text(encoding="utf-8")
        self.assertIn("- [[t1]]", content)
        # Unrelated lines and similar-but-not-exact mentions are untouched.
        self.assertIn("# Day", content)
        self.assertIn("prose with [[t1]] raw link", content)
        self.assertIn("- [[other|Other]]", content)
        self.assertIn("## Notes\nkeep", content)

    def test_two_tasks_project_distinct_bare_lines(self) -> None:
        self.engine.create("t1", "Review [draft]", scheduled=_D1, body="b")
        result = self.engine.create(
            "t2", "Literal %5B text", scheduled=_D1, body="b"
        )
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        content = self._note(_D1).read_text(encoding="utf-8")
        self.assertIn("- [[t1]]", content)
        self.assertIn("- [[t2]]", content)
        # Idempotent re-ensure of t1 leaves both links alone.
        before = self._note(_D1).stat().st_mtime_ns
        again = self.engine.update("t1", scheduled=_D1, priority="high")
        self.assertEqual(again.daily_link_state, self.core.DAILY_LINK_NOT_APPLIED)
        self.assertEqual(self._note(_D1).stat().st_mtime_ns, before)
        final = self._note(_D1).read_text(encoding="utf-8")
        self.assertEqual(final.count("- [[t1]]"), 1)
        self.assertEqual(final.count("- [[t2]]"), 1)

    def test_delete_removes_bare_link_by_exact_slug(self) -> None:
        self.engine.create("t1", "Compare A | B", scheduled=_D1, body="b")
        result = self.engine.delete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        content = self._note(_D1).read_text(encoding="utf-8")
        self.assertNotIn("[[t1", content)
        self.assertIn("## Tasks", content)
        self._no_recovery_marker()

    def test_disabled_mode_accepts_metacharacter_title_unchanged(self) -> None:
        sub = self.tmpdir / "disabled_r6"
        sub.mkdir()
        engine, vault, _ = _make_daily_engine(
            self.core, sub, daily_links_enabled=False
        )
        result = engine.create("t1", "Review [draft] | x", scheduled=_D1, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertIsNone(result.daily_link_state)
        self.assertIsNone(result.daily_link_detail)
        self.assertIsNone(result.daily_link_dates)
        self.assertFalse((vault / "journal" / f"{_D1}.md").exists())
        fm, _ = self.core._parse_frontmatter(
            (vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["title"], "Review [draft] | x")

    # -- prevalidation and failure isolation ----------------------------------

    def test_prevalidation_failure_zero_task_side_effects(self) -> None:
        # Template without a '## Tasks' section fails deterministically
        # BEFORE capture; no task file, no capture call.
        (self.vault / "templates").mkdir()
        (self.vault / "templates" / "daily.md").write_text(
            "# {{date}}\n", encoding="utf-8"
        )
        _write_daily_config(
            self.vault, {"folder": "journal", "template": "templates/daily.md"}
        )
        with self.assertRaises(self.core.ValidationError):
            self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertFalse((self.vault / "tasks" / "t1.md").exists())
        self.assertFalse(self._note(_D1).exists())
        self.assertEqual(
            [c for c in _read_calls(self.tmpdir) if c["argv"][0] == "capture"],
            [],
        )

    def test_capture_failure_zero_projection_writes(self) -> None:
        sub = self.tmpdir / "capture_fail"
        sub.mkdir()
        engine, vault, _ = _make_daily_engine(
            self.core, sub, {"capture_fail": ["tasks/t1"]}
        )
        result = engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertNotEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertIsNone(result.daily_link_state)
        self.assertIsNone(result.daily_link_dates)
        self.assertFalse((vault / "journal" / f"{_D1}.md").exists())

    def test_sync_failure_after_commit_reports_degradation_without_marker(self) -> None:
        real_sync = self.core.gbrain_sync_incremental
        calls = {"n": 0}

        def flaky_sync(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:  # first sync = preflight, second = projection
                raise self.core.GbrainError("injected sync failure")
            return real_sync(*args, **kwargs)

        with mock.patch.object(self.core, "gbrain_sync_incremental", flaky_sync):
            result = self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_SYNC_FAILED)
        self.assertEqual(
            result.daily_link_detail,
            "daily note projection committed but sync failed",
        )
        self.assertEqual(result.daily_link_dates, [_D1])
        self.assertIn("- [[t1]]", self._note(_D1).read_text(encoding="utf-8"))
        self.assertEqual(self._git("status", "--porcelain").strip(), "")
        self._no_recovery_marker()

    def test_git_projection_failure_reports_degradation_without_marker(self) -> None:
        def failing_commit(*args, **kwargs):
            raise self.core.GitError("injected projection commit failure")

        with mock.patch.object(
            self.core, "git_commit_daily_projection_targets", failing_commit
        ):
            result = self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertIsNotNone(result.commit_id)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_COMMIT_FAILED)
        self.assertEqual(
            result.daily_link_detail, "daily note projection commit failed"
        )
        self.assertIn("- [[t1]]", self._note(_D1).read_text(encoding="utf-8"))
        self.assertIn("journal", self._git("status", "--porcelain"))
        self._no_recovery_marker()

    # -- OSError containment after task commit (submit-gating remediation) ---

    def test_writer_oserror_on_create_publish_maps_to_write_failed(self) -> None:
        # R1: creation publishes via atomic no-clobber os.link; an OSError
        # there maps to the typed write-failure path without side effects.
        def failing_link(src, dst, **kwargs):
            raise OSError(28, "injected publish failure")

        with mock.patch.object(os, "link", failing_link):
            result = self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_WRITE_FAILED)
        self.assertEqual(
            result.daily_link_detail, "daily note projection write failed"
        )
        self.assertEqual(result.daily_link_dates, [_D1])
        # Target never created; no temp file survived; no recovery marker.
        self.assertEqual(
            [p.name for p in (self.vault / "journal").iterdir()], []
        )
        self.assertTrue((self.vault / "tasks" / "t1.md").exists())
        self._no_recovery_marker()

    def test_writer_oserror_on_replace_maps_to_write_failed(self) -> None:
        # Existing note WITHOUT the link: the ensure takes the replace
        # path (fingerprint-verified os.replace); an OSError there maps
        # to the typed write-failure path without partial writes.
        self.engine.create("t1", "My Task", body="b")
        _write_note(
            self._note(_D1), "# Day\n\n## Tasks\n- [[other|O]]\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=str(self.vault), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "note fixture"], cwd=str(self.vault), check=True, capture_output=True)

        def failing_replace(src, dst, **kwargs):
            raise OSError(28, "injected replace failure")

        with mock.patch.object(os, "replace", failing_replace):
            result = self.engine.update("t1", scheduled=_D1, body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_WRITE_FAILED)
        self.assertEqual(
            result.daily_link_detail, "daily note projection write failed"
        )
        self.assertEqual(result.daily_link_dates, [_D1])
        content = self._note(_D1).read_text(encoding="utf-8")
        self.assertNotIn("[[t1", content)  # never partially written
        self.assertEqual(
            [p.name for p in (self.vault / "journal").iterdir()],
            [f"{_D1}.md"],
        )
        self._no_recovery_marker()

    def test_writer_oserror_on_temp_write_maps_to_write_failed(self) -> None:
        def failing_fchmod(fd, mode):
            raise OSError(28, "injected fchmod failure")

        with mock.patch.object(os, "fchmod", failing_fchmod):
            result = self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_WRITE_FAILED)
        self.assertEqual(
            result.daily_link_detail, "daily note projection write failed"
        )
        self.assertEqual(
            [p.name for p in (self.vault / "journal").iterdir()], []
        )
        self._no_recovery_marker()

    # -- config freshness (loaded once per operation, never cached) ---------

    def test_daily_config_reloaded_per_operation_uses_new_folder(self) -> None:
        # R3 (issue #140): no engine-lifetime caching; the config is
        # loaded and validated exactly once per projection-bearing
        # operation, so a mid-life config change is picked up by the
        # next operation.
        read_counter = {"n": 0}
        real_load = self.core.load_daily_notes_config

        def counting_load(v):
            read_counter["n"] += 1
            return real_load(v)

        with mock.patch.object(self.core, "load_daily_notes_config", counting_load):
            self.engine.create("t1", "My Task", scheduled=_D1, body="b")
            self.assertEqual(read_counter["n"], 1)
            _write_daily_config(self.vault, {"folder": "journal2"})
            result = self.engine.update("t1", scheduled=_D2, body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        self.assertEqual(result.daily_link_dates, [_D2, _D1])
        # The second operation resolved its ensure target with the NEW
        # config folder; the removal step resolved with the same new
        # snapshot (missing target there => idempotent no-op).
        new_note = self.vault / "journal2" / f"{_D2}.md"
        self.assertTrue(new_note.exists())
        self.assertIn("- [[t1]]", new_note.read_text(encoding="utf-8"))
        self.assertFalse((self.vault / "journal2" / f"{_D1}.md").exists())
        self.assertEqual(read_counter["n"], 2)
        self.assertEqual(self._git("status", "--porcelain").strip(), "")
        self._no_recovery_marker()

    def test_daily_config_template_change_picked_up_between_operations(self) -> None:
        # R3 (issue #140): the second operation uses the new config's
        # template, not a stale engine-cached one.
        (self.vault / "templates").mkdir()
        template = self.vault / "templates" / "daily.md"
        template.write_text(
            "---\ndate: \ntitle: \nmarker: v1\n---\n# {{date}}\n\n## Tasks\n",
            encoding="utf-8",
        )
        _write_daily_config(
            self.vault, {"folder": "journal", "template": "templates/daily.md"}
        )
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertIn("marker: v1", self._note(_D1).read_text(encoding="utf-8"))
        template.write_text(
            "---\ndate: \ntitle: \nmarker: v2\n---\n# {{date}}\n\n## Tasks\n",
            encoding="utf-8",
        )
        result = self.engine.create("t2", "Other Task", scheduled=_D2, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        content2 = self._note(_D2).read_text(encoding="utf-8")
        self.assertIn("marker: v2", content2)
        self.assertIn("- [[t2]]", content2)
        self._no_recovery_marker()

    # -- D1 -> D2 add-before-remove degradation -------------------------------

    def test_d1_to_d2_remove_failure_commits_d2_and_degrades(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        real_apply = self.core.apply_daily_note_projection

        def failing_remove(vault, config, projection, **kwargs):
            if projection.operation == self.core.DAILY_PROJECTION_OP_REMOVE:
                raise self.core.CoreError("injected removal failure")
            return real_apply(vault, config, projection, **kwargs)

        with self._patch_apply(failing_remove):
            result = self.engine.update("t1", scheduled=_D2, body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_WRITE_FAILED)
        self.assertEqual(
            result.daily_link_detail,
            "daily note projection partially applied; "
            "applied targets committed and synced",
        )
        self.assertEqual(result.daily_link_dates, [_D2, _D1])
        # D2 ensure survived (committed + synced); D1 removal failed but
        # the D1 file was never clobbered.
        self.assertIn("- [[t1]]", self._note(_D2).read_text(encoding="utf-8"))
        self.assertIn("- [[t1]]", self._note(_D1).read_text(encoding="utf-8"))
        self.assertEqual(self._git("status", "--porcelain").strip(), "")
        self._no_recovery_marker()

    def test_d1_to_d2_remove_conflict_commits_d2_and_degrades(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        real_apply = self.core.apply_daily_note_projection

        def conflicting_remove(vault, config, projection, **kwargs):
            if projection.operation == self.core.DAILY_PROJECTION_OP_REMOVE:
                return self.core.DailyNoteProjectionOutcome(
                    state=self.core.DAILY_PROJECTION_CONFLICT, attempts=2
                )
            return real_apply(vault, config, projection, **kwargs)

        with self._patch_apply(conflicting_remove):
            result = self.engine.update("t1", scheduled=_D2, body="b2")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_CONFLICT)
        self.assertEqual(
            result.daily_link_detail,
            "daily note projection partially applied; "
            "applied targets committed and synced",
        )
        self.assertIn("- [[t1]]", self._note(_D2).read_text(encoding="utf-8"))
        self._no_recovery_marker()

    # -- delete (D13 ordering) --------------------------------------------------

    def test_delete_scheduled_removes_link_and_returns_task_commit(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        result = self.engine.delete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        self.assertEqual(result.daily_link_dates, [_D1])
        self.assertNotIn("[[t1", self._note(_D1).read_text(encoding="utf-8"))
        # commit_id is the TASK deletion commit, not the daily commit.
        log = _git_log_with_subjects(self.vault)
        self.assertEqual(log[self.core.POSTWRITE_DELETE_COMMIT_MSG], result.commit_id)
        self.assertIn(self.core.DAILY_PROJECTION_COMMIT_MSG, log)
        self.assertFalse((self.vault / "tasks" / "t1.md").exists())
        self.assertEqual(self._git("status", "--porcelain").strip(), "")

    def test_delete_backlog_reports_not_applicable(self) -> None:
        self.engine.create("t1", "My Task", body="b")
        result = self.engine.delete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_NOT_APPLICABLE)

    def test_delete_prevalidation_failure_zero_side_effects(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        # Break the target note structure deterministically.
        self._note(_D1).write_text(
            "## Tasks\na\n## Mid\n## Tasks\nb\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "-A"], cwd=str(self.vault), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "break"], cwd=str(self.vault), check=True, capture_output=True)
        with self.assertRaises(self.core.ValidationError):
            self.engine.delete("t1")
        # Task file untouched; soft-delete never ran.
        self.assertTrue((self.vault / "tasks" / "t1.md").exists())
        self.assertEqual(
            [c for c in _read_calls(self.tmpdir) if c["argv"][0] == "delete"],
            [],
        )

    def test_delete_gbrain_gate_failure_leaves_link_untouched(self) -> None:
        sub = self.tmpdir / "delete_fail"
        sub.mkdir()
        engine, vault, _ = _make_daily_engine(
            self.core, sub, {"delete_fail": ["tasks/t1"]}
        )
        engine.create("t1", "My Task", scheduled=_D1, body="b")
        apply_calls: List[str] = []
        real_apply = self.core.apply_daily_note_projection

        def recording_apply(v, c, projection, **kwargs):
            apply_calls.append(projection.operation)
            return real_apply(v, c, projection, **kwargs)

        with self._patch_apply(recording_apply):
            with self.assertRaises(self.core.GbrainError):
                engine.delete("t1")
        self.assertEqual(apply_calls, [])
        self.assertTrue((vault / "tasks" / "t1.md").exists())
        self.assertIn("- [[t1]]", (vault / "journal" / f"{_D1}.md").read_text(encoding="utf-8"))

    def test_delete_runs_projection_only_after_verified_deletion(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")
        apply_calls: List[str] = []
        real_apply = self.core.apply_daily_note_projection

        def recording_apply(v, c, projection, **kwargs):
            apply_calls.append(projection.operation)
            return real_apply(v, c, projection, **kwargs)

        with self._patch_apply(recording_apply):
            result = self.engine.delete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(apply_calls, [self.core.DAILY_PROJECTION_OP_REMOVE])

    def test_delete_success_with_projection_failure_degrades(self) -> None:
        self.engine.create("t1", "My Task", scheduled=_D1, body="b")

        def failing_apply(vault, config, projection, **kwargs):
            raise self.core.CoreError("injected projection failure")

        with self._patch_apply(failing_apply):
            result = self.engine.delete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertIsNotNone(result.commit_id)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_WRITE_FAILED)
        self.assertEqual(
            result.daily_link_detail, "daily note projection write failed"
        )
        # Task deletion is authoritative; the link removal failed cleanly.
        self.assertFalse((self.vault / "tasks" / "t1.md").exists())
        self.assertIn("- [[t1]]", self._note(_D1).read_text(encoding="utf-8"))
        self._no_recovery_marker()


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class DailyLinkPlanTargetComposeTests(unittest.TestCase):
    """R4 (issue #140): plans compose by resolved target path."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_plan_compose_"))
        self.vault = _make_plain_vault(self.tmpdir)
        self.daily_cfg = self.core.DailyNotesConfig(folder="journal")
        self.monthly_cfg = self.core.DailyNotesConfig(
            folder="journal", format="YYYY-MM"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_same_resolved_target_collapses_to_single_ensure(self) -> None:
        steps = self.core._daily_link_plan(_D1, _D2)
        composed = self.core._compose_daily_link_plan_by_target(
            self.vault, self.monthly_cfg, steps
        )
        self.assertEqual(
            composed, [(self.core.DAILY_PROJECTION_OP_ENSURE, _D2)]
        )

    def test_distinct_targets_keep_ensure_then_remove(self) -> None:
        steps = self.core._daily_link_plan(_D1, _D2)
        composed = self.core._compose_daily_link_plan_by_target(
            self.vault, self.daily_cfg, steps
        )
        self.assertEqual(
            composed,
            [
                (self.core.DAILY_PROJECTION_OP_ENSURE, _D2),
                (self.core.DAILY_PROJECTION_OP_REMOVE, _D1),
            ],
        )

    def test_single_step_plans_pass_through(self) -> None:
        ensure_only = self.core._daily_link_plan(None, _D1)
        self.assertEqual(
            self.core._compose_daily_link_plan_by_target(
                self.vault, self.daily_cfg, ensure_only
            ),
            ensure_only,
        )
        remove_only = self.core._daily_link_plan(_D1, None)
        self.assertEqual(
            self.core._compose_daily_link_plan_by_target(
                self.vault, self.daily_cfg, remove_only
            ),
            remove_only,
        )


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class DailyProjectionCollisionTests(unittest.TestCase):
    """R5 (issue #140): resolved projection targets inside the configured
    tasks folder — or the active archive folder — are rejected
    deterministically before any task side effect, so the direct writer
    can never mutate task/archive Markdown."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_daily_collision_"))
        self.collision_msg = (
            "daily note projection target collides with the task or "
            "archive folder"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine(
        self,
        name: str,
        *,
        profile_data: Optional[dict] = None,
        daily_folder: str = "tasks",
    ):
        sub = self.tmpdir / name
        sub.mkdir()
        vault = _make_vault(sub, name)
        if profile_data is not None:
            _write_profile(vault, data=profile_data)
        _write_daily_config(vault, {"folder": daily_folder})
        behavior = {"vault": str(vault)}
        gbrain_bin = _write_fake_gbrain(sub, behavior)
        gbrain_home = sub / "gbrain_home"
        gbrain_home.mkdir()
        engine = self.core.TaskNotesEngine(
            vault=vault,
            gbrain_bin=str(gbrain_bin),
            gbrain_home=gbrain_home,
            lock_dir=sub / "locks",
            lock_timeout=2.0,
            tz="UTC",
            daily_links_enabled=True,
        )
        return engine, vault, sub

    def _capture_calls(self, sub: Path) -> List[dict]:
        return [c for c in _read_calls(sub) if c["argv"][0] == "capture"]

    def test_target_inside_tasks_folder_rejected_before_side_effects(self) -> None:
        engine, vault, sub = self._engine("inside_tasks", daily_folder="tasks")
        with self.assertRaises(self.core.ValidationError) as ctx:
            engine.create("t1", "My Task", scheduled=_D2, body="b")
        self.assertEqual(str(ctx.exception), self.collision_msg)
        # Zero task side effects: no task file, no capture, no daily note.
        self.assertFalse((vault / "tasks" / "t1.md").exists())
        self.assertFalse((vault / "tasks" / f"{_D2}.md").exists())
        self.assertEqual(self._capture_calls(sub), [])

    def test_collision_with_existing_task_markdown_preserves_bytes(self) -> None:
        # An existing task file matches the resolved daily target and
        # even contains a '## Tasks' section: the rejection must make it
        # impossible for the direct writer to clobber it.
        engine, vault, sub = self._engine("existing_task", daily_folder="tasks")
        target = vault / "tasks" / f"{_D2}.md"
        task_markdown = (
            "---\n"
            "type: note\n"
            'title: "2026-08-28"\n'
            "status: open\n"
            "priority: normal\n"
            "tags:\n"
            "  - task\n"
            "---\n"
            "Work notes\n"
            "\n"
            "## Tasks\n"
            "- inline item\n"
        )
        target.write_text(task_markdown, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(vault), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "task fixture"], cwd=str(vault), check=True, capture_output=True)
        with self.assertRaises(self.core.ValidationError) as ctx:
            engine.create("t1", "My Task", scheduled=_D2, body="b")
        self.assertEqual(str(ctx.exception), self.collision_msg)
        # The task Markdown is byte-identical; nothing else was written.
        self.assertEqual(target.read_text(encoding="utf-8"), task_markdown)
        self.assertFalse((vault / "tasks" / "t1.md").exists())
        self.assertEqual(self._capture_calls(sub), [])

    def test_archive_folder_collision_only_when_active(self) -> None:
        # Inactive archive (moveArchivedTasks false): the archive folder
        # is irrelevant, so a top-level "archive" daily folder is allowed.
        engine, vault, _ = self._engine("archive_inactive", daily_folder="archive")
        result = engine.create("t1", "My Task", scheduled=_D2, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        self.assertTrue((vault / "archive" / f"{_D2}.md").exists())
        # Active archive (moveArchivedTasks true + configured folder):
        # the same daily folder now collides and is rejected.
        active_data = dict(REAL_PROFILE_DATA)
        active_data["moveArchivedTasks"] = True
        active_data["archiveFolder"] = "archive"
        engine2, vault2, sub2 = self._engine(
            "archive_active", profile_data=active_data, daily_folder="archive"
        )
        with self.assertRaises(self.core.ValidationError) as ctx:
            engine2.create("t1", "My Task", scheduled=_D2, body="b")
        self.assertEqual(str(ctx.exception), self.collision_msg)
        self.assertFalse((vault2 / "archive").exists())
        self.assertEqual(self._capture_calls(sub2), [])

    def test_update_transition_targets_all_rejected(self) -> None:
        # Every resolved target of a transition is checked: retargeting
        # the daily folder into the tasks folder makes both the ensure
        # and the remove targets collide, before any task side effect.
        engine, vault, sub = self._engine("update_collision", daily_folder="journal")
        created = engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertEqual(created.daily_link_state, self.core.DAILY_LINK_APPLIED)
        captures_after_create = len(self._capture_calls(sub))
        _write_daily_config(vault, {"folder": "tasks"})
        with self.assertRaises(self.core.ValidationError) as ctx:
            engine.update("t1", scheduled=_D2, body="b2")
        self.assertEqual(str(ctx.exception), self.collision_msg)
        # Task and existing link untouched; no capture ran for the update.
        self.assertEqual(
            len(self._capture_calls(sub)), captures_after_create
        )
        self.assertIn(
            "- [[t1]]",
            (vault / "journal" / f"{_D1}.md").read_text(encoding="utf-8"),
        )
        fm, _ = self.core._parse_frontmatter(
            (vault / "tasks" / "t1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(fm["scheduled"], _D1)

    def test_delete_removal_target_collision_rejected(self) -> None:
        engine, vault, sub = self._engine("delete_collision", daily_folder="journal")
        created = engine.create("t1", "My Task", scheduled=_D1, body="b")
        self.assertEqual(created.daily_link_state, self.core.DAILY_LINK_APPLIED)
        _write_daily_config(vault, {"folder": "tasks"})
        with self.assertRaises(self.core.ValidationError) as ctx:
            engine.delete("t1")
        self.assertEqual(str(ctx.exception), self.collision_msg)
        # The soft-delete gate never ran; task and link are intact.
        self.assertTrue((vault / "tasks" / "t1.md").exists())
        self.assertIn(
            "- [[t1]]",
            (vault / "journal" / f"{_D1}.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            [c for c in _read_calls(sub) if c["argv"][0] == "delete"], []
        )


# ---------------------------------------------------------------------------
# Reconciliation foundations (issue #139, W2a: read-only)
# ---------------------------------------------------------------------------

_RECONCILE_HEAD_A = "a" * 40
_RECONCILE_HEAD_B = "b" * 40


class ReconcileCursorStoreTests(unittest.TestCase):
    """Fixed-path cursor document: bootstrap, fail-closed, atomic publish."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_reconcile_cursor_"))
        self.cursor_path = self.tmpdir / "state" / "cursor.json"
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cursor(self, **overrides):
        params = dict(
            reconciled_head=_RECONCILE_HEAD_A,
            daily_folder="journal",
            daily_format="YYYY-MM-DD",
            projection_format=1,
        )
        params.update(overrides)
        return self.core.DailyLinksReconcileCursor(**params)

    def _valid_document(self, **overrides) -> dict:
        doc = {
            "schema": self.core.DAILY_LINKS_RECONCILE_SCHEMA_ID,
            "version": 1,
            "reconciled_head": _RECONCILE_HEAD_A,
            "daily_folder": "journal",
            "daily_format": "YYYY-MM-DD",
            "projection_format": 1,
        }
        doc.update(overrides)
        return doc

    def test_fixed_runtime_paths_and_schema_identity(self) -> None:
        self.assertEqual(
            str(self.core.DAILY_LINKS_RECONCILE_CURSOR_PATH),
            "/opt/data/.gbrain/josemar-tasknotes-daily-links-reconcile.json",
        )
        self.assertEqual(
            str(self.core.DAILY_LINKS_RECONCILE_PENDING_PATH),
            "/opt/data/.gbrain/josemar-tasknotes-daily-links-reconcile-pending.json",
        )
        self.assertEqual(
            self.core.DAILY_LINKS_RECONCILE_SCHEMA_ID,
            "josemar-tasknotes-daily-links-reconcile",
        )
        self.assertEqual(self.core.DAILY_LINKS_RECONCILE_CURSOR_VERSION, 1)
        self.assertEqual(self.core.DAILY_LINKS_PROJECTION_FORMAT_VERSION, 1)

    def test_missing_cursor_is_bootstrap_signal(self) -> None:
        self.assertIsNone(
            self.core.load_daily_links_reconcile_cursor(self.cursor_path)
        )

    def test_cursor_roundtrip_atomic_restrictive_structural(self) -> None:
        cursor = self._cursor()
        self.core.write_daily_links_reconcile_cursor(cursor, self.cursor_path)
        loaded = self.core.load_daily_links_reconcile_cursor(self.cursor_path)
        self.assertEqual(loaded, cursor)
        # Restrictive file mode, single published file, no temp leftovers.
        st = self.cursor_path.stat()
        self.assertEqual(st.st_mode & 0o777, self.core.RECONCILE_CURSOR_FILE_MODE)
        self.assertEqual(
            [p.name for p in self.cursor_path.parent.iterdir()],
            ["cursor.json"],
        )
        # Structural metadata only: exact key set, no titles/bodies.
        payload = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload.keys()),
            {"schema", "version", "reconciled_head", "daily_folder",
             "daily_format", "projection_format"},
        )

    def test_cursor_rewrite_replaces_atomically(self) -> None:
        self.core.write_daily_links_reconcile_cursor(
            self._cursor(daily_folder="old"), self.cursor_path
        )
        self.core.write_daily_links_reconcile_cursor(
            self._cursor(daily_folder="new"), self.cursor_path
        )
        loaded = self.core.load_daily_links_reconcile_cursor(self.cursor_path)
        self.assertEqual(loaded.daily_folder, "new")
        self.assertEqual(
            [p.name for p in self.cursor_path.parent.iterdir()],
            ["cursor.json"],
        )

    def test_cursor_write_failure_keeps_original_and_cleans_temp(self) -> None:
        self.core.write_daily_links_reconcile_cursor(self._cursor(), self.cursor_path)
        original = self.cursor_path.read_bytes()

        def boom(*args, **kwargs):
            raise OSError("injected replace failure")

        with mock.patch.object(self.core.os, "replace", side_effect=boom):
            with self.assertRaises(self.core.CoreError):
                self.core.write_daily_links_reconcile_cursor(
                    self._cursor(daily_folder="z"), self.cursor_path
                )
        self.assertEqual(self.cursor_path.read_bytes(), original)
        self.assertEqual(
            [p.name for p in self.cursor_path.parent.iterdir()],
            ["cursor.json"],
        )

    def test_cursor_write_over_symlink_replaces_link_safely(self) -> None:
        victim = self.tmpdir / "victim.json"
        victim.write_text("{}", encoding="utf-8")
        self.cursor_path.symlink_to(victim)
        self.core.write_daily_links_reconcile_cursor(self._cursor(), self.cursor_path)
        self.assertFalse(self.cursor_path.is_symlink())
        loaded = self.core.load_daily_links_reconcile_cursor(self.cursor_path)
        self.assertEqual(loaded, self._cursor())
        # The symlink target is untouched.
        self.assertEqual(victim.read_text(encoding="utf-8"), "{}")

    def test_corrupt_and_schema_invalid_cursor_fails_closed(self) -> None:
        cases = [
            "{not json",
            json.dumps(["not", "an", "object"]),
            json.dumps({}),  # missing keys
            json.dumps(self._valid_document(extra_key=1)),  # extra keys
            json.dumps(self._valid_document(schema="other-schema")),
            json.dumps(self._valid_document(version=2)),
            json.dumps(self._valid_document(version=True)),
            json.dumps(self._valid_document(reconciled_head="a" * 39)),
            json.dumps(self._valid_document(reconciled_head="A" * 40)),
            json.dumps(self._valid_document(reconciled_head="g" * 40)),
            json.dumps(self._valid_document(daily_folder="../escape")),
            json.dumps(self._valid_document(daily_folder="/abs")),
            json.dumps(self._valid_document(daily_folder=5)),
            json.dumps(self._valid_document(daily_format="")),
            json.dumps(self._valid_document(daily_format="YYYY$MM")),
            json.dumps(self._valid_document(projection_format=2)),
            json.dumps(self._valid_document(projection_format=True)),
        ]
        for payload in cases:
            self.cursor_path.write_text(payload, encoding="utf-8")
            with self.assertRaises(
                self.core.CoreError,
                msg=f"expected fail-closed for: {payload[:60]}",
            ):
                self.core.load_daily_links_reconcile_cursor(self.cursor_path)

    def test_valid_cursor_variants_accepted(self) -> None:
        # Empty folder (vault root), alternate format, SHA-256-length head.
        self.cursor_path.write_text(
            json.dumps(self._valid_document(
                daily_folder="",
                daily_format="YYYY/MM/DD",
                reconciled_head=_RECONCILE_HEAD_B,
            )),
            encoding="utf-8",
        )
        loaded = self.core.load_daily_links_reconcile_cursor(self.cursor_path)
        self.assertEqual(loaded.daily_folder, "")
        self.assertEqual(loaded.daily_format, "YYYY/MM/DD")
        self.assertEqual(loaded.reconciled_head, _RECONCILE_HEAD_B)

    def test_oversized_cursor_fails_closed(self) -> None:
        self.cursor_path.write_text(
            "x" * (self.core.RECONCILE_CURSOR_MAX_FILE_SIZE + 1), encoding="utf-8"
        )
        with self.assertRaises(self.core.CoreError):
            self.core.load_daily_links_reconcile_cursor(self.cursor_path)

    def test_symlinked_cursor_fails_closed(self) -> None:
        target = self.tmpdir / "elsewhere.json"
        target.write_text("{}", encoding="utf-8")
        self.cursor_path.symlink_to(target)
        with self.assertRaises(self.core.PathError):
            self.core.load_daily_links_reconcile_cursor(self.cursor_path)

    def test_non_regular_cursor_fails_closed(self) -> None:
        self.cursor_path.mkdir()
        with self.assertRaises(self.core.PathError):
            self.core.load_daily_links_reconcile_cursor(self.cursor_path)

    def test_cursor_write_requires_cursor_type(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.write_daily_links_reconcile_cursor(
                {"schema": "x"}, self.cursor_path
            )


class ReconcilePendingStoreTests(unittest.TestCase):
    """Fixed pending sibling: validation and clear semantics."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_reconcile_pending_"))
        self.pending_path = self.tmpdir / "state" / "pending.json"
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _pending(self, **overrides):
        params = dict(
            from_head=_RECONCILE_HEAD_A,
            to_head=_RECONCILE_HEAD_B,
            started_at=1_700_000_000,
            daily_folder="journal",
            daily_format="YYYY-MM-DD",
        )
        params.update(overrides)
        return self.core.DailyLinksReconcilePending(**params)

    def _valid_document(self, **overrides) -> dict:
        doc = {
            "schema": self.core.DAILY_LINKS_RECONCILE_SCHEMA_ID,
            "version": 1,
            "from_head": _RECONCILE_HEAD_A,
            "to_head": _RECONCILE_HEAD_B,
            "started_at": 1_700_000_000,
            "daily_folder": "journal",
            "daily_format": "YYYY-MM-DD",
        }
        doc.update(overrides)
        return doc

    def test_missing_pending_is_none(self) -> None:
        self.assertIsNone(
            self.core.load_daily_links_reconcile_pending(self.pending_path)
        )

    def test_pending_roundtrip(self) -> None:
        pending = self._pending()
        self.core.write_daily_links_reconcile_pending(pending, self.pending_path)
        loaded = self.core.load_daily_links_reconcile_pending(self.pending_path)
        self.assertEqual(loaded, pending)
        self.assertEqual(
            self.pending_path.stat().st_mode & 0o777,
            self.core.RECONCILE_CURSOR_FILE_MODE,
        )
        payload = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload.keys()),
            {"schema", "version", "from_head", "to_head", "started_at",
             "daily_folder", "daily_format"},
        )

    def test_pending_invalid_documents_fail_closed(self) -> None:
        cases = [
            "{not json",
            json.dumps([]),
            json.dumps({}),
            json.dumps(self._valid_document(extra_key=None)),
            json.dumps(self._valid_document(schema="wrong")),
            json.dumps(self._valid_document(version=99)),
            json.dumps(self._valid_document(from_head="a" * 7)),
            json.dumps(self._valid_document(to_head="zzz")),
            json.dumps(self._valid_document(started_at=0)),
            json.dumps(self._valid_document(started_at=-5)),
            json.dumps(self._valid_document(started_at="not-a-timestamp")),
            json.dumps(self._valid_document(started_at=True)),
            # Applied-routing pin is validated like the cursor routing.
            json.dumps(self._valid_document(daily_folder="../escape")),
            json.dumps(self._valid_document(daily_folder=5)),
            json.dumps(self._valid_document(daily_format="bad")),
            json.dumps(self._valid_document(daily_format=None)),
        ]
        for payload in cases:
            self.pending_path.write_text(payload, encoding="utf-8")
            with self.assertRaises(
                self.core.CoreError,
                msg=f"expected fail-closed for: {payload[:60]}",
            ):
                self.core.load_daily_links_reconcile_pending(self.pending_path)

    def test_pending_routing_pin_variants_accepted(self) -> None:
        # Empty folder (vault root) and an alternate format are valid.
        self.pending_path.write_text(
            json.dumps(self._valid_document(daily_folder="", daily_format="YYYY/MM")),
            encoding="utf-8",
        )
        pending = self.core.load_daily_links_reconcile_pending(self.pending_path)
        self.assertEqual(pending.daily_folder, "")
        self.assertEqual(pending.daily_format, "YYYY/MM")

    def test_pending_clear_semantics(self) -> None:
        self.assertFalse(
            self.core.clear_daily_links_reconcile_pending(self.pending_path)
        )
        self.core.write_daily_links_reconcile_pending(
            self._pending(), self.pending_path
        )
        self.assertTrue(
            self.core.clear_daily_links_reconcile_pending(self.pending_path)
        )
        self.assertFalse(self.pending_path.exists())
        self.assertIsNone(
            self.core.load_daily_links_reconcile_pending(self.pending_path)
        )

    def test_pending_write_requires_pending_type(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.write_daily_links_reconcile_pending(
                {"from_head": "x"}, self.pending_path
            )


class GitTaskObjectReaderTests(unittest.TestCase):
    """Fixed-argv/no-shell Git object reader: args, cap, missing, UTF-8."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_gitobj_"))
        self.vault = _make_plain_git_vault(self.tmpdir)
        (self.vault / "tasks").mkdir()
        self.marker = "SECRETMARKER-body-content"
        self.task_text = f"---\ntitle: t1\n---\n{self.marker}\n"
        (self.vault / "tasks" / "t1.md").write_text(
            self.task_text, encoding="utf-8"
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=str(self.vault), check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "task"], cwd=str(self.vault),
            check=True, capture_output=True,
        )
        self.head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.vault),
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _commit_file(self, rel: str, data: bytes) -> str:
        target = self.vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        subprocess.run(
            ["git", "add", "-A"], cwd=str(self.vault), check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "add"], cwd=str(self.vault),
            check=True, capture_output=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.vault),
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def test_present_object_roundtrip(self) -> None:
        result = self.core.read_git_task_object(
            self.vault, self.head, "tasks/t1.md"
        )
        self.assertEqual(result.state, self.core.GIT_OBJECT_PRESENT)
        self.assertEqual(result.text, self.task_text)

    def test_fixed_argv_no_shell(self) -> None:
        real_popen = subprocess.Popen
        calls: List[Any] = []

        class RecordingPopen:
            def __init__(self, argv, **kwargs):
                calls.append((list(argv), dict(kwargs)))
                self._proc = real_popen(argv, **kwargs)

            def poll(self):
                return self._proc.poll()

            def wait(self, timeout=None):
                return self._proc.wait(timeout=timeout)

            @property
            def returncode(self):
                return self._proc.returncode

            @property
            def pid(self):
                return self._proc.pid

        with mock.patch.object(self.core.subprocess, "Popen", RecordingPopen):
            result = self.core.read_git_task_object(
                self.vault, self.head, "tasks/t1.md"
            )
        self.assertEqual(result.state, self.core.GIT_OBJECT_PRESENT)
        self.assertTrue(calls)
        show_calls = [
            (argv, kwargs) for argv, kwargs in calls if "show" in argv
        ]
        self.assertEqual(len(show_calls), 1)
        argv, kwargs = show_calls[0]
        self.assertEqual(
            argv,
            ["git"] + self.core._GIT_BASE_ARGS + ["show", f"{self.head}:tasks/t1.md"],
        )
        for argv, kwargs in calls:
            self.assertIsInstance(argv, list)  # never a shell string
            self.assertFalse(kwargs.get("shell", False))
            self.assertTrue(kwargs.get("start_new_session"))

    def test_missing_path_is_typed_no_before_state(self) -> None:
        result = self.core.read_git_task_object(
            self.vault, self.head, "tasks/absent.md"
        )
        self.assertEqual(result.state, self.core.GIT_OBJECT_MISSING)
        self.assertIsNone(result.text)

    def test_committed_symlink_fails_closed(self) -> None:
        # Git records a symlink as a mode-120000 "blob" whose content is
        # the link target: the ls-tree probe must require an allowed
        # regular-file mode so it can never masquerade as task content.
        self._commit_file("tasks/real_target.md", b"real\n")
        os.symlink("real_target.md", self.vault / "tasks" / "linked.md")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(self.vault), check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "link"], cwd=str(self.vault),
            check=True, capture_output=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.vault), check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        with self.assertRaises(self.core.CoreError) as ctx:
            self.core.read_git_task_object(
                self.vault, head, "tasks/linked.md"
            )
        self.assertNotIn("real_target.md", str(ctx.exception))

    def test_unknown_commit_fails_closed(self) -> None:
        with self.assertRaises(self.core.GitError):
            self.core.read_git_task_object(
                self.vault, "0" * 40, "tasks/t1.md"
            )

    def test_invalid_inputs_rejected_before_subprocess(self) -> None:
        for bad_sha in ("", "a" * 39, "A" * 40, "g" * 40, 123):
            with self.assertRaises(self.core.ValidationError, msg=repr(bad_sha)):
                self.core.read_git_task_object(
                    self.vault, bad_sha, "tasks/t1.md"
                )
        for bad_path in ("/etc/passwd", "a/../b.md", "tasks/t1.txt", "../x.md", ""):
            with self.assertRaises(
                (self.core.PathError, self.core.ValidationError),
                msg=repr(bad_path),
            ):
                self.core.read_git_task_object(
                    self.vault, self.head, bad_path
                )

    def test_object_at_size_cap_reads_and_over_cap_fails(self) -> None:
        cap = self.core.LIST_MAX_FILE_SIZE
        at_cap = self._commit_file("tasks/at_cap.md", b"a" * cap)
        result = self.core.read_git_task_object(
            self.vault, at_cap, "tasks/at_cap.md"
        )
        self.assertEqual(result.state, self.core.GIT_OBJECT_PRESENT)
        self.assertEqual(len(result.text), cap)
        # Deliberately aligned to LIST_MAX_FILE_SIZE, not MAX_OUTPUT: a
        # 100 KB object is over the 64 KB subprocess diagnostic cap but
        # under the object cap.
        under = self._commit_file("tasks/under_64k.md", b"b" * 100_000)
        result = self.core.read_git_task_object(
            self.vault, under, "tasks/under_64k.md"
        )
        self.assertEqual(result.state, self.core.GIT_OBJECT_PRESENT)
        over = self._commit_file(
            "tasks/over_cap.md", b"c" * (cap + 1)
        )
        with self.assertRaises(self.core.SubprocessError) as ctx:
            self.core.read_git_task_object(self.vault, over, "tasks/over_cap.md")
        self.assertNotIn("cccccc", str(ctx.exception))

    def test_oversized_object_error_is_content_free(self) -> None:
        cap = self.core.LIST_MAX_FILE_SIZE
        unit = self.marker.encode("utf-8")
        payload = (unit * (cap // len(unit) + 2))[: cap + 1]
        over = self._commit_file("tasks/secret_big.md", payload)
        with self.assertRaises(self.core.SubprocessError) as ctx:
            self.core.read_git_task_object(
                self.vault, over, "tasks/secret_big.md"
            )
        self.assertNotIn(self.marker, str(ctx.exception))

    def test_strict_utf8_decode(self) -> None:
        text = "---\ntitle: caf\u00e9\n---\ncaf\u00e9 \u2615\n"
        sha = self._commit_file("tasks/unicode.md", text.encode("utf-8"))
        result = self.core.read_git_task_object(
            self.vault, sha, "tasks/unicode.md"
        )
        self.assertEqual(result.text, text)
        bad = self._commit_file(
            "tasks/binary.md", b"---\ntitle: x\n---\n\xff\xfe bad\n"
        )
        with self.assertRaises(self.core.CoreError) as ctx:
            self.core.read_git_task_object(self.vault, bad, "tasks/binary.md")
        self.assertNotIn("\xff", str(ctx.exception))

    def test_non_repo_failure_is_typed_and_content_free(self) -> None:
        bare = self.tmpdir / "not_a_repo"
        bare.mkdir()
        with self.assertRaises(self.core.GitError) as ctx:
            self.core.read_git_task_object(
                bare, self.head, "tasks/t1.md"
            )
        self.assertNotIn(self.marker, str(ctx.exception))


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class ReconcileEnumerationTests(unittest.TestCase):
    """Bounded no-follow candidate enumeration and pure classification."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_reconcile_enum_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _vault(self, name: str, *, move_archived: bool = False):
        sub = self.tmpdir / name
        sub.mkdir()
        vault = _make_vault(sub, name)
        if move_archived:
            data = dict(REAL_PROFILE_DATA)
            data["moveArchivedTasks"] = True
            data["archiveFolder"] = "archive"
            _write_profile(vault, data=data)
        return vault, self.core.load_profile(vault, vault)

    def _write_task(
        self,
        vault: Path,
        folder: str,
        slug: str,
        *,
        scheduled: Optional[str] = None,
        tags: Optional[List[str]] = None,
        raw: Optional[str] = None,
    ) -> None:
        path = vault / folder / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return
        all_tags = list(tags if tags is not None else ["task"])
        lines = [
            "---",
            "type: note",
            f"title: {slug}",
            "status: open",
            "priority: normal",
            "tags:",
        ]
        lines += [f"  - {t}" for t in all_tags]
        if scheduled is not None:
            lines.append(f"scheduled: '{scheduled}'")
        lines += ["---", f"body of {slug}", ""]
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_archive_inactive_excludes_archive_folder(self) -> None:
        vault, profile = self._vault("inactive")
        self._write_task(vault, "tasks", "t1", scheduled=_D1)
        self._write_task(vault, "archive", "t2", scheduled=_D2)
        candidates = self.core.enumerate_reconcile_candidates(vault, profile)
        self.assertEqual(
            [(c.slug, c.location) for c in candidates], [("t1", "tasks")]
        )

    def test_archive_active_enumerates_both_folders(self) -> None:
        vault, profile = self._vault("active", move_archived=True)
        self._write_task(vault, "tasks", "t1", scheduled=_D1)
        self._write_task(vault, "archive", "t2", scheduled=_D2, tags=["task", "archived"])
        candidates = self.core.enumerate_reconcile_candidates(vault, profile)
        self.assertEqual(
            [(c.slug, c.location) for c in candidates],
            [("t1", "tasks"), ("t2", "archive")],
        )
        archived_flags = {c.slug: c.archived for c in candidates}
        self.assertFalse(archived_flags["t1"])
        self.assertTrue(archived_flags["t2"])

    def test_archive_active_with_missing_folder_tolerated(self) -> None:
        vault, profile = self._vault("missing_archive", move_archived=True)
        self._write_task(vault, "tasks", "t1", scheduled=_D1)
        candidates = self.core.enumerate_reconcile_candidates(vault, profile)
        self.assertEqual([c.slug for c in candidates], ["t1"])

    def test_archive_folder_equal_to_tasks_folder_not_duplicated(self) -> None:
        vault, profile = self._vault("same_folder")
        data = dict(REAL_PROFILE_DATA)
        data["moveArchivedTasks"] = True
        data["archiveFolder"] = "tasks"
        _write_profile(vault, data=data)
        profile = self.core.load_profile(vault, vault)
        self._write_task(vault, "tasks", "t1", scheduled=_D1)
        candidates = self.core.enumerate_reconcile_candidates(vault, profile)
        self.assertEqual([(c.slug, c.location) for c in candidates], [("t1", "tasks")])

    def test_ambiguous_same_slug_pair_fails_closed(self) -> None:
        vault, profile = self._vault("ambiguous", move_archived=True)
        self._write_task(vault, "tasks", "t1", scheduled=_D1)
        self._write_task(vault, "archive", "t1", scheduled=_D2)
        with self.assertRaises(self.core.CoreError):
            self.core.enumerate_reconcile_candidates(vault, profile)

    def test_enumeration_bound_fails_closed(self) -> None:
        vault, profile = self._vault("bounds")
        for slug in ("t1", "t2", "t3"):
            self._write_task(vault, "tasks", slug, scheduled=_D1)
        with self.assertRaises(self.core.CoreError):
            self.core.enumerate_reconcile_candidates(vault, profile, max_files=2)
        # Under the bound the same vault enumerates fine.
        candidates = self.core.enumerate_reconcile_candidates(
            vault, profile, max_files=3
        )
        self.assertEqual(len(candidates), 3)

    def test_symlinked_task_entry_fails_closed(self) -> None:
        vault, profile = self._vault("symlink")
        self._write_task(vault, "tasks", "real", scheduled=_D1)
        (vault / "tasks" / "t1.md").symlink_to(vault / "tasks" / "real.md")
        with self.assertRaises(self.core.PathError):
            self.core.enumerate_reconcile_candidates(vault, profile)

    def test_non_task_and_non_candidate_names_skipped(self) -> None:
        vault, profile = self._vault("nontask")
        self._write_task(vault, "tasks", "t1", scheduled=_D1)
        # Plain note without the task tag.
        self._write_task(vault, "tasks", "note1", tags=["journal"], scheduled=_D1)
        # Non-slug .md names and non-markdown entries.
        (vault / "tasks" / "My Note.md").write_text("x\n", encoding="utf-8")
        (vault / "tasks" / "scratch.txt").write_text("x\n", encoding="utf-8")
        candidates = self.core.enumerate_reconcile_candidates(vault, profile)
        self.assertEqual([c.slug for c in candidates], ["t1"])

    def test_malformed_entries_classified_and_excluded(self) -> None:
        vault, profile = self._vault("malformed")
        self._write_task(vault, "tasks", "t1", scheduled=_D1)
        # Task with an unusable scheduled value: malformed, excluded.
        self._write_task(vault, "tasks", "bad_date", scheduled="not-a-date")
        # Task with non-string-list tags: malformed, excluded.
        self._write_task(
            vault, "tasks", "bad_tags",
            raw="---\ntype: note\ntitle: x\ntags: 5\n---\nbody\n",
        )
        # Unparsable YAML frontmatter: malformed, excluded.
        self._write_task(
            vault, "tasks", "bad_yaml",
            raw="---\ntags: [unclosed\n---\nbody\n",
        )
        candidates = self.core.enumerate_reconcile_candidates(vault, profile)
        self.assertEqual([c.slug for c in candidates], ["t1"])

    def test_classification_pure_deterministic(self) -> None:
        _vault, profile = self._vault("classify")
        classify = self.core.classify_reconcile_frontmatter
        # Candidate: backlog.
        c = classify({"tags": ["task"]}, profile)
        self.assertEqual(c.cls, self.core.RECONCILE_CLASS_CANDIDATE)
        self.assertIsNone(c.scheduled)
        self.assertFalse(c.archived)
        # Candidate: plain date and gbrain-normalized bare date collapse.
        c = classify({"tags": ["task"], "scheduled": _D2}, profile)
        self.assertEqual((c.cls, c.scheduled), (self.core.RECONCILE_CLASS_CANDIDATE, _D2))
        c = classify(
            {"tags": ["task"], "scheduled": f"{_D2}T00:00:00.000Z"}, profile
        )
        self.assertEqual((c.cls, c.scheduled), (self.core.RECONCILE_CLASS_CANDIDATE, _D2))
        # Candidate with archive tag.
        c = classify({"tags": ["task", "archived"], "scheduled": _D1}, profile)
        self.assertEqual(c.cls, self.core.RECONCILE_CLASS_CANDIDATE)
        self.assertTrue(c.archived)
        # Non-task: confidently not ours.
        c = classify({"tags": ["journal"]}, profile)
        self.assertEqual(c.cls, self.core.RECONCILE_CLASS_NON_TASK)
        # Malformed: broken tags or unusable scheduled on a task.
        self.assertEqual(
            classify({"tags": "task"}, profile).cls,
            self.core.RECONCILE_CLASS_MALFORMED,
        )
        self.assertEqual(
            classify({"tags": [5]}, profile).cls,
            self.core.RECONCILE_CLASS_MALFORMED,
        )
        self.assertEqual(
            classify({"tags": ["task"], "scheduled": "junk"}, profile).cls,
            self.core.RECONCILE_CLASS_MALFORMED,
        )
        # Deterministic: identical inputs give identical results.
        fm = {"tags": ["task"], "scheduled": _D1}
        self.assertEqual(classify(fm, profile), classify(fm, profile))

    def test_slug_from_filename(self) -> None:
        f = self.core.reconcile_slug_from_filename
        self.assertEqual(f("t1.md"), "t1")
        self.assertEqual(f("2026-08-28-143000-buy.md"), "2026-08-28-143000-buy")
        self.assertIsNone(f("My Note.md"))
        self.assertIsNone(f("t1.txt"))
        self.assertIsNone(f(".md"))
        self.assertIsNone(f("../escape.md"))

    def test_enumeration_is_read_only(self) -> None:
        vault, profile = self._vault("readonly")
        self._write_task(vault, "tasks", "t1", scheduled=_D1)
        before = {
            p: p.read_bytes() for p in sorted((vault / "tasks").iterdir())
        }
        self.core.enumerate_reconcile_candidates(vault, profile)
        after = {
            p: p.read_bytes() for p in sorted((vault / "tasks").iterdir())
        }
        self.assertEqual(before, after)


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class ReconcileSnapshotTests(unittest.TestCase):
    """Deterministic read-only snapshots for later reconciliation phases."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_reconcile_snap_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _vault(self, name: str):
        sub = self.tmpdir / name
        sub.mkdir()
        vault = _make_vault(sub, name)
        return vault, self.core.load_profile(vault, vault)

    def _write_task(self, vault: Path, slug: str, scheduled: Optional[str]) -> None:
        lines = [
            "---",
            "type: note",
            f"title: {slug}",
            "status: open",
            "priority: normal",
            "tags:",
            "  - task",
        ]
        if scheduled is not None:
            lines.append(f"scheduled: '{scheduled}'")
        lines += ["---", "body", ""]
        (vault / "tasks" / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")

    def test_snapshot_deterministic_and_sorted(self) -> None:
        vault, profile = self._vault("snap")
        self._write_task(vault, "zebra", _D2)
        self._write_task(vault, "alpha", _D1)
        self._write_task(vault, "backlog", None)
        snap1 = self.core.build_reconcile_snapshot(vault, profile)
        snap2 = self.core.build_reconcile_snapshot(vault, profile)
        self.assertEqual(snap1, snap2)
        self.assertEqual(
            [c.slug for c in snap1.candidates], ["alpha", "backlog", "zebra"]
        )
        self.assertEqual(
            snap1.candidates[0].scheduled, _D1
        )
        self.assertIsNone(snap1.candidates[1].scheduled)
        self.assertEqual(snap1.candidates[0].location, self.core.RECONCILE_LOCATION_TASKS)

    def test_snapshot_head_matches_rev_parse(self) -> None:
        vault, profile = self._vault("head")
        snap = self.core.build_reconcile_snapshot(vault, profile)
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(vault),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(snap.head, expected)
        self.assertTrue(self.core.is_valid_git_commit_sha(snap.head))

    def test_snapshot_requires_head(self) -> None:
        vault, profile = self._vault("nohead")
        headless = self.tmpdir / "headless"
        headless.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(headless), check=True)
        with self.assertRaises(self.core.CoreError):
            self.core.build_reconcile_snapshot(headless, profile)

    def test_snapshot_is_frozen(self) -> None:
        vault, profile = self._vault("frozen")
        self._write_task(vault, "t1", _D1)
        snap = self.core.build_reconcile_snapshot(vault, profile)
        self.assertTrue(snap.candidates)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snap.head = "x" * 40  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snap.candidates[0].slug = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Reconciliation lifecycle (issue #139, W2b: prepare/apply/commit/finalize)
# ---------------------------------------------------------------------------

_WR_D1 = "2026-09-01"
_WR_D2 = "2026-09-02"
_WR_D3 = "2026-09-03"


def _reconcile_write_task(
    vault: Path,
    slug: str,
    *,
    scheduled: Optional[str] = None,
    tags: Optional[List[str]] = None,
    folder: str = "tasks",
    text: Optional[str] = None,
) -> Path:
    """Write a task-shaped markdown file (or raw text) into the vault."""
    path = vault / folder / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text, encoding="utf-8")
        return path
    all_tags = list(tags if tags is not None else ["task"])
    lines = [
        "---",
        "type: note",
        f"title: {slug}",
        "status: open",
        "priority: normal",
        "tags:",
    ]
    lines += [f"  - {t}" for t in all_tags]
    if scheduled is not None:
        lines.append(f"scheduled: '{scheduled}'")
    lines += ["---", f"body {slug}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _reconcile_commit(vault: Path, message: str = "external") -> str:
    subprocess.run(
        ["git", "add", "-A"], cwd=str(vault), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=str(vault),
        check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(vault), check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _reconcile_note(vault: Path, relative: str) -> str:
    return (vault / relative).read_text(encoding="utf-8")


class ReconcileNetClassificationTests(unittest.TestCase):
    """Pure net-change classification (add/remove/tag-loss/reschedule/...)."""

    def setUp(self) -> None:
        self.core = _load_core()

    def _state(self, slug="t1", *, present=True, is_task=True, location="tasks",
               scheduled=None, archived=False):
        return self.core.ReconcileTaskState(
            slug=slug, present=present, is_task=is_task, location=location,
            scheduled=scheduled, archived=archived,
        )

    def test_added(self) -> None:
        change = self.core.classify_reconcile_net_change(
            None, self._state(scheduled=_WR_D1)
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_ADDED)
        self.assertEqual(change.ensure_date, _WR_D1)
        self.assertIsNone(change.remove_date)
        # A file that appears as a non-task never links.
        change = self.core.classify_reconcile_net_change(
            None, self._state(is_task=False)
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_UNCHANGED)

    def test_removed(self) -> None:
        change = self.core.classify_reconcile_net_change(
            self._state(scheduled=_WR_D1), None
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_REMOVED)
        self.assertEqual(change.remove_date, _WR_D1)
        # A former backlog task was never linked.
        change = self.core.classify_reconcile_net_change(
            self._state(scheduled=None), None
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_REMOVED)
        self.assertIsNone(change.remove_date)

    def test_tag_loss(self) -> None:
        change = self.core.classify_reconcile_net_change(
            self._state(scheduled=_WR_D1),
            self._state(is_task=False, location="tasks"),
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_TAG_LOSS)
        self.assertEqual(change.remove_date, _WR_D1)

    def test_became_task(self) -> None:
        change = self.core.classify_reconcile_net_change(
            self._state(is_task=False, location="tasks"),
            self._state(scheduled=_WR_D2),
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_ADDED)
        self.assertEqual(change.ensure_date, _WR_D2)

    def test_reschedule_and_unschedule(self) -> None:
        change = self.core.classify_reconcile_net_change(
            self._state(scheduled=None), self._state(scheduled=_WR_D1)
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_RESCHEDULED)
        self.assertEqual(change.ensure_date, _WR_D1)
        self.assertIsNone(change.remove_date)
        change = self.core.classify_reconcile_net_change(
            self._state(scheduled=_WR_D1), self._state(scheduled=_WR_D2)
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_RESCHEDULED)
        self.assertEqual(
            (change.ensure_date, change.remove_date), (_WR_D2, _WR_D1)
        )
        change = self.core.classify_reconcile_net_change(
            self._state(scheduled=_WR_D1), self._state(scheduled=None)
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_UNSCHEDULED)
        self.assertEqual(change.remove_date, _WR_D1)
        self.assertIsNone(change.ensure_date)

    def test_archive_move_and_unchanged(self) -> None:
        change = self.core.classify_reconcile_net_change(
            self._state(scheduled=_WR_D1, location="tasks"),
            self._state(scheduled=_WR_D1, location="archive", archived=True),
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_ARCHIVE_MOVE)
        self.assertIsNone(change.ensure_date)
        self.assertIsNone(change.remove_date)
        # Title/body-only edits: identical scheduling state.
        change = self.core.classify_reconcile_net_change(
            self._state(scheduled=_WR_D1), self._state(scheduled=_WR_D1)
        )
        self.assertEqual(change.cls, self.core.RECONCILE_NET_UNCHANGED)
        self.assertEqual(change.current_scheduled, _WR_D1)
        change = self.core.classify_reconcile_net_change(None, None)
        self.assertEqual(change.cls, self.core.RECONCILE_NET_UNCHANGED)

    def test_invalid_inputs_rejected(self) -> None:
        with self.assertRaises(self.core.ValidationError):
            self.core.classify_reconcile_net_change("x", None)
        with self.assertRaises(self.core.ValidationError):
            self.core.classify_reconcile_net_change(None, {"slug": "t"})


class ReconcileComposeTests(unittest.TestCase):
    """Pure composition: final-ensure-wins, ordering, re-home, fences."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_reconcile_comp_"))
        sub = self.tmpdir / "s"
        sub.mkdir()
        self.vault = _make_vault(sub, "vault")
        self.profile = self.core.load_profile(self.vault, self.vault)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _config(self, folder="journal", fmt="YYYY-MM-DD"):
        return self.core.DailyNotesConfig(folder=folder, format=fmt)

    def _net(self, cls, *, current=None, ensure=None, remove=None):
        return self.core.ReconcileNetChange(
            cls=cls, current_scheduled=current,
            ensure_date=ensure, remove_date=remove,
        )

    def _steps(self, nets, *, config=None, prior=None):
        return self.core.compose_reconcile_steps(
            self.vault, self.profile, config or self._config(), prior, nets
        )

    def test_ensure_before_remove(self) -> None:
        nets = {"t1": self._net(
            self.core.RECONCILE_NET_RESCHEDULED,
            current=_WR_D2, ensure=_WR_D2, remove=_WR_D1,
        )}
        steps = self._steps(nets, prior=self._config())
        self.assertEqual(
            [(s.operation, s.slug, s.target_relative) for s in steps],
            [
                ("ensure", "t1", f"journal/{_WR_D2}.md"),
                ("remove", "t1", f"journal/{_WR_D1}.md"),
            ],
        )

    def test_final_ensure_wins_coarse_same_target(self) -> None:
        # YYYY-MM coarse format: a reschedule within the month resolves
        # both dates to one physical target — exactly one ensure.
        config = self._config(folder="monthly", fmt="YYYY-MM")
        prior = self._config(folder="monthly", fmt="YYYY-MM")
        nets = {"t1": self._net(
            self.core.RECONCILE_NET_RESCHEDULED,
            current="2026-11-02", ensure="2026-11-02", remove="2026-11-01",
        )}
        steps = self._steps(nets, config=config, prior=prior)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].operation, "ensure")
        self.assertEqual(steps[0].target_relative, "monthly/2026-11.md")
        # Ensure wins even when it must overwrite a composed remove.
        nets = {"t1": self._net(
            self.core.RECONCILE_NET_RESCHEDULED,
            current=_WR_D1, ensure=_WR_D1, remove=_WR_D1,
        )}
        steps = self._steps(nets, config=config, prior=prior)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].operation, "ensure")

    def test_routing_change_rehomes_unchanged_tasks(self) -> None:
        config = self._config(folder="new")
        prior = self._config(folder="old")
        nets = {
            "t1": self._net(
                self.core.RECONCILE_NET_UNCHANGED, current=_WR_D1
            ),
            "t2": self._net(self.core.RECONCILE_NET_UNCHANGED),  # backlog
        }
        steps = self._steps(nets, config=config, prior=prior)
        self.assertEqual(
            [(s.operation, s.slug, s.routing, s.target_relative) for s in steps],
            [
                ("ensure", "t1", "current", f"new/{_WR_D1}.md"),
                ("remove", "t1", "prior", f"old/{_WR_D1}.md"),
            ],
        )

    def test_template_only_change_does_not_rehome(self) -> None:
        # Identical folder/format routing (template differences are not
        # routing): unchanged tasks produce no transitions.
        config = self._config(folder="journal")
        prior = self._config(folder="journal")
        nets = {"t1": self._net(
            self.core.RECONCILE_NET_UNCHANGED, current=_WR_D1
        )}
        steps = self._steps(nets, config=config, prior=prior)
        self.assertEqual(steps, ())

    def test_config_plus_reschedule_goes_directly_old_to_final(self) -> None:
        config = self._config(folder="new")
        prior = self._config(folder="old")
        nets = {"t1": self._net(
            self.core.RECONCILE_NET_RESCHEDULED,
            current=_WR_D2, ensure=_WR_D2, remove=_WR_D1,
        )}
        steps = self._steps(nets, config=config, prior=prior)
        self.assertEqual(
            [(s.operation, s.routing, s.target_relative) for s in steps],
            [
                ("ensure", "current", f"new/{_WR_D2}.md"),
                ("remove", "prior", f"old/{_WR_D1}.md"),
            ],
        )

    def test_r5_collision_fence_on_new_and_old_targets(self) -> None:
        # New target inside the tasks folder.
        nets = {"t1": self._net(
            self.core.RECONCILE_NET_ADDED, current=_WR_D1, ensure=_WR_D1
        )}
        with self.assertRaises(self.core.ValidationError):
            self._steps(nets, config=self._config(folder="tasks"))
        # Old (prior routing) target inside the active archive folder.
        data = dict(REAL_PROFILE_DATA)
        data["moveArchivedTasks"] = True
        data["archiveFolder"] = "archive"
        _write_profile(self.vault, data=data)
        profile = self.core.load_profile(self.vault, self.vault)
        nets = {"t1": self._net(
            self.core.RECONCILE_NET_UNSCHEDULED, remove=_WR_D1
        )}
        with self.assertRaises(self.core.ValidationError):
            self.core.compose_reconcile_steps(
                self.vault, profile, self._config(),
                self._config(folder="archive"), nets,
            )

    def test_remove_requires_prior_routing(self) -> None:
        nets = {"t1": self._net(
            self.core.RECONCILE_NET_REMOVED, remove=_WR_D1
        )}
        with self.assertRaises(self.core.ValidationError):
            self._steps(nets, prior=None)

    def test_target_overflow_fails_closed(self) -> None:
        nets = {
            f"t{i:02d}": self._net(
                self.core.RECONCILE_NET_ADDED,
                current=f"2026-12-{i + 1:02d}",
                ensure=f"2026-12-{i + 1:02d}",
            )
            for i in range(self.core.MAX_DAILY_PROJECTION_TARGETS + 1)
        }
        with self.assertRaises(self.core.CoreError):
            self._steps(nets)


class ReconcileGitTreeTests(unittest.TestCase):
    """Head-side candidate enumeration via git ls-tree plumbing."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_reconcile_tree_"))
        self.vault = _make_plain_git_vault(self.tmpdir)
        (self.vault / "tasks").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _head(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.vault), check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def test_sorted_slugs_ignores_non_task_names_and_missing_dirs(self) -> None:
        _reconcile_write_task(self.vault, "zebra", scheduled=_WR_D1)
        _reconcile_write_task(self.vault, "alpha", scheduled=_WR_D1)
        (self.vault / "tasks" / "My Note.md").write_text("x\n", encoding="utf-8")
        (self.vault / "tasks" / "scratch.txt").write_text("x\n", encoding="utf-8")
        _reconcile_commit(self.vault)
        slugs = self.core.list_git_tree_task_slugs(
            self.vault, self._head(), "tasks"
        )
        self.assertEqual(slugs, ("alpha", "zebra"))
        self.assertEqual(
            self.core.list_git_tree_task_slugs(
                self.vault, self._head(), "does-not-exist"
            ),
            (),
        )

    def test_symlink_entry_fails_closed(self) -> None:
        _reconcile_write_task(self.vault, "real", scheduled=_WR_D1)
        os.symlink("real.md", self.vault / "tasks" / "linked.md")
        _reconcile_commit(self.vault)
        with self.assertRaises(self.core.CoreError):
            self.core.list_git_tree_task_slugs(
                self.vault, self._head(), "tasks"
            )

    def test_bound_fails_closed(self) -> None:
        _reconcile_write_task(self.vault, "t1", scheduled=_WR_D1)
        _reconcile_write_task(self.vault, "t2", scheduled=_WR_D1)
        _reconcile_commit(self.vault)
        with self.assertRaises(self.core.CoreError):
            self.core.list_git_tree_task_slugs(
                self.vault, self._head(), "tasks", max_files=1
            )


class ReconcileLifecycleTests(unittest.TestCase):
    """Full prepare/apply/targeted-commit/finalize lifecycle under tmp paths."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_reconcile_life_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup(
        self,
        name: str,
        *,
        daily: Optional[dict] = None,
        move_archived: bool = False,
    ):
        sub = self.tmpdir / name
        sub.mkdir()
        vault = _make_vault(sub, name)
        if move_archived:
            data = dict(REAL_PROFILE_DATA)
            data["moveArchivedTasks"] = True
            data["archiveFolder"] = "archive"
            _write_profile(vault, data=data)
        _write_daily_config(vault, daily if daily is not None else {"folder": "journal"})
        (vault / "journal").mkdir(exist_ok=True)
        _reconcile_commit(vault, "init")
        profile = self.core.load_profile(vault, vault)
        config = self.core.load_daily_notes_config(vault)
        runtime = sub / "runtime"
        runtime.mkdir()
        return vault, sub, profile, runtime / "cursor.json", runtime / "pending.json"

    def _run(self, vault, profile, config, cursor_path, pending_path):
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config,
            cursor_path=cursor_path, pending_path=pending_path,
        )
        outcome = self.core.apply_daily_links_reconciliation(
            vault, profile, config, plan, pending_path=pending_path
        )
        cursor = self.core.finalize_daily_links_reconciliation(
            vault, config, sync_succeeded=True,
            cursor_path=cursor_path, pending_path=pending_path,
        )
        return plan, outcome, cursor

    def test_bootstrap_root_and_nested_layouts(self) -> None:
        # Root layout (folder "").
        vault, sub, profile, cur, pend = self._setup(
            "root", daily={"folder": ""}
        )
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_write_task(vault, "backlog")  # no scheduled: never linked
        _reconcile_commit(vault)
        plan, outcome, cursor = self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        self.assertEqual(plan.mode, self.core.RECONCILE_MODE_BOOTSTRAP)
        self.assertEqual(
            [(t.slug, t.operation) for t in plan.transitions],
            [("t1", "ensure")],
        )
        self.assertIn("- [[t1]]", _reconcile_note(vault, f"{_WR_D1}.md"))
        self.assertNotIn("- [[backlog]]", _reconcile_note(vault, f"{_WR_D1}.md"))
        self.assertEqual(cursor.daily_folder, "")
        self.assertEqual(cursor.reconciled_head, outcome.pending.to_head)
        self.assertFalse(pend.exists())
        # Nested layout.
        vault2, _sub2, profile2, cur2, pend2 = self._setup(
            "nested", daily={"folder": "journal/deep"}
        )
        _reconcile_write_task(vault2, "t1", scheduled=_WR_D2)
        _reconcile_commit(vault2)
        plan2, _out2, cursor2 = self._run(
            vault2, profile2, self.core.load_daily_notes_config(vault2), cur2, pend2
        )
        self.assertEqual(
            plan2.transitions[0].target_relative, f"journal/deep/{_WR_D2}.md"
        )
        self.assertIn(
            "- [[t1]]",
            _reconcile_note(vault2, f"journal/deep/{_WR_D2}.md"),
        )
        self.assertEqual(cursor2.daily_folder, "journal/deep")

    def test_external_add_reschedule_unschedule_delete(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("diffs")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        # External: reschedule t1, add t2, delete t3 (never existed → n/a),
        # delete t4 (linked backlog... use a linked task instead).
        _reconcile_write_task(vault, "t4", scheduled=_WR_D2)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        # External change set: t1 -> D2, t2 added at D1, t4 deleted.
        _reconcile_write_task(vault, "t1", scheduled=_WR_D2)
        _reconcile_write_task(vault, "t2", scheduled=_WR_D1)
        (vault / "tasks" / "t4.md").unlink()
        _reconcile_commit(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, self.core.load_daily_notes_config(vault),
            cursor_path=cur,
        )
        classes = dict(plan.net_classes)
        self.assertEqual(classes["t1"], self.core.RECONCILE_NET_RESCHEDULED)
        self.assertEqual(classes["t2"], self.core.RECONCILE_NET_ADDED)
        self.assertEqual(classes["t4"], self.core.RECONCILE_NET_REMOVED)
        steps = [(t.slug, t.operation, t.target_relative) for t in plan.transitions]
        self.assertIn(("t1", "ensure", f"journal/{_WR_D2}.md"), steps)
        self.assertIn(("t1", "remove", f"journal/{_WR_D1}.md"), steps)
        self.assertIn(("t2", "ensure", f"journal/{_WR_D1}.md"), steps)
        self.assertIn(("t4", "remove", f"journal/{_WR_D2}.md"), steps)
        # Ensure destination before old remove.
        ops = [t.operation for t in plan.transitions]
        self.assertEqual(ops.index("ensure") < ops.index("remove"), True)
        outcome = self.core.apply_daily_links_reconciliation(
            vault, profile, self.core.load_daily_notes_config(vault),
            plan, pending_path=pend,
        )
        self.assertTrue(outcome.commit_created)
        self.core.finalize_daily_links_reconciliation(
            vault, self.core.load_daily_notes_config(vault),
            sync_succeeded=True, cursor_path=cur, pending_path=pend,
        )
        d1 = _reconcile_note(vault, f"journal/{_WR_D1}.md")
        self.assertIn("- [[t2]]", d1)
        self.assertNotIn("- [[t1]]", d1)
        d2 = _reconcile_note(vault, f"journal/{_WR_D2}.md")
        self.assertIn("- [[t1]]", d2)
        self.assertNotIn("- [[t4]]", d2)
        # External unschedule: t2 cleared to backlog → link removed.
        _reconcile_write_task(vault, "t2", scheduled=None)
        _reconcile_commit(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, self.core.load_daily_notes_config(vault),
            cursor_path=cur,
        )
        self.assertEqual(
            dict(plan.net_classes)["t2"], self.core.RECONCILE_NET_UNSCHEDULED
        )
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        d1 = _reconcile_note(vault, f"journal/{_WR_D1}.md")
        self.assertNotIn("- [[t2]]", d1)

    def test_move_rename_and_tag_loss(self) -> None:
        vault, _sub, profile, cur, pend = self._setup(
            "moves", move_archived=True
        )
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_write_task(vault, "t2", scheduled=_WR_D2)
        _reconcile_write_task(vault, "t3", scheduled=_WR_D3)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        # Same-slug archive move: no transitions, link unchanged.
        (vault / "archive").mkdir()
        (vault / "tasks" / "t1.md").rename(vault / "archive" / "t1.md")
        _reconcile_commit(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, self.core.load_daily_notes_config(vault),
            cursor_path=cur,
        )
        self.assertEqual(
            dict(plan.net_classes)["t1"], self.core.RECONCILE_NET_ARCHIVE_MOVE
        )
        self.assertEqual(plan.transitions, ())
        # Slug rename: remove+ensure pair on the same note, once.
        (vault / "tasks" / "t2.md").rename(vault / "tasks" / "t2r.md")
        path = vault / "tasks" / "t2r.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("title: t2", "title: t2r"),
            encoding="utf-8",
        )
        _reconcile_commit(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, self.core.load_daily_notes_config(vault),
            cursor_path=cur,
        )
        classes = dict(plan.net_classes)
        self.assertEqual(classes["t2"], self.core.RECONCILE_NET_REMOVED)
        self.assertEqual(classes["t2r"], self.core.RECONCILE_NET_ADDED)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        d2 = _reconcile_note(vault, f"journal/{_WR_D2}.md")
        self.assertEqual(d2.count("- [[t2r]]"), 1)
        self.assertNotIn("- [[t2]]", d2)
        # Task-tag loss: link removed, file remains.
        _reconcile_write_task(
            vault, "t3", scheduled=_WR_D3, tags=["journal"],
        )
        _reconcile_commit(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, self.core.load_daily_notes_config(vault),
            cursor_path=cur,
        )
        self.assertEqual(
            dict(plan.net_classes)["t3"], self.core.RECONCILE_NET_TAG_LOSS
        )
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        d3 = _reconcile_note(vault, f"journal/{_WR_D3}.md")
        self.assertNotIn("- [[t3]]", d3)
        self.assertTrue((vault / "tasks" / "t3.md").exists())

    def test_title_only_edit_never_projects(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("titleonly")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        before = _reconcile_note(vault, f"journal/{_WR_D1}.md")
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(vault),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        task_path = vault / "tasks" / "t1.md"
        task_path.write_text(
            task_path.read_text(encoding="utf-8").replace(
                "title: t1", "title: Renamed Title"
            ),
            encoding="utf-8",
        )
        _reconcile_commit(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, self.core.load_daily_notes_config(vault),
            cursor_path=cur,
        )
        self.assertEqual(
            dict(plan.net_classes)["t1"], self.core.RECONCILE_NET_UNCHANGED
        )
        self.assertEqual(plan.transitions, ())
        outcome = self.core.apply_daily_links_reconciliation(
            vault, profile, self.core.load_daily_notes_config(vault),
            plan, pending_path=pend,
        )
        self.assertFalse(outcome.commit_created)
        self.core.finalize_daily_links_reconciliation(
            vault, self.core.load_daily_notes_config(vault),
            sync_succeeded=True, cursor_path=cur, pending_path=pend,
        )
        self.assertEqual(_reconcile_note(vault, f"journal/{_WR_D1}.md"), before)
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(vault),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(head_after, outcome.pending.to_head)

    def test_cursor_lag_replay_batches_changes(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("lag")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        lagged_head = self.core.load_daily_links_reconcile_cursor(cur).reconciled_head
        # Two external commits while the cursor lags.
        _reconcile_write_task(vault, "t2", scheduled=_WR_D1)
        _reconcile_commit(vault)
        _reconcile_write_task(vault, "t1", scheduled=_WR_D2)
        _reconcile_commit(vault)
        self.assertEqual(
            self.core.load_daily_links_reconcile_cursor(cur).reconciled_head,
            lagged_head,
        )
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, self.core.load_daily_notes_config(vault),
            cursor_path=cur,
        )
        self.assertEqual(plan.from_head, lagged_head)
        classes = dict(plan.net_classes)
        self.assertEqual(classes["t1"], self.core.RECONCILE_NET_RESCHEDULED)
        self.assertEqual(classes["t2"], self.core.RECONCILE_NET_ADDED)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        d1 = _reconcile_note(vault, f"journal/{_WR_D1}.md")
        self.assertIn("- [[t2]]", d1)
        self.assertNotIn("- [[t1]]", d1)
        d2 = _reconcile_note(vault, f"journal/{_WR_D2}.md")
        self.assertIn("- [[t1]]", d2)
        cursor = self.core.load_daily_links_reconcile_cursor(cur)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(vault),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(cursor.reconciled_head, head)

    def test_corrupt_established_cursor_fails_closed(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("corrupt")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        cur.write_text("{corrupt", encoding="utf-8")
        with self.assertRaises(self.core.CoreError):
            self.core.prepare_daily_links_reconciliation(
                vault, profile, self.core.load_daily_notes_config(vault),
                cursor_path=cur,
            )
        self.assertFalse(pend.exists())

    def test_finalize_guards_and_replay_convergence(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("guards")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        config = self.core.load_daily_notes_config(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config, cursor_path=cur
        )
        outcome = self.core.apply_daily_links_reconciliation(
            vault, profile, config, plan, pending_path=pend
        )
        # Sync-failure signal: no finalize, cursor/pending intact.
        with self.assertRaises(self.core.CoreError):
            self.core.finalize_daily_links_reconciliation(
                vault, config, sync_succeeded=False,
                cursor_path=cur, pending_path=pend,
            )
        self.assertFalse(cur.exists())
        self.assertTrue(pend.exists())
        # Head moved after apply: pending identity mismatch, no finalize.
        _reconcile_write_task(vault, "t9", scheduled=_WR_D3)
        _reconcile_commit(vault)
        with self.assertRaises(self.core.CoreError):
            self.core.finalize_daily_links_reconciliation(
                vault, config, sync_succeeded=True,
                cursor_path=cur, pending_path=pend,
            )
        self.assertFalse(cur.exists())
        # Replay converges: bootstrap re-plans (t1 link exists → no-op;
        # the externally added t9 is ensure-only), then finalize succeeds.
        note = _reconcile_note(vault, f"journal/{_WR_D1}.md")
        plan, outcome, cursor = self._run(vault, profile, config, cur, pend)
        self.assertEqual(
            [(t.slug, t.projection.kind) for t in plan.transitions],
            [("t1", "none"), ("t9", "create")],
        )
        self.assertTrue(outcome.commit_created)
        self.assertEqual(
            _reconcile_note(vault, f"journal/{_WR_D1}.md"), note
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(vault),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(cursor.reconciled_head, head)
        self.assertFalse(pend.exists())

    def test_crash_after_apply_replays_without_duplicates(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("crash")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        config = self.core.load_daily_notes_config(vault)
        # Apply + commit, then "crash" before finalize (cursor missing).
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config, cursor_path=cur
        )
        self.core.apply_daily_links_reconciliation(
            vault, profile, config, plan, pending_path=pend
        )
        self.assertTrue(pend.exists())
        self.assertFalse(cur.exists())
        # Replay: ensure becomes a no-op, still converges, one link only.
        plan, outcome, cursor = self._run(vault, profile, config, cur, pend)
        self.assertEqual(
            [t.projection.kind for t in plan.transitions], ["none"]
        )
        self.assertFalse(outcome.commit_created)
        note = _reconcile_note(vault, f"journal/{_WR_D1}.md")
        self.assertEqual(note.count("- [[t1]]"), 1)
        self.assertFalse(pend.exists())

    def test_source_churn_fails_closed_before_side_effects(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("churn")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        config = self.core.load_daily_notes_config(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config, cursor_path=cur
        )
        # Uncommitted external edit between prepare and apply.
        _reconcile_write_task(vault, "t1", scheduled=_WR_D2)
        with self.assertRaises(self.core.CoreError):
            self.core.apply_daily_links_reconciliation(
                vault, profile, config, plan, pending_path=pend
            )
        self.assertFalse(pend.exists())
        self.assertFalse((vault / "journal" / f"{_WR_D1}.md").exists())
        # HEAD drift is also churn.
        (vault / "tasks" / "t1.md").write_text(
            (vault / "tasks" / "t1.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        _reconcile_commit(vault)
        with self.assertRaises(self.core.CoreError):
            self.core.apply_daily_links_reconciliation(
                vault, profile, config, plan, pending_path=pend
            )
        self.assertFalse(pend.exists())

    def test_commit_failure_leaves_no_pending_and_replays(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("commitfail")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        config = self.core.load_daily_notes_config(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config, cursor_path=cur
        )

        def boom(*args, **kwargs):
            raise self.core.GitError("injected commit failure")

        with mock.patch.object(
            self.core, "git_commit_reconcile_targets", side_effect=boom
        ):
            with self.assertRaises(self.core.GitError):
                self.core.apply_daily_links_reconciliation(
                    vault, profile, config, plan, pending_path=pend
                )
        self.assertFalse(pend.exists())
        self.assertFalse(cur.exists())
        # Replay converges from the dirty worktree.
        _plan, outcome, cursor = self._run(vault, profile, config, cur, pend)
        self.assertFalse(outcome.commit_created)
        self.assertEqual(
            _reconcile_note(vault, f"journal/{_WR_D1}.md").count("- [[t1]]"), 1
        )
        self.assertFalse(pend.exists())

    def test_projection_conflict_fails_closed(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("conflict")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        config = self.core.load_daily_notes_config(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config, cursor_path=cur
        )
        # Persistent concurrent mutation: every final check sees drift.
        def churn() -> None:
            note = vault / "journal" / f"{_WR_D1}.md"
            with note.open("a", encoding="utf-8") as fh:
                fh.write("x\n")

        with self.assertRaises(self.core.CoreError):
            self.core.apply_daily_links_reconciliation(
                vault, profile, config, plan, pending_path=pend,
                _final_check_hook=churn,
            )
        self.assertFalse(pend.exists())
        self.assertFalse(cur.exists())

    def test_targeted_staging_exact_paths_and_unrelated_preserved(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("staging")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        config = self.core.load_daily_notes_config(vault)
        # Unrelated dirty file and unrelated pre-staged file.
        (vault / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
        (vault / "staged.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "staged.txt"], cwd=str(vault), check=True,
            capture_output=True,
        )
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config, cursor_path=cur
        )
        git_calls: List[List[str]] = []
        real_run_git = self.core._run_git

        def spy(env_vault, env, args, **kwargs):
            git_calls.append(list(args))
            return real_run_git(env_vault, env, args, **kwargs)

        with mock.patch.object(self.core, "_run_git", side_effect=spy):
            outcome = self.core.apply_daily_links_reconciliation(
                vault, profile, config, plan, pending_path=pend
            )
        self.assertTrue(outcome.commit_created)
        # Exact commit contents: only the changed daily note.
        shown = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=str(vault), check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertEqual(shown, [f"journal/{_WR_D1}.md"])
        # No `git add -A`: every add is explicit and pathspec-scoped.
        for args in git_calls:
            if args[:1] == ["add"]:
                self.assertEqual(args[1], "--")
                self.assertNotIn("-A", args)
                self.assertNotIn("-a", args)
        commit_calls = [a for a in git_calls if a[:1] == ["commit"]]
        self.assertEqual(len(commit_calls), 1)
        self.assertIn("--", commit_calls[0])
        self.assertIn(f"journal/{_WR_D1}.md", commit_calls[0])
        # Unrelated dirty/staged entries untouched.
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(vault),
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertTrue(any(l.endswith("unrelated.txt") for l in status))
        self.assertTrue(
            any(
                l[:2] in ("M ", "A ") and l.endswith("staged.txt")
                for l in status
            )
        )
        subprocess.run(["git", "reset", "-q"], cwd=str(vault), capture_output=True)
        self.core.finalize_daily_links_reconciliation(
            vault, config, sync_succeeded=True,
            cursor_path=cur, pending_path=pend,
        )

    def test_routing_change_rehomes_and_stages_dirty_config(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("reroute")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        # Dirty (uncommitted) routing change: root + format → nested.
        _write_daily_config(
            vault, {"folder": "nested", "format": "YYYY/MM"}
        )
        config2 = self.core.load_daily_notes_config(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config2, cursor_path=cur
        )
        self.assertTrue(plan.routing_changed)
        self.assertTrue(plan.config_commit_needed)
        steps = [(t.slug, t.operation, t.routing) for t in plan.transitions]
        self.assertIn(("t1", "ensure", "current"), steps)
        self.assertIn(("t1", "remove", "prior"), steps)
        outcome = self.core.apply_daily_links_reconciliation(
            vault, profile, config2, plan, pending_path=pend
        )
        self.assertTrue(outcome.commit_created)
        shown = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=str(vault), check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertEqual(
            sorted(shown),
            sorted([
                ".obsidian/daily-notes.json",
                f"journal/{_WR_D1}.md",
                "nested/2026/09.md",  # YYYY/MM format: year/month nested path
            ]),
        )
        self.core.finalize_daily_links_reconciliation(
            vault, config2, sync_succeeded=True,
            cursor_path=cur, pending_path=pend,
        )
        cursor = self.core.load_daily_links_reconcile_cursor(cur)
        self.assertEqual(cursor.daily_folder, "nested")
        self.assertEqual(cursor.daily_format, "YYYY/MM")
        self.assertIn(
            "- [[t1]]", _reconcile_note(vault, "nested/2026/09.md")
        )
        self.assertNotIn(
            "- [[t1]]", _reconcile_note(vault, f"journal/{_WR_D1}.md")
        )

    def test_routing_change_with_committed_config_skips_config_staging(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("reroute2")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        _write_daily_config(vault, {"folder": "nested"})
        _reconcile_commit(vault, "config change")
        config2 = self.core.load_daily_notes_config(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config2, cursor_path=cur
        )
        self.assertTrue(plan.routing_changed)
        self.assertFalse(plan.config_commit_needed)
        outcome = self.core.apply_daily_links_reconciliation(
            vault, profile, config2, plan, pending_path=pend
        )
        shown = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=str(vault), check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertNotIn(".obsidian/daily-notes.json", shown)
        self.core.finalize_daily_links_reconciliation(
            vault, config2, sync_succeeded=True,
            cursor_path=cur, pending_path=pend,
        )

    def test_prepare_overflow_fails_closed_no_partial_cursor(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("overflow")
        for i in range(self.core.MAX_DAILY_PROJECTION_TARGETS + 1):
            _reconcile_write_task(
                vault, f"t{i:02d}", scheduled=f"2026-12-{i + 1:02d}"
            )
        _reconcile_commit(vault)
        with self.assertRaises(self.core.CoreError):
            self.core.prepare_daily_links_reconciliation(
                vault, profile, self.core.load_daily_notes_config(vault),
                cursor_path=cur,
            )
        self.assertFalse(cur.exists())
        self.assertFalse(pend.exists())

    def test_head_dual_location_ambiguous_fails_closed(self) -> None:
        vault, _sub, profile, cur, pend = self._setup(
            "dual", move_archived=True
        )
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        (vault / "archive").mkdir()
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1, folder="archive")
        _reconcile_commit(vault)
        with self.assertRaises(self.core.CoreError):
            self.core.prepare_daily_links_reconciliation(
                vault, profile, self.core.load_daily_notes_config(vault),
                cursor_path=cur,
            )
        self.assertFalse(pend.exists())

    def test_pending_record_is_structural_and_identity_checked(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("pending")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        config = self.core.load_daily_notes_config(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config, cursor_path=cur
        )
        outcome = self.core.apply_daily_links_reconciliation(
            vault, profile, config, plan, pending_path=pend
        )
        pending = self.core.load_daily_links_reconcile_pending(pend)
        self.assertEqual(pending.from_head, plan.from_head)
        self.assertEqual(pending.to_head, outcome.pending.to_head)
        self.assertGreater(pending.started_at, 0)
        # The pending structurally pins the applied routing snapshot.
        self.assertEqual(pending.daily_folder, config.folder)
        self.assertEqual(pending.daily_format, config.format)
        payload = json.loads(pend.read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload.keys()),
            {"schema", "version", "from_head", "to_head", "started_at",
             "daily_folder", "daily_format"},
        )
        cursor = self.core.finalize_daily_links_reconciliation(
            vault, config, sync_succeeded=True,
            cursor_path=cur, pending_path=pend,
        )
        self.assertEqual(cursor.reconciled_head, pending.to_head)
        self.assertEqual(
            cursor.projection_format,
            self.core.DAILY_LINKS_PROJECTION_FORMAT_VERSION,
        )
        self.assertFalse(pend.exists())

    def test_finalize_rejects_config_changed_after_apply_then_converges(
        self,
    ) -> None:
        vault, _sub, profile, cur, pend = self._setup("reroute3")
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        # Routing-change cycle applied: pending pins the applied routing.
        _write_daily_config(vault, {"folder": "nested"})
        config_nested = self.core.load_daily_notes_config(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config_nested, cursor_path=cur
        )
        outcome = self.core.apply_daily_links_reconciliation(
            vault, profile, config_nested, plan, pending_path=pend
        )
        pending = self.core.load_daily_links_reconcile_pending(pend)
        self.assertEqual(pending.daily_folder, "nested")
        cursor_before = self.core.load_daily_links_reconcile_cursor(cur)
        # Finalize with the stale pre-apply config: routing mismatch,
        # old cursor and pending preserved.
        stale_config = self.core.DailyNotesConfig(
            folder="journal", format=config_nested.format
        )
        with self.assertRaises(self.core.CoreError):
            self.core.finalize_daily_links_reconciliation(
                vault, stale_config, sync_succeeded=True,
                cursor_path=cur, pending_path=pend,
            )
        self.assertEqual(
            self.core.load_daily_links_reconcile_cursor(cur), cursor_before
        )
        self.assertEqual(
            self.core.load_daily_links_reconcile_pending(pend), pending
        )
        # Config edited again before finalize: still a routing mismatch.
        _write_daily_config(vault, {"folder": "other"})
        config_other = self.core.load_daily_notes_config(vault)
        with self.assertRaises(self.core.CoreError):
            self.core.finalize_daily_links_reconciliation(
                vault, config_other, sync_succeeded=True,
                cursor_path=cur, pending_path=pend,
            )
        self.assertEqual(
            self.core.load_daily_links_reconcile_cursor(cur), cursor_before
        )
        self.assertEqual(
            self.core.load_daily_links_reconcile_pending(pend), pending
        )
        # A fresh prepare/apply/finalize under the actual current config
        # converges: old-link routing comes from the pending's pinned
        # routing, so the link is re-homed exactly once with no
        # duplicate or lost bare link.
        plan, outcome, cursor = self._run(
            vault, profile, config_other, cur, pend
        )
        self.assertEqual(cursor.daily_folder, "other")
        self.assertFalse(pend.exists())
        self.assertIn(
            "- [[t1]]", _reconcile_note(vault, f"other/{_WR_D1}.md")
        )
        self.assertNotIn(
            "- [[t1]]", _reconcile_note(vault, f"nested/{_WR_D1}.md")
        )
        self.assertNotIn(
            "- [[t1]]", _reconcile_note(vault, f"journal/{_WR_D1}.md")
        )

    def test_finalize_stale_pending_after_cursor_advance_clears(self) -> None:
        vault, _sub, profile, cur, pend = self._setup("stale")
        base_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(vault), check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        _reconcile_write_task(vault, "t1", scheduled=_WR_D1)
        _reconcile_commit(vault)
        config = self.core.load_daily_notes_config(vault)
        self._run(vault, profile, config, cur, pend)
        cursor = self.core.load_daily_links_reconcile_cursor(cur)
        # Simulate the cursor-written/pending-not-cleared crash window:
        # a pending whose to_head already equals the advanced cursor.
        stale = self.core.DailyLinksReconcilePending(
            from_head=base_head,
            to_head=cursor.reconciled_head,
            started_at=1_700_000_000,
            daily_folder=config.folder,
            daily_format=config.format,
        )
        self.core.write_daily_links_reconcile_pending(stale, pend)
        advanced = self.core.finalize_daily_links_reconciliation(
            vault, config, sync_succeeded=True,
            cursor_path=cur, pending_path=pend,
        )
        # Success: stale pending cleared, cursor unchanged.
        self.assertEqual(advanced, cursor)
        self.assertFalse(pend.exists())
        # Move HEAD past the cursor head so a "lagged cursor" state
        # exists for the base-identity case below.
        _reconcile_write_task(vault, "t9", scheduled=_WR_D3)
        _reconcile_commit(vault)
        # Every other mismatch still fails closed: a pending that
        # matches neither the current HEAD nor the cursor head.
        foreign_to = "c" * 40
        foreign = self.core.DailyLinksReconcilePending(
            from_head=base_head,
            to_head=foreign_to,
            started_at=1_700_000_001,
            daily_folder=config.folder,
            daily_format=config.format,
        )
        self.core.write_daily_links_reconcile_pending(foreign, pend)
        with self.assertRaises(self.core.CoreError):
            self.core.finalize_daily_links_reconciliation(
                vault, config, sync_succeeded=True,
                cursor_path=cur, pending_path=pend,
            )
        self.assertEqual(
            self.core.load_daily_links_reconcile_cursor(cur), cursor
        )
        self.assertTrue(pend.exists())
        # A head-matching pending with a foreign base (and the cursor
        # lagging) also still fails the base identity check.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(vault), check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertNotEqual(head, cursor.reconciled_head)
        lagged = self.core.DailyLinksReconcilePending(
            from_head="d" * 40,
            to_head=head,
            started_at=1_700_000_002,
            daily_folder=config.folder,
            daily_format=config.format,
        )
        self.core.write_daily_links_reconcile_pending(lagged, pend)
        with self.assertRaises(self.core.CoreError):
            self.core.finalize_daily_links_reconciliation(
                vault, config, sync_succeeded=True,
                cursor_path=cur, pending_path=pend,
            )
        self.assertTrue(pend.exists())

    def test_commit_helper_allows_max_notes_plus_config_rider(self) -> None:
        vault, _sub, _profile, _cur, _pend = self._setup("commitbound")
        notes = []
        for i in range(self.core.MAX_DAILY_PROJECTION_TARGETS):
            rel = f"journal/2026-06-{i + 1:02d}.md"
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_text("x\n", encoding="utf-8")
            notes.append(rel)
        # Make the tracked config genuinely dirty so the rider has a
        # change to stage (identical bytes would stage nothing).
        (vault / ".obsidian" / "daily-notes.json").write_text(
            '{"folder": "journal", "format": "YYYY-MM-DD"}',
            encoding="utf-8",
        )
        # Exactly MAX note targets plus the single config rider: legal.
        created = self.core.git_commit_reconcile_targets(
            vault,
            [vault / rel for rel in notes]
            + [vault / self.core.RECONCILE_CONFIG_RELPATH],
        )
        self.assertTrue(created)
        shown = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=str(vault), check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertEqual(len(shown), self.core.MAX_DAILY_PROJECTION_TARGETS + 1)
        self.assertIn(self.core.RECONCILE_CONFIG_RELPATH, shown)
        # One note target more than the bound fails closed.
        (vault / "journal" / "2026-07-01.md").write_text("x\n", encoding="utf-8")
        with self.assertRaises(self.core.ValidationError):
            self.core.git_commit_reconcile_targets(
                vault,
                [vault / rel for rel in notes]
                + [vault / "journal" / "2026-07-01.md"]
                + [vault / self.core.RECONCILE_CONFIG_RELPATH],
            )
        # A non-note, non-config path is rejected outright.
        (vault / "other.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(self.core.ValidationError):
            self.core.git_commit_reconcile_targets(
                vault, [vault / "other.txt"]
            )

    def test_routing_change_full_bound_commits_16_notes_plus_config(
        self,
    ) -> None:
        vault, _sub, profile, cur, pend = self._setup("bound17")
        dates = [f"2026-08-{i + 1:02d}" for i in range(8)]
        for i, date in enumerate(dates):
            _reconcile_write_task(vault, f"t{i}", scheduled=date)
        _reconcile_commit(vault)
        self._run(
            vault, profile, self.core.load_daily_notes_config(vault), cur, pend
        )
        # Dirty routing change: 8 ensures + 8 removes = exactly MAX
        # changed notes, plus the config rider: one explicit 17-path
        # commit must succeed (previously the total-path bound rejected
        # it after the projections had already been applied).
        _write_daily_config(vault, {"folder": "daily"})
        config2 = self.core.load_daily_notes_config(vault)
        plan = self.core.prepare_daily_links_reconciliation(
            vault, profile, config2, cursor_path=cur
        )
        self.assertEqual(len(plan.transitions), self.core.MAX_DAILY_PROJECTION_TARGETS)
        outcome = self.core.apply_daily_links_reconciliation(
            vault, profile, config2, plan, pending_path=pend
        )
        self.assertTrue(outcome.commit_created)
        self.assertEqual(
            len(outcome.changed_targets), self.core.MAX_DAILY_PROJECTION_TARGETS
        )
        shown = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=str(vault), check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertEqual(len(shown), self.core.MAX_DAILY_PROJECTION_TARGETS + 1)
        self.assertIn(self.core.RECONCILE_CONFIG_RELPATH, shown)
        self.core.finalize_daily_links_reconciliation(
            vault, config2, sync_succeeded=True,
            cursor_path=cur, pending_path=pend,
        )
        cursor = self.core.load_daily_links_reconcile_cursor(cur)
        self.assertEqual(cursor.daily_folder, "daily")


# ---------------------------------------------------------------------------
# W4a: reconciliation wired into engine mutations (MCP-only integration)
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_yaml(), "PyYAML required")
class ReconcileEngineIntegrationTests(unittest.TestCase):
    """Every normal mutation reconciles under the held lock BEFORE
    preflight/mutation; delete guards a dirty target with zero
    reconciliation I/O; disabled modes are fully inert."""

    def setUp(self) -> None:
        self.core = _load_core()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tnm_w4a_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine(self, name, *, daily=True, reconcile=True):
        sub = self.tmpdir / name
        sub.mkdir()
        cursor = sub / "cursor.json"
        pending = sub / "pending.json"
        engine, vault, _gbrain_bin = _make_daily_engine(
            self.core, sub,
            daily_links_enabled=daily,
            reconcile_enabled=reconcile,
            reconcile_cursor_path=cursor,
            reconcile_pending_path=pending,
        )
        return engine, vault, cursor, pending

    def _record_reconciliation(self, recorder):
        """Wrap the reconciliation lifecycle + preflight/capture seams with
        event recorders; returns a restore-free context manager."""
        real = {
            name: getattr(self.core, name)
            for name in (
                "prepare_daily_links_reconciliation",
                "apply_daily_links_reconciliation",
                "finalize_daily_links_reconciliation",
                "gbrain_sync_incremental",
                "git_preflight_commit",
                "gbrain_capture",
                "gbrain_delete",
            )
        }

        def make_spy(name):
            def spy(*args, **kwargs):
                recorder["events"].append(name)
                if name != "gbrain_sync_incremental":
                    recorder.setdefault(name, []).append(args)
                return real[name](*args, **kwargs)

            return spy

        patches = [
            mock.patch.object(self.core, name, side_effect=make_spy(name))
            for name in real
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_mutation_reconciles_before_preflight_same_config_snapshot(
        self,
    ) -> None:
        engine, vault, cursor, pending = self._engine("order")
        created = engine.create("t1", "Task One", scheduled=_WR_D1, body="b")
        self.assertEqual(created.state, self.core.APPLIED_AND_COMMITTED)
        # Cursor established by the create-time bootstrap; now an external
        # commit lands while the cursor lags.
        self.assertTrue(cursor.exists())
        _reconcile_write_task(vault, "t2", scheduled=_WR_D2)
        _reconcile_commit(vault, "external add")
        recorder = {"events": []}
        self._record_reconciliation(recorder)
        result = engine.complete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        events = recorder["events"]
        # Full lifecycle runs BEFORE the existing preflight and mutation.
        self.assertEqual(events[:4], [
            "prepare_daily_links_reconciliation",
            "apply_daily_links_reconciliation",
            "gbrain_sync_incremental",
            "finalize_daily_links_reconciliation",
        ])
        self.assertLess(
            events.index("finalize_daily_links_reconciliation"),
            events.index("git_preflight_commit"),
        )
        # Reconciliation's own sync precedes preflight's sync; capture is last.
        self.assertEqual(events[4:6], ["git_preflight_commit", "gbrain_sync_incremental"])
        self.assertLess(
            events.index("git_preflight_commit"),
            events.index("gbrain_capture"),
        )
        # The lagging external change was reconciled by the mutation.
        self.assertIn(
            "- [[t2]]", _reconcile_note(vault, f"journal/{_WR_D2}.md")
        )
        # One validated config snapshot passes prepare/apply/finalize.
        configs = [args[2] for args in recorder["prepare_daily_links_reconciliation"]]
        self.assertIs(configs[0], recorder["apply_daily_links_reconciliation"][0][2])
        self.assertIs(
            configs[0],
            recorder["finalize_daily_links_reconciliation"][0][1],
        )
        self.assertIsInstance(configs[0], self.core.DailyNotesConfig)

    def test_reconcile_failure_skips_preflight_and_mutation(self) -> None:
        engine, vault, cursor, pending = self._engine("fail")
        engine.create("t1", "Task One", scheduled=_WR_D1, body="b")
        _reconcile_write_task(vault, "t2", scheduled=_WR_D2)
        _reconcile_commit(vault, "external add")
        task_before = (vault / "tasks" / "t1.md").read_bytes()
        cursor_before = cursor.read_bytes()
        recorder = {"events": []}
        self._record_reconciliation(recorder)
        with mock.patch.object(
            self.core,
            "finalize_daily_links_reconciliation",
            side_effect=self.core.CoreError("injected finalize failure"),
        ):
            with self.assertRaises(self.core.CoreError):
                engine.complete("t1")
        # Cursor semantics preserved: old cursor + pending intact for replay.
        self.assertEqual(cursor.read_bytes(), cursor_before)
        self.assertTrue(pending.exists())
        # Preflight and the mutation never ran; the reconciliation itself
        # got as far as apply (its commit landed) but not finalize.
        self.assertNotIn("git_preflight_commit", recorder["events"])
        self.assertNotIn("gbrain_capture", recorder["events"])
        self.assertIn("apply_daily_links_reconciliation", recorder["events"])
        self.assertEqual((vault / "tasks" / "t1.md").read_bytes(), task_before)

    def test_prepare_failure_skips_preflight_and_mutation(self) -> None:
        engine, vault, cursor, pending = self._engine("prepfail")
        engine.create("t1", "Task One", scheduled=_WR_D1, body="b")
        # Corrupt the established cursor: prepare fails closed.
        cursor.write_text("{corrupt", encoding="utf-8")
        with mock.patch.object(
            self.core, "gbrain_capture", side_effect=AssertionError("must not capture")
        ):
            with self.assertRaises(self.core.CoreError):
                engine.complete("t1")
        self.assertEqual(cursor.read_text(encoding="utf-8"), "{corrupt")
        self.assertFalse(pending.exists())

    def test_reconciliation_inert_when_master_disabled(self) -> None:
        # Master off + reconcile switch on: reconciliation must be fully
        # inert (no lifecycle calls, no cursor/pending I/O).
        engine, vault, cursor, pending = self._engine(
            "inert_master", daily=False, reconcile=True
        )
        recorder = {"events": []}
        self._record_reconciliation(recorder)
        result = engine.create("t1", "Task One", body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        reconcilers = [
            name for name in recorder["events"]
            if name.endswith("_reconciliation")
        ]
        self.assertEqual(reconcilers, [])
        self.assertFalse(cursor.exists())
        self.assertFalse(pending.exists())

    def test_reconciliation_inert_when_switch_disabled(self) -> None:
        # Master on + reconcile switch off: daily projections still work,
        # reconciliation stays inert.
        engine, vault, cursor, pending = self._engine(
            "inert_switch", daily=True, reconcile=False
        )
        recorder = {"events": []}
        self._record_reconciliation(recorder)
        result = engine.create("t1", "Task One", scheduled=_WR_D1, body="b")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        self.assertEqual(result.daily_link_state, self.core.DAILY_LINK_APPLIED)
        reconcilers = [
            name for name in recorder["events"]
            if name.endswith("_reconciliation")
        ]
        self.assertEqual(reconcilers, [])
        self.assertFalse(cursor.exists())
        self.assertFalse(pending.exists())
        self.assertIn(
            "- [[t1]]", _reconcile_note(vault, f"journal/{_WR_D1}.md")
        )

    def test_delete_dirty_target_zero_reconcile_io(self) -> None:
        engine, vault, cursor, pending = self._engine("dirtydelete")
        engine.create("t1", "Task One", scheduled=_WR_D1, body="b")
        self.assertTrue(cursor.exists())
        cursor_before = cursor.read_bytes()
        # Dirty the delete target (uncommitted external edit).
        task_path = vault / "tasks" / "t1.md"
        task_path.write_bytes(task_path.read_bytes() + b"dirty edit\n")
        recorder = {"events": []}
        self._record_reconciliation(recorder)
        with self.assertRaises(self.core.ValidationError):
            engine.delete("t1")
        reconcilers = [
            name for name in recorder["events"]
            if name.endswith("_reconciliation")
        ]
        # Zero reconciliation calls and zero cursor/pending I/O.
        self.assertEqual(reconcilers, [])
        self.assertEqual(cursor.read_bytes(), cursor_before)
        self.assertFalse(pending.exists())
        self.assertFalse(recorder["events"])
        # The task and its dirty edit are untouched.
        self.assertTrue(task_path.read_bytes().endswith(b"dirty edit\n"))

    def test_delete_clean_reconciles_before_preflight_and_delete(self) -> None:
        engine, vault, cursor, pending = self._engine("cleandelete")
        engine.create("t1", "Task One", scheduled=_WR_D1, body="b")
        # External committed change while the cursor lags.
        _reconcile_write_task(vault, "t2", scheduled=_WR_D2)
        _reconcile_commit(vault, "external add")
        recorder = {"events": []}
        self._record_reconciliation(recorder)
        result = engine.delete("t1")
        self.assertEqual(result.state, self.core.APPLIED_AND_COMMITTED)
        events = recorder["events"]
        # Clean target: reconciliation runs first (guard already passed),
        # then preflight, then the delete lifecycle.
        self.assertEqual(events[:4], [
            "prepare_daily_links_reconciliation",
            "apply_daily_links_reconciliation",
            "gbrain_sync_incremental",
            "finalize_daily_links_reconciliation",
        ])
        self.assertLess(
            events.index("finalize_daily_links_reconciliation"),
            events.index("git_preflight_commit"),
        )
        self.assertLess(
            events.index("git_preflight_commit"),
            events.index("gbrain_delete"),
        )
        # The lagging external change was reconciled before the delete;
        # the deleted task's own daily link is gone afterwards.
        self.assertIn(
            "- [[t2]]", _reconcile_note(vault, f"journal/{_WR_D2}.md")
        )
        self.assertNotIn(
            "- [[t1]]", _reconcile_note(vault, f"journal/{_WR_D1}.md")
        )
        self.assertFalse((vault / "tasks" / "t1.md").exists())


if __name__ == "__main__":
    unittest.main()
