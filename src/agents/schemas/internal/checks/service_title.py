from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.enums.internal.checks.service_title import ServiceTitleSourceRole


class TitleSpan(BaseModel):
    """Формула наименования услуги в тексте: координаты, узел дерева и прочитанные в ней значения."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ServiceTitleSourceRole
    char_start: int
    char_end: int
    tree_path_element_ids: tuple[str, ...]
    normalized_operations: tuple[str, ...]
    normalized_service_components: tuple[str, ...]


class TitleMismatch(BaseModel):
    """Расхождение наименования в заголовке акта и в формуле утверждения регламента."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left: TitleSpan
    right: TitleSpan
    added_operations: list[str]
    removed_operations: list[str]
    added_service_components: list[str]
    removed_service_components: list[str]


class ServiceTitleRepresentation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    span: TitleSpan
    quote: str
