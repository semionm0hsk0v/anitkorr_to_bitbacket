"""Подключение к провайдеру и транспорт; политика генерации находится в agents.llm."""

from src.infrastructure.llm.client import ProviderClient

__all__ = ["ProviderClient"]
