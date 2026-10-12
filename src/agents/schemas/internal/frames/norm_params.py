from pydantic import BaseModel, Field

from src.agents.enums.internal.frames.norm_params import CalendarKind, ParameterType, Qualifier


class Parameter(BaseModel):
    """Числовой/измеримый параметр нормы (срок, сумма, возраст, доля)."""

    type: ParameterType = Field(description="Вид величины")
    value: str = Field(description="Значение как в тексте (нормализуется постобработкой)")
    unit: str | None = Field(default=None, description="«дней» | «лет» | «руб.» | «%» | …")
    calendar_kind: CalendarKind | None = Field(default=None, description="Рабочие или календарные дни (только у срока)")
    qualifier: Qualifier = Field(default=Qualifier.EXACT)
    norm_value: float | None = Field(default=None)
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
