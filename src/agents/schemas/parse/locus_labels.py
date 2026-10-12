from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LocusLabel(BaseModel):
    """Одно сокращение и его развёрнутая запись в двух падежах (``{}`` — значение метки)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    abbreviation: str
    nominative: str
    genitive: str

    value_pattern: str = r"(.+)$"
