from __future__ import annotations

import time
from collections.abc import Collection, Sequence

from prometheus_client import Counter, Gauge, Histogram, Info
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.common.config.config import settings

#: Путь экспозиции. Он же в `serviceMonitor.path` обоих `ci_cd/values*.yaml`.
METRICS_PATH = "/metrics"


UNMATCHED_PATH = "__unmatched__"


NO_RESPONSE_STATUS = "none"


_DURATION_BUCKETS = tuple(float(bucket) for bucket in settings["observability.duration_buckets"])

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Обработанные HTTP-запросы навыка.",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Полное время обработки HTTP-запроса навыка.",
    ["method", "path"],
    buckets=_DURATION_BUCKETS,
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "HTTP-запросы навыка, обрабатываемые прямо сейчас.",
    ["method", "path"],
)

BUILD_INFO = Info(
    "antikorr_build",
    "Версия выложенного навыка: связывает серию метрик с конкретным деплоем.",
)


MCP_SLOT_WAIT = Histogram(
    "mcp_slot_wait_seconds", "Ожидание свободного слота MCP.", ["server", "tool"], buckets=_DURATION_BUCKETS
)
MCP_CALL_DURATION = Histogram(
    "mcp_call_duration_seconds", "Длительность фактического обращения MCP.", ["server", "tool"], buckets=_DURATION_BUCKETS
)
MCP_CALLS = Counter("mcp_calls_total", "Фактические обращения MCP.", ["server", "tool", "outcome"])
MCP_RETRIES = Counter("mcp_retries_total", "Повторные попытки MCP.", ["server", "tool"])


def _match_route_path(routes: Sequence[BaseRoute], scope: Scope) -> str | None:
    "Шаблон первого подошедшего маршрута либо ``None``."
    partial: str | None = None
    for route in routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return getattr(route, "path", None)
        if match is Match.PARTIAL and partial is None:
            partial = getattr(route, "path", None)
    return partial


def resolve_path_label(routes: Sequence[BaseRoute], scope: Scope) -> str:
    """Метка ``path`` для запроса: шаблон маршрута, а не конкретный адрес."""
    matched = _match_route_path(routes, scope)
    if matched is not None:
        return matched

    path = str(scope.get("path", ""))
    alternate = path[:-1] if path.endswith("/") and len(path) > 1 else path + "/"
    matched = _match_route_path(routes, {**scope, "path": alternate})
    return matched if matched is not None else UNMATCHED_PATH


class PrometheusMiddleware:
    "Считает запросы, их длительность и число одновременных."

    def __init__(self, app: ASGIApp, routes: Sequence[BaseRoute], skip_paths: Collection[str] = ()) -> None:
        self.app = app
        self._routes = routes
        self._skip = frozenset(skip_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = resolve_path_label(self._routes, scope)
        if path in self._skip:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET"))
        status: str | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = str(message["status"])
            await send(message)

        HTTP_REQUESTS_IN_PROGRESS.labels(method, path).inc()
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            HTTP_REQUESTS.labels(method, path, status or NO_RESPONSE_STATUS).inc()
            HTTP_REQUEST_DURATION.labels(method, path).observe(time.perf_counter() - started)
            HTTP_REQUESTS_IN_PROGRESS.labels(method, path).dec()
