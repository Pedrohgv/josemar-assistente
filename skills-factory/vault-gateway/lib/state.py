import json
from pathlib import Path

from lib.paths import get_state_file


def load_state() -> dict:
    state_file = get_state_file()
    if not state_file.exists():
        return {}

    try:
        with state_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(data, dict):
        return {}
    return data


def save_state(state: dict) -> None:
    state_file = get_state_file()
    with state_file.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=True, indent=2)


def clear_state() -> None:
    state_file: Path = get_state_file()
    if state_file.exists():
        state_file.unlink()
