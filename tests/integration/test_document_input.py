from __future__ import annotations

import uuid

import pytest

from src.agents.schemas.common.review import Review
from src.agents.utils.streaming import (
    FRAGMENT_REVIEW_SUMMARY,
)
from src.app.api.utils import pipeline as pipeline_mod
from src.app.api.utils.intake import IntakeDocument, classify_intake
from src.app.mcp import runner as router_mod
from src.common.schemas.models import MessageFragment

PROJECT_TEXT = """ПРОЕКТ

Министерство социального развития Ульяновской области

ПРИКАЗ

Об утверждении административного регламента предоставления государственной услуги
«Назначение и предоставление ежемесячной денежной выплаты»

В соответствии с Законом Ульяновской области от 27.01.2012 № 3-ЗО «О дополнительных мерах
социальной поддержки работников противопожарной службы Ульяновской области»
п р и к а з ы в а ю:
1. Утвердить прилагаемый административный регламент предоставления государственной услуги.
2. Признать утратившим силу приказ Министерства социального развития Ульяновской области
от 04.03.2024 № 28-П «Об утверждении административного регламента».
3. Настоящий приказ вступает в силу на следующий день после дня его официального опубликования.
4. Заявителями являются граждане, состоящие в трудовых отношениях с противопожарной службой.
5. Срок предоставления государственной услуги составляет 10 рабочих дней.
6. Основанием для отказа в приёме документов является представление неполного комплекта документов.
"""

NOTE_TEXT = """ПОЯСНИТЕЛЬНАЯ ЗАПИСКА

Об утверждении административного регламента предоставления государственной услуги

Министерство социального развития Ульяновской области при разработке проекта
административного регламента руководствовалось следующими нормативными правовыми актами:
постановлением Правительства Ульяновской области от 14.07.2014 № 298-П «О разработке и
утверждении административных регламентов предоставления государственных услуг».
Проект административного регламента разработан в целях оптимизации порядка предоставления
государственной услуги.
Оценка регулирующего воздействия не требуется и не проводилась.
"""

#: Фраза, которой НЕТ в проекте и которая есть только в пояснительной записке.
NOTE_ONLY_QUOTE = "Оценка регулирующего воздействия не требуется и не проводилась."


def _docs(*pairs: tuple[str, str]) -> list[IntakeDocument]:
    return [IntakeDocument(name=n, text=t) for n, t in pairs]


@pytest.mark.parametrize("note_first", [False, True])
def test_project_is_selected_regardless_of_upload_order(note_first) -> None:
    """Проект и пояснительная записка различаются независимо от порядка загрузки."""
    docs = _docs(("Пояснительная записка.docx", NOTE_TEXT), ("0307 Проект.docx", PROJECT_TEXT))
    d = classify_intake(docs if note_first else list(reversed(docs)))
    assert d.subject.name == "0307 Проект.docx"
    assert d.note_names == ["Пояснительная записка.docx"]


def test_single_project_needs_no_explanatory_note() -> None:
    """Один проект принимается без пояснительной записки."""
    d = classify_intake(_docs(("Проект.docx", PROJECT_TEXT)))
    assert d.subject.name == "Проект.docx"
    assert d.notes == []
    assert d.message == ""
    assert d.confident is True


@pytest.fixture
def captured_state(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Клиент API, который доводит запрос до сборки состояния и НЕ запускает граф."""
    captured: dict = {}
    stored_documents: dict[uuid.UUID, tuple[str, str, int]] = {}

    async def fake_astream_events(_app, state):
        captured["state"] = state
        yield {"review": Review(findings=[], checks=[]), "summary": "Справка"}
        yield MessageFragment(fragment_id=FRAGMENT_REVIEW_SUMMARY, fragment_type="response", status="success", content="Справка")

    monkeypatch.setattr(router_mod, "astream_events", fake_astream_events)

    async def deliver_report(**kwargs):
        return "22222222-2222-2222-2222-222222222222"

    monkeypatch.setattr(router_mod, "deliver_report", deliver_report)

    monkeypatch.setattr(pipeline_mod, "mcp_is_enabled", lambda: False)
    monkeypatch.setattr(
        pipeline_mod,
        "extract_document_with_geometry_from_s3",
        lambda file_id, _thread_id=None: _stored_document(stored_documents, file_id),
    )
    captured["stored_documents"] = stored_documents
    return captured


async def _stored_document(
    stored_documents: dict[uuid.UUID, tuple[str, str, int]],
    file_id: uuid.UUID,
) -> tuple[str, str, int, dict | None]:
    return (*stored_documents[file_id], None)


async def _s3_request(call_review_tool, captured_state: dict, *documents: tuple[str, str]):
    file_ids = [uuid.uuid4() for _document in documents]
    captured_state["stored_documents"].update(
        {file_id: (name, text, len(text)) for file_id, (name, text) in zip(file_ids, documents, strict=True)}
    )
    return await call_review_tool({"thread_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "file_ids": [str(f) for f in file_ids]})


async def test_project_and_note_are_accepted_together(call_review_tool, captured_state: dict) -> None:
    """Проект и записка принимаются одним запросом."""
    _result, _packets = await _s3_request(
        call_review_tool,
        captured_state,
        ("Пояснительная записка.txt", NOTE_TEXT),
        ("0307 Проект.txt", PROJECT_TEXT),
    )

    assert _result["finish_reason"] == "success"
    state = captured_state["state"]
    # Предмет экспертизы — проект, несмотря на то что записка загружена ПЕРВОЙ.
    assert state["source_name"] == "0307 Проект.txt"
    assert "п р и к а з ы в а ю" in state["document_text"]
    # И главное: текста пояснительной в document_text нет.
    assert NOTE_ONLY_QUOTE not in state["document_text"]
    assert "ПОЯСНИТЕЛЬНАЯ ЗАПИСКА" not in state["document_text"]

    notes = state["context_documents"]
    assert [d["name"] for d in notes] == ["Пояснительная записка.txt"]
    assert notes[0]["kind"] == "explanatory_note"
    assert NOTE_ONLY_QUOTE in notes[0]["text"]


async def test_invalid_file_id_is_rejected(call_review_tool, captured_state: dict) -> None:
    """Некорректный идентификатор файла отклоняется."""
    result, _ = await call_review_tool({"thread_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "file_ids": ["not-a-uuid"]})
    assert result["finish_reason"] == "error"
    assert "UUID" in result["answer"]


async def test_excess_files_are_rejected(call_review_tool, captured_state: dict) -> None:
    """Загрузка сверх лимита отклоняется."""
    _result, _packets = await _s3_request(
        call_review_tool,
        captured_state,
        *((f"файл-{i}.txt", PROJECT_TEXT) for i in range(6)),
    )
    assert _result["finish_reason"] == "error"
    assert "не более" in _result["answer"]
