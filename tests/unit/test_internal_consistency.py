import asyncio
from threading import Event

import pytest

from src.agents.enums.common.review import CheckStatus
from src.agents.enums.internal.detector import InternalCheck
from src.agents.schemas.internal.detector import DetectorScan
from src.agents.schemas.parse.parsed_document import ParsedDocument
from src.agents.steps import check_internal_consistency_node as node
from src.agents.steps_tools.document.structure import build_document_structure_tree
from src.agents.steps_tools.internal import detector
from src.agents.steps_tools.internal.findings import detector_finding
from src.common.config.config import settings

PROJECT = "ПОРЯДОК\n1. Документы предоставляются в соответствии с пунктом 9 настоящего Порядка."


@pytest.fixture
def runtime(monkeypatch):
    state = {
        "document_text": PROJECT,
        "parsed_document": ParsedDocument(enriched_text=PROJECT, tree=build_document_structure_tree(PROJECT)),
        "source_name": "Порядок",
    }
    events = []
    monkeypatch.setattr(node, "emit_ui_event", lambda state, step, fragment, **kwargs: events.append(kwargs))
    return state, events


def fail(_data):
    raise RuntimeError("check failed")


async def test_real_detector_reports_dead_reference_and_limited_coverage(runtime):
    state, events = runtime
    delta = await node.check_internal_consistency_node(state)
    assert set(delta) == {"internal_review"}
    review = delta["internal_review"]
    assert len(review.findings) == 1
    assert "пунктом 9" in review.findings[0].citations[0].quote
    assert review.checks[0].status is CheckStatus.LIMITED
    assert review.checks[0].reason == settings.agents.internal.limit_reason
    assert events[-1]["outcome"] == "warn"


async def test_empty_result_is_still_limited(runtime):
    state, _ = runtime
    text = "ПОРЯДОК\n1. Документы предоставляются лично."
    state.update(document_text=text, parsed_document=ParsedDocument(enriched_text=text, tree=build_document_structure_tree(text)))
    review = (await node.check_internal_consistency_node(state))["internal_review"]
    assert not review.findings
    assert review.checks[0].status is CheckStatus.LIMITED
    assert "смысловые противоречия не проверялись" in review.checks[0].reason


@pytest.mark.parametrize("all_failed", [False, True])
async def test_check_failures_preserve_independent_findings(runtime, monkeypatch, all_failed):
    state, _ = runtime
    monkeypatch.setattr(
        detector,
        "CHECKS",
        tuple((check, fail if all_failed or check is InternalCheck.TIME_LIMIT else run) for check, run in detector.CHECKS),
    )
    review = (await node.check_internal_consistency_node(state))["internal_review"]
    assert len(review.findings) == (0 if all_failed else 1)
    assert review.checks[0].status is (CheckStatus.NOT_CHECKED if all_failed else CheckStatus.LIMITED)
    assert "Не завершили работу проверки детектора:" in review.checks[0].reason
    assert "сроки" in review.checks[0].reason
    if all_failed:
        assert "отсылки, сроки, значения одной величины, состав приложений, наименование" in review.checks[0].reason


async def test_unconfirmed_second_quote_is_held_without_losing_valid_finding(runtime, monkeypatch):
    state, _ = runtime
    valid = detector_finding(InternalCheck.REFERENCE, place="пункт 1", quote=PROJECT, rationale="Отсылка отсутствует.")
    invalid = detector_finding(
        InternalCheck.TIME_LIMIT,
        place="пункт 1",
        quote=PROJECT,
        rationale="Противоречие.",
        seconds=[("пункт 2", "Несуществующая вторая цитата.")],
    )
    monkeypatch.setattr(node, "scan_internal", lambda data: DetectorScan(findings=[valid, invalid]))
    review = (await node.check_internal_consistency_node(state))["internal_review"]
    assert review.findings == [valid.risk]
    assert review.checks[0].status is CheckStatus.LIMITED
    assert "не включённых в заключение: 1" in review.checks[0].reason


@pytest.mark.parametrize("parsed", [None, "missing"])
async def test_missing_structure_does_not_start_detector(runtime, monkeypatch, parsed):
    state, events = runtime
    if parsed is None:
        state["parsed_document"] = None
    else:
        state.pop("parsed_document")

    def unexpected(_):
        pytest.fail("Detector must not start without structure")

    monkeypatch.setattr(node, "scan_internal", unexpected)
    review = (await node.check_internal_consistency_node(state))["internal_review"]
    assert not review.findings
    assert review.checks[0].status is CheckStatus.NOT_CHECKED
    assert "не запускался" in review.checks[0].reason
    assert events[-1]["outcome"] == "warn"


async def test_detector_failure_returns_not_checked(runtime, monkeypatch):
    state, events = runtime
    monkeypatch.setattr(node, "scan_internal", fail)
    review = (await node.check_internal_consistency_node(state))["internal_review"]
    assert not review.findings
    assert review.checks[0].status is CheckStatus.NOT_CHECKED
    assert "Технический сбой" in review.checks[0].reason
    assert events[-1]["outcome"] == "warn"


async def test_cancellation_propagates_without_success_event(runtime, monkeypatch):
    state, events = runtime
    started, release, finished = Event(), Event(), Event()

    def wait_for_release(_):
        started.set()
        try:
            release.wait(timeout=5)
            return DetectorScan()
        finally:
            finished.set()

    monkeypatch.setattr(node, "scan_internal", wait_for_release)
    task = asyncio.create_task(node.check_internal_consistency_node(state))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [event["status"] for event in events] == ["in_progress"]
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
