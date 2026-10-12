"""Предмет проекта: названия меры и услуги с координатами собственного обозначения."""

from __future__ import annotations

import re

from src.agents.schemas.internal.frames.project_frames import SubjectDesignation
from src.common.config.config import settings

_NOT_PARAGRAPH_BREAK = r"(?!\n[ \t]*\n)"


_SERVICE = re.compile(
    r"((?:предоставлени\w*\s+)?(?:государственн\w*|муниципальн\w*)?\s*услуг\w*"
    rf"(?:{_NOT_PARAGRAPH_BREAK}\s+[а-яё«»\"()-]+){{0,10}})",
    re.IGNORECASE,
)


_SERVICE_QUOTED = re.compile(r"услуг\w*\s*[«\"]([^»\"]{3,300})[»\"]", re.IGNORECASE | re.DOTALL)


_MEASURE = re.compile(
    r"\b((?:пособи|компенсаци|выплат|субсиди|удостоверени|льгот|статус)\w*"
    rf"(?:{_NOT_PARAGRAPH_BREAK}\s+[а-яё«»\"()-]+){{0,7}})",
    re.IGNORECASE,
)


DOCUMENT_SUBJECT_HEAD_CHARS = int(settings["agents.internal.project_frames.document_subject_head_chars"])


_PAIRED_QUOTES = {"»": "«", "\u201d": "\u201c"}


def _trim_unbalanced_quote_tail(value: str) -> str:
    "Обрезает захват на кавычке, закрывающей ЧУЖУЮ цитату."
    depth = 0
    straight_open: int | None = None
    for index, char in enumerate(value):
        if char in _PAIRED_QUOTES.values():
            depth += 1
        elif char in _PAIRED_QUOTES:
            if depth == 0:
                return value[:index]
            depth -= 1
        elif char == '"':
            straight_open = None if straight_open is not None else index
    return value[:straight_open] if straight_open is not None else value


def designation_value(designation: SubjectDesignation | None) -> str | None:
    return designation.value if designation is not None else None


def _designation(
    pattern: re.Pattern[str],
    text: str,
    *,
    offset: int | None,
) -> SubjectDesignation | None:
    match = pattern.search(text)
    if match is None:
        return None
    captured = _trim_unbalanced_quote_tail(match.group(1))
    stripped = captured.strip()
    if not stripped:
        return None
    value = " ".join(stripped.split())
    if offset is None:
        return SubjectDesignation(value=value)
    start = offset + match.start(1) + (len(captured) - len(captured.lstrip()))
    return SubjectDesignation(value=value, char_start=start, char_end=start + len(stripped))


def subject_designations(
    text: str,
    *,
    offset: int | None = 0,
) -> tuple[SubjectDesignation | None, SubjectDesignation | None]:
    "Мера и услуга, которыми акт называет СВОЙ предмет."
    quoted = _designation(_SERVICE_QUOTED, text, offset=offset)
    return (
        _designation(_MEASURE, text, offset=offset),
        quoted or _designation(_SERVICE, text, offset=offset),
    )


_ENACTING_FORMULA = re.compile(r"постановля\w*|приказыва\w*|распоряжа\w*|\bреши(?:л|ла|ло)\b", re.IGNORECASE)


def _document_head_window(text: str) -> str:
    """Заголовок и преамбула БЕЗ нормативного тела: текст до формулы принятия."""
    match = _ENACTING_FORMULA.search(text)
    return text[: match.start()] if match else text


def document_subjects(
    document_text: str,
    doc_title: str,
) -> tuple[SubjectDesignation | None, SubjectDesignation | None]:
    "Предмет документа из его собственной головы; заголовок — запасной источник."
    head_window = _document_head_window(document_text[:DOCUMENT_SUBJECT_HEAD_CHARS])
    head_measure, head_service = subject_designations(head_window, offset=0)
    title_measure, title_service = subject_designations(doc_title, offset=None)
    return head_measure or title_measure, head_service or title_service
