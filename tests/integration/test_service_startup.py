from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.app.api.api import app
from src.common.config.required import REQUIRED_SETTINGS, MissingSettingsError

_REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = _REPO_ROOT / ".env.example"


@pytest.fixture
def missing_setting(monkeypatch: pytest.MonkeyPatch, name):
    """Ровно одна настройка отсутствует; остальные заданы, чтобы падение было адресным."""
    values: dict[str, object] = {
        setting.name: f"value-for-{setting.name}" for setting in REQUIRED_SETTINGS if setting.name != name
    }
    monkeypatch.setattr("src.common.config.required.settings", values)


@pytest.mark.parametrize("name", [setting.name for setting in REQUIRED_SETTINGS])
def test_startup_requires_service_credentials(missing_setting, name) -> None:
    """Сервис не запускается без обязательного ключа."""
    with pytest.raises(MissingSettingsError) as raised, TestClient(app):
        pytest.fail("приложение поднялось без обязательной настройки")

    assert name in str(raised.value)


def test_configuration_example_lists_required_settings() -> None:
    """Пример конфигурации содержит все обязательные настройки."""
    declared = set(re.findall(r"^([A-Z0-9_]+)=", ENV_EXAMPLE.read_text(encoding="utf-8"), flags=re.MULTILINE))
    missing = sorted({setting.name for setting in REQUIRED_SETTINGS} - declared)
    assert not missing, (
        f"В .env.example нет обязательных настроек: {', '.join(missing)}. "
        "Без них `cp .env.example .env` даёт MissingSettingsError на старте."
    )
