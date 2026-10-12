from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from src.agents.schemas.common.context_document import ContextDocument
from src.agents.state import PipelineState
from src.app.api.utils.document_text import extract_document_with_geometry_from_s3
from src.app.api.utils.intake import MAX_INTAKE_FILES, IntakeDecision, IntakeDocument
from src.common import IntakeError
from src.common.schemas.models import Message, MessageFragment
from src.infrastructure.mcp_servers.client import mcp_client
from src.infrastructure.mcp_servers.dependencies import McpRequestSessions
from src.infrastructure.mcp_servers.lifecycle import mcp_is_enabled

FRAGMENT_INTAKE = 0


UNREADABLE_FILE_ANSWER = (
    "Не удалось прочитать вложение. Приложите проект НПА ещё раз файлом .docx, .pdf или .txt "
    "с текстовым слоем: фотография документа и скан без текстового слоя на этом маршруте не читаются."
)


def validate_intake_count(count: int) -> None:
    if count == 0:
        raise IntakeError("Передайте file_id объекта в S3-хранилище.")
    if count > MAX_INTAKE_FILES:
        raise IntakeError(
            f"За одну экспертизу можно загрузить не более {MAX_INTAKE_FILES} файлов: проект НПА и пояснительную записку к нему."
        )


async def read_documents(file_ids: list[UUID], thread_id: str) -> list[IntakeDocument]:
    "Документы приходят ТОЛЬКО из s3-service по ``file_id``."
    validate_intake_count(len(file_ids))
    if len(file_ids) != len(set(file_ids)):
        raise IntakeError("file_id не должен повторяться в одном запросе.")
    stored_documents = await asyncio.gather(*(extract_document_with_geometry_from_s3(file_id, thread_id) for file_id in file_ids))
    return [
        IntakeDocument(
            name=Path(filename or "document.txt").name,
            text=text,
            source_document_id=str(file_id),
            table_geometry=geometry,
        )
        for file_id, (filename, text, _char_count, geometry) in zip(file_ids, stored_documents, strict=True)
    ]


def context_documents(decision: IntakeDecision) -> list[ContextDocument]:
    "Пояснения → ОТДЕЛЬНЫЙ канал состояния (не ``document_text``)."
    return [
        ContextDocument(
            name=d.name,
            kind="explanatory_note",
            text=d.text,
            source_document_id=d.source_document_id,
        )
        for d in decision.notes
    ]


def build_user_message(message: str, filenames: list[str]) -> Message:
    fragments: list[MessageFragment] = []
    if message:
        fragments.append(
            MessageFragment(
                fragment_id=1,
                fragment_type="request",
                content=message,
                status="success",
            )
        )
    for name in filenames or ["document"]:
        fragments.append(
            MessageFragment(
                fragment_id=len(fragments) + 1,
                fragment_type="request",
                content=name or "document",
                status="success",
            )
        )
    return Message(parent_id=None, role="user", status="success", fragments=fragments)


def intake_fragment(decision: IntakeDecision) -> MessageFragment | None:
    "Строка «состав загрузки» в ленте «Размышлений» — что чем распознано."
    if not decision.message:
        return None
    return MessageFragment(
        fragment_id=FRAGMENT_INTAKE,
        fragment_type="think",
        status="success",
        outcome="clear" if decision.confident else "warn",
        content=decision.message,
    )


@asynccontextmanager
async def consultant_sessions(initial_state: PipelineState) -> AsyncIterator[PipelineState]:
    "Request-scoped сессии Консультант+ поверх состояния, если MCP включён."
    if not mcp_is_enabled():
        yield initial_state
        return
    async with McpRequestSessions(mcp_client) as rs:
        await rs.add_server("consultant")
        yield {**initial_state, "mcp_request_sessions": rs}
