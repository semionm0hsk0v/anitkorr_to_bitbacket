"""Общие offline-клиенты интеграционных проверок MCP и LLM."""

from __future__ import annotations

import json
from typing import Any

import httpx
import openai
import pytest
from fastmcp import Client

from src.app.mcp import mcp


class ChatProvider:
    """MockTransport-провайдер чата: отдаёт ответы по очереди (последний повторяется), помнит запросы."""

    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = replies or (self.ok(),)
        self.requests: list[httpx.Request] = []

    @staticmethod
    def ok(content: str = '{"ok": true}') -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "object": "chat.completion",
                "created": 0,
                "model": "offline/model",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.1},
            },
        )

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.replies[min(len(self.requests), len(self.replies)) - 1]


@pytest.fixture
async def chat_provider(monkeypatch: pytest.MonkeyPatch):
    """Продуктовая фабрика клиентов LLM-ролей с подставленным транспортом.

    Хуки, таймаут и заголовки клиента — настоящие; на провод запрос не выходит. Кэш клиентов
    сбрасывается до и после теста, чтобы подмена транспорта не пережила его.
    """
    from src.agents.llm import (
        client as llm_client,
        config as llm_config,
    )

    # langchain-openai наследует DefaultAsyncHttpxClient при первом импорте: подмена до него ломает класс.
    from src.infrastructure.llm.client import _cost_aware_chat_openai

    _cost_aware_chat_openai()
    fake = ChatProvider()
    real = openai.DefaultAsyncHttpxClient
    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", lambda **kwargs: real(transport=httpx.MockTransport(fake), **kwargs))
    await llm_client.llm.aclose()
    llm_client.llm._transport = None
    llm_client.llm._models.clear()
    llm_config._audit_llm_env_overrides.cache_clear()
    yield fake
    await llm_client.llm.aclose()
    llm_client.llm._transport = None
    llm_client.llm._models.clear()


@pytest.fixture
def call_review_tool():
    """Вызов публичного инструмента с записью пакетов прогресса."""

    async def call(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Вызов инструмента через MCP-клиент с перехватом пакетов прогресса."""
        packets: list[dict[str, Any]] = []

        async def on_log(message: Any) -> None:
            data = message.data
            packets.append(json.loads(data["msg"] if isinstance(data, dict) else data))

        async with Client(mcp, log_handler=on_log) as client:
            result = await client.call_tool("antikorr_invoke", {"data": payload})
        return result.data, packets

    return call
