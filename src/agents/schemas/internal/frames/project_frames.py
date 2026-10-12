from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.agents.schemas.internal.addressing.address_space import AddressBook
from src.agents.schemas.internal.frames.norm_frame import NormFrame
from src.agents.schemas.internal.frames.norm_params import Parameter
from src.agents.schemas.parse.structure import DocumentStructureNode


class BuiltProjectFrame(BaseModel):
    "Подготовленный фрагмент: его фрейм, числовые операнды и след адресации."

    model_config = ConfigDict(extra="forbid")

    leaf: DocumentStructureNode
    ancestors: tuple[DocumentStructureNode, ...]
    frame: NormFrame
    parameters: list[Parameter] = Field(default_factory=list)


class SubjectDesignation(BaseModel):
    "Одно обозначение предмета акта с собственными координатами производителя."

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str
    char_start: int | None = None
    char_end: int | None = None


class DocumentFrameContext(BaseModel):
    "Данные документа, ОДИНАКОВЫЕ для всех его фрагментов, вычисленные один раз."

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_text: str
    doc_title: str
    fingerprint: str
    address_book: AddressBook
    measure: SubjectDesignation | None
    service: SubjectDesignation | None
