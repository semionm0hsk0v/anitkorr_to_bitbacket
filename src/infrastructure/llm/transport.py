"""Одна отправка запроса или чтение потока; без повторов и разбора результата агента."""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _convert_to_openai_response_format
from openai import LengthFinishReasonError
from openai.lib._parsing._completions import type_to_response_format_param
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel

from src.common.utils.logger import get_logger
from src.infrastructure.llm.errors import ProviderProtocolError, check_response_error
from src.infrastructure.llm.metadata import completion_metadata, stream_finished

log = get_logger(__name__)


class IncompleteStreamError(ConnectionError):
    """EOF without a terminal finish reason is not a completed generation."""

    kind = "incomplete_stream"


def merge_chunks(raw: Any, chunk: Any) -> Any:
    if raw is None:
        return chunk
    merged = raw + chunk
    merged.response_metadata = {**raw.response_metadata, **chunk.response_metadata}
    return merged


async def _close_stream(stream: Any) -> None:
    original = sys.exception()
    try:
        if hasattr(stream, "aclose"):
            await stream.aclose()
        elif hasattr(stream, "close"):
            await stream.close()
    except BaseException as close_error:
        if original is None:
            raise
        log.warning("llm_stream_close_failed", error_type=type(close_error).__name__)


def _response_choice(data: dict[str, Any], *, streaming: bool) -> dict[str, Any] | None:
    """Check errors/structure only after upper-level metadata have been recorded."""
    check_response_error(data)
    choices = data.get("choices")
    # Some providers omit choices on usage-only SSE frames; JSON completions
    # still require a message, and explicit null/wrong types are malformed.
    if streaming and ("choices" not in data or choices == []) and isinstance(data.get("usage"), dict):
        return None
    if not isinstance(choices, list) or not choices or any(not isinstance(choice, dict) for choice in choices):
        raise ProviderProtocolError()
    choice = choices[0]
    if not isinstance(choice.get("delta" if streaming else "message"), dict):
        raise ProviderProtocolError()
    if choice.get("finish_reason") is not None and not isinstance(choice["finish_reason"], str):
        raise ProviderProtocolError()
    return choice


async def _sdk_stream(
    model: Any, messages: Any, *, on_partial: Callable[[Any], None] | None = None, **kwargs: Any
) -> AsyncGenerator[Any]:
    payload = model._get_request_payload(messages, **kwargs)
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    stream = await model.async_client.create(**payload)
    metadata: dict[str, Any] = {}
    try:
        # Use the SDK SSE decoder before its typed iterator discards the outer
        # envelope of error frames (including received usage/model/provider).
        async for event in stream._iter_events():
            if event.data.startswith("[DONE]"):
                break
            data = event.json()
            if not isinstance(data, dict):
                raise ProviderProtocolError()
            metadata.update(completion_metadata(data))
            if on_partial is not None:
                on_partial(SimpleNamespace(response_metadata=dict(metadata)))
            choice = _response_choice(data, streaming=True)
            if choice is not None and choice.get("finish_reason") is not None:
                metadata["finish_reason"] = choice["finish_reason"]
                if on_partial is not None:
                    on_partial(SimpleNamespace(response_metadata=dict(metadata)))
            content = (choice["delta"].get("content") or "") if choice is not None else ""
            yield AIMessageChunk(content=content, response_metadata=dict(metadata))
    finally:
        await _close_stream(stream)


async def _sdk_message(model: Any, messages: Any, *, on_partial: Callable[[Any], None] | None = None, **kwargs: Any) -> Any:
    payload = model._get_request_payload(messages, **kwargs)
    completion = await model.async_client.create(**payload)
    data = completion.model_dump(exclude_unset=True, warnings=False)
    metadata = {"source_usage": data.get("usage"), "source_model": data.get("model"), **completion_metadata(data)}
    if on_partial is not None:
        on_partial(SimpleNamespace(response_metadata=metadata))
    choice = _response_choice(data, streaming=False)
    if choice is not None and choice.get("finish_reason") is not None:
        metadata["finish_reason"] = choice["finish_reason"]
        if on_partial is not None:
            on_partial(SimpleNamespace(response_metadata=metadata))
    result = model._create_chat_result(data)
    generation = result.generations[0]
    raw = generation.message
    raw.response_metadata = {**(result.llm_output or {}), **(generation.generation_info or {}), **metadata}
    return raw


async def request_message(model: Any, messages: Any, *, on_partial: Callable[[Any], None] | None = None) -> Any:
    if isinstance(model, ChatOpenAI):
        return await _sdk_message(model, messages, on_partial=on_partial)
    raw = await model.ainvoke(messages)
    if on_partial is not None:
        on_partial(raw)
    return raw


async def request_structured(
    model: Any,
    messages: Any,
    schema: Any,
    *,
    method: str | None = None,
    on_partial: Callable[[Any], None] | None = None,
) -> Any:
    if isinstance(model, ChatOpenAI):
        # Call create, not SDK parse: even a dict schema can raise before raw is returned.
        response_format: Any
        if method == "json_mode":
            response_format = {"type": "json_object"}
        elif isinstance(schema, type) and issubclass(schema, BaseModel):
            response_format = type_to_response_format_param(schema)
        else:
            response_format = _convert_to_openai_response_format(schema)
        if model.streaming:
            raw = None
            async with aclosing(_sdk_stream(model, messages, response_format=response_format, on_partial=on_partial)) as stream:
                async for chunk in stream:
                    raw = merge_chunks(raw, chunk)
            if not stream_finished(raw):
                raise IncompleteStreamError("missing terminal finish reason")
            return {"raw": raw, "parsed": None, "parsing_error": ValueError("local parsing required")}
        raw = await _sdk_message(model, messages, response_format=response_format, on_partial=on_partial)
        return {"raw": raw, "parsed": None, "parsing_error": ValueError("local parsing required")}
    kwargs: dict[str, Any] = {"include_raw": True}
    if method is not None:
        kwargs["method"] = method
    runnable = model.with_structured_output(schema, **kwargs)
    return await runnable.ainvoke(messages)


async def stream_messages(model: Any, messages: Any, *, on_partial: Callable[[Any], None] | None = None) -> AsyncGenerator[Any]:
    """Close streams on every exit; report metadata before handing content to callbacks."""
    sdk = isinstance(model, ChatOpenAI)
    stream = _sdk_stream(model, messages, on_partial=on_partial) if sdk else model.astream(messages)
    raw = None
    try:
        async for chunk in stream:
            if not sdk and on_partial is not None:
                raw = merge_chunks(raw, chunk)
                on_partial(raw)
            yield chunk
    finally:
        await _close_stream(stream)


def sdk_length_error_message(exc: BaseException) -> Any | None:
    """Expose factual metadata from legacy SDK parse errors at the adapter boundary."""
    if not isinstance(exc, LengthFinishReasonError):
        return None
    completion = exc.completion.model_dump(mode="python")
    return SimpleNamespace(response_metadata={**completion, "source_usage": completion.get("usage"), "finish_reason": "length"})


def provider_structured_schema(schema: type[BaseModel]) -> type[BaseModel] | dict[str, Any]:
    if getattr(schema, "provider_strict", False):
        return {"name": schema.__name__, "schema": to_strict_json_schema(schema), "strict": True}
    return schema


def copy_model(model: Any, **options: Any) -> Any:
    """SDK copies share their owner's HTTP pools; request options remain local."""
    return model.model_copy(update=options)
