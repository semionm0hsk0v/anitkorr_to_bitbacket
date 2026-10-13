"""Конфигурация LLM-ролей, проверка ENV и параметры запросов."""

from __future__ import annotations

import functools
import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from src.agents.llm.errors import LlmEnvOverrideError, LlmOptionError
from src.common.config.config import settings

_ENV_SEPARATOR = "__"


_ENV_ROOT = "LLM"


_ENV_DYNACONF_PREFIX = "DYNACONF_"


class _Missing:
    """Отсутствие ключа как значение: ``None`` — законное значение параметра."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - только для сообщений об ошибке
        return "MISSING"


_MISSING = _Missing()


@dataclass(frozen=True, slots=True)
class _EnvOverride:
    """Разобранное переопределение, которое действительно доходит до модели."""

    env_name: str
    role: str
    option: str
    #: Путь ниже параметра: ``LLM__LEGAL_VERDICT__PROVIDER__SORT`` → ``("sort",)``.
    nested: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _EnvOverrideIssue:
    """Переопределение, которое применено быть не может, с причиной из закрытого набора."""

    env_name: str
    #: ``UNKNOWN_ROLE`` | ``OPTION_NOT_APPLIED`` | ``MISPLACED_PREFIX``
    reason: str
    role: str | None
    option: str | None
    detail: str


def _option(cfg: Any, key: str, default: Any = _MISSING) -> Any:
    "Значение параметра роли независимо от регистра ключа."
    try:
        keys = list(cfg.keys())
    except AttributeError:
        return default
    wanted = key.casefold()
    matches = [name for name in keys if str(name).casefold() == wanted]
    if not matches:
        return default
    values = [cfg[name] for name in matches]
    if len(matches) > 1 and any(value != values[0] for value in values[1:]):
        raise LlmOptionError(f"параметр {key!r} задан в несовместимых регистрах: {sorted(map(str, matches))}")
    return values[0]


def _split_env_name(env_name: str) -> list[str] | None:
    """Разбить имя переменной на уровни; ``None`` — это не путь по конфигурации."""
    name = env_name[len(_ENV_DYNACONF_PREFIX) :] if env_name.startswith(_ENV_DYNACONF_PREFIX) else env_name
    parts = name.split(_ENV_SEPARATOR)
    if len(parts) < 2 or any(not part for part in parts):
        return None
    return parts


def _parse_env_overrides(
    environ: Mapping[str, str],
    *,
    model_roles: Iterable[str],
    model_options: Iterable[str],
) -> tuple[list[_EnvOverride], list[_EnvOverrideIssue]]:
    "Разобрать окружение на применимые переопределения и на неприменимые с причиной."
    roles = {str(role).casefold() for role in model_roles}
    options = {str(name).casefold() for name in model_options}
    applied: list[_EnvOverride] = []
    issues: list[_EnvOverrideIssue] = []
    for env_name in sorted(environ):
        parts = _split_env_name(env_name)
        if parts is None:
            continue
        root, *rest = parts
        if root.casefold() != _ENV_ROOT.casefold():
            if len(parts) >= 4 and parts[-3].casefold() == _ENV_ROOT.casefold():
                role, name = parts[-2].casefold(), parts[-1].casefold()
                if role in roles and name in options:
                    issues.append(
                        _EnvOverrideIssue(
                            env_name=env_name,
                            reason="MISPLACED_PREFIX",
                            role=role,
                            option=name,
                            detail=f"путь роли начинается не с {_ENV_ROOT}{_ENV_SEPARATOR}; верное имя — "
                            f"{_ENV_ROOT}{_ENV_SEPARATOR}{role.upper()}{_ENV_SEPARATOR}{name.upper()}",
                        )
                    )
            continue
        if len(rest) < 2:
            # Ключ самой секции ``[llm]`` (например ``LLM__RETRIES``), не параметр роли.
            continue
        role, name, *nested = (part.casefold() for part in rest)
        if role not in roles:
            issues.append(
                _EnvOverrideIssue(
                    env_name=env_name,
                    reason="UNKNOWN_ROLE",
                    role=role,
                    option=name,
                    detail=f"роль {role!r} не собирает модель; известные роли: {', '.join(sorted(roles))}",
                )
            )
            continue
        if name not in options:
            issues.append(
                _EnvOverrideIssue(
                    env_name=env_name,
                    reason="OPTION_NOT_APPLIED",
                    role=role,
                    option=name,
                    detail=f"параметр {name!r} до модели не доходит; применимые: {', '.join(sorted(options))}",
                )
            )
            continue
        applied.append(_EnvOverride(env_name=env_name, role=role, option=name, nested=tuple(nested)))
    return applied, issues


def _format_env_override_issues(issues: Iterable[_EnvOverrideIssue]) -> str:
    return "; ".join(f"{issue.env_name} [{issue.reason}]: {issue.detail}" for issue in issues)


def _audit_env_overrides(
    *,
    model_roles: Iterable[str],
    model_options: Iterable[str],
    environ: Mapping[str, str] | None = None,
) -> list[_EnvOverride]:
    "Проверить окружение перед сборкой модели; неприменимое поднять отказом."
    applied, issues = _parse_env_overrides(
        os.environ if environ is None else environ,
        model_roles=model_roles,
        model_options=model_options,
    )
    if issues:
        raise LlmEnvOverrideError(_format_env_override_issues(issues))
    return applied


_LLM_ROLES = frozenset(
    {
        "competence",
        "competence_confirm",
        "competence_parse",
        "legal_plan",
        "legal_selection",
        "legal_verdict",
        "legal_scope",
        "legal_refusal",
        "legal_dedup",
    }
)


_LLM_OPTIONAL_KWARGS = (
    "stream_usage",
    "temperature",
    "top_p",
    "max_tokens",
    "timeout",
    "streaming",
    "max_retries",
)


_EXTRA_BODY_KEYS = ("reasoning", "provider", "plugins", "models", "transforms")


_LLM_MODEL_OPTIONS = frozenset({"model", *_LLM_OPTIONAL_KWARGS, *_EXTRA_BODY_KEYS})


@functools.lru_cache(maxsize=1)
def _audit_llm_env_overrides() -> tuple[str, ...]:
    "Один раз на процесс: переопределения ролей из окружения либо применимы, либо отказ."
    applied = _audit_env_overrides(
        model_roles=_LLM_ROLES,
        model_options=_LLM_MODEL_OPTIONS,
    )
    return tuple(override.env_name for override in applied)


def validate_llm_role_config(
    llm_section: Any = None,
    *,
    model_roles: Iterable[str] = _LLM_ROLES,
    model_options: Iterable[str] = _LLM_MODEL_OPTIONS,
) -> None:
    "Конфигурация ролей без неизвестных ключей: опечатка отказывает, а не молча теряется."
    section = settings.get("llm") if llm_section is None else llm_section
    if not isinstance(section, Mapping):
        raise LlmOptionError("llm must be a mapping of required roles and policy")
    roles = {str(role).casefold() for role in model_roles}
    options = {str(name).casefold() for name in model_options}
    problems: list[str] = []
    unknown_sections = sorted(
        str(name) for name, value in section.items() if isinstance(value, Mapping) and str(name).casefold() not in roles
    )
    if unknown_sections:
        problems.append(f"секции [llm.*], которые не роль: {', '.join(unknown_sections)}; роли: {', '.join(sorted(roles))}")
    for role in sorted(roles):
        cfg = _option(section, role)
        if not isinstance(cfg, Mapping):
            problems.append(f"роль {role}: обязательная секция отсутствует или не является mapping")
            continue
        unknown_keys = sorted(str(key) for key in cfg if str(key).casefold() not in options)
        if unknown_keys:
            problems.append(f"роль {role}: неизвестные ключи {', '.join(unknown_keys)}")
    if problems:
        raise LlmOptionError(f"{'; '.join(problems)}. Допустимые ключи роли: {', '.join(sorted(options))}")


def retry_policy() -> tuple[int, tuple[float, ...]]:
    "Единая политика повторов для генерации текста и данных по схеме."
    retries = settings["llm.retries"]
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise LlmOptionError("llm.retries must be a nonnegative integer (additional attempts)")
    raw_waits = settings["llm.retry_wait_s"]
    if (
        not isinstance(raw_waits, list | tuple)
        or not raw_waits
        or any(isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v < 0 for v in raw_waits)
    ):
        raise LlmOptionError("llm.retry_wait_s must contain finite nonnegative waits")
    return retries, tuple(float(v) for v in raw_waits)


def validate_call_options(*, timeout_s: float | None, retries: int | None, deadline: float | None, min_attempt_s: float) -> None:
    if retries is not None and (isinstance(retries, bool) or not isinstance(retries, int) or retries < 0):
        raise ValueError("retries must be a nonnegative integer (additional attempts)")
    for name, value in (("timeout_s", timeout_s), ("deadline", deadline), ("min_attempt_s", min_attempt_s)):
        if value is None and name != "min_attempt_s":
            continue
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if (name == "timeout_s" and value <= 0) or (name == "min_attempt_s" and value < 0):
            raise ValueError(f"{name} outside supported range")
    if timeout_s is not None and min_attempt_s > timeout_s:
        raise ValueError("min_attempt_s must not exceed timeout_s")


_ESCALATE_FACTOR = float(settings["agents.utils.llm.escalate_factor"])


_ESCALATE_CAP = int(settings["agents.utils.llm.escalate_cap"])


_TOKEN_STEP = int(settings["agents.utils.llm.token_step"])


def model_kwargs(llm_cfg: Any) -> dict[str, Any]:
    "LLM шага целиком из конфига ``[llm.<step>]``: модель, сэмплинг, роутинг."

    _audit_llm_env_overrides()
    validate_llm_role_config()
    model = _option(llm_cfg, "model")
    if not isinstance(model, str) or not model.strip():
        # Роль без модели собрать нельзя; молча подставлять чужое значение — хуже отказа.
        raise LlmOptionError("в конфигурации роли нет ключа `model`")
    kwargs: dict[str, Any] = {
        "model": model,
    }

    for key in _LLM_OPTIONAL_KWARGS:
        value = _option(llm_cfg, key)
        if value is not _MISSING:
            kwargs[key] = value

    extra_body = {key: value for key in _EXTRA_BODY_KEYS if (value := _option(llm_cfg, key)) is not _MISSING}
    if extra_body:
        kwargs["extra_body"] = extra_body

    sdk_retries = kwargs.get("max_retries", 0)
    if isinstance(sdk_retries, bool) or not isinstance(sdk_retries, int) or sdk_retries != 0:
        raise LlmOptionError("LLM SDK max_retries must be 0; retries belong to the operation policy")
    kwargs["max_retries"] = 0
    _validate_model_options(kwargs)
    return kwargs


def _validate_model_options(options: dict[str, Any]) -> None:
    for name in ("streaming", "stream_usage"):
        if name in options and not isinstance(options[name], bool):
            raise LlmOptionError(f"{name} must be a boolean")
    for name in ("max_tokens", "timeout", "temperature", "top_p"):
        if name not in options:
            continue
        value = options[name]
        if value is None and name in {"max_tokens", "temperature", "top_p"}:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise LlmOptionError(f"{name} must be finite")
        if name == "max_tokens" and (not isinstance(value, int) or value <= 0):
            raise LlmOptionError("max_tokens must be a positive integer")
        if name == "timeout" and value <= 0:
            raise LlmOptionError("timeout must be positive")
        if name == "temperature" and not 0 <= value <= 2:
            raise LlmOptionError("temperature must be between 0 and 2")
        if name == "top_p" and not 0 <= value <= 1:
            raise LlmOptionError("top_p must be between 0 and 1")
    extra = options.get("extra_body", {})
    for name in ("reasoning", "provider"):
        if name in extra and not isinstance(extra[name], Mapping):
            raise LlmOptionError(f"{name} must be a mapping")
    for name in ("plugins", "models", "transforms"):
        if name in extra and not isinstance(extra[name], list | tuple):
            raise LlmOptionError(f"{name} must be a sequence")


def validate_llm_config() -> None:
    """Fail before MCP startup; validate configuration without constructing SDK/HTTP clients."""
    validate_llm_role_config()
    _audit_env_overrides(model_roles=_LLM_ROLES, model_options=_LLM_MODEL_OPTIONS)
    retry_policy()
    timeout = settings["llm.http_timeout_s"]
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not math.isfinite(timeout) or timeout <= 0:
        raise LlmOptionError("llm.http_timeout_s must be finite and positive")
    _validate_connection_url(settings.get("ROUTER_AI_BASE_URL"), "ROUTER_AI_BASE_URL", {"http", "https"})
    proxy = settings.get("PROXY_URL")
    if proxy:
        _validate_connection_url(proxy, "PROXY_URL", {"http", "https", "socks5", "socks5h"})
    for role in sorted(_LLM_ROLES):
        model_kwargs(_option(settings.get("llm"), role))


def _validate_connection_url(value: Any, name: str, schemes: set[str]) -> None:
    try:
        url = urlsplit(value) if isinstance(value, str) else None
        valid = url is not None and url.scheme in schemes and bool(url.hostname)
        if url is not None:
            _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise LlmOptionError(f"{name} must be a valid connection URL")
