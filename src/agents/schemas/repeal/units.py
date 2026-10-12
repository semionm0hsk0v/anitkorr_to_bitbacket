from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.enums.repeal.units import UnitRole, UnitScopeReason, UnitScopeStatus

BLOCKING_UNIT_SCOPE_REASONS: frozenset[UnitScopeReason] = frozenset(
    {
        UnitScopeReason.BASE_IDENTITY_NOT_EXACT,
        UnitScopeReason.SOURCE_TEXT_INCOMPLETE,
        UnitScopeReason.UNIT_SEQUENCE_NOT_FOUND,
        UnitScopeReason.UNIT_SEQUENCE_HAS_GAPS,
        UnitScopeReason.UNRESOLVED_UNITS_PRESENT,
        UnitScopeReason.PROJECT_UNIT_NOT_LOCATED,
        UnitScopeReason.PROJECT_UNIT_NOT_PARSED,
        UnitScopeReason.NO_UNIT_BINDS_BASE,
    }
)


class BaseActIdentity(BaseModel):
    "Реквизиты базового акта, по которым пункт признаётся относящимся к нему."

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str
    date: str
    kind: str = ""
    issuer: str = ""

    @property
    def exact(self) -> bool:
        """Достаточно ли реквизитов для положительного вывода об объёме."""
        return bool(self.number and self.date and self.kind)


class AmenderUnit(BaseModel):
    """Один разобранный пункт редактора с доказанными границами."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    role: UnitRole
    #: Границы пункта в переданном тексте: от своей метки до метки следующего пункта.
    start: int
    end: int
    #: Каким положительным доказательством установлена связь с базой.
    relation_proof: str = ""


class UnitScopeAssessment(BaseModel):
    """Типизированный объём отмены редактора относительно ОДНОЙ базы."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: UnitScopeStatus
    reason: UnitScopeReason
    #: Пункты, связь которых с базой доказана.
    bound_units: tuple[str, ...] = ()
    #: Из них — вошедшие в объём отмены проекта.
    covered_units: tuple[str, ...] = ()
    #: Из них — не вошедшие: материал замечания о неполноте.
    uncovered_units: tuple[str, ...] = ()

    unresolved_units: tuple[str, ...] = ()
    #: Все разобранные пункты — для квитанции и компактной трассы.
    units: tuple[AmenderUnit, ...] = ()

    @property
    def blocking(self) -> bool:
        """Пустой результат по этой оценке не является доказанным нулём."""
        return self.reason in BLOCKING_UNIT_SCOPE_REASONS
