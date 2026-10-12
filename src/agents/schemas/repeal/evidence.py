from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictRepealModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepealSourceEvidence(_StrictRepealModel):
    """Реквизиты и идентификатор источника после проверки его действующей редакции."""

    act_number: str = Field(min_length=1, max_length=64)
    act_date: str = Field(default="", max_length=32)
    source_base: str = Field(min_length=1, max_length=128)
    source_doc_num: int = Field(gt=0)


class RepealUnitScopeEvidence(_StrictRepealModel):
    """Unit-level coverage of ONE editor relative to ONE base act.

    Отдельная запись, а не переопределение ``repealed_by_project``: тот остаётся
    АКТ-уровневым («проект признал утратившим силу сам этот акт»), а здесь описан
    объём отмены по пунктам, когда проект снял только отдельные положения редактора.
    Полнота требует полностью разрешённого адресного пространства; доказанная
    неполнота может сохранять неизвестные участки, и терять их нельзя.
    """

    status: Literal["proven_complete", "proven_incomplete"]
    reason: str = Field(min_length=1, max_length=64)
    #: Полная identity базы: номера и даты акт не опознают.
    base_act_kind: str = Field(min_length=1, max_length=32)
    base_act_number: str = Field(min_length=1, max_length=64)
    base_act_date: str = Field(min_length=10, max_length=10)
    #: Пусто означает «орган не различался», а не «совпал».
    base_act_issuer: str = Field(default="", max_length=256)
    #: Пункты редактора, связь которых с базой доказана: непустые и уникальные.
    bound_units: tuple[str, ...] = Field(min_length=1)
    #: Точные границы этих пунктов в тексте редактора, из которого они прочитаны.
    bound_unit_spans: tuple[tuple[int, int], ...] = ()
    covered_units: tuple[str, ...] = ()
    uncovered_units: tuple[str, ...] = ()
    #: Неразрешённые участки (пропуски ряда, непрояснённые пункты, хвост).
    unresolved_units: tuple[str, ...] = ()
    #: Объём отмены проекта в нормализованных адресах, с которым шло сопоставление.
    project_repealed_units: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _scope_is_consistent(self) -> RepealUnitScopeEvidence:
        for units in (self.bound_units, self.covered_units, self.uncovered_units, self.unresolved_units):
            if len(set(units)) != len(units) or any(not unit.strip() for unit in units):
                raise ValueError("unit addresses must be non-empty and unique")
        if set(self.covered_units) & set(self.uncovered_units):
            raise ValueError("covered and uncovered units must not overlap")
        if set(self.covered_units) | set(self.uncovered_units) != set(self.bound_units):
            raise ValueError("unit scope must classify every bound unit exactly once")
        if set(self.covered_units) != set(self.bound_units) & set(self.project_repealed_units):
            raise ValueError("covered units must equal the intersection with project scope")
        if len(self.bound_unit_spans) != len(self.bound_units):
            raise ValueError("every bound unit needs its exact span")
        if any(end <= start or start < 0 for start, end in self.bound_unit_spans):
            raise ValueError("unit spans must be non-empty forward ranges")
        if (self.status == "proven_complete") is bool(self.uncovered_units):
            raise ValueError("proven_complete requires no uncovered unit")
        if self.status == "proven_complete" and self.unresolved_units:
            raise ValueError("proven_complete requires a fully resolved address space")
        return self


class RepealAmenderEvidence(_StrictRepealModel):
    """One editor whose amending relation to the base act was positively proven."""

    source: RepealSourceEvidence
    #: Which positive formula proved the relation, e.g. ``amendment_formula``.
    relation_proof: str = Field(min_length=1, max_length=64)
    #: АКТ-уровень: признал ли проект утратившим силу сам этот акт целиком.
    repealed_by_project: bool

    unit_scope: RepealUnitScopeEvidence | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _whole_act_and_unit_scope_are_distinct(self) -> RepealAmenderEvidence:
        if self.unit_scope is not None and self.repealed_by_project:
            raise ValueError("unit coverage cannot assert repeal of the whole editor")
        return self


class RepealRedundantEvidence(_StrictRepealModel):
    """Пункт, для которого в проверенной редакции прочитана аннотация об утрате силы."""

    target: RepealSourceEvidence
    subpoint: str = Field(min_length=1, max_length=32)


class RepealChainEvidence(_StrictRepealModel):
    """База, доказанные изменяющие акты и точные источники неотменённых актов."""

    base: RepealSourceEvidence
    amenders: tuple[RepealAmenderEvidence, ...] = ()
    orphan_numbers: tuple[str, ...] = ()
    orphan_source_ids: tuple[tuple[str, int], ...] = ()

    @model_validator(mode="after")
    def _chain_is_consistent(self) -> RepealChainEvidence:
        if len(set(self.orphan_source_ids)) != len(self.orphan_source_ids):
            raise ValueError("orphan source identities must be unique")
        orphan_editors = []
        for source_id in self.orphan_source_ids:
            matches = [item for item in self.amenders if (item.source.source_base, item.source.source_doc_num) == source_id]
            if len(matches) != 1:
                raise ValueError("orphan source identity must name exactly one proven editor")
            editor = matches[0]
            if editor.repealed_by_project:
                raise ValueError("an orphan cannot also be repealed by the project")
            if editor.unit_scope is not None:
                raise ValueError("an editor with unit-level scope cannot be reported as a forgotten act")
            orphan_editors.append(editor)
        if {item.source.act_number for item in orphan_editors} != set(self.orphan_numbers):
            raise ValueError("orphan numbers must match the exact orphan source identities")
        return self
