import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.schemas.legal.discovery import DiscoveryOutcome
from src.agents.schemas.legal.norm import Norm
from src.agents.schemas.legal.plan import SelectedAct, SourceSelection
from src.agents.schemas.legal_base.source_refs import CARD_VID_BY_KIND
from src.agents.steps import retrieve_legal_sources_node as retrieval
from src.agents.steps_tools.compliance.plan import prepare_candidates, project_issuer_key
from src.agents.steps_tools.sources import raw_cache
from src.agents.steps_tools.sources.trust import trusted_currentness_scope
from src.common.config.config import settings

REGION = "Ульяновской области"
ISSUER = "Министерство социального развития Ульяновской области"


def law(doc_num=1):
    raw = f"Федеральный закон от 19.05.1995 № {doc_num}-ФЗ"
    return Norm(
        kind="federal_law",
        number=f"{doc_num}-ФЗ",
        date="19.05.1995",
        title=f'{raw} "О поддержке семей"',
        raw=raw,
        bank="LAW",
        doc_num=doc_num,
        origin=SourceOrigin.BODY,
        role=SourceRole.CITED,
    )


def order(doc_num=2, issuer=ISSUER):
    raw = f"Приказ {issuer} от 19.05.1995 № {doc_num}-п"
    return Norm(
        kind="regional_order",
        number=f"{doc_num}-п",
        date="19.05.1995",
        title=f'{raw} "О порядке поддержки семей"',
        raw=raw,
        bank="RLAW076",
        doc_num=doc_num,
        origin=SourceOrigin.BODY,
        role=SourceRole.CITED,
    )


def project(norms, *, issuer=ISSUER, sessions=None):
    return {
        "document_text": "Проект о поддержке семей.\n" + "\n".join(ref.raw for ref in norms),
        "competence_refs": SimpleNamespace(issuer=issuer, region=REGION, project_title="О поддержке семей"),
        "named_acts": [],
        "search_queries": [],
        "mcp_request_sessions": sessions,
    }


def patch_search(monkeypatch, norms, discovery=()):
    monkeypatch.setattr(retrieval, "search_named_acts", AsyncMock(return_value=(norms, [], [])))
    monkeypatch.setattr(retrieval, "search_discovery", AsyncMock(return_value=DiscoveryOutcome(norms=list(discovery))))
    generate = AsyncMock(
        return_value=SimpleNamespace(
            value=SourceSelection(
                selected=[
                    SelectedAct(candidate_id=i, project_quote="Проект", reason="Условия услуги.")
                    for i in range(1, len(norms) + 1)
                ]
            )
        )
    )
    monkeypatch.setattr(retrieval, "llm", SimpleNamespace(generate_structured=generate))
    return generate


def consultant(monkeypatch, tmp_path, norms, *, failed=(), blocked=()):
    """MCP отдаёт метаданные и текст; загрузчик, классификация и проверка редакции — настоящие."""
    by_id = {ref.doc_num: ref for ref in norms}

    async def invoke(server, tool, payload, **kwargs):
        doc_num = payload["doc_num"]
        if doc_num in failed:
            raise TimeoutError("consultant unavailable")
        if tool == "consultant_doc_info":
            ref = by_id[doc_num]
            return {
                "title": ref.title,
                "numbers": [ref.number],
                "dates": [ref.date],
                "par_count": 1,
                "edition": "01.09.2026",
                "in_force": True,
                "status_search": "Все акты, кроме утративших силу",
            }
        if tool == "consultant_get_document_text":
            if doc_num in blocked:
                await asyncio.Event().wait()
            return {"segment": f"Правило поддержки семей. Источник {doc_num}."}
        raise AssertionError(f"Повторный поиск или неожиданный инструмент: {tool}")

    monkeypatch.setattr(raw_cache, "_CACHE_DIR", tmp_path)
    return SimpleNamespace(invoke=AsyncMock(side_effect=invoke))


@pytest.mark.parametrize(
    "updates, reason",
    [
        ({"date": "неизвестна"}, "missing_or_invalid_requisites"),
        ({"raw": "Федеральный закон от 19.05.1995 № 999-ФЗ"}, "quote_or_requisites_not_in_project"),
        ({"bank": "ARB"}, "не законодательство"),
        ({"bank": "RLAW049", "origin": SourceOrigin.SEARCH}, "другого субъекта"),
    ],
)
async def test_known_rejections_do_not_call_model_or_consultant(monkeypatch, updates, reason):
    original = law()
    rejected = original.model_copy(update=updates)
    generate = patch_search(monkeypatch, [rejected])
    sessions = SimpleNamespace(invoke=AsyncMock(side_effect=AssertionError("Не нужно читать исключённый акт")))
    monkeypatch.setattr(retrieval, "get_prompt", lambda *args: pytest.fail("Шаблон не нужен без кандидатов"))
    result = (await retrieval.retrieve_legal_sources_node(project([original], sessions=sessions)))["legal_search_result"]

    generate.assert_not_awaited()
    sessions.invoke.assert_not_awaited()
    assert not result.plan.sources and not result.plan.context_sources
    assert result.plan.norms == [rejected]
    assert result.plan.dropped == 1
    assert result.plan.decisions[0].outcome == "dropped"
    assert reason in result.plan.decisions[0].reason
    assert result.plan.limitations


@pytest.mark.parametrize("failed", [(), (1,), (2,)])
async def test_sources_and_context_share_one_read_without_losing_partial_results(monkeypatch, tmp_path, failed):
    higher, related = law(), order()
    found = related.model_copy(update={"origin": SourceOrigin.SEARCH, "search_query": "порядок поддержки семей"})
    generate = patch_search(monkeypatch, [higher, related], [found])
    sessions = consultant(monkeypatch, tmp_path, [higher, related], failed=failed)
    read = AsyncMock(wraps=retrieval.read_legal_sources)
    monkeypatch.setattr(retrieval, "read_legal_sources", read)

    with trusted_currentness_scope():
        result = (await retrieval.retrieve_legal_sources_node(project([higher, related], sessions=sessions)))[
            "legal_search_result"
        ]

    generate.assert_awaited_once()  # Основные и контекстные источники отбираются совместно.
    read.assert_awaited_once()
    assert [item.ref.doc_num for item in read.call_args.args[1]] == [1, 2]
    assert len(result.plan.norms) == 2
    assert result.plan.norms[1].search_query == found.search_query
    assert [item.planned.ref.doc_num for item in result.sources.readings] == [1]
    assert [item.planned.ref.doc_num for item in result.context.readings] == [2]
    text_calls = Counter(
        call.args[2]["doc_num"] for call in sessions.invoke.call_args_list if call.args[1] == "consultant_get_document_text"
    )
    assert text_calls == Counter({doc_num: 1 for doc_num in (1, 2) if doc_num not in failed})
    for doc_num, group in [(1, result.sources), (2, result.context)]:
        expected = ReadingStatus.TRANSPORT_FAILURE if doc_num in failed else ReadingStatus.COMPLETE
        assert group.readings[0].reading.status is expected
        assert bool(group.limitations) == (doc_num in failed)
        assert bool(group.with_text) == (doc_num not in failed)
    assert not result.plan.limitations


@pytest.mark.parametrize("issuer", ["", "Министерство финансов Ульяновской области"])
async def test_regional_order_does_not_imply_same_legal_force(monkeypatch, tmp_path, issuer):
    ref = order()
    generate = patch_search(monkeypatch, [ref])
    sessions = consultant(monkeypatch, tmp_path, [ref])
    with trusted_currentness_scope():
        result = (await retrieval.retrieve_legal_sources_node(project([ref], issuer=issuer, sessions=sessions)))[
            "legal_search_result"
        ]

    generate.assert_awaited_once()
    assert result.plan.sources[0].ref == ref
    assert not result.plan.context_sources and not result.context.readings


async def test_context_only_is_selected_jointly(monkeypatch, tmp_path):
    ref = order()
    generate = patch_search(monkeypatch, [ref])
    sessions = consultant(monkeypatch, tmp_path, [ref])
    with trusted_currentness_scope():
        result = (await retrieval.retrieve_legal_sources_node(project([ref], sessions=sessions)))["legal_search_result"]
    generate.assert_awaited_once()
    assert not result.sources.readings
    assert result.context.readings[0].reading.status is ReadingStatus.COMPLETE
    assert result.plan.decisions[0].outcome == "context"


async def test_context_cannot_bypass_shared_read_limit(monkeypatch, tmp_path):
    higher, first, second = law(), order(2), order(3)
    generate = patch_search(monkeypatch, [first, second, higher])
    sessions = consultant(monkeypatch, tmp_path, [higher, first, second])
    monkeypatch.setitem(settings["agents.legal"], "max_acts", 2)
    with trusted_currentness_scope():
        result = (await retrieval.retrieve_legal_sources_node(project([higher, first, second], sessions=sessions)))[
            "legal_search_result"
        ]
    generate.assert_awaited_once()
    assert result.sources.readings[0].planned.ref == higher
    assert [item.planned.ref for item in result.context.readings] == [first]
    assert result.plan.dropped == 1
    assert result.plan.decisions[-1].outcome == "deferred"
    assert result.plan.decisions[-1].ref == second
    assert any("Контекстный акт отложен" in text for text in result.plan.limitations)
    assert {call.args[2]["doc_num"] for call in sessions.invoke.call_args_list} == {1, 2}


async def test_context_and_sources_share_deadline(monkeypatch, tmp_path):
    higher, related = law(), order()
    patch_search(monkeypatch, [higher, related])
    sessions = consultant(monkeypatch, tmp_path, [higher, related], blocked=(2,))
    monkeypatch.setitem(settings["agents.legal"], "read_timeout_s", 0.05)
    read = AsyncMock(wraps=retrieval.read_legal_sources)
    monkeypatch.setattr(retrieval, "read_legal_sources", read)
    with trusted_currentness_scope():
        result = (await retrieval.retrieve_legal_sources_node(project([higher, related], sessions=sessions)))[
            "legal_search_result"
        ]
    read.assert_awaited_once()
    assert read.call_args.kwargs["timeout_s"] == 0.05
    assert result.sources.readings[0].reading.status is ReadingStatus.COMPLETE
    assert not result.sources.limitations
    assert result.context.readings[0].reading.status is ReadingStatus.DEADLINE
    assert "дедлайн" in result.context.limitations[0]


def test_repealed_same_issuer_act_is_not_procedure_context():
    ref = order()
    targets = frozenset({("2-п", "19.05.1995", CARD_VID_BY_KIND["regional_order"], "социальногоразвитияульяновскойобласти")})
    candidates, context, decisions, limitations = prepare_candidates(
        [ref],
        ref.raw,
        project_issuer=project_issuer_key(ISSUER, REGION),
        region=REGION,
        repeal_targets=targets,
    )
    assert not candidates and not context
    assert decisions[0].outcome == "dropped"
    assert not limitations
