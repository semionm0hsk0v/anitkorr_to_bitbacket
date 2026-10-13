from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client

import src.app.mcp.runner as mcp_runner
from src.agents.schemas.common.review import Review
from src.agents.utils.streaming import (
    FRAGMENT_BUILD_REPORT,
    FRAGMENT_COMPLIANCE_REVIEW,
    FRAGMENT_PREPARE_DOCUMENT,
    FRAGMENT_REVIEW_SUMMARY,
)
from src.app.api.utils import pipeline
from src.app.api.utils.intake import IntakeDocument
from src.app.mcp import mcp
from src.common.schemas.models import MessageFragment

_PROJECT_ID = "11111111-1111-1111-1111-111111111111"
_NOTE_ID = "22222222-2222-2222-2222-222222222222"

_PROJECT_TEXT = (
    "ПРИКАЗ\n"
    "О внесении изменений в административный регламент\n"
    "1. Утвердить прилагаемые изменения.\n"
    "2. Установить срок рассмотрения заявления 30 дней.\n"
    "3. Признать утратившим силу приказ от 12.03.2020 № 17.\n"
    "4. Контроль возложить на заместителя руководителя.\n"
    "5. Настоящий приказ вступает в силу со дня его официального опубликования.\n"
)
_NOTE_TEXT = "Пояснительная записка\nПроект подготовлен в целях приведения регламента в соответствие.\n"

_CARD = (
    "**Выявлено 1 несоответствие вышестоящему акту**\n\n"
    "---\n\n"
    "**1. Пункт 2 проекта — несоответствие вышестоящему акту**\n\n"
    "> «Установить срок рассмотрения заявления 30 дней.»\n\n"
    "Против: часть 1 статьи 12 Федерального закона от 27.07.2010 № 210-ФЗ — 15 рабочих дней."
)
_CLEAN_NOTE = (
    "Проверка завершена. Противоречий, требующих отражения в заключении, не выявлено: "
    "проверены внутренние противоречия проекта и его соответствие вышестоящим актам."
)


@pytest.fixture(autouse=True)
def _offline_consultant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сессии Консультант+ не открываются: тест не ходит во внешний MCP-клиент."""
    monkeypatch.setattr(pipeline, "mcp_is_enabled", lambda: False)

    async def deliver_report(**kwargs):
        return _NOTE_ID

    monkeypatch.setattr(mcp_runner, "deliver_report", deliver_report)


def _documents(*, with_note: bool = False) -> list[IntakeDocument]:
    docs = [IntakeDocument(name="Проект приказа.docx", text=_PROJECT_TEXT, source_document_id=_PROJECT_ID)]
    if with_note:
        docs.append(IntakeDocument(name="Пояснительная записка.docx", text=_NOTE_TEXT, source_document_id=_NOTE_ID))
    return docs


def _patch_documents(monkeypatch: pytest.MonkeyPatch, *, with_note: bool = False) -> list[list[Any]]:
    """Подменяет чтение из s3-service и запоминает, с чем его позвали."""
    calls: list[list[Any]] = []

    async def fake_read_documents(file_ids: list[Any], thread_id: str) -> list[IntakeDocument]:
        calls.append([str(file_id) for file_id in file_ids])
        return _documents(with_note=with_note)

    monkeypatch.setattr(mcp_runner, "read_documents", fake_read_documents)
    return calls


def _patch_graph(monkeypatch: pytest.MonkeyPatch, events: list[Any]) -> None:
    """Подменяет прогон графа заранее заданной лентой событий."""

    async def fake_stream(_app: Any, _state: Any) -> AsyncIterator[Any]:
        for event in events:
            yield event

    monkeypatch.setattr(mcp_runner, "astream_events", fake_stream)


def _found_stream() -> list[Any]:
    return [
        MessageFragment(
            fragment_id=FRAGMENT_PREPARE_DOCUMENT, fragment_type="think", status="in_progress", content="Разбираю документ"
        ),
        MessageFragment(
            fragment_id=FRAGMENT_PREPARE_DOCUMENT, fragment_type="think", status="success", content="Разобрано 5 норм проекта"
        ),
        MessageFragment(
            fragment_id=FRAGMENT_COMPLIANCE_REVIEW,
            fragment_type="think",
            status="success",
            outcome="found",
            findings_shown=1,
            content=_CARD,
        ),
        MessageFragment(
            fragment_id=FRAGMENT_BUILD_REPORT,
            fragment_type="think",
            status="success",
            outcome="clear",
            content="Заключение сформировано",
        ),
        {"review": Review(findings=[], checks=[]), "summary": "Справка"},
        MessageFragment(fragment_id=FRAGMENT_REVIEW_SUMMARY, fragment_type="response", status="success", content="Справка"),
    ]


def _terminal(packets: list[dict[str, Any]]) -> dict[str, Any]:
    envelopes = [p["data"] for p in packets if isinstance(p.get("data"), dict)]
    assert envelopes, "терминальный конверт обязателен: без него оркестратор считает вызов упавшим"
    return envelopes[-1]


async def test_review_tool_is_available() -> None:
    """Клиент может обнаружить инструмент экспертизы и его описание."""
    async with Client(mcp) as client:
        tools = {tool.name for tool in await client.list_tools()}
        resources = {str(resource.uri) for resource in await client.list_resources()}
        card = await client.read_resource("skill://antikorr/SKILL.md")

    assert "antikorr_invoke" in tools
    assert "skill://antikorr/SKILL.md" in resources
    text = card[0].text  # type: ignore[union-attr]

    assert "tool_name: antikorr_invoke" in text
    assert "mode: entrypoint" in text
    assert "name: antikorr" in text


async def test_confirmed_findings_reach_user(call_review_tool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Подтверждённые нарушения доходят до пользователя."""
    calls = _patch_documents(monkeypatch, with_note=True)
    _patch_graph(monkeypatch, _found_stream())

    payload, packets = await call_review_tool(
        {
            "thread_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "file_ids": [_PROJECT_ID, _NOTE_ID],
            "message_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        }
    )

    assert payload["finish_reason"] == "success"
    assert payload["thread_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert payload["answer"] == ""
    assert calls == [[_PROJECT_ID, _NOTE_ID]]

    assert packets[0]["role"] == "assistant" and packets[0]["fragments"] == []  # скелет сообщения
    progress = [p for p in packets if "fragment_id" in p]
    assert [p["content"] for p in progress if p["status"] == "in_progress"] == ["Разбираю документ"]

    assert "Предмет экспертизы — «Проект приказа.docx»" in progress[0]["content"]

    assert all(_CARD not in (p.get("content") or "") for p in progress)

    terminal = _terminal(packets)
    assert terminal["status"] == "success"
    assert terminal["fragments"][-1]["fragment_type"] == "response"
    assert terminal["fragments"][-1]["content"] == ""
    assert terminal["fragments"][-1]["file_id"] == _NOTE_ID
    assert _CARD not in json.dumps([payload, packets], ensure_ascii=False)


async def test_project_without_findings_gets_report_file(call_review_tool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Проект без находок также получает файл, без текста заключения в чате."""
    _patch_documents(monkeypatch)
    _patch_graph(
        monkeypatch,
        [
            MessageFragment(
                fragment_id=FRAGMENT_PREPARE_DOCUMENT, fragment_type="think", status="in_progress", content="Разбираю документ"
            ),
            {"review": Review(findings=[], checks=[]), "summary": _CLEAN_NOTE},
            MessageFragment(fragment_id=FRAGMENT_REVIEW_SUMMARY, fragment_type="response", status="success", content=_CLEAN_NOTE),
        ],
    )

    payload, packets = await call_review_tool({"thread_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "file_ids": [_PROJECT_ID]})

    assert payload["finish_reason"] == "success"
    assert payload["answer"] == ""
    assert _terminal(packets)["status"] == "success"


async def test_missing_project_prompts_for_file(call_review_tool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Без проекта инструмент просит прикрепить файл."""
    calls = _patch_documents(monkeypatch)

    payload, packets = await call_review_tool({"thread_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"})

    assert payload["finish_reason"] == "error"
    assert "приложите файл проекта" in payload["answer"].lower()
    assert calls == []  # до s3-service дело не дошло
    assert _terminal(packets)["status"] == "error"


async def test_service_failure_is_not_reported_as_clean_project(call_review_tool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Технический сбой не превращается в ответ об отсутствии нарушений."""
    _patch_documents(monkeypatch)

    async def boom(_app: Any, _state: Any) -> AsyncIterator[Any]:
        raise RuntimeError("upstream failed")
        yield  # pragma: no cover - недостижимо, нужен только для async-генератора

    monkeypatch.setattr(mcp_runner, "astream_events", boom)

    payload, packets = await call_review_tool({"thread_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "file_ids": [_PROJECT_ID]})

    assert payload["finish_reason"] == "error"
    assert "не выявлено" not in payload["answer"]
    assert "непроверенным" in payload["answer"]
    assert "upstream failed" not in json.dumps([payload, *packets], ensure_ascii=False)
    assert _terminal(packets)["status"] == "error"
