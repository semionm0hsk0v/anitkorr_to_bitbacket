"""Stateful MCP -> настоящие фабрика/SDK -> MockTransport: идентичность текущего tools/call."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import BaseModel

from src.agents.llm import (
    client as llm_client,
)
from src.agents.llm.config import validate_llm_config
from src.agents.llm.errors import LLMGenerationError
from src.agents.llm.retry import invoke_structured
from src.agents.prompts import describe_prompt, get_prompt
from src.app.mcp import server
from src.common.config.config import settings
from src.infrastructure.llm.context import (
    HEADER_CHAT_ID,
    HEADER_OPERATION,
    HEADER_ORGANIZATION_ID,
    HEADER_REQUEST_ID,
    HEADER_SERVICE_NAME,
    HEADER_THREAD_ID,
    HEADER_USER_ID,
    RequestIdentityMiddleware,
)


class Answer(BaseModel):
    ok: bool


async def test_search_and_selection_use_independent_roles_and_accounting(chat_provider) -> None:
    """План и отбор используют отдельные роли, операции и учёт расхода."""

    chunk = {"id": "gen-1", "object": "chat.completion.chunk", "created": 0, "model": "offline/model"}
    events = [
        dict(chunk, choices=[{"index": 0, "delta": {"role": "assistant", "content": '{"ok": true}'}, "finish_reason": None}]),
        dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        dict(chunk, choices=[], usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.1}),
    ]
    chat_provider.replies = [
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n",
        ),
        chat_provider.ok(),
    ]
    validate_llm_config()
    llm = llm_client.llm
    plan = await llm.generate_structured(
        role="legal_plan", messages=[("user", "Запросы для поиска")], schema=Answer, operation="legal_discovery", retries=0
    )
    selection = await llm.generate_structured(
        role="legal_selection", messages=[("user", "Отбор акта")], schema=Answer, operation="legal_selection", retries=0
    )

    first, second = chat_provider.requests
    planning_body, selection_body = (json.loads(request.content) for request in (first, second))
    assert planning_body["model"] == "google/gemini-3.8-flash"
    assert selection_body["model"] == "google/gemini-3.8-flash"
    assert planning_body["stream"] is True
    assert selection_body["stream"] is False
    assert planning_body["temperature"] == 0
    assert selection_body["temperature"] == 0
    assert planning_body["provider"] == {"order": ["google-ai-studio/flex"], "allow_fallbacks": False}
    assert planning_body["reasoning"]["effort"] == "low"
    assert selection_body["provider"] == {"order": ["google-ai-studio/flex"], "allow_fallbacks": False}
    assert selection_body["reasoning"]["effort"] == "low"
    assert first.headers[HEADER_OPERATION] == "legal_discovery"
    assert second.headers[HEADER_OPERATION] == "legal_selection"
    assert first.headers[HEADER_REQUEST_ID] != second.headers[HEADER_REQUEST_ID]
    assert llm._models["legal_plan"] is not llm._models["legal_selection"]
    assert all(result.value.ok and result.usage_complete and result.cost == 0.1 for result in (plan, selection))
    assert plan.attempts[0].requested_model == planning_body["model"]
    assert selection.attempts[0].requested_model == selection_body["model"]


@pytest.fixture(autouse=True)
def _runner_that_calls_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(**kwargs: Any) -> dict[str, Any]:
        llm = llm_client.llm._build_model(settings.llm.legal_verdict)
        await invoke_structured(llm, [("user", "проект")], Answer, retries=0)
        return {"thread_id": kwargs["thread_id"], "message_id": kwargs["message_id"]}

    monkeypatch.setattr(server, "run_expertise_stream", run)


async def test_model_requests_use_current_user_and_chat(chat_provider) -> None:
    """Запросы модели используют текущего пользователя и чат без переноса данных прошлого вызова."""
    app = server.mcp.http_app(path="/", transport="http")
    wrapped = RequestIdentityMiddleware(app)
    headers = {HEADER_USER_ID: "initialize-user", HEADER_ORGANIZATION_ID: "initialize-org"}

    async def current_headers(request: httpx.Request) -> None:
        for name in (HEADER_USER_ID, HEADER_ORGANIZATION_ID, HEADER_CHAT_ID, HEADER_THREAD_ID):
            request.headers.pop(name, None)
        request.headers.update(headers)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped),
            event_hooks={"request": [current_headers]},
            **kwargs,
        )

    transport = StreamableHttpTransport("http://skill/", httpx_client_factory=factory)
    async with app.lifespan(app), Client(transport) as client:
        first_chat, first_message = str(uuid4()), str(uuid4())
        headers.clear()
        headers.update({HEADER_USER_ID: "call-user", HEADER_CHAT_ID: "header-chat"})
        await client.call_tool("antikorr_invoke", {"data": {"thread_id": first_chat, "message_id": first_message}})
        headers.clear()
        second_chat = str(uuid4())
        result = await client.call_tool("antikorr_invoke", {"data": {"thread_id": second_chat}})

    first, second = chat_provider.requests
    assert first.headers[HEADER_CHAT_ID] == "header-chat"
    assert first.headers[HEADER_THREAD_ID] == first_message
    assert first.headers[HEADER_USER_ID] == "call-user"
    assert HEADER_ORGANIZATION_ID not in first.headers
    assert second.headers[HEADER_CHAT_ID] == second_chat
    assert second.headers[HEADER_THREAD_ID] == result.data["message_id"]
    assert HEADER_USER_ID not in second.headers
    assert HEADER_ORGANIZATION_ID not in second.headers
    assert first.headers[HEADER_REQUEST_ID] != second.headers[HEADER_REQUEST_ID]
    assert all(request.headers[HEADER_OPERATION] == "Answer" for request in (first, second))
    assert all(request.headers[HEADER_SERVICE_NAME] == "antikorr" for request in (first, second))


async def test_model_request_matches_response_message(chat_provider) -> None:
    """Запрос модели и ответ инструмента относятся к одному сообщению."""
    chat = str(uuid4())
    async with Client(server.mcp) as client:
        result = await client.call_tool("antikorr_invoke", {"data": {"thread_id": chat}})
    (request,) = chat_provider.requests
    assert request.headers[HEADER_CHAT_ID] == chat
    assert request.headers[HEADER_THREAD_ID] == result.data["message_id"]
    assert HEADER_USER_ID not in request.headers


@pytest.mark.parametrize("failed", [False, True])
async def test_generation_logs_safe_actual_contract_and_request_ids(chat_provider, monkeypatch, failed):
    records = []
    monkeypatch.setattr(
        llm_client, "log", SimpleNamespace(info=lambda event, **fields: records.append({"event": event, **fields}))
    )
    if failed:
        chat_provider.replies = (httpx.Response(403, json={"error": {"message": "denied", "type": "permission_error"}}),)
    template = get_prompt("competence_parse")
    messages = template.compile(header="PRIVATE_AUDIT_DOCUMENT")
    kwargs = {
        "role": "competence_parse",
        "messages": messages,
        "schema": Answer,
        "prompt": describe_prompt(template),
        "retries": 0,
    }
    if failed:
        with pytest.raises(LLMGenerationError):
            await llm_client.llm.generate_structured(**kwargs)
    else:
        assert (await llm_client.llm.generate_structured(**kwargs)).value.ok
    (record,) = records
    assert record["status"] == ("error" if failed else "ok")
    assert record["prompt"]["name"] == "competence_parse"
    assert record["prompt"]["is_fallback"] is True
    assert record["request_ids"] == [chat_provider.requests[0].headers["X-Request-ID"]]
    assert all(len(record[key]) == 64 for key in ("schema_sha256", "messages_sha256", "config_sha256"))
    assert "PRIVATE_AUDIT_DOCUMENT" not in json.dumps(records)
    assert "test-router-ai-key" not in json.dumps(records)
