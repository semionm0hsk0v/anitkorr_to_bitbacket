import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.schemas.legal.discovery import SearchQuery
from src.agents.schemas.legal.inventory import NamedAct
from src.agents.schemas.legal.norm import Norm
from src.agents.schemas.legal_base.card_search import CardHit
from src.agents.steps_tools.compliance import search as legal_search


async def test_subject_search_keeps_candidates_without_metadata_requests():
    query = SearchQuery(query="социальная поддержка многодетных семей", reason="Проект регулирует меры поддержки семей.")
    hits = [
        CardHit(bank="LAW", doc_num=1, title='Федеральный закон от 19.05.1995 № 81-ФЗ "О пособиях"'),
        CardHit(bank="RLAW073", doc_num=2, title='Закон Ульяновской области от 09.01.2008 № 1-ЗО "О поддержке семей"'),
    ]
    invoke = AsyncMock(return_value=[hit.model_dump() for hit in hits])
    result = await legal_search.search_discovery([query], SimpleNamespace(invoke=invoke))

    invoke.assert_awaited_once_with(
        "consultant",
        "consultant_quick_search",
        {"query": query.query, "max_results": 10, "get_title": True},
        timeout_s=60,
    )
    assert {(norm.bank, norm.doc_num) for norm in result.norms} == {("LAW", 1), ("RLAW073", 2)}
    assert not result.limitations


async def test_subject_queries_run_concurrently_and_overlaps_are_merged(monkeypatch):
    second_started = asyncio.Event()
    first = SearchQuery(query="поддержка семей", reason="Основания поддержки.")
    second = SearchQuery(query="выплаты семьям", reason="Размер выплат.")
    hit = CardHit(bank="LAW", doc_num=1, title='Федеральный закон от 19.05.1995 № 81-ФЗ "О пособиях"')

    async def search(sessions, text, *, limit):
        if text == first.query:
            await asyncio.wait_for(second_started.wait(), 1)
        else:
            second_started.set()
        return [hit]

    lookup = AsyncMock(side_effect=search)
    monkeypatch.setattr(legal_search, "quick_search", lookup)
    result = await legal_search.search_discovery([first, second, first], object())
    assert lookup.await_count == 2
    assert len(result.norms) == 1
    assert result.norms[0].search_query == first.query + "\n" + second.query
    assert result.norms[0].search_reason == first.reason + "\n" + second.reason
    assert "status_search" not in result.norms[0].model_dump()


async def test_subject_search_preserves_successes_when_other_requests_fail(monkeypatch):
    first = SearchQuery(query="поддержка семей", reason="Основания поддержки.")
    second = SearchQuery(query="выплаты семьям", reason="Размер выплат.")

    async def search(sessions, text, *, limit):
        if text == first.query:
            raise TimeoutError()
        return [CardHit(bank="LAW", doc_num=1, title='Федеральный закон от 19.05.1995 № 81-ФЗ "О пособиях"')]

    monkeypatch.setattr(legal_search, "quick_search", search)
    result = await legal_search.search_discovery([first, second], object())
    assert len(result.norms) == 1
    assert any("Сбой предметного" in limit for limit in result.limitations)


async def test_named_search_parallelizes_acts_and_deduplicates_identical_requests():
    both_started = asyncio.Event()
    seen = set()

    async def invoke(server, tool, payload, **kwargs):
        seen.add(payload["number"])
        if len(seen) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        return [
            {
                "bank": "LAW",
                "doc_num": int(payload["number"].split("-")[0]),
                "title": f'Федеральный закон от 19.05.1995 № {payload["number"]} "О пособиях"',
            }
        ]

    acts = [
        NamedAct(
            role="cited",
            origin="body",
            kind="federal_law",
            number=f"{num}-ФЗ",
            date="19.05.1995",
            raw=f"{'Федеральным законом' if index else 'Федеральный закон'} от 19.05.1995 № {num}-ФЗ",
            quote=f"Цитата {index}.",
        )
        for index, num in enumerate([81, 82, 81])
    ]
    underlying = SimpleNamespace(invoke=AsyncMock(side_effect=invoke))
    norms, missing, limits = await legal_search.search_named_acts(acts, underlying, region="")
    assert not missing and not limits
    assert [norm.doc_num for norm in norms] == [81, 82, 81]
    assert underlying.invoke.await_count == 2  # Exact matches stop the fallback ladder.
    merged = legal_search.unique_norms(norms)
    assert len(merged) == 2
    assert acts[0].quote in merged[0].quote and acts[2].quote in merged[0].quote


async def test_named_fallback_is_sequential_and_keeps_original_act_date(monkeypatch):
    act = NamedAct(role="cited", origin="body", kind="federal_law", number="81-ФЗ", date="19.05.1995")
    hits = [
        CardHit(bank="LAW", doc_num=num, title=f'Федеральный закон от {date} № 81-ФЗ "О пособиях"')
        for num, date in [(1, "19.05.2000"), (2, "19.05.1995")]
    ]
    search = AsyncMock(side_effect=[[], hits])
    monkeypatch.setattr(legal_search, "card_search", search)
    norms, missing, limits = await legal_search.search_named_acts([act], object(), region="")
    assert not missing and not limits
    assert [norm.doc_num for norm in norms] == [2]
    assert search.await_count == 2
    assert search.await_args_list[0].args[1].date == "19.05.1995"
    assert search.await_args_list[1].args[1].date == ""


@pytest.mark.parametrize(
    ("first_fields", "second_fields", "expected_calls"),
    [
        (
            {"number": "81-ФЗ"},
            {"number": "81фз", "date": "1.4.2024", "title": "Другое написание названия", "raw": "Иная ссылка"},
            1,
        ),
        ({}, {"number": "82-ФЗ"}, 2),
        ({}, {"date": "02.04.2024"}, 2),
        ({}, {"kind": "rf_law"}, 2),
        (
            {"kind": "regional_law", "raw": "Закон Ульяновской области от 01.04.2024 № 81"},
            {"kind": "regional_law", "raw": "Закон от 01.04.2024 № 81"},
            1,
        ),
        (
            {"kind": "regional_law", "raw": "Закон Ульяновской области от 01.04.2024 № 81"},
            {"kind": "regional_law", "raw": "Закон Самарской области от 01.04.2024 № 81"},
            2,
        ),
        (
            {"kind": "federal_order", "raw": "Приказ Минтруда России от 01.04.2024 № 81"},
            {"kind": "federal_order", "raw": "Приказ Министерства труда Российской Федерации от 01.04.2024 № 81"},
            1,
        ),
        (
            {"kind": "federal_order", "raw": "Приказ Минтруда России от 01.04.2024 № 81"},
            {"kind": "federal_order", "raw": "Приказ Минфина России от 01.04.2024 № 81"},
            2,
        ),
        (
            {"kind": "federal_order", "raw": "Неустановленный издатель"},
            {"kind": "federal_order", "raw": "Другой неустановленный издатель"},
            2,
        ),
        (
            {"kind": "regional_directive", "raw": "Распоряжение Губернатора Ульяновской области от 01.04.2024 № 81"},
            {"kind": "regional_directive", "raw": "Распоряжение Правительства Ульяновской области от 01.04.2024 № 81"},
            2,
        ),
    ],
    ids=[
        "wording",
        "number",
        "date",
        "kind",
        "implicit_region",
        "different_regions",
        "issuer_alias",
        "different_issuers",
        "unknown_issuers",
        "directive_issuers",
    ],
)
async def test_named_search_groups_requisites_without_merging_distinct_acts(
    monkeypatch, first_fields, second_fields, expected_calls
):

    defaults = {"kind": "federal_law", "number": "81", "date": "01.04.2024", "role": "cited", "origin": "body"}
    acts = [
        NamedAct(**(defaults | first_fields | {"quote": "Первая цитата."})),
        NamedAct(**(defaults | second_fields | {"quote": "Вторая цитата.", "origin": "preamble", "role": "target"})),
    ]
    calls = []

    async def find(act, sessions, *, region):
        calls.append(act)
        return [Norm(**act.model_dump(), bank="LAW", doc_num=len(calls))], [], []

    search = AsyncMock(side_effect=find)
    monkeypatch.setattr(legal_search, "_search_named_act", search)
    norms, missing, limits = await legal_search.search_named_acts(acts, object(), region="Ульяновская область")

    assert search.await_count == expected_calls
    assert not missing and not limits
    assert [norm.quote for norm in norms] == [act.quote for act in acts]
    assert [norm.origin for norm in norms] == [act.origin for act in acts]
    assert [norm.role for norm in norms] == [act.role for act in acts]
    assert len(legal_search.unique_norms(norms)) == expected_calls


async def test_empty_subject_search_needs_no_live_session(monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr(legal_search, "quick_search", search)
    result = await legal_search.search_discovery([], None)
    assert not result.norms
    assert result.limitations == ("Предметный поиск не выполнялся: запросы отсутствуют.",)
    search.assert_not_awaited()


async def test_lost_session_is_a_search_limitation(monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr(legal_search, "quick_search", search)
    session = SimpleNamespace(live_session_count=lambda server: 0)
    result = await legal_search.search_discovery([SearchQuery(query="поддержка семей", reason="Основания.")], session)
    assert not result.norms
    assert any("McpSessionUnavailableError" in limitation for limitation in result.limitations)
    search.assert_not_awaited()


async def test_broader_query_is_searched_independently_and_deduplicated(monkeypatch):
    specific = SearchQuery(
        query="субсидия молодым семьям документы", general_terms=["субсидия", "семьям", "правила"], reason="Условия выплаты."
    )
    repeated = SearchQuery(query=" ".join(specific.general_terms), reason="Порядок субсидии.")
    hit = CardHit(bank="LAW", doc_num=1, title='Постановление Правительства РФ от 01.04.2024 № 99 "О субсидиях"')
    lookup = AsyncMock(return_value=[hit])
    monkeypatch.setattr(legal_search, "quick_search", lookup)
    result = await legal_search.search_discovery([specific, repeated], object())
    assert [call.args[1] for call in lookup.call_args_list] == [specific.query, " ".join(specific.general_terms)]
    assert len(result.norms) == 1
    assert result.norms[0].search_query == specific.query + "\n" + " ".join(specific.general_terms)
    assert result.norms[0].search_reason == specific.reason + "\n" + repeated.reason
    assert not result.limitations


async def test_broader_query_survives_failure_of_specific_search(monkeypatch):
    query = SearchQuery(
        query="субсидия молодым семьям документы", general_terms=["субсидия", "семьям", "правила"], reason="Условия выплаты."
    )

    async def lookup(sessions, text, *, limit):
        if text == query.query:
            raise TimeoutError()
        return [CardHit(bank="LAW", doc_num=1, title='Постановление Правительства РФ от 01.04.2024 № 99 "О субсидиях"')]

    monkeypatch.setattr(legal_search, "quick_search", lookup)
    result = await legal_search.search_discovery([query], object())
    assert len(result.norms) == 1
    assert result.norms[0].search_query == " ".join(query.general_terms)
    assert any("Сбой предметного" in text for text in result.limitations)


async def test_subject_query_can_include_numeric_conditions(monkeypatch):
    query = SearchQuery(query="труд несовершеннолетних до 18 лет", reason="Возрастные условия работы.")
    lookup = AsyncMock(return_value=[])
    monkeypatch.setattr(legal_search, "quick_search", lookup)
    result = await legal_search.search_discovery([query], object())
    lookup.assert_awaited_once_with(lookup.call_args.args[0], query.query, limit=10)
    assert not result.norms and not result.limitations


async def test_general_search_relaxes_qualifiers_but_keeps_core_and_precise_query(monkeypatch):
    query = SearchQuery(
        query="специальные требования к документам",
        reason="Документы заявителя.",
        core_term="порядок",
        general_terms=["выплата", "субсидия", "регион"],
    )
    lookup = AsyncMock(return_value=[])
    monkeypatch.setattr(legal_search, "quick_search", lookup)
    await legal_search.search_discovery([query], object())
    assert [call.args[1] for call in lookup.call_args_list] == [
        query.query,
        "порядок выплата субсидия регион",
        "порядок выплата субсидия",
        "порядок выплата регион",
        "порядок субсидия регион",
        "порядок выплата",
        "порядок субсидия",
        "порядок регион",
    ]
    assert [call.kwargs["limit"] for call in lookup.call_args_list] == [10, 10, 5, 5, 5, 5, 5, 5]
