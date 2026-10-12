from __future__ import annotations

import time
import uuid
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class MessageFragment(BaseModel):
    fragment_id: int
    fragment_type: Literal["request", "think", "response"]
    status: Literal["error", "success", "in_progress"]

    outcome: Literal["found", "clear", "warn"] | None = None

    findings_shown: int | None = None
    content: str | None = None
    file_id: str | None = None  # S3 attachment on a successful response
    token_usage_complete: bool | None = None
    token_usage: int | None = None  # filled on completion
    duration_s: float | None = None  # filled on completion


class Message(BaseModel):
    message_id: UUID = Field(default_factory=uuid.uuid4)
    parent_id: UUID | None = None  # parent message_id

    role: Literal["user", "assistant"]
    status: Literal["error", "success", "in_progress"]

    fragments: list[MessageFragment] = Field(default_factory=list)

    created_at: float = Field(default_factory=time.time)
    updated_at: float | None = None

    thinking_duration_s: float | None = None  # filled on completion
    accumulated_token_usage_complete: bool | None = None
    accumulated_token_usage: int | None = None  # filled on completion
