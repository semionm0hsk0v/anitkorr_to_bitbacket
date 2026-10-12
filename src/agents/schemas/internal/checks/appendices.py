from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.agents.enums.internal.checks.appendices import (
    AppendixArtifactKind,
    AppendixIssueKind,
    AppendixOwnerKind,
    AppendixSpanRole,
)


class AppendixSpan(BaseModel):
    """Точный адрес операнда проверки: роль, координаты в тексте, узел дерева, владелец и номера приложений."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AppendixSpanRole
    char_start: int
    char_end: int
    tree_path_element_ids: tuple[str, ...]
    owner_kind: AppendixOwnerKind
    owner_id: str
    appendix_numbers: tuple[int, ...] = ()
    artifact_kind: AppendixArtifactKind = AppendixArtifactKind.UNKNOWN


class AppendixDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_kind: AppendixOwnerKind
    owner_id: str
    numbers: tuple[int, ...]
    declaration_span: AppendixSpan
    owner_span: AppendixSpan | None

    declared_genus: frozenset[str] = frozenset()


class AppendixArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_kind: AppendixOwnerKind
    owner_id: str
    number: int
    classification: AppendixArtifactKind
    header_span: AppendixSpan
    classification_span: AppendixSpan | None

    form_genus: str | None = None


class AppendixIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    issue_kind: AppendixIssueKind
    affected_numbers: tuple[int, ...]
    declaration_spans: list[AppendixSpan]
    owner_spans: list[AppendixSpan]
    material_spans: list[AppendixSpan]
    publishable: bool


class AppendixAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    declarations: list[AppendixDeclaration] = Field(default_factory=list)
    artifacts: list[AppendixArtifact] = Field(default_factory=list)
    issues: list[AppendixIssue] = Field(default_factory=list)
