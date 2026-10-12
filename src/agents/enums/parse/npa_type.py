from enum import StrEnum


class NpaTypeDetection(StrEnum):
    "Откуда взялось множество ``NpaType.matched_types``."

    DISABLED = "disabled"

    DETECTED = "detected"
