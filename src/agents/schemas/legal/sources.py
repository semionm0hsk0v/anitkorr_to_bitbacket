from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.schemas.legal.plan import PlannedSource
from src.agents.schemas.legal_base.source_reading import SourceReading


class LegalReading(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    planned: PlannedSource
    reading: SourceReading


class LegalSources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    readings: tuple[LegalReading, ...]
    limitations: tuple[str, ...]

    @property
    def with_text(self) -> list[LegalReading]:
        return [r for r in self.readings if r.reading.has_text]

    def texts(self) -> dict[str, str]:
        """Допущенные тексты для контекста и проверки цитат; первый при повторном ID."""
        texts: dict[str, str] = {}
        for item in self.with_text:
            reading = item.reading
            if reading.raw is not None:
                texts.setdefault(reading.source_id, str(reading.raw.full_text))
        return texts
