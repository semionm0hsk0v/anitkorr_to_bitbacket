"""Группы уже допущенных находок; модель не создаёт новые юридические выводы."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class FindingGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    members: list[int] = Field(min_length=1, description="Номера записей, полностью покрываемых объяснением представителя, с 1")
    representative: int = Field(ge=1, description="Номер исходной записи, описывающей все самостоятельные нарушения группы")
    reason: str = Field(min_length=1, description="Смысловое совпадение или полное включение; иначе самостоятельный риск")


class FindingGroups(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_strict: ClassVar[bool] = True

    groups: list[FindingGroup] = Field(description="Все исходные записи ровно по одному разу; сомнительные совпадения раздельно")
