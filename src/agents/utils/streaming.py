from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from typing import Any, Literal

from langgraph.config import get_stream_writer

from src.agents.llm.usage import current_usage_snapshot, sum_usage
from src.common.config.config import settings
from src.common.schemas.models import Message, MessageFragment


def progress_text(key: str) -> str:
    "Текст «пузыря» прогресса из конфига (``agents.progress.<key>``)."
    return settings.get(f"agents.progress.{key}", key)


FragmentType = Literal["request", "think", "response"]
FragmentStatus = Literal["error", "success", "in_progress"]


# ID — ключ обновления карточки и порядок отображения, независимо от порядка событий.
# Приём файлов занимает слот 0; параллельные проверки располагаются по направлению.
FRAGMENT_PREPARE_DOCUMENT = 1
FRAGMENT_CHECK_AUTHORITY = 2
FRAGMENT_CHECK_INTERNAL_CONSISTENCY = 3
# Общая карточка подграфа: plan_legal_search → retrieve_legal_sources → check_legal_compliance.
FRAGMENT_COMPLIANCE_REVIEW = 4
FRAGMENT_CHECK_INCOMPLETE_REPEAL = 5
FRAGMENT_BUILD_REPORT = 6
FRAGMENT_REVIEW_SUMMARY = 7


def _normalize_thought_markdown(content: str) -> str:
    text = content.replace("\r\n", "\n").strip()
    text = re.sub(r"(?m)^•\s+", "- ", text)
    text = re.sub(r"(\*\*[^*\n]+:\*\*)\n(?!\n)", r"\1\n\n", text)
    text = re.sub(r"(\*\*[^*\n]+:\*\*)\n(- )", r"\1\n\n\2", text)
    text = re.sub(r"(\*\*[^*\n]+\*\*)\n(?!\n)", r"\1\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _upsert_fragment(message: Message, fragment: MessageFragment) -> None:
    for idx, existing in enumerate(message.fragments):
        if existing.fragment_id == fragment.fragment_id:
            message.fragments[idx] = fragment
            return
    message.fragments.append(fragment)


def emit_ui_event(
    state: Mapping[str, Any],
    step: str,
    fragment_id: int,
    *,
    fragment_type: FragmentType = "think",
    status: FragmentStatus,
    content: str,
    outcome: Literal["found", "clear", "warn"] | None = None,
    findings_shown: int | None = None,
    token_usage: int | None = None,
    duration_s: float | None = None,
    token_usage_complete: bool | None = None,
) -> MessageFragment:
    "Эмит одного UI-фрагмента (``think``-прогресс / ``response``-контент) во фронт."
    if token_usage is None and status != "in_progress":
        current = current_usage_snapshot()
        previous = state.get("legal_stage_usage", {}) if fragment_id == FRAGMENT_COMPLIANCE_REVIEW else {}
        snapshots = [*previous.values(), current]
        combined = sum_usage([s.usage for s in snapshots if s.usage is not None])
        token_usage = combined.total_tokens if combined is not None else None
        if token_usage_complete is None:
            token_usage_complete = all(s.complete for s in snapshots)
    if fragment_type == "think" and status == "success" and content:
        content = _normalize_thought_markdown(content)
    frag = MessageFragment(
        fragment_id=fragment_id,
        fragment_type=fragment_type,
        status=status,
        outcome=outcome,
        findings_shown=findings_shown,
        content=content,
        token_usage=token_usage,
        token_usage_complete=token_usage_complete,
        duration_s=duration_s,
    )
    output_message = state.get("output_message")
    if output_message is not None:
        _upsert_fragment(output_message, frag)
        # accumulated_token_usage = сумма по фрагментам (как в k-agent)
        known = [f.token_usage for f in output_message.fragments if f.token_usage is not None]
        output_message.accumulated_token_usage = sum(known) if known else None
        terminal = [f for f in output_message.fragments if f.fragment_type == "think" and f.status != "in_progress"]
        output_message.accumulated_token_usage_complete = (
            all(f.token_usage_complete is True for f in terminal) if terminal else None
        )
    with contextlib.suppress(Exception):
        get_stream_writer()({step: {"events": [frag]}})
    return frag
