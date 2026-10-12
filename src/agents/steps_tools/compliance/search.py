from __future__ import annotations

import asyncio
from collections.abc import Sequence
from itertools import combinations
from typing import Any

from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.schemas.legal.discovery import DiscoveryOutcome, SearchQuery
from src.agents.schemas.legal.inventory import NamedAct
from src.agents.schemas.legal.norm import Norm
from src.agents.schemas.legal_base.act_refs import ActRequisites
from src.agents.schemas.legal_base.source_refs import CARD_VID_BY_KIND, SourceRef
from src.agents.steps_tools.compliance.plan import identity
from src.agents.steps_tools.sources.act_refs import find_act_requisites, kind_from_requisites, parse_act_requisites
from src.agents.steps_tools.sources.card_search import card_search, quick_search
from src.agents.steps_tools.sources.normalization import card_date
from src.agents.steps_tools.sources.selection import act_banks, card_queries, hit_matches_requisites, pick_hit
from src.agents.steps_tools.sources.transport_barrier import ensure_consultant_session_alive, is_external_transport_failure
from src.common.config.config import settings


async def search_named_acts[ActRef: SourceRef](
    acts: Sequence[ActRef], sessions: Any, *, region: str
) -> tuple[list[Norm], list[ActRef], list[str]]:
    """Найти ссылки проекта по реквизитам; не создавать Norm для ненайденного акта."""
    # Один акт ищем один раз независимо от формулировки ссылки; цитаты сохраняем отдельно.
    keys = [
        identity(act, region=region) if act.number and act.date else (act.kind, act.number, act.date, act.title, act.raw)
        for act in acts
    ]
    unique_acts: dict[tuple[str, ...], ActRef] = {}
    for key, act in zip(keys, acts, strict=True):
        unique_acts.setdefault(key, act)
    async with asyncio.TaskGroup() as group:
        searches = {key: group.create_task(_search_named_act(act, sessions, region=region)) for key, act in unique_acts.items()}
    norms: list[Norm] = []
    unresolved: list[ActRef] = []
    limitations: list[str] = []
    for act, key in zip(acts, keys, strict=True):
        found, missing, limits = searches[key].result()
        norms.extend(
            norm.model_copy(update={"role": act.role, "origin": act.origin, "quote": getattr(act, "quote", "")}) for norm in found
        )
        if missing:
            unresolved.append(act)
        limitations.extend(limits)
    return norms, unresolved, list(dict.fromkeys(limitations))


async def _search_named_act[ActRef: SourceRef](
    act: ActRef, sessions: Any, *, region: str
) -> tuple[list[Norm], list[ActRef], list[str]]:
    norms: list[Norm] = []
    unresolved: list[ActRef] = []
    limitations: list[str] = []
    if not act.number or not act.date:
        unresolved.append(act)
        limitations.append(f"Не выполнен поиск {act.label}: неполные реквизиты.")
        return norms, unresolved, limitations
    mentioned_requisites = find_act_requisites(act.raw)
    source_region = mentioned_requisites[0].region if mentioned_requisites else ""
    requisites = ActRequisites(
        kind=CARD_VID_BY_KIND[act.kind],
        number=act.number,
        date=card_date(act.date),
        title=act.title,
        raw=act.raw,
        federal=act.federal,
        region="" if act.federal else source_region or region,
    )
    banks = act_banks(requisites, region)
    if not banks:
        unresolved.append(act)
        limitations.append(f"Не выполнен поиск {act.label}: региональный банк не определён.")
        return norms, unresolved, limitations
    found = None
    try:
        for query in card_queries(requisites, banks):
            hits = await card_search(sessions, query)
            if hits is None:
                limitations.append(f"Карточный поиск недоступен: {act.label}.")
                break
            matching = [hit for hit in hits if hit_matches_requisites(hit, requisites, banks)]
            # Ссылка проекта должна подтвердиться с исходной датой, без подмены редакцией другого акта.
            matching = [
                hit
                for hit in matching
                if any(
                    req.card_date == requisites.card_date
                    for req in find_act_requisites(hit.title.splitlines()[0] if hit.title else "")
                )
            ]
            found = pick_hit(matching, requisites)
            if found is not None:
                break
    except Exception as exc:
        if not is_external_transport_failure(exc):
            raise
        limitations.append(f"Сбой поиска {act.label} ({type(exc).__name__}).")
    if found is None:
        unresolved.append(act)
        limitations.append(f"Не найден в К+ акт по реквизитам: {act.label}.")
        return norms, unresolved, limitations
    norm = Norm(
        **act.model_dump(include=set(SourceRef.model_fields)),
        quote=act.quote if isinstance(act, NamedAct) else "",
        bank=found.bank,
        doc_num=found.doc_num,
    )
    norms.append(norm.model_copy(update={"title": found.title}))
    return norms, unresolved, limitations


async def search_discovery(queries: Sequence[SearchQuery], sessions: Any) -> DiscoveryOutcome:
    """Выполнить предметные запросы и собрать кандидатов из выдачи К+."""
    if not queries:
        return DiscoveryOutcome(limitations=("Предметный поиск не выполнялся: запросы отсутствуют.",))
    try:
        ensure_consultant_session_alive(sessions)
    except Exception as exc:
        if not is_external_transport_failure(exc):
            raise
        return DiscoveryOutcome(limitations=(f"Не выполнен предметный поиск ({type(exc).__name__}).",))
    norms: list[Norm] = []
    limitations = []
    unique_queries: dict[str, tuple[SearchQuery, int]] = {}
    primary_limit = int(settings["agents.legal.search_results_per_query"])
    relaxed_limit = int(settings["agents.legal.relaxed_results_per_query"])
    for question in queries:
        terms = [question.core_term, *question.general_terms] if question.core_term else question.general_terms
        # Ослабляем уточнения, сохраняя опорное действие/вид регулирования.
        # Дополнительная выдача меньше основной: расширение не должно вытеснять точный поиск.
        relaxed = (
            (
                part
                for size in range(len(question.general_terms) - 1, 0, -1)
                for part in combinations(question.general_terms, size)
            )
            if question.core_term
            else ()
        )
        texts = [
            (question.query, primary_limit),
            (" ".join(terms), primary_limit),
            *((" ".join([question.core_term, *part]), relaxed_limit) for part in relaxed),
        ]
        for text, limit in texts:
            if not text:
                continue
            previous = unique_queries.get(text)
            reason = _merge_search_context(previous[0].reason, question.reason) if previous else question.reason
            unique_queries[text] = (SearchQuery(query=text, reason=reason), max(previous[1], limit) if previous else limit)

    async with asyncio.TaskGroup() as group:
        searches = [group.create_task(_discover_query(query, sessions, limit=limit)) for query, limit in unique_queries.values()]
    # Порядок определяется планом, даже если ответы пришли в другом порядке.
    for search in searches:
        candidates, query_limits = search.result()
        norms.extend(candidates)
        limitations.extend(query_limits)
    norms = unique_norms(norms)
    try:
        ensure_consultant_session_alive(sessions)
    except Exception as exc:
        if not is_external_transport_failure(exc):
            raise
        limitations.append("Сессия правовой базы недоступна после предметного поиска.")
    return DiscoveryOutcome(norms=norms, limitations=tuple(dict.fromkeys(limitations)))


async def _discover_query(query: SearchQuery, sessions: Any, *, limit: int) -> tuple[list[Norm], list[str]]:
    try:
        hits = await quick_search(sessions, query.query, limit=limit)
    except Exception as exc:
        if not is_external_transport_failure(exc):
            raise
        return [], [f"Сбой предметного поиска L1 ({type(exc).__name__}): {query.query}."]
    if hits is None:
        return [], [f"Поиск L1 недоступен: {query.query}."]
    limitations = [f"Поиск L1 ограничен первыми {limit} карточками: {query.query}."] if len(hits) >= limit else []
    norms = []
    for hit in hits:
        header = next(iter(hit.title.splitlines()), "")
        req = parse_act_requisites(header)
        if req is None or not req.date:
            limitations.append(f"Реквизиты поискового кандидата L1 не разобраны: {hit.bank}:{hit.doc_num}.")
            continue
        norm = Norm(
            kind=kind_from_requisites(req),
            number=req.number,
            date=card_date(req.date),
            title=hit.title,
            raw=req.raw,
            role=SourceRole.CITED,
            origin=SourceOrigin.SEARCH,
            bank=hit.bank,
            doc_num=hit.doc_num,
            search_query=query.query,
            search_reason=query.reason,
        )
        norms.append(norm)
    return norms, limitations


def _merge_search_context(first: str, second: str) -> str:
    return "\n".join(dict.fromkeys(part for text in (first, second) for part in text.splitlines() if part))


def _merge_quotes(first: str, second: str) -> str:
    if not second or second in first:
        return first
    if not first or first in second:
        return second
    return f"{first}\n\n{second}"


def unique_norms(norms: list[Norm]) -> list[Norm]:
    """Один документ К+ хранится один раз; цитата проекта сохраняется при объединении."""
    unique: dict[tuple[str, int], Norm] = {}
    for norm in norms:
        key = norm.bank, norm.doc_num
        previous = unique.get(key)
        if previous is None:
            unique[key] = norm
            continue
        preferred = norm if norm.quote and not previous.quote else previous
        unique[key] = preferred.model_copy(
            update={
                "quote": _merge_quotes(previous.quote, norm.quote),
                "search_query": _merge_search_context(previous.search_query, norm.search_query),
                "search_reason": _merge_search_context(previous.search_reason, norm.search_reason),
            }
        )
    return list(unique.values())
