import asyncio

import pytest

from src.agents.steps_tools.sources import retrieval


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, 60])
async def test_pages_overlap_and_keep_contiguous_order(monkeypatch, missing):
    starts = []
    all_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fetch(sessions, base, doc_num, start, end):
        starts.append((start, end))
        if len(starts) == 3:
            all_started.set()
        await all_started.wait()
        if start == 0:
            await release_first.wait()
        else:
            release_first.set()
        return (None if start == missing else str(start)), "mcp"

    monkeypatch.setattr(retrieval, "_PAGE", 60)
    monkeypatch.setattr(retrieval, "fetch_doc_text_with_transport", fetch)
    parts, ranges, incomplete, transport = await asyncio.wait_for(retrieval._fetch_pages(None, "LAW", 1, 145), 1)

    assert starts == [(0, 60), (60, 120), (120, 145)]
    assert parts == (["0", "60", "120"] if missing is None else ["0"])
    assert [(r.start, r.end) for r in ranges] == ([(1, 60), (61, 120), (121, 145)] if missing is None else [(1, 60)])
    assert incomplete is (missing is not None)
    assert transport == "mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_page_tasks_finish_on_cancellation_or_failure(monkeypatch, failure):
    active = set()
    all_started = asyncio.Event()
    fail = asyncio.Event()

    async def fetch(sessions, base, doc_num, start, end):
        active.add(start)
        if len(active) == 3:
            all_started.set()
        try:
            await fail.wait()
            if start == 0:
                raise ConnectionError("transport failed")
            await asyncio.Event().wait()
        finally:
            active.remove(start)

    monkeypatch.setattr(retrieval, "_PAGE", 60)
    monkeypatch.setattr(retrieval, "fetch_doc_text_with_transport", fetch)
    task = asyncio.create_task(retrieval._fetch_pages(None, "LAW", 1, 145))
    await asyncio.wait_for(all_started.wait(), 1)
    if failure:
        fail.set()
        with pytest.raises(ExceptionGroup) as error:
            await asyncio.wait_for(task, 1)
        assert retrieval.is_external_transport_failure(error.value)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not active
