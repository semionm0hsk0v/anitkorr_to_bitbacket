"""Поиск и получение сырого акта с подтверждением текущей редакции и полноты загрузки."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from src.agents.schemas.legal_base.act_refs import ActRequisites
from src.agents.schemas.legal_base.currentness import (
    ReferenceCheckResult,
    ReferenceContentCoverage,
    ReferenceCoverageStatus,
    ReferenceCurrentnessOutcome,
    ReferenceCurrentnessReason,
    ReferenceCurrentnessReceipt,
    ReferenceParagraphRange,
    ReferenceSourceCoordinates,
)
from src.agents.schemas.legal_base.retrieval import CardPathOutcome, RawAct, RawReferenceCacheEnvelope
from src.agents.steps_tools.sources.act_refs import parse_act_requisites
from src.agents.steps_tools.sources.card_search import (
    card_search,
    fetch_doc_info_with_transport,
    fetch_doc_text_with_transport,
)
from src.agents.steps_tools.sources.currentness import (
    decide_live_currentness,
    reference_metadata_fingerprint,
)
from src.agents.steps_tools.sources.raw_cache import (
    cache_candidates,
    read_raw_cache,
    try_write_cache,
)
from src.agents.steps_tools.sources.selection import (
    act_banks,
    act_hygiene_reject,
    candidate_score,
    card_queries,
    expected_number,
    fallback_queries,
    hit_matches_requisites,
    items_have_number,
    keep_fit_hits,
    keep_fit_items,
    pick_hit,
    query_tokens,
)
from src.agents.steps_tools.sources.transport_barrier import is_external_transport_failure
from src.agents.steps_tools.sources.trust import register_trusted_currentness_receipt
from src.agents.steps_tools.sources.version_evidence import (
    authoritative_in_force,
    build_live_evidence,
    build_reference_version,
    edition_from_title,
    issued_from_info,
    live_identity_checks,
    live_version_checks,
    raw_act_from_version,
    status_search_of,
)
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)

_PAGE = int(settings["agents.legal_base.retrieval.page"])  # параграфов за один get_document_text
_MAX_PARS = int(settings["agents.legal_base.retrieval.max_pars"])  # верхняя граница объёма текста акта (бюджет латентности)


def _extract_items(found: Any) -> list | None:
    return found if isinstance(found, list) else (found.get("results") if isinstance(found, dict) else None)


async def _fetch_pages(
    sessions: Any, base: str, doc_num: int, end: int
) -> tuple[list[str], list[ReferenceParagraphRange], bool, str | None]:
    "Параллельно прочитать страницы ``1..end``, сохранив порядок и непрерывное покрытие."
    pages = []
    async with asyncio.TaskGroup() as group:
        for start in range(1, end + 1, _PAGE):
            page_end = min(start + _PAGE - 1, end)
            task = group.create_task(fetch_doc_text_with_transport(sessions, base, doc_num, start - 1, page_end))
            pages.append((start, page_end, task))

    parts: list[str] = []
    ranges: list[ReferenceParagraphRange] = []
    transport: str | None = None
    for start, page_end, task in pages:
        seg, page_transport = task.result()
        transport = page_transport or transport
        if seg is None:
            return parts, ranges, True, transport
        if seg:
            parts.append(seg)
        ranges.append(ReferenceParagraphRange(start=start, end=page_end))
    return parts, ranges, False, transport


def _register_currentness(receipt: ReferenceCurrentnessReceipt) -> None:
    if receipt.outcome is ReferenceCurrentnessOutcome.CURRENT:
        register_trusted_currentness_receipt(receipt)


async def _quick_search_items(sessions: Any, query: str, *, limit: int) -> list[dict[str, Any]]:
    "Free search over the one supported transport (MCP)."
    if sessions is None:
        raise ConnectionError("Consultant free-search transport is unavailable")
    found = await sessions.invoke(
        "consultant",
        "consultant_quick_search",
        {"query": query, "max_results": limit, "get_title": True},
        timeout_s=60,
    )
    return _extract_items(found) or []


async def retrieve_act(
    sessions: Any,
    query: str,
    *,
    use_cache: bool = True,
    max_pars: int = _MAX_PARS,
    prefer_k: int = 1,
    requisites: ActRequisites | None = None,
    region: str = "",
    analysis_date: str | None = None,
) -> RawAct | None:
    "Запрос → сырой акт (действующая редакция) или ``None`` (→ BLOCKED у вызывающего)."
    k = max(prefer_k, 1)
    req = requisites if requisites is not None else (parse_act_requisites(query) if k == 1 else None)

    home_region = region or (req.region if req is not None else "")
    if req is not None and req.number:
        card_outcome = await retrieve_act_by_requisites(
            sessions,
            req,
            query=query,
            region=region,
            use_cache=use_cache,
            max_pars=max_pars,
            analysis_date=analysis_date,
        )
        if card_outcome.act is not None:
            return card_outcome.act
        if not req.federal:
            await log.ainfo("regional_card_path_failed_closed", number=req.card_number, banks=list(act_banks(req, region)))
            return None
        if card_outcome.ambiguous:
            return None

    try:
        found_items = await _quick_search_items(sessions, query, limit=max(3, k))
    except Exception as exc:  # сбой MCP / нет интерактивной авторизации → BLOCKED (не ретраим)
        if sessions is None or is_external_transport_failure(exc):
            raise
        await log.awarning("retrieve_act_search_failed", query=query[:60], error_class=type(exc).__name__, detail=str(exc))
        return None

    items = await keep_fit_items(found_items, home_region)

    exp_num = expected_number(query) if k == 1 else None
    if (not items) or (exp_num and not items_have_number(items, exp_num, max(3, k))):
        original = items
        for fb in fallback_queries(query):
            try:
                found_items = await _quick_search_items(sessions, fb, limit=max(3, k))
            except Exception as exc:  # MCP стал недоступен — прекращаем фоллбэки
                if sessions is None or is_external_transport_failure(exc):
                    raise
                break
            cand = await keep_fit_items(found_items, home_region)
            if cand and (not exp_num or items_have_number(cand, exp_num, max(3, k))):
                items = cand
                await log.ainfo("retrieve_act_fallback_hit", original=query[:50], fallback=fb[:50])
                break
        else:
            items = items or original  # фоллбэки не лучше — исходная выдача (recall-safe)
    if not items:
        await log.ainfo("retrieve_act_not_found", query=query[:60])
        return None

    if k > 1:
        qtok = query_tokens(query)
        top = max(items[:k], key=lambda it: candidate_score(it, qtok))
    else:
        top = items[0]
    lbl = top.get("doc_par_label") or {}
    base, doc_num = lbl.get("bank_name"), lbl.get("doc_num")
    title = top.get("title", "") or ""
    if not base or not doc_num:
        return None
    return await load_act(
        sessions,
        str(base),
        int(doc_num),
        title,
        query,
        use_cache=use_cache,
        max_pars=max_pars,
        region=home_region,
        requisites=req,
        analysis_date=analysis_date,
    )


async def load_act(  # noqa: PLR0915 - one ordered cache/metadata/fetch evidence transaction
    sessions: Any,
    base: str,
    doc_num: int,
    title: str,
    query: str,
    *,
    use_cache: bool = True,
    max_pars: int = _MAX_PARS,
    region: str = "",
    requisites: ActRequisites | None = None,
    analysis_date: str | None = None,
) -> RawAct | None:
    "Найденный документ (банк+номер) → ``RawAct`` с текстом действующей редакции."
    reason = act_hygiene_reject(base, title, region=region)
    if reason:
        await log.ainfo("reference_act_rejected", bank=base, doc_num=doc_num, title=title[:120], reason=reason, path="load")
        return None

    source = ReferenceSourceCoordinates(base=base, doc_num=doc_num)
    envelopes: list[RawReferenceCacheEnvelope] = []
    cache_read_failed = False
    if use_cache:
        for path in cache_candidates(base, doc_num):
            try:
                envelopes.append(read_raw_cache(path))
            except (OSError, ValidationError, ValueError) as exc:
                cache_read_failed = True
                await log.awarning(
                    "reference_cache_read_failed",
                    source_base=source.base,
                    source_doc_num=source.doc_num,
                    error_class=type(exc).__name__,
                )

    if cache_read_failed:
        _register_currentness(
            ReferenceCurrentnessReceipt(
                source=source,
                outcome=ReferenceCurrentnessOutcome.TECHNICAL_FAILURE,
                reason=ReferenceCurrentnessReason.CACHE_READ_ERROR,
            )
        )

    info, info_transport = await fetch_doc_info_with_transport(sessions, base, doc_num)
    if info is None:
        receipt = ReferenceCurrentnessReceipt(
            source=source,
            outcome=ReferenceCurrentnessOutcome.TECHNICAL_FAILURE,
            reason=ReferenceCurrentnessReason.PROVIDER_UNAVAILABLE,
            expected=envelopes[0].version if len(envelopes) == 1 else None,
        )
        _register_currentness(receipt)
        if len(envelopes) == 1:
            envelope = envelopes[0]
            return raw_act_from_version(
                query=query,
                title=envelope.title,
                content=envelope.content,
                version=envelope.version,
                receipt=receipt,
                status_search=None,  # doc_info недоступен в этом запросе — сигнала нет
            )
        return RawAct(
            query=query,
            base=source.base,
            doc_num=source.doc_num,
            title=title,
            full_text="",
            currentness_receipt=receipt,
        )

    reason = act_hygiene_reject(base, title or str(info.get("title") or ""), region=region, kinds=info.get("kinds_of_doc") or ())
    if reason:
        await log.ainfo("reference_act_rejected", bank=base, doc_num=doc_num, title=title[:120], reason=reason, path="doc_info")
        return None

    info_title = str(info.get("title") or "").strip()
    effective_title = info_title or title
    par_count = int(info.get("par_count") or 0)
    if par_count <= 0:
        version = build_reference_version(
            base=source.base,
            doc_num=source.doc_num,
            title=effective_title or title or f"{source.base}/{source.doc_num}",
            edition=str(info.get("edition") or "").strip() or edition_from_title(effective_title),
            issued=issued_from_info(info),
            in_force=authoritative_in_force(info, effective_title),
            par_count=0,
            coverage=ReferenceContentCoverage(status=ReferenceCoverageStatus.INCOMPLETE),
            content="",
        )
        receipt = ReferenceCurrentnessReceipt(
            source=source,
            outcome=ReferenceCurrentnessOutcome.UNKNOWN,
            reason=ReferenceCurrentnessReason.INCOMPLETE_METADATA,
            observed=version,
        )
        _register_currentness(receipt)
        return raw_act_from_version(
            query=query,
            title=effective_title,
            content="",
            version=version,
            receipt=receipt,
            status_search=status_search_of(info),
        )

    explicit_status = authoritative_in_force(info, effective_title)
    if explicit_status is False:
        version = build_reference_version(
            base=source.base,
            doc_num=source.doc_num,
            title=effective_title,
            edition=str(info.get("edition") or "").strip() or edition_from_title(effective_title),
            issued=issued_from_info(info),
            in_force=False,
            par_count=par_count,
            coverage=ReferenceContentCoverage(
                status=ReferenceCoverageStatus.INCOMPLETE,
                requested_ranges=[ReferenceParagraphRange(start=1, end=par_count)],
            ),
            content="",
        )
        receipt = ReferenceCurrentnessReceipt(
            source=source,
            outcome=ReferenceCurrentnessOutcome.STALE,
            reason=ReferenceCurrentnessReason.REVOKED,
            observed=version,
        )
        _register_currentness(receipt)
        return raw_act_from_version(
            query=query,
            title=effective_title,
            content="",
            version=version,
            receipt=receipt,
            status_search=status_search_of(info),
        )

    end = min(par_count, max_pars)
    metadata_change_retries = max(0, min(2, int(settings.agents.reference_currentness.metadata_change_retries)))
    fetched_at = datetime.now(UTC)  # часы читает граница ретрива, схемы остаются чистыми
    metadata_before = reference_metadata_fingerprint(info)
    metadata_after = metadata_before
    metadata_reread_ok = True
    refetch_attempts = 0

    while True:
        parts, fetched_ranges, failed, page_transport = await _fetch_pages(sessions, base, doc_num, end)
        content = "\n".join(parts)
        if failed:
            break
        after_info, _after_transport = await fetch_doc_info_with_transport(sessions, base, doc_num)
        if after_info is None:
            # Согласованность версии не подтверждена: это не «успешно», а неизвестность.
            metadata_reread_ok = False
            break
        metadata_after = reference_metadata_fingerprint(after_info)
        if metadata_after == metadata_before or refetch_attempts >= metadata_change_retries:
            break

        refetch_attempts += 1
        info = after_info
        metadata_before = metadata_after
        info_title = str(info.get("title") or "").strip()
        effective_title = info_title or title
        par_count = int(info.get("par_count") or 0)
        if par_count <= 0:
            break
        end = min(par_count, max_pars)
        await log.ainfo(
            "reference_source_changed_during_fetch",
            base=source.base,
            doc_num=source.doc_num,
            attempt=refetch_attempts,
        )

    transport = page_transport or info_transport or "consultant_unknown"
    coverage_status = (
        ReferenceCoverageStatus.INCOMPLETE
        if failed
        else ReferenceCoverageStatus.TRUNCATED_CONFIGURED
        if end < par_count
        else ReferenceCoverageStatus.COMPLETE
    )
    coverage = ReferenceContentCoverage(
        status=coverage_status,
        requested_ranges=[ReferenceParagraphRange(start=1, end=end)],
        fetched_ranges=fetched_ranges,
    )
    version = build_reference_version(
        base=source.base,
        doc_num=source.doc_num,
        title=effective_title,
        edition=str(info.get("edition") or "").strip() or edition_from_title(effective_title),
        issued=issued_from_info(info),
        in_force=explicit_status,
        par_count=par_count,
        coverage=coverage,
        content=content,
    )

    version_checks = live_version_checks(version=version, content=content, metadata_stable=metadata_after == metadata_before)
    if not metadata_reread_ok:
        version_checks.append(
            ReferenceCheckResult(name="metadata_reread", passed=False, detail="post_fetch_metadata_unavailable")
        )
    live = build_live_evidence(
        transport=transport,
        fetched_at=fetched_at,
        analysis_date=analysis_date,
        metadata_before=metadata_before,
        metadata_after=metadata_after,
        refetch_attempts=refetch_attempts,
        identity_checks=live_identity_checks(
            info=info,
            card_title=title,
            effective_title=effective_title,
            requisites=requisites,
            now=fetched_at,
        ),
        version_checks=version_checks,
    )
    outcome, reason = decide_live_currentness(version, live)
    receipt = ReferenceCurrentnessReceipt(
        outcome=outcome,
        reason=reason,
        source=source,
        live=live,
        expected=version if outcome is ReferenceCurrentnessOutcome.CURRENT else None,
        observed=version,
    )
    if use_cache and content and outcome is ReferenceCurrentnessOutcome.CURRENT:
        await try_write_cache(version=version, title=effective_title, content=content, source=source)
    _register_currentness(receipt)
    return raw_act_from_version(
        query=query,
        title=effective_title,
        content=content,
        version=version,
        receipt=receipt,
        status_search=status_search_of(info),
    )


async def retrieve_act_by_requisites(
    sessions: Any,
    req: ActRequisites,
    *,
    query: str = "",
    region: str = "",
    use_cache: bool = True,
    max_pars: int = _MAX_PARS,
    analysis_date: str | None = None,
) -> CardPathOutcome:
    "Реквизиты акта → сырой акт через КАРТОЧНЫЙ поиск, либо явный исход неудачи."
    if not req.number:
        return CardPathOutcome(act=None)
    banks = act_banks(req, region)
    ambiguous_count = 0
    for card_query in card_queries(req, banks):
        hits = await card_search(sessions, card_query)
        if hits is None:  # карточного поиска нет вовсе — лесенку не продолжаем
            await log.adebug("card_path_unavailable", number=req.number)
            return CardPathOutcome(act=None)
        hits = [hit for hit in await keep_fit_hits(hits, region or req.region) if hit_matches_requisites(hit, req, banks)]
        hit = pick_hit(hits, req)
        if hit is None:
            if not req.date and len(hits) > 1:
                ambiguous_count = max(ambiguous_count, len(hits))
            continue
        await log.ainfo(
            "card_path_hit",
            number=req.card_number,
            vid=card_query.vid_doc,
            date=card_query.date,
            banks=list(banks),
            bank=hit.bank,
            doc_num=hit.doc_num,
            title=hit.title[:80],
        )
        act = await load_act(
            sessions,
            hit.bank,
            hit.doc_num,
            hit.title,
            query or req.raw,
            use_cache=use_cache,
            max_pars=max_pars,
            region=region or req.region,
            requisites=req,
            analysis_date=analysis_date,
        )
        return CardPathOutcome(act=act)
    if ambiguous_count:
        await log.ainfo(
            "card_path_ambiguous_number", number=req.card_number, banks=list(banks), region=region, candidates=ambiguous_count
        )
        return CardPathOutcome(act=None, ambiguous_count=ambiguous_count)
    await log.ainfo("card_path_no_match", number=req.card_number, banks=list(banks), region=region)
    return CardPathOutcome(act=None)
