from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.agents.enums.competence.sources import ProjectAction
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.agents.schemas.legal_base.source_refs import SourceRef
from src.infrastructure.knowledge.organ_statutes import OrganStatute


class CompetenceRefs(BaseModel):
    """Всё, что C2 нужно знать о проекте: кто издаёт, что делает, на что ссылается."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_title: str = ""
    issuer: str = ""
    form: str = ""  # вид проекта: «приказ», «постановление», … (C1 или формула издания)
    action: ProjectAction = ProjectAction.OTHER
    target_act: SourceRef | None = None
    cited_acts: tuple[SourceRef, ...] = Field(default_factory=tuple)
    region: str = ""


class CompetenceSources(BaseModel):
    """Результат C2: прочитанное и перечень ограничений."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    readings: tuple[SourceReading, ...]
    limitations: tuple[str, ...]
    statute: OrganStatute | None
