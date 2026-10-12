from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agents.enums.internal.frames.norm_frame import (
    AddressSpace,
    ContextBlockRole,
    EvidenceSourceKind,
    FrameProvenance,
    GroundingStatus,
    RegulatedVariableKind,
    SourceRole,
)
from src.agents.utils.identifiers import build_stable_id
from src.common.config.config import settings

NORM_FRAME_SCHEMA_VERSION: Literal[1] = 1


WHOLE_SERVICE_DEADLINE_SCOPE = "услуга"


class TreePathElement(BaseModel):
    """Один элемент пути от корня дерева документа к leaf нормы."""

    element_id: str
    kind: str
    label: str = ""


class EvidenceSpan(BaseModel):
    """Координаты доказательства в явно названном исходном тексте."""

    span_id: str = ""
    source_kind: EvidenceSourceKind
    source_id: str
    content_fingerprint: str
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    text: str = ""
    grounding_status: GroundingStatus

    @model_validator(mode="before")
    @classmethod
    def _assign_stable_id(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("span_id"):
            return value
        data = dict(value)
        data["span_id"] = build_stable_id(
            "span",
            {
                "source_kind": str(data.get("source_kind", "")),
                "source_id": str(data.get("source_id", "")),
                "content_fingerprint": str(data.get("content_fingerprint", "")),
                "span": [data.get("char_start"), data.get("char_end")],
                "text": str(data.get("text", "")),
                "grounding_status": str(data.get("grounding_status", "")),
            },
        )
        return data

    @model_validator(mode="after")
    def _validate_coordinates(self) -> EvidenceSpan:
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must either both be set or both be omitted")
        if self.char_start is not None and self.char_end is not None and self.char_end < self.char_start:
            raise ValueError("char_end must not precede char_start")
        if self.grounding_status == GroundingStatus.EXACT:
            if self.char_start is None or self.char_end is None:
                raise ValueError("exact evidence requires coordinates")
            if self.char_end - self.char_start != len(self.text):
                raise ValueError("exact evidence coordinates must match the local evidence text length")
        return self


class ContextBlock(BaseModel):
    """Блок контекста нормы: спан из ``FrameEvidence.context_spans`` и роль, в которой он относится к норме."""

    model_config = ConfigDict(frozen=True)

    span_id: str
    role: ContextBlockRole


class FrameEvidence(BaseModel):
    """Короткая UI-цитата и отдельные spans, пригодные для evidence gates.

    ``context_spans`` — все структурные спаны, найденные вокруг нормы. ``context_blocks`` — те из них, которые относятся к
    норме доказанно (отношение подтверждено, спан точный, граница общая), в порядке близости роли; только они доказательства.
    """

    display_quote: str = ""
    primary_span: EvidenceSpan | None = None
    context_spans: list[EvidenceSpan] = Field(default_factory=list)
    context_blocks: list[ContextBlock] = Field(default_factory=list)
    grounding_status: GroundingStatus = GroundingStatus.UNRESOLVED


PROVEN_ROLE_CONFIDENCE = float(settings["agents.schemas.internal.norm_frame.proven_role_confidence"])
UNPROVEN_ROLE_CONFIDENCE = float(settings["agents.schemas.internal.norm_frame.unproven_role_confidence"])


def role_is_proven(provenance: FrameProvenance, confidence: float) -> bool:
    "Пригодность значения слота как ОПЕРАНДА: жёсткого отсева и доказательства."
    if provenance == FrameProvenance.LEGACY:
        return False
    return confidence >= PROVEN_ROLE_CONFIDENCE


class FrameSlot(BaseModel):
    """Семантическое значение вместе с происхождением и evidence identity."""

    raw_value: str | None = None
    normalized_value: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    provenance: FrameProvenance = FrameProvenance.LEGACY
    evidence_span_ids: list[str] = Field(default_factory=list)

    def role_is_proven(self) -> bool:
        "Значение пригодно как операнд: роль доказана и подтверждена спанами."
        if not (self.normalized_value or self.raw_value) or not self.evidence_span_ids:
            return False
        return role_is_proven(self.provenance, self.confidence)

    def proven_value(self) -> str | None:
        """Значение, пригодное как операнд, иначе ``None``: подсказка не операнд."""
        if not self.role_is_proven():
            return None
        return self.normalized_value


class DurationConstraint(BaseModel):
    """Одно временнОе ограничение нормы: граница + от какого события она отсчитывается.

    Норма может задать несколько границ сразу («не позднее 10 рабочих дней с даты
    регистрации заявления, но не позднее 2 рабочих дней с даты получения сведений»).
    Раньше выживала только первая (``parameters[0]``), то есть замечание по такой норме
    было бы неполным, а граница от другого события — невидимой.
    """

    value: str | None = None
    normalized_value: float | None = None
    unit: str | None = None
    calendar_kind: str | None = None
    qualifier: str | None = None
    start_event: FrameSlot | None = None
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    evidence_span_ids: list[str] = Field(default_factory=list)


class RegulatedVariable(BaseModel):
    """Типизированная переменная, значение которой устанавливает норма."""

    kind: RegulatedVariableKind = RegulatedVariableKind.UNKNOWN
    value: str | float | int | None = None
    normalized_value: str | float | int | None = None
    unit: str | None = None
    qualifier: str | None = None
    start_event: FrameSlot | None = None
    completion_event: FrameSlot | None = None
    target_system: FrameSlot | None = None
    target_system_candidates: list[FrameSlot] = Field(default_factory=list)
    duration: FrameSlot | None = None
    calendar_kind: str | None = None
    duration_constraints: list[DurationConstraint] = Field(default_factory=list)
    registry_target: FrameSlot | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    provenance: FrameProvenance = FrameProvenance.LEGACY
    evidence_span_ids: list[str] = Field(default_factory=list)


class NormFrame(BaseModel):
    """Общий schema-v1 контракт семантики project atom и reference norm."""

    schema_version: Literal[1] = Field(default=NORM_FRAME_SCHEMA_VERSION, frozen=True)
    rule: str = Field(default="", description="Самостоятельная формулировка нормы; интерпретация, не дословная цитата")
    topic: str = Field(default="", description="Предмет регулирования нормы")
    evidence: FrameEvidence = Field(default_factory=FrameEvidence)
    tree_path: list[TreePathElement] = Field(default_factory=list)
    address_space: AddressSpace = AddressSpace.UNKNOWN
    source_role: SourceRole = SourceRole.UNKNOWN
    measure: FrameSlot = Field(default_factory=FrameSlot)
    service: FrameSlot = Field(default_factory=FrameSlot)
    subservice: FrameSlot = Field(default_factory=FrameSlot)
    action: FrameSlot = Field(default_factory=FrameSlot)
    object: FrameSlot = Field(default_factory=FrameSlot)
    procedure_stage: FrameSlot = Field(default_factory=FrameSlot)
    deadline_scope: FrameSlot | None = None
    channel: FrameSlot = Field(default_factory=FrameSlot)
    regulated_variable: RegulatedVariable = Field(default_factory=RegulatedVariable)
    modality: FrameSlot = Field(default_factory=FrameSlot)
    polarity: FrameSlot = Field(default_factory=FrameSlot)


class FrameOperandEvidence(BaseModel):
    """Минимальный receipt operand, на котором основано hard relation."""

    dimension: str
    provenance: FrameProvenance
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_span_ids: list[str] = Field(min_length=1)
    source_kinds: list[EvidenceSourceKind] = Field(min_length=1)
    grounding_statuses: list[GroundingStatus] = Field(min_length=1)

    @model_validator(mode="after")
    def _hard_operand_is_grounded(self) -> FrameOperandEvidence:
        if self.provenance == FrameProvenance.LEGACY:
            raise ValueError("legacy provenance cannot support a hard frame relation")
        if not (len(self.evidence_span_ids) == len(self.source_kinds) == len(self.grounding_statuses)):
            raise ValueError("hard frame operand evidence arrays must have equal lengths")
        if any(
            status != GroundingStatus.EXACT
            and not (status == GroundingStatus.LEGACY_LOCAL and source_kind == EvidenceSourceKind.REFERENCE_NORM)
            for source_kind, status in zip(self.source_kinds, self.grounding_statuses, strict=True)
        ):
            raise ValueError("hard frame operands cannot use ambiguous or unresolved evidence")
        return self


def content_fingerprint(text: str) -> str:
    """Identity исходного content без утверждения о его глобальных координатах."""
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
