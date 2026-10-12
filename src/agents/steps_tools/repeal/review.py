"""Допуск доказательств детектора отмены, находки и полнота проверки."""

from __future__ import annotations

import re

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.schemas.common.review import PROJECT_SOURCE_ID, Check, Citation, Risk
from src.agents.schemas.repeal.detector import OrphanResult, RedundantRepealResult, RepealDetectionResult, UnitScopeGap
from src.agents.schemas.repeal.evidence import RepealAmenderEvidence, RepealChainEvidence
from src.agents.steps_tools.repeal.clauses import act_identity, norm_act_num
from src.agents.steps_tools.sources.normalization import card_date
from src.common.config.config import settings


def repeal_findings(detection: RepealDetectionResult, document_text: str) -> tuple[list[Risk], int]:
    """Подтвердить находки по доказательствам детектора и цитатам проекта; посчитать удержанные."""
    candidates = (
        [orphan_to_risk(r, document_text) for r in detection.results]
        + [
            unit_scope_to_risk(result, gap, document_text)
            for result in detection.unit_incomplete
            for gap in result.unit_incomplete
        ]
        + [redundant_repeal_to_risk(r, document_text) for r in detection.redundant]
    )
    findings = [risk for risk in candidates if risk is not None]
    return findings, len(candidates) - len(findings)


def detector_check(detection: RepealDetectionResult, *, held: int = 0) -> Check:
    reasons: list[str] = []
    if not detection.complete:
        reasons.append("Отменяемые акты и положения проверены детектором не полностью.")
        if detection.blocked_bases:
            reasons.append(f"Не проверены базовые акты: {', '.join(detection.blocked_bases)}.")
        if detection.blocked_subpoints:
            reasons.append(f"Не проверены положения актов: {', '.join(detection.blocked_subpoints)}.")
    if held:
        reasons.append(f"Возможных дефектов отмены, не подтверждённых источниками детектора: {held}; требуется ручная проверка.")
    return Check(kind=RiskKind.REPEAL, status=CheckStatus.LIMITED if reasons else CheckStatus.COMPLETED, reason=" ".join(reasons))


#: Локус находок детектора: все три отношения лежат в перечне пункта-отмены проекта.


def _repeal_risk(*, quote: str, legal_basis: str, rationale: str, recommendation: str) -> Risk | None:
    "Находка детектора в общем формате: цитата проекта первой, основание — первым предложением объяснения."
    if not quote.strip():
        return None
    return Risk(
        kind=RiskKind.REPEAL,
        explanation=" ".join(part for part in (legal_basis, rationale) if part),
        recommendation=recommendation,
        citations=[Citation(source=PROJECT_SOURCE_ID, location=settings["agents.repeal.messages.location"], quote=quote)],
    )


def orphan_to_risk(res: OrphanResult, document_text: str) -> Risk | None:
    "OrphanResult → находка о неотменённых изменявших актах; ``None``, если публиковать её нельзя."
    orphans = ", ".join(act_requisites_label(o.number, o.date) for o in res.orphans)
    base_label = act_requisites_label(res.base_number, res.base_date)
    risk = _repeal_risk(
        quote=res.clause_marker_text or res.clause_quote,
        legal_basis=settings["agents.repeal.messages.completeness_basis"],
        rationale=(
            f"Проект признаёт утратившим силу приказ {base_label}, но не отменяет "
            f"изменявшие его акты: {orphans}. Эти изменения остаются действующими по отношению "
            f"к отменённому акту, создавая правовую коллизию."
        ),
        recommendation=(
            f"Дополнить перечень положениями следующих актов, которыми изменён приказ {base_label}: {orphans}. "
            "Указать конкретные подлежащие отмене пункты; не отменять иные положения этих актов "
            "без самостоятельного основания."
        ),
    )
    if risk is None or (res.evidence is None or not res.evidence.orphan_numbers) or not quote_in_project(risk, document_text):
        return None
    return risk


def unit_scope_to_risk(res: OrphanResult, gap: UnitScopeGap, document_text: str) -> Risk | None:
    "Неполный объём отмены редактора → находка; ``None``, если публиковать её нельзя."
    base_label = act_requisites_label(res.base_number, res.base_date)
    amender_label = act_requisites_label(norm_act_num(gap.amender.number), gap.amender.date)
    uncovered = ", ".join(f"пункт {label}" for label in gap.assessment.uncovered_units)
    covered = ", ".join(f"пункт {label}" for label in gap.assessment.covered_units)
    covered_text = f" Проект снимает только {covered}." if covered else ""
    quote = res.clause_marker_text or res.clause_quote
    risk = _repeal_risk(
        quote=quote,
        legal_basis=settings["agents.repeal.messages.completeness_basis"],
        rationale=(
            f"Проект признаёт утратившим силу приказ {base_label}, однако {uncovered} приказа {amender_label}, "
            f"которыми в этот акт вносились изменения, утратившими силу не признаны.{covered_text} "
            f"Эти изменения остаются действующими по отношению к отменённому акту, создавая правовую коллизию."
        ),
        recommendation=(
            f"Дополнить перечень положениями приказа {amender_label}, которыми изменён приказ {base_label}: {uncovered}. "
            "Иные положения этого приказа без самостоятельного основания не отменять."
        ),
    )
    if risk is None or res.evidence is None:
        return None
    matches = [
        item
        for item in res.evidence.amenders
        if act_identity(item.source.act_number, item.source.act_date) == act_identity(gap.amender.number, gap.amender.date)
    ]
    bound = unit_is_bound(
        res.evidence,
        matches[0] if len(matches) == 1 else None,
        uncovered=gap.assessment.uncovered_units,
        covered=gap.assessment.covered_units,
        bound=gap.assessment.bound_units,
        clause_quote=res.clause_quote,
        quote=quote,
        document_text=document_text,
    )
    return risk if bound and quote_in_project(risk, document_text) else None


def redundant_repeal_to_risk(res: RedundantRepealResult, document_text: str) -> Risk | None:
    "RedundantRepealResult → находка обратного отношения; ``None``, если публиковать её нельзя."
    target_label = act_requisites_label(res.target_number, res.target_date)
    by_label = act_requisites_label(res.repealed_by_number, res.repealed_by_date) if res.repealed_by_number else ""
    already = f" (утратил силу: приказ {by_label})" if by_label else ""
    risk = _repeal_risk(
        quote=res.clause_marker_text or res.clause_quote,
        legal_basis=settings["agents.repeal.messages.redundant_repeal_basis"],
        rationale=(
            f"Проект повторно признаёт утратившим силу пункт {res.subpoint} приказа {target_label}, "
            f"который уже утратил силу{already}. Повторное признание его утратившим силу не требуется."
        ),
        recommendation=f"Исключить из перечня пункт {res.subpoint} приказа {target_label}.",
    )
    if risk is None or res.evidence is None or not quote_in_project(risk, document_text):
        return None
    return risk


def _normalize(text: str) -> str:
    """Регистр и пробелы не различают: так же сравнивает цитату и метрика оценки."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def quote_in_project(risk: Risk, document_text: str) -> bool:
    """Первая цитата находки — текст проекта; пустая цитата текстом проекта не считается."""
    citations = risk.citations
    if not citations or citations[0].source != PROJECT_SOURCE_ID:
        return False
    quote = _normalize(citations[0].quote)
    return bool(quote) and quote in _normalize(document_text)


def unit_is_bound(
    evidence: RepealChainEvidence | None,
    editor: RepealAmenderEvidence | None,
    *,
    uncovered: tuple[str, ...],
    covered: tuple[str, ...],
    bound: tuple[str, ...],
    clause_quote: str,
    quote: str,
    document_text: str,
) -> bool:
    "Неполный объём отмены редактора привязан к одному месту проекта и к доказанным положениям."
    if evidence is None or editor is None or not clause_quote or document_text.count(clause_quote) != 1:
        return False
    source = (editor.source.source_base, editor.source.source_doc_num)
    if sum(1 for item in evidence.amenders if (item.source.source_base, item.source.source_doc_num) == source) != 1:
        return False
    scope = editor.unit_scope
    return (
        scope is not None
        and not editor.repealed_by_project
        and scope.status == "proven_incomplete"
        and bool(scope.uncovered_units)
        and scope.uncovered_units == uncovered
        and scope.covered_units == covered
        and scope.bound_units == bound
        and bool(quote)
        and quote in clause_quote
    )


def act_requisites_label(number: str, date: str | None) -> str:
    "Полные реквизиты акта для текста замечания: «от 19.06.2026 № 83-п»."
    normalized = card_date(date or "")
    return f"от {normalized} № {number}" if normalized else f"№ {number}"
