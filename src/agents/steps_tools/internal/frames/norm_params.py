from __future__ import annotations

import re
from dataclasses import dataclass

from src.agents.enums.internal.frames.norm_params import CalendarKind, Modality, ParameterType, Polarity
from src.agents.schemas.internal.frames.norm_params import Parameter, Qualifier
from src.agents.schemas.parse.structure import DocumentStructureNode
from src.agents.steps_tools.document.morphology import (
    CLAUSE_BOUNDARY,
    LETTERS,
    NEGATION_PARTICLES,
    QUALIFIER_STOP,
    SENTENCE_SPLIT,
)
from src.common.config.config import settings

_PROHIBITION = re.compile(r"\b(запрещ\w*|не\s+допуска\w*|не\s+вправе|не\s+может\s+быть)\b", re.IGNORECASE)
#: Модальный ОПЕРАТОР обязанности: слово, которым норма сама заявляет предписание.
_OBLIGATION_OPERATOR = re.compile(r"\b(обязан\w*|должн\w*|подлежит)\b", re.IGNORECASE)


_OBLIGATION_PERFORMATIVE = re.compile(r"\b(представля\w*|направля\w*|осуществля\w*)\b", re.IGNORECASE)
_RIGHT = re.compile(r"\b(вправе|может|имеет\s+право|по\s+(?:своему\s+)?желанию)\b", re.IGNORECASE)
_ESTABLISH = re.compile(r"\b(устанавлива\w*|определя\w*|явля\w*|понимается|составля\w*|признаётся|признается)\b", re.IGNORECASE)


_QUAL_AT_MOST = re.compile(r"(?:не\s+более|не\s+позднее|максимальн\w*|в\s+течение|не\s+превыша\w*)", re.IGNORECASE)
_QUAL_AT_LEAST = re.compile(r"(?:не\s+менее|не\s+ранее|минимальн\w*)", re.IGNORECASE)


_QUAL_EXACT = re.compile(r"(?:\bровно\b|\bточно\b)", re.IGNORECASE)

#: Окно слева от числа, в котором ищем квалификатор внутри самого листа.
_QUALIFIER_WINDOW = int(settings["agents.internal.norm_params.qualifier_window"])


_QUALIFIER_NOUN_WINDOW = int(settings["agents.internal.norm_params.qualifier_noun_window"])


_QUALIFIER_INHERITANCE_NOUNS: dict[ParameterType, re.Pattern[str]] = {
    ParameterType.DURATION: re.compile(r"\bсрок\w*", re.IGNORECASE),
}


_NUMBER = r"(?<![\d.,])(?:\d{1,3}(?:[\u00a0\u202f ]\d{3})+|\d+)(?:[.,]\d+)?"


_CALENDAR_ATTRIBUTE = r"(?:рабоч\w+|календарн\w+)"
_DURATION_UNIT = (
    rf"(?:{_CALENDAR_ATTRIBUTE}\s+)?"
    r"(?:дн(?:ей|я|и)|день|недел(?:я|и|ь|ю|ей)|месяц(?:а|ев|ы)?|год(?:а|ов|у)?|лет|час(?:а|ов)?|минут(?:а|ы|у)?)"
)


_AGE_MARKER = r"(?:возраст\w*|старше|младше|моложе|достигш\w*|достижени\w*)"
_AGE_UNIT = r"(?:лет|года|год)"
_MONEY_UNIT = r"(?:руб\w*|₽)"
_SHARE_UNIT = r"(?:процент\w*|%)"


_PARAM_PATTERNS: list[tuple[ParameterType, re.Pattern[str]]] = [
    (
        ParameterType.AGE,
        re.compile(rf"{_AGE_MARKER}(?:\s+возраста)?\s+(?P<value>(?P<num>{_NUMBER})\s*{_AGE_UNIT})\b", re.IGNORECASE),
    ),
    (ParameterType.DURATION, re.compile(rf"(?P<value>(?P<num>{_NUMBER})\s+{_DURATION_UNIT})\b", re.IGNORECASE)),
    (ParameterType.MONEY, re.compile(rf"(?P<value>(?P<num>{_NUMBER})\s*{_MONEY_UNIT})", re.IGNORECASE)),
    (ParameterType.SHARE, re.compile(rf"(?P<value>(?P<num>{_NUMBER})\s*{_SHARE_UNIT})", re.IGNORECASE)),
]


_RANGE_PATTERNS: list[tuple[ParameterType, re.Pattern[str]]] = [
    (
        ParameterType.AGE,
        re.compile(
            rf"{_AGE_MARKER}(?:\s+возраста)?\s+от\s+(?P<low>{_NUMBER})\s+до\s+"
            rf"(?P<high>(?P<high_num>{_NUMBER})\s*{_AGE_UNIT})\b",
            re.IGNORECASE,
        ),
    ),
    (
        ParameterType.DURATION,
        re.compile(
            rf"\bот\s+(?P<low>{_NUMBER})\s+до\s+(?P<high>(?P<high_num>{_NUMBER})\s+{_DURATION_UNIT})\b",
            re.IGNORECASE,
        ),
    ),
    (
        ParameterType.MONEY,
        re.compile(
            rf"\bот\s+(?P<low>{_NUMBER})\s+до\s+(?P<high>(?P<high_num>{_NUMBER})\s*{_MONEY_UNIT})",
            re.IGNORECASE,
        ),
    ),
    (
        ParameterType.SHARE,
        re.compile(
            rf"\bот\s+(?P<low>{_NUMBER})\s+до\s+(?P<high>(?P<high_num>{_NUMBER})\s*{_SHARE_UNIT})",
            re.IGNORECASE,
        ),
    ),
]


_DURATION_UNIT_FORMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:дн\w*|день)$", re.IGNORECASE), "дней"),
    (re.compile(r"^недел\w*$", re.IGNORECASE), "недель"),
    (re.compile(r"^месяц\w*$", re.IGNORECASE), "месяцев"),
    (re.compile(r"^(?:год\w*|лет)$", re.IGNORECASE), "лет"),
    (re.compile(r"^час\w*$", re.IGNORECASE), "часов"),
    (re.compile(r"^минут\w*$", re.IGNORECASE), "минут"),
)


_CALENDAR_YEAR_UNIT = re.compile(r"^год\w*$", re.IGNORECASE)
_CALENDAR_YEAR_RANGE = range(
    int(settings["agents.internal.norm_params.calendar_year_min"]),
    int(settings["agents.internal.norm_params.calendar_year_max_exclusive"]),
)


# исчерпывающий/закрытый перечень — ПОДСКАЗКА closure для атома
_CLOSURE = re.compile(r"исчерпыва\w*|не\s+подлеж\w*\s+расшир", re.IGNORECASE)


_CLOSURE_DENIED = re.compile(
    r"\bне\s+(?:(?:явля\w*|носит|нос\w*|счита\w*|призна\w*|был\w*|будет)\s+)?исчерпыва\w*",
    re.IGNORECASE,
)


_CLOSURE_DENIAL_WINDOW = int(settings["agents.internal.norm_params.closure_denial_window"])


def inherited_qualifier_context(
    leaf: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...] = (),
) -> str:
    "Структурный контекст листа для наследования квалификатора, ближайший — первым."
    parts: list[str] = [leaf.parent_intro or ""]
    for node in reversed(ancestors):
        if node.kind == "document":
            continue
        parts.append(node.parent_intro or "")
        parts.append(node.text or "")
    return "\n".join(part for part in parts if part.strip())


def _operator_is_negated(text: str, start: int) -> bool:
    "Примыкает ли частица отрицания к оператору, начинающемуся в ``start``."
    clause = CLAUSE_BOUNDARY.split(text[:start])[-1]
    words = LETTERS.findall(clause)
    return bool(words) and words[-1].casefold() in NEGATION_PARTICLES


def _asserted(pattern: re.Pattern[str], text: str) -> bool:
    """Есть ли в тексте НЕотрицаемое вхождение оператора."""
    return any(not _operator_is_negated(text, match.start()) for match in pattern.finditer(text))


def _denied(pattern: re.Pattern[str], text: str) -> bool:
    """Есть ли в тексте вхождение оператора, снятое примыкающим отрицанием."""
    return any(_operator_is_negated(text, match.start()) for match in pattern.finditer(text))


def detect_modality(text: str) -> tuple[Modality, Polarity] | None:
    "Деонтический оператор, который норма УТВЕРЖДАЕТ, либо ``None``."
    if _asserted(_PROHIBITION, text):
        return Modality.PROHIBITION, Polarity.NEGATIVE
    if _asserted(_RIGHT, text):
        return Modality.RIGHT, Polarity.POSITIVE
    if _asserted(_OBLIGATION_OPERATOR, text):
        return Modality.OBLIGATION, Polarity.POSITIVE

    if any(
        not _denied(_OBLIGATION_OPERATOR, clause) and _asserted(_OBLIGATION_PERFORMATIVE, clause)
        for clause in SENTENCE_SPLIT.split(text)
    ):
        return Modality.OBLIGATION, Polarity.POSITIVE
    if _ESTABLISH.search(text):
        return Modality.ESTABLISHMENT, Polarity.POSITIVE
    return None


def closure_is_asserted(text: str) -> bool:
    "Закрытость перечня, УТВЕРЖДЁННАЯ нормой."
    return any(
        not _CLOSURE_DENIED.search(text[max(0, match.start() - _CLOSURE_DENIAL_WINDOW) : match.end()])
        for match in _CLOSURE.finditer(text)
    )


def _qualifier(window: str) -> Qualifier | None:
    "Квалификатор, ЯВНО заявленный самой нормой у ЭТОЙ величины, либо ``None``."
    best: tuple[int, Qualifier] | None = None
    for pattern, qualifier in (
        (_QUAL_AT_LEAST, Qualifier.AT_LEAST),
        (_QUAL_AT_MOST, Qualifier.AT_MOST),
        (_QUAL_EXACT, Qualifier.EXACT),
    ):
        for match in pattern.finditer(window):
            if best is None or match.start() > best[0]:
                best = (match.start(), qualifier)
    return best[1] if best is not None else None


def _qualifier_window(text: str, value_start: int, previous_end: int) -> str:
    """Окно слева от величины: не дальше предыдущей величины и своего предложения."""
    start = max(previous_end, value_start - _QUALIFIER_WINDOW, 0)
    window = text[start:value_start]
    stops = list(QUALIFIER_STOP.finditer(window))
    return window[stops[-1].end() :] if stops else window


def _calendar_kind(matched: str) -> CalendarKind | None:
    "«рабочие» / «календарные» / None — из самой формулировки срока."
    lowered = matched.casefold()
    if "рабоч" in lowered:
        return CalendarKind.WORKING
    if "календарн" in lowered:
        return CalendarKind.CALENDAR
    return None


def _inherited_qualifier(context: str, parameter_type: ParameterType) -> Qualifier | None:
    "Квалификатор из вводной родителя — но только для СВОЕГО вида величины."
    noun = _QUALIFIER_INHERITANCE_NOUNS.get(parameter_type)
    if not context or noun is None:
        return None

    for line in context.split("\n"):
        for pattern, qualifier in ((_QUAL_AT_LEAST, Qualifier.AT_LEAST), (_QUAL_AT_MOST, Qualifier.AT_MOST)):
            for match in pattern.finditer(line):
                if noun.search(line[match.end() : match.end() + _QUALIFIER_NOUN_WINDOW]):
                    return qualifier
    return None


@dataclass(frozen=True)
class _Operand:
    """Одна граница: её собственные координаты, число и единица как в тексте."""

    value_start: int
    value_end: int
    value: str
    number: str

    unit_source: str

    qualifier: Qualifier | None


@dataclass(frozen=True)
class _NumericConstruction:
    """Числовая конструкция нормы: её полный спан и границы, которые она задаёт."""

    start: int
    end: int
    parameter_type: ParameterType
    operands: tuple[_Operand, ...]


def _duration_unit(value: str) -> str | None:
    """Единица срока в одной форме; ``None`` — форма не поддержана."""
    unit_word = value.rsplit(maxsplit=1)[-1]
    for pattern, canonical in _DURATION_UNIT_FORMS:
        if pattern.match(unit_word):
            return canonical
    return None


def _is_calendar_year(value: str, number: str) -> bool:
    """Обозначение года-даты, а не длительность в годах."""
    if not _CALENDAR_YEAR_UNIT.match(value.rsplit(maxsplit=1)[-1]):
        return False
    return number.isdigit() and int(number) in _CALENDAR_YEAR_RANGE


def _normalized_number(number: str) -> float | None:
    cleaned = number.replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _numeric_constructions(text: str) -> list[_NumericConstruction]:
    """Все распознанные конструкции; пересечения ещё НЕ разрешены."""
    constructions: list[_NumericConstruction] = []
    for parameter_type, pattern in _RANGE_PATTERNS:
        for match in pattern.finditer(text):
            if parameter_type is ParameterType.DURATION and _is_calendar_year(match.group("high"), match.group("high_num")):
                continue
            low_start, low_end = match.span("low")
            high_start, high_end = match.span("high")
            constructions.append(
                _NumericConstruction(
                    start=match.start(),
                    end=match.end(),
                    parameter_type=parameter_type,
                    operands=(
                        _Operand(
                            low_start,
                            low_end,
                            match.group("low"),
                            match.group("low"),
                            match.group("high"),
                            Qualifier.AT_LEAST,
                        ),
                        _Operand(
                            high_start,
                            high_end,
                            match.group("high"),
                            match.group("high_num"),
                            match.group("high"),
                            Qualifier.AT_MOST,
                        ),
                    ),
                )
            )
    for parameter_type, pattern in _PARAM_PATTERNS:
        for match in pattern.finditer(text):
            if parameter_type is ParameterType.DURATION and _is_calendar_year(match.group("value"), match.group("num")):
                continue
            value_start, value_end = match.span("value")
            constructions.append(
                _NumericConstruction(
                    start=match.start(),
                    end=match.end(),
                    parameter_type=parameter_type,
                    operands=(
                        _Operand(value_start, value_end, match.group("value"), match.group("num"), match.group("value"), None),
                    ),
                )
            )
    return constructions


def _resolved_constructions(text: str) -> list[_NumericConstruction]:
    "Пересекающиеся кандидаты разрешаются по конструкции, а не по порядку шаблонов."
    accepted: list[_NumericConstruction] = []
    for construction in sorted(_numeric_constructions(text), key=lambda item: (item.start, -(item.end - item.start))):
        if any(construction.start < item.end and item.start < construction.end for item in accepted):
            continue
        accepted.append(construction)
    return accepted


def extract_parameters(text: str, *, inherited_context: str | None = None) -> list[Parameter]:
    "Числовые параметры фрагмента для детерминированного ``NormFrame``."
    operands = sorted(
        (
            (construction.parameter_type, operand)
            for construction in _resolved_constructions(text)
            for operand in construction.operands
        ),
        key=lambda item: (item[1].value_start, item[1].value_end),
    )
    parameters: list[Parameter] = []
    previous_end = 0
    for parameter_type, operand in operands:
        unit = None
        calendar_kind = None
        if parameter_type is ParameterType.DURATION:
            unit = _duration_unit(operand.unit_source)
            calendar_kind = _calendar_kind(operand.unit_source)

        qualifier = (
            operand.qualifier
            or _qualifier(_qualifier_window(text, operand.value_start, previous_end))
            or _inherited_qualifier(inherited_context or "", parameter_type)
            or Qualifier.EXACT
        )
        parameters.append(
            Parameter(
                type=parameter_type,
                value=operand.value,
                unit=unit,
                calendar_kind=calendar_kind,
                qualifier=qualifier,
                norm_value=_normalized_number(operand.number),
                source_start=operand.value_start,
                source_end=operand.value_end,
            )
        )
        previous_end = operand.value_end
    return parameters
