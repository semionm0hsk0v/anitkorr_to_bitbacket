"""Публикация итога проверки полномочий и материалов для следующих этапов."""

from src.agents.schemas.common.review import LegalSource
from src.agents.schemas.competence.review import CompetenceReview
from src.agents.schemas.competence.sources import CompetenceRefs
from src.agents.state import PipelineState
from src.agents.steps_tools.authority.verdict import stops_early
from src.agents.steps_tools.review.progress import stage_summary, ui_outcome
from src.agents.utils.streaming import FRAGMENT_CHECK_AUTHORITY, emit_ui_event
from src.common.utils.logger import get_logger

log = get_logger(__name__)


async def finish_authority_review(
    state: PipelineState,
    review: CompetenceReview,
    sources_by_id: dict[str, LegalSource],
    competence_refs: CompetenceRefs | None,
    dropped_refs: int,
) -> dict:
    """Опубликовать итог этапа и вернуть накопленные материалы на любом пути завершения."""
    early_stop = stops_early(review)
    await log.ainfo(
        "check_authority",
        scope=review.scope.value,
        findings=len(review.findings),
        early_stop=early_stop,
        dropped_refs=dropped_refs,
    )
    emit_ui_event(
        state,
        "check_authority",
        FRAGMENT_CHECK_AUTHORITY,
        status="success",
        content=stage_summary(review, early_stop=early_stop),
        outcome=ui_outcome(review),
    )
    return {
        "competence_review": review,
        "legal_sources": list(sources_by_id.values()),
        "competence_refs": competence_refs,
    }
