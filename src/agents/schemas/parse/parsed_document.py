from __future__ import annotations

from pydantic import BaseModel, Field

from src.agents.schemas.parse.structure import DocumentStructureTree


class ActBasisRef(BaseModel):
    """Ссылка из преамбулы (акт-основание / якорный акт)."""

    raw: str
    number: str | None = None  # «835», «67-ФЗ», …
    date: str | None = None
    role: str = "basis"  # basis | mention


class ActBasis(BaseModel):
    """Акт-основание из преамбулы (ось B, приоритет 1)."""

    enacting_formula: str = ""  # «приказываю» / «постановляю»
    preamble_text: str = ""
    basis_refs: list[ActBasisRef] = Field(default_factory=list)


class ParsedDocument(BaseModel):
    """Артефакт стадии 1."""

    enriched_text: str
    tree: DocumentStructureTree
    act_basis: ActBasis | None = None
    table_cell_count: int = 0  # smoke-метрика «таблицы без потерь»
