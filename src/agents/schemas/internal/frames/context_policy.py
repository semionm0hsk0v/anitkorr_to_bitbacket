from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.schemas.internal.frames.norm_frame import ContextBlockRole, EvidenceSpan


class ContextCandidate(BaseModel):
    """Одно структурное отношение, найденное в дереве; текст — только в спане."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    span: EvidenceSpan
    role: ContextBlockRole
    structural_distance: int
    source_order: int
    source_boundary_id: str
    relation_proven: bool = True
