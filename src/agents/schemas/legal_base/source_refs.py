from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.common.config.config import settings

ActKind = Literal[
    "federal_law",
    "rf_law",
    "regional_law",
    "code",
    "rf_government_resolution",
    "regional_government_resolution",
    "governor_decree",
    "president_decree",
    "federal_order",
    "regional_order",
    "rf_directive",
    "regional_directive",
    "other",
]


FEDERAL_KINDS: frozenset[str] = frozenset(
    {"federal_law", "rf_law", "code", "rf_government_resolution", "president_decree", "federal_order", "rf_directive"}
)


CARD_VID_BY_KIND: dict[str, str] = {
    "federal_law": "Закон",
    "rf_law": "Закон",
    "regional_law": "Закон",
    "code": "",
    "rf_government_resolution": "Постановление",
    "regional_government_resolution": "Постановление",
    "governor_decree": "Указ",
    "president_decree": "Указ",
    "federal_order": "Приказ",
    "regional_order": "Приказ",
    "rf_directive": "Распоряжение",
    "regional_directive": "Распоряжение",
    "other": "",
}


class SourceRef(BaseModel):
    """Одна ссылка на источник: реквизиты, роль и происхождение.

    ``date`` — ``ДД.ММ.ГГГГ`` или пусто (тогда акт не ищется, §6.2). ``raw`` — фрагмент
    текста, из которого ссылка взята (для запроса и трассы; в лог не пишется).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ActKind
    number: str = ""
    date: str = ""
    title: str = ""
    raw: str = ""
    role: SourceRole
    origin: SourceOrigin

    @property
    def federal(self) -> bool:
        return self.kind in FEDERAL_KINDS

    @property
    def label(self) -> str:
        """«Постановление Правительства области от 16.11.2018 № 25/564-П «О …»» — для ограничений."""
        parts = [settings["agents.competence.sources.kind_labels"][self.kind]]
        if self.date:
            parts.append(f"от {self.date}")
        if self.number:
            parts.append(f"№ {self.number}")
        text = " ".join(parts)
        return f"{text} «{self.title}»" if self.title else text
