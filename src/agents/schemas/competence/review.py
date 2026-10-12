from pydantic import Field

from src.agents.enums.competence.review import CompetenceScope
from src.agents.schemas.common.review import Review


class CompetenceReview(Review):
    """Итог проверки полномочий; scope определяет возможность ранней остановки графа."""

    scope: CompetenceScope = Field(
        description=(
            "act — подтверждено отсутствие полномочий на акт целиком; provisions — превышение в отдельных "
            "положениях; none — превышения не выявлено; unknown — установить не удалось"
        )
    )
