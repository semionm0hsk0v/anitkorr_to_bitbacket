"""Восстановление OCR-разметки с сохранением происхождения каждого символа."""

from __future__ import annotations

import re

from src.agents.steps_tools.document.text_prep import (
    CharOrigin,
    SubstitutionPart,
    group_span,
    keep_match,
    repair_ocr_artifacts_tracked,
    substitute_tracked,
)

_INLINE_CLAUSE_BREAK_RE = re.compile(
    r"(?<=[.:;»\"])[ \t]+(\d+\.\s+\S)",
    re.UNICODE,
)


_CLAUSE_AFTER_LOWERCASE_RE = re.compile(
    r"(?<=[а-яё»\"])[ \t]+(\d+\.\s+[А-ЯЁA-Z])",
    re.UNICODE,
)


_STRUCTURE_BEFORE_CLAUSE_RE = re.compile(
    r"(?:Статья|ст\.?|Часть|ч\.?|п\.?|пункт|пп\.?|Глава|Раздел|Подраздел)\s*$",
    re.IGNORECASE | re.UNICODE,
)


_NESTED_CLAUSE_BREAK_RE = re.compile(
    r"(?<=\S)\s+(\d+\.\d+(?:\.\d+)*\.\s+\S)",
    re.UNICODE,
)


_DOTTED_CLAUSE_NO_SPACE_RE = re.compile(
    r"(?m)^(\d+(?:\.\d+)+\.)(?=[А-ЯЁ])",
    re.UNICODE,
)


_ABBREV_BEFORE_BREAK_RE = re.compile(
    r"(?:^|\s)([а-яёa-z]{1,3})\.\s*$",
    re.IGNORECASE | re.UNICODE,
)


_STRUCTURE_HEADING_BREAK_RE = re.compile(
    r"(?<=\S)\s+(?=(?:ПРИЛОЖЕНИЕ|УТВЕРЖДЕН[АО]?|ПОЛОЖЕНИЕ|ПОЛОЖЕНИЯ|ПОРЯДОК|РЕГЛАМЕНТ|"
    r"МЕТОДИКА|ПРАВИЛА|ИНСТРУКЦИЯ|ПРОЕКТ)\b|(?:Приложение|Статья|Глава|Раздел)\b)",
    re.UNICODE,
)


_NPA_CAPTION_START_RE = re.compile(
    r"(?<![«„])\s+(Об (?:утверждении|изменении|внесении|признании)\b)",
    re.IGNORECASE | re.UNICODE,
)


_DECREE_CAPTION_PREAMBLE_RE = re.compile(
    r"(?<=[а-яё»\"])\s+(В (?:целях|связи с|развитии|соответствии)\b)",
    re.IGNORECASE | re.UNICODE,
)


_OCR_PAGE_PREFIX_RE = re.compile(
    r"\n[ \t]*\d{1,3}[ \t]+(?=\d+(?:\.\d+)*\.\s+\S)",
    re.UNICODE,
)


_OCR_PAGE_NUMBER_LINE_RE = re.compile(r"^[ \t]*\d{1,3}[ \t]*$", re.UNICODE)


_SIGNATURE_TAIL_BREAK_RE = re.compile(
    r"(?<=[.:;»\"])\s+(?=(?:Министр|Председатель|Руководитель|Исполняющий\s+обязанности)\b)",
    re.IGNORECASE | re.UNICODE,
)


def _inline_clause_break_parts(match: re.Match[str], *, source: str) -> list[SubstitutionPart]:
    clause = match.group(1)
    prefix = source[max(0, match.start() - 40) : match.start()]
    if clause and clause[0].isdigit():
        if _STRUCTURE_BEFORE_CLAUSE_RE.search(prefix):
            return keep_match(match)
        return ["\n", group_span(match, 1)]
    if _ABBREV_BEFORE_BREAK_RE.search(prefix):
        return keep_match(match)
    return ["\n", group_span(match, 1)]


def _clause_after_lowercase_parts(match: re.Match[str], *, source: str) -> list[SubstitutionPart]:
    prefix = source[: match.start()]
    tail = prefix[-40:]
    if _STRUCTURE_BEFORE_CLAUSE_RE.search(tail):
        return keep_match(match)
    if tail.endswith("."):
        return keep_match(match)
    return ["\n", group_span(match, 1)]


_LINE_MARKER_SPLIT_RE = re.compile(
    r"(?<=[.:;»\"])[ \t]+(?=\d+(?:\.\d+)*\.\s+\S)" r"|(?<=[а-яё»\"])[ \t]+(?=\d+\.\s+[А-ЯЁA-Z])",
    re.UNICODE,
)


def _explode_line_markers_tracked(line: str, origin: CharOrigin) -> list[tuple[str, CharOrigin]]:
    """То же деление строки, но каждая часть уносит происхождение своих символов."""
    stripped = line.strip()
    if not stripped:
        return [(line, origin)]
    base = len(line) - len(line.lstrip())
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for separator in _LINE_MARKER_SPLIT_RE.finditer(stripped):
        bounds.append((cursor, separator.start()))
        cursor = separator.end()
    bounds.append((cursor, len(stripped)))
    if len(bounds) <= 1:
        return [(line, origin)]

    result: list[tuple[str, list[int | None]]] = []
    for low, high in bounds:
        part = stripped[low:high]
        body = part.strip()
        if not body:
            continue
        start = base + low + (len(part) - len(part.lstrip()))
        part_origin = list(origin[start : start + len(body)])
        if result and _STRUCTURE_BEFORE_CLAUSE_RE.search(result[-1][0][-30:]):
            previous_text, previous_origin = result[-1]
            # Склейка вставляет пробел: у него происхождения нет.
            result[-1] = (f"{previous_text} {body}", [*previous_origin, None, *part_origin])
        else:
            result.append((body, part_origin))
    if len(result) <= 1:
        return [(line, origin)]
    return [(text, tuple(part_origin)) for text, part_origin in result]


_CRLF_RE = re.compile(r"\r\n|\r")


def reflow_with_origin(text: str, origin: CharOrigin) -> tuple[str, CharOrigin]:
    "Тот же reflow, но с картой происхождения каждого символа результата."
    text, origin = substitute_tracked(_CRLF_RE, lambda _match: ["\n"], text, origin)
    text, origin = repair_ocr_artifacts_tracked(text, origin)
    text, origin = substitute_tracked(_OCR_PAGE_PREFIX_RE, lambda _match: ["\n"], text, origin)
    text, origin = substitute_tracked(_DOTTED_CLAUSE_NO_SPACE_RE, lambda match: [group_span(match, 1), " "], text, origin)
    for pattern in (_NPA_CAPTION_START_RE, _DECREE_CAPTION_PREAMBLE_RE, _NESTED_CLAUSE_BREAK_RE):
        text, origin = substitute_tracked(pattern, lambda match: ["\n", group_span(match, 1)], text, origin)
    # ``source`` — текст ДО замены, как и в исходной реализации через ``re.sub``.
    source = text
    text, origin = substitute_tracked(
        _INLINE_CLAUSE_BREAK_RE, lambda match: _inline_clause_break_parts(match, source=source), text, origin
    )
    source = text
    text, origin = substitute_tracked(
        _CLAUSE_AFTER_LOWERCASE_RE, lambda match: _clause_after_lowercase_parts(match, source=source), text, origin
    )
    for pattern in (_STRUCTURE_HEADING_BREAK_RE, _SIGNATURE_TAIL_BREAK_RE):
        text, origin = substitute_tracked(pattern, lambda _match: ["\n"], text, origin)

    exploded: list[tuple[str, CharOrigin]] = []
    position = 0
    for line in text.split("\n"):
        line_origin = origin[position : position + len(line)]
        position += len(line) + 1
        if _OCR_PAGE_NUMBER_LINE_RE.match(line):
            continue
        exploded.extend(_explode_line_markers_tracked(line, line_origin))
    merged: list[int | None] = []
    for index, (_line, line_origin) in enumerate(exploded):
        if index:
            merged.append(None)  # разделитель строк вставлен сборкой
        merged.extend(line_origin)
    return "\n".join(line for line, _line_origin in exploded), tuple(merged)
