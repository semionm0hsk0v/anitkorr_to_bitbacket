import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tenacity import wait_none

from src.infrastructure.mcp_servers import dependencies as transport


@pytest.fixture
async def sessions(monkeypatch, request):
    monkeypatch.setattr(transport, "settings", {"mcp.max_concurrent_calls": getattr(request, "param", 2)})
    transport._PROCESS_GATES.pop(asyncio.get_running_loop(), None)
    created = []

    def create(call_tool):
        mcp = SimpleNamespace(ensure_tool_exists=lambda *args: None)
        pool = transport.McpRequestSessions(mcp)
        holder = transport._SessionHolder(mcp, "consultant", on_failure=lambda failure: None)
        holder.session = SimpleNamespace(call_tool=call_tool)

        async def stop():
            holder.session = None
            holder.closed.set()

        holder.stop = stop
        pool._holders["consultant"] = holder
        created.append(pool)
        return pool, holder

    yield create
    assert all(pool.calls_in_flight == 0 for pool in created)


def reply(value=None):
    return SimpleNamespace(isError=False, structuredContent={"data": value})


@pytest.mark.parametrize("sessions", [2, 8], indirect=True)
async def test_limit_is_shared_between_expertises_and_tools(sessions):
    limit = transport.settings["mcp.max_concurrent_calls"]
    entered = asyncio.Event()
    release = asyncio.Event()
    active = peak = total = 0

    async def call_tool(*args, **kwargs):
        nonlocal active, peak, total
        active += 1
        total += 1
        peak = max(peak, active)
        if active == limit:
            entered.set()
        try:
            await release.wait()
            return reply()
        finally:
            active -= 1

    pools = [sessions(call_tool)[0] for _ in range(2)]
    count = limit + 3
    calls = [asyncio.create_task(pools[i % 2].invoke("consultant", f"tool_{i % 3}", {})) for i in range(count)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert total == limit
        release.set()
        await asyncio.wait_for(asyncio.gather(*calls), 1)
        assert total == count and peak == limit
    finally:
        await asyncio.gather(*(pool.aclose() for pool in pools))


async def test_cancelled_waiter_never_starts_and_orphan_keeps_slot(sessions):
    entered = asyncio.Event()
    release = asyncio.Event()
    started = []

    async def call_tool(name, *args, **kwargs):
        started.append(name)
        if len(started) == 2:
            entered.set()
        await release.wait()
        return reply()

    pool, _ = sessions(call_tool)
    first = asyncio.create_task(pool.invoke("consultant", "first", {}))
    second = asyncio.create_task(pool.invoke("consultant", "second", {}))
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    queued = asyncio.create_task(pool.invoke("consultant", "queued", {}))
    # Both slots belong to actual calls, even though one caller stopped waiting.
    await asyncio.sleep(0)
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    assert started == ["first", "second"]
    release.set()
    await second
    await pool.aclose()
    assert transport._server_gate("consultant")._value == 2


@pytest.mark.parametrize("reason", ["session_lost", "request_closed"])
async def test_queue_stops_when_session_or_request_closes(sessions, reason):
    busy, _ = sessions(AsyncMock())
    gate = transport._server_gate("consultant")
    await gate.acquire()
    await gate.acquire()
    call_tool = AsyncMock(return_value=reply())
    pool, holder = sessions(call_tool)
    queued = asyncio.create_task(pool.invoke("consultant", "queued", {}))
    await asyncio.sleep(0)
    if reason == "session_lost":
        holder.session = None
        holder.closed.set()
    else:
        await pool.aclose()
    with pytest.raises(transport.McpSessionUnavailableError):
        await asyncio.wait_for(queued, 1)
    call_tool.assert_not_awaited()
    gate.release()
    gate.release()
    await pool.aclose()
    await busy.aclose()
    assert gate._value == 2


async def test_failed_attempt_releases_slot_and_retry_uses_same_limit(sessions, monkeypatch):
    monkeypatch.setattr(transport.McpRequestSessions.invoke.retry, "wait", wait_none())
    call_tool = AsyncMock(side_effect=[RuntimeError("503"), reply("ok")])
    pool, _ = sessions(call_tool)
    try:
        assert await pool.invoke(server_name="consultant", tool_name="search", user_query={}, timeout_s=7) == "ok"
        assert call_tool.await_count == 2
        assert call_tool.call_args.kwargs["read_timeout_seconds"].total_seconds() == 7
        assert transport._server_gate("consultant")._value == 2
    finally:
        await pool.aclose()
