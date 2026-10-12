from __future__ import annotations

import re

from src.agents.schemas.parse.structure import DocumentStructureNode, DocumentStructureTree
from src.agents.schemas.repeal.detector import (
    RepealTarget,
)
from src.agents.steps_tools.repeal.units import (
    issuer_from_reference,
    known_act_date,
)
from src.agents.steps_tools.sources.normalization import card_date

_EDITING_TITLE = re.compile(r"о\s+внесении\s+изменени|о\s+призна\w*\s+утратив", re.IGNORECASE)


_STOP_CLAUSE = re.compile(r"вступает\s+в\s+силу|контрол[ья]\s+за\s+исполнением", re.IGNORECASE)


_NUM = r"\d+\s*-\s*[А-Яа-яЁёA-Za-z]"  # «21-п», «11-П», «146-п»


_DATE = r"\d{1,2}\.\d{1,2}\.\d{4}"


_ACT_ANCHOR = re.compile(
    rf"(?P<kind>приказ)[а-яё]*\b.{{0,200}}?от\s+(?P<date>{_DATE})\s*(?:г\.?\s*)?[№N]\s*(?P<num>{_NUM})",
    re.IGNORECASE | re.DOTALL,
)


_SUBPOINT_BEFORE = re.compile(r"(?:под)?пункт[а-яё]*\s+(?P<sub>[\w.«»\"]+)\s*$", re.IGNORECASE)


def norm_act_num(raw: str) -> str:
    "«11-П» / «N 21-п» / «146 - п» → канон «11-п» (цифры + дефис + строчный суффикс, ё→е)."
    m = re.search(r"(\d+)\s*-\s*([А-Яа-яЁёA-Za-z]+)", raw or "")
    if not m:
        return ""
    return f"{m.group(1)}-{m.group(2).lower().replace('ё', 'е')}"


def extract_repealed_acts(clause_text: str) -> list[RepealTarget]:
    "Шаг 1: из текста пункта-отмены проекта вытащить отменяемые акты."
    out: list[RepealTarget] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for m in _ACT_ANCHOR.finditer(clause_text or ""):
        number = norm_act_num(m.group("num"))
        if not number:
            continue
        before = (clause_text or "")[max(0, m.start() - 40) : m.start()]
        sub_m = _SUBPOINT_BEFORE.search(before)
        is_sub = sub_m is not None
        subpoint = sub_m.group("sub").strip('«»"') if sub_m else None
        date = card_date(m.group("date"))
        key = (issuer_from_reference(m.group(0)).casefold(), date, number, subpoint if is_sub else None)
        if key in seen:
            continue
        seen.add(key)
        # хвост после номера до «;» (или +250 симв.) — заголовок акта в «…» для запроса
        tail = (clause_text or "")[m.end() : m.end() + 250]
        title = tail.split(";")[0].strip(' «»"\n')
        out.append(
            RepealTarget(
                number=number,
                date=date,
                is_subpoint=is_sub,
                subpoint=subpoint,
                raw=m.group(0).strip(),
                title=title,
                kind=(m.group("kind") or "").lower(),
            )
        )
    return out


_REPEAL_MARKER = re.compile(
    r"^[ \t]*(?:\d+(?:\.\d+)*[.)][ \t]*)?"
    r"(?:призна(?:ть|ю|ем|ются|ется|ётся)\b\s+утративш[а-яё]{0,6}\s+силу\b|"
    r"отмен(?:ить|яется|яются)\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _node_full_text(node: DocumentStructureNode) -> str:
    """Текст узла + всех потомков по порядку (список отменяемых актов лежит в детях п.2)."""
    parts = [node.text]
    for ch in node.children:
        child_text = _node_full_text(ch)
        if child_text not in (node.text):
            parts.append(child_text)
    return "\n".join(p for p in parts if p)


def _preceding_char(text: str, position: int) -> str:
    """Nearest non-space character before ``position``; the start of ``text`` reads as a line break."""
    index = position - 1
    while index >= 0 and text[index] in " \t\r":
        index -= 1
    return text[index] if index >= 0 else "\n"


def _inside_quotation(text: str, position: int) -> bool:
    "Do not treat an instruction quoted as replacement text as a project order."
    endings: list[tuple[str, bool]] = []
    opening = {"«": "»", "„": "“", "“": "”", '"': '"'}
    for index, char in enumerate(text[:position]):
        if endings and char == endings[-1][0]:
            endings.pop()
        elif char in opening:
            endings.append((opening[char], _preceding_char(text, index) in "\n:"))
    if _preceding_char(text, position) == "\n":
        endings = [ending for ending in endings if ending[1]]
    return bool(endings)


def _node_offset(parent: DocumentStructureNode, node: DocumentStructureNode) -> int | None:
    """Положение текста узла в содержащем его тексте предка."""
    text, parent_text = node.text, parent.text
    start = node.char_start - parent.char_start
    if start >= 0 and parent_text[start : start + len(text)] == text:
        return start
    start = parent_text.find(text)
    return start if start >= 0 and parent_text.find(text, start + 1) < 0 else None


def _has_operative_repeal_marker(node: DocumentStructureNode, ancestors: tuple[DocumentStructureNode, ...]) -> bool:
    text = node.text
    for match in _REPEAL_MARKER.finditer(text):
        if _inside_quotation(text, match.start()):
            continue
        # Текст корня и некоторых предков включает текст детей. Его проверит сам ребёнок.
        if any(
            (start := _node_offset(node, child)) is not None and start <= match.start() < start + len(child.text)
            for child in node.children
        ):
            continue
        quoted_in_parent = False
        for parent in ancestors:
            parent_text = parent.text

            start = _node_offset(parent, node)
            if start is None:
                continue
            if _inside_quotation(parent_text, start + match.start()):
                quoted_in_parent = True
                break
        if not quoted_in_parent:
            return True
    return False


def find_repeal_clauses(tree: DocumentStructureTree) -> list[DocumentStructureNode]:
    "Самостоятельные пункты отмены в порядке документа, без цитируемых инструкций."
    matches: list[DocumentStructureNode] = []

    def _collect(node: DocumentStructureNode, ancestors: tuple[DocumentStructureNode, ...] = ()) -> None:
        if _has_operative_repeal_marker(node, ancestors):
            matches.append(node)
        for ch in node.children:
            _collect(ch, (*ancestors, node))

    _collect(tree.root)
    return matches


_AMENDING_TITLE = re.compile(r"о\s+внесении\s+изменени", re.IGNORECASE)


def _is_amending_act(title: str) -> bool:
    return bool(_AMENDING_TITLE.search(title or ""))


def title_declares_editing(title: str) -> bool:
    return bool(_EDITING_TITLE.search(title or ""))


def _parent_of(root: DocumentStructureNode, target: DocumentStructureNode) -> DocumentStructureNode | None:
    for ch in root.children:
        if ch is target:
            return root
        p = _parent_of(ch, target)
        if p is not None:
            return p
    return None


def repeal_clause_full_text(tree: DocumentStructureTree, marker_node: DocumentStructureNode) -> str:
    "Полный текст пункта отмены с продолжением перечня в следующих узлах."
    parent = _parent_of(tree.root, marker_node)
    parts = [_node_full_text(marker_node)]
    sibs = parent.children if parent is not None else []
    if marker_node in sibs:
        for sib in sibs[sibs.index(marker_node) + 1 :]:
            text = _node_full_text(sib)
            if _REPEAL_MARKER.search(sib.text) or _STOP_CLAUSE.search(sib.text) or not _ACT_ANCHOR.search(text):
                break
            parts.append(text)
    return "\n".join(parts)


def collect_repeal_targets(tree: DocumentStructureTree) -> list[tuple[RepealTarget, DocumentStructureNode, str]]:
    """Отменяемые акты вместе с пунктом проекта; один акт проверяется один раз."""
    targets = []
    seen = set()
    for clause in find_repeal_clauses(tree):
        text = repeal_clause_full_text(tree, clause)
        for target in extract_repealed_acts(text):
            key = (target.kind, issuer_from_reference(target.raw).casefold(), target.date, target.number, target.subpoint)
            if key not in seen:
                seen.add(key)
                targets.append((target, clause, text))
    return targets


def act_identity(number: str, date: str | None) -> tuple[str, str]:
    """Comparable exact identity; an unavailable date remains explicitly empty."""
    return known_act_date(date) or "", norm_act_num(number)
