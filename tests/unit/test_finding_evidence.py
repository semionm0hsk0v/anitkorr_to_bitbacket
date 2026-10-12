from __future__ import annotations

import pytest

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.schemas.common.review import Check, Citation, Review, Risk
from src.agents.steps_tools.review.grounding import grounded, quote_in_source
from src.agents.steps_tools.review.quote_locations import QuoteIndex
from src.agents.steps_tools.review.settle import settle

PROJECT = "1. Текст проекта. 2. Ссылка на приложение № 5."
SOURCES = {"law": "Статья 1. Закон."}


def make_risk(kind: str = "internal", *citations: Citation, explanation: str = "Обоснование.") -> Risk:
    return Risk(
        kind=kind,
        explanation=explanation,
        recommendation="Исправить.",
        citations=list(citations) or [Citation(source="project", location="пункт 1", quote="Текст проекта.")],
    )


def project(quote: str = "Текст проекта.") -> Citation:
    return Citation(source="project", location="пункт 1", quote=quote)


def law(quote: str = "Закон.", source: str = "law") -> Citation:
    return Citation(source=source, location="Закон, статья 1", quote=quote)


@pytest.mark.parametrize(
    "risk",
    [
        pytest.param(make_risk("internal", project("Цитата из пояснительной записки.")), id="note-is-not-the-project"),
        pytest.param(make_risk("internal", project(), project("Несуществующая цитата.")), id="second-quote-invented"),
        pytest.param(make_risk("internal", law(), project()), id="project-not-first"),
        pytest.param(make_risk("internal", project(), law()), id="internal-cites-external-law"),
        pytest.param(make_risk("compliance", project()), id="external-without-read-basis"),
        pytest.param(make_risk("competence", project(), law(source="search_hit")), id="search-hit-is-not-a-read-source"),
        pytest.param(make_risk("compliance", project(), law("Иная норма.")), id="quote-not-in-read-source"),
        pytest.param(Risk(kind="internal", explanation="О.", recommendation="И.", citations=[]), id="no-citation"),
    ],
)
def test_findings_require_quotes_from_original_sources(risk):
    """Нарушения без цитат из проекта и прочитанных источников отклоняются."""
    assert not grounded(risk, PROJECT, SOURCES)


def test_legal_finding_accepts_project_and_law_evidence():
    """Нарушение принимается при наличии цитат из проекта и закона."""
    assert grounded(make_risk("compliance", project(), law()), PROJECT, SOURCES)


def test_rejected_evidence_limits_review_without_losing_valid_findings():
    """Неподтверждённая находка ограничивает проверку, сохраняя подтверждённые."""
    review = Review(
        findings=[make_risk("internal"), make_risk("internal", project("Выдумка."))],
        checks=[Check(kind="internal", status="completed", reason="лишний текст")],
    )
    settled = settle(review, (RiskKind.INTERNAL,), document=PROJECT, sources={})
    assert settled.findings == [review.findings[0]]
    (check,) = settled.checks
    assert check.status is CheckStatus.LIMITED
    assert "не подтверждённых цитатами источников" in check.reason and "лишний текст" not in check.reason


@pytest.mark.parametrize("separator", ["…", "...", ";"])
def test_quote_cannot_skip_a_negation(separator):
    text = "Орган вправе принять заявление. Орган не вправе отказать."
    assert not quote_in_source(f"Орган вправе {separator} отказать.", text)


@pytest.mark.parametrize("text", ["Срок … не установлен.", "Первое; второе.", "Нет... ответа.", "Два\n\tслова."])
def test_literal_punctuation_and_whitespace_are_preserved(text):
    assert quote_in_source(" ".join(text.split()), text)
    assert not quote_in_source(text.lower(), text)


def test_quote_coordinates_survive_whitespace_normalization():
    text = "\n1. Два\n\tслова."
    index = QuoteIndex(text)
    (span,) = index.spans("Два слова.")
    assert text[slice(*span)] == "Два\n\tслова."
