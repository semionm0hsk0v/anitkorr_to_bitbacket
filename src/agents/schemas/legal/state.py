from typing import Annotated

from src.agents.llm.usage import UsageSnapshot, merge_stage_usage
from src.agents.schemas.legal.discovery import ReviewQuestion, SearchQuery
from src.agents.schemas.legal.inventory import NamedAct
from src.agents.schemas.legal.search import LegalSearchResult
from src.agents.state import PipelineState


class LegalReviewState(PipelineState, total=False):
    """Промежуточные каналы остаются внутри legal-ветви."""

    legal_stage_usage: Annotated[dict[str, UsageSnapshot], merge_stage_usage]

    named_acts: list[NamedAct]
    search_queries: list[SearchQuery]
    legal_questions: list[ReviewQuestion]
    legal_search_result: LegalSearchResult
    legal_skip_reason: str
