import asyncio

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.schemas.common.review import Check, Review, Risk
from src.agents.schemas.internal.detector import DetectorInput
from src.agents.steps_tools.internal.detector import scan_internal
from src.agents.steps_tools.internal.review import detector_check, internal_findings
from src.agents.steps_tools.review.progress import stage_summary, ui_outcome
from src.agents.utils.streaming import FRAGMENT_CHECK_INTERNAL_CONSISTENCY, emit_ui_event, progress_text
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)


async def check_internal_consistency_node(state) -> dict:
    """Проверить внутреннюю согласованность в пределах правил детектора без LLM."""
    emit_ui_event(
        state,
        "check_internal_consistency",
        FRAGMENT_CHECK_INTERNAL_CONSISTENCY,
        status="in_progress",
        content=progress_text("review_internal"),
    )
    parsed = state.get("parsed_document")
    findings: list[Risk] = []
    try:
        if parsed is None:
            check = Check(
                kind=RiskKind.INTERNAL,
                status=CheckStatus.NOT_CHECKED,
                reason="Структура проекта не распознана; детектор внутренних противоречий не запускался.",
            )
        else:
            document = state["document_text"]
            data = DetectorInput(
                parsed=parsed,
                document_text=document,
                source_name=state.get("source_name", ""),
                table_geometry=state.get("table_geometry"),
            )
            scan = await asyncio.to_thread(scan_internal, data)
            findings, held = internal_findings(scan, document)
            check = detector_check(scan, held=held, limit_reason=settings.agents.internal.limit_reason)
    except Exception as exc:
        await log.awarning("internal_detector_failed", error_class=type(exc).__name__)
        findings = []
        check = Check(
            kind=RiskKind.INTERNAL,
            status=CheckStatus.NOT_CHECKED,
            reason="Технический сбой детектора внутренних противоречий.",
        )

    review = Review(findings=findings, checks=[check])
    await log.ainfo("internal_detector", findings=len(findings), status=check.status.value)
    emit_ui_event(
        state,
        "check_internal_consistency",
        FRAGMENT_CHECK_INTERNAL_CONSISTENCY,
        status="success",
        content=stage_summary(review),
        outcome=ui_outcome(review),
    )
    return {"internal_review": review}
