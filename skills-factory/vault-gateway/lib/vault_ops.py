from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, date
import json
from pathlib import Path
import re
import shutil

from lib.common import TAG_PATTERN

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


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

INDEX_AUTO_CREATE_MIN_NOTES = 3

INDEX_MANAGED_BEGIN = "<!-- VG:BEGIN managed-summary -->"
INDEX_MANAGED_END = "<!-- VG:END managed-summary -->"

STRUCTURE_MANAGED_BEGIN = "<!-- VG:BEGIN managed-structure -->"
STRUCTURE_MANAGED_END = "<!-- VG:END managed-structure -->"


PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


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


def _replace_managed_block(content: str, begin: str, end: str, block_body: str) -> tuple[str, bool]:
    begin_index = content.find(begin)
    end_index = content.find(end)
    replacement = f"{begin}\n{block_body.rstrip()}\n{end}"

    if begin_index != -1 and end_index != -1 and end_index > begin_index:
        end_index += len(end)
        updated = content[:begin_index] + replacement + content[end_index:]
    else:
        base = content.rstrip()
        if base:
            updated = f"{base}\n\n{replacement}\n"
        else:
            updated = replacement + "\n"

    return updated, updated != content


def _extract_managed_block(content: str, begin: str, end: str) -> str:
    begin_index = content.find(begin)
    end_index = content.find(end)
    if begin_index == -1 or end_index == -1 or end_index <= begin_index:
        return ""
    start = begin_index + len(begin)
    return content[start:end_index].strip()


def _extract_markdown_section(content: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}\s*$\n?(.*?)(?=^##\s+|^#\s+|\Z)"
    )
    match = pattern.search(content)
    if not match:
        return ""
    return match.group(1).strip()


def _normalize_heading_label(value: str) -> str:
    label = (value or "").strip()
    label = re.sub(r"\s+#*$", "", label).strip()
    label = re.sub(r"\s+", " ", label)
    return label.casefold()


def _update_markdown_section(body_text: str, heading: str, payload_text: str, prepend: bool = False) -> str:
    normalized_target = _normalize_heading_label(heading)
    if not normalized_target:
        raise ValueError("section_heading is required when using section mode")

    heading_matches = list(re.finditer(r"(?m)^(#{1,6})[ \t]+(.+?)\s*$", body_text))
    matching_indices = [
        index
        for index, match in enumerate(heading_matches)
        if _normalize_heading_label(match.group(2)) == normalized_target
    ]

    if not matching_indices:
        raise ValueError(f"Section '{heading}' was not found in note body")
    if len(matching_indices) > 1:
        raise ValueError(
            f"Multiple sections named '{heading}' were found; use note.read + replace for an explicit edit"
        )

    target_index = matching_indices[0]
    target_match = heading_matches[target_index]
    target_level = len(target_match.group(1))

    section_start = target_match.end()
    section_end = len(body_text)
    for candidate in heading_matches[target_index + 1 :]:
        if len(candidate.group(1)) <= target_level:
            section_end = candidate.start()
            break

    current_section = body_text[section_start:section_end].lstrip("\n")
    insertion = payload_text.strip()
    if prepend:
        if current_section.strip():
            rebuilt_section = "\n" + insertion + "\n\n" + current_section.rstrip() + "\n"
        else:
            rebuilt_section = "\n" + insertion + "\n"
    else:
        if current_section.strip():
            rebuilt_section = "\n" + current_section.rstrip() + "\n" + insertion + "\n"
        else:
            rebuilt_section = "\n" + insertion + "\n"

    return body_text[:section_start] + rebuilt_section + body_text[section_end:]


def _truncate_text(text: str, limit: int = 1200) -> str:
    normalized = (text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "..."


def _find_nearest_index(vault_root: Path, start_dir: Path) -> Path | None:
    vault_real = vault_root.resolve(strict=False)
    current = start_dir.resolve(strict=False)
    if current == vault_real:
        return None
    if vault_real not in current.parents:
        return None

    while True:
        candidate = current / "_index.md"
        if candidate.exists() and candidate.is_file():
            return candidate
        if current == vault_real:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _build_folder_context(vault_root: Path, folder: Path | None) -> dict | None:
    if folder is None:
        return None
    folder_real = folder.resolve(strict=False)
    vault_real = vault_root.resolve(strict=False)
    if folder_real == vault_real or vault_real not in folder_real.parents:
        return None

    context: dict[str, object] = {
        "folder": _relative(vault_root, folder_real),
    }

    index_path = _find_nearest_index(vault_root, folder_real)
    if index_path is None:
        context["index_found"] = False
        return context

    content = _safe_read_text(index_path)
    context["index_found"] = True
    context["index_path"] = _relative(vault_root, index_path)

    working_rules = _extract_markdown_section(content, "Working Rules")
    if working_rules:
        context["working_rules"] = _truncate_text(working_rules, 1600)

    managed_summary = _extract_managed_block(content, INDEX_MANAGED_BEGIN, INDEX_MANAGED_END)
    if managed_summary:
        context["managed_summary"] = _truncate_text(managed_summary, 1600)

    return context


def _build_vault_structure_context(vault_root: Path) -> dict | None:
    structure_path = vault_root / "Meta" / "vault-structure.md"
    if not structure_path.exists() or not structure_path.is_file():
        return None

    content = _safe_read_text(structure_path)
    managed = _extract_managed_block(content, STRUCTURE_MANAGED_BEGIN, STRUCTURE_MANAGED_END)
    if not managed:
        return {
            "path": _relative(vault_root, structure_path),
            "managed_snapshot_present": False,
        }

    return {
        "path": _relative(vault_root, structure_path),
        "managed_snapshot_present": True,
        "managed_snapshot": _truncate_text(managed, 2200),
    }


def _build_operation_context(
    vault_root: Path,
    primary_folder: Path | None = None,
    secondary_folder: Path | None = None,
) -> dict:
    context: dict[str, object] = {}

    primary = _build_folder_context(vault_root, primary_folder)
    if primary is not None:
        context["folder_context"] = primary

    if secondary_folder is not None:
        secondary = _build_folder_context(vault_root, secondary_folder)
        if secondary is not None:
            if primary is None or secondary.get("folder") != primary.get("folder"):
                context["secondary_folder_context"] = secondary

    vault_structure = _build_vault_structure_context(vault_root)
    if vault_structure is not None:
        context["vault_structure_context"] = vault_structure

    return context


def _count_markdown_notes(base_dir: Path, recursive: bool = True) -> int:
    if not base_dir.exists() or not base_dir.is_dir():
        return 0
    iterator = base_dir.rglob("*.md") if recursive else base_dir.glob("*.md")
    count = 0
    for item in iterator:
        if not item.is_file():
            continue
        if item.name == "_index.md":
            continue
        count += 1
    return count


def _list_recent_notes(folder: Path, max_items: int = 5) -> list[Path]:
    notes = []
    for path in folder.glob("*.md"):
        if not path.is_file() or path.name == "_index.md":
            continue
        notes.append(path)
    notes.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return notes[:max_items]


def _build_index_managed_summary(vault_root: Path, folder: Path) -> str:
    rel_folder = _relative(vault_root, folder)
    direct_notes = _count_markdown_notes(folder, recursive=False)
    total_notes = _count_markdown_notes(folder, recursive=True)
    subfolders = [item for item in folder.iterdir() if item.is_dir() and not _is_hidden(item)] if folder.exists() else []
    subfolders.sort(key=lambda item: item.name.lower())
    recent_notes = _list_recent_notes(folder)

    lines = [
        "## Managed Summary",
        f"- Folder: `{rel_folder}`",
        f"- Direct notes: {direct_notes}",
        f"- Total notes (recursive): {total_notes}",
        f"- Subfolders: {len(subfolders)}",
    ]

    if subfolders:
        lines.append("- Subfolder list: " + ", ".join(item.name for item in subfolders[:12]))

    if recent_notes:
        lines.append("- Recent notes:")
        for note in recent_notes:
            lines.append(f"  - [[{note.stem}]]")

    lines.append(f"- Last structural refresh: {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return "\n".join(lines)


def _build_new_index_file(vault_root: Path, folder: Path) -> str:
    folder_name = folder.name
    folder_tag = _slugify(folder_name) or "area"
    managed_summary = _build_index_managed_summary(vault_root, folder)
    return "\n".join(
        [
            "---",
            "type: area-index",
            f"title: {folder_name}",
            f"updated: {datetime.utcnow().strftime('%Y-%m-%d')}",
            f"tags: [index, {folder_tag}]",
            "---",
            "",
            f"# {folder_name}",
            "",
            "## Purpose",
            "Describe what this folder is for.",
            "",
            "## Working Rules",
            "Add operating instructions for humans and AI agents in this folder.",
            "",
            INDEX_MANAGED_BEGIN,
            managed_summary,
            INDEX_MANAGED_END,
            "",
        ]
    )


def _refresh_folder_index(vault_root: Path, folder: Path) -> tuple[list[str], bool]:
    updates: list[str] = []
    folder = folder.resolve(strict=False)
    vault_real = vault_root.resolve(strict=False)
    if folder == vault_real or vault_real not in folder.parents:
        return updates, False
    if not folder.exists() or not folder.is_dir() or _is_hidden(folder):
        return updates, False

    index_path = folder / "_index.md"
    note_count = _count_markdown_notes(folder, recursive=True)

    created = False
    if not index_path.exists() and note_count >= INDEX_AUTO_CREATE_MIN_NOTES:
        index_path.write_text(_build_new_index_file(vault_root, folder), encoding="utf-8")
        updates.append(f"created {_relative(vault_root, index_path)}")
        created = True

    if not index_path.exists():
        return updates, created

    existing = _safe_read_text(index_path)
    managed_summary = _build_index_managed_summary(vault_root, folder)
    updated, changed = _replace_managed_block(existing, INDEX_MANAGED_BEGIN, INDEX_MANAGED_END, managed_summary)
    if changed:
        index_path.write_text(updated, encoding="utf-8")
        if created:
            updates.append(f"updated managed summary in {_relative(vault_root, index_path)}")
        else:
            updates.append(f"updated {_relative(vault_root, index_path)}")

    return updates, created


def _build_vault_structure_managed_summary(vault_root: Path) -> str:
    top_dirs = []
    extras = []
    for item in sorted(vault_root.iterdir(), key=lambda entry: entry.name.lower()):
        if item.is_dir() and not _is_hidden(item):
            top_dirs.append(item.name)
            if item.name not in STANDARD_DIRS:
                extras.append(item.name)

    lines = [
        "## Managed Structure Snapshot",
        f"- Last refresh: {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "### Root Directories",
    ]
    for name in STANDARD_DIRS:
        marker = "present" if name in top_dirs else "missing"
        lines.append(f"- {name}: {marker}")

    if extras:
        lines.extend(["", "### Non-Standard Root Directories"])
        for name in extras:
            lines.append(f"- {name}")

    areas_root = vault_root / "02-Areas"
    if areas_root.exists() and areas_root.is_dir():
        lines.extend(["", "### Area Folders"])
        for area in sorted((p for p in areas_root.iterdir() if p.is_dir() and not _is_hidden(p)), key=lambda p: p.name.lower()):
            area_notes = _count_markdown_notes(area, recursive=True)
            has_index = (area / "_index.md").exists()
            lines.append(f"- {area.name}: notes={area_notes}, _index.md={'yes' if has_index else 'no'}")

    return "\n".join(lines)


def _refresh_vault_structure_file(vault_root: Path) -> list[str]:
    updates: list[str] = []
    meta_dir = vault_root / "Meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    structure_path = meta_dir / "vault-structure.md"

    if structure_path.exists():
        existing = _safe_read_text(structure_path)
    else:
        existing = "# Vault Structure\n\nThis file tracks the current vault organization.\n"
        updates.append(f"created {_relative(vault_root, structure_path)}")

    managed_summary = _build_vault_structure_managed_summary(vault_root)
    updated, changed = _replace_managed_block(
        existing,
        STRUCTURE_MANAGED_BEGIN,
        STRUCTURE_MANAGED_END,
        managed_summary,
    )
    if changed:
        structure_path.write_text(updated, encoding="utf-8")
        if f"created {_relative(vault_root, structure_path)}" not in updates:
            updates.append(f"updated {_relative(vault_root, structure_path)}")
    return updates


def _refresh_structure_context(vault_root: Path, touched_dirs: list[Path]) -> list[str]:
    updates: list[str] = []
    seen: set[str] = set()

    for directory in touched_dirs:
        for message in _refresh_folder_index(vault_root, directory)[0]:
            if message not in seen:
                seen.add(message)
                updates.append(message)

    for message in _refresh_vault_structure_file(vault_root):
        if message not in seen:
            seen.add(message)
            updates.append(message)

    return updates


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


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "sim", "s", "on"}
    return False


def _parse_inline_list(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not (text.startswith("[") and text.endswith("]")):
        return []

    inner = text[1:-1].strip()
    if not inner:
        return []

    items = [item.strip() for item in inner.split(",")]
    normalized = []
    for item in items:
        if not item:
            continue
        if (item.startswith("\"") and item.endswith("\"")) or (
            item.startswith("'") and item.endswith("'")
        ):
            item = item[1:-1]
        normalized.append(item)
    return normalized


def _parse_scalar_token(raw: str) -> object:
    value = (raw or "").strip()
    if not value:
        return ""

    if value.lower() in {"true", "false"}:
        return value.lower() == "true"

    inline_list = _parse_inline_list(value)
    if inline_list:
        return inline_list

    if (value.startswith("\"") and value.endswith("\"")) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]

    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            return value
    if re.fullmatch(r"-?\d+\.\d+", value):
        try:
            return float(value)
        except ValueError:
            return value

    return value


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_yaml_block(lines: list[str]) -> object:
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        return None

    first = meaningful[0]
    first_indent = _leading_spaces(first)
    first_stripped = first.strip()

    if first_stripped.startswith("- "):
        items: list[object] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue

            indent = _leading_spaces(line)
            stripped = line.strip()
            if indent != first_indent or not stripped.startswith("- "):
                index += 1
                continue

            head = stripped[2:].strip()
            if ":" in head:
                key, value = head.split(":", 1)
                item: dict[str, object] = {key.strip(): _parse_scalar_token(value)}
                index += 1

                while index < len(lines):
                    continuation = lines[index]
                    if not continuation.strip():
                        index += 1
                        continue

                    continuation_indent = _leading_spaces(continuation)
                    continuation_stripped = continuation.strip()
                    if continuation_indent <= first_indent:
                        break
                    if continuation_stripped.startswith("- ") and continuation_indent == first_indent:
                        break

                    if ":" in continuation_stripped:
                        ckey, cvalue = continuation_stripped.split(":", 1)
                        item[ckey.strip()] = _parse_scalar_token(cvalue)
                    index += 1

                items.append(item)
                continue

            items.append(_parse_scalar_token(head))
            index += 1

        return items

    mapping: dict[str, object] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        mapping[key.strip()] = _parse_scalar_token(value)
    return mapping


def _parse_frontmatter_fallback(frontmatter_text: str) -> dict:
    data: dict[str, object] = {}
    lines = frontmatter_text.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if line.startswith(" "):
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue

        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if not key:
            index += 1
            continue

        if value:
            data[key] = _parse_scalar_token(value)
            index += 1
            continue

        block_lines: list[str] = []
        next_index = index + 1
        while next_index < len(lines):
            candidate = lines[next_index]
            if candidate.strip() and not candidate.startswith(" "):
                break
            block_lines.append(candidate)
            next_index += 1

        data[key] = _parse_yaml_block(block_lines)
        index = next_index

    return data


def _extract_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break

    if end_index is None:
        return {}, text

    frontmatter_text = "\n".join(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :])

    if yaml is not None:
        try:
            parsed = yaml.safe_load(frontmatter_text)
            if isinstance(parsed, dict):
                return parsed, body
        except Exception:
            pass

    return _parse_frontmatter_fallback(frontmatter_text), body


def _extract_placeholders(template_text: str) -> list[str]:
    seen = set()
    ordered: list[str] = []
    for match in PLACEHOLDER_PATTERN.finditer(template_text or ""):
        name = (match.group(1) or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _normalize_template_fields(raw_fields: object) -> list[dict]:
    if not isinstance(raw_fields, list):
        return []

    normalized: list[dict] = []
    for item in raw_fields:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or "").strip()
        if not name:
            continue

        field_type = str(item.get("type") or "string").strip().lower() or "string"
        field: dict[str, object] = {
            "name": name,
            "type": field_type,
            "required": _as_bool(item.get("required", False)),
        }

        if "default" in item:
            field["default"] = item.get("default")

        prompt = item.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            field["prompt"] = prompt.strip()

        enum = item.get("enum")
        if isinstance(enum, list):
            enum_values = [str(entry).strip() for entry in enum if str(entry).strip()]
            if enum_values:
                field["enum"] = enum_values

        normalized.append(field)

    return normalized


def _template_record(
    vault_root: Path,
    template_path: Path,
    include_fields: bool = False,
    include_placeholders: bool = False,
    include_body_preview: bool = False,
) -> dict:
    text = _safe_read_text(template_path)
    frontmatter, body = _extract_frontmatter(text)

    aliases = _normalize_tags(frontmatter.get("vg_aliases"))
    fields = _normalize_template_fields(frontmatter.get("vg_fields"))
    template_id = str(frontmatter.get("vg_template_id") or "").strip() or None
    description = str(frontmatter.get("vg_description") or "").strip() or None
    default_target_folder = str(frontmatter.get("vg_default_target_folder") or "").strip() or None

    title = str(frontmatter.get("vg_title") or "").strip() or template_path.stem
    legacy = not bool(_as_bool(frontmatter.get("vg_template")) or template_id or fields)

    record: dict[str, object] = {
        "template_id": template_id,
        "path": _relative(vault_root, template_path),
        "title": title,
        "description": description,
        "legacy": legacy,
        "field_count": len(fields),
        "required_field_count": len([field for field in fields if field.get("required")]),
        "default_target_folder": default_target_folder,
        "aliases": aliases,
    }

    if include_fields:
        record["fields"] = fields
    if include_placeholders:
        record["placeholders"] = _extract_placeholders(text)
    if include_body_preview:
        record["body_preview"] = body.strip()[:2000]

    return record


def _iter_template_paths(vault_root: Path, path_prefix: str | None = None) -> list[Path]:
    base_dir = vault_root / "Templates"
    if path_prefix:
        base_dir = _resolve_relative_path(vault_root, path_prefix)

    if not base_dir.exists() or not base_dir.is_dir():
        return []

    templates = []
    for path in sorted(base_dir.rglob("*.md"), key=lambda item: str(item).lower()):
        if path.is_file():
            templates.append(path)
    return templates


def _query_matches_template(record: dict, query: str) -> bool:
    needle = (query or "").strip().lower()
    if not needle:
        return True

    haystack = [
        str(record.get("template_id") or "").lower(),
        str(record.get("title") or "").lower(),
        str(record.get("description") or "").lower(),
        str(record.get("path") or "").lower(),
    ]
    aliases = record.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            haystack.append(str(alias).lower())

    return any(needle in item for item in haystack)


def list_templates(
    vault_root: Path,
    query: str = "",
    path_prefix: str | None = None,
    include_legacy: bool = True,
    limit: int = 50,
    mode: str = "capture",
) -> dict:
    safe_limit = max(1, min(int(limit or 50), 200))
    normalized_mode = (mode or "capture").strip().lower()

    templates = []
    for template_path in _iter_template_paths(vault_root, path_prefix=path_prefix):
        record = _template_record(vault_root, template_path)
        if not include_legacy and record.get("legacy"):
            continue
        if normalized_mode == "capture" and _is_hidden(template_path):
            continue
        if not _query_matches_template(record, query):
            continue
        templates.append(record)

    templates.sort(key=lambda item: (bool(item.get("legacy")), str(item.get("title") or "").lower()))

    return {
        "query": query,
        "path_prefix": path_prefix or "Templates",
        "mode": normalized_mode,
        "templates": templates[:safe_limit],
        "total": len(templates),
    }


def inspect_template(
    vault_root: Path,
    template_path: str,
    include_body_preview: bool = False,
    include_placeholders: bool = True,
) -> dict:
    path = _resolve_relative_path(vault_root, template_path)
    if path.suffix.lower() != ".md":
        raise ValueError("Template must be a markdown file (.md)")
    if not path.exists() or not path.is_file():
        raise ValueError(f"Template not found at path: {template_path}")

    return _template_record(
        vault_root,
        path,
        include_fields=True,
        include_placeholders=include_placeholders,
        include_body_preview=include_body_preview,
    )


def _find_template_by_id(vault_root: Path, template_id: str) -> tuple[Path | None, dict | None]:
    normalized = (template_id or "").strip().lower()
    if not normalized:
        return None, None

    matches: list[tuple[Path, dict]] = []
    for template_path in _iter_template_paths(vault_root, path_prefix="Templates"):
        record = _template_record(vault_root, template_path, include_fields=True)
        current_id = str(record.get("template_id") or "").strip().lower()
        if current_id == normalized:
            matches.append((template_path, record))

    if not matches:
        return None, None
    if len(matches) > 1:
        raise ValueError(
            "template_id is ambiguous; multiple templates share the same vg_template_id"
        )
    return matches[0]


def _resolve_dynamic_default(value: object) -> object:
    if not isinstance(value, str):
        return value

    token = value.strip().lower()
    now = datetime.utcnow()
    if token == "@today":
        return now.strftime("%Y-%m-%d")
    if token == "@now_iso":
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if token == "@year":
        return now.strftime("%Y")
    if token == "@month":
        return now.strftime("%m")
    return value


def _coerce_field_value(value: object, field_type: str, enum: list[str] | None = None) -> object:
    normalized_type = (field_type or "string").strip().lower()

    if normalized_type == "string":
        output = str(value)
    elif normalized_type == "number":
        if isinstance(value, bool):
            raise ValueError("expected number")
        if isinstance(value, (int, float)):
            output = value
        elif isinstance(value, str) and value.strip():
            try:
                output = float(value.strip())
            except ValueError as exc:
                raise ValueError("expected number") from exc
        else:
            raise ValueError("expected number")
    elif normalized_type == "boolean":
        if isinstance(value, bool):
            output = value
        elif isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "y", "sim", "s", "on"}:
                output = True
            elif lowered in {"false", "0", "no", "n", "nao", "off"}:
                output = False
            else:
                raise ValueError("expected boolean")
        else:
            raise ValueError("expected boolean")
    elif normalized_type == "date":
        if isinstance(value, date):
            output = value.strftime("%Y-%m-%d")
        elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            output = value.strip()
        else:
            raise ValueError("expected date format YYYY-MM-DD")
    elif normalized_type == "list[string]":
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
            output = items
        elif isinstance(value, list):
            output = [str(item).strip() for item in value if str(item).strip()]
        else:
            raise ValueError("expected list[string]")
    elif normalized_type == "list[number]":
        if isinstance(value, str):
            raw_items = [item.strip() for item in value.split(",") if item.strip()]
            parsed = []
            for item in raw_items:
                try:
                    parsed.append(float(item))
                except ValueError as exc:
                    raise ValueError("expected list[number]") from exc
            output = parsed
        elif isinstance(value, list):
            parsed = []
            for item in value:
                if isinstance(item, bool):
                    raise ValueError("expected list[number]")
                if isinstance(item, (int, float)):
                    parsed.append(item)
                elif isinstance(item, str) and item.strip():
                    try:
                        parsed.append(float(item.strip()))
                    except ValueError as exc:
                        raise ValueError("expected list[number]") from exc
                else:
                    raise ValueError("expected list[number]")
            output = parsed
        else:
            raise ValueError("expected list[number]")
    elif normalized_type == "list[boolean]":
        if isinstance(value, str):
            raw_items = [item.strip() for item in value.split(",") if item.strip()]
            parsed = []
            for item in raw_items:
                lowered = item.lower()
                if lowered in {"true", "1", "yes", "y", "sim", "s", "on"}:
                    parsed.append(True)
                elif lowered in {"false", "0", "no", "n", "nao", "off"}:
                    parsed.append(False)
                else:
                    raise ValueError("expected list[boolean]")
            output = parsed
        elif isinstance(value, list):
            parsed = []
            for item in value:
                if isinstance(item, bool):
                    parsed.append(item)
                else:
                    raise ValueError("expected list[boolean]")
            output = parsed
        else:
            raise ValueError("expected list[boolean]")
    else:
        output = value

    if enum and isinstance(output, str):
        normalized_enum = [str(item).strip().lower() for item in enum if str(item).strip()]
        if output.strip().lower() not in normalized_enum:
            raise ValueError(f"expected one of: {', '.join(normalized_enum)}")

    return output


def _stringify_render_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(_stringify_render_value(item) for item in value)
    return str(value)


def _render_template(template_text: str, render_values: dict[str, object]) -> tuple[str, list[str]]:
    unresolved: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = (match.group(1) or "").strip()
        if key in render_values:
            return _stringify_render_value(render_values.get(key))
        unresolved.append(key)
        return match.group(0)

    rendered = PLACEHOLDER_PATTERN.sub(_replace, template_text)
    unresolved_unique = sorted({key for key in unresolved if key})
    return rendered, unresolved_unique


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


def _resolve_capture_template(
    vault_root: Path,
    template_hint: str | None = None,
    template_path: str | None = None,
    template_id: str | None = None,
) -> tuple[Path | None, dict | None]:
    if template_path:
        resolved = _resolve_relative_path(vault_root, template_path)
        if resolved.suffix.lower() != ".md":
            raise ValueError("template_path must point to a markdown file")
        if not resolved.exists() or not resolved.is_file():
            raise ValueError(f"Template not found at path: {template_path}")
        return resolved, _template_record(vault_root, resolved, include_fields=True, include_placeholders=True)

    if template_id:
        by_id = _find_template_by_id(vault_root, template_id)
        if by_id[0] is None:
            raise ValueError(f"Template id not found: {template_id}")
        return by_id

    by_hint = _find_template(vault_root, template_hint or "")
    if by_hint is None:
        return None, None
    return by_hint, _template_record(vault_root, by_hint, include_fields=True, include_placeholders=True)


def _normalize_field_values_map(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, object] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        normalized[name] = value
    return normalized


def _prepare_template_field_values(
    field_defs: list[dict],
    provided_values: dict[str, object],
    missing_fields_policy: str,
) -> tuple[dict[str, object], list[dict], list[str]]:
    field_map = {str(field.get("name")): field for field in field_defs if field.get("name")}
    warnings: list[str] = []

    unknown_fields = sorted([key for key in provided_values.keys() if key not in field_map])
    if unknown_fields:
        warnings.append(f"Unknown template fields ignored: {', '.join(unknown_fields)}")

    resolved_values: dict[str, object] = {}
    missing_fields: list[dict] = []

    for name, field in field_map.items():
        has_value = name in provided_values
        raw_value = provided_values.get(name)

        if not has_value and "default" in field:
            raw_value = _resolve_dynamic_default(field.get("default"))
            has_value = raw_value is not None

        if not has_value or raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            if field.get("required"):
                missing_fields.append(
                    {
                        "name": name,
                        "type": str(field.get("type") or "string"),
                        "prompt": str(field.get("prompt") or ""),
                    }
                )
            continue

        try:
            resolved_values[name] = _coerce_field_value(
                raw_value,
                str(field.get("type") or "string"),
                field.get("enum") if isinstance(field.get("enum"), list) else None,
            )
        except ValueError as exc:
            raise ValueError(f"Invalid value for template field '{name}': {exc}") from exc

    policy = (missing_fields_policy or "ask").strip().lower()
    if missing_fields and policy in {"fail", "defaults"}:
        names = ", ".join(field["name"] for field in missing_fields)
        raise ValueError(f"Missing required template fields: {names}")

    return resolved_values, missing_fields, warnings


def _resolve_capture_target_dir(
    vault_root: Path,
    target_folder: str | None,
    template_record: dict | None,
) -> Path:
    requested = (target_folder or "").strip()
    if requested:
        resolved_folder = requested
    else:
        resolved_folder = str((template_record or {}).get("default_target_folder") or "00-Inbox")

    target_dir = _resolve_relative_path(vault_root, resolved_folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def _pick_capture_title(
    body: str,
    title: str | None,
    template_record: dict | None,
    resolved_fields: dict[str, object],
    template_path: Path | None,
) -> str:
    explicit = (title or "").strip()
    if explicit:
        return explicit

    for key in ("title", "name", "client_name", "meeting_title"):
        value = resolved_fields.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    inferred = str((template_record or {}).get("title") or "").strip()
    if inferred:
        return inferred

    if body.strip():
        return _default_capture_title(body)

    if template_path is not None:
        return template_path.stem

    return datetime.utcnow().strftime("capture-%Y%m%d-%H%M%S")


def _builtin_render_values() -> dict[str, object]:
    now = datetime.utcnow()
    return {
        "today": now.strftime("%Y-%m-%d"),
        "now_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "year": now.strftime("%Y"),
        "month": now.strftime("%m"),
    }


def read_note(
    vault_root: Path,
    path: str | None = None,
    include_frontmatter: bool = True,
    include_body: bool = True,
) -> dict:
    note_path = _resolve_note_path(vault_root, path=path)
    text = _safe_read_text(note_path)
    frontmatter, body = _extract_frontmatter(text)

    result: dict[str, object] = {
        "path": _relative(vault_root, note_path),
        "title": note_path.stem,
    }

    if include_frontmatter:
        result["frontmatter"] = frontmatter if frontmatter else {}
        result["has_frontmatter"] = bool(frontmatter)
    if include_body:
        result["body"] = body.strip()
        result["size_bytes"] = note_path.stat().st_size if note_path.exists() else 0

    result["context"] = _build_operation_context(vault_root, note_path.parent)

    return result


def capture_note(
    vault_root: Path,
    text: str,
    title: str | None = None,
    target_folder: str | None = None,
    template_hint: str | None = None,
    tags: object = None,
    template_path: str | None = None,
    template_id: str | None = None,
    field_values: object = None,
    template_mode: str = "legacy",
    missing_fields_policy: str = "ask",
    append_captured_context: bool = True,
) -> dict:
    body = (text or "").strip()
    normalized_tags = _normalize_tags(tags)
    provided_fields = _normalize_field_values_map(field_values)
    mode = (template_mode or "legacy").strip().lower()
    missing_policy = (missing_fields_policy or "ask").strip().lower()

    if mode not in {"legacy", "auto", "strict", "off"}:
        raise ValueError("Invalid template_mode")
    if missing_policy not in {"ask", "fail", "defaults"}:
        raise ValueError("Invalid missing_fields_policy")

    if (template_id or template_path) and mode == "off":
        raise ValueError("template_mode=off cannot be used with template_id or template_path")

    selected_template_path = None
    selected_template_record = None
    if mode != "off":
        selected_template_path, selected_template_record = _resolve_capture_template(
            vault_root,
            template_hint=template_hint,
            template_path=template_path,
            template_id=template_id,
        )

    if selected_template_path is None and not body:
        raise ValueError("Field 'text' is required when no template is selected")

    if selected_template_path is None and provided_fields:
        raise ValueError("field_values requires a selected template")

    vault_root.mkdir(parents=True, exist_ok=True)
    target_dir = _resolve_capture_target_dir(vault_root, target_folder, selected_template_record)

    field_defs = []
    if isinstance(selected_template_record, dict):
        raw_defs = selected_template_record.get("fields")
        if isinstance(raw_defs, list):
            field_defs = raw_defs

    structured_mode = False
    if selected_template_path is not None and field_defs:
        if mode in {"auto", "strict"}:
            structured_mode = True
        elif provided_fields:
            structured_mode = True

    if mode == "strict" and selected_template_path is not None and not field_defs:
        raise ValueError("Strict template mode requires vg_fields metadata in template frontmatter")

    resolved_fields: dict[str, object] = {}
    missing_fields: list[dict] = []
    warnings: list[str] = []
    unresolved_placeholders: list[str] = []

    if structured_mode:
        resolved_fields, missing_fields, warnings = _prepare_template_field_values(
            field_defs,
            provided_fields,
            missing_policy,
        )

        if missing_fields and missing_policy == "ask":
            pending_result = {
                "pending": True,
                "phase": "awaiting_template_fields",
                "template_used": _relative(vault_root, selected_template_path),
                "missing_fields": missing_fields,
                "provided_fields": sorted([key for key in provided_fields.keys() if key]),
                "warnings": warnings,
                "will_write_note": False,
                "target_folder": _relative(vault_root, target_dir),
            }
            _append_log(vault_root, "note.capture.pending", pending_result)
            return pending_result

    selected_title = _pick_capture_title(
        body,
        title,
        selected_template_record,
        resolved_fields,
        selected_template_path,
    )
    slug = _slugify(selected_title) or datetime.utcnow().strftime("capture-%Y%m%d-%H%M%S")
    note_path = _unique_path(target_dir / f"{slug}.md")

    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if selected_template_path:
        template_text = _safe_read_text(selected_template_path).rstrip()
        if structured_mode:
            render_values = _builtin_render_values()
            render_values.update(resolved_fields)
            if body:
                render_values.setdefault("captured_context", body)

            rendered, unresolved_placeholders = _render_template(template_text, render_values)
            content = rendered.rstrip() + "\n"

            if body and append_captured_context and "captured_context" not in _extract_placeholders(template_text):
                content = f"{content.rstrip()}\n\n## Captured Context\n\n{body}\n"
            if unresolved_placeholders:
                warnings.append(
                    "Unresolved placeholders kept as-is: " + ", ".join(unresolved_placeholders)
                )
        else:
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
    maintenance_updates = _refresh_structure_context(vault_root, [target_dir])
    operation_context = _build_operation_context(vault_root, target_dir)

    result = {
        "path": _relative(vault_root, note_path),
        "title": selected_title,
        "template_used": _relative(vault_root, selected_template_path) if selected_template_path else None,
        "target_folder": _relative(vault_root, target_dir),
        "template_mode_used": "structured" if structured_mode else ("legacy" if selected_template_path else "none"),
        "warnings": warnings,
        "unresolved_placeholders": unresolved_placeholders,
        "maintenance_updates": maintenance_updates,
        "context": operation_context,
    }
    _append_log(vault_root, "note.capture", result)
    return result


def _serialize_frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            items = ", ".join(str(item) for item in value)
            lines.append(f"{key}: [{items}]")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def update_note(
    vault_root: Path,
    text: str | None = None,
    path: str | None = None,
    mode: str = "append",
    frontmatter_fields: dict | None = None,
    section_heading: str | None = None,
) -> dict:
    normalized_mode = (mode or "append").strip().lower()
    valid_modes = {"append", "prepend", "replace", "frontmatter", "section_append", "section_prepend"}
    if normalized_mode not in valid_modes:
        raise ValueError(f"Invalid mode. Expected one of: {', '.join(sorted(valid_modes))}")

    if normalized_mode == "frontmatter":
        if not frontmatter_fields or not isinstance(frontmatter_fields, dict):
            raise ValueError("frontmatter_fields is required when mode is frontmatter")
        if not text:
            text = ""

    payload_text = (text or "").strip()
    if normalized_mode != "frontmatter" and not payload_text:
        raise ValueError("Field 'text' is required")

    if normalized_mode in {"section_append", "section_prepend"} and not str(section_heading or "").strip():
        raise ValueError("section_heading is required when mode is section_append or section_prepend")

    note_path = _resolve_note_path(vault_root, path=path)
    existing = _safe_read_text(note_path)
    existing_frontmatter, existing_body = _extract_frontmatter(existing)

    warnings: list[str] = []

    if normalized_mode == "replace":
        if payload_text.startswith("---"):
            updated = payload_text + "\n"
        elif existing_frontmatter:
            updated = _serialize_frontmatter(existing_frontmatter) + "\n\n" + payload_text + "\n"
            warnings.append("Existing frontmatter auto-preserved (replace text had no YAML block)")
        else:
            updated = payload_text + "\n"

    elif normalized_mode == "frontmatter":
        merged = dict(existing_frontmatter) if existing_frontmatter else {}
        merged.update(frontmatter_fields)
        fm_block = _serialize_frontmatter(merged)
        body_text = existing_body.strip()
        if body_text:
            updated = fm_block + "\n\n" + body_text + "\n"
        else:
            updated = fm_block + "\n"

    elif normalized_mode == "prepend":
        if payload_text.startswith("---"):
            updated = payload_text + "\n\n" + existing
        else:
            updated = payload_text + "\n\n" + existing

    elif normalized_mode in {"section_append", "section_prepend"}:
        updated_body = _update_markdown_section(
            existing_body,
            heading=str(section_heading or ""),
            payload_text=payload_text,
            prepend=normalized_mode == "section_prepend",
        )
        body_text = updated_body.strip("\n")
        if existing_frontmatter:
            updated = _serialize_frontmatter(existing_frontmatter) + "\n\n" + body_text + "\n"
        else:
            updated = body_text + "\n"

    else:
        separator = "\n\n" if existing.strip() else ""
        updated = existing.rstrip() + separator + payload_text + "\n"

    note_path.write_text(updated, encoding="utf-8")
    maintenance_updates = _refresh_structure_context(vault_root, [note_path.parent])
    operation_context = _build_operation_context(vault_root, note_path.parent)

    result: dict[str, object] = {
        "path": _relative(vault_root, note_path),
        "mode": normalized_mode,
        "maintenance_updates": maintenance_updates,
        "context": operation_context,
    }
    if normalized_mode in {"section_append", "section_prepend"}:
        result["section_heading"] = str(section_heading or "").strip()
    if warnings:
        result["warnings"] = warnings
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
    operation_context = _build_operation_context(vault_root, base_dir)
    return {
        "query": query,
        "results": matches[:safe_limit],
        "total_matches": len(matches),
        "context": operation_context,
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
    source_parent = source.parent
    shutil.move(str(source), str(destination))
    maintenance_updates = _refresh_structure_context(vault_root, [source_parent, target_dir])
    operation_context = _build_operation_context(vault_root, target_dir, source_parent)

    result = {
        "from": _relative(vault_root, source),
        "to": _relative(vault_root, destination),
        "maintenance_updates": maintenance_updates,
        "context": operation_context,
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
