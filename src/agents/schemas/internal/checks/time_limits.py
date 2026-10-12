from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agents.enums.internal.frames.norm_params import Qualifier
from src.agents.schemas.internal.frames.project_frames import BuiltProjectFrame

PROOF_SCHEMA_VERSION: Literal[1] = 1


class ExactTimeLimitSpanV1(BaseModel):
    """Text-free exact project locator for the time-limit consistency linter."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["whole_limit", "stage_limit", "whole_scope", "stage_scope", "whole_operation", "stage_operation"]
    span_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    tree_path_element_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _range_is_nonempty(self) -> ExactTimeLimitSpanV1:
        if self.char_end <= self.char_start:
            raise ValueError("time-limit span must be nonempty")
        if not self.tree_path_element_ids or self.tree_path_element_ids[-1] != self.source_id:
            raise ValueError("time-limit source must be the terminal element of its tree path")
        return self


class TimeLimitConstraintEvidenceV1(BaseModel):
    """One exact upper-bound operand; no arithmetic conversion is inferred."""

    model_config = ConfigDict(extra="forbid")

    normalized_value: float
    unit: str = Field(min_length=1)
    calendar_kind: str = Field(min_length=1)
    qualifier: Literal[Qualifier.AT_MOST]
    start_event: str = Field(min_length=1)
    span: ExactTimeLimitSpanV1


class TimeLimitConsistencyEvidenceV1(BaseModel):
    """Proof that an inner stage declares a wider ceiling than the whole service.

    Операнды типизированы производителем: область срока каждой стороны
    (``deadline_scope``), общий операнд операции (``subservice``/``service``/
    ``measure``) и границы ``duration_constraints``. Роли сторон записаны явно, а не
    выводятся из порядка. Основание вложенности названо отдельным полем: этап
    ``stage_name`` — этап предоставления ТОЙ ЖЕ услуги, а не любая соседняя норма.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = Field(default=PROOF_SCHEMA_VERSION, frozen=True)
    operation: str = Field(min_length=1)

    operation_basis: Literal["subservice", "service", "measure", "subservice_prefix"]
    #: Названный этап нормы-части: он же основание вложенности этапа в услугу.
    stage_name: str = Field(min_length=1)
    nesting_basis: Literal["named_stage_of_the_same_service"] = "named_stage_of_the_same_service"
    whole: TimeLimitConstraintEvidenceV1
    stage: TimeLimitConstraintEvidenceV1
    whole_scope: ExactTimeLimitSpanV1
    stage_scope: ExactTimeLimitSpanV1
    whole_operation: ExactTimeLimitSpanV1
    stage_operation: ExactTimeLimitSpanV1
    additional_stage_constraints: list[TimeLimitConstraintEvidenceV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_and_assign_id(self) -> TimeLimitConsistencyEvidenceV1:
        if self.whole.span.role != "whole_limit" or self.stage.span.role != "stage_limit":
            raise ValueError("time-limit operands require directional roles")
        if (
            self.whole_scope.role != "whole_scope"
            or self.stage_scope.role != "stage_scope"
            or self.whole_operation.role != "whole_operation"
            or self.stage_operation.role != "stage_operation"
        ):
            raise ValueError("time-limit scope/operation roles are invalid")
        if (
            self.whole.unit != self.stage.unit
            or self.whole.calendar_kind != self.stage.calendar_kind
            or self.whole.start_event != self.stage.start_event
            or self.stage.normalized_value <= self.whole.normalized_value
        ):
            raise ValueError("time-limit relation prerequisites are not satisfied")
        for item in self.additional_stage_constraints:
            if (
                item.span.role != "stage_limit"
                or item.unit != self.stage.unit
                or item.calendar_kind != self.stage.calendar_kind
                or (item.start_event == self.stage.start_event and item.normalized_value == self.stage.normalized_value)
            ):
                raise ValueError("additional stage constraints must be distinct exact ceilings in the same unit/calendar")
        return self


class TimeLimitHit(BaseModel):
    """Найденное несогласование: две нормы проекта и доказательство отношения их предельных сроков."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left: BuiltProjectFrame
    right: BuiltProjectFrame
    evidence: TimeLimitConsistencyEvidenceV1
