#!/usr/bin/env python3
"""Patch Hermes dashboard profile metadata for Josemar deployments.

Hermes exposes the base HERMES_HOME profile as the hardcoded name
"default". Hermes One uses /api/profiles for its employee cards, so the
base profile appears as "default" even when the assistant identity is
Josemar. This build-time patch keeps the runtime profile mapped to
HERMES_HOME while exposing a configurable display name to dashboard clients.
"""

from __future__ import annotations

from pathlib import Path


PROFILES_PATH = Path("/opt/hermes/hermes_cli/profiles.py")
WEB_SERVER_PATH = Path("/opt/hermes/hermes_cli/web_server.py")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Expected snippet not found in {path}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    PROFILES_PATH,
    '_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")\n',
    '_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")\n\n\n'
    'def _default_profile_display_name() -> str:\n'
    '    """Return the public label for the base HERMES_HOME profile."""\n'
    '    value = (\n'
    '        os.environ.get("HERMES_DEFAULT_PROFILE_DISPLAY_NAME")\n'
    '        or os.environ.get("API_SERVER_MODEL_NAME")\n'
    '        or ""\n'
    '    ).strip()\n'
    '    candidate = value or "default"\n'
    '    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", candidate):\n'
    '        return "default"\n'
    '    canon = candidate.lower()\n'
    '    if canon != "default":\n'
    '        try:\n'
    '            if (_get_profiles_root() / canon).is_dir():\n'
    '                return "default"\n'
    '        except Exception:\n'
    '            pass\n'
    '    return candidate\n\n\n'
    'def _is_default_profile_alias(name: str) -> bool:\n'
    '    return bool(name) and name.casefold() == _default_profile_display_name().casefold()\n',
)

replace_once(
    PROFILES_PATH,
    '    if stripped.casefold() == "default":\n'
    '        return "default"\n',
    '    if stripped.casefold() == "default" or _is_default_profile_alias(stripped):\n'
    '        return "default"\n',
)

replace_once(
    PROFILES_PATH,
    '    if name == "default":\n'
    '        return  # special alias for ~/.hermes\n',
    '    if name == "default" or _is_default_profile_alias(name):\n'
    '        return  # special alias for ~/.hermes\n',
)

replace_once(
    PROFILES_PATH,
    '            name="default",\n'
    '            path=default_home,\n',
    '            name=_default_profile_display_name(),\n'
    '            path=default_home,\n',
)

replace_once(
    WEB_SERVER_PATH,
    'def _profile_to_dict(info) -> Dict[str, Any]:\n'
    '    return {\n'
    '        "name": _profile_attr(info, "name", ""),\n',
    'def _profile_to_dict(info) -> Dict[str, Any]:\n'
    '    name = _profile_attr(info, "name", "")\n'
    '    if bool(_profile_attr(info, "is_default", False)):\n'
    '        try:\n'
    '            from hermes_cli import profiles as profiles_mod\n'
    '            name = profiles_mod._default_profile_display_name()\n'
    '        except Exception:\n'
    '            pass\n'
    '    return {\n'
    '        "name": name,\n',
)

replace_once(
    WEB_SERVER_PATH,
    '            "name": "default",\n',
    '            "name": getattr(profiles_mod, "_default_profile_display_name", lambda: "default")(),\n',
)

replace_once(
    WEB_SERVER_PATH,
    '    return {"active": active, "current": current}\n',
    '    display_name = getattr(profiles_mod, "_default_profile_display_name", lambda: "default")()\n'
    '    def public_name(name: str) -> str:\n'
    '        try:\n'
    '            return display_name if profiles_mod.normalize_profile_name(name) == "default" else name\n'
    '        except Exception:\n'
    '            return name\n'
    '    return {"active": public_name(active), "current": public_name(current)}\n',
)

replace_once(
    WEB_SERVER_PATH,
    'def _profile_setup_command(name: str) -> str:\n'
    '    """Return the shell command used to configure a profile in the CLI."""\n'
    '    _resolve_profile_dir(name)\n'
    '    return "hermes setup" if name == "default" else f"{name} setup"\n',
    'def _profile_setup_command(name: str) -> str:\n'
    '    """Return the shell command used to configure a profile in the CLI."""\n'
    '    from hermes_cli import profiles as profiles_mod\n'
    '    canon = profiles_mod.normalize_profile_name(name)\n'
    '    _resolve_profile_dir(canon)\n'
    '    return "hermes setup" if canon == "default" else f"{canon} setup"\n',
)

print("Patched Hermes dashboard default profile display name")
