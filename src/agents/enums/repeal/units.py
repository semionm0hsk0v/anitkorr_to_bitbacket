from enum import StrEnum


class UnitRole(StrEnum):
    """Роль одного пункта редактора по отношению к РАССМАТРИВАЕМОЙ базе."""

    #: Положительно доказано: пункт изменяет именно эту базу.
    AMENDS_BASE = "amends_base"
    #: Пункт сам утратил силу: отмены не требует, его предмет не читается.
    LOST_FORCE = "lost_force"
    #: Пункт действует и относится к ДРУГОМУ, положительно названному акту.
    OTHER_SUBJECT = "other_subject"
    #: Пункт может относиться к базе, но доказать это по доступному тексту нельзя.
    UNRESOLVED = "unresolved"


class UnitScopeStatus(StrEnum):
    """Итог сопоставления объёма отмены проекта с положениями редактора."""

    #: Все относящиеся к базе положения редактора проект отменил.
    PROVEN_COMPLETE = "proven_complete"
    #: Есть хотя бы одно доказанное положение, которое проект не отменил.
    PROVEN_INCOMPLETE = "proven_incomplete"
    #: Объём не установлен. Это не «покрыто» и не «нарушение».
    UNKNOWN = "unknown"


class UnitScopeReason(StrEnum):
    """Стабильная причина исхода. Молчание без причины неотличимо от честного нуля."""

    #: Реквизиты базы неполны: без даты однономерные акты не различаются.
    BASE_IDENTITY_NOT_EXACT = "base_identity_not_exact"
    #: Текст редактора получен не целиком — по срезу полнота не доказывается.
    SOURCE_TEXT_INCOMPLETE = "source_text_incomplete"
    #: Пунктов верхнего уровня в тексте не нашлось: адресное пространство неизвестно.
    UNIT_SEQUENCE_NOT_FOUND = "unit_sequence_not_found"
    #: В ряду пунктов есть пропуски: пропущенный пункт мог относиться к этой базе.
    UNIT_SEQUENCE_HAS_GAPS = "unit_sequence_has_gaps"
    #: Есть действующие пункты, чей предмет по доступному тексту не разрешён.
    UNRESOLVED_UNITS_PRESENT = "unresolved_units_present"
    #: Проект отменяет положение, которого в разобранном ряду нет: границы неизвестны.
    PROJECT_UNIT_NOT_LOCATED = "project_unit_not_located"
    #: Объём отмены проекта записан не поддержанной формой адреса и не разобран.
    PROJECT_UNIT_NOT_PARSED = "project_unit_not_parsed"
    #: Связь редактора с базой доказана вне этого модуля, но ни один пункт её не несёт.
    NO_UNIT_BINDS_BASE = "no_unit_binds_base"
    #: Все связанные с базой пункты входят в объём отмены проекта.
    ALL_BOUND_UNITS_REPEALED = "all_bound_units_repealed"
    #: Связанные с базой пункты остались за пределами объёма отмены проекта.
    BOUND_UNITS_NOT_REPEALED = "bound_units_not_repealed"
