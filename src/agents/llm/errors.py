"""Ошибки конфигурации и обработки генерации агента."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.common.errors import AntikorrError
from src.infrastructure.llm.errors import quota_exhausted

if TYPE_CHECKING:
    from src.agents.llm.result import AttemptInfo


class LlmOptionError(AntikorrError):
    """Конфигурация роли не позволяет собрать модель однозначно."""

    cause = "Неоднозначный параметр LLM-роли в конфигурации"


class LlmEnvOverrideError(AntikorrError):
    """Переопределение из окружения не может быть применено — и это не замалчивается."""

    cause = "Переопределение параметра LLM-роли из окружения не применимо"


class OutputTruncationError(ValueError):
    "The provider reached its output limit before completing generation."

    kind = "truncated_output"
    finish_reason = "length"


class LLMDeadlineError(TimeoutError):
    """No request was started because the operation budget was exhausted."""

    kind = "deadline_exceeded"


class LLMCallbackError(RuntimeError):
    """The consumer callback failed after possible delivery; never replay."""

    kind = "callback_error"


class EmptyOutputError(ValueError):
    """The provider returned a response envelope with no usable content."""

    kind = "empty_output"


def _is_quota_exhausted(exc: BaseException) -> bool:
    return quota_exhausted(exc)


class LLMGenerationError(RuntimeError):
    """Генерация не завершена; метаданные неуспешных попыток доступны вызывающему коду."""

    def __init__(self, kind: str, attempts: tuple[AttemptInfo, ...]) -> None:
        super().__init__(f"LLM generation failed: {kind}")
        self.kind = kind
        self.attempts = attempts
