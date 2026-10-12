from enum import StrEnum


class AddressSpace(StrEnum):
    """Пространство, в котором ищется адресат отсылки."""

    CORE = "core"  # текст проекта: приказ + утверждаемый им акт
    APPENDIX = "appendix"  # приложение № N (своя нумерация)
    TABLE = "table"  # таблица N приложения (строки-«пункты»)
    AMENDED_ACT = "amended_act"  # изменяемый акт (проект о внесении изменений)
