from enum import StrEnum


class RepealOutcomeReason(StrEnum):
    """Исход проверки полноты отмены: по документу и по одному базовому акту."""

    #: Пункта «признать утратившим силу» в проекте нет вовсе.
    REPEAL_CLAUSE_NOT_FOUND = "repeal_clause_not_found"
    #: Пункт есть, но целиком отменяемых актов в нём нет (только подпункты/ничего).
    NO_BASE_ACTS_IN_CLAUSE = "no_base_acts_in_clause"
    #: Базовый акт не поднят: карточка не нашлась либо транспорт недоступен.
    BASE_NOT_FOUND_OR_TRANSPORT_UNAVAILABLE = "base_not_found_or_transport_unavailable"
    #: Поднят не тот акт: номер поднятого не совпал с номером отменяемого.
    BASE_IDENTITY_MISMATCH = "base_identity_mismatch"
    #: Цель — сам акт-изменитель; его цепочка к базовому регламенту не относится.
    BASE_IS_AMENDING_ACT = "base_is_amending_act"
    #: Редакция поднята не полностью — по срезу нельзя доказать полноту цепочки.
    INCOMPLETE_DOCUMENT_COVERAGE = "incomplete_document_coverage"

    CURRENTNESS_NOT_CURRENT = "currentness_not_current"

    AMENDMENT_CHAIN_EMPTY = "amendment_chain_empty"

    AMENDMENT_SOURCE_UNAVAILABLE = "amendment_source_unavailable"
    #: Часть названных редакторов доказать не удалось — результат остаётся неполным.
    AMENDER_VERIFICATION_INCOMPLETE = "amender_verification_incomplete"
    #: Отмена части редактора не доказывает отмену его изменений именно в этой базе.
    AMENDER_UNIT_SCOPE_UNPROVEN = "amender_unit_scope_unproven"

    UNIT_SCOPE_INCOMPLETE = "unit_scope_incomplete"
    #: Проверка завершена: все доказанные редакторы отменены проектом (доказанный ноль).
    NO_ORPHANS = "no_orphans"
    #: Проверка завершена: найдены неотменённые редакторы.
    ORPHANS_FOUND = "orphans_found"


BLOCKING_REPEAL_REASONS: frozenset[RepealOutcomeReason] = frozenset(
    {
        RepealOutcomeReason.BASE_NOT_FOUND_OR_TRANSPORT_UNAVAILABLE,
        RepealOutcomeReason.BASE_IDENTITY_MISMATCH,
        RepealOutcomeReason.INCOMPLETE_DOCUMENT_COVERAGE,
        RepealOutcomeReason.CURRENTNESS_NOT_CURRENT,
        RepealOutcomeReason.AMENDMENT_SOURCE_UNAVAILABLE,
        RepealOutcomeReason.AMENDER_VERIFICATION_INCOMPLETE,
        RepealOutcomeReason.AMENDER_UNIT_SCOPE_UNPROVEN,
    }
)


class RedundantRepealReason(StrEnum):
    "Исход ОБРАТНОЙ проверки: «проект отменяет уже недействующее положение»."

    #: Подпунктов-целей в пункте отмены нет — проверять нечего.
    NO_SUBPOINT_TARGETS = "no_subpoint_targets"
    #: Отменяемый акт не поднят: карточка не нашлась либо транспорт недоступен.
    TARGET_NOT_FOUND_OR_TRANSPORT_UNAVAILABLE = "target_not_found_or_transport_unavailable"
    #: Поднят не тот акт.
    TARGET_IDENTITY_MISMATCH = "target_identity_mismatch"
    #: Редакция поднята не полностью: молчание среза статусом пункта не является.
    INCOMPLETE_DOCUMENT_COVERAGE = "incomplete_document_coverage"

    CURRENTNESS_NOT_CURRENT = "currentness_not_current"
    #: Структурная единица названа так, что адресовать её в тексте нельзя («а», «абзац»).
    SUBPOINT_NOT_ADDRESSABLE = "subpoint_not_addressable"
    #: Пункт с таким номером в действующей редакции не найден.
    SUBPOINT_NOT_LOCATED = "subpoint_not_located"

    SUBPOINT_AMBIGUOUS_LABEL = "subpoint_ambiguous_label"
    #: Пункт найден и аннотации об утрате силы НЕ несёт — штатное молчание.
    SUBPOINT_IN_FORCE = "subpoint_in_force"
    #: Пункт найден и несёт положительную аннотацию «утратил силу».
    SUBPOINT_LOST_FORCE = "subpoint_lost_force"
