from enum import StrEnum


class SourceRole(StrEnum):
    """Зачем источник читается (§6.1): приоритет состава — в этом же порядке."""

    STATUTE = "statute"  # Положение об органе-издателе
    TARGET = "target"  # изменяемый/отменяемый акт
    CITED = "cited"  # акт преамбулы


class SourceOrigin(StrEnum):
    """Откуда взята ссылка на источник."""

    PREAMBLE = "preamble"  # названа в преамбуле проекта
    HEADER = "header"  # названа в заголовке проекта (изменяемый/отменяемый акт)
    CATALOG = "catalog"  # справочник Положений ``organ_statutes.yaml``
    MODEL = "model"  # в проекте не назван, предложен моделью по памяти (compliance.md §4.1)
    BODY = "body"  # назван в тексте проекта (не в преамбуле и не в заголовке)
    SEARCH = "search"  # реквизиты из карточной выдачи К+, применимость выбирает L1
