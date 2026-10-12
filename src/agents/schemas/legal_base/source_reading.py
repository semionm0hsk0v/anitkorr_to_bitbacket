from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.schemas.legal_base.retrieval import RawAct
from src.agents.schemas.legal_base.source_refs import SourceRef

_LIMITATION_BY_STATUS: dict[ReadingStatus, str] = {
    ReadingStatus.TRUNCATED: "{label}: текст неполный (абзацы 1–{fetched} из {total}).",
    ReadingStatus.STATUS_ONLY: "{label}: действующая редакция не подтверждена, текст не передан.",
    ReadingStatus.NOT_IN_FORCE: "{label}: утратил силу по данным Консультант+ на дату экспертизы; сверка с ним не выполнялась.",
    ReadingStatus.NOT_FOUND: "{label}: не найден в правовой базе по реквизитам.",
    ReadingStatus.IDENTITY_MISMATCH: "{label}: по реквизитам найден другой акт, он не использован.",
    ReadingStatus.NO_DATE: "{label}: неполные реквизиты (нет даты), акт не запрашивался.",
    ReadingStatus.OVER_LIMIT: "{label}: не прочитан — превышен лимит источников ({limit}).",
    ReadingStatus.TRANSPORT_FAILURE: "{label}: сбой обращения к правовой базе.",
    ReadingStatus.DEADLINE: "{label}: дедлайн проверки исчерпан до получения текста.",
}


class SourceReading(BaseModel):
    """Один источник набора и исход его чтения."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: SourceRef
    status: ReadingStatus
    raw: RawAct | None = None
    fetched_paragraphs: int | None = None
    total_paragraphs: int | None = None
    limit: int | None = None

    @property
    def has_text(self) -> bool:
        """Текст можно показать модели (полный или с пометкой неполноты)."""
        return self.status in {ReadingStatus.COMPLETE, ReadingStatus.TRUNCATED}

    @property
    def complete(self) -> bool:
        """Источник может обосновать статус ``completed`` (§5.2)."""
        return self.status is ReadingStatus.COMPLETE

    @property
    def source_id(self) -> str:
        return f"{self.raw.base}:{self.raw.doc_num}" if self.raw is not None else ""

    def model_text(self) -> str:
        """Текст для модели: полный — как есть, обрезанный — с пометкой; иначе пусто."""
        if not self.has_text or self.raw is None:
            return ""
        if self.status is ReadingStatus.TRUNCATED:
            return f"ТЕКСТ НЕПОЛНЫЙ: абзацы 1–{self.fetched_paragraphs} из {self.total_paragraphs}\n\n{self.raw.full_text}"
        return str(self.raw.full_text)

    def limitation(self) -> str:
        """Причина неполноты для статуса проверки; пусто — ограничения нет."""
        template = _LIMITATION_BY_STATUS.get(self.status)
        if template is None:
            return ""
        return template.format(
            label=self.ref.label, fetched=self.fetched_paragraphs, total=self.total_paragraphs, limit=self.limit
        )
