import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.llm.errors import LLMGenerationError
from src.agents.schemas.common.review import Check, Citation, Review, Risk
from src.agents.schemas.legal.deduplication import FindingGroup, FindingGroups
from src.agents.steps_tools.compliance import deduplication as subject


def finding(explanation, *, project="Проект", source="Норма"):
    return Risk(
        kind=RiskKind.COMPLIANCE,
        explanation=explanation,
        recommendation=f"Исправить: {explanation}",
        citations=[Citation(source="project", quote=project), Citation(source="LAW:1", quote=source)],
    )


def review(findings):
    return Review(findings=findings, checks=[Check(kind=RiskKind.COMPLIANCE, status=CheckStatus.COMPLETED)])


def groups(*members):
    return FindingGroups(groups=[FindingGroup(members=list(m), representative=m[-1], reason="Одно нарушение.") for m in members])


def test_semantic_group_keeps_every_piece_of_evidence_and_separate_risks():
    first = finding("Отказ бессрочный", source="Предел пять лет")
    duplicate = finding("Не учтён пятилетний предел", project="Расширенная цитата проекта", source="Связанная норма")
    separate = finding("Заявление регистрируется позднее", source="Регистрация в день подачи")
    original = [first.model_dump(), duplicate.model_dump(), separate.model_dump()]
    result = subject.grouped_findings([first, duplicate, separate], groups([1, 2], [3]))
    assert len(result) == 2
    assert result[0].explanation == duplicate.explanation
    assert result[1] == separate
    assert {tuple(c.model_dump().items()) for c in result[0].citations} == {
        tuple(c.model_dump().items()) for risk in [first, duplicate] for c in risk.citations
    }
    assert [first.model_dump(), duplicate.model_dump(), separate.model_dump()] == original


def test_included_finding_keeps_the_complete_explanation_and_both_sets_of_quotes():
    mixed = finding("Совместное заявление ограничивает личное право; срок отчёта увеличен", source="Личное право")
    included = finding("Срок отчёта увеличен", source="Три календарных дня")
    value = FindingGroups(groups=[FindingGroup(members=[1, 2], representative=1, reason="Второй вывод включён в первый")])
    result = subject.grouped_findings([mixed, included], value)
    assert len(result) == 1
    assert result[0].explanation == mixed.explanation
    assert result[0].recommendation == mixed.recommendation
    assert {c.quote for c in result[0].citations} == {"Проект", "Личное право", "Три календарных дня"}


@pytest.mark.parametrize("members", [[[1]], [[1, 1], [2]], [[1, 3]], [[0, 1, 2]], []])
def test_incomplete_repeated_unknown_and_zero_indices_never_drop_risks(members):
    with pytest.raises(ValueError):
        subject.grouped_findings([finding("Первое"), finding("Второе")], groups(*members))


def test_representative_must_belong_to_group():
    value = groups([1], [2])
    value.groups[0].representative = 2
    with pytest.raises(ValueError):
        subject.grouped_findings([finding("Первое"), finding("Второе")], value)


@pytest.mark.parametrize("failure", ["timeout", "coverage"])
async def test_failed_grouping_preserves_all_findings_and_marks_limitation(monkeypatch, failure):
    generate = AsyncMock(
        side_effect=LLMGenerationError("timeout", ()) if failure == "timeout" else None,
        return_value=SimpleNamespace(value=groups([1])),
    )
    monkeypatch.setattr(subject, "llm", SimpleNamespace(generate_structured=generate))
    original = review([finding("Первое"), finding("Второе")])
    result = await subject.merge_duplicate_findings(original)
    assert result.findings == original.findings
    assert result.checks[0].status is CheckStatus.LIMITED
    assert "все исходные замечания сохранены" in result.checks[0].reason
    assert original.checks[0].status is CheckStatus.COMPLETED


@pytest.mark.parametrize("count", [0, 1])
async def test_no_generation_without_multiple_findings(monkeypatch, count):
    generate = AsyncMock(side_effect=AssertionError("No call needed"))
    monkeypatch.setattr(subject, "llm", SimpleNamespace(generate_structured=generate))
    original = review([finding("Первое")] * count)
    assert await subject.merge_duplicate_findings(original) is original
    generate.assert_not_awaited()


async def test_grouping_cancellation_propagates(monkeypatch):
    generate = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(subject, "llm", SimpleNamespace(generate_structured=generate))
    with pytest.raises(asyncio.CancelledError):
        await subject.merge_duplicate_findings(review([finding("Первое"), finding("Второе")]))


async def test_model_receives_complete_findings_only_and_zero_retries(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(value=groups([1, 2])))
    monkeypatch.setattr(subject, "llm", SimpleNamespace(generate_structured=generate))
    original = review([finding("Первое"), finding("Первое иначе", source="Другой источник")])
    result = await subject.merge_duplicate_findings(original)
    assert len(result.findings) == 1
    call = generate.call_args.kwargs
    assert call["role"] == "legal_dedup" and call["retries"] == 0
    data = call["messages"][1]["content"]
    assert all(r.explanation in data and all(c.quote in data for c in r.citations) for r in original.findings)
