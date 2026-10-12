from collections.abc import Iterable, Mapping, Sequence

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.schemas.common.review import Check, Review, Risk
from src.agents.steps_tools.review.grounding import grounded
from src.common.config.config import settings

_STATUS_RANK = {
    CheckStatus.COMPLETED: 0,
    CheckStatus.NOT_APPLICABLE: 0,
    CheckStatus.LIMITED: 1,
    CheckStatus.NOT_CHECKED: 2,
}


def worse(status: CheckStatus, other: CheckStatus) -> CheckStatus:
    return other if _STATUS_RANK[other] > _STATUS_RANK[status] else status


def settle(
    review: Review,
    kinds: Sequence[RiskKind],
    *,
    document: str,
    sources: Mapping[str, str],
    limitations: Sequence[str] = (),
) -> Review:
    "Accept only grounded findings of the assigned kinds and one check per kind."
    findings: list[Risk] = []
    dropped = dict.fromkeys(kinds, 0)
    for risk in review.findings:
        if risk.kind not in kinds:
            continue
        if grounded(risk, document, sources):
            findings.append(risk)
        else:
            dropped[risk.kind] += 1
    checks = []
    for kind in kinds:
        declared = next((c for c in review.checks if c.kind is kind), None)
        if declared is None:
            status = CheckStatus.LIMITED
            reasons = [settings["agents.review.settle.no_declared_status"]]
        else:
            status = declared.status
            reasons = [declared.reason] if declared.reason and status is not CheckStatus.COMPLETED else []
        if dropped[kind]:
            status = worse(status, CheckStatus.LIMITED)
            reasons.append(f"Выводов, не подтверждённых цитатами источников и не включённых в заключение: {dropped[kind]}.")
        if limitations and kind is not RiskKind.INTERNAL:
            status = worse(status, CheckStatus.LIMITED)
            reasons.extend(limitations)
        checks.append(Check(kind=kind, status=status, reason=" ".join(dict.fromkeys(reasons))))
    return Review(findings=findings, checks=checks)


def not_checked(kinds: Iterable[RiskKind], reason: str) -> list[Check]:
    return [Check(kind=kind, status=CheckStatus.NOT_CHECKED, reason=reason) for kind in kinds]
