from enum import StrEnum


class SourceMode(StrEnum):
    FULL = "full"  # текст целиком (полный или с пометкой «ТЕКСТ НЕПОЛНЫЙ» от C2)
    FRAGMENTS = "fragments"  # только статьи о полномочиях
    WITHHELD = "withheld"  # не передан: не поместился
