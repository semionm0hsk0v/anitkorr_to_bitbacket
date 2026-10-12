"""Генерация агента: текст или данные по схеме."""

from src.agents.llm.client import LLMClient, llm
from src.agents.llm.result import LLMResult

__all__ = ["LLMClient", "LLMResult", "llm"]
