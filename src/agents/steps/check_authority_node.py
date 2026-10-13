from src.agents.llm import llm
from src.agents.prompts import describe_prompt, get_prompt
from src.agents.schemas.competence.parse import CompetenceParse
from src.agents.state import PipelineState
from src.agents.steps_tools.authority.evidence import assessment_schema, block_pool
from src.agents.steps_tools.authority.fragments import review_limitations, select_source_texts, source_block
from src.agents.steps_tools.authority.progress import finish_authority_review
from src.agents.steps_tools.authority.requisites import (
    form_fact,
    parse_input,
    project_title_from_header,
    refs_from_header,
    refs_from_model,
)
from src.agents.steps_tools.authority.sources import authority_refs, collect_competence_sources, source_priority
from src.agents.steps_tools.authority.verdict import (
    competence_verdict,
    stops_early,
    unchecked_competence_review,
    unconfirmed_early_stop,
)
from src.agents.steps_tools.review.failures import failure_reason
from src.agents.steps_tools.sources.source_reading import legal_sources_from_readings
from src.agents.steps_tools.sources.transport_barrier import is_external_transport_failure
from src.agents.utils.streaming import FRAGMENT_CHECK_AUTHORITY, emit_ui_event
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)


async def check_authority_node(state: PipelineState) -> dict:  # noqa: PLR0915 - explicit calls and failure boundaries
    """Проверить полномочия по прочитанным нормам и сохранить материалы для следующих шагов."""
    emit_ui_event(state, "check_authority", FRAGMENT_CHECK_AUTHORITY, status="in_progress", content="Проверка компетенции органа")

    document_text = state["document_text"]
    sources_by_id = {source.id: source for source in state.get("legal_sources", [])}
    competence_refs, dropped_refs = None, 0

    operation = "competence_parse"
    try:
        header = parse_input(state.get("parsed_document"))
        if not header.strip():
            review = unchecked_competence_review("Не получена шапка проекта для проверки компетенции.")
            return await finish_authority_review(state, review, sources_by_id, competence_refs, dropped_refs)

        competence_refs = refs_from_header(header)
        if competence_refs is None:
            template = get_prompt("competence_parse")
            project_title = project_title_from_header(header)
            messages = template.compile(header=header, project_title_extracted="да" if project_title else "нет")
            parsed_requisites = (
                await llm.generate_structured(
                    role="competence_parse",
                    messages=messages,
                    prompt=describe_prompt(template),
                    schema=CompetenceParse,
                    operation="competence_parse",
                    retries=0,
                )
            ).value
            if project_title:
                parsed_requisites = parsed_requisites.model_copy(update={"project_title": project_title})
            competence_refs, dropped_refs = refs_from_model(parsed_requisites, header)

        # Прочитанные источники сохраняются и при последующем сбое проверки.
        operation = "competence_sources"
        competence_sources = await collect_competence_sources(
            state.get("mcp_request_sessions"),
            competence_refs,
            analysis_date=state.get("analysis_date"),
            timeout_s=float(settings["agents.competence.read_timeout_s"]),
            authority_refs=authority_refs(competence_refs, header),
        )
        for source in legal_sources_from_readings(competence_sources.readings):
            sources_by_id.setdefault(source.id, source)

        operation = "competence_review"
        budget_chars = int(settings["agents.competence.verdict_input_chars"])
        source_views = select_source_texts(
            sorted(competence_sources.readings, key=lambda reading: source_priority(reading.ref)),
            project_chars=len(document_text),
            budget_chars=budget_chars,
            issuer=competence_sources.statute,
        )
        limitations = review_limitations(competence_refs, competence_sources, source_views)

        if not any(view.text for view in source_views):
            review = unchecked_competence_review(" ".join([settings["agents.competence.review.no_source_text"], *limitations]))
            return await finish_authority_review(state, review, sources_by_id, competence_refs, dropped_refs)
        if len(document_text) + sum(len(view.text) for view in source_views) > budget_chars:
            review = unchecked_competence_review(settings["agents.competence.review.input_over_budget"])
            return await finish_authority_review(state, review, sources_by_id, competence_refs, dropped_refs)

        blocks, annotated_texts = block_pool(document_text, source_views)
        source_blocks = [source_block(view, annotated_texts[view.reading.source_id]) for view in source_views if view.text]
        template = get_prompt("competence_review")
        messages = template.compile(
            document=annotated_texts["project"],
            analysis_date=state.get("analysis_date", ""),
            issuer=competence_refs.issuer,
            form=competence_refs.form or "не установлен",
            form_fact=form_fact(competence_refs),
            sources="\n\n".join(source_blocks),
            limitations="\n".join(f"- {text}" for text in limitations) or "- нет",
        )
        assessment = (
            await llm.generate_structured(
                role="competence",
                messages=messages,
                prompt=describe_prompt(template),
                schema=assessment_schema(blocks),
                operation="competence_review",
                retries=0,
            )
        ).value
        review = competence_verdict(
            assessment, blocks=blocks, document=document_text, views=source_views, limitations=limitations
        )
        if stops_early(review):
            # A false whole-act rejection would suppress every later check.
            # The second reviewer sees the same evidence, without the first conclusion.
            operation = "competence_confirmation"
            confirmation = (
                await llm.generate_structured(
                    role="competence_confirm",
                    messages=messages,
                    prompt=describe_prompt(template),
                    schema=assessment_schema(blocks),
                    operation=operation,
                    retries=0,
                )
            ).value
            confirmed = competence_verdict(
                confirmation, blocks=blocks, document=document_text, views=source_views, limitations=limitations
            )
            review = confirmed if stops_early(confirmed) else unconfirmed_early_stop(confirmed)
    except Exception as exc:
        await log.awarning(f"{operation}_failed", error_class=type(exc).__name__)
        if operation == "competence_parse":
            reason = f"Не выполнен разбор шапки проекта. {failure_reason(exc)}"
        elif operation == "competence_sources":
            reason = (
                settings["agents.competence.review.sources_transport_failure"]
                if is_external_transport_failure(exc)
                else "Технический сбой получения источников полномочий."
            )
        elif operation == "competence_confirmation":
            reason = "Не выполнено подтверждение отсутствия полномочий на весь акт; ранняя остановка отменена. " + failure_reason(
                exc
            )
        else:
            reason = failure_reason(exc)
        review = unchecked_competence_review(reason)

    return await finish_authority_review(state, review, sources_by_id, competence_refs, dropped_refs)
