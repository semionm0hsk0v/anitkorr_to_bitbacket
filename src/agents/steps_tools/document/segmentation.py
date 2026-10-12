"""Структурный разбор проекта на положения с координатами в каноническом тексте."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from src.agents.enums.parse.segmentation import ProvisionKind, SegmentAddressReason, SegmentAddressStatus
from src.agents.schemas.parse.segmentation import NormSegment
from src.agents.steps_tools.document.locus_labels import APPENDIX, ARTICLE, CLAUSE, PARAGRAPH, PART, SUBCLAUSE, locus
from src.agents.steps_tools.document.reflow import (
    reflow_with_origin,
)
from src.agents.steps_tools.document.text_prep import (
    CharOrigin,
    clean_text,
    identity_origin,
    split_paragraphs,
)

UNLABELED_PROVISION = "положение"
_WS_LEADING_RE = re.compile(r"^\s+")

_CHAPTER_RE = re.compile(
    r"^(?:Глава|глава)\s+([IVXLCivxlc\d]+)(?:\s*[\.—\-–]\s*|\s+)(.*)$",
    re.IGNORECASE | re.UNICODE,
)
_SECTION_RE = re.compile(
    r"^(?:Раздел|раздел)\s+(\d+)(?:\s*[\.—\-–]\s*|\s+)(.*)$",
    re.IGNORECASE | re.UNICODE,
)
_APPENDIX_RE = re.compile(
    r"^(?:Приложение|приложение)\s*(?:№\s*)?(\d+)?(?:\s+|$)(.*)$",
    re.IGNORECASE | re.UNICODE,
)
_ARTICLE_SAME_LINE_RE = re.compile(
    r"^(?:Статья|ст\.?)\s+(\d+)(?:\s*[\.—\-–]\s*|\s+)(.+)$",
    re.IGNORECASE | re.UNICODE,
)
_ARTICLE_ONLY_RE = re.compile(
    r"^(?:Статья|ст\.?)\s+(\d+)\s*\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)
_PART_RE = re.compile(
    r"^(?:Часть|ч\.?)\s+(\d+)(?:\s*[\.—\-–]\s*|\s+)(.*)$",
    re.IGNORECASE | re.UNICODE,
)
_PART_ONLY_RE = re.compile(
    r"^(?:Часть|ч\.?)\s+(\d+)\s*\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)
_PARAGRAPH_SIGN_RE = re.compile(r"^§\s*(\d+)(?:\s+|$)(.*)$", re.UNICODE)

_DOT_CLAUSE_RE = re.compile(
    r"^(\d+(?:\.\d+)*)\.\s+(\S)",
    re.UNICODE,
)


_DOT_CLAUSE_ONLY_RE = re.compile(
    r"^(\d{1,2}(?:\.\d+)*)\.\s*$",
    re.UNICODE,
)
_EXPLICIT_CLAUSE_RE = re.compile(
    r"^(?:п\.?|пункт)\s+(\d+(?:\.\d+)*)(?:\s*[\.—\-–]\s*|\s+)(.*)$",
    re.IGNORECASE | re.UNICODE,
)
_EXPLICIT_CLAUSE_ONLY_RE = re.compile(
    r"^(?:п\.?|пункт)\s+(\d+(?:\.\d+)*)\s*\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)

_LETTER_PAREN_RE = re.compile(
    r"^([а-яёa-z])\)\s+(\S)",
    re.IGNORECASE | re.UNICODE,
)


_DIGIT_PAREN_RE = re.compile(
    r"^(\d{1,2})\)\s+(\S)",
    re.UNICODE,
)
_LETTER_DOT_RE = re.compile(
    r"^([а-яёa-z])\.\s+(\S)",
    re.IGNORECASE | re.UNICODE,
)
_SUBPUNKT_RE = re.compile(
    r"^(?:пп\.?|подпункт)\s+([а-яёa-z])\)?(?:\s*[\.—\-–]\s*|\s+)(.*)$",
    re.IGNORECASE | re.UNICODE,
)
_SUBPUNKT_ONLY_RE = re.compile(
    r"^(?:пп\.?|подпункт)\s+([а-яёa-z])\)?\s*\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)

# OCR часто отдаёт страницу одной строкой; маркеры нумерации — внутри абзаца.
# OCR: «1. Общие положения» без точки перед номером («области 1. Общие»).
# Вложенный номер пункта, слипшийся с текстом без пробела: «1.2.Государственная».
# Защита от «п. 2.» внутри «ст. 12.» — только для буквенных сокращений из 1–3 букв.


# Заголовок приказа и преамбула «В целях…» на одной OCR-строке.
_DECREE_CAPTION_LINE_RE = re.compile(
    r"^Об (?:утверждении|изменении|внесении|признании)\b",
    re.IGNORECASE | re.UNICODE,
)
_DECREE_BODY_LINE_RE = re.compile(
    r"^В (?:целях|связи с|развитии|соответствии)\b",
    re.IGNORECASE | re.UNICODE,
)
_ENACTING_FORMULA_RE = re.compile(r"приказываю|постановляю|распоряжаюсь", re.IGNORECASE | re.UNICODE)
# Начало утверждаемого приложения к приказу (не пункты резолютивной части).
_ATTACHED_ACT_START_RE = re.compile(
    r"^(?:УТВЕРЖДЕН[АО]?|ПОЛОЖЕНИЕ|ПОЛОЖЕНИЯ|ПОРЯДОК|РЕГЛАМЕНТ|МЕТОДИКА|ПРАВИЛА|ИНСТРУКЦИЯ)\b",
    re.UNICODE,
)
_ATTACHED_ACT_TITLE_RE = re.compile(
    r"^[Пп]оложение\s+(?:о|об)\s+",
    re.UNICODE,
)
_REGULATION_SECTION_HEADING_RE = re.compile(
    r"^\d+\.\s+(?:Общие|Основные|Заключительные|Порядок|Права|Срок|Организац|Контроль|Заключит)",
    re.IGNORECASE | re.UNICODE,
)
_SIGNATURE_ROLE_LINE_RE = re.compile(
    r"^(?:Министр|Председатель|Исполняющий\s+обязанности)(?:\s|$)",
    re.IGNORECASE | re.UNICODE,
)
_SIGNATURE_NAME_AT_END_RE = re.compile(
    r"[А-ЯЁ]\.[А-ЯЁ]\.?\s*[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]+)?\s*$",
    re.UNICODE,
)
_SIGNATURE_TAIL_RE = re.compile(
    r"\s+((?:Министр|Председатель|Руководитель|Исполняющий\s+обязанности)"
    r"[^\n]{4,160})$",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)


@dataclass
class _Marker:
    kind: ProvisionKind
    level: int
    label: str
    line_start: int
    line_end: int
    body_on_same_line: str = ""


@dataclass
class _Context:
    appendix: str | None = None
    article: str | None = None
    part: str | None = None
    clause: tuple[int, ...] = ()
    subclause: str | None = None

    def format_ref(self, kind: ProvisionKind) -> str:
        parts: list[str] = []
        if self.appendix:
            parts.append(locus(APPENDIX, self.appendix))
        if self.article:
            parts.append(locus(ARTICLE, self.article))
        if self.part:
            parts.append(locus(PART, self.part))
        if self.clause:
            dotted = ".".join(str(x) for x in self.clause)
            parts.append(locus(CLAUSE, dotted))
        if self.subclause:
            parts.append(f"{locus(SUBCLAUSE, self.subclause)})")
        if parts:
            return ", ".join(parts)
        if kind is ProvisionKind.PREAMBLE:
            return "преамбула"
        return UNLABELED_PROVISION


def _strip_line(line: str) -> str:
    return _WS_LEADING_RE.sub("", line).strip()


def _last_nonempty_line(body: str) -> str:
    last = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            last = stripped
    return last


def _clause_level(number: tuple[int, ...]) -> int:
    return 400 + max(0, len(number) - 1)


def _detect_marker(stripped: str, line_start: int, line_end: int) -> _Marker | None:
    if not stripped:
        return None

    m = _CHAPTER_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.CHAPTER, 100, m.group(1), line_start, line_end, m.group(2).strip())

    m = _SECTION_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.SECTION, 200, m.group(1), line_start, line_end, m.group(2).strip())

    m = _APPENDIX_RE.match(stripped)
    if m:
        label = m.group(1) or ""
        return _Marker(ProvisionKind.APPENDIX, 250, label, line_start, line_end, m.group(2).strip())

    m = _PARAGRAPH_SIGN_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.CLAUSE, 380, m.group(1), line_start, line_end, m.group(2).strip())

    m = _ARTICLE_SAME_LINE_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.ARTICLE, 300, m.group(1), line_start, line_end, m.group(2).strip())

    m = _ARTICLE_ONLY_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.ARTICLE, 300, m.group(1), line_start, line_end)

    m = _PART_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.PART, 350, m.group(1), line_start, line_end, m.group(2).strip())

    m = _PART_ONLY_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.PART, 350, m.group(1), line_start, line_end)

    m = _EXPLICIT_CLAUSE_RE.match(stripped)
    if m:
        nums = tuple(int(x) for x in m.group(1).split("."))
        return _Marker(
            ProvisionKind.CLAUSE,
            _clause_level(nums),
            m.group(1),
            line_start,
            line_end,
            m.group(2).strip(),
        )

    m = _EXPLICIT_CLAUSE_ONLY_RE.match(stripped)
    if m:
        nums = tuple(int(x) for x in m.group(1).split("."))
        return _Marker(ProvisionKind.CLAUSE, _clause_level(nums), m.group(1), line_start, line_end)

    m = _SUBPUNKT_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.SUBCLAUSE, 500, m.group(1), line_start, line_end, m.group(2).strip())

    m = _SUBPUNKT_ONLY_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.SUBCLAUSE, 500, m.group(1), line_start, line_end)

    m = _LETTER_PAREN_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.SUBCLAUSE, 500, m.group(1), line_start, line_end, stripped[m.end(1) :].strip())

    m = _LETTER_DOT_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.SUBCLAUSE, 500, m.group(1), line_start, line_end, stripped[m.end() :].strip())

    m = _DIGIT_PAREN_RE.match(stripped)
    if m:
        return _Marker(ProvisionKind.SUBCLAUSE, 480, m.group(1), line_start, line_end, stripped[m.end(1) + 1 :].strip())

    m = _DOT_CLAUSE_RE.match(stripped)
    if m:
        nums = tuple(int(x) for x in m.group(1).split("."))
        body = stripped[m.end(0) :].strip()
        return _Marker(ProvisionKind.CLAUSE, _clause_level(nums), m.group(1), line_start, line_end, body)

    m = _DOT_CLAUSE_ONLY_RE.match(stripped)
    if m:
        nums = tuple(int(x) for x in m.group(1).split("."))
        return _Marker(ProvisionKind.CLAUSE, _clause_level(nums), m.group(1), line_start, line_end)

    return None


def _apply_marker(ctx: _Context, marker: _Marker) -> None:
    if marker.kind is ProvisionKind.CHAPTER:
        ctx.article = None
        ctx.part = None
        ctx.clause = ()
        ctx.subclause = None
        return
    if marker.kind is ProvisionKind.SECTION:
        ctx.article = None
        ctx.part = None
        ctx.clause = ()
        ctx.subclause = None
        return
    if marker.kind is ProvisionKind.APPENDIX:
        ctx.appendix = marker.label or "1"
        ctx.article = None
        ctx.part = None
        ctx.clause = ()
        ctx.subclause = None
        return
    if marker.kind is ProvisionKind.ARTICLE:
        ctx.article = marker.label
        ctx.part = None
        ctx.clause = ()
        ctx.subclause = None
        return
    if marker.kind is ProvisionKind.PART:
        ctx.part = marker.label
        ctx.clause = ()
        ctx.subclause = None
        return
    if marker.kind is ProvisionKind.CLAUSE:
        ctx.clause = tuple(int(x) for x in marker.label.split("."))
        ctx.subclause = None
        return
    if marker.kind is ProvisionKind.SUBCLAUSE:
        ctx.subclause = marker.label.lower()


def _is_structural_only(marker: _Marker) -> bool:
    """Заголовок без самостоятельного текста нормы — только контекст для следующих пунктов."""
    if marker.body_on_same_line:
        return False
    return marker.kind in {ProvisionKind.CHAPTER, ProvisionKind.SECTION}


def _is_top_level_clause(marker: _Marker) -> bool:
    return marker.kind is ProvisionKind.CLAUSE and "." not in marker.label


def _line_opens_attached_act(stripped: str) -> bool:
    return bool(_ATTACHED_ACT_START_RE.match(stripped) or _ATTACHED_ACT_TITLE_RE.match(stripped))


def _is_signature_line(stripped: str) -> bool:
    if not _SIGNATURE_ROLE_LINE_RE.match(stripped):
        return False
    return bool(_SIGNATURE_NAME_AT_END_RE.search(stripped))


def _split_signature_tail(text: str) -> tuple[str, str | None]:
    match = _SIGNATURE_TAIL_RE.search(text.strip())
    if not match:
        return text, None
    main = text[: match.start()].strip()
    signature = match.group(1).strip()
    if not main:
        return text, None
    return main, signature


def _marker_opens_attached_regulation(marker: _Marker, stripped: str, *, order_clauses_seen: int) -> bool:
    """Повторное «1. Общие положения» после пунктов приказа — начало утверждаемого акта."""
    if order_clauses_seen < 1 or not _is_top_level_clause(marker) or marker.label != "1":
        return False
    probe = marker.body_on_same_line or stripped
    return bool(_REGULATION_SECTION_HEADING_RE.match(probe))


def _is_appendix_list_marker(ctx: _Context, marker: _Marker) -> bool:
    """Нумерация внутри приложения (бланки, перечни) — не самостоятельные пункты НПА."""
    if ctx.appendix is None or ctx.article is not None:
        return False
    return marker.kind in {ProvisionKind.CLAUSE, ProvisionKind.SUBCLAUSE}


def _iter_line_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    pos = 0
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        start = pos
        end = pos + len(line)
        spans.append((start, end, line))
        pos = end + 1
    return spans


@dataclass
class _OpenSegment:
    kind: ProvisionKind
    source_ref: str
    char_start: int
    char_end: int
    level: int = 0
    parent_intro: str | None = None
    is_letter_subclause: bool = False
    lines: list[str] = field(default_factory=list)

    def append_line(self, raw_line: str, line_end: int) -> None:
        self.lines.append(raw_line)
        self.char_end = line_end

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


#: Граница между двумя маркерами нумерации, оставшимися в одной физической строке.


def _paragraph_chunks(text: str) -> list[tuple[str, int, int]]:
    """Параграфы по \\n\\n; при OCR-потоке — по одиночным \\n."""
    paragraphs = split_paragraphs(text)
    if len(paragraphs) > 1:
        return [(p.text.strip(), p.char_start, p.char_end) for p in paragraphs if p.text.strip()]

    if text.count("\n") < 2:
        body = text.strip()
        return [(body, 0, len(text))] if body else []

    chunks: list[tuple[str, int, int]] = []
    pos = 0
    for line in text.split("\n"):
        start = pos
        end = pos + len(line)
        if line.strip():
            chunks.append((line.strip(), start, end))
        pos = end + 1
    return chunks


def _close_segment(open_seg: _OpenSegment | None) -> tuple[str, int, int] | None:
    if open_seg is None:
        return None
    body = open_seg.text
    if not body:
        return None
    return body, open_seg.char_start, open_seg.char_end


def _segment_from_paragraphs(text: str) -> list[NormSegment]:
    segments: list[NormSegment] = []
    for idx, (body, c0, c1) in enumerate(_paragraph_chunks(text)):
        segments.append(
            NormSegment(
                segment_id=idx,
                text=body,
                char_start=c0,
                char_end=c1,
                source_ref=locus(PARAGRAPH, idx + 1),
                kind=ProvisionKind.PARAGRAPH,
            )
        )
    return segments


def _document_has_markers(line_spans: list[tuple[int, int, str]]) -> bool:
    for start, end, raw in line_spans:
        stripped = _strip_line(raw)
        if stripped and _detect_marker(stripped, start, end):
            return True
    return False


@dataclass
class _SegmentParser:
    """Построчный разбор текста НПА в список норм."""

    ctx: _Context = field(default_factory=_Context)
    open_seg: _OpenSegment | None = None
    pending_lines: list[str] = field(default_factory=list)
    pending_start: int | None = None
    pending_end: int = 0
    results: list[tuple[str, int, int, str, ProvisionKind, str | None, bool]] = field(default_factory=list)

    intro_stack: list[tuple[int, str]] = field(default_factory=list)
    saw_norm_marker: bool = False
    decree_caption_seen: bool = False
    norms_enabled: bool = False
    cover_letter_hints: bool = False
    decree_preamble_collecting: bool = False
    decree_operative: bool = False
    decree_top_clauses_seen: int = 0

    colon_list_intro: str | None = None
    colon_list_ref: str | None = None
    colon_list_level: int = 0
    colon_list_seq: int = 0

    def _reset_colon_list(self) -> None:
        self.colon_list_intro = None
        self.colon_list_ref = None
        self.colon_list_level = 0
        self.colon_list_seq = 0

    def _note_line_context(self, stripped: str) -> None:
        if not stripped:
            return
        if re.search(r"Приложение\s*:", stripped, re.IGNORECASE | re.UNICODE):
            self.cover_letter_hints = True
        if _DECREE_CAPTION_LINE_RE.match(stripped):
            self.decree_caption_seen = True

        if _ENACTING_FORMULA_RE.search(stripped):
            self.decree_caption_seen = True
            self.norms_enabled = True
        if not self.norms_enabled and self.decree_caption_seen and _DECREE_BODY_LINE_RE.match(stripped):
            self.norms_enabled = True

    def _flush_decree_preamble(self) -> None:
        if not self.pending_lines:
            self.pending_start = None
            self.decree_preamble_collecting = False
            return
        body = "\n".join(self.pending_lines).strip()
        if body:
            self.results.append(
                (body, self.pending_start or 0, self.pending_end, "преамбула", ProvisionKind.PREAMBLE, None, False)
            )
        self.pending_lines = []
        self.pending_start = None
        self.decree_preamble_collecting = False

    def _emit_signature(self, body: str, char_start: int, char_end: int) -> None:
        text = body.strip()
        if text:
            # Строка подписи («Министр И.О. Фамилия») не подлежит экспертизе.
            self.results.append((text, char_start, char_end, "подпись", ProvisionKind.SIGNATURE, None, False))
            self.saw_norm_marker = True

    def _end_decree_operative(self, line_start: int) -> None:
        self.close_open()
        self.decree_operative = False

    def _should_parse_marker(self, marker: _Marker) -> bool:
        if self.norms_enabled:
            return True
        if marker.kind is not ProvisionKind.CLAUSE:
            return True
        if "." in marker.label:
            return True
        if self.cover_letter_hints and not self.decree_caption_seen:
            return False
        return not self.decree_caption_seen

    def flush_pending_as_preamble(self, *, source_ref: str = "преамбула") -> None:
        if not self.pending_lines:
            self.pending_start = None
            return
        body = "\n".join(self.pending_lines).strip()
        if body and not self.saw_norm_marker:
            self.results.append(
                (body, self.pending_start or 0, self.pending_end, source_ref, ProvisionKind.PREAMBLE, None, False)
            )
        self.pending_lines = []
        self.pending_start = None

    def _pending_has_decree_caption(self) -> bool:
        return any(_DECREE_CAPTION_LINE_RE.match(_strip_line(line)) for line in self.pending_lines)

    def close_open(self) -> None:
        closed = _close_segment(self.open_seg)
        if closed:
            body, c0, c1 = closed
            assert self.open_seg is not None
            if self.decree_operative and self.open_seg.kind is ProvisionKind.CLAUSE:
                main, signature = _split_signature_tail(body)
                if signature:
                    body = main
                    c1 = c0 + len(main)
            else:
                signature = None
            if body:
                self.results.append(
                    (
                        body,
                        c0,
                        c1,
                        self.open_seg.source_ref,
                        self.open_seg.kind,
                        self.open_seg.parent_intro,
                        self.open_seg.is_letter_subclause,
                    )
                )
                self._push_intro_if_colon(self.open_seg.level, self.open_seg.kind, body)
            if signature:
                self._emit_signature(signature, c1, c1 + len(signature) + 1)
        self.open_seg = None

    def _push_intro_if_colon(self, level: int, kind: ProvisionKind, body: str) -> None:
        """Если пункт/подпункт оканчивается на «:», его вводная фраза наследуется вложенными."""
        if kind not in {ProvisionKind.CLAUSE, ProvisionKind.SUBCLAUSE}:
            return
        if not body.rstrip().endswith(":"):
            return
        intro = _last_nonempty_line(body)
        if intro:
            self.intro_stack.append((level, intro))

    def _parent_intro_for(self, level: int) -> str | None:
        while self.intro_stack and self.intro_stack[-1][0] >= level:
            self.intro_stack.pop()
        return self.intro_stack[-1][1] if self.intro_stack else None

    def start_segment(
        self,
        kind: ProvisionKind,
        ref: str,
        line_start: int,
        first_line: str,
        line_end: int,
        *,
        level: int = 0,
        is_letter_subclause: bool = False,
    ) -> None:
        self.close_open()
        # Новый маркер-сегмент закрывает текущий перечень после двоеточия.
        self._reset_colon_list()
        parent_intro = self._parent_intro_for(level) if kind in {ProvisionKind.CLAUSE, ProvisionKind.SUBCLAUSE} else None
        self.open_seg = _OpenSegment(
            kind=kind,
            source_ref=ref,
            char_start=line_start,
            char_end=line_end,
            level=level,
            parent_intro=parent_intro,
            is_letter_subclause=is_letter_subclause,
        )
        self.open_seg.append_line(first_line, line_end)

    def _maybe_spawn_abzac(self, raw: str, line_end: int) -> bool:
        "Абзац перечня после «:» → отдельный сегмент с вводной фразой как parent_intro."
        seg = self.open_seg
        if seg is None or seg.kind not in {ProvisionKind.CLAUSE, ProvisionKind.SUBCLAUSE, ProvisionKind.APPENDIX}:
            return False
        last = _last_nonempty_line(seg.text)
        if self.colon_list_intro is None:
            # Перечень открывается строкой-вводной, оканчивающейся на «:».
            if not last.endswith(":"):
                return False
            self.colon_list_intro = last
            self.colon_list_ref = seg.source_ref
            self.colon_list_level = seg.level + 1
            self.colon_list_seq = 0
        elif not last.endswith((";", ":")):
            self._reset_colon_list()
            return False
        self.colon_list_seq += 1
        ref = f"{self.colon_list_ref}, {locus(PARAGRAPH, self.colon_list_seq)}"
        intro, level = self.colon_list_intro, self.colon_list_level
        start = line_end - len(raw)
        self.close_open()
        self.open_seg = _OpenSegment(
            kind=ProvisionKind.SUBCLAUSE, source_ref=ref, char_start=start, char_end=line_end, level=level, parent_intro=intro
        )
        self.open_seg.append_line(raw, line_end)
        return True

    def feed_continuation(self, raw: str, line_end: int, *, empty: bool = False) -> None:
        stripped = _strip_line(raw) if not empty else ""
        if not empty and self.decree_operative and self.decree_top_clauses_seen >= 1 and _is_signature_line(stripped):
            self.close_open()
            self._emit_signature(stripped, line_end - len(raw), line_end)
            self.pending_end = line_end
            return

        if self.open_seg is not None:
            if not empty and self._maybe_spawn_abzac(raw, line_end):
                return
            self.open_seg.append_line("" if empty else raw, line_end)
            return

        if not empty:
            self._note_line_context(stripped)

        if not empty and self.decree_operative and _line_opens_attached_act(stripped):
            self._end_decree_operative(line_end - len(raw))

        if (
            not empty
            and self.pending_lines
            and stripped
            and _DECREE_CAPTION_LINE_RE.match(stripped)
            and not self._pending_has_decree_caption()
        ):
            self.decree_caption_seen = True
            self.flush_pending_as_preamble(source_ref="сопроводительная часть")

        if not empty and stripped and _DECREE_CAPTION_LINE_RE.match(stripped) and not self.decree_preamble_collecting:
            self.decree_caption_seen = True
            self.decree_preamble_collecting = True
            if self.pending_start is None:
                self.pending_start = line_end - len(raw)
            self.pending_lines.append(raw)
            self.pending_end = line_end
            return

        if self.decree_preamble_collecting:
            if not empty:
                if self.pending_start is None:
                    self.pending_start = line_end - len(raw)
                self.pending_lines.append(raw)
            self.pending_end = line_end
            return

        if self.pending_start is None and not empty:
            self.pending_start = line_end - len(raw)
        if not empty:
            self.pending_lines.append(raw)
        self.pending_end = line_end

    def feed_marker(self, marker: _Marker, raw: str, line_start: int, line_end: int) -> None:
        stripped = _strip_line(raw)
        if not self._should_parse_marker(marker):
            self.feed_continuation(raw, line_end)
            return

        if self.decree_preamble_collecting and self.norms_enabled and _is_top_level_clause(marker):
            self._flush_decree_preamble()
            self.decree_operative = True

        if self.decree_operative and _marker_opens_attached_regulation(
            marker, stripped, order_clauses_seen=self.decree_top_clauses_seen
        ):
            self._end_decree_operative(line_start)

        if (
            not self.saw_norm_marker
            and marker.kind not in {ProvisionKind.CHAPTER, ProvisionKind.SECTION}
            and not self.decree_preamble_collecting
        ):
            self.flush_pending_as_preamble()

        if marker.kind in {ProvisionKind.CHAPTER, ProvisionKind.SECTION} and _is_structural_only(marker):
            _apply_marker(self.ctx, marker)
            if marker.body_on_same_line:
                self.feed_continuation(raw, line_end)
            return

        if _is_structural_only(marker):
            _apply_marker(self.ctx, marker)
            return

        self.saw_norm_marker = True

        if _is_appendix_list_marker(self.ctx, marker):
            if self.open_seg is None:
                ref = self.ctx.format_ref(ProvisionKind.APPENDIX)
                self.start_segment(ProvisionKind.APPENDIX, ref, line_start, raw, line_end, level=marker.level)
            else:
                self.feed_continuation(raw, line_end)
            return

        _apply_marker(self.ctx, marker)
        if marker.kind in {ProvisionKind.CHAPTER, ProvisionKind.SECTION}:
            return

        if self.decree_operative and _is_top_level_clause(marker):
            self.decree_top_clauses_seen += 1

        ref = self.ctx.format_ref(marker.kind)
        is_letter_subclause = marker.kind is ProvisionKind.SUBCLAUSE and marker.level >= 500
        self.start_segment(
            marker.kind, ref, line_start, raw, line_end, level=marker.level, is_letter_subclause=is_letter_subclause
        )

    def parse(self, line_spans: list[tuple[int, int, str]]) -> list[tuple[str, int, int, str, ProvisionKind, str | None, bool]]:
        for line_start, line_end, raw in line_spans:
            stripped = _strip_line(raw)
            self._note_line_context(stripped)
            if not stripped:
                self.feed_continuation(raw, line_end, empty=True)
                continue
            marker = _detect_marker(stripped, line_start, line_end)
            if marker is None:
                self.feed_continuation(raw, line_end)
                continue
            self.feed_marker(marker, raw, line_start, line_end)

        self.close_open()
        if self.decree_preamble_collecting:
            self._flush_decree_preamble()
        self.flush_pending_as_preamble()
        return self.results


def _significant(text: str) -> str:
    """Значащие символы: пробельная раскладка при нормализации меняется, они — нет."""
    return "".join(char for char in text if not char.isspace())


def _significant_origins(reflow_text: str, origin: CharOrigin, start: int, end: int) -> list[int]:
    """Происхождение значащих символов сегмента в каноническом тексте."""
    upper = min(end, len(reflow_text), len(origin))
    return [
        source
        for index in range(max(start, 0), upper)
        if not reflow_text[index].isspace() and (source := origin[index]) is not None
    ]


def _project_to_canonical(
    segments: list[NormSegment],
    reflow_text: str,
    origin: CharOrigin,
    canonical_text: str,
) -> list[NormSegment]:
    """Переносит адреса сегментов в канонический текст по карте происхождения."""
    projected: list[NormSegment] = []
    for segment in segments:
        origins = _significant_origins(reflow_text, origin, segment.char_start, segment.char_end)
        if not origins:
            projected.append(
                (segment).model_copy(
                    update={
                        "address_status": SegmentAddressStatus.UNRESOLVED,
                        "address_reason": SegmentAddressReason.CANONICAL_ORIGIN_ABSENT,
                    }
                )
            )
            continue
        if any(later <= earlier for earlier, later in itertools.pairwise(origins)):
            projected.append(
                (segment).model_copy(
                    update={
                        "address_status": SegmentAddressStatus.UNRESOLVED,
                        "address_reason": SegmentAddressReason.ORIGIN_NOT_MONOTONIC,
                    }
                )
            )
            continue
        char_start, char_end = origins[0], origins[-1] + 1
        canonical_slice = canonical_text[char_start:char_end]
        if _significant(canonical_slice) != _significant(segment.text):
            projected.append(
                (segment).model_copy(
                    update={
                        "char_start": char_start,
                        "char_end": char_end,
                        "address_status": SegmentAddressStatus.UNRESOLVED,
                        "address_reason": SegmentAddressReason.CANONICAL_CONTENT_DIVERGED,
                    }
                )
            )
            continue
        projected.append(
            (segment).model_copy(
                update={
                    "text": canonical_slice,
                    "char_start": char_start,
                    "char_end": char_end,
                    "address_status": SegmentAddressStatus.RESOLVED,
                    "address_reason": None,
                }
            )
        )
    return projected


def segment_norms(text: str, *, normalize: bool = True) -> list[NormSegment]:
    "Разбивает текст проекта НПА на нормы / положения."
    if not text or not text.strip():
        return []

    canonical = clean_text(text) if normalize else text
    doc, origin = reflow_with_origin(canonical, identity_origin(canonical))
    line_spans = _iter_line_spans(doc)
    if not _document_has_markers(line_spans):
        return _project_to_canonical(_segment_from_paragraphs(doc), doc, origin, canonical)

    raw_results = _SegmentParser().parse(line_spans)
    if not raw_results:
        return _project_to_canonical(_segment_from_paragraphs(doc), doc, origin, canonical)

    segments = [
        NormSegment(
            segment_id=idx,
            text=body,
            char_start=c0,
            char_end=c1,
            source_ref=ref,
            kind=kind,
            parent_intro=parent_intro,
            is_letter_subclause=is_letter_subclause,
        )
        for idx, (body, c0, c1, ref, kind, parent_intro, is_letter_subclause) in enumerate(raw_results)
    ]

    return _project_to_canonical(segments, doc, origin, canonical)
