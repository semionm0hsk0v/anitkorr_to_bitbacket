import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.enums.competence.sources import ProjectAction
from src.agents.enums.legal_base.currentness import ReferenceCurrentnessOutcome
from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.schemas.competence.sources import CompetenceRefs
from src.agents.schemas.legal_base.source_refs import SourceRef
from src.agents.steps_tools.authority.sources import collect_competence_sources
from src.agents.steps_tools.sources import raw_cache
from src.agents.steps_tools.sources.currentness import current_analysis_date
from src.agents.steps_tools.sources.trust import trusted_currentness_scope


def law(number, *, role=SourceRole.CITED, date="19.05.1995"):
    return SourceRef(
        kind="federal_law",
        number=f"{number}-ФЗ",
        date=date,
        role=role,
        origin=SourceOrigin.PREAMBLE,
        raw=f"Федеральный закон от {date} № {number}-ФЗ",
    )


def card(number):
    return {
        "bank": "LAW",
        "doc_num": number,
        "title": f'Федеральный закон от 19.05.1995 № {number}-ФЗ "О полномочиях"',
    }


def metadata(number):
    return {
        "title": card(number)["title"],
        "numbers": [f"{number}-ФЗ"],
        "dates": ["19.05.1995"],
        "par_count": 1,
        "edition": "10.01.2026",
        "in_force": True,
        "status_search": "Все акты, кроме утративших силу",
    }


async def collect(refs, session):
    with trusted_currentness_scope():
        return await collect_competence_sources(session, refs, analysis_date=current_analysis_date(), timeout_s=5)


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(raw_cache, "_CACHE_DIR", tmp_path)


async def test_parallel_card_search_then_direct_read_preserves_priority_and_roles():
    searches_started = set()
    both_started = asyncio.Event()

    async def invoke(server, tool, payload, **kwargs):
        if tool == "consultant_card_search":
            number = int(payload["number"].split("-")[0])
            searches_started.add(number)
            if len(searches_started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            return [card(number)]
        if tool == "consultant_doc_info":
            assert both_started.is_set()
            return metadata(payload["doc_num"])
        if tool == "consultant_get_document_text":
            return {"segment": f"Полномочия органа. Закон {payload['doc_num']}."}
        raise AssertionError(f"Неожиданный инструмент: {tool}")

    session = SimpleNamespace(invoke=AsyncMock(side_effect=invoke))
    refs = CompetenceRefs(
        action=ProjectAction.AMEND,
        target_act=law(82, role=SourceRole.TARGET),
        cited_acts=(law(81), law(81), law(84, date="")),
    )
    result = await collect(refs, session)

    assert [reading.ref.number for reading in result.readings] == ["82-ФЗ", "81-ФЗ", "84-ФЗ"]
    assert [reading.ref.role for reading in result.readings[:2]] == [SourceRole.TARGET, SourceRole.CITED]
    assert [reading.status for reading in result.readings] == [
        ReadingStatus.COMPLETE,
        ReadingStatus.COMPLETE,
        ReadingStatus.NO_DATE,
    ]
    calls = Counter(call.args[1] for call in session.invoke.call_args_list)
    assert calls == {"consultant_card_search": 2, "consultant_doc_info": 4, "consultant_get_document_text": 2}
    assert [reading.source_id for reading in result.readings[:2]] == ["LAW:82", "LAW:81"]
    assert all(reading.raw.currentness_receipt.live.all_checks_passed for reading in result.readings[:2])


async def test_missing_and_failed_searches_keep_successful_sources_and_limits():
    async def invoke(server, tool, payload, **kwargs):
        if tool == "consultant_card_search":
            number = int(payload["number"].split("-")[0])
            if number == 82:
                return []
            if number == 83:
                raise TimeoutError("search timed out")
            return [card(number)]
        if tool == "consultant_doc_info":
            return metadata(payload["doc_num"])
        if tool == "consultant_get_document_text":
            return {"segment": "Полномочия органа."}
        raise AssertionError(tool)

    session = SimpleNamespace(invoke=AsyncMock(side_effect=invoke))
    result = await collect(CompetenceRefs(cited_acts=(law(81), law(82), law(83))), session)

    assert [reading.status for reading in result.readings] == [
        ReadingStatus.COMPLETE,
        ReadingStatus.NOT_FOUND,
        ReadingStatus.NOT_FOUND,
    ]
    assert any("82-ФЗ" in text and "Не найден" in text for text in result.limitations)
    assert any("83-ФЗ" in text and "Сбой поиска" in text for text in result.limitations)
    assert sum(call.args[1] == "consultant_get_document_text" for call in session.invoke.call_args_list) == 1


async def test_revoked_source_text_is_not_read_or_exposed():
    async def invoke(server, tool, payload, **kwargs):
        if tool == "consultant_card_search":
            return [card(81)]
        if tool == "consultant_doc_info":
            return {**metadata(81), "in_force": False, "status_search": "Утратил силу или отменен"}
        raise AssertionError(f"Текст отменённого источника запрашиваться не должен: {tool}")

    session = SimpleNamespace(invoke=AsyncMock(side_effect=invoke))
    result = await collect(CompetenceRefs(cited_acts=(law(81),)), session)
    assert result.readings[0].raw.currentness_receipt.outcome is ReferenceCurrentnessOutcome.STALE
    assert result.readings[0].raw.version.in_force is False
    assert result.readings[0].model_text() == ""


async def test_no_searchable_refs_does_not_call_consultant():
    session = SimpleNamespace(invoke=AsyncMock())
    result = await collect(CompetenceRefs(cited_acts=(law(81, date=""),)), session)
    assert result.readings[0].status is ReadingStatus.NO_DATE
    session.invoke.assert_not_awaited()


@pytest.mark.parametrize("blocked_tool", ["consultant_card_search", "consultant_get_document_text"])
async def test_cancellation_stops_search_and_read_tasks(blocked_tool):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def invoke(server, tool, payload, **kwargs):
        if tool == blocked_tool:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        if tool == "consultant_card_search":
            return [card(81)]
        if tool == "consultant_doc_info":
            return metadata(81)
        raise AssertionError(tool)

    session = SimpleNamespace(invoke=AsyncMock(side_effect=invoke))
    task = asyncio.create_task(collect(CompetenceRefs(cited_acts=(law(81),)), session))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_fast_source_is_read_while_other_card_search_is_pending():
    reading_started = asyncio.Event()
    slow_search_started = asyncio.Event()

    async def invoke(server, tool, payload, **kwargs):
        if tool == "consultant_card_search":
            number = int(payload["number"].split("-")[0])
            if number == 82:
                slow_search_started.set()
                await reading_started.wait()
            return [card(number)]
        if tool == "consultant_doc_info":
            return metadata(payload["doc_num"])
        if tool == "consultant_get_document_text":
            if payload["doc_num"] == 81:
                await slow_search_started.wait()
                reading_started.set()
            return {"segment": f"Полномочия органа: {payload['doc_num']}."}
        raise AssertionError(tool)

    session = SimpleNamespace(invoke=AsyncMock(side_effect=invoke))
    result = await asyncio.wait_for(collect(CompetenceRefs(cited_acts=(law(82), law(81))), session), 1)
    assert reading_started.is_set()
    assert [reading.ref.number for reading in result.readings] == ["82-ФЗ", "81-ФЗ"]
    assert all(reading.status is ReadingStatus.COMPLETE for reading in result.readings)
