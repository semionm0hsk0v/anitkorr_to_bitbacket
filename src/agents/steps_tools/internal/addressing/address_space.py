"""Построение адресной книги проекта: основной текст, приложения, таблицы и изменяемые акты."""

from __future__ import annotations

import re

from src.agents.enums.internal.addressing.address_space import AddressSpace
from src.agents.schemas.internal.addressing.address_space import AddressBook, Region
from src.agents.schemas.parse.structure import DocumentStructureTree
from src.agents.steps_tools.document.references import collect_internal_addresses
from src.agents.steps_tools.internal.addressing.table_overlay import apply_table_geometry
from src.common.config.config import settings

_APPENDIX_HEAD = re.compile(
    r"^[\s_]*(?:ПРИЛОЖЕНИЕ|Приложение)(?![а-яё])\s*(?:№\s*)?(\d{1,2})?\s*(?P<rest>.*)$",
    re.UNICODE,
)


_APPENDIX_HEAD_TAIL = re.compile(r"(?:ПРИЛОЖЕНИЕ|Приложение)(?![а-яё])\s*(?:№\s*)?(\d{1,2})\s*$", re.UNICODE)


_TABLE_HEAD = re.compile(r"(?:Таблица|ТАБЛИЦА)(?![а-яё])\s*(?:№\s*)?(\d{1,2})\s*$", re.UNICODE)


_ITEM_HEAD = re.compile(
    r"^\s*(?:(?P<dotted>\d{1,2}(?:\.\d+)*)\.\s+\S|(?P<bare>\d{1,2}(?:\.\d+)+)\s+[А-ЯЁ])",
    re.UNICODE,
)


_ITEM_NUMBER_ALONE = re.compile(
    r"^\s*(?:(?P<dotted>\d{1,2}(?:\.\d{1,3})*)\.|(?P<bare>\d{1,2}(?:\.\d{1,3})+))\s*$",
    re.UNICODE,
)

_ITEM_TEXT_LOOKAHEAD = int(settings["agents.internal.address_space.item_text_lookahead"])


def _item_number_at(lines: list[tuple[int, int, str]], idx: int) -> str | None:
    """Номер пункта, заголовок которого лежит в строке ``idx``: на одной строке с текстом либо отдельной строкой."""
    line = lines[idx][2]
    head = _ITEM_HEAD.match(line)
    if head is not None:
        return head.group("dotted") or head.group("bare")
    alone = _ITEM_NUMBER_ALONE.match(line)
    if alone is None:
        return None
    text = next((nxt.strip() for _s, _e, nxt in lines[idx + 1 : idx + 1 + _ITEM_TEXT_LOOKAHEAD] if nxt.strip()), "")
    if alone.group("dotted") is not None:
        return alone.group("dotted") if text[:1].isalpha() else None
    return alone.group("bare") if text[:1].isupper() else None


_APPENDIX_ATTACH = re.compile(r"^\s*к\s+(?!настоящ)", re.IGNORECASE | re.UNICODE)
#: Формула, открывающая резолютивную часть. OCR разряжает буквы («п р и к а з ы в а ю»).
_ENACTING = re.compile(r"п\s*р\s*и\s*к\s*а\s*з\s*ы\s*в\s*а\s*ю|постановляю|распоряжаюсь", re.IGNORECASE | re.UNICODE)
#: Наименование проекта о внесении изменений.
_AMENDMENT_CAPTION = re.compile(r"о\s+внесении\s+изменени", re.IGNORECASE | re.UNICODE)

_AMENDMENT_ORDER = re.compile(r"внести\s+в\b[\s\S]{0,400}?измен", re.IGNORECASE | re.UNICODE)
#: Пункт верхнего уровня резолютивной части («2. Настоящий приказ вступает в силу…»).
_TOP_CLAUSE = re.compile(r"^\s*(\d{1,2})\.\s+\S", re.UNICODE)
#: Сколько символов от начала считать наименованием, если формулы «приказываю» нет.
_MAX_CAPTION_CHARS = int(settings["agents.internal.address_space.max_caption_chars"])


def _lines(text: str) -> list[tuple[int, int, str]]:
    """(char_start, char_end, строка) — офсеты в исходном тексте."""
    out: list[tuple[int, int, str]] = []
    pos = 0
    # split без нормализации переводов строки: офсеты обязаны совпадать с исходным текстом
    for line in (text or "").split("\n"):
        out.append((pos, pos + len(line), line))
        pos += len(line) + 1
    return out


def _caption(text: str) -> str:
    """Наименование проекта: всё до формулы «приказываю» (или первые 1500 символов)."""
    m = _ENACTING.search(text or "")
    end = m.start() if m else _MAX_CAPTION_CHARS
    return (text or "")[: min(end, _MAX_CAPTION_CHARS)]


def is_amendment_project(text: str) -> bool:
    "Проект о внесении изменений: наименование начинается с «О внесении изменений в…»."
    return _AMENDMENT_CAPTION.search(_caption(text)) is not None


def amendment_spans(text: str) -> list[tuple[int, int]]:
    "Участки, где отсылки адресуют ИЗМЕНЯЕМЫЙ акт, а не проект."
    body = text or ""
    if is_amendment_project(body):
        m = _ENACTING.search(body)
        return [(m.end() if m else 0, len(body))]

    lines = _lines(body)
    spans: list[tuple[int, int]] = []
    for match in _AMENDMENT_ORDER.finditer(body):
        opened = False
        next_number = 0
        for start, _end, line in lines:
            top = _TOP_CLAUSE.match(line)
            if not opened:
                if start > match.start():
                    break
                if start <= match.start() < start + len(line) + 1:
                    opened = True
                    span_start = start
                    next_number = (int(top.group(1)) + 1) if top is not None else 0
                continue
            if top is not None and int(top.group(1)) == next_number:
                spans.append((span_start, start))
                break
        else:
            if opened:
                spans.append((span_start, len(body)))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Склеивает пересекающиеся участки (одно предписание может дать несколько совпадений)."""
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _is_appendix_heading(line: str, following: list[str]) -> tuple[bool, str | None]:
    "Заголовок приложения? Возвращает (да/нет, номер)."

    def attached() -> bool:
        for nxt in following:
            if not nxt.strip():
                continue
            return bool(_APPENDIX_ATTACH.match(nxt))
        return False

    m = _APPENDIX_HEAD.match(line)
    if m is not None:
        rest = (m.group("rest") or "").strip(" \t:.")
        number = m.group(1)
        if rest:
            return (bool(_APPENDIX_ATTACH.match(rest)), number)
        return (True, number) if not following else (attached(), number)

    tail = _APPENDIX_HEAD_TAIL.search(line)
    if tail is not None and attached():
        return True, tail.group(1)
    return False, None


def _precedes_table_head(lines: list[tuple[int, int, str]], idx: int, window: int = 4) -> bool:
    "Строка ``idx`` — заголовок раздела перед шапкой новой таблицы, а не строка таблицы?"
    for position in range(idx + 1, min(len(lines), idx + 1 + window)):
        nxt = lines[position][2]
        if not nxt.strip():
            continue
        if _TABLE_HEAD.search(nxt):
            return True
        if _item_number_at(lines, position) is not None:
            return False
    return False


def _table_ended(num: str, last_row: int, lines: list[tuple[int, int, str]], idx: int) -> bool:
    "Нумерованная строка внутри таблицы уже НЕ строка таблицы?"
    if "." in num:
        return True
    if int(num.split(".", 1)[0]) <= last_row:
        return True
    return _precedes_table_head(lines, idx)


def _scan_regions(text: str) -> list[Region]:  # noqa: PLR0915 — линейный однопроходный сканер, дробить смысла нет
    """Разбивает документ на регионы CORE / APPENDIX / TABLE и наполняет их реестры."""
    body = text or ""
    lines = _lines(body)
    core = Region(space=AddressSpace.CORE, char_start=0, char_end=len(body))
    regions: list[Region] = [core]
    current = core
    appendix_region: Region | None = None
    appendix_no: str | None = None
    appendix_seq = 0
    last_row = 0
    open_item: tuple[str, int] | None = None  # (номер, начало текста пункта)

    def close_item(at: int) -> None:
        nonlocal open_item
        if open_item is not None:
            num, item_start = open_item
            if num not in current.items:
                raw = body[item_start:at]
                text = raw.strip()
                current.items[num] = text
                if text:
                    lo = item_start + len(raw) - len(raw.lstrip())
                    current.item_spans[num] = (lo, lo + len(text))
            open_item = None

    def close_region(at: int) -> None:
        close_item(at)
        current.char_end = at

    for idx, (start, end, line) in enumerate(lines):
        following = [ln for _s, _e, ln in lines[idx + 1 : idx + 4]]
        heading, number = _is_appendix_heading(line, following)
        if heading:
            close_region(start)
            appendix_seq += 1
            appendix_no = number or str(appendix_seq)
            appendix_region = current = Region(
                space=AddressSpace.APPENDIX, char_start=start, char_end=len(body), appendix=appendix_no
            )
            regions.append(current)
            last_row = 0
            continue

        table_m = _TABLE_HEAD.search(line)
        if table_m is not None and appendix_region is not None:
            close_region(end)
            current = Region(
                space=AddressSpace.TABLE, char_start=end, char_end=len(body), appendix=appendix_no, table=table_m.group(1)
            )
            regions.append(current)
            last_row = 0
            continue

        num = _item_number_at(lines, idx)
        if num is None:
            continue
        if current.space is AddressSpace.TABLE and _table_ended(num, last_row, lines, idx):
            close_region(start)
            current = appendix_region if appendix_region is not None else core
            current.char_end = len(body)
            last_row = 0
        elif current.space is AddressSpace.TABLE:
            last_row = int(num.split(".", 1)[0])
        close_item(start)
        open_item = (num, start)

    close_region(len(body))
    return regions


def _tree_core_addresses(tree: DocumentStructureTree | None) -> set[str]:
    "Адреса пунктов ОСНОВНОГО текста по меткам дерева (приложения исключены)."
    return set() if tree is None else collect_internal_addresses(tree, outside_appendices=True)


def build_address_book(
    text: str, tree: DocumentStructureTree | None = None, *, table_geometry: dict | None = None
) -> AddressBook:
    """Собирает адресную книгу проекта по сырому тексту (+ дерево для основного текста)."""
    body = text or ""
    regions = _scan_regions(body)
    if table_geometry is not None:
        apply_table_geometry(body, regions, table_geometry)
    return AddressBook(
        regions=regions,
        core_addresses=_tree_core_addresses(tree),
        is_amendment=is_amendment_project(body),
        amendment_spans=amendment_spans(body),
    )
