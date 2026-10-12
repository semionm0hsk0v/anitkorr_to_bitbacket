from __future__ import annotations

import re
from dataclasses import dataclass

from src.agents.enums.internal.frames.frame_match import FrameMismatchReason
from src.agents.enums.internal.frames.norm_frame import FramePairOutcome, FrameRelation
from src.agents.schemas.internal.frames.norm_frame import (
    EvidenceSourceKind,
    FrameOperandEvidence,
    FrameProvenance,
    FrameSlot,
    GroundingStatus,
    NormFrame,
    RegulatedVariable,
    RegulatedVariableKind,
    role_is_proven,
)
from src.agents.steps_tools.document.morphology import normalized_text
from src.agents.steps_tools.internal.frames.context_policy import eligible_evidence_spans
from src.agents.steps_tools.internal.frames.service_stages import STAGE_FAMILIES

#: Отношения, при которых измерение сходится: тождество и вложенность в любую сторону.
POSITIVE_RELATIONS = frozenset({FrameRelation.SAME, FrameRelation.GENERAL_TO_SPECIFIC, FrameRelation.SPECIFIC_TO_GENERAL})

_PUNCTUATION = re.compile(r"[^0-9a-zа-я]+", re.IGNORECASE)
_GENERIC_SERVICE_LABEL = re.compile(
    r"(?:(?:предоставлени[ея]|оказани[ея])\s+)?"
    r"(?:(?:государственн|муниципальн)\w*\s+"
    r"(?:(?:(?:и|или)\s+)?(?:государственн|муниципальн)\w*\s+)?)?услуг\w*"
)
_STOP_WORDS = frozenset(
    {
        "в",
        "во",
        "для",
        "и",
        "к",
        "на",
        "о",
        "об",
        "по",
        "при",
        "с",
        "со",
        "услуга",
        "услуги",
        "услуг",
    }
)


_TOKEN_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^выда(?:ть|ча|ет|ется|ваем\w*|н\w*)$"), "выдача"),
    (re.compile(r"^регистр(?:ировать|ация|ирует|ируется|ирован\w*)$"), "регистрация"),
    (re.compile(r"^предостав(?:ить|ление|ляет|ляется|лен\w*)$"), "предоставление"),
    (re.compile(r"^направ(?:ить|ление|ляет|ляется|лен\w*)$"), "направление"),
    (re.compile(r"^рассматр(?:ивать|ивает|ивается|иваем\w*)$"), "рассмотрение"),
    (re.compile(r"^ожида(?:ть|ние|ет|ется)$"), "ожидание"),
    (re.compile(r"^документ(?:а|у|ом|е|ы|ов|ами|ах)?$"), "документ"),
    (re.compile(r"^заявлени(?:е|я|ю|ем|и|й|ями|ях)$"), "заявление"),
    (re.compile(r"^реестр(?:а|у|ом|е|ы|ов|ами|ах)?$"), "реестр"),
    (re.compile(r"^систем(?:а|ы|е|у|ой|ами|ах)$"), "система"),
)

_ACTION_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("registration", re.compile(r"\b(?:регистрац|регистрир|реестр|внесени\w*\s+запис)")),
    ("document_issue", re.compile(r"\b(?:выдач|выда\w*|направлен|предоставлен).{0,50}\bдокумент")),
    ("inspection", re.compile(r"\b(?:осмотр|обследован|проверк)")),
    ("queue_wait", re.compile(r"\b(?:ожидани|очеред)")),
    ("decision", re.compile(r"\b(?:решени|отказ|удовлетворен)")),
)
_SUBSERVICE_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("inspection", re.compile(r"\b(?:техосмотр|осмотр|обследован|проверк)")),
    ("duplicate_issue", re.compile(r"\b(?:дубликат|повторн\w*\s+выдач)")),
    ("registration", re.compile(r"\b(?:регистрац|регистрир)")),
    ("document_issue", re.compile(r"\b(?:выдач|получени).{0,50}\bдокумент")),
    ("queue_wait", re.compile(r"\b(?:ожидани|очеред)")),
)


def normalize_frame_value(value: str | None) -> str:
    """Нормализует регистр, ``ё``, punctuation и whitespace без fuzzy guess."""
    if not value:
        return ""
    return _PUNCTUATION.sub(" ", normalized_text(value)).strip()


def _alias_tokens(value: str) -> tuple[str, ...]:
    result: list[str] = []
    for token in normalize_frame_value(value).split():
        alias = next((replacement for pattern, replacement in _TOKEN_ALIASES if pattern.fullmatch(token)), token)
        result.append(alias)
    return tuple(result)


def _positive_tokens(value: str) -> frozenset[str]:
    return frozenset(token for token in _alias_tokens(value) if token not in _STOP_WORDS)


def _family(value: str, ontology: tuple[tuple[str, re.Pattern[str]], ...]) -> str | None:
    normalized = normalize_frame_value(value)
    return next((name for name, pattern in ontology if pattern.search(normalized)), None)


def _allowed_span(frame: NormFrame, span_id: str):
    span = eligible_evidence_spans(frame).get(span_id)
    if span is None:
        return None
    if span.grounding_status == GroundingStatus.EXACT:
        return span
    if span.grounding_status == GroundingStatus.LEGACY_LOCAL and span.source_kind == EvidenceSourceKind.REFERENCE_NORM:
        return span
    return None


@dataclass(frozen=True)
class _Operand:
    frame: NormFrame
    dimension: str
    value: str
    provenance: FrameProvenance
    confidence: float
    evidence_span_ids: tuple[str, ...]

    def hard_receipt(self) -> FrameOperandEvidence | None:
        if not self.value or not role_is_proven(self.provenance, self.confidence):
            return None
        spans = [_allowed_span(self.frame, span_id) for span_id in self.evidence_span_ids]
        if not spans or any(span is None for span in spans):
            return None
        return FrameOperandEvidence(
            dimension=self.dimension,
            provenance=self.provenance,
            confidence=self.confidence,
            evidence_span_ids=list(self.evidence_span_ids),
            source_kinds=[span.source_kind for span in spans if span is not None],
            grounding_statuses=[span.grounding_status for span in spans if span is not None],
        )


def _slot_operand(frame: NormFrame, dimension: str, slot: FrameSlot) -> _Operand:
    return _Operand(
        frame=frame,
        dimension=dimension,
        value=slot.normalized_value or slot.raw_value or "",
        provenance=slot.provenance,
        confidence=slot.confidence,
        evidence_span_ids=tuple(slot.evidence_span_ids),
    )


def _variable_operand(frame: NormFrame, variable: RegulatedVariable) -> _Operand:
    return _Operand(
        frame=frame,
        dimension="regulated_variable",
        value=variable.kind.value if variable.kind != RegulatedVariableKind.UNKNOWN else "",
        provenance=variable.provenance,
        confidence=variable.confidence,
        evidence_span_ids=tuple(variable.evidence_span_ids),
    )


def _positive_relation(left: str, right: str) -> FrameRelation:
    left_normalized = normalize_frame_value(left)
    right_normalized = normalize_frame_value(right)
    if not left_normalized or not right_normalized:
        return FrameRelation.UNKNOWN
    if left_normalized == right_normalized or _alias_tokens(left_normalized) == _alias_tokens(right_normalized):
        return FrameRelation.SAME
    left_tokens = _positive_tokens(left_normalized)
    right_tokens = _positive_tokens(right_normalized)
    if left_tokens and left_tokens < right_tokens:
        return FrameRelation.GENERAL_TO_SPECIFIC
    if right_tokens and right_tokens < left_tokens:
        return FrameRelation.SPECIFIC_TO_GENERAL
    return FrameRelation.UNKNOWN


def _hard_pair(
    left: _Operand,
    right: _Operand,
) -> list[FrameOperandEvidence] | None:
    left_receipt = left.hard_receipt()
    right_receipt = right.hard_receipt()
    return [left_receipt, right_receipt] if left_receipt is not None and right_receipt is not None else None


def _supported_positive_relation(
    left: _Operand,
    right: _Operand,
) -> FrameRelation:
    relation = _positive_relation(left.value, right.value)
    if relation == FrameRelation.UNKNOWN:
        return relation
    if _hard_pair(left, right) is None:
        return FrameRelation.UNKNOWN
    return relation


def _container_scope_designation(operand: _Operand) -> bool:
    """Значение унаследовано от предмета контейнера, а не извлечено из самой нормы."""
    return operand.provenance is FrameProvenance.INHERITED


def _explicit_slot_relation(
    left: _Operand,
    right: _Operand,
) -> tuple[FrameRelation, list[FrameOperandEvidence]]:
    positive = _supported_positive_relation(
        left,
        right,
    )
    if positive != FrameRelation.UNKNOWN:
        return positive, []
    operands = _hard_pair(left, right)

    if operands is not None and not (_container_scope_designation(left) and _container_scope_designation(right)):
        return FrameRelation.DISJOINT, operands
    return FrameRelation.UNKNOWN, []


def _generic_service_pair(left: str, right: str) -> bool:
    left_normalized = normalize_frame_value(left)
    right_normalized = normalize_frame_value(right)
    return left_normalized != right_normalized and any(
        _GENERIC_SERVICE_LABEL.fullmatch(value) is not None for value in (left_normalized, right_normalized)
    )


def _service_relation(left: _Operand, right: _Operand) -> tuple[FrameRelation, list[FrameOperandEvidence]]:

    if _generic_service_pair(left.value, right.value):
        return FrameRelation.UNKNOWN, []
    return _explicit_slot_relation(left, right)


def _are_sibling_paths(left: NormFrame, right: NormFrame) -> bool:
    left_ids = [item.element_id for item in left.tree_path]
    right_ids = [item.element_id for item in right.tree_path]
    if len(left_ids) < 2 or len(right_ids) < 2:
        return False
    common = 0
    for left_id, right_id in zip(left_ids, right_ids, strict=False):
        if left_id != right_id:
            break
        common += 1
    return 0 < common < min(len(left_ids), len(right_ids))


def _subservice_relation(
    left: _Operand,
    right: _Operand,
    *,
    sibling_paths: bool,
) -> tuple[FrameRelation, list[FrameOperandEvidence]]:
    positive = _supported_positive_relation(
        left,
        right,
    )
    if positive != FrameRelation.UNKNOWN:
        return positive, []
    left_family = _family(left.value, _SUBSERVICE_FAMILIES)
    right_family = _family(right.value, _SUBSERVICE_FAMILIES)
    if sibling_paths or (left_family is not None and right_family is not None and left_family != right_family):
        operands = _hard_pair(left, right)
        if operands is not None:
            return FrameRelation.DISJOINT, operands
    return FrameRelation.UNKNOWN, []


def _stage_relation(
    left: _Operand,
    right: _Operand,
) -> tuple[FrameRelation, list[FrameOperandEvidence]]:
    positive = _supported_positive_relation(
        left,
        right,
    )
    if positive != FrameRelation.UNKNOWN:
        return positive, []
    left_family = _family(left.value, STAGE_FAMILIES)
    right_family = _family(right.value, STAGE_FAMILIES)
    if left_family == "general_service" and right_family:
        return FrameRelation.GENERAL_TO_SPECIFIC, []
    if right_family == "general_service" and left_family:
        return FrameRelation.SPECIFIC_TO_GENERAL, []
    if left_family is not None and right_family is not None and left_family != right_family:
        operands = _hard_pair(left, right)
        if operands is not None:
            return FrameRelation.DISJOINT, operands
    return FrameRelation.UNKNOWN, []


def _action_object_relation(
    left_frame: NormFrame,
    right_frame: NormFrame,
) -> tuple[FrameRelation, list[FrameOperandEvidence]]:
    left_action = _slot_operand(left_frame, "action", left_frame.action)
    right_action = _slot_operand(right_frame, "action", right_frame.action)
    left_object = _slot_operand(left_frame, "object", left_frame.object)
    right_object = _slot_operand(right_frame, "object", right_frame.object)
    action_relation = _supported_positive_relation(
        left_action,
        right_action,
    )
    object_relation = _supported_positive_relation(
        left_object,
        right_object,
    )
    if FrameRelation.UNKNOWN not in {action_relation, object_relation}:
        return FrameRelation.SAME, []
    left_family = _family(f"{left_action.value} {left_object.value}", _ACTION_FAMILIES)
    right_family = _family(f"{right_action.value} {right_object.value}", _ACTION_FAMILIES)
    if left_family is not None and right_family is not None and left_family != right_family:
        # Either grounded action or grounded object may carry the typed family.
        action_operands = _hard_pair(
            left_action,
            right_action,
        )
        object_operands = _hard_pair(
            left_object,
            right_object,
        )
        operands = action_operands or object_operands
        if operands is not None:
            return FrameRelation.DISJOINT, operands
    if action_relation != FrameRelation.UNKNOWN or object_relation != FrameRelation.UNKNOWN:
        return FrameRelation.SAME, []
    return FrameRelation.UNKNOWN, []


_VARIABLE_GROUPS: dict[RegulatedVariableKind, str] = {
    RegulatedVariableKind.DURATION: "time",
    RegulatedVariableKind.DEADLINE: "time",
    RegulatedVariableKind.MONETARY_AMOUNT: "amount",
    RegulatedVariableKind.SHARE: "share",
    RegulatedVariableKind.AGE: "age",
    RegulatedVariableKind.AREA: "area",
    RegulatedVariableKind.RESULT_KIND: "document_result",
    RegulatedVariableKind.DOCUMENT_RESULT: "document_result",
    RegulatedVariableKind.DOCUMENT_DELIVERY: "document_result",
    RegulatedVariableKind.REGISTRY_TARGET: "registration",
    RegulatedVariableKind.INFORMATION_SYSTEM_TARGET: "registration",
    RegulatedVariableKind.REGISTRATION_START_EVENT: "registration",
    RegulatedVariableKind.REGISTRATION_COMPLETION_EVENT: "registration",
    RegulatedVariableKind.QUEUE_WAITING_TIME: "queue",
    RegulatedVariableKind.SET_IDENTITY: "set",
    RegulatedVariableKind.LIST_IDENTITY: "set",
    RegulatedVariableKind.SUBMISSION_CHANNEL: "submission",
    RegulatedVariableKind.SUBMISSION_FORM: "submission",
}


def _variable_relation(
    left: _Operand,
    right: _Operand,
    left_kind: RegulatedVariableKind,
    right_kind: RegulatedVariableKind,
) -> tuple[FrameRelation, list[FrameOperandEvidence]]:
    if RegulatedVariableKind.UNKNOWN in {left_kind, right_kind}:
        return FrameRelation.UNKNOWN, []
    operands = _hard_pair(left, right)
    if operands is None:
        return FrameRelation.UNKNOWN, []
    if left_kind == right_kind:
        return FrameRelation.SAME, []

    if (
        left.frame.regulated_variable.target_system is not None
        and not left.frame.regulated_variable.target_system.normalized_value
    ) or (
        right.frame.regulated_variable.target_system is not None
        and not right.frame.regulated_variable.target_system.normalized_value
    ):
        return FrameRelation.UNKNOWN, []
    left_group = _VARIABLE_GROUPS.get(left_kind)
    right_group = _VARIABLE_GROUPS.get(right_kind)
    if left_group is not None and left_group == right_group:
        if left_kind == RegulatedVariableKind.DURATION and right_kind == RegulatedVariableKind.DEADLINE:
            return FrameRelation.GENERAL_TO_SPECIFIC, []
        if left_kind == RegulatedVariableKind.DEADLINE and right_kind == RegulatedVariableKind.DURATION:
            return FrameRelation.SPECIFIC_TO_GENERAL, []
        return FrameRelation.SAME, []
    if left_group is not None and right_group is not None:
        return FrameRelation.DISJOINT, operands
    return FrameRelation.UNKNOWN, []


@dataclass(frozen=True)
class _PairComparison:
    "Отношения двух рамок по шести измерениям и жёсткий отказ."

    relations: tuple[FrameRelation, ...]
    rejection: tuple[FrameMismatchReason, list[FrameOperandEvidence]] | None


def _compare(left: NormFrame, right: NormFrame) -> _PairComparison:
    measure, measure_hard = _explicit_slot_relation(
        _slot_operand(left, "measure", left.measure),
        _slot_operand(right, "measure", right.measure),
    )
    service, service_hard = _service_relation(
        _slot_operand(left, "service", left.service),
        _slot_operand(right, "service", right.service),
    )
    subservice, subservice_hard = _subservice_relation(
        _slot_operand(left, "subservice", left.subservice),
        _slot_operand(right, "subservice", right.subservice),
        sibling_paths=_are_sibling_paths(left, right),
    )
    stage, stage_hard = _stage_relation(
        _slot_operand(left, "procedure_stage", left.procedure_stage),
        _slot_operand(right, "procedure_stage", right.procedure_stage),
    )
    action_object, action_object_hard = _action_object_relation(
        left,
        right,
    )
    left_variable = _variable_operand(left, left.regulated_variable)
    right_variable = _variable_operand(right, right.regulated_variable)
    variable, variable_hard = _variable_relation(
        left_variable,
        right_variable,
        left.regulated_variable.kind,
        right.regulated_variable.kind,
    )
    first_rejection = next(
        (
            (reason, operands)
            for relation, reason, operands in (
                (measure, FrameMismatchReason.FRAME_MEASURE_MISMATCH, measure_hard),
                (service, FrameMismatchReason.FRAME_SERVICE_MISMATCH, service_hard),
                (subservice, FrameMismatchReason.FRAME_SUBSERVICE_MISMATCH, subservice_hard),
                (stage, FrameMismatchReason.FRAME_STAGE_DISJOINT, stage_hard),
                (action_object, FrameMismatchReason.FRAME_ACTION_OBJECT_MISMATCH, action_object_hard),
                (variable, FrameMismatchReason.FRAME_VARIABLE_MISMATCH, variable_hard),
            )
            if relation == FrameRelation.DISJOINT
        ),
        None,
    )
    if first_rejection is not None and len(first_rejection[1]) != 2:
        raise ValueError("incompatible frame decision requires two grounded operands")
    return _PairComparison(
        relations=(measure, service, subservice, stage, action_object, variable),
        rejection=first_rejection,
    )


def match_frames(
    left: NormFrame,
    right: NormFrame,
) -> FramePairOutcome:
    """Совместимы ли два frames: исход без внешнего состояния и побочных эффектов."""
    comparison = _compare(left, right)
    if comparison.rejection is not None:
        return FramePairOutcome.INCOMPATIBLE
    if any(relation in POSITIVE_RELATIONS for relation in comparison.relations):
        return FramePairOutcome.COMPATIBLE
    return FramePairOutcome.UNKNOWN
