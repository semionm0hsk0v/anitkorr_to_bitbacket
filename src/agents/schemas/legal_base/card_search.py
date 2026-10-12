from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class CardQuery(BaseModel):
    """Условия карточки поиска. Пустые поля в запрос не попадают."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str = ""  # COMMON_NUMBER — уже в написании К+ (``card_number``)
    vid_doc: str = ""  # COMMON_VID_DOC — одно значение из словаря К+
    date: str = ""  # COMMON_DATE — «ДД.ММ.ГГГГ», ровно одна дата
    docname: str = ""  # COMMON_DOCNAME — слова названия
    text: str = ""  # COMMON_TEXT — слова текста
    banks: tuple[str, ...] = ()  # срезы поиска; пусто — все доступные (медленнее)
    limit: int = 20

    def payload(self) -> dict[str, Any]:
        """Условия → аргументы инструмента (без пустых полей)."""
        out: dict[str, Any] = {}
        for key, value in (
            ("number", self.number),
            ("vid_doc", self.vid_doc),
            ("date", self.date),
            ("docname", self.docname),
            ("text", self.text),
        ):
            if value:
                out[key] = value
        if self.banks:
            out["slices"] = list(self.banks)
        out["limit"] = self.limit
        return out

    def is_empty(self) -> bool:
        return not any((self.number, self.vid_doc, self.date, self.docname, self.text))


class CardHit(BaseModel):
    """Документ карточной выдачи."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bank: str
    doc_num: int
    title: str


class ConsultantCapabilities(BaseModel):
    "Operation-level Consultant capabilities available to the current request."

    model_config = ConfigDict(extra="forbid", frozen=True)

    free_search: bool
    exact_card_search: bool
    doc_info: bool
    doc_text: bool

    @property
    def exact_retrieval(self) -> bool:
        return self.exact_card_search and self.doc_info and self.doc_text
