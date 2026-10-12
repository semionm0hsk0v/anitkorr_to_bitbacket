from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.enums.internal.detector import InternalCheck
from src.agents.schemas.common.review import Check, Risk
from src.agents.schemas.internal.detector import DetectorScan
from src.agents.steps_tools.internal.detector import CHECKS
from src.agents.steps_tools.review.grounding import grounded

CHECK_LABELS = {
    InternalCheck.REFERENCE: "отсылки",
    InternalCheck.APPENDIX: "состав приложений",
    InternalCheck.SERVICE_TITLE: "наименование",
    InternalCheck.TIME_LIMIT: "сроки",
    InternalCheck.SAME_VARIABLE: "значения одной величины",
}


def internal_findings(scan: DetectorScan, document: str) -> tuple[list[Risk], int]:
    """Подтвердить все цитаты находок по исходному проекту; посчитать удержанные."""
    findings = [item.risk for item in scan.findings if grounded(item.risk, document, {})]
    return findings, len(scan.findings) - len(findings)


def detector_check(scan: DetectorScan, *, held: int, limit_reason: str) -> Check:
    """Учесть сбои проверок, неподтверждённые находки и границы покрытия детектора."""
    reasons = []
    if scan.failed:
        names = ", ".join(CHECK_LABELS[check] for check, _ in CHECKS if check in scan.failed)
        reasons.append(f"Не завершили работу проверки детектора: {names}.")
    if held:
        reasons.append(f"Выводов, не подтверждённых цитатами источников и не включённых в заключение: {held}.")
    reasons.append(limit_reason)
    return Check(
        kind=RiskKind.INTERNAL,
        status=CheckStatus.NOT_CHECKED if len(scan.failed) == len(CHECKS) else CheckStatus.LIMITED,
        reason=" ".join(reasons),
    )
