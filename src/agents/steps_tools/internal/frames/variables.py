"""Переменные нормы, временные границы и собственные действия с доказательствами."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from src.agents.enums.internal.frames.norm_params import ParameterType
from src.agents.schemas.internal.frames.context_policy import ContextCandidate
from src.agents.schemas.internal.frames.norm_frame import (
    DurationConstraint,
    FrameProvenance,
    FrameSlot,
    RegulatedVariable,
    RegulatedVariableKind,
)
from src.agents.schemas.internal.frames.norm_params import Parameter
from src.agents.steps_tools.document.morphology import (
    ALNUM_LOWER,
    normalized_text,
)
from src.agents.steps_tools.internal.frames.context_policy import GOVERNING_CONTEXT_ROLES

_QUEUE = re.compile(r"очеред|ожидани", re.IGNORECASE)


_REGISTRY = re.compile(r"(?:реестр\w*|регистр\w*\s+запис\w*)", re.IGNORECASE)


_SYSTEM = re.compile(r"(?:информационн\w*|автоматизированн\w*)\s+систем\w*", re.IGNORECASE)


_SYSTEM_TARGET = re.compile(
    r"\b(?:ГИС|ИИС|АИС)\s+(?:[«\"][^»\"]+[»\"]|[A-Za-zА-Яа-яЁё0-9][\w.-]*(?:\s+[A-Za-zА-Яа-яЁё0-9][\w.-]*){0,4})(?:\s+[«\"][^»\"]+[»\"])?",
    re.IGNORECASE,
)


_SYSTEM_GENERIC_TARGET = re.compile(r"\bедин(?:ая|ую|ой|ой)\s+систем\w*", re.IGNORECASE)


_REGISTRATION = re.compile(r"\bрегистрац\w*", re.IGNORECASE)


_START_EVENT = re.compile(
    r"(?:со\s+дня|с\s+момента|с\s+даты)\s+([^.;\n]+)|дн[её]м\s+регистрации[^.;\n]{0,160}?\s+считается\s+([^.;\n]+)",
    re.IGNORECASE,
)


_START_EVENT_AFTER_VALUE = re.compile(
    r"^[^.;\n]{0,40}?(?:со\s+дня|с\s+момента|с\s+даты)\s+([^.;,\n]+)",
    re.IGNORECASE,
)


_COMPLETION_EVENT = re.compile(
    r"(?:до\s+(?:дня|момента)\s+|заканчивается\s+)([^.;\n]+)",
    re.IGNORECASE,
)


_RESULT_DOCUMENT = re.compile(
    r"(?:выда|направлен|предоставлен|получен)\w*.{0,80}?"
    r"(дубликат\w*|удостоверени\w*|справк\w*|свидетельств\w*|документ\w*)",
    re.IGNORECASE | re.DOTALL,
)


_SUBMISSION_CHANNEL_CLAUSE = re.compile(
    r"(?P<action>(?:не)?представ\w*)\s+(?P<object>документ\w*)"
    r"[^.;]{0,120}?\b(?P<channel>лично|самостоятельно)\b",
    re.IGNORECASE,
)


_ACTION = re.compile(
    r"\b(выда\w*|направ\w*|предостав\w*|регистр\w*|внос\w*|измен\w*|"
    r"исправ\w*|принима\w*|рассматрива\w*|ожида\w*)",
    re.IGNORECASE,
)


def frame_slot(value: str | None, *, provenance: FrameProvenance, evidence_ids: Iterable[str], confidence: float) -> FrameSlot:
    raw = (value or "").strip()
    if not raw:
        return FrameSlot()
    return FrameSlot(
        raw_value=raw,
        normalized_value=normalized_text(raw),
        confidence=confidence,
        provenance=provenance,
        evidence_span_ids=list(dict.fromkeys(evidence_ids)),
    )


def first_captured_value(pattern: re.Pattern[str], texts: Iterable[str]) -> str | None:
    for text in texts:
        match = pattern.search(text)
        if match:
            return " ".join(match.group(1).split())
    return None


def _single_captured_value(pattern: re.Pattern[str], text: str) -> tuple[str | None, bool]:
    """Bind a temporal event only when all grounded captures agree exactly."""
    values = [" ".join(value.split()) for match in pattern.finditer(text) for value in match.groups() if value and value.strip()]
    distinct = list(dict.fromkeys(normalized_text(value) for value in values))
    return (values[0] if len(distinct) == 1 else None), bool(values)


def _parameter_position(text: str, parameter: Parameter, cursor: int) -> int:
    "Позиция границы в ТОМ ЖЕ тексте, из которого её извлекли, либо ``-1``."
    value = str(parameter.value or "")
    if not value:
        return -1
    start, end = parameter.source_start, parameter.source_end
    if start is not None and end is not None and start >= cursor and text[start:end] == value:
        return start
    return text.find(value, cursor)


def _start_event_value(value: str | None) -> str | None:
    "Событие-начало без хвостовой структурной пунктуации."
    trimmed = (value or "").strip().rstrip(":;,-–— ")
    return trimmed or None


def _duration_constraints(parameter_text: str, parameters: list[Parameter], evidence_ids: list[str]) -> list[DurationConstraint]:
    "ВСЕ временнЫе границы нормы, каждая со своим событием-началом."
    duration_parameters = [parameter for parameter in parameters if parameter.type is ParameterType.DURATION]
    positions: list[int] = []
    cursor = 0
    for parameter in duration_parameters:
        position = _parameter_position(parameter_text, parameter, cursor)
        if position >= 0:
            cursor = position + len(str(parameter.value or ""))
        positions.append(position)

    constraints: list[DurationConstraint] = []
    for index, parameter in enumerate(duration_parameters):
        value = str(parameter.value or "")
        position = positions[index]
        next_position = next((item for item in positions[index + 1 :] if item >= 0), len(parameter_text))
        tail = parameter_text[position + len(value) : next_position] if position >= 0 else ""
        match = _START_EVENT_AFTER_VALUE.match(tail) if tail else None
        start_event = frame_slot(
            _start_event_value(" ".join(match.group(1).split())) if match else None,
            provenance=FrameProvenance.LEXICAL,
            evidence_ids=evidence_ids,
            confidence=0.9,
        )
        constraints.append(
            DurationConstraint(
                value=value or None,
                normalized_value=parameter.norm_value,
                unit=parameter.unit,
                calendar_kind=parameter.calendar_kind,
                qualifier=parameter.qualifier.value,
                start_event=start_event if start_event.raw_value else None,
                source_start=parameter.source_start,
                source_end=parameter.source_end,
                evidence_span_ids=list(evidence_ids),
            )
        )
    return constraints


_PARAMETER_DERIVED_KINDS = frozenset(
    {
        RegulatedVariableKind.DURATION,
        RegulatedVariableKind.MONETARY_AMOUNT,
        RegulatedVariableKind.SHARE,
        RegulatedVariableKind.AGE,
        RegulatedVariableKind.AREA,
    }
)


def regulated_variable(
    text: str,
    parameters: list[Parameter],
    evidence_ids: list[str],
    *,
    parameter_text: str | None = None,
    governing_grounds_span_ids: list[str] | None = None,
) -> RegulatedVariable:
    kind = RegulatedVariableKind.UNKNOWN
    governing_grounds_span_ids = governing_grounds_span_ids or []
    target_system: FrameSlot | None = None
    duration_parameters = [item for item in parameters if item.type is ParameterType.DURATION]

    parameter = duration_parameters[0] if duration_parameters else (parameters[0] if parameters else None)

    duration_constraints = _duration_constraints(parameter_text if parameter_text is not None else text, parameters, evidence_ids)
    duration = (
        frame_slot(
            str(parameter.value),
            provenance=FrameProvenance.LEXICAL,
            evidence_ids=evidence_ids,
            confidence=0.9,
        )
        if parameter is not None and parameter.type is ParameterType.DURATION and parameter.value is not None
        else None
    )
    start_value, has_start_event = _single_captured_value(_START_EVENT, text)
    start_event = frame_slot(
        _start_event_value(start_value), provenance=FrameProvenance.LEXICAL, evidence_ids=evidence_ids, confidence=0.9
    )
    completion_value, has_completion_event = _single_captured_value(_COMPLETION_EVENT, text)
    completion_event = frame_slot(
        completion_value,
        provenance=FrameProvenance.LEXICAL,
        evidence_ids=evidence_ids,
        confidence=0.9,
    )
    named_matches = list(_SYSTEM_TARGET.finditer(text))

    system_values = [
        match.group(0)
        for match in named_matches
        if not any(
            other is not match and other.start() <= match.start() and match.end() <= other.end() for other in named_matches
        )
    ]
    system_values.extend(match.group(0) for match in _SYSTEM_GENERIC_TARGET.finditer(text))

    system_values = [
        value
        for value in system_values
        if not any(
            set(ALNUM_LOWER.findall(value.casefold())) < set(ALNUM_LOWER.findall(other.casefold()))
            for other in system_values
            if other != value
        )
    ]
    system_by_normalized = dict.fromkeys(" ".join(value.casefold().split()) for value in system_values)
    distinct_systems = list(system_by_normalized)
    distinct_system_values = [
        next(value for value in system_values if " ".join(value.casefold().split()) == normalized)
        for normalized in distinct_systems
    ]
    target_system = frame_slot(
        distinct_system_values[0] if len(distinct_systems) == 1 else None,
        provenance=FrameProvenance.LEXICAL,
        evidence_ids=evidence_ids,
        confidence=0.9,
    )
    target_system_candidates = (
        [
            frame_slot(value, provenance=FrameProvenance.LEXICAL, evidence_ids=evidence_ids, confidence=0.9)
            for value in distinct_system_values
        ]
        if len(distinct_systems) > 1
        else []
    )
    registry_match = _REGISTRY.search(text)
    registry_target = frame_slot(
        registry_match.group(0) if registry_match else None,
        provenance=FrameProvenance.LEXICAL,
        evidence_ids=evidence_ids,
        confidence=0.9,
    )
    if target_system.raw_value or target_system_candidates:
        kind = RegulatedVariableKind.INFORMATION_SYSTEM_TARGET
    elif _QUEUE.search(text):
        kind = RegulatedVariableKind.QUEUE_WAITING_TIME
    elif registry_match:
        kind = RegulatedVariableKind.REGISTRY_TARGET
    elif duration_parameters:
        kind = RegulatedVariableKind.DURATION
    elif not parameters and governing_grounds_span_ids:
        kind = RegulatedVariableKind.RESULT_KIND
        return RegulatedVariable(
            kind=kind,
            value=None,
            normalized_value=None,
            confidence=0.9,
            provenance=FrameProvenance.TREE,
            evidence_span_ids=list(dict.fromkeys([*evidence_ids, *governing_grounds_span_ids])),
        )
    elif submission := _SUBMISSION_CHANNEL_CLAUSE.search(parameter_text if parameter_text is not None else text):
        kind = RegulatedVariableKind.SUBMISSION_CHANNEL
        return RegulatedVariable(
            kind=kind,
            value=submission.group("channel"),
            normalized_value=normalized_text(submission.group("channel")),
            confidence=0.9,
            provenance=FrameProvenance.LEXICAL,
            evidence_span_ids=evidence_ids,
        )
    elif match := _RESULT_DOCUMENT.search(text):
        kind = RegulatedVariableKind.DOCUMENT_RESULT
        return RegulatedVariable(
            kind=kind,
            value=match.group(1),
            normalized_value=normalized_text(match.group(1)),
            confidence=0.9,
            provenance=FrameProvenance.LEXICAL,
            evidence_span_ids=evidence_ids,
        )
    elif match := _SYSTEM.search(text):
        kind = RegulatedVariableKind.INFORMATION_SYSTEM_TARGET
        target_system = (
            target_system
            if target_system.raw_value
            else frame_slot(match.group(0), provenance=FrameProvenance.LEXICAL, evidence_ids=evidence_ids, confidence=0.9)
        )
    elif parameters:
        kind_map = {
            ParameterType.DURATION: RegulatedVariableKind.DURATION,
            ParameterType.MONEY: RegulatedVariableKind.MONETARY_AMOUNT,
            ParameterType.SHARE: RegulatedVariableKind.SHARE,
            ParameterType.AGE: RegulatedVariableKind.AGE,
        }
        kind = kind_map[parameters[0].type]
    elif has_completion_event:
        kind = RegulatedVariableKind.REGISTRATION_COMPLETION_EVENT
    elif has_start_event:
        kind = RegulatedVariableKind.REGISTRATION_START_EVENT
    if kind == RegulatedVariableKind.UNKNOWN:
        return RegulatedVariable()

    value_parameter = parameter if kind in _PARAMETER_DERIVED_KINDS else None
    return RegulatedVariable(
        kind=kind,
        value=value_parameter.value if value_parameter else None,
        normalized_value=value_parameter.norm_value if value_parameter else None,
        unit=value_parameter.unit if value_parameter else None,
        calendar_kind=value_parameter.calendar_kind if value_parameter else None,
        duration_constraints=duration_constraints,
        qualifier=value_parameter.qualifier.value if value_parameter else None,
        target_system=target_system,
        target_system_candidates=target_system_candidates,
        duration=duration,
        registry_target=registry_target,
        start_event=start_event,
        completion_event=completion_event,
        confidence=0.9,
        provenance=FrameProvenance.LEXICAL,
        evidence_span_ids=evidence_ids,
    )


def inherit_duration_start_event(variable: RegulatedVariable, accepted: list[ContextCandidate]) -> RegulatedVariable:
    "Bind a missing duration endpoint to one exact governing context value."
    if variable.kind is not RegulatedVariableKind.DURATION or not variable.duration_constraints:
        return variable
    if all(item.start_event is not None for item in variable.duration_constraints):
        return variable
    candidate_events: list[tuple[str, str]] = []
    for span in (item.span for item in accepted if item.role in GOVERNING_CONTEXT_ROLES):
        value, mentioned = _single_captured_value(_START_EVENT, span.text)
        if mentioned and value:
            candidate_events.append((value, span.span_id))
    normalized = {normalized_text(_start_event_value(value)) for value, _span_id in candidate_events}
    if len(normalized) != 1:
        return variable
    value = _start_event_value(candidate_events[0][0])
    span_ids = list(dict.fromkeys(span_id for _value, span_id in candidate_events))
    inherited = frame_slot(value, provenance=FrameProvenance.TREE, evidence_ids=span_ids, confidence=0.9)
    constraints = [
        item if item.start_event is not None else item.model_copy(update={"start_event": inherited})
        for item in variable.duration_constraints
    ]
    return variable.model_copy(
        update={
            "start_event": (
                variable.start_event if variable.start_event is not None and variable.start_event.raw_value else inherited
            ),
            "duration_constraints": constraints,
            "evidence_span_ids": list(dict.fromkeys([*variable.evidence_span_ids, *span_ids])),
        }
    )


@dataclass(frozen=True)
class _ActionObject:
    "Действие/объект нормы вместе с ответом, ДОКАЗАНА ли их роль."

    action: str | None = None
    object: str | None = None
    proven: bool = True


def _event_reference_regions(text: str) -> list[tuple[int, int]]:
    "Участки, где норма ССЫЛАЕТСЯ на чужое событие, а не совершает действие."
    return [(match.start(), match.end()) for pattern in (_START_EVENT, _COMPLETION_EVENT) for match in pattern.finditer(text)]


def _outside_event_reference(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    """Первое вхождение подсказки ВНЕ ссылки на событие, иначе ``None``."""
    regions = _event_reference_regions(text)
    return next(
        (
            match
            for match in pattern.finditer(text)
            if not any(start <= match.start() and match.end() <= end for start, end in regions)
        ),
        None,
    )


def identify_action_object(text: str, variable: RegulatedVariable) -> _ActionObject:

    if _outside_event_reference(_REGISTRATION, text) is not None:
        return _ActionObject("регистрация", "заявление")
    if variable.kind == RegulatedVariableKind.QUEUE_WAITING_TIME:
        return _ActionObject("ожидание", "очередь")

    if variable.kind == RegulatedVariableKind.REGISTRY_TARGET and (registry := _REGISTRY.search(text)) is not None:
        return _ActionObject(_action_cue(text) or "внесение записи", registry.group(0))
    if variable.kind == RegulatedVariableKind.DOCUMENT_RESULT and (match := _RESULT_DOCUMENT.search(text)) is not None:
        return _ActionObject(_action_cue(text) or "выдача", match.group(1))
    if variable.kind in {RegulatedVariableKind.SUBMISSION_CHANNEL, RegulatedVariableKind.SUBMISSION_FORM}:
        match = _SUBMISSION_CHANNEL_CLAUSE.search(text)
        if match is not None:
            return _ActionObject(match.group("action"), match.group("object"))
    own_action = _action_cue(text)
    if own_action is not None:
        return _ActionObject(own_action, None)

    borrowed = first_captured_value(_ACTION, (text,))
    return _ActionObject(borrowed, None, proven=borrowed is None)


def _action_cue(text: str) -> str | None:
    """Глагольная подсказка действия САМОЙ нормы (вне ссылок на чужие события)."""
    match = _outside_event_reference(_ACTION, text)
    return " ".join(match.group(1).split()) if match else None
