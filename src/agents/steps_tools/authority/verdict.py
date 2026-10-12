"""Допуск выводов компетенции, итоговый охват и условие раннего завершения экспертизы."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.enums.competence.review import CompetenceScope
from src.agents.schemas.common.review import PROJECT_SOURCE_ID, Check, Citation, Risk
from src.agents.schemas.competence.assessment import AuthorityAssessment, CompetenceAssessment, FormAssessment
from src.agents.schemas.competence.fragments import SourceView
from src.agents.schemas.competence.review import CompetenceReview
from src.agents.schemas.competence.verdict import SettledAnswer
from src.agents.steps_tools.review.grounding import quote_in_source
from src.agents.steps_tools.review.settle import not_checked
from src.common.config.config import settings


def authority_issues(answer: AuthorityAssessment, blocks: Mapping[str, Any]) -> list[str]:
    """Проверить доказательства полномочий до допуска вывода."""
    groups = (answer.project_evidence, answer.norm_evidence, answer.issuer_link_evidence, answer.counterevidence)
    if any(identity not in blocks for group in groups for identity in group):
        raise ValueError("invented authority evidence ID")
    issues = []
    if any(blocks[i]["source"] != PROJECT_SOURCE_ID for i in answer.project_evidence):
        issues.append("Решение проекта процитировано из правового источника.")
    if any(blocks[i]["source"] == PROJECT_SOURCE_ID for i in answer.norm_evidence):
        issues.append("Полномочие подтверждено самим проектом, а не нормой источника.")
    if answer.answer != "not_established" and (not answer.norm_evidence or not answer.issuer_link_evidence):
        issues.append("Не подтверждены норма о полномочии или её связь с издателем.")
    if answer.answer == "violated" and answer.project_decision != answer.governed_decision:
        issues.append("Норма распределяет другое решение, чем принимает проект.")
    if answer.counterevidence and not answer.resolution.strip():
        issues.append("Не разрешены противоречащие выводу нормы.")
    if answer.answer != "violated" and answer.violation_scope is not None:
        issues.append("Указан объём нарушения без самого нарушения.")
    if answer.answer == "violated" and answer.violation_scope is None:
        issues.append("Не указан объём нарушения полномочий.")
    if answer.answer == "violated" and not answer.recommendation.strip():
        issues.append("Нет рекомендации по устранению нарушения полномочий.")
    return issues


def settle_answer(
    answer: AuthorityAssessment | FormAssessment,
    *,
    question: str,
    blocks: Mapping[str, Any],
    document: str,
    texts: Mapping[str, str],
    issues: Sequence[str],
) -> SettledAnswer:
    """Разрешить ID в реальные цитаты и удержать неподтверждённое нарушение."""
    if isinstance(answer, AuthorityAssessment):
        identities = answer.project_evidence + answer.norm_evidence + answer.issuer_link_evidence
        extent = answer.violation_scope
    else:
        identities, extent = answer.evidence, None
        if any(identity not in blocks for identity in identities):
            raise ValueError("invented answer evidence ID")
    citations = [Citation(**{key: blocks[i][key] for key in ("source", "quote", "location")}) for i in dict.fromkeys(identities)]
    project = [c for c in citations if c.source == PROJECT_SOURCE_ID and quote_in_source(c.quote, document)]
    sources = [c for c in citations if c.source in texts and quote_in_source(c.quote, texts[c.source])]
    value = "not_established" if issues else answer.answer
    downgraded = value == "violated" and (not project or not sources)
    if downgraded:
        value = "not_established"
    return SettledAnswer(
        question=question,
        answer=value,
        explanation=answer.explanation,
        recommendation=answer.recommendation if value == "violated" else "",
        citations=tuple(project + sources),
        extent=extent if value == "violated" else None,
        downgraded=downgraded,
    )


def derive_scope(organ: SettledAnswer, form: SettledAnswer) -> CompetenceScope:
    """Таблица §5.1."""
    if form.answer == "violated" or (organ.answer == "violated" and organ.extent == "act"):
        return CompetenceScope.ACT
    if organ.answer == "violated" and organ.extent == "provisions":
        return CompetenceScope.PROVISIONS
    if organ.answer == "compliant" and form.answer == "compliant":
        return CompetenceScope.NONE
    return CompetenceScope.UNKNOWN


def _finding(answer: SettledAnswer) -> Risk:
    return Risk(
        kind=RiskKind.COMPETENCE,
        explanation=answer.explanation,
        recommendation=answer.recommendation or "Привести акт в соответствие с распределением полномочий.",
        citations=list(answer.citations),
    )


def competence_verdict(
    assessment: CompetenceAssessment,
    *,
    blocks: Mapping[str, Any],
    document: str,
    views: Sequence[SourceView],
    limitations: Sequence[str] = (),
) -> CompetenceReview:
    """Проверить ответ модели и собрать итог без промежуточного чек-листа."""
    texts = {v.reading.source_id: v.text for v in views if v.text and v.reading.source_id}
    labels = {v.reading.source_id: v.reading.ref.label for v in views if v.reading.source_id}
    complete = {v.reading.source_id for v in views if v.complete}
    organ_issues = authority_issues(assessment.authority, blocks)
    form_issues: list[str] = []
    if assessment.form.answer == "violated" and not assessment.form.recommendation.strip():
        form_issues.append("Нет рекомендации по устранению нарушения формы.")
    organ = settle_answer(
        assessment.authority, question="authority", blocks=blocks, document=document, texts=texts, issues=organ_issues
    )
    form = settle_answer(assessment.form, question="form", blocks=blocks, document=document, texts=texts, issues=form_issues)
    scope = derive_scope(organ, form)

    findings: list[Risk] = []
    if scope is CompetenceScope.ACT:
        findings = [_finding(a) for a in (form, organ) if a.answer == "violated"]
        decisive = [a for a in (form, organ) if a.answer == "violated"]
    elif scope is CompetenceScope.PROVISIONS:
        findings, decisive = [_finding(organ)], [organ]
    elif scope is CompetenceScope.NONE:
        decisive = [organ, form]
    else:
        decisive = []

    decisive_questions = frozenset(a.question for a in decisive) | {"input"}
    forcing = [lim.text for lim in assessment.limitations if lim.question in decisive_questions]
    deferred = [lim.text for lim in assessment.limitations if lim.question not in decisive_questions]

    reasons: list[str] = list(forcing)
    if structural_issues := organ_issues + form_issues:
        reasons.append("Неподтверждённый ответ модели: " + " ".join(structural_issues))
    reasons.extend(
        settings["agents.competence.verdict.downgraded"].format(
            question=settings["agents.competence.verdict.question_labels"]["organ" if a.question == "authority" else a.question]
        )
        for a in (organ, form)
        if a.downgraded
    )
    if scope is CompetenceScope.UNKNOWN:
        reasons.append(settings["agents.competence.verdict.not_established"])
    relied = frozenset().union(*(a.source_ids for a in decisive)) if decisive else frozenset()
    if decisive and not relied:
        reasons.append(settings["agents.competence.verdict.no_source_citation"])
    if partial := sorted(relied - complete):
        reasons.append(
            settings["agents.competence.verdict.incomplete_source"].format(labels="; ".join(labels.get(i, i) for i in partial))
        )
    if reasons:
        reasons.extend(deferred)
        reasons.extend(limitations)
    status = CheckStatus.LIMITED if reasons else CheckStatus.COMPLETED
    review = CompetenceReview(
        findings=findings,
        checks=[Check(kind=RiskKind.COMPETENCE, status=status, reason=" ".join(dict.fromkeys(reasons)))],
        scope=scope,
    )
    return review


def unchecked_competence_review(reason: str) -> CompetenceReview:
    """Отразить невозможность проверки без вывода об отсутствии нарушений."""
    return CompetenceReview(findings=[], checks=not_checked((RiskKind.COMPETENCE,), reason), scope=CompetenceScope.UNKNOWN)


def stops_early(review: CompetenceReview | None) -> bool:
    "Early termination needs the whole act, a completed check and a grounded finding."
    if review is None or review.scope is not CompetenceScope.ACT:
        return False
    check = next((c for c in review.checks if c.kind is RiskKind.COMPETENCE), None)
    return (
        check is not None
        and check.status is CheckStatus.COMPLETED
        and any(f.kind is RiskKind.COMPETENCE for f in review.findings)
    )
