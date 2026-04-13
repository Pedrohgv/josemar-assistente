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
    link_notes,
    scan_vault,
    search_notes,
    summarize_audit,
    summarize_deep_clean,
    summarize_defrag,
    summarize_inbox,
    summarize_tag_garden,
    update_note,
)


BACKUP_CONFIRMATION = "eu tenho backup e quero continuar"
NON_DESTRUCTIVE_CONFIRMATION = "aprovar port nao destrutivo"
DESTRUCTIVE_EXECUTION_CONFIRMATION = "executar port destrutivo"


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


def _is_strict_yes(text: str) -> bool:
    return normalize_text(text) in {"sim", "s", "yes", "y", "ok"}


def _is_strict_no(text: str) -> bool:
    return normalize_text(text) in {"nao", "n", "no"}


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
        "Plano de port detectado:",
        f"- Vault existe: {plan.get('vault_exists')}",
        f"- Modo: {'destrutivo' if plan.get('destructive') else 'nao destrutivo'}",
        f"- Pastas padrao faltando: {', '.join(plan.get('missing_standard_dirs', [])) or '(nenhuma)'}",
        f"- Itens fora do padrao na raiz: {', '.join(plan.get('non_standard_root_entries', [])) or '(nenhum)'}",
        "- Acoes:",
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
            "message": "Onboarding cancelado. Quando quiser retomar, diga 'onboarding' ou 'initialize my vault'.",
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
            normalized = "novo vault"

        if contains_any(normalized, ["port", "migrat", "vault existente", "existing vault"]):
            onboarding = {
                "phase": "ask_destructive",
                "mode": "port",
                "started_at": onboarding.get("started_at", now),
            }
            _save_onboarding_state(state_key, onboarding)
            return {
                "message": (
                    "Modo de port selecionado. Deseja executar em modo destrutivo? "
                    "(sim/nao).\n"
                    "- sim: pode mover itens fora do padrao para arquivo\n"
                    "- nao: apenas cria estrutura faltante"
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
                    "Modo novo vault selecionado. Vou criar a estrutura padrao sem sobrescrever arquivos existentes. "
                    "Posso executar agora? (sim/nao)"
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
                "Vamos iniciar o onboarding do vault. Escolha uma opcao:\n"
                "1) novo vault\n"
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
                "message": "Estrutura base criada para novo vault com modo seguro (nao destrutivo).",
                "needs_user_input": False,
                "phase": "completed",
                "result": result,
            }
        if _is_strict_no(normalized):
            _clear_onboarding_state(state_key)
            return {
                "message": "Onboarding cancelado sem alteracoes.",
                "needs_user_input": False,
                "phase": "cancelled",
            }
        return {
            "message": "Responda com 'sim' para criar a estrutura base ou 'nao' para cancelar.",
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
                    "ATENCAO: modo destrutivo pode mover conteudo para 04-Archive e alterar a organizacao da raiz.\n"
                    "RECOMENDACAO FORTE: faca backup completo do vault antes de continuar.\n"
                    f"Para continuar, digite exatamente: {BACKUP_CONFIRMATION}"
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
                    f"Se estiver de acordo, digite exatamente: {NON_DESTRUCTIVE_CONFIRMATION}"
                ),
                "needs_user_input": True,
                "phase": "confirm_non_destructive",
                "plan": plan,
            }
        return {
            "message": "Responda com 'sim' ou 'nao' para o modo destrutivo.",
            "needs_user_input": True,
            "phase": "ask_destructive",
        }

    if phase == "warn_backup":
        if normalized == BACKUP_CONFIRMATION:
            plan = build_port_plan(scan_vault(vault_root), destructive=True)
            onboarding["phase"] = "confirm_destructive"
            onboarding["plan"] = plan
            _save_onboarding_state(state_key, onboarding)
            return {
                "message": (
                    f"{_render_plan(plan)}\n\n"
                    f"Para executar o port destrutivo, digite exatamente: {DESTRUCTIVE_EXECUTION_CONFIRMATION}"
                ),
                "needs_user_input": True,
                "phase": "confirm_destructive",
                "plan": plan,
            }
        return {
            "message": (
                "Ainda aguardando confirmacao de backup.\n"
                f"Digite exatamente: {BACKUP_CONFIRMATION}"
            ),
            "needs_user_input": True,
            "phase": "warn_backup",
        }

    if phase == "confirm_non_destructive":
        if normalized == NON_DESTRUCTIVE_CONFIRMATION:
            result = apply_non_destructive_port(vault_root, "port-existing-non-destructive")
            _clear_onboarding_state(state_key)
            return {
                "message": "Port nao destrutivo concluido com sucesso.",
                "needs_user_input": False,
                "phase": "completed",
                "result": result,
            }
        if _is_strict_no(normalized):
            _clear_onboarding_state(state_key)
            return {
                "message": "Port nao destrutivo cancelado sem execucao.",
                "needs_user_input": False,
                "phase": "cancelled",
            }
        return {
            "message": (
                "Aguardando confirmacao. "
                f"Digite exatamente: {NON_DESTRUCTIVE_CONFIRMATION}"
            ),
            "needs_user_input": True,
            "phase": "confirm_non_destructive",
        }

    if phase == "confirm_destructive":
        if normalized == DESTRUCTIVE_EXECUTION_CONFIRMATION:
            result = apply_destructive_port(vault_root, "port-existing-destructive")
            _clear_onboarding_state(state_key)
            return {
                "message": "Port destrutivo concluido. Revise o arquivo de log em Meta/vault-gateway-log.md.",
                "needs_user_input": False,
                "phase": "completed",
                "result": result,
            }
        if _is_strict_no(normalized):
            _clear_onboarding_state(state_key)
            return {
                "message": "Port destrutivo cancelado.",
                "needs_user_input": False,
                "phase": "cancelled",
            }
        return {
            "message": (
                "Aguardando confirmacao final. "
                f"Digite exatamente: {DESTRUCTIVE_EXECUTION_CONFIRMATION}"
            ),
            "needs_user_input": True,
            "phase": "confirm_destructive",
        }

    onboarding["phase"] = "choose_path"
    _save_onboarding_state(state_key, onboarding)
    return {
        "message": "Fluxo de onboarding resetado. Escolha 'novo vault' ou 'port existing vault'.",
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

        if route == "note.capture":
            result = capture_note(
                vault_root=vault_root,
                text=str(payload.get("text") or ""),
                title=payload.get("title"),
                target_folder=str(payload.get("target_folder") or "00-Inbox"),
                template_hint=payload.get("template_hint"),
                tags=payload.get("tags"),
            )
            return {
                "message": "Nota capturada com sucesso.",
                "needs_user_input": False,
                "result": result,
            }

        if route == "note.update":
            result = update_note(
                vault_root=vault_root,
                text=str(payload.get("text") or ""),
                path=payload.get("path"),
                mode=str(payload.get("mode") or "append"),
            )
            return {
                "message": "Nota atualizada com sucesso.",
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
                "message": "Busca concluida.",
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
                "message": "Link entre notas atualizado.",
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
                "message": "Nota movida com sucesso.",
                "needs_user_input": False,
                "result": result,
            }

        if route == "inbox.triage":
            summary = summarize_inbox(vault_root)
            return {
                "message": "Resumo de inbox triage gerado.",
                "needs_user_input": False,
                "summary": summary,
            }

        if route == "vault.defrag":
            summary = summarize_defrag(vault_root)
            return {
                "message": "Resumo de defrag estrutural gerado.",
                "needs_user_input": False,
                "summary": summary,
            }

        if route == "vault.audit":
            summary = summarize_audit(vault_root)
            return {
                "message": "Resumo de vault audit gerado.",
                "needs_user_input": False,
                "summary": summary,
            }

        if route == "vault.deep-clean":
            summary = summarize_deep_clean(vault_root)
            return {
                "message": "Resumo de deep clean gerado.",
                "needs_user_input": False,
                "summary": summary,
            }

        if route == "tags.garden":
            summary = summarize_tag_garden(vault_root)
            return {
                "message": "Resumo de tag garden gerado.",
                "needs_user_input": False,
                "summary": summary,
            }
    except ConfigurationError:
        return {
            "message": "Configuracao de vault invalida no ambiente.",
            "error": "configuration_error",
            "needs_user_input": False,
            "playbook": metadata.get("playbook"),
        }
    except ValueError as exc:
        return {
            "message": "Entrada invalida para rota.",
            "error": "validation_error",
            "details": str(exc),
            "needs_user_input": True,
            "playbook": metadata.get("playbook"),
        }
    except OSError:
        return {
            "message": "Falha ao acessar arquivos do vault.",
            "error": "execution_error",
            "needs_user_input": False,
            "playbook": metadata.get("playbook"),
        }
    except Exception:
        return {
            "message": "Erro interno ao executar rota.",
            "error": "internal_error",
            "needs_user_input": False,
            "playbook": metadata.get("playbook"),
        }

    return {
        "message": "Rota sem handler implementado.",
        "error": "handler_not_implemented",
        "needs_user_input": False,
        "playbook": metadata.get("playbook"),
    }
