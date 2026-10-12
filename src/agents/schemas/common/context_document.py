from typing import TypedDict


class ContextDocument(TypedDict):
    "Сопроводительный документ загрузки (пояснительная записка и т.п.)."

    name: str
    kind: str  # explanatory_note
    text: str
    source_document_id: str | None
