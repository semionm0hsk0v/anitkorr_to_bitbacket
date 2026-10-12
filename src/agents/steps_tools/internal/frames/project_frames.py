"""Сборка рамок норм из дерева, точных спанов, предметных значений и принятого контекста."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from src.agents.schemas.internal.frames.norm_frame import (
    UNPROVEN_ROLE_CONFIDENCE,
    ContextBlock,
    EvidenceSpan,
    FrameEvidence,
    FrameProvenance,
    FrameSlot,
    GroundingStatus,
    NormFrame,
    SourceRole,
    TreePathElement,
    content_fingerprint,
)
from src.agents.schemas.internal.frames.norm_params import Parameter
from src.agents.schemas.internal.frames.project_frames import BuiltProjectFrame, DocumentFrameContext
from src.agents.schemas.parse.structure import DocumentStructureNode
from src.agents.steps_tools.document.morphology import (
    normalized_text,
)
from src.agents.steps_tools.internal.addressing.address_space import build_address_book
from src.agents.steps_tools.internal.frames.context_policy import accepted_context
from src.agents.steps_tools.internal.frames.norm_params import (
    detect_modality,
    extract_parameters,
    inherited_qualifier_context,
)
from src.agents.steps_tools.internal.frames.operation_names import (
    resolve_subservice,
)
from src.agents.steps_tools.internal.frames.project_context import (
    designation_texts,
    governing_grounds_list_span_ids,
    ground_project_text,
    is_enumerated_leaf,
    project_boundary,
    project_context_candidates,
    project_display_quote,
    resolved_intros,
    unaddressed_primary_span,
)
from src.agents.steps_tools.internal.frames.service_stages import (
    STAGE_SEARCH,
    deadline_scope,
    resolved_stage,
    stage_role_is_proven,
)
from src.agents.steps_tools.internal.frames.subjects import (
    designation_value,
    document_subjects,
)
from src.agents.steps_tools.internal.frames.variables import (
    first_captured_value,
    frame_slot,
    identify_action_object,
    inherit_duration_start_event,
    regulated_variable,
)

_CHANNELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("электронно", re.compile(r"электронн\w*\s+(?:форм|вид)|единый портал|госуслуг", re.IGNORECASE)),
    ("лично", re.compile(r"при личн\w* обращени|лично заявител", re.IGNORECASE)),
    ("почтой", re.compile(r"почтов\w* отправлен|посредством почтов", re.IGNORECASE)),
    ("МФЦ", re.compile(r"\bмфц\b|многофункциональн\w* центр", re.IGNORECASE)),
)


_SUBSERVICE_HEADING = re.compile(
    r"\b((?:проведени|выдач|получени|исправлени|регистраци|переоформлени|"
    r"предоставлени|назначени)\w*(?:\s+[а-яё-]+){0,6})",
    re.IGNORECASE,
)


_DEFINITION = re.compile(
    r"(?:для\s+цел(?:ей|ей\s+настоящего).{0,160}?(?:понимается|означает)|"
    r"\b(?:термин|понятие)\s+[«\"]?.+?[»\"]?\s+(?:означает|определяется)|"
    r"\bопределяется\s+как)",
    re.IGNORECASE | re.DOTALL,
)


_FORM = re.compile(
    r"(?:^|\n)\s*(?:приложени[ея]\s*\d*\s*)?(?:форма|образец)\s+"
    r"(?:заявлен|уведомлен|решен|справк)|\bполе\s+форм[ыа]\s+(?:заявлен|уведомлен|решен|справк)|"
    r"\bобразец\s+заполнен",
    re.IGNORECASE,
)


_UNPROVEN_ROLE_CONFIDENCE = UNPROVEN_ROLE_CONFIDENCE


def _deadline_scope_slot(
    leaf: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...],
    supporting_ids: Callable[[str | None], list[str]],
    *,
    resolved_intro: dict[str, str],
) -> FrameSlot | None:
    "Слот области срока: конструкция, ВВОДЯЩАЯ норму, плюс подтверждающий её спан."
    deadline = deadline_scope(
        [
            (leaf.text or "").split("\n", 1)[0],
            resolved_intro[leaf.element_id],
            *(resolved_intro[node.element_id].split("\n", 1)[0] for node in reversed(ancestors)),
            *((node.text or "").split("\n", 1)[0] for node in reversed(ancestors) if node.kind != "document"),
        ]
    )
    if deadline is None:
        return None
    evidence_ids = supporting_ids(deadline[1].strip())
    if not evidence_ids:
        return None
    return frame_slot(deadline[0], provenance=FrameProvenance.TREE, evidence_ids=evidence_ids, confidence=0.9)


def _source_role(leaf: DocumentStructureNode, ancestors: tuple[DocumentStructureNode, ...]) -> SourceRole:
    path = (*ancestors, leaf)
    kinds = {node.kind.casefold() for node in path}
    labels = "\n".join(node.label for node in path if node.label)
    text = leaf.text or ""
    if "preamble" in kinds:
        return SourceRole.PREAMBLE
    if _FORM.search(f"{labels}\n{text}"):
        return SourceRole.FORM
    if _DEFINITION.search(text):
        return SourceRole.DEFINITION
    if leaf.kind.casefold() in {"clause", "subclause", "article", "part", "paragraph"}:
        return SourceRole.OPERATIVE_NORM

    if leaf.kind.casefold() == "appendix":
        return SourceRole.APPENDIX
    return SourceRole.UNKNOWN


def prepare_project_frames(
    leaf_paths: Iterable[tuple[DocumentStructureNode, tuple[DocumentStructureNode, ...]]],
    *,
    document_text: str,
    doc_title: str = "",
    reanchor_window: int = 500,
) -> list[BuiltProjectFrame]:
    "Фреймы ВСЕХ нормативных фрагментов документа: общие данные считаются один раз."
    document = build_document_frame_context(document_text, doc_title)
    return [
        _build_frame(
            leaf,
            ancestors,
            document=document,
            parameters=extract_parameters(leaf.text or "", inherited_context=inherited_qualifier_context(leaf, ancestors)),
            reanchor_window=reanchor_window,
        )
        for leaf, ancestors in leaf_paths
    ]


def _build_frame(
    leaf: DocumentStructureNode,
    ancestors: tuple[DocumentStructureNode, ...],
    *,
    document: DocumentFrameContext,
    parameters: list[Parameter],
    reanchor_window: int,
) -> BuiltProjectFrame:
    """Строит production frame из дерева, canonical text и явной лексики."""
    document_text = document.document_text
    if leaf.address_resolved:
        primary = ground_project_text(
            leaf.text or "",
            document_text=document_text,
            source_id=leaf.element_id,
            expected_start=leaf.char_start,
            expected_end=leaf.char_end,
            reanchor_window=reanchor_window,
            fingerprint=document.fingerprint,
        )
    else:
        primary = unaddressed_primary_span(leaf, document=document)
    measure, service = document.measure, document.service
    primary_position = primary.char_start if primary.char_start is not None else leaf.char_start
    boundary = project_boundary(document.address_book, primary_position, ancestors)

    resolved_intro, intro_owner = resolved_intros((*ancestors, leaf), document=document, boundary_id=boundary.boundary_id)
    context_candidates = project_context_candidates(
        leaf,
        ancestors,
        document=document,
        primary=primary,
        boundary=boundary,
        intro_owner=intro_owner,
    )
    context_spans: list[EvidenceSpan] = []
    seen_context_spans: set[str] = set()
    for candidate in context_candidates:
        if candidate.span.span_id not in seen_context_spans:
            context_spans.append(candidate.span)
            seen_context_spans.add(candidate.span.span_id)

    primary_ids = [primary.span_id] if primary.grounding_status != GroundingStatus.UNRESOLVED else []

    def supporting_ids(value: str | None) -> list[str]:
        needle = normalized_text(value)
        if not needle:
            return []
        spans = [primary, *context_spans]
        return [
            span.span_id
            for span in spans
            if span.grounding_status != GroundingStatus.UNRESOLVED and needle in normalized_text(span.text)
        ]

    def cue_span_ids(pattern: re.Pattern[str]) -> list[str]:
        """Спаны, в которых ДЕЙСТВИТЕЛЬНО стоит породившая значение подсказка."""
        return [
            span.span_id
            for span in (primary, *context_spans)
            if span.grounding_status != GroundingStatus.UNRESOLVED and pattern.search(span.text)
        ]

    leaf_intro = resolved_intro[leaf.element_id]
    own_texts, heading_texts, structural_texts = designation_texts(leaf, ancestors, resolved_intro)

    own_text = leaf.text or ""
    own_intro_length = len(own_text.split("\n", 1)[0])
    part_operation = resolve_subservice(
        (
            ((own_text, own_intro_length),),
            ((leaf_intro, None),),
            [
                *((resolved_intro[node.element_id], None) for node in reversed(ancestors)),
                *((node.label or "", None) for node in reversed(ancestors)),
            ],
        )
    )
    typed_subservice = part_operation.value if part_operation is not None and part_operation.proven else None
    subservice_hint: str | None = None
    if not typed_subservice:
        subservice_hint = part_operation.value if part_operation is not None else None
        if not subservice_hint:
            subservice_hint = first_captured_value(_SUBSERVICE_HEADING, reversed(structural_texts))
        if not subservice_hint and service is not None:
            subservice_hint = first_captured_value(_SUBSERVICE_HEADING, (service.value,))
    stage = resolved_stage(own_texts, heading_texts)
    stage_is_proven = stage_role_is_proven(stage, own_text)
    stage_pattern = next((pattern for name, pattern in STAGE_SEARCH if name == stage), None)

    full = "\n".join(item for item in [leaf.parent_intro or "", leaf.text or ""] if item)
    channel = next((name for name, pattern in _CHANNELS if pattern.search(full)), None)
    explicit_modality = detect_modality(full)
    variable = regulated_variable(
        full,
        parameters,
        primary_ids,
        parameter_text=leaf.text or "",
        governing_grounds_span_ids=(governing_grounds_list_span_ids(context_candidates) if is_enumerated_leaf(leaf) else []),
    )
    action_object = identify_action_object(full, variable)
    accepted = accepted_context(context_candidates, primary=primary, boundary_id=boundary.boundary_id)
    modality = explicit_modality[0].value if explicit_modality else None
    polarity = explicit_modality[1].value if explicit_modality else None

    display_quote = project_display_quote(leaf, ancestors, grounding_status=primary.grounding_status)

    frame = NormFrame(
        evidence=FrameEvidence(
            display_quote=display_quote,
            primary_span=primary,
            context_spans=context_spans,
            context_blocks=[ContextBlock(span_id=item.span.span_id, role=item.role) for item in accepted],
            grounding_status=primary.grounding_status,
        ),
        tree_path=[TreePathElement(element_id=node.element_id, kind=node.kind, label=node.label) for node in (*ancestors, leaf)],
        address_space=boundary.address_space,
        source_role=_source_role(leaf, ancestors),
        measure=frame_slot(
            designation_value(measure),
            provenance=FrameProvenance.INHERITED,
            evidence_ids=supporting_ids(designation_value(measure)),
            confidence=0.8,
        ),
        service=frame_slot(
            designation_value(service),
            provenance=FrameProvenance.INHERITED,
            evidence_ids=supporting_ids(designation_value(service)),
            confidence=0.8,
        ),
        subservice=(
            frame_slot(
                typed_subservice,
                provenance=FrameProvenance.TREE,
                evidence_ids=supporting_ids(typed_subservice),
                confidence=0.9,
            )
            if typed_subservice
            else frame_slot(
                subservice_hint,
                provenance=FrameProvenance.LEXICAL,
                evidence_ids=supporting_ids(subservice_hint),
                confidence=_UNPROVEN_ROLE_CONFIDENCE,
            )
        ),
        action=frame_slot(
            action_object.action,
            provenance=FrameProvenance.LEXICAL,
            evidence_ids=primary_ids,
            confidence=0.9 if action_object.proven else _UNPROVEN_ROLE_CONFIDENCE,
        ),
        object=frame_slot(
            action_object.object,
            provenance=FrameProvenance.LEXICAL,
            evidence_ids=primary_ids,
            confidence=0.9 if action_object.proven else _UNPROVEN_ROLE_CONFIDENCE,
        ),
        procedure_stage=frame_slot(
            stage,
            provenance=FrameProvenance.TREE if stage_is_proven else FrameProvenance.LEXICAL,
            evidence_ids=primary_ids if stage_is_proven else (cue_span_ids(stage_pattern) if stage_pattern is not None else []),
            confidence=0.9 if stage_is_proven else _UNPROVEN_ROLE_CONFIDENCE,
        ),
        deadline_scope=_deadline_scope_slot(leaf, ancestors, supporting_ids, resolved_intro=resolved_intro),
        channel=frame_slot(channel, provenance=FrameProvenance.LEXICAL, evidence_ids=primary_ids, confidence=0.9),
        regulated_variable=inherit_duration_start_event(variable, accepted),
        modality=frame_slot(modality, provenance=FrameProvenance.LEXICAL, evidence_ids=primary_ids, confidence=0.95),
        polarity=frame_slot(polarity, provenance=FrameProvenance.LEXICAL, evidence_ids=primary_ids, confidence=0.95),
    )
    return BuiltProjectFrame(
        leaf=leaf,
        ancestors=ancestors,
        frame=frame,
        parameters=parameters,
    )


def build_document_frame_context(document_text: str, doc_title: str = "") -> DocumentFrameContext:
    measure, service = document_subjects(document_text, doc_title)
    return DocumentFrameContext(
        document_text=document_text,
        doc_title=doc_title,
        fingerprint=content_fingerprint(document_text),
        address_book=build_address_book(document_text),
        measure=measure,
        service=service,
    )
