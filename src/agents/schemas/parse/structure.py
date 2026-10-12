from pydantic import BaseModel, Field


class DocumentStructureNode(BaseModel):
    """Узел дерева структуры документа (этап 2)."""

    element_id: str = Field(description="Уникальный идентификатор элемента в дереве")
    kind: str = Field(
        description=(
            "Тип элемента: document | preamble | chapter | section | article | part | clause | subclause | appendix | paragraph"
        ),
    )
    label: str = Field(
        default="",
        description="Человекочитаемая метка (ст. 5, п. 1.2, прил. 1 и т.п.)",
    )
    text: str = Field(description="Текст элемента")
    char_start: int = Field(ge=0, description="Начало в исходном document_text")
    char_end: int = Field(ge=0, description="Конец в исходном document_text")
    parent_intro: str | None = Field(
        default=None,
        description="Вводная фраза родительского пункта, оканчивающаяся на «:» (читается вместе с этим узлом)",
    )
    address_resolved: bool = Field(
        default=True,
        description=(
            "Адрес узла доказан переносом происхождения символов в канонический текст: "
            "text[char_start:char_end] == text узла. При false координаты каноническими "
            "не считаются, узел остаётся кандидатом для анализа, но не может дать exact "
            "evidence, hard reject или публикационное доказательство"
        ),
    )
    address_reason: str | None = Field(
        default=None,
        description="Причина неразрешённого адреса (SegmentAddressReason), если address_resolved=false",
    )
    children: list["DocumentStructureNode"] = Field(default_factory=list)


class DocumentStructureTree(BaseModel):
    """Корень дерева структуры проекта НПА."""

    root: DocumentStructureNode
