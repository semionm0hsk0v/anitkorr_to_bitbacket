from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from src.agents.enums.internal.detector import InternalCheck
from src.agents.enums.internal.frames.norm_params import ParameterType, Qualifier
from src.agents.schemas.internal.checks.time_limits import (
    ExactTimeLimitSpanV1,
    TimeLimitConsistencyEvidenceV1,
    TimeLimitConstraintEvidenceV1,
    TimeLimitHit,
)
from src.agents.schemas.internal.findings import DetectorFinding
from src.agents.schemas.internal.frames.norm_frame import (
    WHOLE_SERVICE_DEADLINE_SCOPE,
    DurationConstraint,
    EvidenceSpan,
    GroundingStatus,
)
from src.agents.schemas.internal.frames.project_frames import BuiltProjectFrame
from src.agents.steps_tools.document.locus_labels import humanize_locus
from src.agents.steps_tools.document.morphology import (
    ALNUM,
    GENITIVE_ATTRIBUTE_TAIL,
    LETTERS,
    WORD,
    canonical_token,
    canonical_tokens,
    first_clause,
    normalized_text,
)
from src.agents.steps_tools.document.segmentation import UNLABELED_PROVISION
from src.agents.steps_tools.internal.findings import detector_finding
from src.agents.steps_tools.internal.frames.general_overlap import explicit_premises, unresolved_applicability_markers
from src.agents.steps_tools.internal.frames.norm_params import extract_parameters
from src.agents.steps_tools.internal.frames.service_stages import (
    CLAUSE_NUMBER_PREFIX,
    DEADLINE_HEAD,
    NESTED_STAGE_NAMES,
    STAGE_CONSTITUTING_CONSTRUCTIONS,
    WHOLE_SERVICE_HEAD,
    stage_noun,
)
from src.common.config.config import settings

#: Порядок опроса операнда одной операции: подуслуга сильнее услуги, услуга — меры.
_OPERATION_SLOTS: tuple[Literal["subservice", "service", "measure"], ...] = ("subservice", "service", "measure")

#: Слот, из которого берутся операнды операции для каждого основания тождества.
_OPERATION_SLOT_BY_BASIS: dict[str, Literal["subservice", "service", "measure"]] = {
    "subservice": "subservice",
    "service": "service",
    "measure": "measure",
    "subservice_prefix": "subservice",
}


_MIN_SHARED_PREFIX_TOKENS = int(settings["agents.internal.time_limit.min_shared_prefix_tokens"])


def _canonical_start_event(value: str) -> str | None:
    "Событие-начало целиком: регистр и пробелы приведены, СОСТАВ сохранён."
    normalized = normalized_text(value)
    return normalized or None


def _shared_start_event(left: str, right: str) -> str | None:
    "Событие, названное ОБЕИМИ сторонами, либо ``None``, если тождество не доказано."
    left_tokens, right_tokens = canonical_tokens(left), canonical_tokens(right)
    if not left_tokens or not right_tokens or left_tokens[0] != right_tokens[0]:
        return None
    if normalized_text(left) == normalized_text(right):
        return normalized_text(left)
    narrow, wide = (left, right) if len(left_tokens) <= len(right_tokens) else (right, left)
    narrow_tokens, wide_tokens = canonical_tokens(narrow), canonical_tokens(wide)
    if not set(narrow_tokens) <= set(wide_tokens):
        return None
    if not _wide_name_declares_the_short_one(wide_tokens, frozenset(narrow_tokens)):
        return None
    return normalized_text(narrow)


#: Основа слова «далее» — им нормативный акт вводит сокращение («(далее – заявление)»).
_ABBREVIATION_MARKER = canonical_token("далее")


def _wide_name_declares_the_short_one(wide_tokens: tuple[str, ...], narrow_tokens: frozenset[str]) -> bool:
    """Полное название события само вводит короткое оборотом «далее – …»."""
    return any(
        token == _ABBREVIATION_MARKER and index + 1 < len(wide_tokens) and wide_tokens[index + 1] in narrow_tokens
        for index, token in enumerate(wide_tokens)
    )


def _with_start_event(constraint: TimeLimitConstraintEvidenceV1, event: str) -> TimeLimitConstraintEvidenceV1:
    """Граница с ДОКАЗАННЫМ общим названием события вместо собственного."""
    if constraint.start_event == event:
        return constraint
    return constraint.model_copy(update={"start_event": event})


def _spans_by_id(built: BuiltProjectFrame) -> dict[str, EvidenceSpan]:
    spans = {span.span_id: span for span in built.frame.evidence.context_spans}
    primary = built.frame.evidence.primary_span
    if primary is not None:
        spans[primary.span_id] = primary
    return spans


def _exact_span(
    built: BuiltProjectFrame,
    span: EvidenceSpan,
    role: Literal["whole_limit", "stage_limit", "whole_scope", "stage_scope", "whole_operation", "stage_operation"],
    document_text: str,
) -> ExactTimeLimitSpanV1 | None:
    """Text-free локатор операнда; пересчитывается по каноническому тексту документа."""
    if span.grounding_status is not GroundingStatus.EXACT or span.char_start is None or span.char_end is None:
        return None
    exact = document_text[span.char_start : span.char_end]
    if not exact or exact != span.text:
        return None
    primary = built.frame.evidence.primary_span
    is_own_span = primary is not None and span.span_id == primary.span_id
    if not is_own_span and span.span_id not in {block.span_id for block in built.frame.evidence.context_blocks}:
        return None
    tree_ids = [item.element_id for item in built.frame.tree_path]
    if span.source_id not in tree_ids:
        return None
    return ExactTimeLimitSpanV1(
        role=role,
        span_id=span.span_id,
        source_id=span.source_id,
        char_start=span.char_start,
        char_end=span.char_end,
        tree_path_element_ids=tree_ids[: tree_ids.index(span.source_id) + 1],
    )


def _operand_span(
    built: BuiltProjectFrame,
    slot_name: str,
    role: Literal["whole_scope", "stage_scope", "whole_operation", "stage_operation"],
    document_text: str,
) -> ExactTimeLimitSpanV1 | None:
    "Exact-координаты спана, которым производитель подтвердил ДОКАЗАННЫЙ слот."
    slot = getattr(built.frame, _OPERATION_SLOT_BY_BASIS.get(slot_name, slot_name), None)
    if slot is None or not slot.role_is_proven():
        return None
    spans = _spans_by_id(built)
    for span_id in slot.evidence_span_ids:
        span = spans.get(span_id)
        if span is None:
            continue
        exact = _exact_span(built, span, role, document_text)
        if exact is not None:
            return exact
    return None


def _constraint(
    built: BuiltProjectFrame,
    constraint: DurationConstraint,
    *,
    role: Literal["whole_limit", "stage_limit"],
    document_text: str,
) -> TimeLimitConstraintEvidenceV1 | None:
    """Верхняя граница, пригодная как операнд: значение, единица, тип и своё событие."""
    primary = built.frame.evidence.primary_span
    if primary is None:
        return None
    event = constraint.start_event
    canonical_event = _canonical_start_event(event.normalized_value or event.raw_value or "") if event is not None else None
    if (
        constraint.normalized_value is None
        or not constraint.unit
        or not constraint.calendar_kind
        or constraint.qualifier != Qualifier.AT_MOST
        or canonical_event is None
        or event is None
        or not event.role_is_proven()
    ):
        return None
    span = _exact_span(built, primary, role, document_text)
    if span is None:
        return None
    return TimeLimitConstraintEvidenceV1(
        normalized_value=float(constraint.normalized_value),
        unit=normalized_text(constraint.unit),
        calendar_kind=normalized_text(constraint.calendar_kind),
        qualifier=Qualifier.AT_MOST,
        start_event=canonical_event,
        span=span,
    )


def _deadline_scope(built: BuiltProjectFrame) -> str | None:
    slot = built.frame.deadline_scope
    return slot.proven_value() if slot is not None else None


def _operand_texts(built: BuiltProjectFrame, document_text: str) -> tuple[str, ...]:
    "Тексты всех спанов, из которых норма берёт свои операнды."
    return tuple(
        document_text[span.char_start : span.char_end]
        for span in _spans_by_id(built).values()
        if span.char_start is not None and span.char_end is not None
    )


def _premises(built: BuiltProjectFrame, document_text: str) -> frozenset[tuple[str, str]]:
    "Явные предпосылки применимости, объявленные текстом нормы и её вводных конструкций."
    found: set[tuple[str, str]] = set()
    for text in _operand_texts(built, document_text):
        found |= explicit_premises(text)
    return frozenset(found)


def _unparsed_conditions(built: BuiltProjectFrame, document_text: str) -> tuple[str, ...]:
    """Признаки условности нормы, которые не удалось разобрать в предпосылку."""
    return tuple(marker for text in _operand_texts(built, document_text) for marker in unresolved_applicability_markers(text))


def _service_tokens(*frames: BuiltProjectFrame) -> frozenset[str]:
    "Основы слов ДОКАЗАННОГО названия услуги — родовой словарь самого проекта."
    tokens: set[str] = set()
    for built in frames:
        value = built.frame.service.proven_value()
        if value:
            tokens |= set(canonical_tokens(value))
    return frozenset(tokens)


OperationBasis = Literal["subservice", "service", "measure", "subservice_prefix"]


OperationInventories = Mapping[str, frozenset[str]]


def _operation_tokens(value: str) -> tuple[str, ...]:
    return tuple(WORD.findall(normalized_text(value)))


def _starts_with(value: str, prefix: tuple[str, ...]) -> bool:
    return _operation_tokens(value)[: len(prefix)] == prefix


def _shared_operation_prefix(
    whole_value: str,
    stage_value: str,
    *,
    whole_inventory: frozenset[str],
    stage_inventory: frozenset[str],
    service_tokens: frozenset[str],
) -> str | None:
    "Общее начало двух РАЗНЫХ названий одной части, либо ``None``."
    whole_tokens = _operation_tokens(whole_value)
    stage_tokens = _operation_tokens(stage_value)
    shared: list[str] = []
    for left, right in zip(whole_tokens, stage_tokens, strict=False):
        if left != right:
            break
        shared.append(left)
    prefix = tuple(shared)
    if len(prefix) < _MIN_SHARED_PREFIX_TOKENS or GENITIVE_ATTRIBUTE_TAIL.search(prefix[-1]):
        return None
    if sum(1 for item in whole_inventory if _starts_with(item, prefix)) != 1:
        return None
    if sum(1 for item in stage_inventory if _starts_with(item, prefix)) != 1:
        return None
    if not _divergent_tails_name_one_part(whole_tokens[len(prefix) :], stage_tokens[len(prefix) :], service_tokens):
        return None
    return " ".join(prefix)


def _divergent_tails_name_one_part(
    whole_tail: tuple[str, ...],
    stage_tail: tuple[str, ...],
    service_tokens: frozenset[str],
) -> bool:
    "Расходящиеся хвосты двух названий говорят об одной части, либо нет."
    if not whole_tail or not stage_tail:
        return True
    return any(_tail_names_the_service(tail, service_tokens) for tail in (whole_tail, stage_tail))


#: Основа слова «услуга»: обозначение услуги как таковой, а не её видового признака.
_SERVICE_HEAD = canonical_token("услуга")


def _tail_names_the_service(tail: tuple[str, ...], service_tokens: frozenset[str]) -> bool:
    """Хвост — родовое обозначение услуги, а не её видовой признак."""
    stems = {canonical_token(token) for token in tail}
    return bool(stems) and _SERVICE_HEAD in stems and stems <= service_tokens


def operation_inventories(candidates: Iterable[BuiltProjectFrame]) -> dict[str, frozenset[str]]:
    """Перечни доказанных названий частей услуги по области срока (см. ``_shared_operation``)."""
    found: dict[str, set[str]] = {}
    for built in candidates:
        scope = _deadline_scope(built)
        value = built.frame.subservice.proven_value()
        if scope is None or not value:
            continue
        found.setdefault(scope, set()).add(value)
    return {scope: frozenset(values) for scope, values in found.items()}


def _shared_operation(
    whole: BuiltProjectFrame, stage: BuiltProjectFrame, inventories: OperationInventories | None = None
) -> tuple[str, OperationBasis] | None:
    "Одна операция, доказанная ОБЕИМИ сторонами, либо ``None``."
    left = whole.frame.subservice
    right = stage.frame.subservice
    proven = left.proven_value(), right.proven_value()
    if proven[0] and proven[0] == proven[1]:
        return proven[0], "subservice"
    if proven[0] and proven[1] and inventories is not None:
        whole_scope, stage_scope = _deadline_scope(whole), _deadline_scope(stage)
        prefix = _shared_operation_prefix(
            proven[0],
            proven[1],
            whole_inventory=inventories.get(whole_scope or "", frozenset()),
            stage_inventory=inventories.get(stage_scope or "", frozenset()),
            service_tokens=_service_tokens(whole, stage),
        )
        if prefix is not None:
            return prefix, "subservice_prefix"
    if left.raw_value or right.raw_value:
        return None
    for slot_name in _OPERATION_SLOTS[1:]:
        value = getattr(whole.frame, slot_name).proven_value()
        if value and value == getattr(stage.frame, slot_name).proven_value():
            return value, slot_name
    return None


def _constraint_value_is_readable(constraint: TimeLimitConstraintEvidenceV1, document_text: str) -> bool:
    "Объявленная граница читается в СВОЁМ срезе документа тем же извлечением величин."
    exact = document_text[constraint.span.char_start : constraint.span.char_end]
    if not exact:
        return False
    return any(
        parameter.type is ParameterType.DURATION
        and parameter.norm_value == constraint.normalized_value
        and parameter.unit is not None
        and normalized_text(parameter.unit) == constraint.unit
        and parameter.calendar_kind is not None
        and normalized_text(parameter.calendar_kind) == constraint.calendar_kind
        for parameter in extract_parameters(exact)
    )


def time_limit_values_are_readable(evidence: TimeLimitConsistencyEvidenceV1, document_text: str) -> bool:
    """Каждая граница доказательства выведена из текста своей нормы, а не заявлена."""
    return all(
        _constraint_value_is_readable(item, document_text)
        for item in (evidence.whole, evidence.stage, *evidence.additional_stage_constraints)
    )


def time_limit_consistency_evidence(
    first: BuiltProjectFrame,
    second: BuiltProjectFrame,
    *,
    document_text: str,
    inventories: OperationInventories | None = None,
) -> TimeLimitConsistencyEvidenceV1 | None:
    """Разбор пары; ``observed`` копит пройденные причины отказа по возрастанию глубины."""
    scopes = {id(first): _deadline_scope(first), id(second): _deadline_scope(second)}
    if not all(scopes.values()):
        return None
    for whole, stage in ((first, second), (second, first)):
        whole_scope_value, stage_scope_value = scopes[id(whole)], scopes[id(stage)]
        if whole_scope_value != WHOLE_SERVICE_DEADLINE_SCOPE or not stage_scope_value:
            continue
        if stage_scope_value == WHOLE_SERVICE_DEADLINE_SCOPE:
            continue
        if stage_scope_value not in NESTED_STAGE_NAMES:
            continue
        operation = _shared_operation(whole, stage, inventories)
        if operation is None:
            continue
        operation_value, operation_basis = operation
        ancestor_refusal = _ancestor_applicability_refusal(whole, stage, document_text=document_text)
        if ancestor_refusal is not None:
            continue
        spans = (
            _operand_span(whole, "deadline_scope", "whole_scope", document_text),
            _operand_span(stage, "deadline_scope", "stage_scope", document_text),
            _operand_span(whole, operation_basis, "whole_operation", document_text),
            _operand_span(stage, operation_basis, "stage_operation", document_text),
        )
        if any(item is None for item in spans):
            continue
        whole_scope, stage_scope, whole_operation, stage_operation = spans
        assert whole_scope is not None and stage_scope is not None
        assert whole_operation is not None and stage_operation is not None
        stage_constraints = [
            item
            for raw in stage.frame.regulated_variable.duration_constraints
            if (item := _constraint(stage, raw, role="stage_limit", document_text=document_text)) is not None
        ]
        whole_constraints = [
            item
            for raw in whole.frame.regulated_variable.duration_constraints
            if (item := _constraint(whole, raw, role="whole_limit", document_text=document_text)) is not None
        ]
        if not whole_constraints or not stage_constraints:
            continue
        for whole_evidence in whole_constraints:
            for stage_evidence in stage_constraints:
                if stage_evidence.unit != whole_evidence.unit or stage_evidence.calendar_kind != whole_evidence.calendar_kind:
                    continue
                shared_event = _shared_start_event(stage_evidence.start_event, whole_evidence.start_event)
                if shared_event is None:
                    continue

                binding_stage = _binding_bound(stage_constraints, stage_evidence, shared_event)
                binding_whole = _binding_bound(whole_constraints, whole_evidence, shared_event)
                if binding_stage.normalized_value <= binding_whole.normalized_value:
                    continue
                if not _applicability_is_accounted_for(whole, stage, document_text=document_text):
                    continue
                return TimeLimitConsistencyEvidenceV1(
                    operation=operation_value,
                    operation_basis=operation_basis,
                    stage_name=stage_scope_value,
                    whole=_with_start_event(binding_whole, shared_event),
                    stage=_with_start_event(binding_stage, shared_event),
                    whole_scope=whole_scope,
                    stage_scope=stage_scope,
                    whole_operation=whole_operation,
                    stage_operation=stage_operation,
                    additional_stage_constraints=[item for item in stage_constraints if item is not binding_stage],
                )
    return None


_BOUND_QUALIFIER = re.compile(r"не\s+позднее|не\s+более|не\s+превыш\w*|в\s+течение|ровно", re.IGNORECASE)
_EVENT_PREAMBLE = re.compile(r"с\s+дат\w*|со\s+дня|с\s+момента|начиная\s+с", re.IGNORECASE)
_SCOPE_INTRO = re.compile(r"в\s+части|в\s+отношении", re.IGNORECASE)
_GLUE = re.compile(r"\b(?:и|но|а|также|либо|или)\b", re.IGNORECASE)


_PURPOSE_QUALIFIER = re.compile(r"\bнеобходим\w+\s+для\s+(?:[^\W\d_]+\s+){0,3}?(?:под)?услуг\w*", re.IGNORECASE)


_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s*")


_ACCOUNTED_SLOTS: tuple[str, ...] = ("subservice", "service", "measure", "deadline_scope")


def _unaccounted_words(built: BuiltProjectFrame, document_text: str) -> tuple[str, ...]:
    "Слова текста нормы, НЕ объяснённые ни одним доказанным операндом."
    primary = built.frame.evidence.primary_span
    if primary is None or primary.char_start is None or primary.char_end is None:
        return ()
    norm = document_text[primary.char_start : primary.char_end]
    keep = [True] * len(norm)

    def cut(start: int, end: int) -> None:
        for index in range(max(0, start), min(len(norm), end)):
            keep[index] = False

    marker = _LIST_MARKER.match(norm)
    if marker is not None:
        cut(*marker.span())

    constraints = sorted(
        (item for item in built.frame.regulated_variable.duration_constraints if item.source_start is not None),
        key=lambda item: item.source_start or 0,
    )
    starts = [item.source_start or 0 for item in constraints]
    for position, constraint in enumerate(constraints):
        start, end = constraint.source_start, constraint.source_end
        if start is None or end is None:
            continue
        cut(start, end)

        limit = starts[position + 1] if position + 1 < len(starts) else len(norm)
        tail = norm[end:limit]
        stripped = tail.lstrip(" ,;–—-")
        lead = len(tail) - len(stripped)
        preamble = _EVENT_PREAMBLE.match(stripped)
        if preamble is not None:
            cut_end = end + lead + preamble.end()
            event = constraint.start_event
            event_value = event.raw_value if event is not None and event.role_is_proven() else None
            if event_value:
                found_at = stripped.find(event_value, preamble.end())
                if found_at >= 0:
                    cut_end = end + lead + found_at + len(event_value)
            cut(end, cut_end)
    for slot_name in _ACCOUNTED_SLOTS:
        slot = getattr(built.frame, slot_name, None)

        value = (slot.raw_value if slot is not None and slot.role_is_proven() else None) or ""
        if value and (position := norm.find(value)) >= 0:
            cut(position, position + len(value))
    for pattern in (_BOUND_QUALIFIER, _EVENT_PREAMBLE, _SCOPE_INTRO, _GLUE, _PURPOSE_QUALIFIER):
        for match in pattern.finditer(norm):
            cut(*match.span())
    rest = "".join(char for char, kept in zip(norm, keep, strict=True) if kept)
    return tuple(word.casefold() for word in ALNUM.findall(rest))


_ABOUT_CONNECTOR = re.compile(r"^\s*об?\s+", re.IGNORECASE)


def _deadline_construction_tail(clause: str) -> str | None:
    "Остаток клаузы СВЕРХ конструкции, которой ``frame_builder`` доказал область срока."
    whole_match = WHOLE_SERVICE_HEAD.match(clause)
    if whole_match is not None:
        return clause[whole_match.end() :]
    word_matches = list(LETTERS.finditer(clause))
    if not word_matches:
        return None
    if stage_noun(word_matches[0].group()) is not None:
        return clause[word_matches[0].end() :]
    for _name, pattern in STAGE_CONSTITUTING_CONSTRUCTIONS:
        match = pattern.match(clause)
        if match is None:
            continue
        tail = clause[match.end() :]
        about = _ABOUT_CONNECTOR.match(tail)
        if about is not None:
            service = WHOLE_SERVICE_HEAD.match(tail[about.end() :])
            if service is not None:
                return tail[about.end() + service.end() :]
        return tail
    return None


def _scope_intro_residual_words(built: BuiltProjectFrame, line: str) -> tuple[str, ...]:
    "Слова ОДНОЙ вводной строки за пределами доказанной конструкции срока."
    intro = CLAUSE_NUMBER_PREFIX.sub("", line.strip())
    head = DEADLINE_HEAD.match(intro)
    if head is None:
        clause_tail = intro
    else:
        clause = first_clause(intro[head.end() :])
        tail = _deadline_construction_tail(clause)
        clause_tail = clause if tail is None else tail
    keep = [True] * len(clause_tail)

    def cut(start: int, end: int) -> None:
        for index in range(max(0, start), min(len(clause_tail), end)):
            keep[index] = False

    for slot_name in _ACCOUNTED_SLOTS:
        slot = getattr(built.frame, slot_name, None)
        value = (slot.raw_value if slot is not None and slot.role_is_proven() else None) or ""
        if value and (position := clause_tail.find(value)) >= 0:
            cut(position, position + len(value))
    for constraint in built.frame.regulated_variable.duration_constraints:
        event = constraint.start_event
        value = event.raw_value if event is not None and event.role_is_proven() else None
        if value and (position := clause_tail.find(value)) >= 0:
            cut(position, position + len(value))
    for pattern in (_BOUND_QUALIFIER, _EVENT_PREAMBLE, _SCOPE_INTRO, _GLUE, _PURPOSE_QUALIFIER):
        for match in pattern.finditer(clause_tail):
            cut(*match.span())
    rest = "".join(char for char, kept in zip(clause_tail, keep, strict=True) if kept)
    return tuple(word.casefold() for word in ALNUM.findall(rest))


def _ancestor_scope_residual(built: BuiltProjectFrame, document_text: str) -> tuple[str, ...]:
    "Слова ВСЕХ вводных, доказавших ``deadline_scope`` этой нормы, вне их конструкции."
    slot = built.frame.deadline_scope
    if slot is None or not slot.role_is_proven():
        return ()
    spans = _spans_by_id(built)
    words: list[str] = []
    for span_id in slot.evidence_span_ids:
        span = spans.get(span_id)
        if span is None or span.char_start is None or span.char_end is None:
            continue
        line = document_text[span.char_start : span.char_end]
        words.extend(_scope_intro_residual_words(built, line))
    return tuple(words)


def _ancestor_applicability_refusal(whole: BuiltProjectFrame, stage: BuiltProjectFrame, *, document_text: str) -> str | None:
    "Ограничение применимости, объявленное ВВОДНОЙ конструкцией, а не самой нормой."
    if _unparsed_conditions(whole, document_text) or _unparsed_conditions(stage, document_text):
        return "applicability_conditions_unparsed"
    whole_premises, stage_premises = _premises(whole, document_text), _premises(stage, document_text)
    if not (whole_premises <= stage_premises or stage_premises <= whole_premises):
        return "applicability_premises_not_comparable"
    whole_scope_rest = _ancestor_scope_residual(whole, document_text)
    stage_scope_rest = _ancestor_scope_residual(stage, document_text)
    if whole_scope_rest and stage_scope_rest and whole_scope_rest != stage_scope_rest:
        return "ancestor_scope_intro_unaccounted"
    return None


def _applicability_is_accounted_for(whole: BuiltProjectFrame, stage: BuiltProjectFrame, *, document_text: str) -> bool:
    "Совместная применимость норм ДОКАЗАНА, а не предположена по отсутствию маркеров."
    whole_rest = _unaccounted_words(whole, document_text)
    stage_rest = _unaccounted_words(stage, document_text)
    if not whole_rest or not stage_rest:
        return True
    return whole_rest == stage_rest


def _binding_bound(
    constraints: Sequence[TimeLimitConstraintEvidenceV1],
    candidate: TimeLimitConstraintEvidenceV1,
    shared_event: str,
) -> TimeLimitConstraintEvidenceV1:
    "Предел стороны среди границ той же единицы, календаря и ДОКАЗАННО того же события."
    same = [
        item
        for item in constraints
        if item.unit == candidate.unit
        and item.calendar_kind == candidate.calendar_kind
        and _shared_start_event(item.start_event, shared_event) is not None
    ]
    return min(same, key=lambda item: item.normalized_value)


def _sibling_group_id(built: BuiltProjectFrame) -> str:
    """Элемент-родитель нормы: соседи одного перечня делят его, разные перечни — нет."""
    path = built.frame.tree_path
    return path[-2].element_id if len(path) >= 2 else built.leaf.element_id


def time_limit_conflicts(
    frames: Iterable[BuiltProjectFrame],
    *,
    document_text: str,
) -> list[TimeLimitHit]:
    "Самостоятельный детерминированный проход по нормам проекта."
    candidates: list[BuiltProjectFrame] = [
        built for built in frames if _deadline_scope(built) is not None and built.frame.regulated_variable.duration_constraints
    ]
    inventories = operation_inventories(candidates)
    hits: list[TimeLimitHit] = []
    seen_relations: set[tuple[object, ...]] = set()
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if left.leaf.element_id == right.leaf.element_id:
                continue
            evidence = time_limit_consistency_evidence(left, right, document_text=document_text, inventories=inventories)
            if evidence is None:
                continue

            whole_frame = left if _deadline_scope(left) == WHOLE_SERVICE_DEADLINE_SCOPE else right
            relation_key = (
                evidence.operation,
                evidence.whole.normalized_value,
                evidence.stage.normalized_value,
                evidence.whole.unit,
                evidence.whole.calendar_kind,
                evidence.whole.start_event,
                _sibling_group_id(whole_frame),
                evidence.stage.span.char_start,
                evidence.stage.span.char_end,
            )
            if relation_key in seen_relations:
                continue
            seen_relations.add(relation_key)
            hits.append(TimeLimitHit(left=left, right=right, evidence=evidence))
    return hits


def time_limit_findings(frames: Iterable[BuiltProjectFrame], *, document_text: str) -> list[DetectorFinding]:
    "Находки отношения предельных сроков по операндам проекта."
    hits = time_limit_conflicts(frames, document_text=document_text)
    findings = []
    for hit in hits:
        if not time_limit_values_are_readable(hit.evidence, document_text):
            continue
        primary_left = hit.left.frame.evidence.primary_span
        primary_right = hit.right.frame.evidence.primary_span
        if primary_left is None or primary_right is None:
            continue
        second_place = humanize_locus(hit.right.leaf.label or UNLABELED_PROVISION) or hit.right.leaf.element_id
        findings.append(
            detector_finding(
                InternalCheck.TIME_LIMIT,
                place=humanize_locus(hit.left.leaf.label or UNLABELED_PROVISION),
                quote=primary_left.text,
                rationale=settings["agents.internal.messages.time_limit_rationale"],
                remedy=settings["agents.internal.messages.time_limit_remedy"],
                seconds=[(second_place, primary_right.text)],
                related_places=[second_place],
            )
        )
    return findings


__all__: Sequence[str] = (
    "operation_inventories",
    "time_limit_conflicts",
    "time_limit_consistency_evidence",
    "time_limit_findings",
)
