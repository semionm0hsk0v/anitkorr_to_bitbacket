from __future__ import annotations

from src.agents.schemas.internal.frames.context_policy import ContextCandidate
from src.agents.schemas.internal.frames.norm_frame import ContextBlockRole, EvidenceSpan, GroundingStatus, NormFrame

GOVERNING_CONTEXT_ROLES = frozenset(
    {
        ContextBlockRole.PARENT_INTRO,
        ContextBlockRole.LIST_INTRO,
        ContextBlockRole.ANCESTOR_HEADING,
    }
)


_ROLE_PRIORITY = {
    ContextBlockRole.PARENT_INTRO: 0,
    ContextBlockRole.LIST_INTRO: 1,
    ContextBlockRole.ANCESTOR_HEADING: 2,
    ContextBlockRole.LIST_SIBLING: 3,
    ContextBlockRole.DOCUMENT_SUBJECT: 4,
}


def accepted_context(candidates: list[ContextCandidate], *, primary: EvidenceSpan, boundary_id: str) -> list[ContextCandidate]:
    "Кандидаты, относящиеся к норме доказанно, в порядке близости роли; без лимитов и усечения."
    if primary.grounding_status is not GroundingStatus.EXACT:
        return []
    accepted: list[ContextCandidate] = []
    seen: set[tuple[int | None, int | None]] = set()
    ordered = sorted(
        candidates,
        key=lambda item: (
            _ROLE_PRIORITY[item.role],
            item.structural_distance,
            item.source_order,
            item.span.char_start if item.span.char_start is not None else 2**63,
            item.candidate_id,
        ),
    )
    for candidate in ordered:
        span = candidate.span
        coordinates = (span.char_start, span.char_end)
        if (
            span.grounding_status is not GroundingStatus.EXACT
            or not candidate.relation_proven
            or candidate.source_boundary_id != boundary_id
            or not span.text
            or coordinates in seen
        ):
            continue
        seen.add(coordinates)
        accepted.append(candidate)
    return accepted


def _contains(container: EvidenceSpan, span: EvidenceSpan) -> bool:
    """Спан того же источника целиком внутри контейнера, и текст по его координатам равен тексту контейнера."""
    return (
        span.grounding_status is GroundingStatus.EXACT
        and span.source_kind is container.source_kind
        and span.source_id == container.source_id
        and span.content_fingerprint == container.content_fingerprint
        and span.char_start is not None
        and span.char_end is not None
        and container.char_start is not None
        and container.char_end is not None
        and container.char_start <= span.char_start <= span.char_end <= container.char_end
        and container.text[span.char_start - container.char_start : span.char_end - container.char_start] == span.text
    )


def eligible_evidence_spans(frame: NormFrame) -> dict[str, EvidenceSpan]:
    "Доказательства для жёстких решений: первичный спан, принятые блоки контекста и точные фрагменты того и другого."
    primary = frame.evidence.primary_span
    if primary is None or primary.grounding_status is not GroundingStatus.EXACT:
        return {}
    pool = {span.span_id: span for span in frame.evidence.context_spans}
    blocks = [pool[block.span_id] for block in frame.evidence.context_blocks if block.span_id in pool]
    result: dict[str, EvidenceSpan] = {primary.span_id: primary}
    containers = [primary, *blocks]
    for span in frame.evidence.context_spans:
        if any(_contains(container, span) for container in containers):
            result[span.span_id] = span
    for span in blocks:
        result[span.span_id] = span
    return result
