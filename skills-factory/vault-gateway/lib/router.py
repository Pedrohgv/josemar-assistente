from pathlib import Path
import json


ROUTES_FILE = Path(__file__).resolve().parent.parent / "contracts" / "routes.json"


def load_routes() -> dict:
    try:
        with ROUTES_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Route contract file not found: {ROUTES_FILE}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid route contract JSON: {exc}") from exc

    routes = data.get("routes", {})
    if not isinstance(routes, dict) or not routes:
        raise RuntimeError("Route contract has no valid routes")

    return routes


def parse_route(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""

    candidate = payload.get("route")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return ""


def extract_route_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}

    nested = payload.get("payload")
    return dict(nested) if isinstance(nested, dict) else {}


def is_known_route(route_name: str, routes: dict | None = None) -> bool:
    if routes is None:
        routes = load_routes()
    return route_name in routes


def _type_error(key: str, expected: str) -> str:
    return f"Invalid type for '{key}'. Expected {expected}."


def _extract_enum_options(descriptor: str) -> list[str]:
    descriptor_text = descriptor.strip().lower()
    if ":" not in descriptor_text:
        return []

    options_part = descriptor_text.split(":", 1)[1]
    options_part = options_part.split("(", 1)[0]
    if "|" not in options_part:
        return []

    options = [item.strip() for item in options_part.split("|") if item.strip()]
    return options


def _validate_value_type(key: str, value: object, descriptor: str) -> str | None:
    descriptor_text = descriptor.strip().lower()

    enum_options = _extract_enum_options(descriptor_text)
    if enum_options:
        if not isinstance(value, str):
            return _type_error(key, f"one of: {', '.join(enum_options)}")
        normalized = value.strip().lower()
        if normalized not in enum_options:
            return _type_error(key, f"one of: {', '.join(enum_options)}")
        return None

    if "list[string]" in descriptor_text:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return _type_error(key, "list[string]")
        return None

    if "boolean" in descriptor_text:
        if not isinstance(value, bool):
            return _type_error(key, "boolean")
        return None

    if "number" in descriptor_text:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return _type_error(key, "number")
        return None

    if "object" in descriptor_text:
        if not isinstance(value, dict):
            return _type_error(key, "object")
        return None

    if "relative path" in descriptor_text:
        if not isinstance(value, str):
            return _type_error(key, "relative path string")
        raw = value.strip().replace("\\", "/")
        if not raw or raw.startswith("/") or ".." in raw.split("/"):
            return _type_error(key, "relative path string")
        return None

    if "string" in descriptor_text:
        if not isinstance(value, str):
            return _type_error(key, "string")
        return None

    return None


def validate_route_payload(route_name: str, route_payload: dict, routes: dict | None = None) -> list[str]:
    if routes is None:
        routes = load_routes()

    metadata = routes.get(route_name, {})
    schema = metadata.get("payload", {})
    if not isinstance(schema, dict):
        return []

    errors: list[str] = []
    payload_keys = set(route_payload.keys())
    allowed_keys = set(schema.keys())

    unknown_keys = sorted(payload_keys - allowed_keys)
    if unknown_keys:
        errors.append(f"Unknown payload keys: {', '.join(unknown_keys)}")

    required_keys = []
    for key, descriptor in schema.items():
        if isinstance(descriptor, str) and descriptor.strip().lower().startswith("required"):
            required_keys.append(key)

    for key in required_keys:
        value = route_payload.get(key)
        if value is None:
            errors.append(f"Missing required payload key: {key}")
            continue
        if isinstance(value, str) and not value.strip():
            errors.append(f"Missing required payload key: {key}")

    for key, descriptor in schema.items():
        if not isinstance(descriptor, str):
            continue
        if key not in route_payload:
            continue

        value = route_payload.get(key)
        if value is None:
            if descriptor.strip().lower().startswith("required"):
                errors.append(f"Missing required payload key: {key}")
            continue

        type_error = _validate_value_type(key, value, descriptor)
        if type_error:
            errors.append(type_error)

    return errors


def resolve_alias(route_name: str, routes: dict | None = None) -> str:
    if routes is None:
        routes = load_routes()
    metadata = routes.get(route_name, {})
    alias_of = metadata.get("alias_of")
    if isinstance(alias_of, str) and alias_of in routes:
        return alias_of
    return route_name


def get_route_metadata(route_name: str, routes: dict | None = None) -> dict:
    if routes is None:
        routes = load_routes()
    return routes.get(route_name, {})
