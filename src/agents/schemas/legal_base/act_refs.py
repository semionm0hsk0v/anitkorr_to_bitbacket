from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agents.steps_tools.sources.normalization import (
    act_authority_hint,
    card_date,
    card_number,
    card_number_identity,
    normal_identity,
)


class ActRequisites(BaseModel):
    "Реквизиты акта, разложенные из ссылки, — вход карточного поиска."

    model_config = ConfigDict(extra="forbid")

    kind: str = ""
    number: str = ""
    date: str = ""
    region: str = ""
    federal: bool = False
    title: str = ""
    raw: str = ""

    @property
    def card_number(self) -> str:
        return card_number(self.number)

    @property
    def identity_number(self) -> str:
        """Ключ тождества номера — для дедупа; ``card_number`` — для запроса в К+."""

        return card_number_identity(self.number)

    @property
    def card_date(self) -> str:
        "Дата в написании К+ — строго ``ДД.ММ.ГГГГ``."

        return card_date(self.date)

    def __repr__(self) -> str:  # диагностика в логах
        return (
            f"ActRequisites(kind={self.kind!r}, number={self.number!r}, date={self.date!r}, "
            f"region={self.region!r}, federal={self.federal})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ActRequisites):
            return NotImplemented
        return (
            self.kind,
            self.identity_number,
            self.card_date,
            self.federal,
            normal_identity(self.region),
            act_authority_hint(self),
        ) == (
            other.kind,
            other.identity_number,
            other.card_date,
            other.federal,
            normal_identity(other.region),
            act_authority_hint(other),
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.kind,
                self.identity_number,
                self.card_date,
                self.federal,
                normal_identity(self.region),
                act_authority_hint(self),
            )
        )
