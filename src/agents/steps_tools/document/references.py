from __future__ import annotations

import re
from dataclasses import dataclass

from src.agents.enums.parse.references import AddressLevel, UnresolvedReason
from src.agents.schemas.parse.references import InternalRef, ReferenceScan, UnresolvedChain
from src.agents.schemas.parse.structure import DocumentStructureNode, DocumentStructureTree
from src.agents.steps_tools.document.morphology import ORDINAL_WORD

_DOTTED = re.compile(r"\d+(?:\.\d+)*")
_ADDRESS_KINDS = {"clause", "article", "section", "part", "chapter"}


_INTERNAL_REF = re.compile(
    r"(?P<chain>(?:(?:под)?пункт\w*|пп?\.|раздел\w*|разд\.|глав\w*|абзац\w*)[\s\S]{0,90}?)"
    r"настоящ\w*\s+(?:админ\w*\s+)?(?P<act>регламент\w*|положени\w*|приказ\w*|порядк\w*|правил\w*)",
    re.IGNORECASE,
)

#: Перечисление номеров одного уровня: «2», «2 и 9», «1, 2 и 9», «1.2».
_ENUMERATION = r"\d+(?:\.\d+)*(?:\s*(?:,|и)\s*\d+(?:\.\d+)*)*"


_LEVEL_MARKERS: tuple[tuple[AddressLevel, str], ...] = (
    (AddressLevel.SUBCLAUSE, r"(?<![а-яё])подпункт\w*|(?<![а-яёa-z.])пп\.|(?<![а-яёa-z.])подп\."),
    (AddressLevel.CLAUSE, r"(?<![а-яё])пункт\w*|(?<![а-яёa-z.])п\."),
    (AddressLevel.SECTION, r"(?<![а-яё])раздел\w*|(?<![а-яёa-z.])разд\."),
    (AddressLevel.CHAPTER, r"(?<![а-яё])глав\w*"),
    (AddressLevel.PARAGRAPH, r"(?<![а-яё])абзац\w*"),
    (AddressLevel.PART, r"(?<![а-яё])част\w*"),
)

#: Для каждого уровня — «маркер + перечисление номеров» и «маркер + порядковое слово».
_NUMBERED_BY_LEVEL: tuple[tuple[AddressLevel, re.Pattern[str]], ...] = tuple(
    (level, re.compile(rf"(?:{marker})\s*(?:№\s*)?(?P<nums>{_ENUMERATION})", re.IGNORECASE)) for level, marker in _LEVEL_MARKERS
)
_ORDINAL_BY_LEVEL: tuple[tuple[AddressLevel, re.Pattern[str]], ...] = tuple(
    (level, re.compile(rf"(?:{marker})\s+{ORDINAL_WORD}", re.IGNORECASE)) for level, marker in _LEVEL_MARKERS
)


_TARGET_PRIORITY: tuple[AddressLevel, ...] = (AddressLevel.CLAUSE, AddressLevel.SUBCLAUSE, AddressLevel.SECTION)

#: Квалификаторы пространства внутри цепочки.
_TABLE_QUALIFIER = re.compile(r"таблиц\w*\s*(?:№\s*)?(\d{1,2})", re.IGNORECASE)
_APPENDIX_QUALIFIER = re.compile(r"приложени\w*\s*(?:№\s*)?(\d{1,2})", re.IGNORECASE)


def collect_internal_addresses(tree: DocumentStructureTree, *, outside_appendices: bool = False) -> set[str]:
    "Множество числовых адресов проекта по меткам узлов дерева."
    addrs: set[str] = set()

    def visit(node: DocumentStructureNode, inside_appendix: bool) -> None:
        inside_appendix = inside_appendix or node.kind == "appendix"
        if node.kind in _ADDRESS_KINDS and not (outside_appendices and inside_appendix):
            addrs.update(_DOTTED.findall(node.label or ""))
        for child in node.children:
            visit(child, inside_appendix)

    visit(tree.root, False)
    return addrs


@dataclass(frozen=True)
class _AddressComponent:
    """Звено адресной цепочки: уровень + все перечисленные на нём номера."""

    level: AddressLevel
    numbers: tuple[str, ...]
    at: int


def _components(chain: str) -> list[_AddressComponent]:
    """Все числовые звенья цепочки в порядке появления."""
    found: list[_AddressComponent] = []
    for level, pattern in _NUMBERED_BY_LEVEL:
        for m in pattern.finditer(chain):
            numbers = tuple(_DOTTED.findall(m.group("nums")))
            if numbers:
                found.append(_AddressComponent(level=level, numbers=numbers, at=m.start()))
    found.sort(key=lambda component: component.at)
    return found


def _target(components: list[_AddressComponent]) -> _AddressComponent | None:
    "Адресат цепочки: пункт > точечный подпункт > раздел; при повторах — последний."
    for level in _TARGET_PRIORITY:
        candidates = [c for c in components if c.level is level]
        if level is AddressLevel.SUBCLAUSE:
            candidates = [c for c in candidates if all("." in number for number in c.numbers)]
        if candidates:
            return candidates[-1]
    return None


def _unresolved_reason(chain: str, components: list[_AddressComponent]) -> tuple[UnresolvedReason, str]:
    """Почему у цепочки нет адресата — кодом и прозой."""
    if components:
        levels = ", ".join(sorted({c.level.value for c in components}))
        return (
            UnresolvedReason.ADDRESS_LEVEL_UNSUPPORTED,
            f"уровень адресации не поддержан реестрами ({levels}) — отсылка не проверена",
        )
    if any(pattern.search(chain) for _, pattern in _ORDINAL_BY_LEVEL):
        return (
            UnresolvedReason.ADDRESS_NOT_NUMERIC,
            "номер адресата записан порядковым словом — числовой адрес не разобран",
        )
    return (UnresolvedReason.ADDRESS_ABSENT, "в цепочке отсылки не назван номер адресата")


def _qualifiers(chain: str) -> tuple[str | None, str | None]:
    """(номер приложения, номер таблицы) из цепочки отсылки."""
    table = _TABLE_QUALIFIER.search(chain)
    appendix = _APPENDIX_QUALIFIER.search(chain)
    return (appendix.group(1) if appendix else None, table.group(1) if table else None)


def canonical_reference_address(text: str) -> str:
    """The same numbered address in either writing order; unknown addresses stay unknown."""
    components = _components(text)
    if not components:
        return ""
    remainder = text
    for _, pattern in _NUMBERED_BY_LEVEL:
        remainder = pattern.sub("", remainder)
    remainder = _APPENDIX_QUALIFIER.sub("", _TABLE_QUALIFIER.sub("", remainder))
    if remainder.strip(" ,.;:()«»"):
        return ""  # Preserve lettered/ordinal leaves and other unparsed qualifications.
    appendix, table = _qualifiers(text)
    parts = sorted(f"{item.level.value}:{','.join(item.numbers)}" for item in components)
    return "|".join([f"appendix:{appendix or ''}", f"table:{table or ''}", *parts])


def scan_internal_refs(text: str) -> ReferenceScan:
    """Полный разбор внутренних отсылок узла: адреса + неразрешённые цепочки с причиной."""
    scan = ReferenceScan()
    for m in _INTERNAL_REF.finditer(text):
        chain = m.group("chain")
        act = (m.group("act") or "").lower()
        components = _components(chain)
        target = _target(components)
        if target is None:
            code, reason = _unresolved_reason(chain, components)
            scan.unresolved.append(
                UnresolvedChain(
                    phrase=chain.strip(),
                    char_start=m.start("chain"),
                    act=act,
                    reason_code=code,
                    reason=reason,
                )
            )
            continue
        appendix, table = _qualifiers(chain)
        for number in target.numbers:
            scan.refs.append(
                InternalRef(
                    address=number,
                    phrase=chain.strip(),
                    char_start=m.start("chain"),
                    appendix=appendix,
                    table=table,
                    act=act,
                    level=target.level,
                )
            )
    return scan
