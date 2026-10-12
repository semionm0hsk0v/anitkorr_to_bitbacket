from __future__ import annotations

from src.agents.schemas.legal_base.source_refs import SourceRef


class NamedAct(SourceRef):
    """Акт, названный в проекте: реквизиты и цитата упоминания."""

    quote: str = ""
