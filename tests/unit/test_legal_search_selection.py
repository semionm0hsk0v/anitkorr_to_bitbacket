import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.llm.errors import LLMGenerationError
from src.agents.schemas.competence.parse import CompetenceParse
from src.agents.schemas.legal.discovery import DiscoveryOutcome, DiscoveryPlan, ReviewQuestion, SearchQuery
from src.agents.schemas.legal.norm import Norm
from src.agents.schemas.legal.plan import NamedActChoice, PlannedSource, SelectedAct, SourceSelection
from src.agents.schemas.legal.sources import LegalReading, LegalSources
from src.agents.schemas.legal_base.retrieval import RawAct
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.agents.steps import plan_legal_search_node as preparation, retrieve_legal_sources_node as retrieval
from src.agents.steps_tools.authority.requisites import refs_from_model
from src.agents.steps_tools.compliance.plan import (
    accept_selection,
    accept_source_selection,
    prepare_candidates,
    project_blocks,
    project_issuer_key,
    selection_material,
    selection_quote_matches,
    title_mentions,
)
from src.agents.steps_tools.sources import source_reading
from src.common.config.config import settings


def chosen(*ids, quote="Проект", reason="Самостоятельные условия поддержки."):
    return SourceSelection(selected=[SelectedAct(candidate_id=i, project_quote=quote, reason=reason) for i in ids])


def candidate(doc_num, origin):
    return Norm(
        kind="federal_law",
        number=f"{doc_num}-ФЗ",
        date="19.05.1995",
        title="О социальной поддержке",
        bank="LAW",
        doc_num=doc_num,
        origin=origin,
        role=SourceRole.CITED,
    )


@pytest.mark.parametrize("discovery_result", ["empty_queries", "failed", "partial", "success"])
async def test_available_candidates_always_reach_model_selection(monkeypatch, discovery_result):
    named = candidate(1, SourceOrigin.BODY)
    additional = candidate(2, SourceOrigin.SEARCH)
    queries = [SearchQuery(query="социальная поддержка семей", reason="Проверить основания поддержки.")]
    named_search = AsyncMock(return_value=([named], [], []))
    subject_search = AsyncMock(
        return_value=DiscoveryOutcome(
            norms=[additional],
            limitations=("Часть запросов не выполнена.",) if discovery_result == "partial" else (),
        )
    )
    if discovery_result == "empty_queries":
        subject_search.return_value = DiscoveryOutcome(limitations=("Предметный поиск не выполнялся: запросы отсутствуют.",))
    elif discovery_result == "failed":
        subject_search.return_value = DiscoveryOutcome(limitations=("Сбой предметного поиска L1 (TimeoutError).",))

    selected_ids = (1, 2) if discovery_result in {"partial", "success"} else (1,)
    selection = AsyncMock(return_value=SimpleNamespace(value=chosen(*selected_ids)))
    monkeypatch.setattr(retrieval, "search_named_acts", named_search)
    monkeypatch.setattr(retrieval, "search_discovery", subject_search)
    monkeypatch.setattr(retrieval, "llm", SimpleNamespace(generate_structured=selection))
    monkeypatch.setattr(retrieval, "read_legal_sources", AsyncMock(return_value=LegalSources(readings=(), limitations=())))

    delta = await retrieval.retrieve_legal_sources_node(
        {
            "document_text": "Проект о социальной поддержке семей.",
            "named_acts": [],
            "search_queries": [] if discovery_result == "empty_queries" else queries,
        }
    )
    result = delta["legal_search_result"].plan

    expected = [named, additional] if discovery_result in {"partial", "success"} else [named]
    selection.assert_awaited_once()

    messages = selection.call_args.kwargs["messages"]
    data = json.loads(messages[1]["content"].split("<inventory>")[1].split("</inventory>")[0])
    assert [row["number"] for row in data] == [item.number for item in expected]
    assert [source.ref for source in result.sources] == expected
    assert result.norms == expected
    assert result.source == "model"
    if discovery_result == "empty_queries":
        subject_search.assert_awaited_once_with([], None)
        assert "Предметный поиск не выполнялся: запросы отсутствуют." in result.limitations
    elif discovery_result == "failed":
        assert "Сбой предметного поиска L1 (TimeoutError)." in result.limitations
    elif discovery_result == "partial":
        assert "Часть запросов не выполнена." in result.limitations
    else:
        assert not result.limitations


async def test_selection_fallback_preserves_both_search_results(monkeypatch):
    candidates = [candidate(1, SourceOrigin.BODY), candidate(2, SourceOrigin.SEARCH)]
    generate = AsyncMock(return_value=SimpleNamespace(value=chosen(1, reason="Нет данных.")))
    generate.side_effect = LLMGenerationError("timeout", ())
    monkeypatch.setattr(retrieval, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(retrieval, "search_named_acts", AsyncMock(return_value=(candidates, [], [])))
    read = AsyncMock(return_value=LegalSources(readings=(), limitations=()))
    monkeypatch.setattr(retrieval, "read_legal_sources", read)

    delta = await retrieval.retrieve_legal_sources_node(
        {"document_text": "Проект о социальной поддержке семей.", "named_acts": [], "search_queries": []}
    )
    result = delta["legal_search_result"].plan
    assert [source.ref for source in read.call_args.args[1]] == candidates

    assert result.source == "parser_fallback"
    assert [source.ref for source in result.sources] == candidates
    assert all(isinstance(source.ref, Norm) for source in result.sources)
    assert [(decision.ref, decision.outcome) for decision in result.decisions] == [(ref, "accepted") for ref in candidates]
    assert result.limitations == ("Предметный поиск не выполнялся: запросы отсутствуют.",)


async def test_preparation_failure_keeps_named_acts_without_extra_state(monkeypatch):
    document = "В соответствии с Федеральным законом от 19.05.1995 № 81-ФЗ."
    monkeypatch.setattr(preparation.llm, "generate_structured", AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(preparation, "emit_ui_event", lambda *args, **kwargs: None)

    result = await preparation.plan_legal_search_node({"document_text": document})

    assert result["named_acts"]
    assert result["search_queries"] == []
    assert result["legal_questions"] == []
    assert set(result) == {"named_acts", "search_queries", "legal_questions"}


async def test_discovery_receives_project_metadata_and_note_text(monkeypatch):
    title = "О социальной поддержке семей"
    region = "Ульяновской области"
    issuer = "Министерство социального развития Ульяновской области"
    document = f"{issuer}\n{title}\nВ соответствии с Федеральным законом от 19.05.1995 № 81-ФЗ."
    note = "Проект разработан для поддержки семей."
    questions = [ReviewQuestion(question="Порядок обращения за поддержкой.", project_blocks=["B1"])]
    objects = [ReviewQuestion(question="Актуальны ли нормативные наименования объектов?", project_blocks=["B1"])]
    generate = AsyncMock(return_value=SimpleNamespace(value=DiscoveryPlan(queries=[], questions=questions, objects=objects)))
    monkeypatch.setattr(preparation.llm, "generate_structured", generate)
    monkeypatch.setattr(preparation, "emit_ui_event", lambda *args, **kwargs: None)

    result = await preparation.plan_legal_search_node(
        {
            "document_text": document,
            "analysis_date": "2026-10-10",
            "competence_refs": SimpleNamespace(project_title=title, region=region, issuer=issuer),
            "context_documents": [{"name": "unused-filename.docx", "kind": "explanatory_note", "text": note}],
        }
    )

    generate.assert_awaited_once()
    assert generate.call_args.kwargs["role"] == "legal_plan"
    assert generate.call_args.kwargs["operation"] == "legal_discovery"
    messages = generate.call_args.kwargs["messages"]
    text = "\n".join(m["content"] for m in messages if m["role"] == "user")
    for tag, value in [("project_title", title), ("region", region), ("issuer", issuer), ("analysis_date", "2026-10-10")]:
        assert text.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0].strip().splitlines()[-1] == value
    inventory = text.split("<mentioned_acts>", 1)[1].split("</mentioned_acts>", 1)[0]
    assert "- Федеральным законом от 19.05.1995 № 81-ФЗ" in inventory
    assert '"kind"' not in inventory and '"quote"' not in inventory
    assert result["named_acts"]
    assert result["legal_questions"] == [*questions, *objects]
    assert note in text and "unused-filename.docx" not in text
    assert "{{" not in text


async def test_selection_receives_questions_even_without_additional_search(monkeypatch):
    document = "1. Заявитель вправе подать заявление через представителя."
    questions = [ReviewQuestion(question="Допустимые способы обращения.", project_blocks=["B1"])]
    generate = AsyncMock(return_value=SimpleNamespace(value=chosen(1, quote="через представителя")))
    monkeypatch.setattr(retrieval, "search_named_acts", AsyncMock(return_value=([candidate(1, SourceOrigin.BODY)], [], [])))
    monkeypatch.setattr(retrieval, "search_discovery", AsyncMock(return_value=DiscoveryOutcome()))
    monkeypatch.setattr(retrieval, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(retrieval, "read_legal_sources", AsyncMock(return_value=LegalSources(readings=(), limitations=())))
    await retrieval.retrieve_legal_sources_node(
        {"document_text": document, "named_acts": [], "search_queries": [], "legal_questions": questions}
    )
    data = generate.call_args.kwargs["messages"][1]["content"]
    assert "Допустимые способы обращения." in data
    assert document in data
    assert "⟦B1⟧" in data


@pytest.mark.parametrize(
    "title, expected",
    [
        ("О социальной\nподдержке семей", "О социальной поддержке семей"),
        ("Выдуманное название", ""),
    ],
)
def test_project_title_must_be_present_in_header(title, expected):
    parsed = CompetenceParse(
        project_title=title, issuer="", region="", form="приказ", action="establish", target_act=None, cited_acts=[]
    )
    refs, _ = refs_from_model(parsed, "ПРИКАЗ\nО социальной\nподдержке семей\nприказываю:")
    assert refs.project_title == expected


@pytest.mark.parametrize("status", ["complete", "not_in_force"])
async def test_retrieval_keeps_search_inventory_separate_from_reading(monkeypatch, status):
    named = candidate(1, SourceOrigin.BODY).model_copy(update={"quote": "Ссылка проекта."})
    found = candidate(1, SourceOrigin.SEARCH).model_copy(update={"search_query": "поддержка семей"})
    generate = AsyncMock(return_value=SimpleNamespace(value=chosen(1, reason="Определяет условия поддержки.")))
    monkeypatch.setattr(retrieval, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(retrieval, "search_named_acts", AsyncMock(return_value=([named], [], [])))
    monkeypatch.setattr(retrieval, "search_discovery", AsyncMock(return_value=DiscoveryOutcome(norms=[found])))

    async def read(sessions, sources, **kwargs):
        assert len(sources) == 1
        source = sources[0]
        raw = RawAct(query="", base="LAW", doc_num=1, title="О поддержке", full_text="Текст акта.", status_search=status)
        return LegalSources(
            readings=(
                LegalReading(
                    planned=source,
                    reading=SourceReading(ref=source.ref, status=ReadingStatus(status), raw=raw),
                ),
            ),
            limitations=(),
        )

    monkeypatch.setattr(retrieval, "read_legal_sources", read)
    delta = await retrieval.retrieve_legal_sources_node(
        {
            "document_text": "Проект о поддержке семей.",
            "analysis_date": "2026-10-10",
            "competence_refs": SimpleNamespace(project_title="О поддержке", region="Ульяновской области", issuer="Правительство"),
            "context_documents": [{"text": "Причины подготовки проекта.", "name": "unused.docx"}],
            "named_acts": [],
            "search_queries": [SearchQuery(query="поддержка семей", reason="Условия поддержки.")],
        }
    )
    result = delta["legal_search_result"]
    assert set(delta) == {"legal_search_result"}
    assert len(result.plan.norms) == 1
    norm = result.plan.norms[0]
    assert norm.quote == named.quote and norm.search_query == found.search_query
    assert "status_search" not in norm.model_dump()
    assert result.plan.sources[0].ref == norm
    reading = result.sources.readings[0].reading
    assert reading.status == ReadingStatus(status)
    assert reading.raw.status_search == status
    assert reading.model_text() == ("Текст акта." if status == "complete" else "")
    call = generate.call_args.kwargs
    assert call["prompt"].name == "legal_selection" and call["schema"] is SourceSelection
    assert call["role"] == "legal_selection" and call["operation"] == "legal_selection"
    system, user = call["messages"]
    assert "status_search" not in user["content"]
    assert system["role"] == "system" and user["role"] == "user"
    for tag, value in [
        ("project_title", "О поддержке"),
        ("issuer", "Правительство"),
        ("region", "Ульяновской области"),
        ("analysis_date", "2026-10-10"),
        ("explanatory_note", "Причины подготовки проекта."),
    ]:
        assert value in user["content"].split(f"<{tag}>")[1].split(f"</{tag}>")[0]
        assert value not in system["content"]
    assert "unused.docx" not in user["content"] and "{{" not in user["content"]


async def test_retrieval_does_not_hide_reading_programming_errors(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(value=chosen(1, reason="Нет данных.")))
    monkeypatch.setattr(retrieval, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(retrieval, "search_named_acts", AsyncMock(return_value=([], [], [])))
    monkeypatch.setattr(retrieval, "read_legal_sources", AsyncMock(side_effect=ValueError("broken reader")))
    with pytest.raises(ValueError, match="broken reader"):
        await retrieval.retrieve_legal_sources_node({"document_text": "Проект.", "named_acts": [], "search_queries": []})


async def test_named_and_subject_search_start_concurrently(monkeypatch):
    named_started = asyncio.Event()
    subject_started = asyncio.Event()

    async def named(*args, **kwargs):
        named_started.set()
        await asyncio.wait_for(subject_started.wait(), 1)
        return [], [], []

    async def subject(*args, **kwargs):
        subject_started.set()
        await asyncio.wait_for(named_started.wait(), 1)
        return DiscoveryOutcome()

    monkeypatch.setattr(retrieval, "search_named_acts", named)
    monkeypatch.setattr(retrieval, "search_discovery", subject)
    monkeypatch.setattr(
        retrieval,
        "llm",
        SimpleNamespace(generate_structured=AsyncMock(return_value=SimpleNamespace(value=chosen(1, reason="Нет данных.")))),
    )
    monkeypatch.setattr(retrieval, "read_legal_sources", AsyncMock(return_value=LegalSources(readings=(), limitations=())))
    await retrieval.retrieve_legal_sources_node(
        {
            "document_text": "Проект.",
            "named_acts": [],
            "search_queries": [SearchQuery(query="поддержка семей", reason="Основания.")],
        }
    )
    assert named_started.is_set() and subject_started.is_set()


async def test_discovery_programming_error_is_not_hidden(monkeypatch):
    monkeypatch.setattr(retrieval, "search_named_acts", AsyncMock(return_value=([], [], [])))
    monkeypatch.setattr(retrieval, "search_discovery", AsyncMock(side_effect=ValueError("broken search")))
    with pytest.raises(ValueError, match="broken search"):
        await retrieval.retrieve_legal_sources_node(
            {
                "document_text": "Проект.",
                "named_acts": [],
                "search_queries": [SearchQuery(query="поддержка семей", reason="Основания.")],
            }
        )


async def test_prompt_error_is_not_treated_as_failed_generation(monkeypatch):
    def broken_template(*args):
        raise ValueError("broken prompt")

    generate = AsyncMock()
    monkeypatch.setattr(retrieval, "get_prompt", broken_template)
    monkeypatch.setattr(retrieval, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(retrieval, "search_named_acts", AsyncMock(return_value=([candidate(1, SourceOrigin.BODY)], [], [])))
    with pytest.raises(ValueError, match="broken prompt"):
        await retrieval.retrieve_legal_sources_node({"document_text": "Проект.", "named_acts": [], "search_queries": []})
    generate.assert_not_awaited()


async def test_partial_readings_survive_transport_failure(monkeypatch):
    candidates = [candidate(1, SourceOrigin.BODY), candidate(2, SourceOrigin.BODY)]
    generate = AsyncMock(return_value=SimpleNamespace(value=chosen(1, 2, reason="Те же отношения.")))
    raw = RawAct(query="", base="LAW", doc_num=1, title="О поддержке", full_text="Полученный текст.")

    async def load(sessions, bank, doc_num, *args, **kwargs):
        if doc_num == 2:
            raise TimeoutError()
        return raw

    monkeypatch.setattr(retrieval, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(retrieval, "search_named_acts", AsyncMock(return_value=(candidates, [], [])))
    monkeypatch.setattr(source_reading.retrieval, "load_act", load)
    delta = await retrieval.retrieve_legal_sources_node({"document_text": "Проект.", "named_acts": [], "search_queries": []})
    result = delta["legal_search_result"]
    first, second = result.sources.readings
    assert first.reading.raw == raw
    assert first.reading.status is ReadingStatus.STATUS_ONLY  # Нет подтверждения актуальности — текст модели не передаётся.
    assert second.reading.status is ReadingStatus.TRANSPORT_FAILURE
    assert len(result.plan.sources) == 2
    assert any("сбой обращения" in limitation for limitation in result.sources.limitations)
    assert result.context == LegalSources(readings=(), limitations=())


def patch_selection_inputs(monkeypatch, candidates, generate):
    monkeypatch.setattr(retrieval, "search_named_acts", AsyncMock(return_value=(candidates, [], [])))
    monkeypatch.setattr(retrieval, "search_discovery", AsyncMock(return_value=DiscoveryOutcome()))
    monkeypatch.setattr(retrieval, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(retrieval, "read_legal_sources", AsyncMock(return_value=LegalSources(readings=(), limitations=())))


async def test_empty_inventory_skips_selection(monkeypatch):
    generate = AsyncMock()
    patch_selection_inputs(monkeypatch, [], generate)
    result = await retrieval.retrieve_legal_sources_node({"document_text": "Проект.", "named_acts": [], "search_queries": []})
    generate.assert_not_awaited()
    assert result["legal_search_result"].plan.sources == ()


async def test_joint_selection_preserves_input_order_without_truncating_to_five(monkeypatch):
    candidates = [candidate(i, SourceOrigin.SEARCH) for i in range(1, 8)]
    generate = AsyncMock(return_value=SimpleNamespace(value=chosen(*range(7, 0, -1))))
    patch_selection_inputs(monkeypatch, candidates, generate)
    result = (await retrieval.retrieve_legal_sources_node({"document_text": "Проект.", "named_acts": [], "search_queries": []}))[
        "legal_search_result"
    ].plan
    generate.assert_awaited_once()
    assert [s.ref for s in result.sources] == candidates
    assert not result.limitations  # Все выбранные акты сохранены для проверки.


async def test_selection_cancellation_drains_active_calls(monkeypatch):
    full = asyncio.Event()
    calls = active = 0

    async def generate(**kwargs):
        nonlocal calls, active
        calls += 1
        active += 1
        full.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    patch_selection_inputs(monkeypatch, [candidate(i, SourceOrigin.BODY) for i in range(1, 9)], generate)
    task = asyncio.create_task(
        retrieval.retrieve_legal_sources_node({"document_text": "Проект.", "named_acts": [], "search_queries": []})
    )
    try:
        await asyncio.wait_for(full.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert active == 0 and calls == 1
    retrieval.read_legal_sources.assert_not_awaited()


def test_currentness_exclusion_still_requires_reading():
    norm = candidate(1, SourceOrigin.SEARCH)
    result = accept_selection(
        [NamedActChoice(keep=False, reason="Возможно, утратил силу.")],
        [norm],
    )
    assert result.sources[0].ref == norm
    assert not result.limitations


@pytest.mark.parametrize(
    "raw, title, quote, expected_raw",
    [
        ("", "О поддержке семей", "", None),
        ("О поддержке семей", "О поддержке семей", "", None),
        ("от 19.05.1995 № 1-ФЗ", "О поддержке семей", "Во исполнение акта от 19.05.1995 № 1-ФЗ.", None),
        ("О поддержке\nсемей", "О поддержке семей", "", None),
        (
            "Постановление Правительства Ульяновской области",
            "О поддержке семей",
            "",
            "Постановление Правительства Ульяновской области",
        ),
    ],
)
async def test_candidate_prompt_keeps_only_relevant_fields(monkeypatch, raw, title, quote, expected_raw):
    norm = candidate(1, SourceOrigin.SEARCH).model_copy(
        update={
            "raw": raw,
            "title": title,
            "quote": quote,
            "search_query": "служебный поисковый запрос",
            "search_reason": "служебная причина поиска",
        }
    )
    generate = AsyncMock(return_value=SimpleNamespace(value=chosen(1, reason="Связан с проектом.")))
    patch_selection_inputs(monkeypatch, [norm], generate)
    result = (
        await retrieval.retrieve_legal_sources_node(
            {"document_text": "Проект о поддержке семей.", "named_acts": [], "search_queries": []}
        )
    )["legal_search_result"]
    user = generate.call_args.kwargs["messages"][1]["content"]
    block = user.split("<inventory>")[1].split("</inventory>")[0]
    payload = json.loads(block)[0]
    expected = {
        "candidate_id": 1,
        "purpose": "requirements",
        "kind": norm.kind,
        "number": norm.number,
        "date": norm.date,
        "title": title,
    }
    if quote:
        expected["quote"] = quote
    if expected_raw is not None:
        expected["raw"] = expected_raw
    assert payload == expected
    assert norm.search_reason not in user
    for field in ("bank", "doc_num", "origin", "role", "search_query", "search_reason"):
        assert field not in block
    assert result.plan.norms == [norm]
    assert result.plan.sources[0].ref == norm
    assert retrieval.read_legal_sources.call_args.args[1][0].ref == norm


async def test_compact_card_keeps_full_title_for_reading_and_object_matches(monkeypatch):
    title = (
        "Постановление Правительства РФ от 19.05.1995 N 1\n(ред. от 01.01.2026)\n"
        '"Об утверждении правил предоставления поддержки"\n'
        '(вместе с "Перечнем организаций", "Положением об организации «Северный центр»")'
    )
    norm = candidate(1, SourceOrigin.SEARCH).model_copy(update={"title": title})
    generate = AsyncMock(return_value=SimpleNamespace(value=chosen(1, reason="Правила поддержки.")))
    patch_selection_inputs(monkeypatch, [norm], generate)
    result = (
        await retrieval.retrieve_legal_sources_node(
            {"document_text": "Проект о поддержке семей. Северный центр.", "named_acts": [], "search_queries": []}
        )
    )["legal_search_result"]
    user = generate.call_args.kwargs["messages"][1]["content"]
    card = json.loads(user.split("<inventory>")[1].split("</inventory>")[0])[0]
    assert card["title"] == title.split("\n(вместе с", 1)[0]
    assert card["project_mentions"] == ["Северный центр"]
    assert all(field not in card for field in ("kind", "number", "date", "quote"))
    assert result.plan.sources[0].ref.title == title
    assert result.plan.norms == [norm]
    assert result.plan.sources[0].ref == norm
    assert retrieval.read_legal_sources.call_args.args[1][0].ref == norm


async def test_uncertain_candidate_reaches_reading(monkeypatch):
    norm = candidate(1, SourceOrigin.SEARCH)
    generate = AsyncMock(
        return_value=SimpleNamespace(value=chosen(1, reason="По названию точный круг лиц неизвестен; нужен текст."))
    )
    patch_selection_inputs(monkeypatch, [norm], generate)
    result = (
        await retrieval.retrieve_legal_sources_node(
            {"document_text": "Проект о социальной поддержке.", "named_acts": [], "search_queries": []}
        )
    )["legal_search_result"]
    assert [source.ref for source in result.plan.sources] == [norm]
    assert retrieval.read_legal_sources.call_args.args[1][0].ref == norm
    assert "неизвестен" in result.plan.decisions[0].reason


async def test_direct_name_matches_are_shown_first_without_changing_candidate_ids(monkeypatch):
    unrelated = candidate(1, SourceOrigin.SEARCH)
    direct = candidate(2, SourceOrigin.SEARCH).model_copy(update={"title": 'Закон "Северный район"'})
    generate = AsyncMock(return_value=SimpleNamespace(value=chosen(2, quote="Северный район")))
    patch_selection_inputs(monkeypatch, [unrelated, direct], generate)
    result = (
        await retrieval.retrieve_legal_sources_node(
            {"document_text": "Проект. Северный район.", "named_acts": [], "search_queries": []}
        )
    )["legal_search_result"]
    user = generate.call_args.kwargs["messages"][1]["content"]
    inventory = json.loads(user.split("<inventory>")[1].split("</inventory>")[0])
    assert [row["candidate_id"] for row in inventory] == [2, 1]
    assert inventory[0]["project_mentions"] == ["Северный район"]
    assert result.plan.norms == [unrelated, direct]
    assert result.plan.sources[0].ref == direct


def test_filter_keeps_admission_checks_and_reports_shared_safety_limit():
    norm = candidate(1, SourceOrigin.SEARCH)
    invalid = norm.model_copy(update={"date": "неизвестна"})
    candidates, context, decisions, limitations = prepare_candidates([invalid], "Проект.")
    assert candidates == [] and context == []
    assert "missing_or_invalid_requisites" in decisions[0].reason
    assert limitations
    limit = int(settings["agents.legal.max_acts"])
    norms = [candidate(i, SourceOrigin.SEARCH) for i in range(1, limit + 2)]
    result = accept_selection([NamedActChoice(keep=True, reason="Нужен текст.")] * len(norms), norms)
    assert len(result.sources) == limit
    assert result.decisions[-1].outcome == "deferred"
    assert any("лимита" in text for text in result.limitations)


def test_filter_preserves_identity_and_cited_requisites():
    raw = "Приказ Министерства социального развития Ульяновской области от 19.05.1995 № 1"
    norm = candidate(1, SourceOrigin.SEARCH).model_copy(update={"kind": "regional_order", "number": "1", "raw": raw})
    issuer = project_issuer_key("Министерство социального развития Ульяновской области", "Ульяновской области")
    candidates, context, decisions, _ = prepare_candidates([norm], raw, project_issuer=issuer)
    assert not candidates and not context
    assert "not_higher_force_same_issuer" in decisions[0].reason

    cited = candidate(1, SourceOrigin.BODY).model_copy(update={"raw": "Федеральный закон от 19.05.1995 № 1-ФЗ"})
    candidates, _, decisions, _ = prepare_candidates([cited], "Проект без этой ссылки.")
    assert not candidates
    assert "quote_or_requisites_not_in_project" in decisions[0].reason
    candidates, _, decisions, _ = prepare_candidates([cited, cited.model_copy(update={"doc_num": 2})], cited.raw)
    assert candidates == [cited]
    assert decisions[-1].outcome == "duplicate"


@pytest.mark.parametrize(
    "ids, expected, unknown", [((2,), (2,), False), ((99, 2), (2,), True), ((99,), (1, 2), True), ((), (), False)]
)
def test_joint_selection_checks_ids_and_preserves_recoverable_results(ids, expected, unknown):
    norms = [candidate(i, SourceOrigin.SEARCH) for i in (1, 2)]
    result = accept_source_selection(chosen(*ids), norms, "Проект.")
    assert [s.ref.doc_num for s in result.sources] == list(expected)
    assert any("неизвестный ID" in text for text in result.limitations) == unknown
    assert result.source == ("parser_fallback" if ids == (99,) else "model")


def test_joint_selection_recovers_duplicate_ids_and_unverified_quote_without_limiting_review():
    norm = candidate(1, SourceOrigin.SEARCH)
    result = accept_source_selection(chosen(1, 1, quote="Выдуманное положение"), [norm], "Проект.")
    assert [s.ref for s in result.sources] == [norm]
    assert not result.limitations


@pytest.mark.parametrize(
    "quote, matches",
    [
        ("предоставления Министерством государственной услуги", True),
        ("Выплата не производится ранее 5 дней", True),
        ("Выплата производится ранее 5 дней", False),
        ("Выплата не производится ранее 6 дней", False),
        ("Министерством предоставления государственной услуги", False),
        ("   ", False),
    ],
)
def test_selection_binding_handles_ocr_spaces_without_changing_meaning(quote, matches):
    document = "порядок предоставления Министерством государственнойуслуги. Выплата не производится ранее 5 дней."
    norm = candidate(1, SourceOrigin.SEARCH)
    result = accept_source_selection(chosen(1, quote=quote), [norm], document)
    assert result.sources[0].ref == norm
    assert selection_quote_matches(quote, document) == matches
    assert not result.limitations


def test_failed_context_only_selection_is_explicit_fallback():
    context = PlannedSource(ref=candidate(1, SourceOrigin.BODY))
    result = accept_source_selection(None, [], "Проект.", context=[context])
    assert result.source == "parser_fallback"
    assert result.context_sources == (context,)
    assert not result.limitations


def test_joint_selection_can_exclude_unnecessary_context():
    norm = candidate(1, SourceOrigin.SEARCH)
    context = PlannedSource(ref=candidate(2, SourceOrigin.BODY))
    result = accept_source_selection(chosen(1), [norm], "Проект.", context=[context])
    assert [s.ref for s in result.sources] == [norm]
    assert not result.context_sources
    assert result.decisions[-1].outcome == "excluded"
    assert not result.limitations


def test_title_mentions_preserves_nested_names_and_distinguishes_object_kind():
    title = 'Закон "О преобразовании поселений муниципального образования "Северный район" и изменении закона "О территориях""'
    document = "Применяется в муниципальном образовании «Северный район»."
    assert title_mentions(title, document) == ["Северный район"]
    assert not title_mentions(title, "Применяется на территории Северного муниципального округа.")
    assert title_mentions("Закон «Об учреждении «Южный центр»»", "Действует Южный\nцентр.") == ["Южный центр"]


def test_project_blocks_preserve_every_character_and_complete_lines(monkeypatch):
    monkeypatch.setitem(settings["agents.legal"], "selection_block_chars", 20)
    document = "Первая строка.\nВторая строка.\nТретья строка.\nПоследняя строка без перевода"
    blocks = project_blocks(document)
    assert "".join(blocks.values()) == document
    assert list(blocks) == [f"B{i}" for i in range(1, len(blocks) + 1)]
    assert all(text.endswith("\n") for text in list(blocks.values())[:-1])


def test_selection_material_keeps_referenced_blocks_and_cited_context(monkeypatch):
    monkeypatch.setitem(settings["agents.legal"], "selection_block_chars", 5)
    lines = [f"Полное положение {i}.\n" for i in range(1, 8)]
    document = "".join(lines)
    material = selection_material(document, [["B3", "B4"]], ["Полное положение 7."])
    for index in (3, 4, 7):
        assert f"Полное положение {index}." in material
    assert "Полное положение 1." not in material
    assert "Полное положение 6." not in material
    assert "Полное положение 2." not in material
    assert "Полное положение 5." not in material


@pytest.mark.parametrize("questions", [[], [[]], [["B999"]], [["B1"], []], [["B1"], ["B999"]]])
def test_selection_material_uses_full_project_when_any_question_lacks_valid_blocks(questions):
    document = "Полный проект со всеми приложениями."
    assert selection_material(document, questions, []) == document


def test_selection_material_does_not_publish_invented_cited_context(monkeypatch):
    monkeypatch.setitem(settings["agents.legal"], "selection_block_chars", 5)
    document = "Правило первое.\nПравило второе.\nПравило третье.\nПравило четвёртое."
    material = selection_material(document, [["B1"]], ["Выдуманная цитата."])
    assert "Выдуманная цитата." not in material
    assert "Правило первое." in material
