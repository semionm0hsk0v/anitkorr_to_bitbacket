from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from src.agents.schemas.parse.text_prep import TextSpan

_WORD_CHAR = r"[\wа-яёА-ЯЁ]"
_HYPHEN_BREAK_RE = re.compile(rf"({_WORD_CHAR})-\n({_WORD_CHAR})", re.UNICODE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_PARA_SPLIT_RE = re.compile(r"\n{2,}")
_SOFT_HYPHEN = "\u00ad"
_SOFT_HYPHEN_RE = re.compile(_SOFT_HYPHEN)
# Строка из 1–3 цифр между фрагментами текста (номер страницы OCR).
_OCR_INLINE_PAGE_RE = re.compile(
    rf"(?<={_WORD_CHAR}|[.,:;»\"')\]])\n[ \t]*\d{{1,3}}[ \t]*\n(?={_WORD_CHAR}|«)",
    re.UNICODE,
)
# Склейка разорванного слова: следующая строка — только короткий фрагмент (1–10 букв).
_OCR_WORD_JOIN_RE = re.compile(
    r"(?<=[а-яёa-z])(?<![.!?;:»\"')])\n(?=[а-яёa-z]{1,10}$)",
    re.UNICODE | re.MULTILINE,
)
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


#: ``origin[i]`` — индекс символа в ИСХОДНОМ тексте или ``None`` для вставленного.
CharOrigin = tuple[int | None, ...]


SubstitutionPart = tuple[int, int] | str


def identity_origin(text: str) -> CharOrigin:
    """Карта происхождения для неизменённого текста."""
    return tuple(range(len(text)))


def substitute_tracked(
    pattern: re.Pattern[str],
    parts_of: Callable[[re.Match[str]], Sequence[SubstitutionPart]],
    text: str,
    origin: CharOrigin,
) -> tuple[str, CharOrigin]:
    "``pattern.sub`` с переносом происхождения символов."
    chunks: list[str] = []
    mapped: list[int | None] = []
    cursor = 0
    for match in pattern.finditer(text):
        chunks.append(text[cursor : match.start()])
        mapped.extend(origin[cursor : match.start()])
        for part in parts_of(match):
            if isinstance(part, str):
                chunks.append(part)
                mapped.extend([None] * len(part))
                continue
            start, end = part
            chunks.append(text[start:end])
            mapped.extend(origin[start:end])
        cursor = match.end()
    chunks.append(text[cursor:])
    mapped.extend(origin[cursor:])
    result = "".join(chunks)
    if len(result) != len(mapped):  # pragma: no cover - инвариант построения
        raise AssertionError("origin map must cover every character of the result")
    return result, tuple(mapped)


def keep_match(match: re.Match[str]) -> list[SubstitutionPart]:
    """Замена «оставить как есть» — фрагмент сохраняет своё происхождение."""
    return [(match.start(), match.end())]


def group_span(match: re.Match[str], number: int) -> SubstitutionPart:
    """Фрагмент группы совпадения как часть замены."""
    return (match.start(number), match.end(number))


def repair_ocr_artifacts_tracked(text: str, origin: CharOrigin) -> tuple[str, CharOrigin]:
    "Тот же ремонт OCR, но с картой происхождения символов."
    if not text:
        return text, origin
    for pattern, parts_of in (
        (_SOFT_HYPHEN_RE, lambda _match: []),
        (_HYPHEN_BREAK_RE, lambda match: [group_span(match, 1), group_span(match, 2)]),
        (_OCR_INLINE_PAGE_RE, lambda _match: [" "]),
        (_OCR_WORD_JOIN_RE, lambda _match: []),
        (_MULTI_SPACE_RE, lambda _match: [" "]),
    ):
        text, origin = substitute_tracked(pattern, parts_of, text, origin)
    return text, origin


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = repair_ocr_artifacts_tracked(text, identity_origin(text))[0]
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip("\n")


def split_paragraphs(text: str) -> list[TextSpan]:
    """Режет текст на абзацы по двойным переводам строки с координатами в исходнике."""
    if not text:
        return []
    spans: list[TextSpan] = []
    cursor = 0
    for match in _PARA_SPLIT_RE.finditer(text):
        chunk = text[cursor : match.start()]
        if chunk.strip():
            spans.append(TextSpan(char_start=cursor, char_end=match.start(), text=chunk))
        cursor = match.end()
    tail = text[cursor:]
    if tail.strip():
        spans.append(TextSpan(char_start=cursor, char_end=len(text), text=tail))
    return spans
