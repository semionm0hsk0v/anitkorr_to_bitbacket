from __future__ import annotations

from src.agents.schemas.common.context_document import ContextDocument
from src.agents.state import PipelineState
from src.agents.steps_tools.sources.currentness import current_analysis_date


def assemble_initial_state(
    *,
    source_name: str,
    document_text: str,
    user_message: str | None = None,
    analysis_date: str | None = None,
    context_documents: list[ContextDocument] | None = None,
    table_geometry: dict | None = None,
    extra: dict | None = None,
) -> PipelineState:
    "Каноническое стартовое состояние из уже извлечённого текста."

    state: PipelineState = {
        "source_name": source_name,
        "document_text": document_text,
        "context_documents": list(context_documents or []),
        "user_message": user_message,
        "analysis_date": analysis_date or current_analysis_date(),
    }
    if table_geometry is not None:
        state["table_geometry"] = table_geometry
    if extra:
        state.update(extra)  # type: ignore[typeddict-item]
    return state
