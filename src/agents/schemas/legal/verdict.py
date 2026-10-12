from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.schemas.legal.sources import LegalReading


class ComplianceIssue(BaseModel):
    """Одно найденное несоответствие проекта вышестоящей норме (L3, ``compliance-v2.md`` §4.2)."""

    model_config = ConfigDict(extra="forbid")

    project_quote: str = Field(description="Дословный фрагмент проекта в месте нарушения")
    source_id: str = Field(description="ID акта из переданных источников")
    source_quote: str = Field(description="Дословный фрагмент вышестоящей нормы")
    explanation: str = Field(
        description="Конкретное расхождение и правовое последствие с учётом применимости нормы, "
        "связанных положений, исключений и соотношения юридической силы актов"
    )


class LegalVerdict(BaseModel):
    """Ответ совместной сверки проекта с прочитанными источниками."""

    model_config = ConfigDict(extra="forbid")

    provider_strict: ClassVar[bool] = True

    issues: list[ComplianceIssue] = Field(description="Несоответствия; нет — пустой список")


class SourceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item: LegalReading
    text: str

    @property
    def source_id(self) -> str:
        return self.item.reading.source_id

    @property
    def title(self) -> str:
        raw = self.item.reading.raw
        return " ".join(str(raw.title).split()) if raw is not None else self.item.reading.ref.label

    def render(self) -> str:
        raw = self.item.reading.raw
        edition = []
        if raw is not None and raw.edition:
            edition.append(f"редакция от {raw.edition}")

        edition.append("по данным Консультант+ на дату экспертизы акт не утратил силу")
        if self.item.reading.status is ReadingStatus.TRUNCATED:
            edition.append(
                f"ТЕКСТ АКТА НЕПОЛНЫЙ: получены абзацы 1–{self.item.reading.fetched_paragraphs} "
                f"из {self.item.reading.total_paragraphs}"
            )
        return (
            f"=== source_id: {self.source_id} ===\n{self.title}\nСведения о редакции: {'; '.join(edition)}\n"
            f"Показано: весь доступный текст акта\n\n{self.text}"
        )


class BlockResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)
    block: SourceBlock
    verdict: LegalVerdict | None
    exception: BaseException | None = None
    limitation: str = ""
