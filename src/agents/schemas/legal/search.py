from pydantic import BaseModel, ConfigDict

from src.agents.schemas.legal.plan import PlanOutcome
from src.agents.schemas.legal.sources import LegalSources


class LegalSearchResult(BaseModel):
    """Подтверждённый план и чтения; контекстные акты не являются вышестоящими источниками."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: PlanOutcome
    sources: LegalSources
    context: LegalSources
