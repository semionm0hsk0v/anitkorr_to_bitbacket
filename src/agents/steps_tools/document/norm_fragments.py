from __future__ import annotations

import re

from src.agents.schemas.parse.structure import DocumentStructureNode
from src.agents.steps_tools.document.morphology import normalized_text


def iter_leaves_with_ancestors(
    node: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...] = (),
):
    """Листья вместе с полным путём предков от root до непосредственного parent."""
    if not node.children:
        if (node.text or "").strip():
            yield node, ancestors
        return
    lineage = (*ancestors, node)
    for child in node.children:
        yield from iter_leaves_with_ancestors(child, lineage)


_NUMBERING_LABEL = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[^\W\d_]\)|\d+\))\s*", re.IGNORECASE)


_SENTENCE_ABBREVIATIONS = frozenset(
    {
        "п",
        "пп",
        "подп",
        "ст",
        "стст",
        "ч",
        "абз",
        "гл",
        "разд",
        "прил",
        "прим",
        "г",
        "гг",
        "им",
        "др",
        "т",
        "тт",
        "руб",
        "коп",
        "тыс",
        "млн",
        "млрд",
        "проц",
        "эт",
        "корп",
        "стр",
        "д",
        "дн",
    }
)
#: Позиция ПОСЛЕ терминального знака: граница-КАНДИДАТ предложения.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?:])(?=\s)")
#: Последнее слово перед точкой-кандидатом.
_TRAILING_WORD = re.compile(r"([^\W\d_]+)\.$", re.IGNORECASE)


_CAPTION_KINDS = frozenset({"document", "appendix", "chapter", "section"})


def _preceded_by_a_number(stripped: str, word_start: int) -> bool:
    "Стоит ли цифра непосредственно перед сокращением (не считая пробела)."
    prefix = stripped[:word_start].rstrip()
    return bool(prefix) and prefix[-1].isdigit()


def _closes_sentence(buffer: str, following: str) -> bool:
    "Закрывает ли накопленный фрагмент предложение перед текстом ``following``."
    stripped = buffer.strip()
    if not stripped:
        return False

    if _NUMBERING_LABEL.fullmatch(stripped):
        return False
    if not stripped.endswith("."):
        return True
    word = _TRAILING_WORD.search(stripped)
    if not (word and word.group(1).casefold() in _SENTENCE_ABBREVIATIONS):
        return True
    if not _preceded_by_a_number(stripped, word.start(1)):
        return False
    head = following.lstrip()[:1]
    return bool(head) and head.isupper()


def _sentences(text: str) -> list[str]:
    "Предложения фрагмента целиком, без разрыва ссылок и нумерации."
    chunks = _SENTENCE_BOUNDARY.split(text)
    parts: list[str] = []
    buffer = ""
    for index, chunk in enumerate(chunks):
        buffer += chunk
        following = chunks[index + 1] if index + 1 < len(chunks) else ""
        if following and _closes_sentence(buffer, following):
            parts.append(buffer)
            buffer = ""
    if buffer.strip():
        parts.append(buffer)
    return parts


def sentence_spans(text: str) -> list[tuple[int, int]]:
    "Границы предложений ``text`` как пары ``(начало, конец)``; вместе они покрывают текст без пропусков."
    spans: list[tuple[int, int]] = []
    position = 0
    for sentence in _sentences(text):
        spans.append((position, position + len(sentence)))
        position += len(sentence)
    return spans


def _delivered_intro(node: DocumentStructureNode) -> str:
    "Вводная, которую сегментатор уже отдал ДЕТЯМ узла как ``parent_intro``."
    intros = {child.parent_intro or "" for child in node.children if (child.parent_intro or "").strip()}
    return next(iter(intros)) if len(intros) == 1 else ""


def own_normative_text(node: DocumentStructureNode) -> str:
    "Собственный нормативный фрагмент контейнера либо пустая строка."
    if node.kind.casefold() in _CAPTION_KINDS or not node.children:
        return ""
    own = node.text or ""
    if not own.strip():
        return ""
    intro = _delivered_intro(node)
    head = own
    if intro and normalized_text(own).endswith(normalized_text(intro)):
        cut = own.rstrip().rfind(intro.strip()) if normalized_text(own) != normalized_text(intro) else -1
        if cut > 0:
            head = own[:cut]
        else:
            sentences = _sentences(own)
            head = "".join(sentences[:-1]) if len(sentences) > 1 and sentences[-1].rstrip().endswith(":") else ""
    head = head.rstrip()
    return head if _NUMBERING_LABEL.sub("", head).strip() else ""


def _own_fragment_node(node: DocumentStructureNode, text: str) -> DocumentStructureNode:
    "Представление собственного фрагмента контейнера как адресуемого узла."
    return node.model_copy(
        update={
            "text": text,
            "char_end": node.char_start + len(text),
            "children": [],
        }
    )


def governing_intro(node: DocumentStructureNode) -> str:
    "Часть собственного текста узла, которая ВВОДИТ его детей."
    intro = _delivered_intro(node)
    if not intro:
        return ""
    own = own_normative_text(node)
    if not own:
        return intro
    remainder = (node.text or "")[len(own) :].strip()

    return remainder if remainder and normalized_text(intro).endswith(normalized_text(remainder)) else intro


def _child_view(child: DocumentStructureNode, intro: str) -> DocumentStructureNode:
    """Ребёнок, чья вводная сужена до управляющей конструкции родителя."""
    if not intro or normalized_text(child.parent_intro) == normalized_text(intro):
        return child
    return child.model_copy(update={"parent_intro": intro})


def iter_normative_fragments(
    node: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...] = (),
):
    "Нормативные фрагменты документа в порядке текста: листья и собственные тексты контейнеров."
    if not node.children:
        if (node.text or "").strip():
            yield node, ancestors
        return
    own = own_normative_text(node)
    if own:
        yield _own_fragment_node(node, own), ancestors
    intro = governing_intro(node)
    lineage = (*ancestors, node)
    for child in node.children:
        yield from iter_normative_fragments(_child_view(child, intro), lineage)
