from __future__ import annotations

from pydantic import Field

from src.agents.schemas.legal_base.source_refs import SourceRef


class Norm(SourceRef):
    """Документ, найденный в К+: реквизиты, координаты и сведения поиска."""

    bank: str = Field(min_length=1)
    doc_num: int = Field(gt=0)
    quote: str = ""
    search_query: str = ""
    search_reason: str = ""
