from __future__ import annotations

from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from src.agents.schemas.legal.norm import Norm


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(
        min_length=2, max_length=160, description="Короткий предметный запрос; регион и названия объектов допустимы"
    )
    reason: str = Field(min_length=1, description="Связь поиска с положением проекта, одна фраза из 5–10 слов")
    core_term: str = Field(
        default="",
        pattern=r"^\S*$",
        description="Опорное слово: вид обязательного регулирования или юридическое действие источника, не имя объекта",
    )
    general_terms: list[Annotated[str, Field(pattern=r"^\S+$")]] = Field(
        default_factory=list,
        max_length=3,
        description="До трёх отдельных слов, уточняющих родовой предмет и регион опорного термина",
    )


class ReviewQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, description="Название самостоятельной проверки содержания, 4–8 слов")
    project_blocks: list[str] = Field(
        max_length=2, description="ID одного–двух характерных фрагментов document, раскрывающих вопрос"
    )


class DiscoveryPlan(BaseModel):
    provider_strict: ClassVar[bool] = True
    model_config = ConfigDict(extra="forbid")
    questions: list[ReviewQuestion] = Field(default_factory=list)
    objects: list[ReviewQuestion] = Field(
        default_factory=list,
        description="Отдельные вопросы официальных наименований, состава и статуса нормативно перечисленных объектов",
    )
    queries: list[SearchQuery] = Field(max_length=6)


class DiscoveryOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    norms: list[Norm] = Field(default_factory=list)
    limitations: tuple[str, ...] = ()
