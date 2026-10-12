from src.agents.llm import llm
from src.agents.prompts import describe_prompt, get_prompt
from src.agents.schemas.legal.discovery import DiscoveryPlan
from src.agents.steps_tools.compliance.plan import extract_cited_acts, project_blocks
from src.agents.utils.streaming import FRAGMENT_COMPLIANCE_REVIEW, emit_ui_event, progress_text
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)


async def plan_legal_search_node(state) -> dict:
    """Выделить вопросы сверки, явные ссылки и запросы для поиска остальных актов."""
    emit_ui_event(
        state, "plan_legal_search", FRAGMENT_COMPLIANCE_REVIEW, status="in_progress", content=progress_text("review_legal")
    )

    # Явные ссылки проекта — начальные кандидаты для сверки.
    document_text = state["document_text"]
    cited_acts = extract_cited_acts(document_text)
    search_queries = []
    questions = []

    # Регион и издатель уже установлены на этапе проверки компетенции.
    competence_refs = state.get("competence_refs")
    region = competence_refs.region if competence_refs else ""
    issuer = competence_refs.issuer if competence_refs else ""

    # Модель готовит запросы для поиска актов, не названных в проекте.
    try:
        template = get_prompt("legal_discovery")
        messages = template.compile(
            document="\n".join(f"⟦{identity}⟧\n{text}" for identity, text in project_blocks(document_text).items()),
            analysis_date=state.get("analysis_date", ""),
            project_title=competence_refs.project_title if competence_refs else "",
            mentioned_acts="\n".join(f"- {act.raw or act.label}" for act in cited_acts) or "В проекте акты не найдены.",
            explanatory_note="\n\n".join(d["text"] for d in state.get("context_documents", [])),
            issuer=" ".join(issuer.split()) or "не определён",
            region=" ".join(region.split()) or "не определён",
        )
        discovery_plan = (
            await llm.generate_structured(
                role="legal_plan",
                messages=messages,
                prompt=describe_prompt(template),
                schema=DiscoveryPlan,
                operation="legal_discovery",
                retries=max(0, int(settings["agents.legal.llm_retries"])),
            )
        ).value
    except Exception as exc:
        # Продолжаем поиск по явным ссылкам проекта.
        await log.awarning("legal_discovery_preparation_failed", error_class=type(exc).__name__)
    else:
        search_queries = discovery_plan.queries
        questions = [*discovery_plan.questions, *discovery_plan.objects]

    return {
        "named_acts": cited_acts,
        "search_queries": search_queries,
        "legal_questions": questions,
    }
