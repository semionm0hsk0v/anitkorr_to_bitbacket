"""Получение и проверка источников для цепочки изменений и повторной отмены положений."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from typing import Any, Literal

from src.agents.enums.repeal.detector import BLOCKING_REPEAL_REASONS, RedundantRepealReason, RepealOutcomeReason
from src.agents.enums.repeal.units import UnitScopeStatus
from src.agents.schemas.legal_base.act_refs import ActRequisites
from src.agents.schemas.legal_base.currentness import (
    ReferenceCoverageStatus,
)
from src.agents.schemas.legal_base.retrieval import RawAct
from src.agents.schemas.parse.structure import DocumentStructureNode
from src.agents.schemas.repeal.detector import (
    Amender,
    OrphanResult,
    RedundantRepealResult,
    RepealDetectionResult,
    RepealTarget,
)
from src.agents.schemas.repeal.evidence import (
    RepealAmenderEvidence,
    RepealChainEvidence,
    RepealRedundantEvidence,
    RepealSourceEvidence,
    RepealUnitScopeEvidence,
)
from src.agents.schemas.repeal.units import UnitScopeAssessment
from src.agents.steps_tools.repeal.amendments import (
    _RED_ITEM,
    amends_base_act,
    diff_orphans,
    parse_amendment_chain,
    same_issuer,
    unit_targets_for,
)
from src.agents.steps_tools.repeal.clauses import (
    _is_amending_act,
    act_identity,
    norm_act_num,
    title_declares_editing,
)
from src.agents.steps_tools.repeal.units import (
    assess_unit_scope,
    issuer_from_reference,
    issuer_relation,
    known_act_date,
    normalize_act_kind,
    parse_unit_labels,
)
from src.agents.steps_tools.sources.card_search import fetch_doc_info_with_transport, fetch_links_to_doc
from src.agents.steps_tools.sources.currentness import (
    has_currentness_evidence,
)
from src.agents.steps_tools.sources.normalization import card_date
from src.agents.steps_tools.sources.retrieval import retrieve_act
from src.common.utils.concurrency import gather_results
from src.common.utils.logger import get_logger

log = get_logger(__name__)


def _info_date(info: Mapping[str, Any]) -> str | None:
    """Дата акта из карточки doc_info («2026-06-19») → ``19.06.2026``; нет даты — ``None``."""
    dates = info.get("dates")
    value = dates[0] if isinstance(dates, list) and dates and isinstance(dates[0], str) else ""
    try:
        return dt.date.fromisoformat(value[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return card_date(value) or None


async def amenders_from_links(sessions: Any, raw: RawAct) -> list[Amender] | None:
    "Кандидаты в изменявшие акты по обратным ссылкам К+ на базовый акт."
    links = await fetch_links_to_doc(sessions, raw.base, raw.doc_num)
    if links is None:
        return None
    found: list[Amender] = []
    for bank, doc_num in links:
        if (bank, doc_num) == (raw.base, raw.doc_num):
            continue
        info, _transport = await fetch_doc_info_with_transport(sessions, bank, doc_num)
        title = str((info or {}).get("title") or "").strip()
        if info is None or not title:
            return None  # карточка ссылающегося не прочитана: полнота списка не доказана
        number = norm_act_num(title)
        if number and _is_amending_act(title):
            found.append(Amender(number=number, date=_info_date(info), raw=title.splitlines()[0], source="links"))
    return found


def _merge_amenders(*chains: list[Amender]) -> list[Amender]:
    """Объединить кандидатов, оставив первого из совпавших по ``(дата, номер)``."""
    merged: list[Amender] = []
    seen: set[tuple[str, str]] = set()
    for chain in chains:
        for amender in chain:
            key = (card_date(amender.date or ""), amender.number)
            if key not in seen:
                seen.add(key)
                merged.append(amender)
    return merged


#: Родовые слова заголовка регламента — выкидываем из К+-запроса (не различают акт).
_GENERIC_TITLE_WORDS = frozenset(
    [
        "об",
        "утверждении",
        "административного",
        "регламента",
        "предоставления",
        "министерством",
        "социального",
        "развития",
        "ульяновской",
        "области",
        "государственной",
        "услуги",
        "приказ",
        "приказа",
        "министерства",
        "настоящего",
        "изменений",
        "внесении",
        "отдельные",
        "нормативные",
        "правовые",
        "акты",
    ]
)


def build_act_query(target: RepealTarget) -> str:
    "К+-запрос по отменяемому базовому акту: «приказ <министерство> <номер> <отличит."
    ministry = (target.raw or "").split(" от ")[0].strip()  # «приказ Министерства … области»
    title = target.title or ""
    tail = title.rsplit("услуги", 1)[-1] if "услуги" in title.lower() else title
    words = [w for w in re.findall(r"[А-Яа-яЁёA-Za-z]+", tail) if len(w) > 3 and w.lower() not in _GENERIC_TITLE_WORDS]
    distinctive = " ".join(words[:6])
    return " ".join(p for p in (ministry, target.number, distinctive) if p).strip()


def build_act_requisites(target: RepealTarget, region: str = "") -> ActRequisites:
    "Отменяемый акт → реквизиты для КАРТОЧНОГО поиска (вид + номер + дата + субъект)."
    return ActRequisites(
        kind="Приказ",
        number=target.number,
        date=card_date(target.date or ""),
        region=region,
        federal=False,
        title=target.title or "",
        raw=target.raw or "",
    )


def _source_evidence(raw: RawAct, *, act_number: str, act_date: str) -> RepealSourceEvidence:
    """Сохранить идентичность источника после проверки актуальности."""
    assert raw.version is not None  # has_currentness_evidence уже подтвердил редакцию.
    version = raw.version
    return RepealSourceEvidence(
        act_number=act_number,
        act_date=card_date(act_date or ""),
        source_base=version.source.base,
        source_doc_num=version.source.doc_num,
    )


def _orphan_identities(result: OrphanResult) -> set[tuple[str, str]]:
    return {act_identity(item.number, item.date) for item in result.orphans}


def _build_chain_evidence(
    base: RepealSourceEvidence,
    result: OrphanResult,
    amender_evidence: list[RepealAmenderEvidence],
) -> RepealChainEvidence:
    """Build a receipt without coercing unit-scope unknown into a boolean."""
    orphan_identities = _orphan_identities(result)
    orphan_numbers = {number for _date, number in orphan_identities}
    unit_scope_identities = {act_identity(item.amender.number, item.amender.date) for item in result.unit_scope_unknown}
    chain_amenders: list[RepealAmenderEvidence] = []
    orphan_source_ids: list[tuple[str, int]] = []
    for item in amender_evidence:
        identity = act_identity(item.source.act_number, item.source.act_date)
        if identity in unit_scope_identities and item.unit_scope is None:
            continue
        is_orphan = identity in orphan_identities
        chain_amenders.append(item.model_copy(update={"repealed_by_project": not is_orphan and item.unit_scope is None}))
        if is_orphan:
            orphan_source_ids.append((item.source.source_base, item.source.source_doc_num))
    return RepealChainEvidence(
        base=base,
        amenders=tuple(chain_amenders),
        orphan_numbers=tuple(sorted(orphan_numbers)),
        orphan_source_ids=tuple(sorted(orphan_source_ids)),
    )


async def _check_base_with_status(
    sessions: Any,
    base: RepealTarget,
    repealed: list[RepealTarget],
    clause: Any,
    clause_text: str,
    region: str = "",
    analysis_date: str | None = None,
) -> tuple[OrphanResult | None, RepealOutcomeReason]:
    "Один БАЗОВЫЙ акт: поднять действующую редакцию из К+ → цепочка изменявших → орфаны."
    query = build_act_query(base)
    raw = await retrieve_act(
        sessions,
        query,
        use_cache=True,
        max_pars=2_147_483_647,
        requisites=build_act_requisites(base, region),
        region=region,
        analysis_date=analysis_date,
    )
    if raw is None:
        await log.ainfo("repeal_base_not_found", base=base.number, query=query[:70])
        return None, RepealOutcomeReason.BASE_NOT_FOUND_OR_TRANSPORT_UNAVAILABLE
    if base.number != norm_act_num(raw.title):
        await log.ainfo("repeal_base_mismatch", want=base.number, got=norm_act_num(raw.title), title=raw.title[:80])
        return None, RepealOutcomeReason.BASE_IDENTITY_MISMATCH
    if _is_amending_act(raw.title):
        await log.ainfo("repeal_skip_amending_act", base=base.number, title=raw.title[:80])
        return None, RepealOutcomeReason.BASE_IS_AMENDING_ACT
    coverage = raw.version.coverage if raw.version is not None else None
    if coverage is None or coverage.status is not ReferenceCoverageStatus.COMPLETE:
        await log.awarning(
            "repeal_coverage_incomplete",
            base=base.number,
            coverage=coverage.status.value if coverage is not None else "missing",
            par_count=raw.par_count,
        )
        return None, RepealOutcomeReason.INCOMPLETE_DOCUMENT_COVERAGE
    if not has_currentness_evidence(raw):
        await log.awarning("repeal_currentness_unverified", base=base.number)
        return None, RepealOutcomeReason.CURRENTNESS_NOT_CURRENT
    base_source = _source_evidence(raw, act_number=norm_act_num(raw.title) or base.number, act_date=base.date or "")
    annotated = parse_amendment_chain(raw.full_text)
    linked = await amenders_from_links(sessions, raw)
    amenders = _merge_amenders(annotated, linked or [])

    base_issuer = issuer_from_reference(raw.title)
    amenders, amender_evidence, unverified_count, unit_scopes = await _verify_amenders(
        sessions, base, amenders, region, raw.title, analysis_date, raw.full_text, repealed, base_issuer
    )
    res = diff_orphans(base.number, repealed, amenders, base_date=base.date, unit_scope=unit_scopes)
    res.evidence = _build_chain_evidence(base_source, res, amender_evidence)
    res.base_date = card_date(base.date or "")
    res.clause_quote = clause_text
    res.clause_marker_text = (getattr(clause, "text", "") or "").strip()
    await log.ainfo("repeal_diff", base=base.number, amenders=len(amenders), orphans=[o.number for o in res.orphans])
    if unverified_count:
        reason = RepealOutcomeReason.AMENDER_VERIFICATION_INCOMPLETE
    elif res.unit_scope_unknown:
        reason = RepealOutcomeReason.AMENDER_UNIT_SCOPE_UNPROVEN
    elif linked is None:
        reason = RepealOutcomeReason.AMENDMENT_SOURCE_UNAVAILABLE
    elif not amenders:
        reason = RepealOutcomeReason.AMENDMENT_CHAIN_EMPTY
    elif res.orphans:
        reason = RepealOutcomeReason.ORPHANS_FOUND
    elif res.unit_incomplete:
        reason = RepealOutcomeReason.UNIT_SCOPE_INCOMPLETE
    else:
        reason = RepealOutcomeReason.NO_ORPHANS
    return (res if (res.orphans or res.unit_incomplete) else None), reason


#: Слова, которые есть в заголовке ЛЮБОГО регионального приказа и органа не различают.


def _unit_scope_evidence(
    assessment: UnitScopeAssessment,
    base: RepealTarget,
    base_issuer: str,
    project_units: tuple[str, ...],
) -> RepealUnitScopeEvidence | None:
    """Квитанция объёма — только для ДОКАЗАННОГО исхода; неизвестность её не получает."""
    if assessment.status is UnitScopeStatus.UNKNOWN or not base.kind:
        return None
    spans = {unit.label: (unit.start, unit.end) for unit in assessment.units}
    if any(label not in spans for label in assessment.bound_units):
        return None
    status: Literal["proven_complete", "proven_incomplete"] = (
        "proven_complete" if assessment.status is UnitScopeStatus.PROVEN_COMPLETE else "proven_incomplete"
    )
    return RepealUnitScopeEvidence(
        status=status,
        reason=assessment.reason.value,
        base_act_kind=base.kind,
        base_act_number=norm_act_num(base.number),
        base_act_date=card_date(base.date or ""),
        base_act_issuer=base_issuer,
        bound_units=assessment.bound_units,
        bound_unit_spans=tuple(spans[label] for label in assessment.bound_units),
        covered_units=assessment.covered_units,
        uncovered_units=assessment.uncovered_units,
        unresolved_units=assessment.unresolved_units,
        project_repealed_units=project_units,
    )


class _ExcludedAmender:
    "Кандидат из обратных ссылок, доказанно не правивший базовый акт (другой орган или нет"


_EXCLUDED = _ExcludedAmender()


async def _verify_amender(
    sessions: Any,
    base: RepealTarget,
    amender: Amender,
    region: str,
    base_title: str,
    analysis_date: str | None,
    base_full_text: str = "",
    repealed: list[RepealTarget] | None = None,
    base_issuer: str = "",
) -> tuple[Amender, RepealAmenderEvidence, UnitScopeAssessment | None] | _ExcludedAmender | None:
    "Card-verify an editor's exact requisites and PROVE its amending link to the base."
    req = ActRequisites(
        kind="Приказ",
        number=amender.number,
        date=card_date(amender.date or ""),
        region=region,
        federal=False,
        raw=amender.raw,
    )
    raw = await retrieve_act(
        sessions,
        amender.raw or amender.number,
        use_cache=True,
        requisites=req,
        region=region,
        analysis_date=analysis_date,
    )
    if raw is None or norm_act_num(raw.title) != amender.number:
        await log.awarning("repeal_amender_unverified", amender=amender.number, reason="exact_card_miss")
        return None
    if not has_currentness_evidence(raw):
        await log.awarning(
            "repeal_amender_unverified",
            amender=amender.number,
            reason="currentness_not_current",
        )
        return None
    if not same_issuer(base_title, raw.title):
        await log.awarning("repeal_amender_unverified", amender=amender.number, base=base.number, reason="other_issuer")
        return _EXCLUDED if amender.source == "links" else None
    relation = amends_base_act(raw.title, raw.full_text, base)
    relation_proof = "amendment_formula" if relation is True else ""
    if (
        not relation_proof
        and raw.version is not None
        and raw.version.coverage.status is ReferenceCoverageStatus.TRUNCATED_CONFIGURED
    ):
        full = await retrieve_act(
            sessions,
            amender.raw or amender.number,
            use_cache=True,
            requisites=req,
            region=region,
            max_pars=2_147_483_647,
            analysis_date=analysis_date,
        )
        if full is not None and has_currentness_evidence(full):
            full_relation = amends_base_act(full.title, full.full_text, base)
            if full_relation is True:
                raw, relation_proof = full, "amendment_formula"
    if not relation_proof:
        annotated = any(
            item.number == amender.number and card_date(item.date or "") == card_date(amender.date or "")
            for item in parse_amendment_chain(base_full_text)
        )
        if annotated and title_declares_editing(raw.title):
            relation_proof = "base_editorial_annotation"
            await log.ainfo(
                "repeal_amender_annotation_only",
                amender=amender.number,
                base=base.number,
                reason="source_annotation_absent",
            )
        else:
            await log.awarning(
                "repeal_amender_unverified",
                amender=amender.number,
                base=base.number,
                reason="base_relation_absent",
            )
            return _EXCLUDED if amender.source == "links" else None
    source = _source_evidence(raw, act_number=amender.number, act_date=amender.date or "")

    unit_targets = unit_targets_for(amender, repealed or [])
    assessment: UnitScopeAssessment | None = None
    scope_evidence: RepealUnitScopeEvidence | None = None
    if unit_targets:
        editor_issuer = issuer_from_reference(raw.title)
        if (
            not base_issuer
            or normalize_act_kind(base_title) != base.kind
            or issuer_relation(base_issuer, issuer_from_reference(base.raw)) is not True
            or any(
                target.kind != normalize_act_kind(raw.title)
                or not editor_issuer
                or issuer_relation(editor_issuer, issuer_from_reference(target.raw)) is not True
                for target in unit_targets
            )
        ):
            unit_targets = ()
    if unit_targets:
        project_units = tuple(dict.fromkeys(target.subpoint or "" for target in unit_targets))
        assessment = assess_unit_scope(
            amender_text=raw.full_text,
            base_number=base.number,
            base_date=base.date,
            base_kind=base.kind,
            base_issuer=base_issuer,
            repealed_units=project_units,
            source_complete=(raw.version is not None and raw.version.coverage.status is ReferenceCoverageStatus.COMPLETE),
        )
        normalized_units = tuple(dict.fromkeys(label for item in project_units for label in parse_unit_labels(item)[0]))
        scope_evidence = _unit_scope_evidence(assessment, base, base_issuer, normalized_units)
        if scope_evidence is None:
            assessment = None  # без квитанции доказанным объём не считается
    return (
        amender,
        RepealAmenderEvidence(
            source=source,
            relation_proof=relation_proof,
            repealed_by_project=False,
            unit_scope=scope_evidence,
        ),
        assessment,
    )


async def _verify_amenders(
    sessions: Any,
    base: RepealTarget,
    amenders: list[Amender],
    region: str,
    base_title: str,
    analysis_date: str | None,
    base_full_text: str = "",
    repealed: list[RepealTarget] | None = None,
    base_issuer: str = "",
) -> tuple[list[Amender], list[RepealAmenderEvidence], int, dict[tuple[str, str], UnitScopeAssessment]]:
    """Проверенные редакторы, их доказательства, ЧИСЛО непроверенных и объёмы по пунктам."""
    checked = await gather_results(
        *(
            _verify_amender(
                sessions,
                base,
                amender,
                region,
                base_title,
                analysis_date,
                base_full_text,
                repealed,
                base_issuer,
            )
            for amender in amenders
        )
    )
    verified = [item for item in checked if isinstance(item, tuple)]
    scopes = {
        (known_act_date(amender.date) or "", norm_act_num(amender.number)): assessment
        for amender, _evidence, assessment in verified
        if assessment is not None
    }
    return (
        [amender for amender, _evidence, _assessment in verified],
        [evidence for _amender, evidence, _assessment in verified],
        sum(1 for item in checked if item is None),
        scopes,
    )


_POINT_LABEL_TEMPLATE = r"^[ \t]*{label}[.)](?!\d)"


_POINT_LOST_FORCE_TEMPLATE = r"^[ \t]*{label}[.)](?!\d)\s*(?:[-–—]\s*)?утратил[аио]?\s+силу\b"


_ADDRESSABLE_SUBPOINT = re.compile(r"^\d+(?:\.\d+)*$")


def _point_paragraphs(act_full_text: str, subpoint: str) -> list[str]:
    "ВСЕ абзацы, начинающиеся меткой ``subpoint``."
    pattern = re.compile(_POINT_LABEL_TEMPLATE.format(label=re.escape(subpoint)), re.IGNORECASE | re.MULTILINE)
    text = act_full_text or ""
    out: list[str] = []
    for match in pattern.finditer(text):
        end = text.find("\n", match.start())
        out.append(text[match.start() : end if end != -1 else len(text)])
    return out


def point_lost_force(act_full_text: str, subpoint: str) -> str | None:
    "Абзац с ПОЛОЖИТЕЛЬНОЙ аннотацией «утратил силу» у пункта ``subpoint``."
    if not _ADDRESSABLE_SUBPOINT.match((subpoint or "").strip()):
        return None
    paragraphs = _point_paragraphs(act_full_text, subpoint.strip())
    if len(paragraphs) != 1:
        return None
    pattern = re.compile(_POINT_LOST_FORCE_TEMPLATE.format(label=re.escape(subpoint.strip())), re.IGNORECASE)
    return paragraphs[0] if pattern.search(paragraphs[0]) else None


_BLOCKING_REDUNDANT_REASONS: frozenset[RedundantRepealReason] = frozenset(
    {
        RedundantRepealReason.TARGET_NOT_FOUND_OR_TRANSPORT_UNAVAILABLE,
        RedundantRepealReason.TARGET_IDENTITY_MISMATCH,
        RedundantRepealReason.INCOMPLETE_DOCUMENT_COVERAGE,
        RedundantRepealReason.CURRENTNESS_NOT_CURRENT,
        RedundantRepealReason.SUBPOINT_NOT_ADDRESSABLE,
        RedundantRepealReason.SUBPOINT_NOT_LOCATED,
        RedundantRepealReason.SUBPOINT_AMBIGUOUS_LABEL,
    }
)


async def _check_redundant_repeal(
    sessions: Any,
    target: RepealTarget,
    clause: Any,
    clause_text: str,
    region: str = "",
    analysis_date: str | None = None,
) -> tuple[RedundantRepealResult | None, RedundantRepealReason]:
    "Один ПОДПУНКТ-цель: доказан ли положительно статус «уже утратил силу»."
    subpoint = (target.subpoint or "").strip()
    if not _ADDRESSABLE_SUBPOINT.match(subpoint):
        return None, RedundantRepealReason.SUBPOINT_NOT_ADDRESSABLE
    raw = await retrieve_act(
        sessions,
        build_act_query(target),
        use_cache=True,
        max_pars=2_147_483_647,
        requisites=build_act_requisites(target, region),
        region=region,
        analysis_date=analysis_date,
    )
    if raw is None:
        return None, RedundantRepealReason.TARGET_NOT_FOUND_OR_TRANSPORT_UNAVAILABLE
    if target.number != norm_act_num(raw.title):
        return None, RedundantRepealReason.TARGET_IDENTITY_MISMATCH
    coverage = raw.version.coverage if raw.version is not None else None
    if coverage is None or coverage.status is not ReferenceCoverageStatus.COMPLETE:
        return None, RedundantRepealReason.INCOMPLETE_DOCUMENT_COVERAGE
    if not has_currentness_evidence(raw):
        return None, RedundantRepealReason.CURRENTNESS_NOT_CURRENT
    source = _source_evidence(raw, act_number=target.number, act_date=target.date or "")
    candidates = _point_paragraphs(raw.full_text, subpoint)
    if not candidates:
        return None, RedundantRepealReason.SUBPOINT_NOT_LOCATED
    if len(candidates) > 1:
        return None, RedundantRepealReason.SUBPOINT_AMBIGUOUS_LABEL
    annotation = point_lost_force(raw.full_text, subpoint)
    if annotation is None:
        return None, RedundantRepealReason.SUBPOINT_IN_FORCE

    by_act = _RED_ITEM.search(annotation)
    by_number = norm_act_num(by_act.group("num")) if by_act is not None else ""
    by_date = card_date(by_act.group("date")) if by_act is not None else ""
    result = RedundantRepealResult(
        target_number=target.number,
        target_date=card_date(target.date or ""),
        subpoint=subpoint,
        repealed_by_number=by_number,
        repealed_by_date=by_date,
        clause_quote=clause_text,
        clause_marker_text=(getattr(clause, "text", "") or "").strip(),
        evidence=RepealRedundantEvidence(
            target=source,
            subpoint=subpoint,
        ),
    )
    await log.ainfo("repeal_redundant_point", target=target.number, subpoint=subpoint, by=result.repealed_by_number)
    return result, RedundantRepealReason.SUBPOINT_LOST_FORCE


async def detect_orphan_amendments_with_status(
    sessions: Any,
    targets: list[tuple[RepealTarget, DocumentStructureNode, str]],
    *,
    region: str = "",
    analysis_date: str | None = None,
) -> RepealDetectionResult:
    """Проверить все отменяемые акты, учитывая общий перечень отмены проекта."""
    repealed = [target for target, _clause, _text in targets]
    bases = [(target, clause, text) for target, clause, text in targets if not target.is_subpoint]
    subpoints = [(target, clause, text) for target, clause, text in targets if target.is_subpoint]
    checks: list[list[Any]] = await gather_results(
        gather_results(
            *(
                _check_base_with_status(sessions, base, repealed, clause, text, region, analysis_date)
                for base, clause, text in bases
            )
        ),
        gather_results(
            *(
                _check_redundant_repeal(sessions, target, clause, text, region, analysis_date)
                for target, clause, text in subpoints
            )
        ),
    )
    checked, redundant_checked = checks
    return RepealDetectionResult(
        results=[result for result, _ in checked if result is not None and result.orphans],
        redundant=[result for result, _ in redundant_checked if result is not None],
        unit_incomplete=[result for result, _ in checked if result is not None and result.unit_incomplete],
        blocked_bases=tuple(
            base.number for (base, _, _), (_, reason) in zip(bases, checked, strict=True) if reason in BLOCKING_REPEAL_REASONS
        ),
        blocked_subpoints=tuple(
            f"{target.number}:{target.subpoint or ''}"
            for (target, _, _), (_, reason) in zip(subpoints, redundant_checked, strict=True)
            if reason in _BLOCKING_REDUNDANT_REASONS
        ),
    )
