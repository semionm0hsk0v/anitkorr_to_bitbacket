from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from src.agents.schemas.common.context_document import ContextDocument
from src.agents.schemas.common.review import LegalSource, Review
from src.agents.schemas.competence.review import CompetenceReview
from src.agents.schemas.competence.sources import CompetenceRefs
from src.agents.schemas.parse.npa_type import NpaType
from src.agents.schemas.parse.parsed_document import ParsedDocument
from src.common.schemas.models import Message
from src.infrastructure.mcp_servers.dependencies import McpRequestSessions


class PipelineState(TypedDict, total=False):
    output_message: Message

    legal_sources: list[LegalSource]
    competence_review: CompetenceReview
    competence_refs: CompetenceRefs | None

    internal_review: Review
    repeal_review: Review  # опубликованные находки детектора и полнота его скана
    legal_review: Review  # сверка с вышестоящими актами (compliance.md), без отмены

    # Итог: четыре статуса и все находки — один результат для чата и DOCX.
    review_result: Review
    review_summary: str

    source_name: str
    source_document_id: str | None  # внешний ID исходного объекта в document storage
    document_text: str  # ТОЛЬКО текст проекта — предмета экспертизы
    table_geometry: dict | None  # PDF-derived exact rows, bound to this canonical text only
    # сопроводительные документы (пояснительная записка): контекст, не предмет
    context_documents: list[ContextDocument]
    user_message: str | None
    analysis_date: str
    mcp_request_sessions: McpRequestSessions

    parsed_document: ParsedDocument
    npa_type: NpaType

    token_usage_incomplete: Annotated[bool, operator.or_]
    token_usage_total: Annotated[int, operator.add]
    prompt_tokens_total: Annotated[int, operator.add]
    completion_tokens_total: Annotated[int, operator.add]
    cache_read_tokens_total: Annotated[int, operator.add]
