from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agents.schemas.legal_base.currentness import (
    ReferenceCurrentnessReceipt,
    ReferenceVersionFingerprint,
    validate_reference_content,
)
from src.agents.steps_tools.sources.normalization import canonicalize_reference_title


class RawAct(BaseModel):
    """Сырой акт из Консультант+ (вход для LLM-извлечения норм, стадия B1.3)."""

    query: str
    base: str
    doc_num: int
    title: str
    edition: str | None = None
    issued: str | None = None  # дата принятия
    in_force: bool | None = None
    par_count: int = 0
    full_text: str = ""
    provenance: str = "Консультант+"
    version: ReferenceVersionFingerprint | None = None
    currentness_receipt: ReferenceCurrentnessReceipt | None = None

    status_search: str | None = None


class RawReferenceCacheEnvelope(BaseModel):
    """Persisted raw cache whitelist: version metadata + content, never runtime query."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = Field(default=1, frozen=True)
    version: ReferenceVersionFingerprint
    title: str = Field(min_length=1, max_length=8_000)
    content: str

    @model_validator(mode="after")
    def _validate_integrity_and_identity(self) -> RawReferenceCacheEnvelope:
        if canonicalize_reference_title(self.title) != self.version.identity.title:
            raise ValueError("cache title must match normalized version identity")
        if not validate_reference_content(self.version, self.content):
            raise ValueError("cache content fingerprint mismatch")
        return self


class CardPathOutcome(BaseModel):
    "Результат карточного поиска: акт, либо явное различение «пусто» и «неоднозначно»."

    model_config = ConfigDict(extra="forbid", frozen=True)

    act: RawAct | None
    ambiguous_count: int = 0

    @property
    def ambiguous(self) -> bool:
        return self.act is None and self.ambiguous_count > 0
