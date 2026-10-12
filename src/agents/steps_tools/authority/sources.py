"""Подбор актов о полномочиях, приоритет источников и получение их текстов."""

from __future__ import annotations

import asyncio
from collections import Counter
from itertools import pairwise
from typing import Any

from src.agents.enums.competence.sources import ProjectAction
from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.schemas.competence.sources import CompetenceRefs, CompetenceSources
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.agents.schemas.legal_base.source_refs import CARD_VID_BY_KIND, SourceRef
from src.agents.steps_tools.compliance.search import search_named_acts
from src.agents.steps_tools.sources.normalization import card_date, card_number_identity
from src.agents.steps_tools.sources.source_reading import read_all
from src.common.config.config import settings
from src.common.utils.logger import get_logger
from src.infrastructure.knowledge.organ_statutes import OrganStatute, find_organ_statute, load_organ_statutes

log = get_logger(__name__)


#: Порядок чтения актов преамбулы (§6.1, п. 3): законы, постановления, указы, приказы.
_CITED_ORDER: dict[str, int] = {
    "federal_law": 0,
    "rf_law": 0,
    "regional_law": 0,
    "rf_government_resolution": 1,
    "regional_government_resolution": 1,
    "governor_decree": 2,
    "president_decree": 2,
    "federal_order": 3,
    "regional_order": 3,
    "rf_directive": 1,
    "regional_directive": 1,
    "other": 4,
}
#: Название, по которому акт преамбулы узнаётся как Положение об органе (§6.1, п. 1).
_STATUTE_TITLE_MARKERS = ("о министерстве", "положения о", "положение о")


def _identity(ref: SourceRef) -> tuple[str, str, str]:
    return (CARD_VID_BY_KIND[ref.kind], card_number_identity(ref.number), card_date(ref.date))


def _statute_ref(row: OrganStatute) -> SourceRef:
    statute = row.statute
    region = load_organ_statutes().region
    return SourceRef(
        kind="regional_government_resolution",
        number=statute.number,
        date=statute.date,
        title=statute.title,
        raw=f"постановление Правительства {_genitive_region(region)} от {statute.date} № {statute.number} «{statute.title}»",
        role=SourceRole.STATUTE,
        origin=SourceOrigin.CATALOG,
    )


def _genitive_region(region: str) -> str:
    """«Ульяновская область» → «Ульяновской области» — для текста ссылки на Положение."""
    words = region.split()
    if len(words) == 2 and words[0].endswith("ская") and words[1] == "область":
        return f"{words[0][:-2]}ой области"
    return region


def _is_statute_of(ref: SourceRef, row: OrganStatute | None) -> bool:
    """Акт преамбулы — Положение об органе-издателе (§6.1, п. 1)."""
    if ref.kind != "regional_government_resolution":
        return False
    if row is not None and _identity(ref) == _identity(_statute_ref(row)):
        return True
    title = " ".join(ref.title.lower().replace("ё", "е").split())
    if not any(marker in title for marker in _STATUTE_TITLE_MARKERS):
        return False
    # Положение о ДРУГОМ органе источником полномочий издателя не является.
    return row is None or find_organ_statute(ref.title) == row


def compose_sources(
    refs: CompetenceRefs, *, max_sources: int, authority_refs: tuple[SourceRef, ...] = ()
) -> tuple[list[SourceReading], list[SourceRef], OrganStatute | None]:
    """Состав набора по §6.1: (заранее решённые исходы, что читать — по приоритету, строка справочника)."""
    row = find_organ_statute(refs.issuer)
    cited = list(refs.cited_acts)
    statute = next((ref for ref in cited if _is_statute_of(ref, row)), None)
    if statute is not None:
        cited.remove(statute)
        statute = statute.model_copy(update={"role": SourceRole.STATUTE})
    elif row is not None:
        statute = _statute_ref(row)

    ordered: list[SourceRef] = [statute] if statute is not None else []
    if refs.target_act is not None and refs.action in {ProjectAction.AMEND, ProjectAction.REPEAL}:
        ordered.append(refs.target_act)
    ordered.extend(authority_refs)
    ordered.extend(sorted(cited, key=lambda ref: _CITED_ORDER.get(ref.kind, len(_CITED_ORDER))))

    decided: list[SourceReading] = []
    to_read: list[SourceRef] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in ordered:
        key = _identity(ref)
        if ref.number and key in seen:
            continue
        seen.add(key)
        if ref.kind == "code":
            decided.append(SourceReading(ref=ref, status=ReadingStatus.NOT_READ_CODE))
        elif not ref.date or not ref.number:
            decided.append(SourceReading(ref=ref, status=ReadingStatus.NO_DATE))
        elif len(to_read) >= max_sources:
            decided.append(SourceReading(ref=ref, status=ReadingStatus.OVER_LIMIT, limit=max_sources))
        else:
            to_read.append(ref)
    return decided, to_read, row


async def _read_source(
    sessions: Any, ref: SourceRef, *, region: str, analysis_date: str | None, max_pars: int, timeout_s: float
) -> tuple[SourceReading, list[str]]:
    """Начать чтение найденного акта, не ожидая поиска остальных источников."""
    norms, _, limitations = await search_named_acts([ref], sessions, region=region)
    if not norms:
        return SourceReading(ref=ref, status=ReadingStatus.NOT_FOUND), limitations
    readings = await read_all(
        sessions,
        norms,
        region=region,
        analysis_date=analysis_date,
        max_pars=max_pars,
        timeout_s=timeout_s,
        attempt_s=float(settings["agents.competence.source_attempt_s"]),
        attempts=int(settings["agents.competence.source_attempts"]),
    )
    return readings[0], limitations


async def collect_competence_sources(
    sessions: Any,
    refs: CompetenceRefs,
    *,
    analysis_date: str | None,
    timeout_s: float,
    authority_refs: tuple[SourceRef, ...] = (),
) -> CompetenceSources:
    """Собрать источники, найти их карточки в К+ и прочитать по найденным координатам."""
    max_sources = int(settings["agents.competence.max_sources"])
    max_pars = int(settings["agents.competence.max_source_paragraphs"])
    decided, to_read, row = compose_sources(refs, max_sources=max_sources, authority_refs=authority_refs)
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(
                _read_source(
                    sessions, ref, region=refs.region, analysis_date=analysis_date, max_pars=max_pars, timeout_s=timeout_s
                )
            )
            for ref in to_read
        ]
    # Задачи завершаются в произвольном порядке; приоритет источников остаётся прежним.
    results = [task.result() for task in tasks]
    readings = (*[reading for reading, _ in results], *decided)
    limitations = [text for _, limits in results for text in limits]
    if not any(r.ref.role is SourceRole.STATUTE for r in readings):
        limitations.append(settings["agents.competence.sources.statute_not_found"])
    limitations.extend(text for r in readings if (text := r.limitation()))
    await log.ainfo(
        "competence_sources",
        statute=row.key if row is not None else None,
        statute_origin=next((r.ref.origin.value for r in readings if r.ref.role is SourceRole.STATUTE), None),
        statuses=dict(Counter(r.status.value for r in readings)),
        limitations=len(limitations),
    )
    return CompetenceSources(
        readings=readings,
        limitations=tuple(limitations),
        statute=row,
    )


REGULATION = SourceRef(
    kind="regional_government_resolution",
    number="298-П",
    date="14.07.2014",
    title="О разработке и утверждении административных\nрегламентов предоставления государственных услуг",
    raw="от 14.07.2014 № 298-П",
    role=SourceRole.CITED,
    origin=SourceOrigin.CATALOG,
)
DELEGATION = SourceRef(
    kind="regional_law",
    number="109-ЗО",
    date="05.07.2013",
    title="О наделении органов местного самоуправления\nмуниципальных районов, муниципальных округов и отдельных городских\n"
    "округов Ульяновской области государственными полномочиями по опеке и\nпопечительству в отношении несовершеннолетних",
    raw="Законом Ульяновской области от 05.07.2013 № 109-ЗО",
    role=SourceRole.CITED,
    origin=SourceOrigin.CATALOG,
)


def regulation_project(refs: CompetenceRefs, header: str) -> bool:
    words = " ".join(header.lower().split()).split()
    return (
        "ульяновск" in refs.region.lower()
        and refs.action not in {ProjectAction.AMEND, ProjectAction.REPEAL}
        and "об утверждении" in " ".join(words)
        and any(a.startswith("административн") and b.startswith("регламент") for a, b in pairwise(words))
    )


def authority_refs(refs: CompetenceRefs, header: str) -> tuple[SourceRef, ...]:
    if not regulation_project(refs, header):
        return ()
    header = " ".join(header.lower().split())
    separate = any("проживан" in header[i : i + 25] for i in range(len(header)) if header.startswith("раздельн", i))
    guardian = (
        "эмансип" in header
        or separate
        or ("освобожд" in header and "опек" in header)
        or all(word in header for word in ("сдел", "имуществ", "несовершеннолет"))
    )
    return (REGULATION, DELEGATION) if guardian else (REGULATION,)


def source_priority(ref: SourceRef) -> int:
    for priority, candidate in enumerate((REGULATION, DELEGATION)):
        if (ref.kind, ref.number, ref.date) == (candidate.kind, candidate.number, candidate.date):
            return priority
    return 2
