"""Разрешение отсылок в доказанном адресном пространстве и проверка предмета адресата."""

from __future__ import annotations

import re

from src.agents.enums.internal.addressing.address_space import AddressSpace
from src.agents.schemas.internal.addressing.address_space import Address, AddressBook, RefResolution, Region, SubjectMismatch
from src.agents.schemas.parse.references import InternalRef
from src.agents.steps_tools.document.locus_labels import APPENDIX
from src.agents.steps_tools.document.morphology import normalized_text

_APPENDIX_LABEL = re.compile(rf"{re.escape(APPENDIX)}\s*(\d+)", re.IGNORECASE | re.UNICODE)


def _source_appendix(label: str) -> str | None:
    """Номер приложения, внутри которого физически находится ссылка (по метке дерева)."""
    m = _APPENDIX_LABEL.search(label or "")
    return m.group(1) if m else None


def _appendix_at(book: AddressBook, pos: int) -> str | None:
    "Приложение, ФИЗИЧЕСКИ содержащее офсет ссылки (структурный факт, не догадка)."
    numbers = {
        region.appendix for region in book.regions if region.appendix is not None and region.char_start <= pos < region.char_end
    }
    return numbers.pop() if len(numbers) == 1 else None


def _source_appendix_proved(book: AddressBook, source_label: str, source_pos: int | None) -> str | None:
    "Приложение-источник ссылки: метка дерева и фактический офсет."
    by_label = _source_appendix(source_label)
    by_offset = _appendix_at(book, source_pos) if source_pos is not None else None
    if by_label is not None and by_offset is not None and by_label != by_offset:
        return None
    return by_label if by_label is not None else by_offset


def _logical_owner(candidates: list[Region]) -> list[Region] | None:
    "Регионы ОДНОГО логического пространства, либо ``None``, если целей несколько."
    if not candidates:
        return None
    return candidates if len({(region.appendix, region.table) for region in candidates}) == 1 else None


def _table_owner(book: AddressBook, appendix: str | None, table: str, context: str | None) -> list[Region] | None:
    "Допустимое пространство таблицы — ДО проверки самого адреса."
    if appendix is not None:
        return _logical_owner(book.table_regions(appendix, table))
    if context is not None:
        scoped = _logical_owner(book.table_regions(context, table))
        if scoped is not None:
            return scoped
    return _logical_owner(book.table_regions(None, table))


def _resolve_in_owner(book: AddressBook, addr: Address, owner: list[Region] | None) -> RefResolution:
    "Проверка адреса ВНУТРИ уже доказанного пространства."
    if owner is None:
        return RefResolution(address=addr, status="unknown", reason="владелец адреса не доказан")
    states = [(region, book.region_has(region, addr.item)) for region in owner]
    proved = [region for region, state in states if state is True]
    single = owner[0] if len(owner) == 1 else None
    if proved:
        return RefResolution(
            address=_located(addr, owner[0]),
            status="exists",
            region=proved[0] if len(proved) == 1 else None,
        )
    if any(state is None for _region, state in states):
        return RefResolution(address=addr, status="unknown", region=single, reason="нумерация пространства не разобрана")
    return RefResolution(address=addr, status="missing", region=single)


def _located(addr: Address, region: Region | None) -> Address:
    """Уточняет локус номером приложения, ЕСЛИ владелец адреса доказан и единственный."""
    if region is None or addr.appendix is not None or region.appendix is None:
        return addr
    return Address(space=addr.space, item=addr.item, appendix=region.appendix, table=addr.table)


def resolve_reference(
    book: AddressBook,
    ref: InternalRef,
    *,
    source_label: str = "",
    source_pos: int | None = None,
) -> RefResolution:
    "Определяет пространство адресата отсылки и проверяет его существование."
    item, appendix, table = ref.address, ref.appendix, ref.table
    own = _source_appendix_proved(book, source_label, source_pos)

    if table is not None:
        addr = Address(space=AddressSpace.TABLE, item=item, appendix=appendix, table=table)
        return _resolve_in_owner(book, addr, _table_owner(book, appendix, table, own))

    if appendix is not None:
        addr = Address(space=AddressSpace.APPENDIX, item=item, appendix=appendix)
        return _resolve_in_owner(book, addr, _logical_owner(book.appendix_regions(appendix)))

    if source_pos is not None and book.in_amendment(source_pos):
        return RefResolution(
            address=Address(space=AddressSpace.AMENDED_ACT, item=item),
            status="external",
            reason="отсылка сделана в предписании о внесении изменений — адресат в изменяемом акте",
        )

    if own is not None:
        owner = _logical_owner(book.appendix_regions(own))
        addr = Address(space=AddressSpace.APPENDIX, item=item, appendix=own)
        here = _resolve_in_owner(book, addr, owner)
        if here.status == "exists":
            return here
        core = book.core_has(item)
        if core:
            return RefResolution(address=Address(space=AddressSpace.CORE, item=item), status="exists")
        # Хотя бы одна из двух нумераций не разобрана → отсутствие адресата НЕ доказано.
        if core is None or here.status != "missing":
            return RefResolution(address=addr, status="unknown", region=here.region, reason="нумерация пространства не разобрана")

        return RefResolution(address=Address(space=AddressSpace.CORE, item=item), status="missing", region=here.region)

    core = book.core_has(item)
    addr = Address(space=AddressSpace.CORE, item=item)
    if core is None:
        return RefResolution(address=addr, status="unknown", reason="нумерация основного текста не разобрана")
    return RefResolution(address=addr, status="exists" if core else "missing")


_SUBJECT_STEMS = (
    "фотограф",
    "свидетельств",
    "удостоверени",
    "справк",
    "паспорт",
    "доверенност",
    "выписк",
    "договор",
    "сертификат",
    "диплом",
    "аттестат",
    "полис",
    "заявлени",
    "документ",
)


def subject_stem(context: str) -> tuple[str, str] | None:
    "Предмет отсылки — вид документа, названный НЕПОСРЕДСТВЕННО перед отсылкой."
    hay = normalized_text(context)
    best: tuple[int, str] | None = None
    for stem in _SUBJECT_STEMS:
        pos = hay.rfind(stem)
        if pos >= 0 and (best is None or pos > best[0]):
            best = (pos, stem)
    if best is None:
        return None
    pos, stem = best
    word = re.match(r"\w*", hay[pos:])
    return stem, (word.group(0) if word else stem)


def check_subject(region: Region | None, addr: Address, context: str) -> SubjectMismatch | None:
    "Совпадает ли предмет отсылки с содержанием строки-адресата."
    if region is None or region.space is not AddressSpace.TABLE:
        return None
    if addr.item in region.geometry_fragmented or addr.item in region.geometry_unknown:
        return None
    target_text = region.items.get(addr.item, "")
    if len(target_text) < 30:
        return None
    found = subject_stem(context)
    if found is None:
        return None
    stem, subject = found
    if stem in normalized_text(target_text):
        return None
    hits = [num for num, body in region.items.items() if num != addr.item and stem in normalized_text(body)]
    if len(hits) != 1:
        return None
    return SubjectMismatch(
        stem=stem,
        subject=subject,
        target=addr,
        target_text=target_text,
        correct=Address(space=AddressSpace.TABLE, item=hits[0], appendix=addr.appendix, table=addr.table),
    )
