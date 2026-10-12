"""Контекст входящего MCP-вызова и подпись исходящих запросов."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import uuid4

import httpx
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

SERVICE_NAME = "antikorr"


HEADER_SERVICE_NAME = "X-Service-Name"


HEADER_USER_ID = "X-User-Id"


HEADER_ORGANIZATION_ID = "X-Organization-Id"


HEADER_CHAT_ID = "X-Chat-Id"


HEADER_THREAD_ID = "X-Thread-Id"


HEADER_REQUEST_ID = "X-Request-ID"

HEADER_OPERATION = "X-LLM-Operation"


_HEADER_VALUE_MAX = 256


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped[:_HEADER_VALUE_MAX] if stripped else None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, candidate in headers.items():
        if key.lower() == lowered:
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Кто и в каком диалоге спрашивает: поля названы по заголовкам шлюза, а не по контракту навыка."""

    user_id: str | None = None
    organization_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None

    def fill_missing(self, *, chat_id: str | None = None, thread_id: str | None = None) -> RequestIdentity:
        """Дозаполнить пустое из тела навыка; уже привязанное (заголовки) не трогать."""
        return RequestIdentity(
            user_id=self.user_id,
            organization_id=self.organization_id,
            chat_id=self.chat_id or _clip(chat_id),
            thread_id=self.thread_id or _clip(thread_id),
        )

    def as_gateway_headers(self) -> dict[str, str]:
        pairs = (
            (HEADER_USER_ID, self.user_id),
            (HEADER_ORGANIZATION_ID, self.organization_id),
            (HEADER_CHAT_ID, self.chat_id),
            (HEADER_THREAD_ID, self.thread_id),
        )
        return {name: value for name, value in pairs if value is not None}


_EMPTY_IDENTITY = RequestIdentity()


_IDENTITY: ContextVar[RequestIdentity] = ContextVar("llm_gateway_identity", default=_EMPTY_IDENTITY)


_REQUEST_IDS: ContextVar[list[str] | None] = ContextVar("llm_gateway_request_ids", default=None)

_OPERATION: ContextVar[str | None] = ContextVar("llm_gateway_operation", default=None)


def current_identity() -> RequestIdentity:
    return _IDENTITY.get()


def identity_from_headers(headers: Mapping[str, str]) -> RequestIdentity:
    return RequestIdentity(
        user_id=_clip(_header(headers, HEADER_USER_ID)),
        organization_id=_clip(_header(headers, HEADER_ORGANIZATION_ID)),
        chat_id=_clip(_header(headers, HEADER_CHAT_ID)),
        thread_id=_clip(_header(headers, HEADER_THREAD_ID)),
    )


def bind_identity(identity: RequestIdentity) -> RequestIdentity:
    """Заменить идентичность целиком. В MCP — на задаче сессии для этого ``tools/call``."""
    _IDENTITY.set(identity)
    return identity


def bind_skill_identity(*, thread_id: str, message_id: str | None) -> RequestIdentity:
    """Контракт навыка: ``thread_id`` — диалог (чат шлюза), ``message_id`` — один вызов (поток шлюза)."""
    return bind_identity(current_identity().fill_missing(chat_id=thread_id, thread_id=message_id))


@contextmanager
def llm_call_scope(*, operation: str | None = None) -> Iterator[list[str]]:
    """Собрать ``X-Request-ID`` всех попыток одного логического вызова (для лога ``llm_call``)."""
    request_ids: list[str] = []
    ids_token = _REQUEST_IDS.set(request_ids)
    operation_token = _OPERATION.set(_clip(operation))
    try:
        yield request_ids
    finally:
        _OPERATION.reset(operation_token)
        _REQUEST_IDS.reset(ids_token)


def attach_gateway_headers(request: httpx.Request) -> None:
    """Дописать метки шлюза в запрос; уже стоящие в запросе значения важнее привязанных."""
    for name, value in current_identity().as_gateway_headers().items():
        if name not in request.headers:
            request.headers[name] = value
    operation = _OPERATION.get()
    if operation is not None and HEADER_OPERATION not in request.headers:
        request.headers[HEADER_OPERATION] = operation
    if HEADER_REQUEST_ID not in request.headers:
        request.headers[HEADER_REQUEST_ID] = uuid4().hex
    request_ids = _REQUEST_IDS.get()
    if request_ids is not None:
        request_ids.append(request.headers[HEADER_REQUEST_ID])


async def attach_gateway_headers_async(request: httpx.Request) -> None:
    attach_gateway_headers(request)


class RequestIdentityMiddleware:
    """Привязать идентичность сразу при получении запроса.

    Чистый ASGI сохраняет контекст асинхронного запроса.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        identity = identity_from_headers(Headers(scope=scope))
        token: Token[RequestIdentity] = _IDENTITY.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _IDENTITY.reset(token)
