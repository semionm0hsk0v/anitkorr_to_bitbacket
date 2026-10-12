from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.schemas.common.review import PROJECT_SOURCE_ID, Citation


class SettledAnswer(BaseModel):
    """Ответ после заземления цитат: действующее значение и подтверждённые цитаты."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    answer: str
    explanation: str
    recommendation: str
    citations: tuple[Citation, ...]
    extent: str | None = None
    downgraded: bool = False

    @property
    def source_ids(self) -> frozenset[str]:
        return frozenset(c.source for c in self.citations if c.source != PROJECT_SOURCE_ID)
