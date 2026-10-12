from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.schemas.internal.frames.project_frames import BuiltProjectFrame


class _Fact(BaseModel):
    "Значение из рамки нормы и спаны, на которых оно стоит."

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str | int | float | bool | None
    spans: tuple[str, ...]
    named: bool = True


class _Overlap(BaseModel):
    """Общая норма о регистрации в целом охватывает частную ветку: какие факты проверить."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    general_is_left: bool
    canonical_channel: str
    scope: _Fact
    channel: _Fact


class SameVariableHit(BaseModel):
    """Кандидат: две нормы проекта с одной операцией и разными точными значениями одной переменной."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left: BuiltProjectFrame
    right: BuiltProjectFrame
    facet: str

    scope: tuple[tuple[_Fact, _Fact], ...]
    #: Общие предпосылки значения: единица, квалификатор, события, система.
    context: tuple[tuple[_Fact, _Fact], ...]
    #: Все точные значения переменной у каждой стороны (у системы их может быть несколько).
    values_a: tuple[_Fact, ...]
    values_b: tuple[_Fact, ...]
    overlap: _Overlap | None
