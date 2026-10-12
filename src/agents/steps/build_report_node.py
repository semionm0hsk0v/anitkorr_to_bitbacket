from src.agents.state import PipelineState
from src.agents.steps_tools.authority.verdict import stops_early
from src.agents.steps_tools.report.assemble import assemble_review, body_paragraphs, summary_lines
from src.agents.steps_tools.review.progress import ui_outcome
from src.agents.utils.streaming import FRAGMENT_BUILD_REPORT, emit_ui_event, progress_text


async def build_report_node(state: PipelineState) -> dict:
    """Собрать заключение из результатов проверок и оформить находки и справку."""
    competence = state.get("competence_review")
    early_stop = stops_early(competence)
    review = assemble_review(
        competence=competence,
        internal=state.get("internal_review"),
        compliance=state.get("legal_review"),
        repeal=state.get("repeal_review"),
        early_stop=early_stop,
    )
    body = body_paragraphs(review)
    summary = summary_lines(review, analysis_date=state.get("analysis_date", ""), early_stop=early_stop)
    emit_ui_event(
        state,
        "build_report",
        FRAGMENT_BUILD_REPORT,
        status="success",
        content="\n\n".join(body) if body else progress_text("review_report_empty"),
        findings_shown=len(review.findings),
        outcome=ui_outcome(review),
    )
    return {"review_result": review, "review_summary": "\n".join(summary)}
