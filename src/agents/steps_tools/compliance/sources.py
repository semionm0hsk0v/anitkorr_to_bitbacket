from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.enums.legal_base.source_refs import SourceOrigin
from src.agents.schemas.legal.plan import PlannedSource
from src.agents.schemas.legal.sources import LegalReading, LegalSources
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.agents.steps_tools.sources.source_reading import read_all
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)


def _decided(planned: PlannedSource) -> ReadingStatus | None:
    if planned.ref.kind == "code":
        return ReadingStatus.NOT_READ_CODE
    if not planned.ref.date:
        return ReadingStatus.NO_DATE
    return None


_UNCONFIRMED_GUESS = frozenset({ReadingStatus.NOT_FOUND, ReadingStatus.IDENTITY_MISMATCH})


_MODEL_MEMORY_ERROR = _UNCONFIRMED_GUESS | {ReadingStatus.NOT_IN_FORCE}
#: Исходы, при которых документ получен и не отвергнут как чужой.
_IDENTIFIED = frozenset({ReadingStatus.COMPLETE, ReadingStatus.TRUNCATED, ReadingStatus.STATUS_ONLY, ReadingStatus.NOT_IN_FORCE})
_IDENTITY_CHECKS = frozenset({"requested_requisites", "title_identity"})


def requisites_confirmed(reading: SourceReading) -> bool:
    "Положительное доказательство тождества: успешная проверка ЗАПРОШЕННЫХ реквизитов."
    receipt = getattr(reading.raw, "currentness_receipt", None) if reading.raw is not None else None
    checks = getattr(getattr(receipt, "live", None), "identity_checks", ()) or ()
    identity = [c for c in checks if c.name in _IDENTITY_CHECKS]
    requested = [c for c in identity if c.name == "requested_requisites"]
    return (
        reading.status in _IDENTIFIED
        and bool(requested)
        and all(c.passed for c in identity)
        and all(c.detail != "no_explicit_requisites" for c in requested)
    )


def _conflict_transition(item: LegalReading) -> str:
    "Итог проверки варианта противоречивых реквизитов (L1 ``requisites_conflict``)."
    reading, others = item.reading, "; ".join(item.planned.requisites_conflict)
    label = reading.ref.label
    if requisites_confirmed(reading):
        return f"{label}: реквизиты подтверждены карточкой К+ ({reading.source_id}); другие варианты в проекте: {others}."
    if reading.status in _UNCONFIRMED_GUESS:
        return f"{label}: реквизиты не подтверждены карточкой К+, источник не используется; другие варианты в проекте: {others}."
    return f"{label}: реквизиты не проверены ({reading.status.value}); другие варианты в проекте: {others}."


async def read_legal_sources(
    sessions: Any, planned: Sequence[PlannedSource], *, region: str, analysis_date: str | None, timeout_s: float
) -> LegalSources:
    max_pars = int(settings["agents.legal.max_source_paragraphs"])
    to_read = [p for p in planned if _decided(p) is None]
    read = await read_all(
        sessions,
        [p.ref for p in to_read],
        region=region,
        analysis_date=analysis_date,
        max_pars=max_pars,
        timeout_s=timeout_s,
        attempt_s=float(settings["agents.legal.source_attempt_s"]),
        attempts=int(settings["agents.legal.source_attempts"]),
    )
    by_ref = dict(zip([id(p) for p in to_read], read, strict=True))
    readings: list[LegalReading] = []
    for p in planned:
        status = _decided(p)
        reading = SourceReading(ref=p.ref, status=status) if status is not None else by_ref[id(p)]
        readings.append(LegalReading(planned=p, reading=reading))
    await log.ainfo(
        "legal_sources",
        planned=len(planned),
        statuses=dict(Counter(r.reading.status.value for r in readings)),
        chars=sum(len(r.reading.model_text()) for r in readings),
    )
    return summarize_readings(readings)


def summarize_readings(readings: Sequence[LegalReading]) -> LegalSources:
    """Собрать ограничения только выбранного набора уже прочитанных актов."""
    limitations: list[str] = []
    for item in readings:
        if item.planned.requisites_conflict:
            limitations.append(_conflict_transition(item))
            if item.reading.status in _UNCONFIRMED_GUESS:
                continue
        if item.reading.ref.origin is SourceOrigin.MODEL and item.reading.status in _MODEL_MEMORY_ERROR:
            continue
        if item.reading.status is ReadingStatus.NOT_READ_CODE:
            limitations.append(settings["agents.legal.sources.code_limitation"].format(label=item.reading.ref.label))
        elif text := item.reading.limitation():
            limitations.append(text)
    return LegalSources(readings=tuple(readings), limitations=tuple(limitations))
