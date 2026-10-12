from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.schemas.common.review import Check, Review, Risk
from src.agents.steps_tools.repeal.clauses import collect_repeal_targets
from src.agents.steps_tools.repeal.detector import detect_orphan_amendments_with_status
from src.agents.steps_tools.repeal.review import detector_check, repeal_findings
from src.agents.steps_tools.review.progress import stage_summary, ui_outcome
from src.agents.steps_tools.sources.card_search import consultant_capabilities
from src.agents.steps_tools.sources.region_banks import region_from_text
from src.agents.steps_tools.sources.transport_barrier import is_external_transport_failure
from src.agents.utils.streaming import FRAGMENT_CHECK_INCOMPLETE_REPEAL, emit_ui_event, progress_text
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)


async def check_incomplete_repeal_node(state) -> dict:
    """Проверить полноту отмены и вернуть подтверждённые находки и статус проверки."""
    emit_ui_event(
        state, "check_incomplete_repeal", FRAGMENT_CHECK_INCOMPLETE_REPEAL, status="in_progress", content=progress_text("repeal")
    )
    parsed_document = state.get("parsed_document")
    sessions = state.get("mcp_request_sessions")
    findings: list[Risk] = []
    try:
        if parsed_document is None:
            check = Check(
                kind=RiskKind.REPEAL,
                status=CheckStatus.NOT_CHECKED,
                reason="Структура проекта не распознана; детектор отмены не запускался.",
            )
        else:
            targets = collect_repeal_targets(parsed_document.tree)
            if not targets:
                check = Check(kind=RiskKind.REPEAL, status=CheckStatus.COMPLETED)
            elif not consultant_capabilities(sessions).exact_retrieval:
                check = Check(
                    kind=RiskKind.REPEAL,
                    status=CheckStatus.NOT_CHECKED,
                    reason="Точные операции Консультант+ недоступны; детектор отмены не запускался.",
                )
            else:
                detection = await detect_orphan_amendments_with_status(
                    sessions,
                    targets,
                    region=region_from_text(parsed_document.enriched_text),
                    analysis_date=state.get("analysis_date"),
                )
                findings, held = repeal_findings(detection, state["document_text"])
                check = detector_check(detection, held=held)
    except Exception as exc:
        transport_failed = is_external_transport_failure(exc)
        await log.awarning(
            "repeal_transport_failed" if transport_failed else "repeal_detector_failed", error_class=type(exc).__name__
        )
        findings = []
        check = Check(
            kind=RiskKind.REPEAL,
            status=CheckStatus.NOT_CHECKED,
            reason=(
                "Сбой соединения с Консультант+; детектор отмены не завершил проверку."
                if transport_failed
                else "Технический сбой детектора отмены."
            ),
        )

    review = Review(findings=findings, checks=[check])
    await log.ainfo("repeal", findings=len(findings), status=check.status.value)
    emit_ui_event(
        state,
        "check_incomplete_repeal",
        FRAGMENT_CHECK_INCOMPLETE_REPEAL,
        status="success",
        content=stage_summary(review, labels={RiskKind.REPEAL: settings["agents.report.detector_label"]}),
        outcome=ui_outcome(review),
    )
    return {"repeal_review": review}
