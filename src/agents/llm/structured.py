"""Одна попытка вызова провайдера, structured output и восстановление JSON."""

from __future__ import annotations

import contextlib
import json
from typing import Any, Literal

from pydantic import BaseModel

from src.agents.llm.errors import EmptyOutputError, OutputTruncationError
from src.agents.llm.usage import _record_usage_from_message
from src.infrastructure.llm.errors import ProviderFinishReasonError
from src.infrastructure.llm.metadata import _response_finish_reason
from src.infrastructure.llm.transport import provider_structured_schema, request_message, request_structured


def _strip_code_fence(text: str) -> str:
    """Снять markdown-обёртку ```json … ``` / ``` … ``` вокруг JSON (частый случай Kimi)."""
    t = text.strip()
    # Unwrap only the whole response; suffixes must reach the container scanner.
    if t.startswith("```") and t.endswith("```"):
        t = t[3:-3]
        if t[:4].lower() == "json":
            t = t[4:]
    return t.strip()


def _balanced_json_objects(text: str):
    """Scan whole containers; never expose an object nested in an array or string."""
    stack: list[str] = []
    start = 0
    in_str = False
    escaped = False
    for index, char in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char in "[{":
            if not stack:
                start = index
            stack.append(char)
        elif char in "]}":
            if not stack or stack.pop() != ("[" if char == "]" else "{"):
                raise ValueError("Mismatched JSON container")
            if not stack:
                candidate = text[start : index + 1]
                if candidate.startswith("["):
                    raise ValueError("Array is not a recoverable JSON object")
                yield candidate
    if stack or in_str:
        raise ValueError("Unfinished JSON container or string")


def _response_text(content: Any) -> str | None:
    "Текст ответа модели из ``AIMessage.content``: строка или список блоков."
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type", "text") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _extract_json_block(text: str) -> dict[str, Any]:
    text = _strip_code_fence(text)
    try:
        return json.loads(text)
    except Exception:
        pass

    objects = []
    for candidate in _balanced_json_objects(text):
        try:
            objects.append(json.loads(candidate))
        except ValueError:
            continue
    if len(objects) != 1:
        raise ValueError("Expected one unambiguous JSON object")
    return objects[0]


def _raw_output_was_truncated(raw: Any) -> bool:
    """Return true only for an explicit provider output-limit finish reason."""
    metadata = getattr(raw, "response_metadata", None) or {}
    additional = getattr(raw, "additional_kwargs", None) or {}
    candidates = [metadata, additional]
    for container in candidates:
        if not isinstance(container, dict):
            continue
        reason = container.get("finish_reason") or container.get("stop_reason")
        if isinstance(reason, str) and reason.casefold() in {"length", "max_tokens", "max_tokens_reached"}:
            return True
        choices = container.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict) and str(choice.get("finish_reason", "")).casefold() in {
                    "length",
                    "max_tokens",
                    "max_tokens_reached",
                }:
                    return True
    return False


async def _call_llm_structured_async_once[T: BaseModel](
    llm: Any,
    messages_or_prompt: Any,
    schema: type[T],
    *,
    structured_method: Literal["json_schema", "json_mode"] | None,
) -> T:
    if hasattr(llm, "with_structured_output"):
        result = await request_structured(
            llm,
            messages_or_prompt,
            provider_structured_schema(schema),
            method=structured_method,
            on_partial=_record_usage_from_message,
        )
        if isinstance(result, dict) and ("parsed" in result or "raw" in result):
            raw_message = result.get("raw")
            _record_usage_from_message(raw_message)
            finish_reason = _response_finish_reason(raw_message)
            if finish_reason and finish_reason.casefold() in {
                "error",
                "failed",
                "failure",
                "content_filter",
                "tool_calls",
                "function_call",
            }:
                raise ProviderFinishReasonError(finish_reason)
            if _raw_output_was_truncated(raw_message):
                raise OutputTruncationError("provider output limit reached") from None
            raw_content = _response_text(getattr(raw_message, "content", ""))
            if (raw_content is None or not raw_content.strip()) and result.get("parsed") is None:
                raise EmptyOutputError("empty structured output")
            err = result.get("parsing_error")
            if err:
                recovered = _recover_structured(raw_message, schema)
                if recovered is not None:
                    return recovered
                if raw_content is None or not raw_content.strip():
                    raise EmptyOutputError("empty structured output") from None
                # Preserve typed schema errors rather than a generic provider parser error.
                decoded = _extract_json_block(raw_content)
                return schema.model_validate(decoded)
            obj = result.get("parsed")
        else:
            obj = result
        if isinstance(obj, schema):
            return obj

        parsed = schema.model_validate(obj)
        return parsed

    bound = llm
    if hasattr(llm, "bind"):
        with contextlib.suppress(Exception):
            bound = llm.bind(response_format={"type": "json_object"})

    raw = await request_message(bound, messages_or_prompt)

    _record_usage_from_message(raw)  # единый источник usage (симметрично structured-ветке)
    finish_reason = _response_finish_reason(raw)
    if finish_reason and finish_reason.casefold() in {
        "error",
        "failed",
        "failure",
        "content_filter",
        "tool_calls",
        "function_call",
    }:
        raise ProviderFinishReasonError(finish_reason)

    if _raw_output_was_truncated(raw):
        raise OutputTruncationError("provider output limit reached") from None
    if isinstance(raw, dict):
        data = raw
    else:
        text = _response_text(getattr(raw, "content", raw if isinstance(raw, str) else str(raw)))
        if text is None or not text.strip():
            raise EmptyOutputError("empty structured output")
        try:
            data = json.loads(text)
        except Exception:
            data = _extract_json_block(text)

    parsed = schema.model_validate(data)
    return parsed


def _recover_structured[T: BaseModel](raw: Any, schema: type[T]) -> T | None:
    "Достать объект схемы из сырого ответа, когда парсер провайдера не справился."
    if raw is None or _raw_output_was_truncated(raw):
        return None
    text = _response_text(getattr(raw, "content", raw if isinstance(raw, str) else ""))
    if text is None or not text.strip():
        return None
    try:
        stripped = _strip_code_fence(text)
        try:
            data = json.loads(stripped)
        except (TypeError, ValueError):
            data = _extract_json_block(text)
        return schema.model_validate(data)
    except Exception:
        return None
