import asyncio
import json

from src.agents.llm import llm
from src.agents.llm.errors import LLMGenerationError
from src.agents.prompts import describe_prompt, get_prompt
from src.agents.schemas.legal.plan import PlanDecision, SourceSelection
from src.agents.schemas.legal.search import LegalSearchResult
from src.agents.steps_tools.compliance.plan import (
    accept_source_selection,
    prepare_candidates,
    project_issuer_key,
    project_repeal_targets,
    selection_card,
    selection_material,
)
from src.agents.steps_tools.compliance.search import search_discovery, search_named_acts, unique_norms
from src.agents.steps_tools.compliance.sources import read_legal_sources, summarize_readings
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)


async def retrieve_legal_sources_node(state) -> dict:
    """Найти кандидатов в К+, отобрать применимые акты и прочитать их тексты."""
    document_text = state["document_text"]
    parsed_document = state.get("parsed_document")
    analysis_date = state.get("analysis_date")
    consultant_session = state.get("mcp_request_sessions")
    competence_refs = state.get("competence_refs")
    region = competence_refs.region if competence_refs else ""
    issuer = competence_refs.issuer if competence_refs else ""
    issuer_key = project_issuer_key(issuer, region)
    named_acts = state["named_acts"]

    # Параллельно ищем акты по явным ссылкам проекта и предметным запросам предыдущего шага.
    named_result, discovery = await asyncio.gather(
        search_named_acts(named_acts, consultant_session, region=region),
        search_discovery(state["search_queries"], consultant_session),
    )
    # Собираем единый инвентарь кандидатов, сохраняя ограничения обоих поисков.
    norms, unresolved, search_limits = named_result
    norms = unique_norms([*norms, *discovery.norms])
    search_limits.extend(discovery.limitations)

    repeal_targets = project_repeal_targets(parsed_document)
    candidates, context_candidates, early_decisions, early_limits = prepare_candidates(
        norms, document_text, project_issuer=issuer_key, region=region, repeal_targets=repeal_targets
    )

    # Совместный отбор видит весь набор: самостоятельные требования и контекст не дублируются.
    inventory = []
    for i, norm in enumerate([*candidates, *(s.ref for s in context_candidates)], 1):
        candidate = selection_card(norm, document_text)
        candidate["candidate_id"] = i
        candidate["purpose"] = "requirements" if i <= len(candidates) else "context"
        inventory.append(candidate)
    # Сначала прямые связи с исходным проектом; ни одна карточка не удаляется.
    # ID остаются привязаны к исходным candidates независимо от порядка показа.
    inventory.sort(key=lambda item: (item["purpose"] == "context", not item.get("project_mentions"), not item.get("quote")))
    selection = None
    if inventory:
        questions = state.get("legal_questions", [])
        material = selection_material(
            document_text,
            [question.project_blocks for question in questions],
            [act.quote for act in named_acts],
        )
        template = get_prompt("legal_selection")
        messages = template.compile(
            document=material,
            analysis_date=analysis_date or "",
            project_title=competence_refs.project_title if competence_refs else "",
            mentioned_acts="\n".join(f"- {act.raw or act.label}" for act in named_acts) or "В проекте акты не найдены.",
            explanatory_note="\n\n".join(d["text"] for d in state.get("context_documents", [])),
            issuer=" ".join(issuer.split()) or "не определён",
            region=" ".join(region.split()) or "не определён",
            target_acts=int(settings["agents.legal.target_acts"]),
            questions=json.dumps([question.model_dump() for question in questions], ensure_ascii=False),
            inventory=json.dumps(inventory, ensure_ascii=False),
        )
        try:
            selection = (
                await llm.generate_structured(
                    role="legal_selection",
                    messages=messages,
                    prompt=describe_prompt(template),
                    schema=SourceSelection,
                    operation="legal_selection",
                    retries=max(0, int(settings["agents.legal.llm_retries"])),
                )
            ).value
        except LLMGenerationError as exc:
            await log.awarning("legal_selection_failed", error_kind=exc.kind)
    selected_acts = accept_source_selection(
        selection,
        candidates,
        document_text,
        context=context_candidates,
        repeal_targets=repeal_targets,
    )
    selected_acts = selected_acts.model_copy(
        update={
            "norms": norms,
            "unresolved_acts": unresolved,
            "dropped": selected_acts.dropped + sum(d.outcome == "dropped" for d in early_decisions),
            "decisions": (
                *early_decisions,
                *selected_acts.decisions,
                *(PlanDecision(outcome="unresolved", reason="named_act_not_found", ref=act) for act in unresolved),
            ),
            "limitations": tuple(dict.fromkeys([*search_limits, *early_limits, *selected_acts.limitations])),
        }
    )
    await log.ainfo(
        "legal_selection", source=selected_acts.source, accepted=len(selected_acts.sources), dropped=selected_acts.dropped
    )

    # Оба назначения читаются вместе: один документ — одна загрузка, общий таймаут.
    readings = await read_legal_sources(
        consultant_session,
        (*selected_acts.sources, *selected_acts.context_sources),
        region=region,
        analysis_date=analysis_date,
        timeout_s=float(settings["agents.legal.read_timeout_s"]),
    )

    source_count = len(selected_acts.sources)
    sources = summarize_readings(readings.readings[:source_count])
    context = summarize_readings(readings.readings[source_count:])

    return {"legal_search_result": LegalSearchResult(plan=selected_acts, sources=sources, context=context)}
