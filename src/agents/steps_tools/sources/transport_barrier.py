from __future__ import annotations

import socket
from typing import Any

from src.infrastructure.mcp_servers.dependencies import (
    McpSessionFailure,
    McpSessionUnavailableError,
)


def _transport_exception_types() -> tuple[type[BaseException], ...]:
    """Семейство внешнего транспорта; необязательные библиотеки подключаются мягко."""
    types: list[type[BaseException]] = [
        McpSessionUnavailableError,
        ConnectionError,
        TimeoutError,
        socket.gaierror,
    ]
    try:
        from mcp.shared.exceptions import McpError  # noqa: PLC0415

        types.append(McpError)
    except ImportError:  # pragma: no cover - mcp is a production dependency
        pass
    try:
        import httpx  # noqa: PLC0415

        types.append(httpx.HTTPError)
    except ImportError:  # pragma: no cover
        pass
    try:
        import anyio  # noqa: PLC0415

        types.extend((anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream))
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


_TRANSPORT_TYPES = _transport_exception_types()


def is_external_transport_failure(exc: BaseException) -> bool:
    "Исключение целиком принадлежит семейству внешнего транспорта."
    if isinstance(exc, BaseExceptionGroup):
        leaves = exc.exceptions
        return bool(leaves) and all(is_external_transport_failure(leaf) for leaf in leaves)
    return isinstance(exc, _TRANSPORT_TYPES)


def consultant_transport_failures(sessions: Any) -> tuple[McpSessionFailure, ...]:
    """Гибели сессий, зарегистрированные запросом; фейковые сессии тестов их не имеют."""
    failures = getattr(sessions, "transport_failures", None)
    if not isinstance(failures, tuple):
        return ()
    return failures


def consultant_pool_lost(sessions: Any) -> bool:
    """В настоящем пуле не осталось живых сессий К+; у двойника без диагностики — ``False``."""
    count = getattr(sessions, "live_session_count", None)
    return callable(count) and count("consultant") == 0


def ensure_consultant_session_alive(sessions: Any) -> None:
    "Fail before work when a real pool has no live Consultant session."
    if consultant_pool_lost(sessions):
        raise McpSessionUnavailableError("consultant", consultant_transport_failures(sessions))
