from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.agents.schemas.legal_base.source_refs import ActKind


class CitedAct(BaseModel):
    """Явная ссылка проекта; код проверяет реквизиты по дословной quote."""

    model_config = ConfigDict(extra="forbid")

    kind: ActKind = Field(description="Вид акта из фиксированного словаря")
    quote: str = Field(description="Дословный фрагмент шапки/преамбулы, содержащий номер и дату акта")


class CompetenceParse(BaseModel):
    """Реквизиты проекта и ссылки для подбора источников полномочий."""

    model_config = ConfigDict(extra="forbid")

    project_title: str = Field(default="", description="Название самого проекта дословно из шапки; неизвестно — пусто")
    issuer: str = Field(description="Орган-издатель дословно из шапки")
    region: str = Field(description="Субъект РФ дословно из реквизитов издателя; неизвестен — пусто")
    form: Literal["приказ", "постановление", "распоряжение", "указ", "закон", "иное"]
    action: Literal["approve", "amend", "repeal", "establish", "other"] = Field(
        description="approve — утверждает; amend — вносит изменения; repeal — признаёт утратившим силу или отменяет; "
        "establish — устанавливает или создаёт; other — иное"
    )
    target_act: CitedAct | None = Field(description="Для amend/repeal — изменяемый или отменяемый акт; иначе null")
    cited_acts: list[CitedAct] = Field(description="Акты преамбулы, на основании которых издаётся проект, в порядке текста")
