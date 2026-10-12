"""Извлечение метаданных из ответа внешнего провайдера."""

from __future__ import annotations

from typing import Any


def _extract_cache_read(um: dict) -> int | None:
    "Кэш-чтение из usage-словаря — форма зависит от провайдера/нормализации."
    details = um.get("input_token_details") or {}
    if isinstance(details, dict) and details.get("cache_read") is not None:
        return details.get("cache_read")
    details = um.get("prompt_tokens_details") or {}
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return details.get("cached_tokens")
    return um.get("cache_read_input_tokens")


def _response_finish_reason(msg: Any) -> str | None:
    metadata = getattr(msg, "response_metadata", None) or {}
    additional = getattr(msg, "additional_kwargs", None) or {}
    for container in (metadata, additional):
        if not isinstance(container, dict):
            continue
        value = container.get("finish_reason") or container.get("stop_reason")
        if isinstance(value, str) and value:
            return value
        choices = container.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str):
                    return choice["finish_reason"]
    return None


def _response_cost(msg: Any) -> float | None:
    "Фактическая стоимость вызова так, как её вернул провайдер (RouterAI — в рублях)."
    containers: list[Any] = []
    for source in (getattr(msg, "response_metadata", None), getattr(msg, "additional_kwargs", None)):
        if not isinstance(source, dict):
            continue
        containers.append(source)
        containers.extend(source.get(key) for key in ("token_usage", "usage"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("cost", "total_cost", "cost_usd"):
            value = container.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                return float(value)
    return None


def _response_provider(msg: Any) -> str | None:
    """Read only a short provider identifier, never a provider payload."""
    metadata = getattr(msg, "response_metadata", None) or {}
    additional = getattr(msg, "additional_kwargs", None) or {}
    for container in (metadata, additional):
        if not isinstance(container, dict):
            continue
        for key in ("provider", "provider_name", "provider_slug", "upstream_provider"):
            value = container.get(key)
            if (identifier := _safe_identifier(value)) is not None:
                return identifier
    return None


def _safe_identifier(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value.isascii()
        and all(char.isalnum() or char in "_-/.:" for char in value)
    ):
        return value
    return None


def _response_model(msg: Any) -> str | None:
    metadata = getattr(msg, "response_metadata", None) or {}
    if "source_model" in metadata:
        return _safe_identifier(metadata["source_model"])
    return _safe_identifier(metadata.get("model") or metadata.get("model_name"))


def stream_finished(msg: Any) -> bool:
    return _response_finish_reason(msg) in {
        "stop",
        "length",
        "max_tokens",
        "max_tokens_reached",
        "content_filter",
        "tool_calls",
        "function_call",
        "error",
        "failed",
        "failure",
    }


def completion_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Select upper-level metadata without inspecting choices or copying content/errors."""
    metadata = {
        key: data[key]
        for key in ("model", "provider", "provider_name", "provider_slug", "upstream_provider", "cost", "total_cost", "cost_usd")
        if data.get(key) is not None
    }
    if data.get("usage") is not None:
        metadata["source_usage"] = data["usage"]
        metadata["token_usage"] = data["usage"]
    return metadata
