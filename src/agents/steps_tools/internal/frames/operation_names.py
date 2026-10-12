"""Названия операций подуслуг: синтаксические границы и доказанность применимости."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

from src.agents.steps_tools.document.morphology import (
    AMBIGUOUS_VERB_TAIL,
    ATTRIBUTIVE_OBJECT_TAIL,
    ATTRIBUTIVE_OBLIQUE_PLURAL,
    COORDINATORS,
    FINITE_VERB_TAIL,
    LETTERS,
    NEGATION_PARTICLE,
    NOMINAL_TAIL,
    PREDICATE_TAIL,
    PREPOSITIONS,
    WORD,
    ends_with_vowel,
    normalized_text,
)
from src.agents.steps_tools.document.norm_fragments import sentence_spans
from src.common.config.config import settings

_OPERATION_HEAD_STEMS: tuple[str, ...] = (
    "проведени",
    "выдач",
    "получени",
    "исправлени",
    "регистраци",
    "переоформлени",
    "предоставлени",
    "назначени",
    "оформлени",
    "рассмотрени",
    "продлени",
    "прекращени",
    "приостановлени",
    "внесени",
    "заключени",
    "постановк",
    "принят",
)


_OPERATION_HEAD = r"(?:" + "|".join(_OPERATION_HEAD_STEMS) + r")"


_SUBSERVICE_PART = re.compile(rf"\bв\s+части\s+({_OPERATION_HEAD}\w*)", re.IGNORECASE)


_OPERATION_HEAD_WORD = re.compile(rf"^{_OPERATION_HEAD}\w*$", re.IGNORECASE)


_TOKEN = re.compile(rf"(?P<word>{WORD.pattern})|(?P<number>\d+)|(?P<punct>[^\s\w])")


_NEGATED_APPLICABILITY_AUXILIARY = frozenset({"может", "могут", "должен", "должна", "должно", "должны"})


_APPLICABILITY_INFINITIVE = re.compile(r"(?:примен|распростран|относ)и?\w*(?:ть|ться)$", re.IGNORECASE)


_NAME_PREPOSITION = frozenset({"в", "во", "на", "о", "об", "обо", "для", "из", "от", "без", "взамен"})


_EXCLUSION_CUE = frozenset({"кроме", "исключением", "исключая", "помимо", "вместо"})


_ATTRIBUTIVE_MIN_LENGTH = int(settings["agents.internal.project_frames.attributive_min_length"])


_STRUCTURAL_DELIMITER = frozenset({":", ";", ".", "!", "?", "-", "–", "—"})


@dataclass(frozen=True)
class _PartOperation:
    "Ответ на ТРИ разных вопроса о конструкции «в части <операция>»."

    value: str
    proven: bool
    reason: str = ""


def _sentence_bounds(text: str, position: int) -> tuple[int, int]:
    "Границы предложения вокруг позиции (по общему разбору предложений ``sentence_spans``); завершающий знак в предложение"
    start, end = 0, len(text)
    for span_start, span_end in sentence_spans(text):
        if span_start <= position < span_end:
            start, end = span_start, span_end
            break
    if end > start and text[end - 1] in ".!?":
        end -= 1
    return start, end


def _negated_predicate(fragment: str) -> bool:
    "«не» вместе с личной формой глагола либо явное изъятие."
    words = [match.group().casefold() for match in LETTERS.finditer(fragment)]
    if set(words) & _EXCLUSION_CUE:
        return True
    for index, word in enumerate(words):
        if word != NEGATION_PARTICLE or index + 1 >= len(words):
            continue
        predicate = words[index + 1]
        if PREDICATE_TAIL.search(predicate):
            return True
        if predicate in _NEGATED_APPLICABILITY_AUXILIARY and any(
            _APPLICABILITY_INFINITIVE.search(candidate) for candidate in words[index + 2 : index + 5]
        ):
            return True
    return False


def _applicability_is_negated(text: str, head_start: int, name_end: int, boundary: str) -> bool:
    "Относится ли отрицание применимости к САМОЙ конструкции «в части X»."
    sentence_start, sentence_end = _sentence_bounds(text, head_start)
    before = text[sentence_start:head_start]
    before = before.rsplit(";", 1)[-1]
    if _negated_predicate(before):
        return True
    if boundary in {":", ";"}:
        return False
    after = text[name_end:sentence_end].split(";", 1)[0]
    return _negated_predicate(after)


def _next_word(text: str, position: int) -> str | None:
    match = _TOKEN.search(text, position)
    while match is not None and match.lastgroup != "word":
        match = _TOKEN.search(text, match.end())
    return match.group().casefold() if match is not None else None


def _bears_externally_assigned_case(word: str) -> bool:
    "Может ли словоформа быть именной в падеже, назначенном ИЗВНЕ."
    return len(word) >= 3 and not ends_with_vowel(word) and word.casefold() not in PREPOSITIONS


def _is_attributive_oblique_plural(word: str) -> bool:
    """Стоит ли слово в косвенном падеже МНОЖЕСТВЕННОГО числа адъективного склонения."""
    return len(word) >= _ATTRIBUTIVE_MIN_LENGTH and ATTRIBUTIVE_OBLIQUE_PLURAL.search(word) is not None


def _syntax_assigns_the_case(word: str, action: str, *, governed_slot: bool, attributive_before: bool) -> bool:
    "Назначает ли падеж этой словоформы синтаксис, а не её собственное окончание."
    if not _bears_externally_assigned_case(word):
        return False
    if attributive_before:
        return action in {"skip", "ambiguous"}
    return governed_slot and action == "skip"


@dataclass(frozen=True)
class _ChainState:
    "Что предыдущий разобранный токен оставляет следующему."

    governed_slot: bool = False
    attributive_before: bool = False
    awaiting_coordinated_head: bool = False


def _advance_chain(state: _ChainState, word: str, action: str) -> _ChainState:
    """Состояние после разбора одного слова."""
    second_head = state.awaiting_coordinated_head and action == "accept" and _OPERATION_HEAD_WORD.match(word) is not None
    return _ChainState(
        governed_slot=second_head,
        attributive_before=action == "accept" and _is_attributive_oblique_plural(word),
        awaiting_coordinated_head=state.awaiting_coordinated_head and not second_head,
    )


def _coordinates_operation_heads(text: str, position: int, action: str, *, governed_slot: bool) -> bool:
    "Соединяет ли союз две ГОЛОВЫ операции, а не два объекта."
    if action != "enumeration" or not governed_slot:
        return False
    return _OPERATION_HEAD_WORD.match(_next_word(text, position) or "") is not None


def _word_action(lowered: str, *, expects_object: bool) -> str:
    "Роль слова в родительном обороте названия — по ПОДДЕРЖАННОМУ набору."
    if lowered in COORDINATORS:
        return "enumeration"
    if lowered == NEGATION_PARTICLE or lowered in _EXCLUSION_CUE or FINITE_VERB_TAIL.search(lowered):
        return "stop"
    if expects_object:
        return "accept"
    if lowered in _NAME_PREPOSITION:
        return "preposition"
    if NOMINAL_TAIL.search(lowered):
        return "accept"
    if AMBIGUOUS_VERB_TAIL.search(lowered):
        return "ambiguous"
    return "skip"


def _operation_name(text: str, head: re.Match[str]) -> _PartOperation:
    "Название операции, полнота его границ и принадлежность области нормы."
    start = head.start(1)
    position = head.end(1)
    value_end = head.end(1)
    expects_object = False
    frozen = False
    boundary = ""
    reason = ""
    proven_boundary = False

    chain = _ChainState(governed_slot=bool(NOMINAL_TAIL.search(head.group(1).casefold())))
    while (token := _TOKEN.search(text, position)) is not None:
        position = token.end()
        if token.lastgroup == "number":
            reason = reason or "clause_started"
            break
        if token.lastgroup == "punct":
            symbol = token.group()
            chain = _ChainState()
            if symbol in _STRUCTURAL_DELIMITER:
                proven_boundary, boundary, value_end = True, symbol, token.start()
                break

            reason = reason or ("comma_ambiguous" if symbol == "," else "unsupported_construction")
            frozen = frozen or symbol != ","
            continue
        lowered = token.group().casefold()
        if "\n" in text[value_end : token.start()] and not NOMINAL_TAIL.search(lowered):
            reason = reason or "line_break_boundary"
            break
        action = _word_action(lowered, expects_object=expects_object)
        if _syntax_assigns_the_case(
            lowered, action, governed_slot=chain.governed_slot, attributive_before=chain.attributive_before
        ):
            action = "accept"
        elif _coordinates_operation_heads(text, position, action, governed_slot=chain.governed_slot):
            action, chain = "accept", replace(chain, awaiting_coordinated_head=True)
        chain = _advance_chain(chain, lowered, action)
        if action == "stop":
            reason = reason or "clause_started"
            break
        if action == "enumeration":
            reason = reason or "enumeration"
            continue
        if action == "skip":
            reason = reason or "unsupported_grammar"
            continue
        if action == "ambiguous":
            reason = reason or "ambiguous_grammar"

        expects_object = action == "preposition" or (
            expects_object and action == "accept" and ATTRIBUTIVE_OBJECT_TAIL.search(lowered) is not None
        )
        value_end = value_end if frozen else token.end()
    else:
        proven_boundary = not expects_object
        value_end = len(text)
    return _part_operation(
        text,
        start,
        value_end,
        boundary,
        reason=reason if not expects_object else reason or "unsupported_grammar",
        proven_boundary=proven_boundary,
    )


def _part_operation(
    text: str,
    start: int,
    value_end: int,
    boundary: str,
    *,
    reason: str,
    proven_boundary: bool,
) -> _PartOperation:
    """Итог разбора: текст подсказки, доказанность границ и первая причина отказа."""
    value = " ".join(text[start:value_end].split())
    if not proven_boundary:
        reason = reason or "boundary_unproven"
    elif _applicability_is_negated(text, start, value_end, boundary):
        reason = "applicability_negated"
    return _PartOperation(value=value, proven=proven_boundary and not reason, reason=reason)


def _part_operations(text: str, *, within: int | None = None) -> list[_PartOperation]:
    "Все конструкции «в части <операция>», головы которых стоят во вводной."
    operations: list[_PartOperation] = []
    for match in _SUBSERVICE_PART.finditer(text):
        if within is not None and match.start() >= within:
            continue

        operations.append(_operation_name(text, match))
    return operations


def resolve_subservice(scopes: Iterable[Iterable[tuple[str, int | None]]]) -> _PartOperation | None:
    "Подуслуга из конструкции «в части <операция>», вводящей норму."
    for scope in scopes:
        found = [operation for text, within in scope if text for operation in _part_operations(text, within=within)]
        distinct = list(dict.fromkeys(normalized_text(item.value) for item in found))
        if len(distinct) == 1:
            return _PartOperation(found[0].value, all(item.proven for item in found))
        if found:
            return None
    return None
