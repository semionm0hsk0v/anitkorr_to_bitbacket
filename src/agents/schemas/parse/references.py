from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.agents.enums.parse.references import AddressLevel, UnresolvedReason


class InternalRef(BaseModel):
    "Один извлечённый адресат: номер + уровень структуры + квалификатор пространства."

    model_config = ConfigDict(extra="forbid")

    address: str
    phrase: str
    char_start: int
    appendix: str | None = None
    table: str | None = None
    act: str = ""
    level: AddressLevel = AddressLevel.CLAUSE

    def __repr__(self) -> str:  # pragma: no cover
        return f"InternalRef({self.address!r}, level={self.level.value!r}, appendix={self.appendix!r}, table={self.table!r})"


class UnresolvedChain(BaseModel):
    "Распознанная ссылочная цепочка, из которой адрес не извлечён."

    model_config = ConfigDict(extra="forbid", frozen=True)

    phrase: str
    char_start: int
    act: str
    reason_code: UnresolvedReason
    reason: str


class ReferenceScan(BaseModel):
    """Полный результат разбора отсылок узла: и разрешённые адреса, и отказы."""

    model_config = ConfigDict(extra="forbid")

    refs: list[InternalRef] = Field(default_factory=list)
    unresolved: list[UnresolvedChain] = Field(default_factory=list)
