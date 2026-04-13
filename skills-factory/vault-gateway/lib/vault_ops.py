from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import shutil

from lib.common import TAG_PATTERN


STANDARD_DIRS = [
    "00-Inbox",
    "01-Projects",
    "02-Areas",
    "03-Resources",
    "04-Archive",
    "05-People",
    "06-Meetings",
    "07-Daily",
    "MOC",
    "Templates",
    "Meta",
]


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9\s._-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-._")


def _resolve_relative_path(vault_root: Path, relative_path: str) -> Path:
    candidate_raw = (relative_path or "").strip().replace("\\", "/")
    if not candidate_raw:
        raise ValueError("Path is required")
    if candidate_raw.startswith("/"):
        raise ValueError("Absolute paths are not allowed")

    vault_real = vault_root.resolve(strict=False)
    candidate = (vault_root / candidate_raw).resolve(strict=False)
    if candidate != vault_real and vault_real not in candidate.parents:
        raise ValueError("Path escapes vault root")
    return candidate


def _relative(vault_root: Path, path: Path) -> str:
    return str(path.resolve(strict=False).relative_to(vault_root.resolve(strict=False)))


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _is_hidden(path: Path) -> bool:
    return path.name.startswith(".")


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1", errors="ignore")
    except OSError:
        return ""


def _append_log(vault_root: Path, action: str, details: dict) -> None:
    meta_dir = vault_root / "Meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    log_file = meta_dir / "vault-gateway-log.md"
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps(details, ensure_ascii=True)
    line = f"- {timestamp} | {action} | {payload}\n"
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _normalize_tags(raw_tags: object) -> list[str]:
    if raw_tags is None:
        return []
    if isinstance(raw_tags, str):
        items = [item.strip() for item in raw_tags.split(",") if item.strip()]
        return items
    if isinstance(raw_tags, list):
        tags = []
        for item in raw_tags:
            if isinstance(item, str) and item.strip():
                tags.append(item.strip())
        return tags
    return []


def _find_template(vault_root: Path, hint: str) -> Path | None:
    if not hint:
        return None
    template_dir = vault_root / "Templates"
    if not template_dir.exists():
        return None

    normalized_hint = hint.strip().lower()
    candidates = [path for path in template_dir.rglob("*.md") if path.is_file()]

    for candidate in candidates:
        stem = candidate.stem.lower()
        name = candidate.name.lower()
        if normalized_hint == stem or normalized_hint == name:
            return candidate

    for candidate in candidates:
        stem = candidate.stem.lower()
        name = candidate.name.lower()
        if normalized_hint in stem or normalized_hint in name:
            return candidate

    return None


def _default_capture_title(text: str) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return datetime.utcnow().strftime("capture-%Y%m%d-%H%M%S")
    words = cleaned.split(" ")
    return " ".join(words[:8])


def _resolve_note_path(vault_root: Path, path: str | None = None) -> Path:
    if not path:
        raise ValueError("Field 'path' is required")

    note_path = _resolve_relative_path(vault_root, path)
    if note_path.suffix.lower() != ".md":
        raise ValueError("Only markdown notes (.md) are supported")
    if not note_path.exists() or not note_path.is_file():
        raise ValueError(f"Note not found at path: {path}")

    return note_path


def capture_note(
    vault_root: Path,
    text: str,
    title: str | None = None,
    target_folder: str = "00-Inbox",
    template_hint: str | None = None,
    tags: object = None,
) -> dict:
    body = (text or "").strip()
    if not body:
        raise ValueError("Field 'text' is required")

    vault_root.mkdir(parents=True, exist_ok=True)
    target_dir = _resolve_relative_path(vault_root, target_folder)
    target_dir.mkdir(parents=True, exist_ok=True)

    selected_title = (title or "").strip() or _default_capture_title(body)
    slug = _slugify(selected_title) or datetime.utcnow().strftime("capture-%Y%m%d-%H%M%S")
    note_path = _unique_path(target_dir / f"{slug}.md")

    normalized_tags = _normalize_tags(tags)
    template_path = _find_template(vault_root, template_hint or "")
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if template_path:
        template_text = _safe_read_text(template_path).rstrip()
        content = (
            f"{template_text}\n\n"
            "## Captured Context\n\n"
            f"{body}\n"
        )
    else:
        frontmatter = ["---", "type: note", f"created: {timestamp}"]
        if normalized_tags:
            frontmatter.append(
                "tags: [" + ", ".join(tag.strip().replace("'", "") for tag in normalized_tags) + "]"
            )
        frontmatter.append("---")
        frontmatter_text = "\n".join(frontmatter)
        content = f"{frontmatter_text}\n\n# {selected_title}\n\n{body}\n"

    note_path.write_text(content, encoding="utf-8")

    result = {
        "path": _relative(vault_root, note_path),
        "title": selected_title,
        "template_used": _relative(vault_root, template_path) if template_path else None,
        "target_folder": _relative(vault_root, target_dir),
    }
    _append_log(vault_root, "note.capture", result)
    return result


def update_note(
    vault_root: Path,
    text: str,
    path: str | None = None,
    mode: str = "append",
) -> dict:
    payload_text = (text or "").strip()
    if not payload_text:
        raise ValueError("Field 'text' is required")

    note_path = _resolve_note_path(vault_root, path=path)
    existing = _safe_read_text(note_path)

    normalized_mode = (mode or "append").strip().lower()
    if normalized_mode == "replace":
        updated = payload_text + "\n"
    elif normalized_mode == "prepend":
        updated = payload_text + "\n\n" + existing
    else:
        separator = "\n\n" if existing.strip() else ""
        updated = existing.rstrip() + separator + payload_text + "\n"

    note_path.write_text(updated, encoding="utf-8")

    result = {
        "path": _relative(vault_root, note_path),
        "mode": normalized_mode,
    }
    _append_log(vault_root, "note.update", result)
    return result


def search_notes(vault_root: Path, query: str, limit: int = 20, path_prefix: str | None = None) -> dict:
    needle = (query or "").strip().lower()
    if not needle:
        raise ValueError("Field 'query' is required")

    base_dir = vault_root
    if path_prefix:
        base_dir = _resolve_relative_path(vault_root, path_prefix)
        if not base_dir.exists() or not base_dir.is_dir():
            raise ValueError("path_prefix does not exist or is not a directory")

    safe_limit = max(1, min(int(limit or 20), 200))
    matches = []

    for note_path in base_dir.rglob("*.md"):
        if not note_path.is_file():
            continue

        rel = _relative(vault_root, note_path)
        content = _safe_read_text(note_path)
        lowered_content = content.lower()
        lowered_rel = rel.lower()
        stem = note_path.stem.lower()

        score = 0
        if needle in stem:
            score += 5
        if needle in lowered_rel:
            score += 3
        if needle in lowered_content:
            score += 2

        if score == 0:
            continue

        snippet = ""
        for line in content.splitlines():
            if needle in line.lower():
                snippet = line.strip()[:200]
                break

        matches.append(
            {
                "path": rel,
                "title": note_path.stem,
                "score": score,
                "snippet": snippet,
            }
        )

    matches.sort(key=lambda item: (-item["score"], item["path"]))
    return {
        "query": query,
        "results": matches[:safe_limit],
        "total_matches": len(matches),
    }


def _add_link_to_note(note_path: Path, link_text: str) -> bool:
    content = _safe_read_text(note_path)
    if link_text in content:
        return False

    stripped = content.rstrip()
    if "## Links" in stripped:
        updated = stripped + f"\n- {link_text}\n"
    else:
        updated = stripped + f"\n\n## Links\n- {link_text}\n"

    note_path.write_text(updated, encoding="utf-8")
    return True


def link_notes(
    vault_root: Path,
    source_path: str | None = None,
    target_path: str | None = None,
    bidirectional: bool = False,
) -> dict:
    source = _resolve_note_path(vault_root, path=source_path)
    target = _resolve_note_path(vault_root, path=target_path)

    forward_link = f"[[{target.stem}]]"
    inserted_forward = _add_link_to_note(source, forward_link)

    inserted_back = False
    if bidirectional:
        backward_link = f"[[{source.stem}]]"
        inserted_back = _add_link_to_note(target, backward_link)

    result = {
        "source": _relative(vault_root, source),
        "target": _relative(vault_root, target),
        "inserted_forward": inserted_forward,
        "inserted_back": inserted_back,
        "bidirectional": bool(bidirectional),
    }
    _append_log(vault_root, "note.link", result)
    return result


def file_note(
    vault_root: Path,
    source_path: str | None = None,
    target_folder: str = "01-Projects",
) -> dict:
    source = _resolve_note_path(vault_root, path=source_path)
    target_dir = _resolve_relative_path(vault_root, target_folder)
    target_dir.mkdir(parents=True, exist_ok=True)

    destination = _unique_path(target_dir / source.name)
    shutil.move(str(source), str(destination))

    result = {
        "from": _relative(vault_root, source),
        "to": _relative(vault_root, destination),
    }
    _append_log(vault_root, "note.file", result)
    return result


def scan_vault(vault_root: Path) -> dict:
    exists = vault_root.exists()
    result = {
        "vault_root": str(vault_root),
        "exists": exists,
        "top_level_dirs": [],
        "top_level_files": [],
        "missing_standard_dirs": [],
        "non_standard_root_entries": [],
        "markdown_files": 0,
        "all_files": 0,
    }

    if not exists:
        result["missing_standard_dirs"] = list(STANDARD_DIRS)
        return result

    top_entries = sorted(vault_root.iterdir(), key=lambda item: item.name.lower())
    for entry in top_entries:
        if entry.is_dir():
            result["top_level_dirs"].append(entry.name)
        else:
            result["top_level_files"].append(entry.name)

        if not _is_hidden(entry) and entry.name not in STANDARD_DIRS:
            result["non_standard_root_entries"].append(entry.name)

    for directory in STANDARD_DIRS:
        if not (vault_root / directory).exists():
            result["missing_standard_dirs"].append(directory)

    for path in vault_root.rglob("*"):
        if path.is_file():
            result["all_files"] += 1
            if path.suffix.lower() == ".md":
                result["markdown_files"] += 1

    return result


def build_port_plan(scan: dict, destructive: bool) -> dict:
    actions = []
    missing = scan.get("missing_standard_dirs", [])
    non_standard = scan.get("non_standard_root_entries", [])

    if missing:
        actions.append(f"Create missing standard directories: {', '.join(missing)}")
    else:
        actions.append("All standard directories already exist")

    actions.append("Create baseline MOC and Meta files when absent")

    if destructive:
        if non_standard:
            actions.append(
                "Move non-standard top-level entries into 04-Archive/Imported-Root-<timestamp>"
            )
        else:
            actions.append("No non-standard top-level entries to move")
    else:
        if non_standard:
            actions.append("Keep non-standard top-level entries in place")

    return {
        "destructive": destructive,
        "vault_exists": scan.get("exists", False),
        "missing_standard_dirs": missing,
        "non_standard_root_entries": non_standard,
        "actions": actions,
    }


def _create_baseline_files(vault_root: Path) -> list[str]:
    created = []

    moc_index = vault_root / "MOC" / "Index.md"
    if not moc_index.exists():
        moc_index.parent.mkdir(parents=True, exist_ok=True)
        moc_index.write_text(
            "# Vault Index\n\n"
            "## Areas\n"
            "- [[02-Areas]]\n"
            "\n"
            "## Core\n"
            "- [[00-Inbox]]\n"
            "- [[01-Projects]]\n"
            "- [[03-Resources]]\n",
            encoding="utf-8",
        )
        created.append("MOC/Index.md")

    vault_structure = vault_root / "Meta" / "vault-structure.md"
    if not vault_structure.exists():
        vault_structure.parent.mkdir(parents=True, exist_ok=True)
        vault_structure.write_text(
            "# Vault Structure\n\n"
            "Managed by vault-gateway onboarding/port flows.\n",
            encoding="utf-8",
        )
        created.append("Meta/vault-structure.md")

    return created


def apply_non_destructive_port(vault_root: Path, reason: str) -> dict:
    vault_root.mkdir(parents=True, exist_ok=True)

    created_dirs = []
    for directory in STANDARD_DIRS:
        path = vault_root / directory
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(directory)

    created_files = _create_baseline_files(vault_root)

    result = {
        "mode": "non-destructive",
        "created_dirs": created_dirs,
        "created_files": created_files,
        "moved_entries": [],
        "reason": reason,
    }
    _append_log(vault_root, "apply_non_destructive_port", result)
    return result


def apply_destructive_port(vault_root: Path, reason: str) -> dict:
    baseline = apply_non_destructive_port(vault_root, reason)

    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    archive_bucket = vault_root / "04-Archive" / f"Imported-Root-{timestamp}"
    moved_entries = []

    non_standard_entries = []
    for entry in sorted(vault_root.iterdir(), key=lambda item: item.name.lower()):
        if _is_hidden(entry):
            continue
        if entry.name in STANDARD_DIRS:
            continue
        non_standard_entries.append(entry)

    if non_standard_entries:
        archive_bucket.mkdir(parents=True, exist_ok=True)

    for entry in non_standard_entries:
        target = archive_bucket / entry.name
        suffix = 1
        while target.exists():
            target = archive_bucket / f"{entry.name}-{suffix}"
            suffix += 1
        shutil.move(str(entry), str(target))
        moved_entries.append(f"{entry.name} -> {target.relative_to(vault_root)}")

    result = {
        "mode": "destructive",
        "created_dirs": baseline["created_dirs"],
        "created_files": baseline["created_files"],
        "moved_entries": moved_entries,
        "reason": reason,
    }
    _append_log(vault_root, "apply_destructive_port", result)
    return result


def summarize_inbox(vault_root: Path) -> dict:
    inbox = vault_root / "00-Inbox"
    if not inbox.exists():
        return {
            "inbox_path": str(inbox),
            "exists": False,
            "total_files": 0,
            "by_extension": {},
            "oldest_file": None,
            "newest_file": None,
        }

    files = [path for path in inbox.rglob("*") if path.is_file()]
    by_extension: Counter[str] = Counter()
    oldest = None
    newest = None

    for file_path in files:
        ext = file_path.suffix.lower() or "(no extension)"
        by_extension[ext] += 1
        stat = file_path.stat()
        if oldest is None or stat.st_mtime < oldest[1]:
            oldest = (str(file_path.relative_to(vault_root)), stat.st_mtime)
        if newest is None or stat.st_mtime > newest[1]:
            newest = (str(file_path.relative_to(vault_root)), stat.st_mtime)

    return {
        "inbox_path": str(inbox),
        "exists": True,
        "total_files": len(files),
        "by_extension": dict(sorted(by_extension.items(), key=lambda item: item[0])),
        "oldest_file": oldest[0] if oldest else None,
        "newest_file": newest[0] if newest else None,
    }


def summarize_defrag(vault_root: Path) -> dict:
    scan = scan_vault(vault_root)
    recommendations = []

    if scan["missing_standard_dirs"]:
        recommendations.append("Create missing standard directories")
    if scan["non_standard_root_entries"]:
        recommendations.append("Review non-standard root entries and archive or map them")
    if not recommendations:
        recommendations.append("Structure already aligned with baseline")

    return {
        "scan": scan,
        "recommendations": recommendations,
    }


def summarize_audit(vault_root: Path) -> dict:
    scan = scan_vault(vault_root)
    markdown_files = [path for path in vault_root.rglob("*.md") if path.is_file()]
    missing_frontmatter = 0

    for path in markdown_files:
        text = _safe_read_text(path)
        if not text.startswith("---\n"):
            missing_frontmatter += 1

    return {
        "scan": scan,
        "markdown_without_frontmatter": missing_frontmatter,
    }


def summarize_deep_clean(vault_root: Path) -> dict:
    empty_dirs = []
    for path in vault_root.rglob("*"):
        if path.is_dir() and not any(path.iterdir()):
            empty_dirs.append(str(path.relative_to(vault_root)))

    stems: dict[str, list[str]] = defaultdict(list)
    for path in vault_root.rglob("*.md"):
        stems[path.stem.lower()].append(str(path.relative_to(vault_root)))

    duplicates = {
        stem: locations
        for stem, locations in stems.items()
        if len(locations) > 1
    }

    return {
        "empty_directories": sorted(empty_dirs)[:200],
        "duplicate_note_stems": duplicates,
    }


def summarize_tag_garden(vault_root: Path) -> dict:
    frequencies: Counter[str] = Counter()
    case_variants: dict[str, set[str]] = defaultdict(set)

    for path in vault_root.rglob("*.md"):
        text = _safe_read_text(path)
        tags = TAG_PATTERN.findall(text)
        for raw_tag in tags:
            normalized = raw_tag.lower()
            frequencies[normalized] += 1
            case_variants[normalized].add(raw_tag)

    top_tags = frequencies.most_common(50)
    variants = {
        key: sorted(list(values))
        for key, values in case_variants.items()
        if len(values) > 1
    }

    return {
        "top_tags": top_tags,
        "case_variants": variants,
        "total_unique_tags": len(frequencies),
    }
