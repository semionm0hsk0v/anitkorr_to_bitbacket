from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.agents.schemas.repeal.evidence import RepealChainEvidence, RepealRedundantEvidence
from src.agents.schemas.repeal.units import UnitScopeAssessment


class RepealTarget(BaseModel):
    """Акт (или его подпункт), который проект признаёт утратившим силу."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str
    date: str | None = None
    is_subpoint: bool = False
    subpoint: str | None = None  # «1» для «пункт 1 приказа … № 44-п»
    raw: str = ""
    title: str = ""  # хвост после номера до «;» — материал для К+-запроса (не точный заголовок)

    kind: str = ""


class Amender(BaseModel):
    """Акт, которым в базовый акт вносились изменения (из сносок К+ или обратных ссылок)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str
    date: str | None = None
    raw: str = ""

    source: Literal["annotation", "links"] = "annotation"


class UnitScopeUnknown(BaseModel):
    """NamedAct-level amendment is proven, but project coverage of its unit is not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    amender: Amender
    project_targets: tuple[RepealTarget, ...]


class UnitScopeGap(BaseModel):
    """Редактор, чьи относящиеся к базе положения проект снял НЕ полностью."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    amender: Amender
    assessment: UnitScopeAssessment


class OrphanResult(BaseModel):
    """Результат диффа по одному базовому акту."""

    model_config = ConfigDict(extra="forbid")

    base_number: str
    base_date: str = ""
    orphans: list[Amender] = Field(default_factory=list)

    unit_scope_unknown: list[UnitScopeUnknown] = Field(default_factory=list)

    unit_incomplete: list[UnitScopeGap] = Field(default_factory=list)
    # Место отмены в проекте.
    clause_quote: str = ""  # полный собранный текст пункта-отмены
    clause_marker_text: str = ""  # текст узла-маркера (вербатим из проекта — для цитаты-гейта)
    #: Типизированное доказательство цепочки; без него находка не публикуется.
    evidence: RepealChainEvidence | None = None


class RedundantRepealResult(BaseModel):
    """Положение, которое проект отменяет повторно: оно уже утратило силу."""

    model_config = ConfigDict(extra="forbid")

    target_number: str
    target_date: str = ""
    subpoint: str = ""
    #: Реквизиты акта, которым положение было признано утратившим силу (если названы).
    repealed_by_number: str = ""
    repealed_by_date: str = ""
    clause_quote: str = ""
    clause_marker_text: str = ""
    #: Типизированное доказательство статуса; без него находка не публикуется.
    evidence: RepealRedundantEvidence | None = None


class RepealDetectionResult(BaseModel):
    """Findings plus explicit incompleteness of the deterministic provider scan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    results: list[OrphanResult] = Field(default_factory=list)

    redundant: list[RedundantRepealResult] = Field(default_factory=list)

    unit_incomplete: list[OrphanResult] = Field(default_factory=list)
    blocked_bases: tuple[str, ...] = ()

    blocked_subpoints: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not (self.blocked_bases or self.blocked_subpoints)
