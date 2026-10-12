from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.schemas.common.review import LegalSource
from src.agents.schemas.legal.norm import Norm
from src.agents.schemas.legal_base.act_refs import ActRequisites
from src.agents.schemas.legal_base.currentness import ReferenceCoverageStatus, ReferenceCurrentnessReason
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.agents.schemas.legal_base.source_refs import CARD_VID_BY_KIND, SourceRef
from src.agents.steps_tools.sources import retrieval
from src.agents.steps_tools.sources.currentness import confirmed_current_text, has_currentness_evidence
from src.agents.steps_tools.sources.normalization import card_date
from src.agents.steps_tools.sources.transport_barrier import (
    consultant_pool_lost,
    consultant_transport_failures,
    ensure_consultant_session_alive,
    is_external_transport_failure,
)
from src.common.utils.logger import get_logger

log = get_logger(__name__)

_NOT_THIS_ACT = frozenset(
    {ReferenceCurrentnessReason.IDENTITY_REQUISITE_MISMATCH, ReferenceCurrentnessReason.TITLE_IDENTITY_MISMATCH}
)


def _requisites(ref: SourceRef, region: str) -> ActRequisites:
    return ActRequisites(
        kind=CARD_VID_BY_KIND[ref.kind],
        number=ref.number,
        date=card_date(ref.date),
        region="" if ref.federal else region,
        federal=ref.federal,
        title=ref.title,
        raw=ref.raw or ref.label,
    )


_STATUS_SEARCH_REVOKED = "утратил силу или отменен"


_STATUS_SEARCH_ACTIVE_PREFIX = "все акты, кроме утративших силу"


def classify_status_search(value: str | None) -> str:
    """``"active"`` / ``"revoked"`` / ``"unknown"`` — единственное место интерпретации."""
    normalized = " ".join(str(value or "").split()).casefold()
    if not normalized:
        return "unknown"
    if normalized == _STATUS_SEARCH_REVOKED:
        return "revoked"
    if normalized.startswith(_STATUS_SEARCH_ACTIVE_PREFIX):
        return "active"
    return "unknown"


def classify_reading(ref: SourceRef, raw: Any | None) -> SourceReading:
    "Сырой акт → исход чтения по доверию к редакции, действию акта и полноте текста (§6.3)."
    if raw is None:
        return SourceReading(ref=ref, status=ReadingStatus.NOT_FOUND)
    receipt = raw.currentness_receipt
    if receipt is not None and receipt.reason in _NOT_THIS_ACT:
        return SourceReading(ref=ref, status=ReadingStatus.IDENTITY_MISMATCH)
    version = raw.version
    if not has_currentness_evidence(raw) or version is None:
        return SourceReading(ref=ref, status=ReadingStatus.STATUS_ONLY, raw=raw)
    status_search = classify_status_search(getattr(raw, "status_search", None))
    if status_search == "revoked":
        return SourceReading(ref=ref, status=ReadingStatus.NOT_IN_FORCE, raw=raw)
    if status_search == "unknown":
        return SourceReading(ref=ref, status=ReadingStatus.STATUS_ONLY, raw=raw)
    coverage = version.coverage
    if coverage.status is ReferenceCoverageStatus.COMPLETE:
        return SourceReading(ref=ref, status=ReadingStatus.COMPLETE, raw=raw)
    if coverage.status is ReferenceCoverageStatus.TRUNCATED_CONFIGURED:
        fetched = max((r.end for r in coverage.fetched_ranges), default=0)
        return SourceReading(
            ref=ref,
            status=ReadingStatus.TRUNCATED,
            raw=raw,
            fetched_paragraphs=fetched,
            total_paragraphs=version.par_count or None,
        )
    return SourceReading(ref=ref, status=ReadingStatus.STATUS_ONLY, raw=raw)


async def _read(sessions: Any, ref: SourceRef, *, region: str, analysis_date: str | None, max_pars: int) -> SourceReading:
    requisites = _requisites(ref, region)
    try:
        ensure_consultant_session_alive(sessions)
        if isinstance(ref, Norm):
            raw = await retrieval.load_act(
                sessions,
                ref.bank,
                ref.doc_num,
                ref.title,
                requisites.raw,
                requisites=requisites,
                region=region,
                analysis_date=analysis_date,
                max_pars=max_pars,
            )
        else:
            raw = await retrieval.retrieve_act(
                sessions,
                requisites.raw,
                requisites=requisites,
                region=region,
                analysis_date=analysis_date,
                max_pars=max_pars,
            )
        # Also catch a session loss hidden by an optional metadata operation.
        ensure_consultant_session_alive(sessions)
    except Exception as exc:
        if not is_external_transport_failure(exc):
            raise
        await log.awarning("competence_source_transport_failure", role=ref.role.value, error_class=type(exc).__name__)
        return SourceReading(ref=ref, status=ReadingStatus.TRANSPORT_FAILURE)
    return classify_reading(ref, raw)


async def _read_with_retry(
    sessions: Any,
    ref: SourceRef,
    *,
    region: str,
    analysis_date: str | None,
    max_pars: int,
    attempt_s: float,
    attempts: int,
    deadline: float,
) -> SourceReading:
    """Чтение с ограниченными повторами и таймаутом."""
    for attempt in range(1, attempts + 1):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        if attempt > 1 and remaining < attempt_s / 2:
            break
        try:
            return await asyncio.wait_for(
                _read(sessions, ref, region=region, analysis_date=analysis_date, max_pars=max_pars),
                timeout=min(attempt_s, remaining),
            )
        except TimeoutError:
            lost = consultant_pool_lost(sessions)
            exhausted = remaining < attempt_s
            await log.awarning("competence_source_attempt_timeout", role=ref.role.value, attempt=attempt, attempt_s=attempt_s)
            if lost:
                return SourceReading(ref=ref, status=ReadingStatus.TRANSPORT_FAILURE)
            if exhausted:
                break
    return SourceReading(ref=ref, status=ReadingStatus.DEADLINE)


async def read_all(
    sessions: Any,
    refs: Sequence[SourceRef],
    *,
    region: str,
    analysis_date: str | None,
    max_pars: int,
    timeout_s: float,
    attempt_s: float,
    attempts: int,
) -> list[SourceReading]:
    "Параллельное чтение; не успевшее к дедлайну — ``DEADLINE``, успевшее сохраняется."
    if not refs:
        return []
    if timeout_s <= 0:
        return [SourceReading(ref=ref, status=ReadingStatus.DEADLINE) for ref in refs]
    deadline = time.perf_counter() + timeout_s
    tasks = [
        asyncio.create_task(
            _read_with_retry(
                sessions,
                ref,
                region=region,
                analysis_date=analysis_date,
                max_pars=max_pars,
                attempt_s=attempt_s,
                attempts=attempts,
                deadline=deadline,
            )
        )
        for ref in refs
    ]
    losses_before = len(consultant_transport_failures(sessions))
    try:
        _, pending = await asyncio.wait(tasks, timeout=timeout_s)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    cut_off = (
        ReadingStatus.TRANSPORT_FAILURE
        if consultant_pool_lost(sessions) and len(consultant_transport_failures(sessions)) > losses_before
        else ReadingStatus.DEADLINE
    )
    return [
        SourceReading(ref=ref, status=cut_off) if task in pending else task.result()
        for ref, task in zip(refs, tasks, strict=True)
    ]


def legal_source_from_act(raw: Any) -> LegalSource:
    "A fetched law as the request's source map entry; text only for a confirmed current reading."
    key = f"{raw.base}:{raw.doc_num}"
    receipt = raw.currentness_receipt
    if confirmed_current_text(raw):
        observed = receipt.observed if receipt is not None else None
        facts: dict[str, Any] = {}
        if observed is not None:
            facts = {
                name: value
                for name, value in {
                    "edition": observed.edition,
                    "issued": observed.issued,
                    "in_force": observed.in_force,
                    "par_count": observed.par_count or None,
                    "coverage": observed.coverage.status.value,
                }.items()
                if value is not None
            }
        return LegalSource(
            id=key,
            text=raw.full_text,
            reference=f"{raw.title} (Консультант+, {key}; прочитан при проверке компетенции)",
            edition={"currentness": "CURRENT", **facts},
        )
    edition: dict[str, Any] = {"currentness": receipt.outcome.value if receipt is not None else "UNKNOWN"}
    if receipt is not None:
        edition["reason"] = receipt.reason.value
    return LegalSource(
        id=key,
        text="",
        reference=f"{raw.title} (Консультант+, {key}; текст не передан: действующая редакция не подтверждена)",
        edition=edition,
    )


def legal_sources_from_readings(readings: Sequence[SourceReading]) -> list[LegalSource]:
    """Прочитанные акты и их подтверждённый статус — в карту источников запроса."""
    sources: dict[str, LegalSource] = {}
    for reading in readings:
        if reading.raw is not None:
            source = legal_source_from_act(reading.raw)
            sources.setdefault(source.id, source)
    return list(sources.values())
