from collections.abc import Mapping
from typing import Literal

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.schemas.common.review import Review
from src.agents.utils.streaming import progress_text
from src.common.config.config import settings


def stage_summary(review: Review, *, early_stop: bool = False, labels: Mapping[RiskKind, str] | None = None) -> str:
    """Progress line of one stage: statuses only; findings are shown by the report."""
    names = {**settings["agents.report.risk_labels"], **(labels or {})}
    lines = [
        progress_text(f"review_check_{check.status.value}").format(
            label=names[check.kind], reason=check.reason, n=sum(1 for f in review.findings if f.kind is check.kind)
        )
        for check in review.checks
    ]
    if early_stop:
        lines.append(progress_text("review_early_stop"))
    return "\n\n".join(lines)


Outcome = Literal["found", "clear", "warn"]


def ui_outcome(review: Review) -> Outcome:
    if any(c.status in (CheckStatus.LIMITED, CheckStatus.NOT_CHECKED) for c in review.checks):
        return "warn"
    return "found" if review.findings else "clear"
