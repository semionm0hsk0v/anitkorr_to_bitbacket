from enum import StrEnum


class InternalCheck(StrEnum):
    """Какая детерминированная проверка выдала находку (ключ сравнения с эталоном и разбивки в логах)."""

    REFERENCE = "reference"
    APPENDIX = "appendix"
    SERVICE_TITLE = "service_title"
    TIME_LIMIT = "time_limit"
    SAME_VARIABLE = "same_variable"
