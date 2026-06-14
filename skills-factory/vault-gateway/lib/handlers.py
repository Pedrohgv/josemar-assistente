from __future__ import annotations

from datetime import datetime

from lib.common import contains_any, normalize_text
from lib.paths import ConfigurationError, assert_safe_vault_root, get_vault_root
from lib.state import clear_state, load_state, save_state
from lib.vault_ops import (
    apply_destructive_port,
    apply_non_destructive_port,
    build_port_plan,
    capture_note,
    file_note,
    inspect_template,
    list_templates,
    link_notes,
    read_note,
    rename_note,
    scan_vault,
    search_notes,
    summarize_audit,
    summarize_deep_clean,
    summarize_defrag,
    summarize_inbox,
    summarize_tag_garden,
    update_note,
)


BACKUP_CONFIRMATION = "i have a backup and want to continue"
NON_DESTRUCTIVE_CONFIRMATION = "approve non-destructive port"
DESTRUCTIVE_EXECUTION_CONFIRMATION = "execute destructive port"
LEGACY_BACKUP_CONFIRMATION = "eu tenho backup e quero continuar"
LEGACY_NON_DESTRUCTIVE_CONFIRMATION = "aprovar port nao destrutivo"
LEGACY_DESTRUCTIVE_EXECUTION_CONFIRMATION = "executar port destrutivo"


def _payload_text(payload: dict) -> str:
    candidates = [
        payload.get("input"),
        payload.get("message"),
        payload.get("text"),
        payload.get("prompt"),
        payload.get("answer"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "sim", "y", "s"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _maintenance_suffix(result: dict) -> str:
    updates = result.get("maintenance_updates")
    if not isinstance(updates, list):
        return ""
    cleaned = [str(item).strip() for item in updates if str(item).strip()]
    if not cleaned:
        return ""
    first = cleaned[0]
    if len(cleaned) == 1:
        return f" I also updated context files ({first})."
    return f" I also updated context files ({first} plus {len(cleaned) - 1} more)."


def _is_strict_yes(text: str) -> bool:
    return normalize_text(text) in {"sim", "s", "yes", "y", "ok"}


def _is_strict_no(text: str) -> bool:
    return normalize_text(text) in {"nao", "n", "no"}


def _matches_exact_confirmation(text: str, confirmation: str, legacy_confirmation: str) -> bool:
    return text in {confirmation, legacy_confirmation}


def _state_key(payload: dict) -> str:
    for key in ("state_key", "session_id", "conversation_id", "sender_id"):
        value = payload.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return "default"


def _load_onboarding_state(state_key: str) -> dict:
    state = load_state()
    onboarding = state.get("onboarding", {})
    if not isinstance(onboarding, dict):
        return {}

    if "phase" in onboarding:
        if state_key == "default":
            return onboarding
        return {}

    scoped = onboarding.get(state_key, {})
    if not isinstance(scoped, dict):
        return {}
    return scoped


def _save_onboarding_state(state_key: str, onboarding: dict) -> None:
    state = load_state()
    existing = state.get("onboarding", {})
    if not isinstance(existing, dict) or "phase" in existing:
        existing = {}
    existing[state_key] = onboarding
    state["onboarding"] = existing
    save_state(state)


def _clear_onboarding_state(state_key: str) -> None:
    state = load_state()

    onboarding = state.get("onboarding", {})
    if isinstance(onboarding, dict):
        if "phase" in onboarding and state_key == "default":
            del state["onboarding"]
        elif state_key in onboarding:
            del onboarding[state_key]
            if onboarding:
                state["onboarding"] = onboarding
            else:
                del state["onboarding"]

    if state:
        save_state(state)
        return
    clear_state()


def _render_plan(plan: dict) -> str:
    lines = [
        "Detected port plan:",
        f"- Vault exists: {plan.get('vault_exists')}",
        f"- Mode: {'destructive' if plan.get('destructive') else 'non-destructive'}",
        f"- Missing standard folders: {', '.join(plan.get('missing_standard_dirs', [])) or '(none)'}",
        f"- Non-standard root items: {', '.join(plan.get('non_standard_root_entries', [])) or '(none)'}",
        "- Actions:",
    ]
    for action in plan.get("actions", []):
        lines.append(f"  - {action}")
    return "\n".join(lines)


def handle_onboarding(payload: dict) -> dict:
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    input_text = _payload_text(payload)
    normalized = normalize_text(input_text)
    vault_root = get_vault_root()
    state_key = _state_key(payload)

    if contains_any(normalized, ["cancelar onboarding", "cancel onboarding", "cancelar", "cancel"]):
        _clear_onboarding_state(state_key)
        return {
            "message": "Onboarding cancelled. When you want to resume, say 'onboarding' or 'initialize my vault'.",
            "needs_user_input": False,
            "phase": "cancelled",
            "timestamp": now,
        }

    onboarding = _load_onboarding_state(state_key)
    phase = onboarding.get("phase", "choose_path")

    if phase == "choose_path":
        requested_mode = str(payload.get("mode") or "").strip().lower()
        if requested_mode == "port":
            normalized = "port existing vault"
        elif requested_mode == "new":
            normalized = "new vault"

        if contains_any(normalized, ["port", "migrat", "vault existente", "existing vault"]):
            onboarding = {
                "phase": "ask_destructive",
                "mode": "port",
                "started_at": onboarding.get("started_at", now),
            }
            _save_onboarding_state(state_key, onboarding)
            return {
                "message": (
                    "Port mode selected. Do you want to run destructive mode? "
                    "(yes/no).\n"
                    "- yes: can move non-standard root items to the archive\n"
                    "- no: only creates missing structure"
                ),
                "needs_user_input": True,
                "phase": "ask_destructive",
            }

        if contains_any(normalized, ["novo vault", "new vault", "do zero", "from scratch", "iniciar"]):
            onboarding = {
                "phase": "confirm_new",
                "mode": "new",
                "started_at": onboarding.get("started_at", now),
            }
            _save_onboarding_state(state_key, onboarding)
            return {
                "message": (
                    "New vault mode selected. I will create the standard structure without overwriting existing files. "
                    "Can I execute now? (yes/no)"
                ),
                "needs_user_input": True,
                "phase": "confirm_new",
            }

        onboarding = {
            "phase": "choose_path",
            "mode": None,
            "started_at": onboarding.get("started_at", now),
        }
        _save_onboarding_state(state_key, onboarding)
        return {
            "message": (
                "Let's start vault onboarding. Choose an option:\n"
                "1) new vault\n"
                "2) port existing vault"
            ),
            "needs_user_input": True,
            "phase": "choose_path",
        }

    if phase == "confirm_new":
        if _is_strict_yes(normalized):
            result = apply_non_destructive_port(vault_root, "new-vault-onboarding")
            _clear_onboarding_state(state_key)
            return {
                "message": "Base structure created for the new vault in safe mode (non-destructive).",
                "needs_user_input": False,
                "phase": "completed",
                "result": result,
            }
        if _is_strict_no(normalized):
            _clear_onboarding_state(state_key)
            return {
                "message": "Onboarding cancelled without changes.",
                "needs_user_input": False,
                "phase": "cancelled",
            }
        return {
            "message": "Reply with 'yes' to create the base structure or 'no' to cancel.",
            "needs_user_input": True,
            "phase": "confirm_new",
        }

    if phase == "ask_destructive":
        if _is_strict_yes(normalized):
            onboarding["phase"] = "warn_backup"
            onboarding["destructive"] = True
            _save_onboarding_state(state_key, onboarding)
            return {
                "message": (
                    "WARNING: destructive mode can move content to 04-Archive and change root organization.\n"
                    "STRONG RECOMMENDATION: make a complete vault backup before continuing.\n"
                    f"To continue, type exactly: {BACKUP_CONFIRMATION}"
                ),
                "needs_user_input": True,
                "phase": "warn_backup",
            }
        if _is_strict_no(normalized):
            plan = build_port_plan(scan_vault(vault_root), destructive=False)
            onboarding["phase"] = "confirm_non_destructive"
            onboarding["destructive"] = False
            onboarding["plan"] = plan
            _save_onboarding_state(state_key, onboarding)
            return {
                "message": (
                    f"{_render_plan(plan)}\n\n"
                    f"If you agree, type exactly: {NON_DESTRUCTIVE_CONFIRMATION}"
                ),
                "needs_user_input": True,
                "phase": "confirm_non_destructive",
                "plan": plan,
            }
        return {
            "message": "Reply with 'yes' or 'no' for destructive mode.",
            "needs_user_input": True,
            "phase": "ask_destructive",
        }

    if phase == "warn_backup":
        if _matches_exact_confirmation(normalized, BACKUP_CONFIRMATION, LEGACY_BACKUP_CONFIRMATION):
            plan = build_port_plan(scan_vault(vault_root), destructive=True)
            onboarding["phase"] = "confirm_destructive"
            onboarding["plan"] = plan
            _save_onboarding_state(state_key, onboarding)
            return {
                "message": (
                    f"{_render_plan(plan)}\n\n"
                    f"To execute the destructive port, type exactly: {DESTRUCTIVE_EXECUTION_CONFIRMATION}"
                ),
                "needs_user_input": True,
                "phase": "confirm_destructive",
                "plan": plan,
            }
        return {
            "message": (
                "Still waiting for backup confirmation.\n"
                f"Type exactly: {BACKUP_CONFIRMATION}"
            ),
            "needs_user_input": True,
            "phase": "warn_backup",
        }

    if phase == "confirm_non_destructive":
        if _matches_exact_confirmation(
            normalized,
            NON_DESTRUCTIVE_CONFIRMATION,
            LEGACY_NON_DESTRUCTIVE_CONFIRMATION,
        ):
            result = apply_non_destructive_port(vault_root, "port-existing-non-destructive")
            _clear_onboarding_state(state_key)
            return {
                "message": "Non-destructive port completed successfully.",
                "needs_user_input": False,
                "phase": "completed",
                "result": result,
            }
        if _is_strict_no(normalized):
            _clear_onboarding_state(state_key)
            return {
                "message": "Non-destructive port cancelled without execution.",
                "needs_user_input": False,
                "phase": "cancelled",
            }
        return {
            "message": (
                "Waiting for confirmation. "
                f"Type exactly: {NON_DESTRUCTIVE_CONFIRMATION}"
            ),
            "needs_user_input": True,
            "phase": "confirm_non_destructive",
        }

    if phase == "confirm_destructive":
        if _matches_exact_confirmation(
            normalized,
            DESTRUCTIVE_EXECUTION_CONFIRMATION,
            LEGACY_DESTRUCTIVE_EXECUTION_CONFIRMATION,
        ):
            result = apply_destructive_port(vault_root, "port-existing-destructive")
            _clear_onboarding_state(state_key)
            return {
                "message": "Destructive port completed. Review the log file at Meta/vault-gateway-log.md.",
                "needs_user_input": False,
                "phase": "completed",
                "result": result,
            }
        if _is_strict_no(normalized):
            _clear_onboarding_state(state_key)
            return {
                "message": "Destructive port cancelled.",
                "needs_user_input": False,
                "phase": "cancelled",
            }
        return {
            "message": (
                "Waiting for final confirmation. "
                f"Type exactly: {DESTRUCTIVE_EXECUTION_CONFIRMATION}"
            ),
            "needs_user_input": True,
            "phase": "confirm_destructive",
        }

    onboarding["phase"] = "choose_path"
    _save_onboarding_state(state_key, onboarding)
    return {
        "message": "Onboarding flow reset. Choose 'new vault' or 'port existing vault'.",
        "needs_user_input": True,
        "phase": "choose_path",
    }


def handle_route(route: str, payload: dict, metadata: dict) -> dict:
    vault_root = get_vault_root()

    try:
        assert_safe_vault_root(vault_root)

        if route == "onboarding":
            result = handle_onboarding(payload)
            result["vault_root"] = str(vault_root)
            return result

        if route == "template.list":
            result = list_templates(
                vault_root=vault_root,
                query=str(payload.get("query") or ""),
                path_prefix=payload.get("path_prefix"),
                include_legacy=_as_bool(payload.get("include_legacy", True)),
                limit=int(payload.get("limit") or 50),
                mode=str(payload.get("mode") or "capture"),
            )
            return {
                "message": "Template catalog loaded.",
                "needs_user_input": False,
                "result": result,
            }

        if route == "template.inspect":
            result = inspect_template(
                vault_root=vault_root,
                template_path=str(payload.get("template_path") or ""),
                include_body_preview=_as_bool(payload.get("include_body_preview", False)),
                include_placeholders=_as_bool(payload.get("include_placeholders", True)),
            )
            return {
                "message": "Template inspected successfully.",
                "needs_user_input": False,
                "result": result,
            }

        if route == "note.capture":
            append_captured_context = payload.get("append_captured_context")
            result = capture_note(
                vault_root=vault_root,
                text=str(payload.get("text") or ""),
                title=payload.get("title"),
                target_folder=payload.get("target_folder"),
                template_hint=payload.get("template_hint"),
                tags=payload.get("tags"),
                template_path=payload.get("template_path"),
                template_id=payload.get("template_id"),
                field_values=payload.get("field_values"),
                template_mode=str(payload.get("template_mode") or "legacy"),
                missing_fields_policy=str(payload.get("missing_fields_policy") or "ask"),
                append_captured_context=(
                    True if append_captured_context is None else _as_bool(append_captured_context)
                ),
            )
            if result.get("pending"):
                pending_result = dict(result)
                pending_result.pop("pending", None)
                return {
                    "message": "Required template fields are still missing.",
                    "needs_user_input": True,
                    "phase": result.get("phase"),
                    "result": pending_result,
                }
            return {
                "message": "Note captured successfully." + _maintenance_suffix(result),
                "needs_user_input": False,
                "result": result,
            }

        if route == "note.read":
            result = read_note(
                vault_root=vault_root,
                path=payload.get("path"),
                include_frontmatter=_as_bool(payload.get("include_frontmatter", True)),
                include_body=_as_bool(payload.get("include_body", True)),
            )
            return {
                "message": "Note read successfully.",
                "needs_user_input": False,
                "result": result,
            }

        if route == "note.update":
            fm_fields = payload.get("frontmatter_fields")
            result = update_note(
                vault_root=vault_root,
                text=str(payload.get("text") or ""),
                path=payload.get("path"),
                mode=str(payload.get("mode") or "append"),
                frontmatter_fields=fm_fields if isinstance(fm_fields, dict) else None,
                section_heading=payload.get("section_heading"),
            )
            return {
                "message": "Note updated successfully." + _maintenance_suffix(result),
                "needs_user_input": False,
                "result": result,
            }

        if route == "note.search":
            result = search_notes(
                vault_root=vault_root,
                query=str(payload.get("query") or ""),
                limit=int(payload.get("limit") or 20),
                path_prefix=payload.get("path_prefix"),
            )
            return {
                "message": "Search completed.",
                "needs_user_input": False,
                "result": result,
            }

        if route == "note.link":
            result = link_notes(
                vault_root=vault_root,
                source_path=payload.get("source_path"),
                target_path=payload.get("target_path"),
                bidirectional=_as_bool(payload.get("bidirectional", False)),
            )
            return {
                "message": "Link between notes updated.",
                "needs_user_input": False,
                "result": result,
            }

        if route == "note.file":
            result = file_note(
                vault_root=vault_root,
                source_path=payload.get("source_path"),
                target_folder=str(payload.get("target_folder") or "01-Projects"),
            )
            return {
                "message": "Note moved successfully." + _maintenance_suffix(result),
                "needs_user_input": False,
                "result": result,
            }

        if route == "note.rename":
            result = rename_note(
                vault_root=vault_root,
                path=payload.get("path"),
                new_title=payload.get("new_title"),
                rewrite_wikilinks=_as_bool(payload.get("rewrite_wikilinks", True)),
            )
            return {
                "message": "Note renamed successfully." + _maintenance_suffix(result),
                "needs_user_input": False,
                "result": result,
            }

        if route == "inbox.triage":
            summary = summarize_inbox(vault_root)
            return {
                "message": "Inbox triage summary generated.",
                "needs_user_input": False,
                "summary": summary,
            }

        if route == "vault.defrag":
            summary = summarize_defrag(vault_root)
            return {
                "message": "Structural defrag summary generated.",
                "needs_user_input": False,
                "summary": summary,
            }

        if route == "vault.audit":
            summary = summarize_audit(vault_root)
            return {
                "message": "Vault audit summary generated.",
                "needs_user_input": False,
                "summary": summary,
            }

        if route == "vault.deep-clean":
            summary = summarize_deep_clean(vault_root)
            return {
                "message": "Deep clean summary generated.",
                "needs_user_input": False,
                "summary": summary,
            }

        if route == "tags.garden":
            summary = summarize_tag_garden(vault_root)
            return {
                "message": "Tag garden summary generated.",
                "needs_user_input": False,
                "summary": summary,
            }
    except ConfigurationError:
        return {
            "message": "Invalid vault configuration in the environment.",
            "error": "configuration_error",
            "needs_user_input": False,
        }
    except ValueError as exc:
        return {
            "message": "Invalid route input.",
            "error": "validation_error",
            "details": str(exc),
            "needs_user_input": True,
        }
    except OSError as exc:
        details = exc.strerror or str(exc) or type(exc).__name__
        filename = getattr(exc, "filename", None)
        message = "Failed to access vault files."
        if filename:
            message = f"{message} ({type(exc).__name__}: {details}: {filename})"
        else:
            message = f"{message} ({type(exc).__name__}: {details})"
        return {
            "message": message,
            "error": "execution_error",
            "needs_user_input": False,
        }
    except Exception:
        return {
            "message": "Internal error while executing route.",
            "error": "internal_error",
            "needs_user_input": False,
        }

    return {
        "message": "Route has no implemented handler.",
        "error": "handler_not_implemented",
        "needs_user_input": False,
    }
