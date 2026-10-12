from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from src.agents.schemas.legal.inventory import NamedAct
from src.agents.schemas.legal.norm import Norm
from src.agents.schemas.legal_base.source_refs import SourceRef


class NamedActChoice(BaseModel):
    """Внутреннее решение для допуска кандидата кодом после совместного отбора."""

    provider_strict: ClassVar[bool] = True
    model_config = ConfigDict(extra="forbid")

    keep: bool = Field(description="Оставить для чтения; false — только при явном основании исключить")
    reason: str = Field(min_length=1, description="Краткая причина решения; при сомнении — что осталось неизвестным")


class SelectedAct(BaseModel):
    """Выбранная карточка и проверяемая привязка к положению проекта."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: int = Field(ge=1, description="ID карточки из inventory; реквизиты не переписывать")
    project_quote: str = Field(min_length=1, description="Дословный непрерывный фрагмент положения проекта, обычно 5–8 слов")
    reason: str = Field(min_length=1, description="Какое требование акт может устанавливать к положению, до 12 слов")


class SourceSelection(BaseModel):
    """Минимальный набор карточек; целевой объём задаётся задачей, не обрезкой ответа."""

    provider_strict: ClassVar[bool] = True
    model_config = ConfigDict(extra="forbid")

    selected: list[SelectedAct]


class PlannedSource(BaseModel):
    """Акт к чтению и сведения о конфликтующих реквизитах."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: Norm | SourceRef
    requisites_conflict: tuple[str, ...] = ()
    #: ID карточки К+, которая уже в L1 подтвердила реквизиты и название этого варианта.
    confirmed_by: str = ""


class PlanDecision(BaseModel):
    """Результат допуска или исключения источника."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: str
    reason: str
    ref: Norm | SourceRef | None = None


class PlanOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sources: tuple[PlannedSource, ...]
    context_sources: tuple[PlannedSource, ...] = ()
    norms: list[Norm] = Field(default_factory=list)
    unresolved_acts: list[NamedAct] = Field(default_factory=list)
    source: str  # "model" | "model_partial" | "parser_fallback"
    dropped: int = 0  # кандидатов, отклонённых или отложенных кодом
    limitations: tuple[str, ...] = Field(default_factory=tuple)
    decisions: tuple[PlanDecision, ...] = ()
