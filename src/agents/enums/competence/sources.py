from enum import StrEnum


class ProjectAction(StrEnum):
    """Что проект делает с правовым массивом (§4.1, ``action``)."""

    APPROVE = "approve"
    AMEND = "amend"
    REPEAL = "repeal"
    ESTABLISH = "establish"
    OTHER = "other"
