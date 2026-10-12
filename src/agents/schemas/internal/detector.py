from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from src.agents.enums.internal.detector import InternalCheck
from src.agents.schemas.internal.findings import DetectorFinding
from src.agents.schemas.internal.frames.project_frames import BuiltProjectFrame
from src.agents.schemas.parse.parsed_document import ParsedDocument


class DetectorInput(BaseModel):
    """Всё, что читают проверки: разбор проекта, его текст и геометрия таблиц PDF (если есть)."""

    model_config = ConfigDict(extra="forbid")

    parsed: ParsedDocument
    document_text: str
    source_name: str = ""
    table_geometry: dict | None = None
    _frames: list[BuiltProjectFrame] | None = PrivateAttr(default=None)


class DetectorScan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[DetectorFinding] = Field(default_factory=list)
    #: проверка → класс исключения, которым она упала
    failed: dict[InternalCheck, str] = Field(default_factory=dict)
