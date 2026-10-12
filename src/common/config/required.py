from __future__ import annotations

from dataclasses import dataclass

from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)

# Путь навыка в Vault; совпадает с ENV_FOR_DYNACONF манифестов ci_cd/values*.yaml.
VAULT_PATH = "secrets/dynaconf/antikorr"

_VAULT_SECRET = f"секрет Vault {VAULT_PATH} (локально — .env)"
_TOML_OR_HELM = "src/common/config/antikorr.toml, в кластере — config: в ci_cd/values*.yaml"


class MissingSettingsError(RuntimeError):
    """Обязательной настройки нет: сервис не поднимается."""


@dataclass(frozen=True)
class RequiredSetting:
    "Настройка, без которой запускать сервис бессмысленно."

    name: str
    source: str
    why: str


REQUIRED_SETTINGS: tuple[RequiredSetting, ...] = (
    RequiredSetting(
        name="ROUTER_AI_API_KEY",
        source=_VAULT_SECRET,
        why=(
            "ключ читается при сборке КАЖДОЙ LLM-роли (src/infrastructure/llm/client.py); вся семантика "
            "экспертизы — LLM, поэтому без ключа не собирается ни одна роль и сервис не "
            "поднимается вовсе"
        ),
    ),
    RequiredSetting(
        name="ROUTER_AI_BASE_URL",
        source=_TOML_OR_HELM,
        why=(
            "адрес RouterAI. Значение по умолчанию в antikorr.toml есть, но пустой override "
            "LLM не отключает: langchain_openai делает `openai_api_base or os.getenv(...)`, "
            "пустая строка отбрасывается, и клиент молча уходит на api.openai.com — ключ "
            "RouterAI отправляется чужому провайдеру"
        ),
    ),
    RequiredSetting(
        name="S3_SERVICE_BASE_URL",
        source=_TOML_OR_HELM,
        why=("адрес s3-service. `file_ids` — единственный вход навыка (src/app/mcp/runner.py), другого источника документа нет"),
    ),
    RequiredSetting(
        name="S3_SERVICE_ACCESS_TOKEN",
        source=_VAULT_SECRET,
        why=(
            "без токена S3ServiceAsyncClient._service_headers() отказывает на каждом скачивании: "
            "ни один документ не может быть прочитан"
        ),
    ),
    RequiredSetting(
        name="BASE_MCP_URL",
        source="config: в ci_cd/values*.yaml (локально — .env)",
        why=(
            "пустой URL не отказывает, а молча выключает MCP (mcp_is_enabled()) — сверка тогда "
            "отдаёт «нарушений не найдено» вместо признания невозможности проверки"
        ),
    ),
)


def _is_set(name: str) -> bool:
    """Пустая строка и пробелы значением не считаются: Helm и .env задают «нет значения» именно так."""
    value = settings.get(name, None)
    if value is None:
        return False
    return bool(str(value).strip())


def missing_settings(required: tuple[RequiredSetting, ...] = REQUIRED_SETTINGS) -> list[RequiredSetting]:
    return [setting for setting in required if not _is_set(setting.name)]


def format_missing(missing: list[RequiredSetting]) -> str:
    lines = [
        f"Не заданы обязательные настройки ({len(missing)}). Без них сервис нерабочий, поэтому старт прерван:",
    ]
    lines += [f"  - {setting.name} — {setting.why}.\n    Источник: {setting.source}" for setting in missing]
    lines.append("Значения настроек здесь не печатаются: часть из них — секреты.")
    return "\n".join(lines)


def check_required_settings(required: tuple[RequiredSetting, ...] = REQUIRED_SETTINGS) -> None:
    "Падает с явной ошибкой, если хоть одна обязательная настройка не задана."
    missing = missing_settings(required)
    if not missing:
        return
    message = format_missing(missing)
    # Имена — не секреты; значения в сообщение не попадают.
    log.critical("required_settings_missing", settings=[setting.name for setting in missing])
    raise MissingSettingsError(message)


def require_settings(*names: str) -> None:
    "Проверить ИМЕНОВАННОЕ подмножество настроек прямо в точке чтения."
    by_name = {setting.name: setting for setting in REQUIRED_SETTINGS}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise ValueError(f"настройки не объявлены в REQUIRED_SETTINGS: {', '.join(sorted(unknown))}")
    check_required_settings(tuple(by_name[name] for name in names))
