"""The public MCP tool, real intake/renderer and S3 client, offline HTTP transport."""

from __future__ import annotations

import json
from io import BytesIO

import httpx
import pytest
from docx import Document
from tenacity import stop_after_attempt, wait_none

from src.agents.schemas.common.review import Review
from src.agents.utils.streaming import (
    FRAGMENT_PREPARE_DOCUMENT,
    FRAGMENT_REVIEW_SUMMARY,
)
from src.app.api.utils import document_text, pipeline
from src.app.mcp import report_delivery, runner
from src.common.schemas.models import MessageFragment
from src.infrastructure.s3_services.async_client import S3ServiceAsyncClient

THREAD = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
MID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
FILE = "11111111-1111-1111-1111-111111111111"
REPORT = "22222222-2222-2222-2222-222222222222"
SUMMARY = "Проверка выполнена частично. Дата экспертизы: 2026-10-09."
PROJECT = "ПРИКАЗ\n1. Утвердить Порядок.\n2. Установить срок 10 дней."
SECRET = "private-upstream-token-and-traceback"


@pytest.fixture
async def wire(monkeypatch):
    scenario = {
        "filename": "Проект.docx",
        "text": PROJECT,
        "size": 20,
        "download": [200],
        "meta": 200,
        "upload": 200,
        "requests": [],
        "runs": 0,
        "state": None,
        "documents": [],
    }

    async def handler(request):
        scenario["requests"].append(request)
        if request.url.path.startswith("/s3/meta/"):
            if scenario["meta"] != 200:
                return httpx.Response(scenario["meta"], text=SECRET)
            return httpx.Response(
                200,
                json={
                    "file_id": FILE,
                    "bucket_name": "documents",
                    "original_filename": scenario["filename"],
                    "storage_key": "unused",
                    "size_bytes": scenario["size"],
                    "content_type": "application/octet-stream",
                },
            )
        if request.url.path.startswith("/s3/download/"):
            assert request.url.path == f"/s3/download/{FILE}/anonymized"
            status = scenario["download"][0]
            if len(scenario["download"]) > 1:
                scenario["download"].pop(0)
            if status == "timeout":
                raise httpx.ReadTimeout(SECRET, request=request)
            if status == "transport":
                raise httpx.ConnectError(SECRET, request=request)
            return httpx.Response(status, text=scenario["text"] if status == 200 else SECRET)
        assert request.method == "POST" and request.url.path.startswith("/s3/upload/antikorr/reports/")
        assert request.url.params["kind"] == "anonymized"
        assert request.url.params["chat_id"] == THREAD
        scenario["documents"].append(request.content)
        if scenario["upload"] != 200:
            return httpx.Response(scenario["upload"], text=SECRET)
        return httpx.Response(
            200,
            json={
                "status": scenario.get("upload_status", "ok"),
                "file_id": scenario.get("report_id", REPORT),
                "storage_key": "result",
            },
        )

    async def graph(_app, state):
        scenario["runs"] += 1
        scenario["state"] = state
        yield MessageFragment(
            fragment_id=FRAGMENT_PREPARE_DOCUMENT, fragment_type="think", status="in_progress", content="Проверяю проект"
        )
        yield {"review": Review(findings=[], checks=[]), "summary": SUMMARY}
        yield MessageFragment(fragment_id=FRAGMENT_REVIEW_SUMMARY, fragment_type="response", status="success", content=SUMMARY)

    monkeypatch.setattr(pipeline, "mcp_is_enabled", lambda: False)
    monkeypatch.setattr(runner, "astream_events", graph)
    monkeypatch.setattr(S3ServiceAsyncClient.download_to_bytes.retry, "wait", wait_none())
    monkeypatch.setattr(S3ServiceAsyncClient.download_to_bytes.retry, "stop", stop_after_attempt(3))
    http = httpx.AsyncClient(base_url="https://s3.invalid", transport=httpx.MockTransport(handler))
    s3 = S3ServiceAsyncClient(base_url="https://s3.invalid", service_token="offline", client=http)
    monkeypatch.setattr(document_text, "s3_client", s3)
    monkeypatch.setattr(report_delivery, "s3_client", s3)
    yield scenario
    await http.aclose()


def terminal(payload, packets):
    envelopes = [p["data"] for p in packets if "data" in p]
    assert len(envelopes) == 1
    assert packets[-1] == {"data": envelopes[0]}
    envelope = envelopes[0]
    assert envelope["message_id"] == payload["message_id"] == MID
    assert envelope["status"] == payload["finish_reason"]
    assert sum(f["fragment_type"] == "response" for f in envelope["fragments"]) == 1
    response = envelope["fragments"][-1]
    assert response["content"] == payload["answer"]
    assert SECRET not in json.dumps([payload, packets], ensure_ascii=False)
    return response


async def invoke(call_review_tool):
    return await call_review_tool({"thread_id": THREAD, "message_id": MID, "file_ids": [FILE]})


async def test_report_is_uploaded_once_and_attached_to_final_answer(call_review_tool, wire):
    """Заключение загружается один раз и прикрепляется к итоговому ответу."""
    payload, packets = await invoke(call_review_tool)
    response = terminal(payload, packets)
    assert payload["finish_reason"] == "success" and payload["answer"] == SUMMARY
    assert response["file_id"] == REPORT
    assert wire["runs"] == 1 and len(wire["documents"]) == 1
    paragraphs = [p.text for p in Document(BytesIO(wire["documents"][0])).paragraphs]
    assert paragraphs[-1] == SUMMARY
    assert "Проект.docx" in paragraphs
    assert all(p.get("fragment_type") != "response" for p in packets[:-1])


@pytest.mark.parametrize(
    "status,needle",
    [
        (202, "ожидание"),
        (404, "Файл не найден"),
        (422, "Извлечение текста или обезличивание"),
        (503, "временно недоступен"),
        ("timeout", "временно недоступен"),
        ("transport", "временно недоступен"),
    ],
)
async def test_document_loading_error_is_explained_to_user(call_review_tool, wire, status, needle):
    """Ошибки загрузки объясняются пользователю без внутренних подробностей."""
    wire["download"] = [status]
    payload, packets = await invoke(call_review_tool)
    assert payload["finish_reason"] == "error" and needle in payload["answer"]
    assert terminal(payload, packets)["file_id"] is None
    assert wire["runs"] == 0 and not wire["documents"]
    assert "не выявлено" not in payload["answer"]
    if status == 202:
        assert len([r for r in wire["requests"] if "/download/" in r.url.path]) == 3
        assert "позднее" in payload["answer"]
    if status == 422:
        assert "документ является сканом" not in payload["answer"]


@pytest.mark.parametrize(
    "changes,needle",
    [
        ({"filename": "Проект.exe"}, "Неподдерживаемый формат"),
        ({"size": 10**12}, "Файл больше"),
        ({"text": "текст" * 100}, "Извлечённый текст больше"),
        ({"text": " \n\t"}, "нет текста"),
        (
            {"filename": "Пояснительная записка.docx", "text": "ПОЯСНИТЕЛЬНАЯ ЗАПИСКА\nОбоснование проекта."},
            "обязательный проект",
        ),
    ],
)
async def test_invalid_document_is_rejected_before_review(call_review_tool, wire, monkeypatch, changes, needle):
    """Недопустимый документ отклоняется до запуска экспертизы."""
    wire.update(changes)
    monkeypatch.setattr(document_text, "MAX_UPLOAD_BYTES", 100)
    payload, packets = await invoke(call_review_tool)
    assert payload["finish_reason"] == "error" and needle in payload["answer"]
    terminal(payload, packets)
    assert wire["runs"] == 0 and not wire["documents"]


@pytest.mark.parametrize("changes", [{"upload": 503}, {"upload_status": "pending"}, {"report_id": "bad-id"}])
async def test_failed_report_upload_preserves_text_answer(call_review_tool, wire, changes):
    """При сбое выдачи файла текст заключения остаётся доступным."""
    wire.update(changes)
    payload, packets = await invoke(call_review_tool)
    assert payload["finish_reason"] == "success"
    assert payload["answer"].startswith(SUMMARY)
    assert "доставить DOCX не удалось" in payload["answer"]
    assert terminal(payload, packets)["file_id"] is None
    assert wire["runs"] == 1


EXTRACTED_TEXT = (
    "Министерство социального развития Ульяновской области<br>"
    "ПРИКАЗ<br>"
    "Об утверждении Порядка<br>"
    "1. Утвердить прилагаемый Порядок.<br>"
    "Основание отказа<br>"
    "Непредставление документов<br>"
    "Срок рассмотрения<br>"
    "10 рабочих дней<br>"
    "Приложение № 1<br>"
    "2. Признать утратившим силу приказ от 01.01.2020 № 1-п."
)


async def test_loading_preserves_tables_and_appendices(call_review_tool, wire):
    """Обезличенный HTML-текст PDF сохраняет строки таблиц и приложения во входе графа."""
    wire.update(filename="Проект.pdf", text=EXTRACTED_TEXT)
    payload, packets = await invoke(call_review_tool)
    assert payload["finish_reason"] == "success"
    terminal(payload, packets)
    lines = wire["state"]["document_text"].splitlines()
    for expected in ("Основание отказа", "Непредставление документов", "Срок рассмотрения", "10 рабочих дней", "Приложение № 1"):
        assert expected in lines
