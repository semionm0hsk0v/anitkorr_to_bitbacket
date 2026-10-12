from collections.abc import Sequence

from src.agents.enums.common.review import RiskKind
from src.agents.enums.internal.detector import InternalCheck
from src.agents.schemas.common.review import PROJECT_SOURCE_ID, Citation, Risk
from src.agents.schemas.internal.findings import DetectorFinding
from src.common.config.config import settings

#: Рекомендация, когда проверка не назвала свою правку.


def project_citation(location: str, quote: str) -> Citation:
    return Citation(source=PROJECT_SOURCE_ID, location=location, quote=quote)


def detector_finding(
    check: InternalCheck,
    *,
    place: str,
    quote: str,
    rationale: str,
    remedy: str = "",
    seconds: Sequence[tuple[str, str]] = (),
    related_places: Sequence[str] = (),
) -> DetectorFinding:
    """``seconds`` — ``(место, цитата)`` конфликтующих мест проекта; пусто, когда цитировать нечего."""
    citations = [project_citation(place, quote), *(project_citation(*item) for item in seconds)]
    risk = Risk(
        kind=RiskKind.INTERNAL,
        explanation=rationale,
        recommendation=remedy.strip() or settings["agents.internal.messages.generic_recommendation"],
        citations=citations,
    )
    return DetectorFinding(check=check, risk=risk, related_places=tuple(related_places))
