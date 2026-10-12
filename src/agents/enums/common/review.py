from enum import StrEnum


class RiskKind(StrEnum):
    INTERNAL = "internal"
    COMPLIANCE = "compliance"
    REPEAL = "repeal"
    COMPETENCE = "competence"


class CheckStatus(StrEnum):
    COMPLETED = "completed"
    LIMITED = "limited"
    NOT_CHECKED = "not_checked"
    NOT_APPLICABLE = "not_applicable"
