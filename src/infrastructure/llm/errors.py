"""Ошибки LLM: типы, классификация и безопасные поля журналирования."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, get_args
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, LengthFinishReasonError
from pydantic import ValidationError
from pydantic_core import ErrorType

from src.common.config.config import settings


class ProviderFinishReasonError(RuntimeError):
    """The provider marked the response as failed despite returning a payload."""

    def __init__(self, reason: str) -> None:
        super().__init__("provider did not complete usable output")
        self.finish_reason = reason
        self.kind = (
            "filtered_output"
            if reason == "content_filter"
            else ("unsupported_output" if reason in {"tool_calls", "function_call"} else "provider_error")
        )


def _retry_error_text(exc: BaseException) -> str:
    """Never log external messages, schema locations or input values."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            sorted(
                {
                    item["type"] if item["type"] in get_args(ErrorType) else "custom_validation"
                    for item in exc.errors(include_input=False, include_context=False)
                }
            )
        )[:200]
    return type(exc).__name__


_CAUSE_DEPTH = int(settings["infrastructure.llm.cause_depth"])


def _next_in_chain(exc: BaseException) -> BaseException | None:
    if exc.__cause__ is not None:
        return exc.__cause__
    return None if exc.__suppress_context__ else exc.__context__


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Сам сбой и его причины (до ``_CAUSE_DEPTH`` звеньев вглубь), без циклов."""
    chain = [exc]
    seen = {id(exc)}
    while len(chain) <= _CAUSE_DEPTH:
        nxt = _next_in_chain(chain[-1])
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        chain.append(nxt)
    return chain


def _safe_attr(obj: Any, name: str) -> Any:
    """``getattr`` без исключений: у ``httpx.RequestError.request`` свойство бросает ``RuntimeError``."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def exception_status_code(exc: BaseException) -> int | None:
    """HTTP-статус из цепочки исключения: ``status_code`` самого сбоя, причины или их ``response``."""
    for item in _exception_chain(exc):
        for holder in (item, _safe_attr(item, "response")):
            status = _safe_attr(holder, "status_code")
            if isinstance(status, int) and not isinstance(status, bool):
                return status
    return None


def _request_target(url: Any) -> str:
    """Origin only: path, credentials and query may all contain sensitive values."""
    try:
        parts = urlsplit(str(url))
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname or ''}{port}"
    except ValueError:
        return "unparsable"


def llm_error_log_fields(exc: BaseException) -> dict[str, Any]:
    "Поля лога сбоя вызова модели: тип и текст, цепочка причин, статус, адрес запроса."
    chain = _exception_chain(exc)
    fields: dict[str, Any] = {"error_type": type(exc).__name__, "error": _retry_error_text(exc)}
    if len(chain) > 1:
        fields["cause_type"] = type(chain[1]).__name__
        fields["cause"] = _retry_error_text(chain[1])
    if len(chain) > 2:
        fields["root_cause_type"] = type(chain[-1]).__name__
        fields["root_cause"] = _retry_error_text(chain[-1])
    code = exception_code(exc)
    if code is not None:
        fields["error_code"] = code
    status = exception_status_code(exc)
    if status is not None:
        fields["status_code"] = status
    for item in chain:
        request = _safe_attr(item, "request")
        if request is None:
            continue
        url = _safe_attr(request, "url")
        if url is not None:
            fields["request_url"] = _request_target(url)
        method = _safe_attr(request, "method")
        if isinstance(method, str) and method:
            fields["request_method"] = method
        headers = _safe_attr(request, "headers")
        if isinstance(headers, Mapping) and headers:
            fields["request_header_names"] = sorted({str(name) for name in headers})
        break
    return fields


_SAFE_CODES = frozenset(
    {
        "insufficient_quota",
        "insufficient_balance",
        "quota_exceeded",
        "rate_limit_exceeded",
        "context_length_exceeded",
        "upstream_timeout",
        "upstream_connect_error",
        "upstream_error",
        "invalid_api_key",
        "model_not_found",
    }
)


class ProviderResponseError(RuntimeError):
    """A soft provider error, with only allowlisted code and numeric failure status."""

    def __init__(self, error: Any) -> None:
        super().__init__("provider returned an error response")
        fields = error if isinstance(error, dict) else {}
        code = fields.get("code")
        self.code = code if isinstance(code, str) and code in _SAFE_CODES else None
        status = fields.get("status_code", fields.get("status", code))
        self.status_code = (
            status
            if isinstance(status, int) and not isinstance(status, bool) and 400 <= status < 600
            else {
                "invalid_api_key": 401,
                "model_not_found": 404,
                "context_length_exceeded": 400,
                "rate_limit_exceeded": 429,
            }.get(self.code or "")
        )


class ProviderProtocolError(RuntimeError):
    """Malformed response structure; no external payload is kept in the error."""

    kind = "invalid_response"

    def __init__(self) -> None:
        super().__init__("provider returned a malformed response")


def check_response_error(data: dict[str, Any]) -> None:
    """Error envelopes take precedence over any otherwise usable content."""
    if data.get("error") is not None:
        raise ProviderResponseError(data["error"])
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict) and choices[0].get("error") is not None:
        raise ProviderResponseError(choices[0]["error"])


def exception_code(exc: BaseException) -> str | None:
    for item in _exception_chain(exc):
        code = _safe_attr(item, "code")
        if isinstance(code, str) and code in _SAFE_CODES:
            return code
    return None


def quota_exhausted(exc: BaseException) -> bool:
    """Only provider errors may use the narrow legacy spending-limit fallback."""
    if isinstance(exc, ValidationError):
        return False
    if exception_code(exc) in {"insufficient_quota", "insufficient_balance", "quota_exceeded"}:
        return True
    return (
        isinstance(exc, APIStatusError)
        and exc.status_code in {402, 429}
        and any(marker in str(exc).casefold() for marker in ("spending limit", "insufficient balance", "insufficient_quota"))
    )


def llm_error_kind(exc: BaseException, *, finish_reason: str | None = None) -> str:
    """Classify typed/structured signals before any legacy text heuristics."""
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str):
        return kind
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    reason = finish_reason or getattr(exc, "finish_reason", None)
    if isinstance(exc, LengthFinishReasonError) or reason in {"length", "max_tokens", "max_tokens_reached"}:
        return "truncated_output"
    if isinstance(exc, ValidationError):
        return "invalid_schema"
    if quota_exhausted(exc):
        return "quota_error"
    code = exception_code(exc)
    if isinstance(exc, (TimeoutError, APITimeoutError)) or code == "upstream_timeout":
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError, APIConnectionError, httpx.TransportError)) or code == "upstream_connect_error":
        return "transport_error"
    status = exception_status_code(exc)
    if status == 429:
        return "rate_limit"
    if isinstance(exc, (APIError, ProviderFinishReasonError, ProviderResponseError)) or status is not None:
        return "provider_error"
    # Compatibility with local parse doubles; never use payload text for quota.
    name = type(exc).__name__.casefold()
    if "empty" in name:
        return "empty_output"
    if "json" in name or "parse" in name:
        return "invalid_json"
    if isinstance(exc, ValueError):
        return "invalid_json"
    return "unknown_error"


def safe_error_fingerprint_for_class(error_class: str, *, reason_code: str = "external_failure") -> str:
    material = f"external-failure|{error_class}|{reason_code}"
    return f"external-failure:{sha256(material.encode()).hexdigest()[:24]}"
