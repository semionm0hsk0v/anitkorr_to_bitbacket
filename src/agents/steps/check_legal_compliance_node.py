import asyncio

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.schemas.common.branch_output import ComplianceBranchOutput
from src.agents.schemas.common.review import Review
from src.agents.schemas.legal.state import LegalReviewState
from src.agents.schemas.legal.verdict import SourceBlock
from src.agents.steps_tools.compliance.deduplication import merge_duplicate_findings
from src.agents.steps_tools.compliance.verdict import assemble_compliance_review, build_source_blocks, check_source_blocks
from src.agents.steps_tools.review.progress import stage_summary, ui_outcome
from src.agents.steps_tools.review.settle import not_checked, worse
from src.agents.utils.streaming import FRAGMENT_COMPLIANCE_REVIEW, emit_ui_event
from src.common.config.config import settings
from src.common.utils.concurrency import gather_results
from src.common.utils.logger import get_logger

log = get_logger(__name__)


async def check_legal_compliance_node(state: LegalReviewState) -> ComplianceBranchOutput:
    """Сверить проект с вышестоящими актами, подтвердить цитаты и собрать результат."""
    deadline = asyncio.get_running_loop().time() + float(settings["agents.legal.review.timeout_s"])
    kinds = (RiskKind.COMPLIANCE,)
    document = state["document_text"]
    analysis_date = state.get("analysis_date", "")
    found = state.get("legal_search_result")
    source_blocks: list[SourceBlock] = []
    limitations: list[str] = []
    if found is not None:
        limitations = [*found.plan.limitations, *found.sources.limitations, *found.context.limitations]
        source_blocks = build_source_blocks(found.sources.readings)

    if found is None or not source_blocks:
        reason = (
            state["legal_skip_reason"]
            if found is None
            else " ".join([settings["agents.legal.review.no_compliance_source"], *limitations])
        )
        review = Review(findings=[], checks=not_checked(kinds, reason))
    else:
        context_sources = found.context.texts()
        citation_sources = found.sources.texts()
        related_source_text = "\n\n".join(f"source_id: {source_id}\n{text}" for source_id, text in context_sources.items())
        competence_refs = state.get("competence_refs")
        prompt_variables = {
            "document": document,
            "analysis_date": analysis_date,
            "region": competence_refs.region if competence_refs else "",
            "issuer": competence_refs.issuer if competence_refs else "",
            "project_title": competence_refs.project_title if competence_refs else "",
            "mentioned_acts": "\n".join(f"- {act.raw or act.label}" for act in state.get("named_acts", []))
            or "В проекте акты не найдены.",
            "explanatory_note": "\n\n".join(d["text"] for d in state.get("context_documents", [])),
        }
        # Independent general, scope and closed-refusal reviews share full evidence.
        # Procedural findings must not crowd out the limits of access to the service.
        batches = await gather_results(
            check_source_blocks(
                prompt_variables,
                source_blocks,
                timeout_s=float(settings["llm.legal_verdict.timeout"]),
                related_sources=related_source_text,
            ),
            check_source_blocks(
                prompt_variables,
                source_blocks,
                timeout_s=float(settings["llm.legal_scope.timeout"]),
                related_sources=related_source_text,
                role="legal_scope",
            ),
            check_source_blocks(
                prompt_variables,
                source_blocks,
                timeout_s=float(settings["llm.legal_refusal.timeout"]),
                related_sources=related_source_text,
                role="legal_refusal",
            ),
        )
        block_results = [result for batch in batches for result in batch]
        review = assemble_compliance_review(
            block_results,
            limitations,
            document=document,
            sources=citation_sources,
        )
        try:
            async with asyncio.timeout(max(0, deadline - asyncio.get_running_loop().time())):
                review = await merge_duplicate_findings(review)
        except TimeoutError:
            reason = "Смысловое объединение повторов не завершено в пределах времени этапа; все замечания сохранены."
            review = Review(
                findings=review.findings,
                checks=[
                    check.model_copy(
                        update={
                            "status": worse(check.status, CheckStatus.LIMITED),
                            "reason": " ".join(filter(None, (check.reason, reason))),
                        }
                    )
                    for check in review.checks
                ],
            )
    await log.ainfo(
        "check_legal_compliance",
        findings=len(review.findings),
        statuses=[check.status.value for check in review.checks],
    )
    emit_ui_event(
        state,
        "check_legal_compliance",
        FRAGMENT_COMPLIANCE_REVIEW,
        status="success",
        content=stage_summary(review),
        outcome=ui_outcome(review),
    )
    return {"legal_review": review}
