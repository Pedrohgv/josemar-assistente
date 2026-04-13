import os
from pathlib import Path


class ConfigurationError(ValueError):
    pass


def get_workspace_root() -> Path:
    return Path(os.getenv("WORKSPACE_DIR", "/root/.openclaw/workspace"))


def get_vault_root() -> Path:
    return Path(os.getenv("OBSIDIAN_VAULT_DIR", "/root/.openclaw/obsidian"))


def assert_safe_vault_root(vault_root: Path) -> None:
    resolved = vault_root.resolve(strict=False)
    resolved_text = str(resolved).strip()
    if not resolved_text:
        raise ConfigurationError("Unsafe vault root: empty path")

    allowed_raw = os.getenv("VAULT_GATEWAY_ALLOWED_ROOTS", "/root/.openclaw/obsidian")
    allowed_values = [item.strip() for item in allowed_raw.split(":") if item.strip()]
    allowed_prefixes = [Path(item).resolve(strict=False) for item in allowed_values]

    if not allowed_prefixes:
        raise ConfigurationError("No allowed vault roots configured")

    for prefix in allowed_prefixes:
        if resolved == prefix or prefix in resolved.parents:
            return

    allowed_text = ", ".join(str(item) for item in allowed_prefixes)
    raise ConfigurationError(
        f"Vault root '{resolved}' is outside allowed prefixes: {allowed_text}"
    )


def get_state_dir() -> Path:
    state_dir = get_workspace_root() / ".vault-gateway"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_state_file() -> Path:
    return get_state_dir() / "state.json"
