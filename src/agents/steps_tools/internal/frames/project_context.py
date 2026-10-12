"""Точные спаны проекта и доказанный контекст внутри одного адресного пространства."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from src.agents.schemas.internal.addressing.address_space import AddressBook
from src.agents.schemas.internal.frames.context_policy import ContextCandidate
from src.agents.schemas.internal.frames.norm_frame import (
    AddressSpace,
    ContextBlockRole,
    EvidenceSourceKind,
    EvidenceSpan,
    GroundingStatus,
    content_fingerprint,
)
from src.agents.schemas.internal.frames.project_frames import DocumentFrameContext
from src.agents.schemas.parse.structure import DocumentStructureNode
from src.agents.steps_tools.document.morphology import (
    normalized_text,
)
from src.agents.steps_tools.document.norm_fragments import governing_intro, own_normative_text
from src.agents.steps_tools.internal.frames.context_policy import GOVERNING_CONTEXT_ROLES
from src.agents.steps_tools.internal.frames.norm_params import (
    closure_is_asserted,
)
from src.agents.steps_tools.internal.frames.subjects import (
    DOCUMENT_SUBJECT_HEAD_CHARS,
)

_NUMBERED_LABEL = re.compile(r"\bп\.\s*(\d+(?:\.\d+)*)", re.IGNORECASE)


_HOMOGENEOUS_LIST_INTRO = re.compile(
    r"\b(?:документ|основан|услов|сведен|требован|категор|случа|способ|канал|результат|действи)",
    re.IGNORECASE,
)


_SERVICE_BRANCH_ITEM = re.compile(
    r"\b(?:в\s+части|государственн\w*\s+услуг|муниципальн\w*\s+услуг|мер[аы]\s+поддержк)",
    re.IGNORECASE,
)


def unaddressed_primary_span(
    leaf: DocumentStructureNode,
    *,
    document: DocumentFrameContext,
) -> EvidenceSpan:
    "Первичный спан нормы с недоказанным адресом: текст есть, координат нет."
    return EvidenceSpan(
        source_kind=EvidenceSourceKind.PROJECT,
        source_id=leaf.element_id,
        content_fingerprint=document.fingerprint,
        text=leaf.text or "",
        grounding_status=GroundingStatus.UNRESOLVED,
    )


def ground_project_text(
    text: str,
    *,
    document_text: str,
    source_id: str,
    expected_start: int,
    expected_end: int,
    reanchor_window: int,
    fingerprint: str | None = None,
) -> EvidenceSpan:
    "Проверяет parser span, затем exact re-anchor в ограниченном окне."
    fingerprint = fingerprint if fingerprint is not None else content_fingerprint(document_text)
    if text and document_text[expected_start:expected_end] == text:
        span = EvidenceSpan(
            source_kind=EvidenceSourceKind.PROJECT,
            source_id=source_id,
            content_fingerprint=fingerprint,
            char_start=expected_start,
            char_end=expected_end,
            text=text,
            grounding_status=GroundingStatus.EXACT,
        )
        return span

    lo = max(0, expected_start - reanchor_window)
    hi = min(len(document_text), max(expected_end, expected_start) + reanchor_window)
    positions: list[int] = []
    cursor = lo
    while text:
        found = document_text.find(text, cursor, hi)
        if found < 0:
            break
        positions.append(found)
        cursor = found + 1
    if positions:
        selected = min(positions, key=lambda pos: (abs(pos - expected_start), pos))
        status = GroundingStatus.EXACT if len(positions) == 1 else GroundingStatus.AMBIGUOUS
        return EvidenceSpan(
            source_kind=EvidenceSourceKind.PROJECT,
            source_id=source_id,
            content_fingerprint=fingerprint,
            char_start=selected,
            char_end=selected + len(text),
            text=text,
            grounding_status=status,
        )
    return EvidenceSpan(
        source_kind=EvidenceSourceKind.PROJECT,
        source_id=source_id,
        content_fingerprint=fingerprint,
        text=text,
        grounding_status=GroundingStatus.UNRESOLVED,
    )


def governing_grounds_list_span_ids(candidates: list[ContextCandidate]) -> list[str]:
    "Спаны ДОКАЗАННОГО управляющего контекста, объявляющие закрытый перечень ОСНОВАНИЙ."
    span_ids: list[str] = []
    for candidate in candidates:
        if candidate.role not in GOVERNING_CONTEXT_ROLES or not candidate.relation_proven:
            continue
        span = candidate.span
        if span.grounding_status is not GroundingStatus.EXACT:
            continue
        match = _HOMOGENEOUS_LIST_INTRO.search(span.text)
        if match is None or not match.group(0).casefold().startswith("основан"):
            continue
        if not closure_is_asserted(span.text):
            continue
        span_ids.append(span.span_id)
    return span_ids


@dataclass(frozen=True)
class _ProjectBoundary:
    address_space: AddressSpace
    boundary_id: str


def project_boundary(
    book: AddressBook,
    position: int,
    ancestors: tuple[DocumentStructureNode, ...] = (),
) -> _ProjectBoundary:
    "Return a region identity strong enough to keep context inside one address space."
    for index, (start, end) in enumerate(book.amendment_spans):
        if start <= position < end:
            return _ProjectBoundary(AddressSpace.AMENDED_ACT, f"project:amended:{index}:{start}:{end}")

    containing = [region for region in book.regions if region.char_start <= position < region.char_end]
    if containing:
        region = min(containing, key=lambda item: item.char_end - item.char_start)
        try:
            address_space = AddressSpace(region.space.value)
        except ValueError:
            address_space = AddressSpace.UNKNOWN
        if address_space is not AddressSpace.CORE:
            return _ProjectBoundary(
                address_space,
                ":".join(
                    (
                        "project",
                        address_space.value,
                        region.appendix or "-",
                        region.table or "-",
                        str(region.char_start),
                        str(region.char_end),
                    )
                ),
            )

    appendix = next((node for node in reversed(ancestors) if node.kind.casefold() == "appendix"), None)
    if appendix is not None:
        return _ProjectBoundary(AddressSpace.APPENDIX, f"project:appendix-node:{appendix.element_id}")
    return _ProjectBoundary(AddressSpace.CORE, "project:core")


def _producer_context_span(
    value: str,
    *,
    document_text: str,
    primary_source_id: str,
    content_hash: str,
    char_start: int,
    char_end: int,
) -> EvidenceSpan | None:
    "Ground a producer-known block at the coordinates the producer itself matched."
    if not (0 <= char_start < char_end <= len(document_text)):
        return None
    fragment = document_text[char_start:char_end]
    if normalized_text(fragment) != normalized_text(value):
        return None
    return EvidenceSpan(
        source_kind=EvidenceSourceKind.PROJECT,
        source_id=primary_source_id,
        content_fingerprint=content_hash,
        char_start=char_start,
        char_end=char_end,
        text=fragment,
        grounding_status=GroundingStatus.EXACT,
    )


def _exact_context_span(
    value: str,
    *,
    document_text: str,
    primary_source_id: str,
    content_hash: str,
    search_start: int,
    search_end: int,
) -> EvidenceSpan:
    """Ground a producer-known block only inside its structural source coordinates."""
    clean = value.strip()
    lo = max(0, min(search_start, len(document_text)))
    hi = max(lo, min(search_end, len(document_text)))
    positions: list[int] = []
    cursor = lo
    while clean:
        found = document_text.find(clean, cursor, hi)
        if found < 0:
            break
        positions.append(found)
        cursor = found + 1
    if len(positions) == 1:
        start = positions[0]
        return EvidenceSpan(
            source_kind=EvidenceSourceKind.PROJECT,
            source_id=primary_source_id,
            content_fingerprint=content_hash,
            char_start=start,
            char_end=start + len(clean),
            text=clean,
            grounding_status=GroundingStatus.EXACT,
        )
    return EvidenceSpan(
        source_kind=EvidenceSourceKind.PROJECT,
        source_id=primary_source_id,
        content_fingerprint=content_hash,
        text=clean,
        grounding_status=GroundingStatus.AMBIGUOUS if positions else GroundingStatus.UNRESOLVED,
    )


def _numbered_path(label: str) -> tuple[int, ...] | None:
    match = _NUMBERED_LABEL.search(label or "")
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _same_numbered_branch(ancestor: DocumentStructureNode, leaf: DocumentStructureNode) -> bool:
    ancestor_path = _numbered_path(ancestor.label)
    leaf_path = _numbered_path(leaf.label)
    if ancestor_path is None or leaf_path is None:
        return True
    return leaf_path[: len(ancestor_path)] == ancestor_path


def is_enumerated_leaf(node: DocumentStructureNode) -> bool:
    return not node.children and node.kind.casefold() == "subclause" and not (node.text or "").rstrip().endswith(":")


def _swallowed_parent_intro(ancestors: tuple[DocumentStructureNode, ...]) -> str:
    "Вводная непосредственного родителя, если она — ЕГО ЖЕ собственный текст целиком."
    if not ancestors:
        return ""
    parent = ancestors[-1]
    if len(parent.children) != 1:
        return ""
    if own_normative_text(parent):
        return ""
    return governing_intro(parent)


def project_display_quote(
    leaf: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...],
    *,
    grounding_status: GroundingStatus,
) -> str:
    "Каноническая цитата атома: ``leaf.text``, плюс вводная родителя, если она"
    if grounding_status == GroundingStatus.UNRESOLVED:
        return ""
    text = leaf.text or ""
    intro = _swallowed_parent_intro(ancestors)
    if intro:
        return f"{intro}\n{text}"
    return text


def _ordered_common_list_siblings(
    leaf: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...],
) -> list[DocumentStructureNode]:
    if (
        not ancestors
        or not leaf.parent_intro
        or not _HOMOGENEOUS_LIST_INTRO.search(leaf.parent_intro)
        or _SERVICE_BRANCH_ITEM.search(leaf.parent_intro)
    ):
        return []
    parent = ancestors[-1]
    children = list(parent.children)
    if leaf.element_id not in {item.element_id for item in children} or not is_enumerated_leaf(leaf):
        return []
    ordered = sorted(children, key=lambda item: (item.char_start, item.char_end, item.element_id))
    if ordered != children or any(left.char_end > right.char_start for left, right in pairwise(ordered)):
        return []
    intro = normalized_text(leaf.parent_intro)
    related = [item for item in ordered if is_enumerated_leaf(item) and normalized_text(item.parent_intro) == intro]
    if any(_SERVICE_BRANCH_ITEM.search(item.text or "") for item in related):
        return []
    return related if len(related) >= 2 else []


def _intro_owner(
    node: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...],
    *,
    document: DocumentFrameContext,
    boundary_id: str,
) -> DocumentStructureNode | None:
    "Адресованный узел, чей СОБСТВЕННЫЙ текст оканчивается вводной конструкцией узла."
    intro = normalized_text(node.parent_intro)
    if not intro:
        return None
    return next(
        (
            item
            for item in _relation_nodes(node, ancestors)
            if item.kind.casefold() != "document"
            and normalized_text(item.text).endswith(intro)
            and project_boundary(document.address_book, item.char_start, ancestors).boundary_id == boundary_id
        ),
        None,
    )


_INTRO_LINE_TERMINATORS = (".", ":", ";", "!", "?")


def _wrapped_intro(owner_text: str, intro: str) -> str:
    "Вводная конструкция владельца целиком: только перенос строки, не соседние нормы."
    lines = owner_text.split("\n")
    index = len(lines) - 1
    while index > 0 and not lines[index - 1].rstrip().endswith(_INTRO_LINE_TERMINATORS):
        index -= 1
    wrapped = "\n".join(lines[index:]).strip()
    return wrapped if normalized_text(wrapped).endswith(normalized_text(intro)) else intro


def designation_texts(
    leaf: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...],
    resolved_intro: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    "Три области, в которых ищутся структурные обозначения нормы."
    leaf_intro = resolved_intro[leaf.element_id]
    heading_texts = [
        *(resolved_intro[node.element_id] for node in ancestors),
        *(node.label or "" for node in ancestors),
    ]
    structural_texts = [
        *heading_texts,
        *(node.text or "" for node in ancestors if node.kind != "document"),
        leaf_intro,
    ]
    return [leaf.text or "", leaf_intro], heading_texts, structural_texts


def resolved_intros(
    chain: tuple[DocumentStructureNode, ...],
    *,
    document: DocumentFrameContext,
    boundary_id: str,
) -> tuple[dict[str, str], DocumentStructureNode | None]:
    "Вводные конструкции цепочки и адресованный владелец вводной ПОСЛЕДНЕГО узла."
    intros: dict[str, str] = {}
    owner: DocumentStructureNode | None = None
    for index, node in enumerate(chain):
        intro = node.parent_intro or ""
        owner = _intro_owner(node, chain[:index], document=document, boundary_id=boundary_id)
        intros[node.element_id] = _wrapped_intro(owner.text, intro) if owner is not None else intro
    return intros, owner


def _relation_nodes(
    leaf: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...],
) -> list[DocumentStructureNode]:
    nodes: list[DocumentStructureNode] = []
    if ancestors:
        nodes.extend(
            sorted(
                (item for item in ancestors[-1].children if item.char_end <= leaf.char_start),
                key=lambda item: (item.char_end, item.char_start),
                reverse=True,
            )
        )
    nodes.extend(reversed(ancestors))
    seen: set[str] = set()
    unique: list[DocumentStructureNode] = []
    for item in nodes:
        if item.element_id in seen:
            continue
        seen.add(item.element_id)
        unique.append(item)
    return unique


def project_context_candidates(
    leaf: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...],
    *,
    document: DocumentFrameContext,
    primary: EvidenceSpan,
    boundary: _ProjectBoundary,
    intro_owner: DocumentStructureNode | None,
) -> list[ContextCandidate]:
    """Build only positive structural context relations; selection/budgeting is pure policy."""
    candidates: list[ContextCandidate] = []
    document_text = document.document_text
    content_hash = primary.content_fingerprint

    def add(
        *,
        candidate_id: str,
        value: str,
        role: ContextBlockRole,
        relation: DocumentStructureNode | None,
        distance: int,
        order: int,
        source_boundary: _ProjectBoundary | None = None,
        relation_proven: bool = True,
        search_start: int | None = None,
        search_end: int | None = None,
        exact_start: int | None = None,
        exact_end: int | None = None,
    ) -> None:
        start = relation.char_start if search_start is None and relation is not None else (search_start or 0)
        end = relation.char_end if search_end is None and relation is not None else (search_end or len(document_text))
        span = (
            _producer_context_span(
                value,
                document_text=document_text,
                primary_source_id=primary.source_id,
                content_hash=content_hash,
                char_start=exact_start,
                char_end=exact_end,
            )
            if exact_start is not None and exact_end is not None
            else None
        ) or _exact_context_span(
            value,
            document_text=document_text,
            primary_source_id=primary.source_id,
            content_hash=content_hash,
            search_start=start,
            search_end=end,
        )
        candidates.append(
            ContextCandidate(
                candidate_id=candidate_id,
                span=span,
                role=role,
                structural_distance=distance,
                source_order=order,
                source_boundary_id=(source_boundary or boundary).boundary_id,
                relation_proven=relation_proven,
            )
        )

    if leaf.parent_intro:
        # Владелец вводной уже найден при восстановлении вводных конструкций цепочки.
        relation = intro_owner
        add(
            candidate_id=f"{leaf.element_id}:parent-intro",
            value=(relation.text if relation is not None else leaf.parent_intro),
            role=ContextBlockRole.PARENT_INTRO,
            relation=relation,
            distance=0,
            order=relation.char_start if relation is not None else leaf.char_start,
            relation_proven=relation is not None,
            search_start=relation.char_start if relation is not None else max(0, leaf.char_start - 500),
            search_end=relation.char_end if relation is not None else leaf.char_start,
        )

    for distance, ancestor in enumerate(reversed(ancestors), start=1):
        if ancestor.kind.casefold() == "document" or not _same_numbered_branch(ancestor, leaf):
            continue
        ancestor_boundary = project_boundary(document.address_book, ancestor.char_start, ancestors)
        text = (ancestor.text or "").strip()
        if not text or len(text) > 1200:
            continue
        shared_intro = normalized_text(text)
        list_children = [
            child
            for child in ancestor.children
            if is_enumerated_leaf(child)
            and normalized_text(child.parent_intro)
            and shared_intro.endswith(normalized_text(child.parent_intro))
        ]
        role = ContextBlockRole.LIST_INTRO if len(list_children) >= 2 else ContextBlockRole.ANCESTOR_HEADING
        add(
            candidate_id=f"{leaf.element_id}:ancestor:{ancestor.element_id}:{role.value}",
            value=text,
            role=role,
            relation=ancestor,
            distance=distance,
            order=ancestor.char_start,
            source_boundary=ancestor_boundary,
        )

    for sibling in _ordered_common_list_siblings(leaf, ancestors):
        if sibling.element_id == leaf.element_id:
            continue
        sibling_boundary = project_boundary(document.address_book, sibling.char_start, ancestors)
        add(
            candidate_id=f"{leaf.element_id}:list-sibling:{sibling.element_id}",
            value=sibling.text,
            role=ContextBlockRole.LIST_SIBLING,
            relation=sibling,
            distance=1,
            order=sibling.char_start,
            source_boundary=sibling_boundary,
        )

    if boundary.address_space is AddressSpace.CORE:
        head_end = min(len(document_text), DOCUMENT_SUBJECT_HEAD_CHARS)
        for name, designation in (("measure", document.measure), ("service", document.service)):
            if designation is not None:
                add(
                    candidate_id=f"{leaf.element_id}:document-subject:{name}",
                    value=designation.value,
                    role=ContextBlockRole.DOCUMENT_SUBJECT,
                    relation=None,
                    distance=len(ancestors) + 1,
                    order=(
                        designation.char_start
                        if designation.char_start is not None
                        else document_text.find(designation.value, 0, head_end)
                    ),
                    search_start=0,
                    search_end=head_end,
                    exact_start=designation.char_start,
                    exact_end=designation.char_end,
                )
    return candidates
