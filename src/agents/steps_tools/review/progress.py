from typing import Literal

from src.agents.enums.common.review import CheckStatus
from src.agents.schemas.common.review import Review
from src.agents.utils.streaming import progress_text


def stage_summary(review: Review, *, early_stop: bool = False, sources_unavailable: bool = False) -> str:
    """Bounded user-facing progress; diagnostic reasons stay in the review."""
    if early_stop:
        return progress_text("review_early_stop")
    lines = []
    for check in review.checks:
        key = f"review_check_{check.status.value}"
        if check.status is CheckStatus.NOT_CHECKED and sources_unavailable:
            key = "review_check_sources_unavailable"
        line = progress_text(key).format(label=progress_text(f"label_{check.kind.value}"))
        lines.append(line)
    return "\n\n".join(lines)


Outcome = Literal["found", "clear", "warn"]


def ui_outcome(review: Review) -> Outcome:
    if any(c.status in (CheckStatus.LIMITED, CheckStatus.NOT_CHECKED) for c in review.checks):
        return "warn"
    return "found" if review.findings else "clear"
