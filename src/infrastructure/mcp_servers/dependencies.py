from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from weakref import WeakKeyDictionary

from mcp import ClientSession
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from src.common.config.config import settings
from src.common.observability.metrics import MCP_CALLS, MCP_CALL_DURATION, MCP_RETRIES, MCP_SLOT_WAIT
from src.common.utils.logger import get_logger
from src.infrastructure.mcp_servers.client import McpClient

log = get_logger(__name__)


_STOP_TIMEOUT_S = float(settings["infrastructure.mcp_servers.dependencies.stop_timeout_s"])


_STOP_DYING_TIMEOUT_S = float(settings["infrastructure.mcp_servers.dependencies.stop_dying_timeout_s"])


_DEATH_GRACE_S = float(settings["infrastructure.mcp_servers.dependencies.death_grace_s"])


# В продукте один event loop на процесс. Разные тестовые loop не делят asyncio-примитивы.
_PROCESS_GATES: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]] = WeakKeyDictionary()


def _server_gate(server_name: str) -> asyncio.Semaphore:
    gates = _PROCESS_GATES.setdefault(asyncio.get_running_loop(), {})
    if server_name not in gates:
        gates[server_name] = asyncio.Semaphore(max(1, int(settings["mcp.max_concurrent_calls"])))
    return gates[server_name]


def _record_retry(state: RetryCallState) -> None:
    server_name = state.args[1] if len(state.args) > 1 else state.kwargs["server_name"]
    tool_name = state.args[2] if len(state.args) > 2 else state.kwargs["tool_name"]
    MCP_RETRIES.labels(server_name, tool_name).inc()


def exception_class_chain(exc: BaseException, *, limit: int = 12) -> tuple[str, ...]:
    "Имена классов исключения и его вложений/причин — БЕЗ текста сообщений."
    chain: list[str] = []

    def walk(err: BaseException) -> None:
        if len(chain) >= limit:
            return
        chain.append(type(err).__name__)
        for nested in getattr(err, "exceptions", ()):
            walk(nested)
        for linked in (err.__cause__, err.__context__):
            if linked is not None and linked is not err and len(chain) < limit:
                walk(linked)

    walk(exc)
    unique: list[str] = []
    for name in chain:
        if name not in unique:
            unique.append(name)
    return tuple(unique)


@dataclass(frozen=True, slots=True)
class McpSessionFailure:
    """Text-free запись о гибели одной сессии транспорта в рамках запроса."""

    server_name: str
    error_class: str
    error_chain: tuple[str, ...]

    occurred_at: float
    #: Сессия погибла ещё при открытии (до первого вызова) или уже в работе.
    at_startup: bool = False


class McpSessionUnavailableError(RuntimeError):
    "У запроса не осталось живых сессий сервера ``server_name``."

    def __init__(self, server_name: str, failures: tuple[McpSessionFailure, ...], *, in_flight: bool = False) -> None:
        self.server_name = server_name
        self.failures = failures
        self.in_flight = in_flight
        phase = "during the call" if in_flight else "before the call"
        super().__init__(f"MCP server '{server_name}': no live session left ({phase}; sessions lost: {len(failures)})")


def _is_retryable_invoke_error(exc: BaseException) -> bool:
    if isinstance(exc, McpSessionUnavailableError):
        return False
    msg = str(exc)
    non_retry_markers = [
        "StatusCode.INVALID_ARGUMENT",
        "INVALID_ARGUMENT",
        "Не найден указанный номер метки",
    ]
    if any(m in msg for m in non_retry_markers):
        return False

    retry_markers = [
        "TimeoutError",
        "timed out",
        "DeadlineExceeded",
        "StatusCode.DEADLINE_EXCEEDED",
        "StatusCode.UNAVAILABLE",
        "UNAVAILABLE",
        "connection",
        "ConnectionResetError",
        "ServerDisconnectedError",
        "502",
        "503",
        "504",
        "429",
    ]
    return any(m in msg for m in retry_markers)


class _SessionHolder:
    "Одна MCP-сессия, открытая и закрываемая в СОБСТВЕННОЙ asyncio-задаче."

    def __init__(self, mcp: McpClient, server_name: str, *, on_failure: Any) -> None:
        self._mcp = mcp
        self.server_name = server_name
        self._on_failure = on_failure
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._startup_error: BaseException | None = None
        self._dying = False
        #: Сессия больше не годна для вызовов (гибель, отмена транспортом или закрытие).
        self.closed = asyncio.Event()
        self.session: ClientSession | None = None
        self.failure: McpSessionFailure | None = None

    @property
    def alive(self) -> bool:
        return self.session is not None and self.failure is None and not self._dying

    async def wait_settled(self, timeout_s: float) -> None:
        """Дать задаче держателя завершиться (или хотя бы зафиксировать гибель)."""
        task = self._task
        if task is None or task.done():
            return
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)

    async def start(self) -> None:
        """Открыть сессию; сбой открытия пробрасывается вызывающему как раньше."""
        self._task = asyncio.create_task(self._run(), name=f"mcp-session:{self.server_name}")
        await self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error

    async def _run(self) -> None:
        opened = False
        try:
            async with self._mcp.instance.session(server_name=self.server_name) as session:
                self.session = session
                opened = True
                self._ready.set()
                try:
                    await self._stop.wait()
                except asyncio.CancelledError:
                    if not self._stop.is_set():
                        self._dying = True
                        self.closed.set()
                    raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # ExceptionGroup транспорта, McpError, httpx.* и т.п.
            self.failure = McpSessionFailure(
                server_name=self.server_name,
                error_class=type(exc).__name__,
                error_chain=exception_class_chain(exc),
                occurred_at=time.monotonic(),
                at_startup=not opened,
            )
            if not opened:
                self._startup_error = exc
            await log.awarning(
                "mcp_session_failed",
                server=self.server_name,
                at_startup=not opened,
                error_class=self.failure.error_class,
                error_chain=list(self.failure.error_chain),
            )
            self._on_failure(self.failure)

        finally:
            self.session = None
            self.closed.set()
            self._ready.set()

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        if not task.done():
            try:
                short = self._dying or self.failure is not None
                timeout = _STOP_DYING_TIMEOUT_S if short else _STOP_TIMEOUT_S
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except TimeoutError:
                task.cancel()
            except Exception:  # сбой при закрытии уже записан держателем
                pass
        await asyncio.gather(task, return_exceptions=True)


class McpRequestSessions:
    """Одна сессия на сервер в рамках экспертизы; вызовы выполняются параллельно."""

    def __init__(self, mcp: McpClient):
        self.mcp = mcp
        self._holders: dict[str, _SessionHolder] = {}
        self._failures: list[McpSessionFailure] = []
        self._calls: set[asyncio.Task[Any]] = set()
        self._closed = asyncio.Event()

    async def __aenter__(self) -> McpRequestSessions:
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    async def aclose(self) -> None:
        self._closed.set()
        calls = list(self._calls)
        for call in calls:
            call.cancel()
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)
        holders = list(self._holders.values())
        self._holders.clear()
        if holders:
            await asyncio.gather(*(holder.stop() for holder in holders), return_exceptions=True)

    @property
    def transport_failures(self) -> tuple[McpSessionFailure, ...]:
        """Сбои сессий этого запроса в порядке регистрации (только классы ошибок)."""
        return tuple(self._failures)

    def live_session_count(self, server_name: str) -> int:
        holder = self._holders.get(server_name)
        return int(not self._closed.is_set() and holder is not None and holder.alive)

    @property
    def calls_in_flight(self) -> int:
        """Незавершённые сетевые вызовы, включая брошенные вызывающим."""
        return len(self._calls)

    async def add_server(self, server_name: str) -> None:
        if self._closed.is_set():
            raise McpSessionUnavailableError(server_name, self.transport_failures)
        if server_name in self._holders:
            return
        if server_name not in self.mcp.connections:
            raise RuntimeError(f"Server '{server_name}' not found")
        holder = _SessionHolder(self.mcp, server_name, on_failure=self._failures.append)
        self._holders[server_name] = holder
        await holder.start()

    def supports(self, server_name: str, tool_name: str) -> bool:
        "Return whether this request can actually invoke a warmed-up tool."
        return (server_name, tool_name) in self.mcp.tools and self.live_session_count(server_name) > 0

    @staticmethod
    async def _holder_died(holder: _SessionHolder) -> bool:
        await holder.wait_settled(_DEATH_GRACE_S)
        return not holder.alive

    def _finish_call(self, call: asyncio.Task[Any]) -> None:
        self._calls.discard(call)
        if not call.cancelled():
            call.exception()  # Retrieve failures of calls whose caller stopped waiting.

    async def _call(self, holder: _SessionHolder, tool_name: str, user_query: dict[str, Any], timeout_s: float | None) -> Any:
        """Один сетевой вызов на держателе; прерывается гибелью его сессии."""
        session = holder.session
        if session is None or not holder.alive:
            raise McpSessionUnavailableError(holder.server_name, self.transport_failures, in_flight=True)
        request = asyncio.ensure_future(
            session.call_tool(
                tool_name,
                {"data": user_query},
                read_timeout_seconds=None if timeout_s is None else timedelta(seconds=float(timeout_s)),
            )
        )
        closed = asyncio.ensure_future(holder.closed.wait())
        try:
            await asyncio.wait({request, closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            closed.cancel()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, closed, return_exceptions=True)
        if not request.cancelled() and request.exception() is None:
            return request.result()
        cause = None if request.cancelled() else request.exception()
        if not holder.alive or await self._holder_died(holder):
            raise McpSessionUnavailableError(holder.server_name, self.transport_failures, in_flight=True) from cause
        assert cause is not None
        raise cause

    async def _acquire_slot(self, gate: asyncio.Semaphore, holder: _SessionHolder, tool_name: str) -> None:
        acquire = asyncio.create_task(gate.acquire())
        session_closed = asyncio.create_task(holder.closed.wait())
        request_closed = asyncio.create_task(self._closed.wait())
        started = time.perf_counter()
        try:
            try:
                await asyncio.wait({acquire, session_closed, request_closed}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (acquire, session_closed, request_closed):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(acquire, session_closed, request_closed, return_exceptions=True)
            if self._closed.is_set() or holder.closed.is_set() or not holder.alive:
                raise McpSessionUnavailableError(holder.server_name, self.transport_failures)
        except BaseException:
            # Отмена могла совпасть с выдачей слота; незабранный слот возвращаем.
            if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                gate.release()
            raise
        finally:
            MCP_SLOT_WAIT.labels(holder.server_name, tool_name).observe(time.perf_counter() - started)

    async def _measured_call(
        self, holder: _SessionHolder, tool_name: str, user_query: dict[str, Any], timeout_s: float | None
    ) -> Any:
        started = time.perf_counter()
        outcome = "success"
        try:
            result = await self._call(holder, tool_name, user_query, timeout_s)
            if result.isError:
                first = result.content[0] if result.content else None
                detail = getattr(first, "text", None) or type(first).__name__
                raise RuntimeError(f"Error: {detail}")
            structured = result.structuredContent
            if not isinstance(structured, dict) or "data" not in structured:
                raise RuntimeError(f"Tool '{tool_name}' returned no structured data")
            return structured["data"]
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            MCP_CALL_DURATION.labels(holder.server_name, tool_name).observe(time.perf_counter() - started)
            MCP_CALLS.labels(holder.server_name, tool_name, outcome).inc()

    @retry(
        retry=retry_if_exception(_is_retryable_invoke_error),
        stop=stop_after_attempt(3),
        wait=wait_random_exponential(multiplier=0.5, max=5),
        reraise=True,
        before_sleep=_record_retry,
    )
    async def invoke(
        self,
        server_name: str,
        tool_name: str,
        user_query: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ):
        if self._closed.is_set():
            raise McpSessionUnavailableError(server_name, self.transport_failures)
        self.mcp.ensure_tool_exists(server_name, tool_name)

        holder = self._holders.get(server_name)
        if holder is None:
            raise RuntimeError(f"Server '{server_name}' not added. Call add_server('{server_name}') first.")
        if self._closed.is_set() or holder.closed.is_set() or not holder.alive:
            raise McpSessionUnavailableError(server_name, self.transport_failures)

        gate = _server_gate(server_name)
        await self._acquire_slot(gate, holder, tool_name)
        # Сессия могла погибнуть, пока вызов ожидал свободного слота.
        if self._closed.is_set() or holder.closed.is_set() or not holder.alive:
            gate.release()
            raise McpSessionUnavailableError(server_name, self.transport_failures)
        call = asyncio.create_task(self._measured_call(holder, tool_name, user_query, timeout_s), name=f"mcp-call:{tool_name}")
        # shield ниже сохраняет сетевой вызов при отмене ожидания: слот принадлежит вызову.
        call.add_done_callback(lambda _call: gate.release())
        self._calls.add(call)
        try:
            result = await asyncio.shield(call)
        finally:
            if call.done():
                self._finish_call(call)
            else:
                call.add_done_callback(self._finish_call)

        return result
