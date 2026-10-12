from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.agents.enums.internal.addressing.address_space import AddressSpace

RefStatus = Literal["exists", "missing", "unknown", "external"]


class Address(BaseModel):
    """Адрес-адресат отсылки вместе с пространством, в котором он читается."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    space: AddressSpace
    item: str
    appendix: str | None = None
    table: str | None = None

    def human(self) -> str:
        """Человекочитаемый локус: «пункт 7 таблицы 2 приложения № 1»."""
        parts = [f"пункт {self.item}"]
        if self.table:
            parts.append(f"таблицы {self.table}")
        if self.appendix:
            parts.append(f"приложения № {self.appendix}")
        if self.space is AddressSpace.AMENDED_ACT:
            parts.append("изменяемого акта")
        return " ".join(parts)


class Region(BaseModel):
    """Непрерывный участок документа с собственной нумерацией."""

    model_config = ConfigDict(extra="forbid")

    space: AddressSpace
    char_start: int
    char_end: int
    appendix: str | None = None
    table: str | None = None
    #: номер пункта → дословный текст пункта (для сверки предмета отсылки)
    items: dict[str, str] = Field(default_factory=dict)

    item_spans: dict[str, tuple[int, int]] = Field(default_factory=dict)
    geometry_rows: dict[str, tuple[tuple[int, int], ...]] = Field(default_factory=dict)
    geometry_fragmented: set[str] = Field(default_factory=set)
    geometry_unknown: set[str] = Field(default_factory=set)


class AddressBook(BaseModel):
    """Реестры адресов проекта, разложенные по пространствам."""

    model_config = ConfigDict(extra="forbid")

    regions: list[Region]
    core_addresses: set[str]
    is_amendment: bool
    amendment_spans: list[tuple[int, int]]

    def appendix_regions(self, number: str | None) -> list[Region]:
        "ВСЕ приложения с этим номером."
        if number is None:
            return []
        return [r for r in self.regions if r.space is AddressSpace.APPENDIX and r.appendix == number]

    def appendix_region(self, number: str | None) -> Region | None:
        """Единственное приложение с этим номером; при неоднозначности — ``None``."""
        regions = self.appendix_regions(number)
        return regions[0] if len(regions) == 1 else None

    def table_regions(self, appendix: str | None, table: str | None) -> list[Region]:
        "ВСЕ таблицы с этим номером (без номера приложения — во всём документе)."
        if table is None:
            return []
        return [
            r
            for r in self.regions
            if r.space is AddressSpace.TABLE and r.table == table and (appendix is None or r.appendix == appendix)
        ]

    def in_amendment(self, pos: int) -> bool:
        return any(lo <= pos < hi for lo, hi in self.amendment_spans)

    def core_has(self, item: str) -> bool | None:
        """Есть ли адрес в основном тексте? ``None`` — реестр пуст, судить нельзя."""
        registry = set(self.core_addresses)
        for region in self.regions:
            if region.space is AddressSpace.CORE:
                registry |= set(region.items)
        return _registry_has(item, registry)

    def region_has(self, region: Region | None, item: str) -> bool | None:
        if region is None:
            return None
        if item in region.geometry_unknown:
            return None
        if item in region.geometry_rows:
            return True
        return _registry_has(item, set(region.items))


class RefResolution(BaseModel):
    """Куда ведёт отсылка и что мы можем о ней утверждать."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    address: Address
    status: RefStatus
    region: Region | None = None
    reason: str = ""


class SubjectMismatch(BaseModel):
    """Отсылка ведёт к существующей строке таблицы, но НЕ О ТОМ предмете."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stem: str

    subject: str
    target: Address
    target_text: str
    correct: Address


def _registry_has(item: str, registry: set[str]) -> bool | None:
    """Адрес есть, если он в реестре или под ним есть вложенный («2» ← «2.12»)."""
    if not registry:
        return None
    if item in registry:
        return True
    prefix = item + "."
    return any(a.startswith(prefix) for a in registry)
