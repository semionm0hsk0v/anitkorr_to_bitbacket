from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import httpx

from src.agents import antikorr_app
from src.agents.graph import astream_events
from src.agents.state import PipelineState
from src.agents.utils.initial_state import assemble_initial_state
from src.agents.utils.streaming import FRAGMENT_REVIEW_SUMMARY
from src.app.api.utils.intake import classify_intake
from src.app.api.utils.pipeline import (
    UNREADABLE_FILE_ANSWER,
    build_user_message,
    consultant_sessions,
    context_documents,
    intake_fragment,
    read_documents,
)
from src.app.mcp.report_delivery import deliver_report
from src.app.mcp.schemas import HistoryMessage
from src.common import IntakeError
from src.common.schemas.models import Message, MessageFragment
from src.common.utils.logger import get_logger
from src.infrastructure.s3_services.async_client import (
    AnonymizingFileIsNotReadyError,
    S3FileNotFoundError,
    S3ServiceClientError,
)

log = get_logger(__name__)


StreamCallback = Callable[[dict[str, Any]], Awaitable[None]]

_NO_FILE_ANSWER = (
    "Нужен проект НПА: приложите файл проекта в формате .docx, .pdf или .txt с текстовым слоем. "
    "При необходимости приложите вместе с ним пояснительную записку."
)
_FAILED_ANSWER = (
    "Проверку завершить не удалось из-за технической ошибки. Повторите запрос позже. "
    "Это не вывод об отсутствии нарушений: проект остался непроверенным."
)
_FALLBACK_ANSWER = (
    "Проверка завершена, но сформировать заключение не удалось. Повторите запрос; если повторится — сообщите администратору."
)


def resolve_file_ids(history_messages: list[HistoryMessage], file_ids: list[str] | None) -> list[str]:
    "Файлы текущего сообщения, иначе — файлы ПОСЛЕДНЕГО сообщения пользователя с вложением."
    current = [str(file_id) for file_id in file_ids or [] if str(file_id).strip()]
    if current:
        return list(dict.fromkeys(current))
    for message in reversed(history_messages):
        if message.role != "user" or not message.file_ids:
            continue
        previous = [str(file_id) for file_id in message.file_ids if str(file_id).strip()]
        if previous:
            return list(dict.fromkeys(previous))
    return []


def _parse_file_ids(values: list[str]) -> list[UUID]:
    """Идентификаторы s3-service. Нечитаемый — ошибка вложения, а не сбой сервиса."""
    parsed: list[UUID] = []
    for value in values:
        try:
            parsed.append(UUID(value))
        except (ValueError, AttributeError, TypeError):
            raise IntakeError("Неверный UUID вложения. Загрузите файл проекта заново и повторите запрос.")
    return parsed


async def run_expertise_stream(
    *,
    thread_id: str,
    history_messages: list[HistoryMessage] | None = None,
    file_ids: list[str] | None = None,
    message_id: str | None = None,
    on_event: StreamCallback | None = None,
) -> dict[str, Any]:
    "Провести экспертизу, стримить прогресс пакетами и вернуть ОДИН ответ пользователю."
    history = history_messages or []
    mid = message_id or str(uuid.uuid4())
    output_message = Message(message_id=UUID(mid), role="assistant", status="in_progress")
    streamed: list[MessageFragment] = []

    async def emit(fragment: MessageFragment) -> None:
        streamed.append(fragment)
        if on_event is not None:
            await on_event(fragment.model_dump(mode="json"))

    async def finish(*, status: str, answer: str, file_id: str | None = None) -> dict[str, Any]:
        """Терминальный конверт ``data`` + ответ инструмента (контракт оркестратора)."""
        output_message.status = "success" if status == "success" else "error"
        output_message.updated_at = time.time()
        output_message.thinking_duration_s = max(0.0, output_message.updated_at - output_message.created_at)
        output_message.fragments = [
            *streamed,
            MessageFragment(
                fragment_id=FRAGMENT_REVIEW_SUMMARY,
                fragment_type="response",
                status=output_message.status,
                content=answer,
                file_id=file_id,
            ),
        ]
        if on_event is not None:
            await on_event({"data": output_message.model_dump(mode="json")})
        return {"answer": answer, "thread_id": thread_id, "message_id": mid, "finish_reason": status}

    if on_event is not None:
        await on_event(output_message.model_dump(mode="json"))  # скелет сообщения

    requested = resolve_file_ids(history, file_ids)
    if not requested:
        return await finish(status="error", answer=_NO_FILE_ANSWER)

    try:
        docs = await read_documents(_parse_file_ids(requested), thread_id)
        decision = classify_intake(docs)
    except AnonymizingFileIsNotReadyError:
        return await finish(
            status="error",
            answer="Документ ещё обрабатывается: ожидание извлечения и обезличивания исчерпано. Повторите запрос позднее.",
        )
    except S3FileNotFoundError:
        return await finish(status="error", answer="Файл не найден. Загрузите проект заново и повторите запрос.")
    except (S3ServiceClientError, httpx.RequestError) as exc:
        answer = (
            "Извлечение текста или обезличивание документа не удалось. Загрузите документ заново; "
            "проверьте, что текст выделяется и копируется. Контур не выполняет OCR сканов. "
            "Если ошибка повторится, обратитесь к администратору."
            if isinstance(exc, S3ServiceClientError) and exc.status_code == 422
            else "Сервис документов временно недоступен. Повторите запрос позднее. Проект остался непроверенным."
        )
        return await finish(status="error", answer=answer)
    except IntakeError as exc:
        # Текст этой ошибки написан для пользователя (см. common/errors.py).
        await log.ainfo("mcp_intake_rejected", thread_id=thread_id, files=len(requested))
        return await finish(status="error", answer=str(exc))
    except Exception:
        await log.aexception("mcp_intake_failed", thread_id=thread_id, files=len(requested))
        return await finish(status="error", answer=UNREADABLE_FILE_ANSWER)

    if (composition := intake_fragment(decision)) is not None:
        await emit(composition)

    user_message = build_user_message("", [document.name for document in docs])
    initial_state: PipelineState = assemble_initial_state(
        source_name=decision.subject.name,
        document_text=decision.subject.text,
        context_documents=context_documents(decision),
        table_geometry=decision.subject.table_geometry,
        extra={
            "source_document_id": decision.subject.source_document_id,
            "thread_id": thread_id,
            "message_id": mid,
            "input_message": user_message,
            "output_message": output_message,
        },
    )

    try:
        found, terminal_note, report = await _collect_review(initial_state, emit)
    except Exception:
        await log.aexception("mcp_expertise_failed", thread_id=thread_id, message_id=mid)
        return await finish(status="error", answer=_FAILED_ANSWER)

    answer = _assemble_answer([fragment.content or "" for fragment in found], terminal_note)
    if report is None or not terminal_note or answer == _FALLBACK_ANSWER:
        return await finish(status="error", answer=_FALLBACK_ANSWER)
    try:
        report_file_id = await deliver_report(
            review=report["review"],
            summary=report["summary"],
            source_name=decision.subject.name,
            analysis_date=initial_state["analysis_date"],
            thread_id=thread_id,
        )
    except Exception:
        await log.awarning("mcp_report_delivery_failed", thread_id=thread_id, message_id=mid)
        return await finish(
            status="success",
            answer=answer + "\n\nТекст заключения готов, но доставить DOCX не удалось. Файл недоступен. "
            "Повторите запрос позднее для получения DOCX.",
        )
    await log.ainfo(
        "mcp_expertise_done",
        thread_id=thread_id,
        message_id=mid,
        findings_blocks=len(found),
        findings_cards=sum(fragment.findings_shown or 0 for fragment in found),
        answer_chars=len(answer),
    )
    return await finish(status="success", answer=answer, file_id=report_file_id)


async def _collect_review(initial_state: PipelineState, emit: Callable[[MessageFragment], Awaitable[None]]):
    found: list[MessageFragment] = []
    terminal_note: str | None = None
    report: dict[str, Any] | None = None
    async with consultant_sessions(initial_state) as state:
        async for event in astream_events(antikorr_app, state):
            if isinstance(event, dict) and "review" in event and "summary" in event:
                report = event
            if not isinstance(event, MessageFragment):
                continue
            if event.fragment_type == "response":
                terminal_note = event.content
            elif _carries_findings(event):
                found.append(event)
            elif event.content:
                await emit(event)
    return found, terminal_note, report


def _carries_findings(fragment: MessageFragment) -> bool:
    "Блок детектора несёт карточки разбора → его место в ответе, а не в ленте прогресса."
    return bool(fragment.findings_shown) and bool(fragment.content)


def _assemble_answer(found_blocks: list[str], terminal_note: str | None) -> str:
    "Ответ пользователю: блоки находок и за ними справка (дата и статусы проверок)."
    blocks = [block.strip() for block in found_blocks if block and block.strip()]
    if blocks:
        if terminal_note and terminal_note.strip():
            blocks.append(terminal_note.strip())
        return "\n\n".join(blocks)
    if terminal_note and terminal_note.strip():
        return terminal_note.strip()
    return _FALLBACK_ANSWER
