from enum import StrEnum


class Modality(StrEnum):
    """Деонтическая модальность нормы (закрытый словарь)."""

    OBLIGATION = "обязанность"
    PROHIBITION = "запрет"
    RIGHT = "право"
    ESTABLISHMENT = "установление"
    REFERENCE = "отсылка"


class Polarity(StrEnum):
    """Полярность предиката (для деонтического конфликта)."""

    POSITIVE = "положительная"
    NEGATIVE = "отрицательная"


class ParameterType(StrEnum):
    """Вид числового параметра нормы: единственное место, где записаны метки видов («срок» и т. д.)."""

    DURATION = "срок"
    AGE = "возраст"
    MONEY = "сумма"
    SHARE = "доля"


class CalendarKind(StrEnum):
    """Какими днями считается срок: рабочими или календарными."""

    WORKING = "рабочие"
    CALENDAR = "календарные"


class Qualifier(StrEnum):
    """Квалификатор числового параметра."""

    AT_LEAST = "не_менее"
    AT_MOST = "не_более"
    EXACT = "ровно"
