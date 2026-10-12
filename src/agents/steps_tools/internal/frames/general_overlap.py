from __future__ import annotations

import re

from src.agents.steps_tools.document.morphology import ALNUM_LOWER, normalized_text

_TRANSFER_RE = re.compile(
    r"\b(?:передач\w*|переда\w*|направ\w*|перенос\w*|выгруз\w*|загруз\w*|transfer\w*|send\w*|upload\w*)\b",
    re.IGNORECASE,
)
_REGISTRATION_RE = re.compile(r"\b(?:регистрац\w*|registr\w*)\b", re.IGNORECASE)
_CATEGORICAL_REGISTRATION_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.\s*)?регистрац\w*\s+(?:заявлен\w*|запрос\w*)"
    r"(?:\s+и\s+приложенн\w*(?:\s+к\s+нему)?\s+документ\w*)?\s+осуществля\w*\b",
    re.IGNORECASE,
)
_CONDITIONAL_SCOPE_RE = re.compile(r"\b(?:при|если|в\s+случае|за\s+исключением|только\s+при)\b", re.IGNORECASE)


def normalize_material_value(value: str) -> str:
    """Conservative value normalization used only for exact operand dedupe."""
    normalized = normalized_text(value)
    normalized = re.sub(r"\bедин\w*\s+систем\w*\b", "единая система", normalized)
    return normalized


def material_tokens(value: object) -> list[str]:
    return ALNUM_LOWER.findall(normalized_text(str(value)))


def token_matches(left: str, right: str) -> bool:
    return left == right or (len(left) >= 5 and len(right) >= 5 and left[:5] == right[:5])


def text_mentions_material(text: str, value: object) -> bool:
    "Назван ли операнд СВОИМ текстом-носителем."
    text_items = material_tokens(text)
    value_items = material_tokens(value)
    return bool(value_items) and all(any(token_matches(item, token) for item in text_items) for token in value_items)


_CHANNEL_NAMES: dict[str, re.Pattern[str]] = {
    "multifunctional_center": re.compile(r"\bмфц\b|многофункциональн\w*\s+центр\w*"),
    "public_services_portal": re.compile(r"\b(?:епгу|рпгу)\b|портал\w*\s+(?:государственн\w*\s+)?услуг"),
    "authority_office": re.compile(r"\bминистерств\w*|\bведомств\w*|\bорган\w*"),
}

SPECIFIC_CHANNELS = ("multifunctional_center", "authority_office")


def canonical_channel(value: str) -> str:
    normalized = normalize_material_value(value)
    return next((key for key, pattern in _CHANNEL_NAMES.items() if pattern.search(normalized)), normalized)


def categorical_registration_scope(scope_text: str) -> bool:
    "Recognize an exact, positive rule about registration as a whole."
    normalized = " ".join(scope_text.split())
    return not _CONDITIONAL_SCOPE_RE.search(normalized) and bool(_CATEGORICAL_REGISTRATION_RE.search(normalized))


def specific_registration_branch(scope_text: str, channel: str) -> bool:
    """Require a positive conditional registration branch naming the channel."""
    normalized = normalize_material_value(scope_text)
    key = canonical_channel(channel)
    if key not in SPECIFIC_CHANNELS:
        return False
    return bool(
        re.search(
            rf"\bпри\s+представлени\w*.+?регистрац\w*\s+осуществля\w*.+?(?:{_CHANNEL_NAMES[key].pattern})",
            normalized,
            re.DOTALL,
        )
    )


def system_is_registration_operand(clause_text: str, raw_system: str) -> bool:
    """Reject systems tied only to a later transfer rather than registration."""
    text = normalize_material_value(clause_text)
    needle = normalize_material_value(raw_system)
    position = text.find(needle)
    if position < 0:
        return False
    boundary = max(text.rfind(mark, 0, position) for mark in (".", ";", "\n", ","))
    local = text[boundary + 1 : position + len(needle)]
    registration = list(_REGISTRATION_RE.finditer(local))
    if not registration:
        # Passive legal drafting can put the system before the predicate.
        local = text[max(0, position - 240) : position + len(needle) + 80]
        registration = list(_REGISTRATION_RE.finditer(local))
    if not registration:
        return False
    transfer = list(_TRANSFER_RE.finditer(local))
    return not transfer or transfer[-1].start() < registration[-1].start()


_PREMISE_IN_TEXT = re.compile(
    r"\bпри\s+(наличии|отсутствии)\s+(.+?)(?=\s+в\s+(?:части|течение)\b|\s*[,;:—–-]|\s+\d|$)",
    re.IGNORECASE,
)


def explicit_premises(text: str) -> frozenset[tuple[str, str]]:
    "Явные предпосылки применимости, объявленные САМИМ текстом нормы."
    found: set[tuple[str, str]] = set()
    for match in _PREMISE_IN_TEXT.finditer(text):
        value = normalize_material_value(match.group(2))
        if value:
            found.add((match.group(1).casefold(), value))
    return frozenset(found)


_APPLICABILITY_MARKER = re.compile(
    r"\b(?:если|когда|в\s+случа\w*|в\s+зависимости\s+от|при\s+услови\w*|при\s+наличии"
    r"|при\s+отсутствии|при\s+соблюдении|при\s+непредставлении|за\s+исключением|кроме\s+случ\w*"
    r"|постольку|поскольку|с\s+уч[её]том\s+того|при\s+том\s+условии)\b",
    re.IGNORECASE,
)


def unresolved_applicability_markers(text: str) -> tuple[str, ...]:
    "Признаки условности, НЕ покрытые разобранной предпосылкой."
    resolved = [match.span() for match in _PREMISE_IN_TEXT.finditer(text)]
    return tuple(
        match.group(0).casefold()
        for match in _APPLICABILITY_MARKER.finditer(text)
        if not any(start <= match.start() < end for start, end in resolved)
    )
