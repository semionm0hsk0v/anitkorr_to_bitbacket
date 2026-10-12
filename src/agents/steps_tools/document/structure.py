from __future__ import annotations

import re

from src.agents.enums.parse.segmentation import ProvisionKind, SegmentAddressStatus
from src.agents.schemas.parse.segmentation import NormSegment
from src.agents.schemas.parse.structure import DocumentStructureNode, DocumentStructureTree
from src.agents.steps_tools.document.segmentation import segment_norms

_KIND_TO_NODE: dict[ProvisionKind, str] = {
    ProvisionKind.PREAMBLE: "preamble",
    ProvisionKind.CHAPTER: "chapter",
    ProvisionKind.SECTION: "section",
    ProvisionKind.APPENDIX: "appendix",
    ProvisionKind.ARTICLE: "article",
    ProvisionKind.PART: "part",
    ProvisionKind.CLAUSE: "clause",
    ProvisionKind.SUBCLAUSE: "subclause",
    ProvisionKind.PARAGRAPH: "paragraph",
    ProvisionKind.SIGNATURE: "signature",
}

# Чем больше depth — тем глубже вложенность (детальнее узел).
_KIND_DEPTH: dict[ProvisionKind, int] = {
    ProvisionKind.PREAMBLE: 10,
    ProvisionKind.CHAPTER: 20,
    ProvisionKind.SECTION: 20,
    ProvisionKind.APPENDIX: 25,
    ProvisionKind.ARTICLE: 30,
    ProvisionKind.PART: 40,
    ProvisionKind.CLAUSE: 50,
    ProvisionKind.SUBCLAUSE: 60,
    ProvisionKind.PARAGRAPH: 55,
    ProvisionKind.SIGNATURE: 15,
}

_CLAUSE_NUM_RE = re.compile(r"п\.\s*([\d.]+)", re.IGNORECASE)


def _segment_depth(segment: NormSegment) -> int:
    base = _KIND_DEPTH.get(segment.kind, 50)
    if segment.kind is ProvisionKind.CLAUSE:
        m = _CLAUSE_NUM_RE.search(segment.source_ref)
        if m:
            parts = [p for p in m.group(1).split(".") if p]
            base += max(0, len(parts) - 1)
    elif segment.kind is ProvisionKind.SUBCLAUSE and segment.is_letter_subclause:
        base += 1
    return base


def _make_element_id(parent_id: str, kind: str, label: str, ordinal: int) -> str:
    if label:
        slug = re.sub(r"[^\wа-яёА-ЯЁ.-]+", "-", label.strip().lower(), flags=re.UNICODE)
        slug = re.sub(r"-{2,}", "-", slug).strip("-") or "x"
    else:
        slug = str(ordinal)
    return f"{parent_id}/{kind}-{slug}"


def count_structure_nodes(node: DocumentStructureNode) -> int:
    return 1 + sum(count_structure_nodes(child) for child in node.children)


def build_document_structure_tree(
    text: str,
    *,
    segments: list[NormSegment] | None = None,
) -> DocumentStructureTree:
    "Собирает дерево структуры документа с уникальными element_id."
    segs = segments if segments is not None else segment_norms(text, normalize=False)
    source = text or ""
    excerpt = source.strip()

    char_start = len(source) - len(source.lstrip()) if excerpt else 0
    char_end = char_start + len(excerpt)

    root = DocumentStructureNode(
        element_id="doc",
        kind="document",
        label="Документ",
        text=excerpt,
        char_start=char_start,
        char_end=char_end,
        children=[],
    )

    stack: list[tuple[int, DocumentStructureNode]] = [(0, root)]
    counters: dict[str, int] = {}

    for seg in segs:
        depth = _segment_depth(seg)
        while len(stack) > 1 and stack[-1][0] >= depth:
            stack.pop()

        parent = stack[-1][1]
        node_kind = _KIND_TO_NODE.get(seg.kind, "paragraph")
        counter_key = f"{parent.element_id}:{node_kind}"
        counters[counter_key] = counters.get(counter_key, 0) + 1
        label = seg.source_ref.strip() or node_kind
        element_id = _make_element_id(parent.element_id, node_kind, label, counters[counter_key])

        resolved = seg.address_status is SegmentAddressStatus.RESOLVED
        node = DocumentStructureNode(
            element_id=element_id,
            kind=node_kind,
            label=label,
            text=seg.text,
            char_start=seg.char_start,
            char_end=seg.char_end,
            parent_intro=seg.parent_intro,
            address_resolved=resolved,
            address_reason=None if resolved else str(seg.address_reason or ""),
            children=[],
        )
        parent.children.append(node)

        if seg.kind not in {ProvisionKind.PARAGRAPH, ProvisionKind.SIGNATURE, ProvisionKind.PREAMBLE}:
            stack.append((depth, node))

    return DocumentStructureTree(root=root)
