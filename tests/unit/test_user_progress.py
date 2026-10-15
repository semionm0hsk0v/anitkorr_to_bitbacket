import pytest

from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.schemas.common.review import Check, Review, Risk
from src.agents.steps_tools.review.progress import stage_summary, ui_outcome


@pytest.mark.parametrize("kind", list(RiskKind))
@pytest.mark.parametrize("status", list(CheckStatus))
def test_progress_is_bounded_and_never_exposes_diagnostics(kind, status):
    diagnostics = "L1 LAW:123 [LOC_8558FE8A] Карточный поиск недоступен. " * 1000
    review = Review(findings=[], checks=[Check(kind=kind, status=status, reason=diagnostics)])
    before = review.model_dump_json()

    text = stage_summary(review)

    assert len(text) <= 115
    for internal in ("L1", "LAW:", "LOC_", "Карточный", "детектор", "Детектор", "частично", "неполно", "Смысловые"):
        assert internal not in text
    assert review.model_dump_json() == before
    assert "замечаний" not in text.lower()
    if status is CheckStatus.LIMITED:
        assert text.endswith(" выполнена.")
        assert ui_outcome(review) == "warn"
    elif status is CheckStatus.NOT_CHECKED:
        assert text.endswith(" не выполнена.")
        assert ui_outcome(review) == "warn"
    elif status is CheckStatus.NOT_APPLICABLE:
        assert "не требуется" in text
    else:
        assert text.endswith(" выполнена.")


@pytest.mark.parametrize("kind", list(RiskKind))
@pytest.mark.parametrize("status", [CheckStatus.COMPLETED, CheckStatus.LIMITED])
def test_progress_does_not_depend_on_finding_count(kind, status):
    risks = [
        Risk(kind=kind, explanation="Замечание", recommendation="Исправить", citations=[])
        for kind in (RiskKind.INTERNAL, RiskKind.INTERNAL, RiskKind.COMPLIANCE)
    ]
    review = Review(findings=risks, checks=[Check(kind=kind, status=status)])
    before = review.model_dump_json()

    text = stage_summary(review)

    assert text == stage_summary(Review(findings=[], checks=review.checks))
    assert text.endswith(" выполнена.")
    assert "Смысловые" not in text and "частично" not in text
    assert "замечаний" not in text.lower()
    assert not any(character.isdigit() for character in text)
    assert review.model_dump_json() == before


def test_missing_sources_are_explained_only_when_known():
    review = Review(findings=[], checks=[Check(kind=RiskKind.COMPETENCE, status=CheckStatus.NOT_CHECKED)])
    assert "нет текстов актов" in stage_summary(review, sources_unavailable=True)
    assert "текстов актов" not in stage_summary(review)


def test_early_stop_explains_why_other_checks_stop():
    text = stage_summary(Review(findings=[], checks=[]), early_stop=True)
    assert "Орган не вправе принять этот акт" in text
    assert "Остальные проверки остановлены" in text
