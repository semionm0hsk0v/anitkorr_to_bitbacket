from enum import StrEnum


class AddressLevel(StrEnum):
    """Уровень структуры, к которому обращается звено адресной цепочки."""

    CLAUSE = "clause"
    SUBCLAUSE = "subclause"
    SECTION = "section"
    CHAPTER = "chapter"
    PARAGRAPH = "paragraph"
    PART = "part"


class UnresolvedReason(StrEnum):
    """Почему распознанная цепочка не дала адреса."""

    ADDRESS_NOT_NUMERIC = "ADDRESS_NOT_NUMERIC"
    #: Числовой адрес есть, но его уровень реестрами не адресуется (абзац/часть).
    ADDRESS_LEVEL_UNSUPPORTED = "ADDRESS_LEVEL_UNSUPPORTED"
    #: В цепочке нет ни номера, ни порядкового слова.
    ADDRESS_ABSENT = "ADDRESS_ABSENT"
