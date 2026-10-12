from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class HistoryMessageFragment(BaseModel):
    fragment_type: Literal["request", "think", "response"]
    content: str | None = Field(default=None, max_length=200_000)


class HistoryMessage(BaseModel):
    """Элемент истории диалога (контракт native-orchestrator)."""

    role: Literal["user", "assistant"]
    fragments: list[HistoryMessageFragment] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)


class AntikorrInvokeRequest(BaseModel):
    """Вход инструмента ``antikorr_invoke``."""

    thread_id: UUID = Field(description="UUID текущего чата; используется как chat_id при загрузке DOCX")
    history_messages: list[HistoryMessage] = Field(
        default_factory=list,
        description="История сообщений: из неё берётся проект, приложенный предыдущим сообщением",
    )
    file_ids: list[str] | None = Field(
        default=None,
        description="Файлы текущего сообщения в s3-service (UUID): проект НПА и, при наличии, пояснительная записка",
    )
    message_id: UUID | None = Field(
        default=None,
        description="Идентификатор сообщения (опционально; иначе генерируется)",
    )
