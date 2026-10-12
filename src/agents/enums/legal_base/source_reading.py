from enum import StrEnum


class ReadingStatus(StrEnum):
    "Исход чтения одного источника (§6.2–6.3)."

    COMPLETE = "complete"  # редакция и действие акта на дату экспертизы подтверждены, текст полный
    TRUNCATED = "truncated"  # редакция и действие акта подтверждены, текст обрезан бюджетом абзацев
    STATUS_ONLY = "status_only"  # редакция не подтверждена, либо действие акта не подтверждено — текст не передан
    NOT_FOUND = "not_found"  # по реквизитам акт в правовой базе не найден
    NOT_IN_FORCE = "not_in_force"
    IDENTITY_MISMATCH = "identity_mismatch"  # К+ вернул акт с другими реквизитами — не наш, не используется
    NO_DATE = "no_date"  # неполные реквизиты: без даты не ищется (§6.2)
    NOT_READ_CODE = "not_read_code"  # кодексы не читаются (§6.1)
    OVER_LIMIT = "over_limit"  # сверх ``max_sources``
    TRANSPORT_FAILURE = "transport_failure"  # сбой обращения к правовой базе
    DEADLINE = "deadline"  # дедлайн узла исчерпан до получения текста
