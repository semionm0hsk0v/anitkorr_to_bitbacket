from enum import StrEnum


class ProvisionKind(StrEnum):
    """Тип структурной единицы в проекте."""

    PREAMBLE = "preamble"
    CHAPTER = "chapter"
    SECTION = "section"
    APPENDIX = "appendix"
    ARTICLE = "article"
    PART = "part"
    CLAUSE = "clause"
    SUBCLAUSE = "subclause"
    PARAGRAPH = "paragraph"
    SIGNATURE = "signature"


class SegmentAddressStatus(StrEnum):
    """Разрешён ли адрес нормы в каноническом тексте."""

    #: ``canonical[char_start:char_end] == text`` — адрес доказан происхождением символов.
    RESOLVED = "resolved"

    UNRESOLVED = "unresolved"


class SegmentAddressReason(StrEnum):
    """Почему адрес нормы не перенесён в канонический текст."""

    #: Ни одного значащего символа с известным происхождением (защитная ветвь).
    CANONICAL_ORIGIN_ABSENT = "canonical_origin_absent"
    #: Происхождение значащих символов не возрастает — перенос недоказуем.
    ORIGIN_NOT_MONOTONIC = "origin_not_monotonic"

    CANONICAL_CONTENT_DIVERGED = "canonical_content_diverged"
