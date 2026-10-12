import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.agents.enums.common.review import CheckStatus
from src.agents.enums.repeal.detector import RepealOutcomeReason
from src.agents.schemas.repeal.detector import Amender, OrphanResult, RepealDetectionResult, RepealTarget, UnitScopeGap
from src.agents.schemas.repeal.evidence import RepealAmenderEvidence, RepealChainEvidence, RepealSourceEvidence
from src.agents.steps import check_incomplete_repeal_node as node
from src.agents.steps_tools.document.structure import build_document_structure_tree
from src.agents.steps_tools.repeal import detector as detector_tools, review as review_tools
from src.agents.steps_tools.repeal.clauses import collect_repeal_targets

CLAUSE = "2. Признать утратившим силу приказ от 01.02.2024 № 21-п."


def source_evidence(number, doc_num):
    return RepealSourceEvidence(
        act_number=number,
        source_base="RLAW076",
        source_doc_num=doc_num,
    )


def orphan(*, proven=True, quote=CLAUSE):
    evidence = RepealChainEvidence(
        base=source_evidence("21-п", 21),
        amenders=(
            RepealAmenderEvidence(
                source=source_evidence("83-п", 83), relation_proof="amendment_formula", repealed_by_project=False
            ),
        ),
        orphan_numbers=("83-п",),
        orphan_source_ids=(("RLAW076", 83),),
    )
    return OrphanResult(
        base_number="21-п", orphans=[Amender(number="83-п")], clause_marker_text=quote, evidence=evidence if proven else None
    )


@pytest.fixture
def runtime(monkeypatch):
    runtime = SimpleNamespace(
        state={
            "document_text": CLAUSE,
            "parsed_document": SimpleNamespace(tree=build_document_structure_tree(CLAUSE), enriched_text=CLAUSE),
            "mcp_request_sessions": SimpleNamespace(transport_failures=()),
            "analysis_date": "2026-10-10",
        },
        exact_retrieval=True,
        detection=RepealDetectionResult(),
        error=None,
        foreign_failure=False,
        calls=[],
        events=[],
    )

    async def detect(sessions, targets, *, region, analysis_date):
        runtime.calls.append((sessions, targets, analysis_date))
        if runtime.foreign_failure:
            sessions.transport_failures = (SimpleNamespace(error_class="ConnectionError", error_chain=("ConnectionError",)),)
        if runtime.error is not None:
            raise runtime.error
        return runtime.detection

    monkeypatch.setattr(node, "consultant_capabilities", lambda _: SimpleNamespace(exact_retrieval=runtime.exact_retrieval))
    monkeypatch.setattr(node, "detect_orphan_amendments_with_status", detect)
    monkeypatch.setattr(node, "emit_ui_event", lambda state, step, fragment, **kwargs: runtime.events.append(kwargs))
    return runtime


@pytest.mark.parametrize("missing", ["tree", "transport"])
async def test_missing_prerequisites_do_not_start_detector(runtime, missing):
    if missing == "tree":
        runtime.state.pop("parsed_document")
    else:
        runtime.exact_retrieval = False
    delta = await node.check_incomplete_repeal_node(runtime.state)
    assert runtime.calls == []
    assert delta["repeal_review"].checks[0].status is CheckStatus.NOT_CHECKED
    assert runtime.events[-1]["outcome"] == "warn"


@pytest.mark.parametrize("has_finding", [False, True])
async def test_complete_scan_publishes_only_proven_findings(runtime, has_finding):
    runtime.detection = RepealDetectionResult(results=[orphan()] if has_finding else [])
    delta = await node.check_incomplete_repeal_node(runtime.state)
    review = delta["repeal_review"]
    assert len(review.findings) == int(has_finding)
    assert review.checks[0].status is CheckStatus.COMPLETED
    assert runtime.calls == [
        (
            runtime.state["mcp_request_sessions"],
            collect_repeal_targets(runtime.state["parsed_document"].tree),
            runtime.state["analysis_date"],
        )
    ]
    assert [event["status"] for event in runtime.events] == ["in_progress", "success"]
    assert runtime.events[-1]["outcome"] == ("found" if has_finding else "clear")
    assert "branch_failures" not in delta


async def test_partial_scan_keeps_proven_findings(runtime):
    runtime.detection = RepealDetectionResult(results=[orphan()], blocked_bases=("44-п",))
    delta = await node.check_incomplete_repeal_node(runtime.state)
    assert len(delta["repeal_review"].findings) == 1
    check = delta["repeal_review"].checks[0]
    assert check.status is CheckStatus.LIMITED
    assert "44-п" in check.reason
    assert runtime.events[-1]["outcome"] == "warn"


@pytest.mark.parametrize("unproven", [orphan(proven=False), orphan(quote="Цитата, которой в проекте нет.")])
async def test_held_finding_limits_an_otherwise_complete_scan(runtime, unproven):
    runtime.detection = RepealDetectionResult(results=[orphan(), unproven])
    delta = await node.check_incomplete_repeal_node(runtime.state)
    assert len(delta["repeal_review"].findings) == 1
    check = delta["repeal_review"].checks[0]
    assert check.status is CheckStatus.LIMITED
    assert "не подтверждённых источниками детектора: 1" in check.reason


async def test_detector_stops_parallel_reads_before_propagating_failure(monkeypatch):
    started_base, started_subpoint = asyncio.Event(), asyncio.Event()
    stopped = []

    async def check_base(sessions, base, *args):
        if base.number == "21-п":
            await started_base.wait()
            await started_subpoint.wait()
            raise ConnectionError("read failed")
        started_base.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("base")

    async def check_subpoint(*args):
        started_subpoint.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("subpoint")

    targets = [
        RepealTarget(number="21-п"),
        RepealTarget(number="44-п"),
        RepealTarget(number="83-п", is_subpoint=True, subpoint="1"),
    ]
    monkeypatch.setattr(detector_tools, "_check_base_with_status", check_base)
    monkeypatch.setattr(detector_tools, "_check_redundant_repeal", check_subpoint)
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(
            detector_tools.detect_orphan_amendments_with_status(
                None, [(target, SimpleNamespace(text=CLAUSE), CLAUSE) for target in targets]
            ),
            timeout=1,
        )
    assert sorted(stopped) == ["base", "subpoint"]


async def test_transport_failure_warns(runtime):
    runtime.error = ConnectionError("transport unavailable")
    delta = await node.check_incomplete_repeal_node(runtime.state)
    assert set(delta) == {"repeal_review"}
    assert delta["repeal_review"].checks[0].status is CheckStatus.NOT_CHECKED
    assert "Сбой соединения" in delta["repeal_review"].checks[0].reason
    assert runtime.events[-1]["outcome"] == "warn"


async def test_foreign_session_failure_does_not_limit_successful_scan(runtime):
    runtime.foreign_failure = True
    runtime.detection = RepealDetectionResult(results=[orphan()])
    delta = await node.check_incomplete_repeal_node(runtime.state)
    assert len(delta["repeal_review"].findings) == 1
    assert delta["repeal_review"].checks[0].status is CheckStatus.COMPLETED
    assert runtime.events[-1]["outcome"] == "found"
    assert "branch_failures" not in delta


@pytest.mark.parametrize("is_subpoint", [False, True])
async def test_missing_act_read_is_reported_by_detector_and_limits_review(runtime, monkeypatch, is_subpoint):
    clause = SimpleNamespace(text=CLAUSE)
    target = RepealTarget(number="21-п", is_subpoint=is_subpoint, subpoint="1" if is_subpoint else None)

    async def missing_act(*args, **kwargs):
        return None

    monkeypatch.setattr(detector_tools, "retrieve_act", missing_act)
    runtime.detection = await detector_tools.detect_orphan_amendments_with_status(None, [(target, clause, CLAUSE)])
    assert not runtime.detection.complete
    assert runtime.detection.blocked_bases == (() if is_subpoint else ("21-п",))
    assert runtime.detection.blocked_subpoints == (("21-п:1",) if is_subpoint else ())
    delta = await node.check_incomplete_repeal_node(runtime.state)
    check = delta["repeal_review"].checks[0]
    assert check.status is CheckStatus.LIMITED
    assert ("21-п:1" if is_subpoint else "21-п") in check.reason
    assert runtime.events[-1]["outcome"] == "warn"


@pytest.mark.parametrize("has_amender", [False, True])
@pytest.mark.parametrize("links_available", [False, True])
async def test_unread_reverse_links_limit_chain_even_with_known_amenders(monkeypatch, has_amender, links_available):
    raw = SimpleNamespace(
        title="Приказ от 01.02.2024 № 21-п",
        full_text="Текст базового акта.",
        version=SimpleNamespace(coverage=SimpleNamespace(status=detector_tools.ReferenceCoverageStatus.COMPLETE)),
    )
    amenders = [Amender(number="83-п")] if has_amender else []
    evidence = (
        [RepealAmenderEvidence(source=source_evidence("83-п", 83), relation_proof="amendment_formula", repealed_by_project=False)]
        if has_amender
        else []
    )

    async def retrieve(*args, **kwargs):
        return raw

    async def links(*args):
        return [] if links_available else None

    async def verify(*args):
        return amenders, evidence, 0, {}

    monkeypatch.setattr(detector_tools, "retrieve_act", retrieve)
    monkeypatch.setattr(detector_tools, "has_currentness_evidence", lambda _: True)
    monkeypatch.setattr(detector_tools, "_source_evidence", lambda *args, **kwargs: source_evidence("21-п", 21))
    monkeypatch.setattr(detector_tools, "parse_amendment_chain", lambda _: amenders)
    monkeypatch.setattr(detector_tools, "amenders_from_links", links)
    monkeypatch.setattr(detector_tools, "_verify_amenders", verify)
    base = RepealTarget(number="21-п", date="01.02.2024")
    result, reason = await detector_tools._check_base_with_status(None, base, [base], SimpleNamespace(text=CLAUSE), CLAUSE)
    if not links_available:
        assert reason is RepealOutcomeReason.AMENDMENT_SOURCE_UNAVAILABLE
    else:
        assert reason is (RepealOutcomeReason.ORPHANS_FOUND if has_amender else RepealOutcomeReason.AMENDMENT_CHAIN_EMPTY)
    assert (result is not None) == has_amender


async def test_detector_error_returns_one_final_warning(runtime):
    runtime.error = ValueError("invalid detector result")
    delta = await node.check_incomplete_repeal_node(runtime.state)
    assert set(delta) == {"repeal_review"}
    assert delta["repeal_review"].findings == []
    assert delta["repeal_review"].checks[0].status is CheckStatus.NOT_CHECKED
    assert "Технический сбой" in delta["repeal_review"].checks[0].reason
    assert [event["status"] for event in runtime.events] == ["in_progress", "success"]
    assert runtime.events[-1]["outcome"] == "warn"


async def test_cancellation_propagates(runtime):
    runtime.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await node.check_incomplete_repeal_node(runtime.state)
    assert [event["status"] for event in runtime.events] == ["in_progress"]


async def test_project_without_repeal_finishes_without_transport(runtime, monkeypatch):
    text = "1. Утвердить прилагаемый Порядок.\n2. Контроль за исполнением приказа оставляю за собой."
    runtime.state["document_text"] = text
    runtime.state["parsed_document"] = SimpleNamespace(tree=build_document_structure_tree(text), enriched_text=text)

    def unavailable(_):
        raise AssertionError("No external capability is needed for a project without repeal")

    monkeypatch.setattr(node, "consultant_capabilities", unavailable)
    delta = await node.check_incomplete_repeal_node(runtime.state)
    assert runtime.calls == []
    assert delta["repeal_review"].checks[0].status is CheckStatus.COMPLETED
    assert runtime.events[-1]["outcome"] == "clear"


@pytest.mark.parametrize("listed", [False, True])
async def test_all_repeal_clauses_share_the_complete_project_list(monkeypatch, listed):
    second = "3. Признать утратившим силу приказ от 03.04.2024 № 83-п."
    text = CLAUSE + ("\n" + second if listed else "")
    targets = collect_repeal_targets(build_document_structure_tree(text))
    calls = []

    async def retrieve(_sessions, _query, *, requisites, **kwargs):
        calls.append(requisites.number)
        base = requisites.number == "21-п"
        return SimpleNamespace(
            title=f"Приказ Министерства социального развития от {requisites.date} № {requisites.number} "
            + ("Об утверждении Порядка" if base else "О внесении изменений"),
            full_text=("(в ред. приказа от 03.04.2024 № 83-п)" if base else "1. Внести изменения в приказ от 01.02.2024 № 21-п."),
            version=SimpleNamespace(
                coverage=SimpleNamespace(status=detector_tools.ReferenceCoverageStatus.COMPLETE),
                source=SimpleNamespace(base="RLAW076", doc_num=21 if base else 83),
            ),
        )

    async def links(*_):
        return []

    monkeypatch.setattr(detector_tools, "retrieve_act", retrieve)
    monkeypatch.setattr(detector_tools, "has_currentness_evidence", lambda _: True)
    monkeypatch.setattr(detector_tools, "amenders_from_links", links)
    detection = await detector_tools.detect_orphan_amendments_with_status(None, targets)
    assert detection.complete
    assert len(detection.results) == (0 if listed else 1)
    if not listed:
        result = detection.results[0]
        assert [item.number for item in result.orphans] == ["83-п"]
        assert result.evidence.orphan_source_ids == (("RLAW076", 83),)
        assert review_tools.orphan_to_risk(result, text) is not None
    else:
        assert calls.count("83-п") == 2  # own repeal target and verified editor of the base
        assert targets[0][2] == CLAUSE
        assert targets[1][2] == second


def test_repeal_list_continuations_and_multiple_clauses_keep_their_locations():
    first = "2. Признать утратившими силу:\nприказ от 01.02.2024 № 21-п;\nприказ от 03.04.2024 № 83-п."
    second = "3. Отменить приказ от 05.06.2024 № 44-п."
    targets = collect_repeal_targets(build_document_structure_tree(first + "\n" + second))
    assert [target.number for target, _, _ in targets] == ["21-п", "83-п", "44-п"]
    assert targets[0][2] == targets[1][2] == first
    assert targets[2][2] == second


def test_repeal_in_quoted_replacement_is_not_a_project_order():
    text = (
        "1. Пункт 2 изложить в следующей редакции:\n«2. Признать утратившим силу приказ от 01.02.2024 № 21-п.»\n"
        "3. Признать утратившим силу приказ от 03.04.2024 № 83-п."
    )
    targets = collect_repeal_targets(build_document_structure_tree(text))
    assert [target.number for target, _, _ in targets] == ["83-п"]


def test_repeated_repeal_target_is_checked_once_but_distinct_dates_are_kept():
    text = (
        CLAUSE + "\n3. Признать утратившим силу приказ от 01.02.2024 № 21-п."
        "\n4. Признать утратившим силу приказ от 01.02.2025 № 21-п."
    )
    targets = collect_repeal_targets(build_document_structure_tree(text))
    assert [(target.number, target.date) for target, _, _ in targets] == [("21-п", "01.02.2024"), ("21-п", "01.02.2025")]


def test_same_number_with_different_date_does_not_cover_an_editor():
    result = detector_tools.diff_orphans(
        "21-п", [RepealTarget(number="83-п", date="01.02.2025")], [Amender(number="83-п", date="01.02.2024")]
    )
    assert [item.number for item in result.orphans] == ["83-п"]


@pytest.mark.parametrize("changed", ["repealed", "unknown_source", "wrong_number", "duplicate_source"])
def test_chain_rejects_inconsistent_orphan_identity(changed):
    data = orphan().evidence.model_dump()
    if changed == "repealed":
        data["amenders"][0]["repealed_by_project"] = True
    elif changed == "unknown_source":
        data["orphan_source_ids"] = (("RLAW076", 44),)
    elif changed == "wrong_number":
        data["orphan_numbers"] = ("44-п",)
    else:
        data["orphan_source_ids"] *= 2
    with pytest.raises(ValidationError):
        RepealChainEvidence.model_validate(data)


@pytest.mark.parametrize("current", [False, True])
async def test_redundant_repeal_requires_current_source_and_positive_point_annotation(monkeypatch, current):
    raw = SimpleNamespace(
        title="Приказ от 01.02.2024 № 21-п",
        full_text="1. Утратил силу (приказ от 03.04.2024 № 83-п).",
        version=SimpleNamespace(
            coverage=SimpleNamespace(status=detector_tools.ReferenceCoverageStatus.COMPLETE),
            source=SimpleNamespace(base="RLAW076", doc_num=21),
        ),
    )

    async def retrieve(*args, **kwargs):
        return raw

    monkeypatch.setattr(detector_tools, "retrieve_act", retrieve)
    monkeypatch.setattr(detector_tools, "has_currentness_evidence", lambda _: current)
    target = RepealTarget(number="21-п", date="01.02.2024", is_subpoint=True, subpoint="1")
    result, _ = await detector_tools._check_redundant_repeal(None, target, SimpleNamespace(text=CLAUSE), CLAUSE)
    assert (result is not None) == current
    if current:
        assert result.repealed_by_number == "83-п"
        assert review_tools.redundant_repeal_to_risk(result, CLAUSE) is not None
        raw.full_text = "1. Внести изменения в Порядок."
        assert (await detector_tools._check_redundant_repeal(None, target, SimpleNamespace(text=CLAUSE), CLAUSE))[0] is None


@pytest.mark.parametrize("invalid", [None, "different_scope", "missing_clause"])
def test_unit_gap_is_published_only_with_matching_scope_and_project_quote(invalid):
    base = RepealTarget(number="21-п", date="01.02.2024", kind="приказ")
    issuer = "Министерства социального развития Ульяновской области"
    text = (
        f"1. Внести изменения в приказ {issuer} от 01.02.2024 № 21-п.\n"
        f"2. Внести изменения в приказ {issuer} от 01.02.2024 № 21-п."
    )
    assessment = detector_tools.assess_unit_scope(
        amender_text=text,
        base_number=base.number,
        base_date=base.date,
        base_kind=base.kind,
        base_issuer=issuer,
        repealed_units=("1",),
    )
    scope = detector_tools._unit_scope_evidence(assessment, base, issuer, ("1",))
    assert scope is not None and scope.uncovered_units == ("2",)
    if invalid == "different_scope":
        scope = scope.model_copy(update={"bound_units": ("1", "3"), "uncovered_units": ("3",)})
    amender = Amender(number="83-п", date="03.04.2024")
    editor = RepealAmenderEvidence(
        source=source_evidence(amender.number, 83).model_copy(update={"act_date": amender.date}),
        relation_proof="amendment_formula",
        repealed_by_project=False,
        unit_scope=scope,
    )
    evidence = RepealChainEvidence(base=source_evidence(base.number, 21), amenders=(editor,))
    result = OrphanResult(
        base_number=base.number,
        base_date=base.date,
        clause_marker_text=CLAUSE,
        clause_quote=CLAUSE if invalid != "missing_clause" else "Другой пункт.",
        evidence=evidence,
    )
    risk = review_tools.unit_scope_to_risk(result, UnitScopeGap(amender=amender, assessment=assessment), CLAUSE)
    assert (risk is not None) == (invalid is None)
    if risk is not None:
        assert "пункт 2" in risk.recommendation
        assert "Иные положения" in risk.recommendation
