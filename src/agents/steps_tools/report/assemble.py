"""Сборка результатов ветвей, текста находок и справки о полноте заключения."""

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.schemas.common.review import Check, Review, Risk
from src.common.config.config import settings

#: Порядок направлений в заключении: полномочия, затем проверки содержания.
REPORT_ORDER = (RiskKind.COMPETENCE, RiskKind.INTERNAL, RiskKind.COMPLIANCE, RiskKind.REPEAL)


def _identity(risk: Risk) -> tuple:
    def norm(text: str) -> str:
        return " ".join(text.split())

    return (
        risk.kind,
        norm(risk.explanation),
        norm(risk.recommendation),
        tuple((c.source, norm(c.location), norm(c.quote)) for c in risk.citations),
    )


def assemble_review(
    *,
    competence: Review | None,
    internal: Review | None,
    compliance: Review | None,
    repeal: Review | None,
    early_stop: bool,
) -> Review:
    """Перенести допущенные находки и статусы ветвей, объединяя только полные дубли."""
    checks: list[Check] = []
    findings: list[Risk] = []
    seen: set[tuple] = set()
    for kind, review in zip(REPORT_ORDER, (competence, internal, compliance, repeal), strict=True):
        check = next((c for c in review.checks if c.kind is kind), None) if review is not None else None
        if check is None:
            reason = (
                f"{settings['agents.review.settle.early_stop']}; направление не проверялось."
                if early_stop
                else "Проверка не выполнялась."
            )
            check = Check(kind=kind, status=CheckStatus.NOT_CHECKED, reason=reason)
        checks.append(check)
        for risk in review.findings if review is not None else ():
            if (key := _identity(risk)) not in seen:
                seen.add(key)
                findings.append(risk)
    # Устойчивый порядок сохраняет последовательность находок внутри направления.
    findings.sort(key=lambda risk: REPORT_ORDER.index(risk.kind))
    return Review(findings=findings, checks=checks)


def finding_paragraphs(risk: Risk, number: int) -> list[str]:
    project, *others = risk.citations
    heading = f"{number}. {settings['agents.report.risk_labels'][risk.kind]}" + (
        f" — {project.location}" if project.location else ""
    )
    return [
        heading,
        settings["agents.report.project_quote"].format(quote=project.quote),
        risk.explanation,
        *(f"{c.location}: «{c.quote}»." if c.location else f"«{c.quote}»." for c in others),
        settings["agents.report.recommendation"].format(recommendation=risk.recommendation),
    ]


def body_paragraphs(review: Review) -> list[str]:
    return [paragraph for index, risk in enumerate(review.findings, 1) for paragraph in finding_paragraphs(risk, index)]


def summary_lines(review: Review, *, analysis_date: str, early_stop: bool) -> list[str]:
    executed = [c for c in review.checks if c.status in (CheckStatus.COMPLETED, CheckStatus.LIMITED)]
    lines: list[str] = []
    if early_stop:
        lines.append(settings["agents.report.early_stop"].format(reason=settings["agents.review.settle.early_stop"]))
    if review.findings:
        lines.append(settings["agents.report.findings_count"].format(count=len(review.findings)))
    elif executed:
        lines.append(settings["agents.report.no_findings"])
    else:
        lines.append(settings["agents.report.not_checked"])
    if not early_stop and any(c.status in (CheckStatus.LIMITED, CheckStatus.NOT_CHECKED) for c in review.checks):
        lines.append(settings["agents.report.incomplete"])
    lines.append(settings["agents.report.analysis_date"].format(analysis_date=analysis_date))
    lines.append(settings["agents.report.status_heading"])
    for check in review.checks:
        reason = f" — {check.reason}" if check.reason else ""
        label = settings["agents.report.risk_labels"][check.kind]
        status = settings["agents.report.status_labels"][check.status]
        lines.append(f"- {label}: {status}{reason}")
    return lines
