import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.enums.common.review import CheckStatus
from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.llm.errors import LLMGenerationError
from src.agents.schemas.legal.norm import Norm
from src.agents.schemas.legal.plan import NamedActChoice, PlanOutcome, PlannedSource, SelectedAct, SourceSelection
from src.agents.schemas.legal.search import LegalSearchResult
from src.agents.schemas.legal.sources import LegalReading, LegalSources
from src.agents.schemas.legal.verdict import BlockResult, ComplianceIssue, LegalVerdict, SourceBlock
from src.agents.schemas.legal_base.retrieval import RawAct
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.agents.schemas.legal_base.source_refs import SourceRef
from src.agents.steps import check_legal_compliance_node as node
from src.agents.steps_tools.compliance import verdict
from src.agents.steps_tools.compliance.plan import accept_selection, accept_source_selection
from src.agents.steps_tools.compliance.sources import summarize_readings
from src.agents.steps_tools.compliance.verdict import assemble_compliance_review
from src.common.config.config import settings

DOCUMENT = "1. Решение принимается за 40 дней."
LAW = "Статья 1. Решение принимается за 30 дней."


def reading(number=1, text=LAW):
    ref = SourceRef(kind="federal_law", number=f"{number}-ФЗ", date="01.01.2026", role=SourceRole.CITED, origin=SourceOrigin.BODY)
    return LegalReading(
        planned=PlannedSource(ref=ref),
        reading=SourceReading(
            ref=ref,
            status=ReadingStatus.COMPLETE,
            raw=RawAct(
                query="", base="LAW", doc_num=number, title=f"Федеральный закон от 01.01.2026 N {number}-ФЗ", full_text=text
            ),
        ),
    )


def unit(number=1, text=LAW):
    return SourceBlock(item=reading(number, text), text=text)


def issue(**updates):
    return ComplianceIssue.model_validate(
        {
            "project_quote": "Решение принимается за 40 дней.",
            "source_id": "LAW:1",
            "source_quote": "Решение принимается за 30 дней.",
            "explanation": "Срок превышен.",
        }
        | updates
    )


def state(items=()):
    return {
        "document_text": DOCUMENT,
        "analysis_date": "2026-10-11",
        "legal_search_result": LegalSearchResult(
            plan=PlanOutcome(sources=tuple(item.planned for item in items), source="model"),
            sources=LegalSources(readings=tuple(items), limitations=()),
            context=LegalSources(readings=(), limitations=()),
        ),
    }


def prompt_context():
    return {
        "document": DOCUMENT,
        "analysis_date": "2026-10-11",
        "region": "",
        "issuer": "",
        "project_title": "",
        "mentioned_acts": "",
        "explanatory_note": "",
    }


@pytest.mark.parametrize("skip", [True, False])
async def test_no_sources_skips_generation(monkeypatch, skip):
    check_source_blocks = AsyncMock()
    monkeypatch.setattr(node, "check_source_blocks", check_source_blocks)
    inputs = {"document_text": DOCUMENT, "legal_skip_reason": "К+ недоступен."} if skip else state()
    delta = await node.check_legal_compliance_node(inputs)
    assert delta["legal_review"].checks[0].status is CheckStatus.NOT_CHECKED
    assert set(delta) == {"legal_review"}
    check_source_blocks.assert_not_awaited()


async def test_context_alone_does_not_start_review_and_preserves_limits(monkeypatch):
    inputs = state()
    found = inputs["legal_search_result"]
    inputs["legal_search_result"] = found.model_copy(
        update={
            "plan": found.plan.model_copy(update={"limitations": ("Поиск неполон.",)}),
            "sources": LegalSources(readings=(), limitations=("Закон не прочитан.",)),
            "context": LegalSources(readings=(reading(),), limitations=("Контекст неполон.",)),
        }
    )
    check_source_blocks = AsyncMock()
    monkeypatch.setattr(node, "check_source_blocks", check_source_blocks)

    delta = await node.check_legal_compliance_node(inputs)

    check = delta["legal_review"].checks[0]
    assert check.status is CheckStatus.NOT_CHECKED
    for reason in ("Поиск неполон.", "Закон не прочитан.", "Контекст неполон."):
        assert reason in check.reason
    check_source_blocks.assert_not_awaited()


@pytest.mark.parametrize("source_id", ["LAW:1", "LAW:2"])
async def test_node_uses_first_main_text_for_evidence_and_context_only_for_applicability(monkeypatch, source_id):
    inputs = state([reading(), reading(text="Повторное чтение с другим текстом.")])
    inputs["legal_search_result"] = inputs["legal_search_result"].model_copy(
        update={
            "context": LegalSources(
                readings=(reading(text="Контекстная версия основного акта."), reading(2), reading(2, "Дубль контекста.")),
                limitations=("Контекст неполон.",),
            )
        }
    )
    result = LegalVerdict(issues=[issue().model_copy(update={"source_id": source_id})])
    generate = AsyncMock(return_value=SimpleNamespace(value=result))
    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))

    delta = await node.check_legal_compliance_node(inputs)

    review = delta["legal_review"]
    assert len(review.findings) == int(source_id == "LAW:1")
    if review.findings:
        assert review.findings[0].citations[1].source == source_id
    else:
        assert "вне основного блока" in review.checks[0].reason
    assert review.checks[0].status is CheckStatus.LIMITED
    assert "Контекст неполон." in review.checks[0].reason
    related = f"source_id: LAW:1\nКонтекстная версия основного акта.\n\nsource_id: LAW:2\n{LAW}"
    messages = "\n".join(message["content"] for message in generate.call_args.kwargs["messages"])
    assert related in messages
    assert "Повторное чтение с другим текстом." not in messages
    assert "Дубль контекста." not in messages


@pytest.mark.parametrize("selection_case", ["duplicate", "above_target", "fallback", "currentness"])
@pytest.mark.parametrize("unread", [False, True])
async def test_recovered_selection_limits_review_only_when_material_is_unread(monkeypatch, selection_case, unread):
    items = [reading(1), reading(2)]
    norms = [Norm(**item.reading.ref.model_dump(), bank="LAW", doc_num=i) for i, item in enumerate(items, 1)]
    monkeypatch.setitem(settings, "agents.legal.target_acts", 1 if selection_case == "above_target" else 5)
    ids = [1, 2, 1] if selection_case == "duplicate" else [1, 2]
    selection = SourceSelection(
        selected=[SelectedAct(candidate_id=i, project_quote="Неподтверждённая привязка", reason="Применим.") for i in ids]
    )
    if selection_case == "currentness":
        plan = accept_selection([NamedActChoice(keep=False, reason="Возможно, утратил силу.")] * 2, norms)
    else:
        plan = accept_source_selection(None if selection_case == "fallback" else selection, norms, DOCUMENT)
    assert len(plan.sources) == 2
    assert not plan.limitations
    if unread:
        items[1] = items[1].model_copy(
            update={"reading": items[1].reading.model_copy(update={"status": ReadingStatus.TRANSPORT_FAILURE, "raw": None})}
        )
    inputs = state(items)
    inputs["legal_search_result"] = inputs["legal_search_result"].model_copy(
        update={"plan": plan, "sources": summarize_readings(items)}
    )
    monkeypatch.setattr(
        verdict,
        "llm",
        SimpleNamespace(generate_structured=AsyncMock(return_value=SimpleNamespace(value=LegalVerdict(issues=[])))),
    )

    review = (await node.check_legal_compliance_node(inputs))["legal_review"]

    assert not review.findings
    assert review.checks[0].status is (CheckStatus.LIMITED if unread else CheckStatus.COMPLETED)
    assert ("сбой обращения" in review.checks[0].reason) == unread


@pytest.mark.parametrize("failed", [0, 1, 2])
@pytest.mark.parametrize("has_finding", [False, True])
def test_status_and_findings_follow_completed_work(failed, has_finding):
    results = [
        BlockResult(block=unit(i + 1), verdict=None, exception=LLMGenerationError("timeout", ()))
        if i < failed
        else BlockResult(
            block=unit(i + 1),
            verdict=LegalVerdict(issues=[issue().model_copy(update={"source_id": "LAW:2"})] if has_finding and i == 1 else []),
        )
        for i in range(2)
    ]
    review = assemble_compliance_review(results, [], document=DOCUMENT, sources={"LAW:1": LAW, "LAW:2": LAW})
    expected = CheckStatus.COMPLETED if failed == 0 else CheckStatus.NOT_CHECKED if failed == 2 else CheckStatus.LIMITED
    assert review.checks[0].status is expected
    assert len(review.findings) == int(has_finding and failed < 2)


@pytest.mark.parametrize(
    "distinct_fields",
    [
        {"explanation": "Отдельное нарушение процедуры."},
        {"explanation": "Отдельное нарушение процедуры.", "source_quote": "Другая обязанность."},
    ],
)
def test_admission_precedes_deduplication_and_preserves_distinct_defects(distinct_fields):
    valid = issue()
    invented = valid.model_copy(update={"source_quote": "Вымышленная норма."})
    distinct = valid.model_copy(update=distinct_fields)
    result = BlockResult(block=unit(), verdict=LegalVerdict(issues=[invented, valid, valid, distinct]))
    review = assemble_compliance_review(
        [result], ["Не прочитано приложение."], document=DOCUMENT, sources={"LAW:1": LAW + " Другая обязанность."}
    )
    assert len(review.findings) == 2
    assert review.checks[0].status is CheckStatus.LIMITED
    assert review.checks[0].reason.count("Не прочитано приложение.") == 1
    assert "не подтверждённых цитатами" in review.checks[0].reason


@pytest.mark.parametrize("invalid_fields", [{"project_quote": ""}, {"source_quote": ""}, {"explanation": ""}])
@pytest.mark.parametrize("valid_first", [False, True])
async def test_invalid_model_finding_does_not_discard_valid_findings(monkeypatch, invalid_fields, valid_first):
    valid = issue()
    invalid = ComplianceIssue.model_validate(valid.model_dump() | invalid_fields)
    issues = [valid, invalid] if valid_first else [invalid, valid]
    generate = AsyncMock(return_value=SimpleNamespace(value=LegalVerdict(issues=issues)))
    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))

    review = (await node.check_legal_compliance_node(state([reading()])))["legal_review"]

    assert len(review.findings) == 1
    assert review.findings[0].citations[0].quote == valid.project_quote
    assert review.checks[0].status is CheckStatus.LIMITED
    assert "неполные обязательные данные" in review.checks[0].reason


async def test_invalid_model_finding_alone_is_not_a_clean_review(monkeypatch):
    invalid = ComplianceIssue.model_validate(issue().model_dump() | {"project_quote": ""})
    generate = AsyncMock(return_value=SimpleNamespace(value=LegalVerdict(issues=[invalid])))
    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))

    review = (await node.check_legal_compliance_node(state([reading()])))["legal_review"]

    assert not review.findings
    assert review.checks[0].status is CheckStatus.LIMITED
    assert "неполные обязательные данные" in review.checks[0].reason


@pytest.mark.parametrize("target", ["project", "source", "both"])
def test_restores_verbatim_quotes_from_glued_extracted_words(target):
    document = DOCUMENT.replace("Решение принимается", "Решениепринимается") if target != "source" else DOCUMENT
    source = LAW.replace("Решение принимается", "Решениепринимается") if target != "project" else LAW
    candidate = issue()
    result = BlockResult(block=unit(text=source), verdict=LegalVerdict(issues=[candidate]))

    review = assemble_compliance_review([result], [], document=document, sources={"LAW:1": source})

    assert len(review.findings) == 1
    project_citation, source_citation = review.findings[0].citations
    assert project_citation.quote in document
    assert source_citation.quote in source
    assert project_citation.quote == document.removeprefix("1. ")
    assert source_citation.quote == source.removeprefix("Статья 1. ")
    assert review.checks[0].status is CheckStatus.COMPLETED
    assert candidate.project_quote == "Решение принимается за 40 дней."


@pytest.mark.parametrize(
    "quote",
    [
        "Решение принимается за 4 0 дней.",
        "Решение принимается за 30 дней.",
        "Решение принимается за 40 дней;",
        "Решение [...] за 40 дней.",
        "Вымышленное положение.",
        " ",
    ],
)
def test_spacing_restoration_does_not_repair_numbers_punctuation_or_missing_words(quote):
    candidate = issue().model_copy(update={"project_quote": quote})
    result = BlockResult(block=unit(), verdict=LegalVerdict(issues=[candidate]))

    review = assemble_compliance_review([result], [], document=DOCUMENT, sources={"LAW:1": LAW})

    assert not review.findings
    assert review.checks[0].status is CheckStatus.LIMITED


def test_spacing_restoration_abstains_on_different_original_word_boundaries():
    document = "1. Решениепринимается за 40 дней.\n2. Решение принимаетсяза 40 дней."
    result = BlockResult(block=unit(), verdict=LegalVerdict(issues=[issue()]))

    review = assemble_compliance_review([result], [], document=document, sources={"LAW:1": LAW})

    assert not review.findings
    assert review.checks[0].status is CheckStatus.LIMITED


def test_repeated_restored_quote_keeps_all_confirmed_locations():
    document = "1. Решениепринимается за 40 дней.\n2. Решениепринимается за 40 дней."
    result = BlockResult(block=unit(), verdict=LegalVerdict(issues=[issue()]))

    review = assemble_compliance_review([result], [], document=document, sources={"LAW:1": LAW})

    assert len(review.findings) == 1
    citation = review.findings[0].citations[0]
    assert citation.quote == "Решениепринимается за 40 дней."
    assert citation.location.startswith("Вхождения цитаты:")


async def test_joint_generation_failure_marks_all_sources_unchecked(monkeypatch):
    generate = AsyncMock(side_effect=LLMGenerationError("timeout", ()))
    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))
    results = await verdict.check_source_blocks(prompt_context(), [unit(1), unit(2)], timeout_s=1)
    generate.assert_awaited_once()
    assert len(results) == 2
    assert all(result.verdict is None and isinstance(result.exception, LLMGenerationError) for result in results)


@pytest.mark.parametrize("cancel", [False, True])
async def test_node_failure_or_cancellation_does_not_emit_success(monkeypatch, cancel):
    started, stopped = asyncio.Event(), asyncio.Event()
    events = []

    async def generate(**kwargs):
        started.set()
        try:
            if not cancel:
                raise ValueError("generation failed")
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(node, "emit_ui_event", lambda *args, **kwargs: events.append(kwargs))
    task = asyncio.create_task(node.check_legal_compliance_node(state([reading(), reading(2)])))
    await asyncio.wait_for(started.wait(), 1)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ValueError):
        await task
    assert stopped.is_set()
    assert events and all(event["status"] == "in_progress" for event in events)


async def test_joint_preparation_failure_does_not_start_generation(monkeypatch):
    def compile_prompt(**kwargs):
        raise ValueError("template failed")

    generate = AsyncMock()
    monkeypatch.setattr(verdict, "get_prompt", lambda _: SimpleNamespace(compile=compile_prompt))
    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))
    with pytest.raises(ValueError, match="template failed"):
        await verdict.check_source_blocks(prompt_context(), [unit(1), unit(2)], timeout_s=1)
    generate.assert_not_awaited()


@pytest.mark.parametrize("status", [ReadingStatus.COMPLETE, ReadingStatus.TRUNCATED])
async def test_full_sources_reach_joint_verdict_once(monkeypatch, status):
    paragraphs = [f"Статья {i}. Уникальное правило {i}. " + "Требование. " * 700 for i in range(1, 13)]
    text = "\n".join(paragraphs)
    item = reading(text=text)
    item = item.model_copy(
        update={
            "reading": item.reading.model_copy(
                update={
                    "status": status,
                    "fetched_paragraphs": 12,
                    "total_paragraphs": 15 if status is ReadingStatus.TRUNCATED else 12,
                }
            )
        }
    )
    units = verdict.build_source_blocks([item, item, reading(2)])
    assert len(units) == 2
    generate = AsyncMock(return_value=SimpleNamespace(value=LegalVerdict(issues=[])))
    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))
    results = await verdict.check_source_blocks(prompt_context(), units, timeout_s=1)
    assert all(result.verdict is not None for result in results)
    generate.assert_awaited_once()
    user = generate.call_args.kwargs["messages"][1]["content"]
    assert all(user.count(paragraph) == 1 for paragraph in paragraphs)
    assert "Другие прочитанные источники отсутствуют." in user
    assert "выбраны планом" not in user
    assert ("ТЕКСТ АКТА НЕПОЛНЫЙ" in user) == (status is ReadingStatus.TRUNCATED)


def test_missing_material_limits_result_without_a_finding():
    verdict = LegalVerdict(issues=[])
    review = assemble_compliance_review(
        [BlockResult(block=unit(text=""), verdict=verdict)],
        ["Отсутствует приложение к закону."],
        document="",
        sources={},
    )
    assert review.checks[0].status is CheckStatus.LIMITED
    assert "Отсутствует приложение" in review.checks[0].reason


@pytest.mark.parametrize(("repeat_project", "repeat_source"), [(False, False), (True, False), (False, True), (True, True)])
def test_repeated_quote_preserves_finding_and_lists_all_occurrences(repeat_project, repeat_source):
    document = "1. Срок 40 дней." + ("\n2. Срок 40 дней." if repeat_project else "")
    source = "Статья 1. Срок 30 дней." + ("\nСтатья 2. Срок 30 дней." if repeat_source else "")
    verdict = LegalVerdict(
        issues=[
            issue(
                project_quote="Срок 40 дней.",
                source_quote="Срок 30 дней.",
            )
        ]
    )
    review = assemble_compliance_review(
        [BlockResult(block=unit(text=source), verdict=verdict)],
        [],
        document=document,
        sources={"LAW:1": source},
    )
    (risk,) = review.findings
    assert review.checks[0].status is CheckStatus.COMPLETED
    assert not review.checks[0].reason
    project_location, source_location = (citation.location for citation in risk.citations)
    assert "999" not in project_location and "999" not in source_location
    assert "символы 4–16" in project_location
    assert "символы 11–23" in source_location
    if repeat_project:
        assert "Вхождения цитаты:" in project_location
        assert "символы 21–33" in project_location
    if repeat_source:
        assert "Вхождения цитаты:" in source_location
        assert "символы 35–47" in source_location


@pytest.mark.parametrize("missing_quote", ["project_quote", "source_quote"])
def test_missing_quote_still_rejects_finding_when_other_quote_repeats(missing_quote):
    document, source = "1. Срок 40 дней.\n2. Срок 40 дней.", "Статья 1. Срок 30 дней.\nСтатья 2. Срок 30 дней."
    candidate = issue(project_quote="Срок 40 дней.", source_quote="Срок 30 дней.")
    verdict = LegalVerdict(issues=[candidate.model_copy(update={missing_quote: "Вымышленный текст."})])
    review = assemble_compliance_review(
        [BlockResult(block=unit(text=source), verdict=verdict)], [], document=document, sources={"LAW:1": source}
    )
    assert not review.findings
    assert review.checks[0].status is CheckStatus.LIMITED
    assert "не подтверждённых цитатами" in review.checks[0].reason


@pytest.mark.parametrize("failed_role", [None, "legal_scope", "legal_refusal", "both"])
async def test_parallel_reviews_preserve_materials_and_report_partial_failure(monkeypatch, failed_role):
    started = set()
    both_started = asyncio.Event()

    async def generate(**kwargs):
        role = kwargs["role"]
        started.add(role)
        if len(started) == 3:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        if failed_role in (role, "both"):
            raise LLMGenerationError("timeout", ())
        return SimpleNamespace(value=LegalVerdict(issues=[issue()]))

    generate_mock = AsyncMock(side_effect=generate)
    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate_mock))
    review = (await node.check_legal_compliance_node(state([reading()])))["legal_review"]
    assert generate_mock.await_count == 3
    assert started == {"legal_verdict", "legal_scope", "legal_refusal"}
    for call in generate_mock.call_args_list:
        assert call.kwargs["retries"] == 0
        data = "\n".join(m["content"] for m in call.kwargs["messages"] if m["role"] == "user")
        assert DOCUMENT in data and LAW in data
    assert len(review.findings) == int(failed_role != "both")
    expected = (
        CheckStatus.COMPLETED
        if failed_role is None
        else (CheckStatus.NOT_CHECKED if failed_role == "both" else CheckStatus.LIMITED)
    )
    assert review.checks[0].status is expected
    assert bool(review.checks[0].reason) is (failed_role is not None)


async def test_cancelled_node_stops_both_reviews(monkeypatch):
    started, stopped = set(), set()
    both_started = asyncio.Event()

    async def generate(**kwargs):
        role = kwargs["role"]
        started.add(role)
        if len(started) == 3:
            both_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add(role)

    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))
    task = asyncio.create_task(node.check_legal_compliance_node(state([reading()])))
    await asyncio.wait_for(both_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == {"legal_verdict", "legal_scope", "legal_refusal"}


@pytest.mark.parametrize("role", ["legal_verdict", "legal_scope", "legal_refusal"])
async def test_unknown_source_limits_review_and_preserves_valid_finding(monkeypatch, role):
    valid = issue()
    unknown = valid.model_copy(update={"source_id": "LAW:999"})
    generate = AsyncMock(return_value=SimpleNamespace(value=LegalVerdict(issues=[valid, unknown])))
    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))

    results = await verdict.check_source_blocks(prompt_context(), [unit()], timeout_s=1, role=role)
    review = assemble_compliance_review(results, [], document=DOCUMENT, sources={"LAW:1": LAW})

    assert len(review.findings) == 1
    assert review.findings[0].citations[1].source == "LAW:1"
    assert review.findings[0].explanation == valid.explanation
    assert review.findings[0].recommendation == "Привести положение проекта в соответствие с вышестоящей нормой."
    assert all(citation.location for citation in review.findings[0].citations)
    assert review.checks[0].status is CheckStatus.LIMITED
    assert "вне основного блока" in review.checks[0].reason


async def test_stage_deadline_stops_deduplication_and_preserves_findings(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(value=LegalVerdict(issues=[issue()])))
    stopped = asyncio.Event()

    async def blocked_dedup(review):
        assert review.findings
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(verdict, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(node, "merge_duplicate_findings", blocked_dedup)
    monkeypatch.setattr(
        node,
        "settings",
        {
            "agents.legal.review.timeout_s": 0.05,
            "llm.legal_verdict.timeout": settings["llm.legal_verdict.timeout"],
            "llm.legal_scope.timeout": settings["llm.legal_scope.timeout"],
            "llm.legal_refusal.timeout": settings["llm.legal_refusal.timeout"],
        },
    )
    review = (await asyncio.wait_for(node.check_legal_compliance_node(state([reading()])), 1))["legal_review"]
    assert stopped.is_set()
    assert len(review.findings) == 1
    assert review.checks[0].status is CheckStatus.LIMITED
    assert "пределах времени этапа" in review.checks[0].reason
