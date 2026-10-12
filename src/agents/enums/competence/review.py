from enum import StrEnum


class CompetenceScope(StrEnum):
    """Where an excess of authority was established: the routing input, not a finding."""

    ACT = "act"
    PROVISIONS = "provisions"
    NONE = "none"
    UNKNOWN = "unknown"
