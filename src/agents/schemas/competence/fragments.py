from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.enums.competence.fragments import SourceMode
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.common.config.config import settings


class SourceView(BaseModel):
    """Что из одного прочитанного источника увидит модель."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reading: SourceReading
    mode: SourceMode
    text: str
    selected: int = 0
    total: int = 0

    @property
    def complete(self) -> bool:
        """Может ли источник обосновать ``completed``: полный текст и показан целиком."""
        return self.mode is SourceMode.FULL and self.reading.complete

    def limitation(self) -> str:
        label = self.reading.ref.label
        if self.mode is SourceMode.FRAGMENTS:
            return settings["agents.competence.fragments.fragmented"].format(
                label=label, selected=self.selected, total=self.total
            )
        if self.mode is SourceMode.WITHHELD:
            return (
                settings["agents.competence.fragments.no_authority_articles"]
                if self.total
                else settings["agents.competence.fragments.withheld"]
            ).format(label=label, total=self.total)
        return ""
