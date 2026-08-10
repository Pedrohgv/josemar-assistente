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
            # Normalize dates in frontmatter for disk.
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
                disk_fm = dict(fm)
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


if __name__ == "__main__":
    unittest.main()
