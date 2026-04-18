import re


def normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def contains_any(text: str, terms: list[str]) -> bool:
    normalized = normalize_text(text)
    for term in terms:
        if normalize_text(term) in normalized:
            return True
    return False


def _tokenize(text: str) -> list[str]:
    normalized = normalize_text(text)
    return re.findall(r"[a-z0-9]+", normalized)


def is_yes(text: str) -> bool:
    normalized = normalize_text(text)
    if normalized in {"yes", "y", "sim", "s", "ok"}:
        return True

    tokens = set(_tokenize(normalized))
    return bool(tokens.intersection({"yes", "sim", "ok", "confirmo", "pode", "prosseguir", "continue"}))


def is_no(text: str) -> bool:
    normalized = normalize_text(text)
    if normalized in {"no", "n", "nao"}:
        return True

    tokens = set(_tokenize(normalized))
    return bool(tokens.intersection({"no", "nao", "cancel", "cancelar", "parar", "stop"}))


TAG_PATTERN = re.compile(r"(?<!\w)#([A-Za-z0-9_/-]+)")
