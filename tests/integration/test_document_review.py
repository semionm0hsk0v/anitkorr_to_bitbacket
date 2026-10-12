from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

import src.agents.steps.check_authority_node as competence_module
from src.agents.enums.common.review import CheckStatus, RiskKind
from src.agents.enums.competence.review import CompetenceScope
from src.agents.enums.internal.detector import InternalCheck
from src.agents.enums.legal_base.currentness import (
    ReferenceCoverageStatus,
    ReferenceCurrentnessOutcome,
    ReferenceCurrentnessReason,
)
from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.graph import build_graph
from src.agents.llm import client as llm_client
from src.agents.schemas.common.review import Check, Citation, Risk
from src.agents.schemas.competence.assessment import CompetenceAssessment
from src.agents.schemas.competence.parse import CompetenceParse
from src.agents.schemas.competence.sources import CompetenceSources
from src.agents.schemas.internal.detector import DetectorScan
from src.agents.schemas.internal.findings import DetectorFinding
from src.agents.schemas.legal.discovery import DiscoveryOutcome, DiscoveryPlan, SearchQuery
from src.agents.schemas.legal.norm import Norm
from src.agents.schemas.legal.plan import SelectedAct, SourceSelection
from src.agents.schemas.legal.sources import LegalReading, LegalSources
from src.agents.schemas.legal.verdict import ComplianceIssue, LegalVerdict
from src.agents.schemas.legal_base.currentness import (
    ReferenceActIdentity,
    ReferenceCheckResult,
    ReferenceContentCoverage,
    ReferenceCurrentnessReceipt,
    ReferenceLiveFetchEvidence,
    ReferenceParagraphRange,
    ReferenceSourceCoordinates,
    ReferenceVersionFingerprint,
    reference_content_fingerprint,
)
from src.agents.schemas.legal_base.retrieval import RawAct
from src.agents.schemas.legal_base.source_refs import SourceRef
from src.agents.schemas.repeal.detector import Amender, OrphanResult, RepealDetectionResult
from src.agents.schemas.repeal.evidence import (
    RepealAmenderEvidence,
    RepealChainEvidence,
    RepealSourceEvidence,
)
from src.agents.steps import (
    check_internal_consistency_node as internal_node,
    plan_legal_search_node as legal_selection_stage,
    retrieve_legal_sources_node as legal_search_stage,
)
from src.agents.steps_tools.compliance.verdict import issue_to_risk
from src.agents.steps_tools.internal.detector import scan_internal
from src.agents.steps_tools.sources.currentness import (
    reference_metadata_fingerprint,
)
from src.agents.steps_tools.sources.source_reading import classify_reading
from src.agents.steps_tools.sources.trust import register_trusted_currentness_receipt, trusted_currentness_scope
from src.agents.utils.initial_state import assemble_initial_state
from src.common.config.config import settings


def wire_answer(answer, messages):
    human = messages[1]["content"] if isinstance(messages[1], dict) else messages[1].content
    human = human.split("Полный проект со всеми приложениями (source=project):\n", 1)[1]
    project, sources = human.split("\n\nПрочитанные источники:\n", 1)
    texts = {"project": project}
    for part in re.split(r"(?m)^source=", sources)[1:]:
        identity, body = part.split(" — ", 1)
        texts[identity] = body
    pool = {}
    for source, text in texts.items():
        for match in re.finditer(r"⟦([^⟧]+)⟧\n(.*?)(?=\n⟦|\Z)", text, re.DOTALL):
            pool[match[1]] = (source, " ".join(match[2].split()))

    def evidence(citations):
        return [
            identity
            for citation in citations
            for identity, (source, text) in pool.items()
            if source == citation.source and " ".join(citation.quote.split()) in text
        ]

    finding = answer.findings[0] if answer.findings else None
    violated = answer.scope in (CompetenceScope.ACT, CompetenceScope.PROVISIONS)
    citations = list(finding.citations) if violated and finding else ([] if violated else [GRANT])
    identities = evidence(citations)
    projects = [i for i in identities if pool[i][0] == "project"] or [i for i, (s, _) in pool.items() if s == "project"][:1]
    laws = [i for i in identities if pool[i][0] != "project"]
    return CompetenceAssessment.model_validate(
        {
            "authority": {
                "project_decision": "other",
                "governed_decision": "other",
                "project_evidence": projects,
                "norm_evidence": laws,
                "issuer_link_evidence": projects[:1],
                "counterevidence": [],
                "resolution": "",
                "answer": "violated" if violated else "compliant",
                "violation_scope": answer.scope.value if violated else None,
                "explanation": finding.explanation
                if finding
                else "Решение закреплено за другим органом."
                if violated
                else "Полномочие закреплено за органом.",
                "recommendation": finding.recommendation if finding else "Исправить." if violated else "",
            },
            "form": {"answer": "compliant", "explanation": "Вид акта допустим.", "evidence": evidence([GRANT])},
            "limitations": [],
        }
    )


def assert_block_finding(actual, expected):
    """Same finding and gold quotations, now contained in exact blocks with code-generated locations."""
    assert actual.model_dump(exclude={"citations"}) == expected.model_dump(exclude={"citations"})
    for citation in expected.citations:
        assert any(c.source == citation.source and citation.quote in c.quote for c in actual.citations)
    assert all("символы" in c.location and "(B" in c.location for c in actual.citations)


def _version(
    doc_num: int,
    text: str,
    *,
    edition: str,
    in_force: bool,
    coverage: ReferenceContentCoverage | None = None,
    par_count: int = 1,
) -> ReferenceVersionFingerprint:
    return ReferenceVersionFingerprint(
        source=ReferenceSourceCoordinates(base="RLAW076", doc_num=doc_num),
        identity=ReferenceActIdentity(
            title=f"Приказ Минсоцразвития Ульяновской области № {doc_num}-п",
            act_kind="Приказ",
            act_number=f"{doc_num}-п",
            issuer="Минсоцразвития Ульяновской области",
            jurisdiction="Ульяновская область",
        ),
        edition=edition,
        issued="22.05.2024",
        in_force=in_force,
        par_count=par_count,
        coverage=coverage
        or ReferenceContentCoverage(
            status=ReferenceCoverageStatus.COMPLETE,
            requested_ranges=[ReferenceParagraphRange(start=1, end=par_count)],
            fetched_ranges=[ReferenceParagraphRange(start=1, end=par_count)],
        ),
        content_fingerprint=reference_content_fingerprint(text),
    )


_ACTIVE_STATUS_SEARCH = "Все акты, кроме утративших силу, отмененных и не вступивших в силу"


def live_current_receipt(version: ReferenceVersionFingerprint) -> ReferenceCurrentnessReceipt:
    """CURRENT receipt of a completed fetch of the exact card (every recorded check passed)."""
    fingerprint = reference_metadata_fingerprint({"title": version.identity.title, "par_count": version.par_count})
    live = ReferenceLiveFetchEvidence(
        transport="mcp",
        fetched_at=datetime.now(UTC),
        analysis_date_supported=True,
        metadata_fingerprint_before=fingerprint,
        metadata_fingerprint_after=fingerprint,
        identity_checks=(ReferenceCheckResult(name="title_identity", passed=True),),
        version_checks=(ReferenceCheckResult(name="requested_range_fetched", passed=True),),
    )
    truncated = version.coverage.status is ReferenceCoverageStatus.TRUNCATED_CONFIGURED
    return ReferenceCurrentnessReceipt(
        outcome=ReferenceCurrentnessOutcome.CURRENT,
        reason=ReferenceCurrentnessReason.LIVE_CURRENT_CARD_TRUNCATED
        if truncated
        else ReferenceCurrentnessReason.LIVE_CURRENT_CARD,
        source=version.source,
        live=live,
        expected=version,
        observed=version,
    )


def current_act(
    doc_num: int, text: str, *, edition: str = "01.09.2026", status_search: str | None = _ACTIVE_STATUS_SEARCH
) -> RawAct:
    """A text the loader confirmed as the current edition (register the receipt to trust it)."""
    version = _version(doc_num, text, edition=edition, in_force=True)
    return RawAct(
        query="q",
        base="RLAW076",
        doc_num=doc_num,
        title=version.identity.title,
        edition=version.edition,
        in_force=True,
        par_count=1,
        full_text=text,
        version=version,
        currentness_receipt=live_current_receipt(version),
        status_search=status_search,
    )


def revoked_act(doc_num: int, cached_text: str) -> RawAct:
    """Old cached text and metadata next to a fresh confirmation that the act was revoked."""
    cached = _version(doc_num, cached_text, edition="01.02.2024", in_force=True)
    fresh = _version(doc_num, cached_text, edition="15.08.2026", in_force=False)
    receipt = ReferenceCurrentnessReceipt(
        source=fresh.source,
        outcome=ReferenceCurrentnessOutcome.STALE,
        reason=ReferenceCurrentnessReason.REVOKED,
        expected=cached,
        observed=fresh,
    )
    assert receipt.outcome is ReferenceCurrentnessOutcome.STALE
    assert receipt.reason is ReferenceCurrentnessReason.REVOKED
    return RawAct(
        query="q",
        base="RLAW076",
        doc_num=doc_num,
        title=cached.identity.title,
        edition=cached.edition,
        in_force=True,
        par_count=1,
        full_text=cached_text,
        version=cached,
        currentness_receipt=receipt,
    )


def live_revoked_act(doc_num: int) -> RawAct:
    """What the live loader returns once the card reports the act revoked: status, no text."""
    incomplete = ReferenceContentCoverage(status=ReferenceCoverageStatus.INCOMPLETE)
    fresh = _version(doc_num, "", edition="15.08.2026", in_force=False, coverage=incomplete)
    receipt = ReferenceCurrentnessReceipt(
        source=fresh.source,
        outcome=ReferenceCurrentnessOutcome.STALE,
        reason=ReferenceCurrentnessReason.REVOKED,
        observed=fresh,
    )
    return RawAct(
        query="q",
        base="RLAW076",
        doc_num=doc_num,
        title=fresh.identity.title,
        edition=fresh.edition,
        in_force=False,
        par_count=1,
        full_text="",
        version=fresh,
        currentness_receipt=receipt,
    )


def trust(*acts: RawAct) -> None:
    """What the live loader does for a receipt it issued in this request."""
    for act in acts:
        if act.currentness_receipt is not None:
            register_trusted_currentness_receipt(act.currentness_receipt)


USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}


def cite(quote: str, source: str = "project", location: str = "пункт 1") -> Citation:
    return Citation(source=source, location=location, quote=quote)


def risk(kind: RiskKind, *citations: Citation, explanation: str = "Обоснование нарушения.") -> Risk:
    return Risk(kind=kind, explanation=explanation, recommendation="Исправить.", citations=list(citations))


GRANT = Citation(source="RLAW076:1", location="Положение", quote="Полномочия Правительства области.")


STATUTE_REF = SourceRef(
    kind="regional_government_resolution",
    number="25/564-П",
    date="16.11.2018",
    title="О Министерстве социального развития Ульяновской области",
    raw="постановление Правительства Ульяновской области от 16.11.2018 № 25/564-П",
    role=SourceRole.STATUTE,
    origin=SourceOrigin.CATALOG,
)


@dataclass
class ScriptedCompetence:
    scope: CompetenceScope
    findings: tuple[Risk, ...]


def competence(scope: CompetenceScope, *findings: Risk) -> ScriptedCompetence:
    """Ожидаемый вывод; ссылки привязываются к реально переданным блокам в wire_answer."""
    return ScriptedCompetence(scope, findings)


def patch_competence_sources(monkeypatch, source: Any) -> None:
    "C1 с заданным ответом модели и C2 без сети: ``source`` — прочитанное Положение."

    parser = StructuredModel(
        [
            CompetenceParse(
                issuer="Министерство социального развития Ульяновской области",
                region="Ульяновской области",
                form="приказ",
                action="other",
                target_act=None,
                cited_acts=[],
            )
        ]
    )
    monkeypatch.setitem(llm_client.llm._models, "competence_parse", parser)

    async def collect(_sessions, _refs, *, analysis_date, timeout_s, authority_refs=()):
        if isinstance(source, BaseException):
            raise source
        if source is None:
            return CompetenceSources(
                readings=(), limitations=(settings["agents.competence.sources.statute_not_found"],), statute=None
            )
        trust(source)  # what the live loader does for a receipt it issued in this request
        reading = classify_reading(STATUTE_REF, source)
        limitations = tuple(text for text in (reading.limitation(),) if text)
        return CompetenceSources(readings=(reading,), limitations=limitations, statute=None)

    monkeypatch.setattr(competence_module, "collect_competence_sources", collect)


def orphan_result(clause: str) -> OrphanResult:
    """Proven detector result; the real node applies publication rules and builds the finding."""
    return OrphanResult(
        base_number="21-п",
        orphans=[Amender(number="83-п")],
        clause_marker_text=clause,
        evidence=RepealChainEvidence(
            base=RepealSourceEvidence(act_number="21-п", source_base="RLAW076", source_doc_num=1),
            amenders=(
                RepealAmenderEvidence(
                    source=RepealSourceEvidence(act_number="83-п", source_base="RLAW076", source_doc_num=2),
                    relation_proof="amendment_formula",
                    repealed_by_project=False,
                ),
            ),
            orphan_numbers=("83-п",),
            orphan_source_ids=(("RLAW076", 2),),
        ),
    )


class StructuredModel:
    "A structured role behind the real ``invoke_structured``: one scripted"

    model_name = "offline-structured"
    max_tokens = 8192
    max_retries = 0

    def __init__(self, results: Sequence[Any], before: Callable[[], Awaitable[None]] | None = None) -> None:
        self.results = list(results)
        self.before = before
        self.seen: list[list[BaseMessage]] = []

    def with_structured_output(self, schema, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        index = len(self.seen)
        self.seen.append(list(messages))
        if self.before is not None:
            await self.before()
        result = self.results[index]
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, ScriptedCompetence):
            result = wire_answer(result, messages)
        raw = AIMessage(content=json.dumps(result.model_dump(mode="json"), ensure_ascii=False), usage_metadata=USAGE)
        return {"parsed": result.model_dump(mode="json"), "raw": raw}


HIGHER_REF = SourceRef(
    kind="regional_government_resolution",
    number="298-П",
    date="14.07.2014",
    raw="постановлением Правительства Ульяновской области от 14.07.2014 № 298-П",
    role=SourceRole.CITED,
    origin=SourceOrigin.PREAMBLE,
)


def issue(project_quote: str, source_quote: str, source_id: str) -> ComplianceIssue:
    return ComplianceIssue(
        project_quote=project_quote,
        source_id=source_id,
        source_quote=source_quote,
        explanation="Положение проекта несовместимо с нормой.",
    )


def verdict(*issues: ComplianceIssue) -> LegalVerdict:
    return LegalVerdict(issues=list(issues))


def patch_compliance_sources(monkeypatch, source: Any) -> list[dict]:
    "План поиска без модели и загрузка без сети: source — заранее прочитанный акт."
    calls: list[dict] = []

    async def plan(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            value=SourceSelection(
                selected=[SelectedAct(candidate_id=1, project_quote="Решение принимается", reason="Регулирует сроки процедуры.")]
            )
        )

    async def read(_sessions, planned, *, region, analysis_date, timeout_s):
        if isinstance(source, BaseException):
            raise source
        if source is not None:
            trust(source)
        reading = classify_reading(HIGHER_REF, source)
        limitations = tuple(text for text in (reading.limitation(),) if text)
        return LegalSources(readings=(LegalReading(planned=planned[0], reading=reading),), limitations=limitations)

    async def prepared(*args, **kwargs):
        return SimpleNamespace(
            value=DiscoveryPlan(
                queries=[SearchQuery(query="порядок принятия решений", reason="Проверить сроки принятия решений.")]
            )
        )

    async def subject_search(*args, **kwargs):
        norm = Norm(**HIGHER_REF.model_dump(), bank="LAW", doc_num=1).model_copy(update={"origin": SourceOrigin.SEARCH})
        return DiscoveryOutcome(norms=[norm])

    async def named_search(acts, sessions, *, region):
        return [], [], []

    monkeypatch.setattr(legal_search_stage, "search_named_acts", named_search)
    monkeypatch.setattr(legal_search_stage, "search_discovery", subject_search)

    monkeypatch.setattr(legal_selection_stage, "llm", SimpleNamespace(generate_structured=prepared))
    monkeypatch.setattr(legal_search_stage, "llm", SimpleNamespace(generate_structured=plan))
    monkeypatch.setattr(legal_search_stage, "read_legal_sources", read)

    return calls


repeal_module = importlib.import_module("src.agents.steps.check_incomplete_repeal_node")


CLAUSE = "2. Признать утратившим силу приказ Министерства от 01.02.2024 № 21-п."


TABLE_ROW = "| 9 | Фотография | 1 шт. | для детей старше 14 лет |"


PROJECT = "\n".join(
    [
        "Министерство социального развития Ульяновской области",
        "ПРИКАЗ",
        "1. Решение принимается за 5 дней.",
        CLAUSE,
        "3. Решение принимается за 10 дней.",
        "ПРИЛОЖЕНИЕ № 1",
        TABLE_ROW,
        "КОНЕЦ ПРИЛОЖЕНИЯ",
    ]
)


LAW_ACT = current_act(1, "Статья 1. Правило закона. Статья 2. Полномочия Правительства области.", edition="10.01.2026")


LAW_SOURCE_ID = f"{LAW_ACT.base}:{LAW_ACT.doc_num}"


ALL_KINDS = (RiskKind.COMPETENCE, RiskKind.INTERNAL, RiskKind.COMPLIANCE, RiskKind.REPEAL)


INTERNAL = risk(
    RiskKind.INTERNAL,
    cite("Решение принимается за 5 дней."),
    cite("Решение принимается за 10 дней.", location="пункт 3"),
    explanation="Для одного решения установлены несовместимые сроки.",
)


COMPETENCE = risk(
    RiskKind.COMPETENCE, cite("ПРИКАЗ", location="вид акта"), cite("Полномочия Правительства области.", LAW_SOURCE_ID)
)


HIGHER_ACT = current_act(298, "Пункт 1. Правило закона: решение принимается за 5 дней.", edition="10.07.2026")


HIGHER_ID = f"{HIGHER_ACT.base}:{HIGHER_ACT.doc_num}"


COMPLIANCE_ISSUE = issue("Решение принимается за 10 дней.", "Правило закона: решение принимается за 5 дней.", HIGHER_ID)


COMPLIANCE = issue_to_risk(COMPLIANCE_ISSUE)


@pytest.fixture(autouse=True)
def internal(monkeypatch):
    """The internal detector's boundary: one finding by default; ``internal`` is what it was started with."""
    calls = []

    def detect(data):
        calls.append(data)
        return DetectorScan(findings=[DetectorFinding(check=InternalCheck.REFERENCE, risk=INTERNAL)])

    monkeypatch.setattr(internal_node, "scan_internal", detect)
    return calls


@pytest.fixture
def detector(monkeypatch):
    """The detector's domain boundary; orchestration and publication run in the real node."""
    calls: list[dict] = []
    options: dict[str, Any] = {
        "findings": True,
        "before": None,
    }

    async def detect(sessions, targets, *, region, analysis_date):
        calls.append({"targets": targets, "analysis_date": analysis_date})
        if options["before"] is not None:
            await options["before"]()
        return RepealDetectionResult(results=[orphan_result(CLAUSE)] if options["findings"] else [])

    monkeypatch.setattr(repeal_module, "consultant_capabilities", lambda _: SimpleNamespace(exact_retrieval=True))
    monkeypatch.setattr(repeal_module, "detect_orphan_amendments_with_status", detect)
    return calls, options


@dataclass
class Installed:
    expert: StructuredModel
    legal: StructuredModel
    plans: list[dict]


def install(
    monkeypatch,
    *,
    external: list[Any],
    legal: list[Any] | None = None,
    competence_source: Any = LAW_ACT,
    higher: Any = HIGHER_ACT,
) -> Installed:
    """``external`` — competence's one call; ``legal`` — the compliance blocks' verdicts, in call order."""
    expert = StructuredModel(external)
    checker = StructuredModel(legal if legal is not None else [verdict()])
    monkeypatch.setitem(llm_client.llm._models, "competence", expert)
    monkeypatch.setitem(llm_client.llm._models, "legal_verdict", checker)
    monkeypatch.setitem(llm_client.llm._models, "legal_scope", StructuredModel([verdict()]))
    monkeypatch.setitem(llm_client.llm._models, "legal_refusal", StructuredModel([LegalVerdict(issues=[])]))
    patch_competence_sources(monkeypatch, competence_source)
    plans = patch_compliance_sources(monkeypatch, higher)
    return Installed(expert, checker, plans)


def state(document: str = PROJECT, **extra):
    base = assemble_initial_state(source_name="Проект.docx", analysis_date="2026-09-23", document_text=document)
    return base | extra


async def run(initial=None):
    with trusted_currentness_scope():
        return await build_graph().compile().ainvoke(initial if initial is not None else state())


def checks_of(final) -> dict[RiskKind, Check]:
    return {check.kind: check for check in final["review_result"].checks}


def assert_only_internal_is_limited(final) -> None:
    """Смысловые противоречия не проверяются: внутреннее направление всегда ``limited``, остальные выполнены."""
    for kind, check in checks_of(final).items():
        expected = CheckStatus.LIMITED if kind is RiskKind.INTERNAL else CheckStatus.COMPLETED
        assert check.status is expected, kind
    assert checks_of(final)[RiskKind.INTERNAL].reason == settings.agents.internal.limit_reason


async def test_complete_review_produces_one_report(monkeypatch, detector, internal):
    """Весь проект проходит проверки и получает одно итоговое заключение."""
    calls, _ = detector
    installed = install(monkeypatch, external=[competence(CompetenceScope.NONE)], legal=[verdict(COMPLIANCE_ISSUE)])
    expert, checker = installed.expert, installed.legal
    document = PROJECT + "\n" + "содержание приложения " * 5000 + "\nПОСЛЕДНЯЯ СТРОКА"
    final = await run(initial=state(document))

    assert final["parsed_document"] is not None
    assert len(calls) == 1 and calls[0]["targets"][0][0].number == "21-п"
    assert calls[0]["analysis_date"] == "2026-09-23"
    # every role reads the complete project, tables included, without truncation
    assert document in internal[0].document_text and TABLE_ROW in internal[0].document_text
    competence_text = "\n".join(m["content"] for m in expert.seen[0] if m["role"] == "user")
    # C4 sends exact, addressed evidence blocks rather than one unaddressed string.
    assert "ПОСЛЕДНЯЯ СТРОКА" in competence_text and TABLE_ROW in competence_text
    assert "Полный проект со всеми приложениями" in competence_text
    (sent,) = checker.seen
    assert sent[0]["role"] == "system" and document not in sent[0]["content"]
    assert any(m["role"] == "user" and document in m["content"] and TABLE_ROW in m["content"] for m in sent)
    # the block carries the read act's full current text under its source id
    assert any(
        m["role"] == "user" and HIGHER_ID in m["content"] and "Правило закона: решение принимается за 5 дней." in m["content"]
        for m in sent
    )

    assert set(checks_of(final)) == set(ALL_KINDS)
    assert_only_internal_is_limited(final)
    kinds = [finding.kind for finding in final["review_result"].findings]
    assert kinds == [RiskKind.INTERNAL, RiskKind.COMPLIANCE, RiskKind.REPEAL]
    assert final["review_result"].findings[-1].citations[0].quote == CLAUSE

    assert final["token_usage_total"] == 5 * USAGE["total_tokens"]
    assert "compliance_sources" not in final


@pytest.mark.parametrize("reading", ["revoked", "live-revocation-without-text"])
async def test_unverified_law_limits_review(monkeypatch, detector, reading):
    """Неподтверждённый источник ограничивает проверку и не публикуется."""
    stale_text = "Пункт 1. Старый текст из кэша."
    act = {"revoked": revoked_act(298, stale_text), "live-revocation-without-text": live_revoked_act(298)}[reading]
    installed = install(monkeypatch, external=[competence(CompetenceScope.NONE)], higher=act)
    final = await run()
    assert installed.legal.seen == []  # nothing to check against: no block, no model call
    check = checks_of(final)[RiskKind.COMPLIANCE]
    assert check.status is CheckStatus.NOT_CHECKED
    assert "действующая редакция не подтверждена" in check.reason
    assert not [f for f in final["review_result"].findings if f.kind is RiskKind.COMPLIANCE]
    assert "compliance_sources" not in final


async def test_invented_legal_quote_is_not_published(monkeypatch, detector):
    """Цитата, которой нет в законе, не попадает в результат."""
    invented = issue("Решение принимается за 10 дней.", "Норма, которой в акте нет.", HIGHER_ID)
    other_act = issue("Решение принимается за 10 дней.", "Правило закона: решение принимается за 5 дней.", "LAW:999")
    installed = install(
        monkeypatch,
        external=[competence(CompetenceScope.NONE)],
        legal=[verdict(COMPLIANCE_ISSUE, invented, other_act)],
    )
    final = await run()
    assert len(installed.legal.seen) == 1
    (finding,) = [f for f in final["review_result"].findings if f.kind is RiskKind.COMPLIANCE]
    assert finding.explanation == COMPLIANCE.explanation
    assert [(c.source, c.quote) for c in finding.citations] == [(c.source, c.quote) for c in COMPLIANCE.citations]
    assert all("символы" in c.location for c in finding.citations)
    check = checks_of(final)[RiskKind.COMPLIANCE]
    assert check.status is CheckStatus.LIMITED and "не подтверждённых цитатами" in check.reason


async def test_confirmed_lack_of_authority_stops_review(monkeypatch, detector, internal):
    """Доказанное отсутствие полномочий на весь акт останавливает экспертизу."""
    calls, _ = detector
    installed = install(monkeypatch, external=[competence(CompetenceScope.ACT, COMPETENCE)])
    final = await run()

    assert len(installed.expert.seen) == 1 and installed.legal.seen == [] and installed.plans == []
    assert internal == []  # internal detector not started
    assert calls == []  # repeal detector not started
    checks = checks_of(final)
    assert checks[RiskKind.COMPETENCE].status is CheckStatus.COMPLETED
    for kind in (RiskKind.INTERNAL, RiskKind.COMPLIANCE, RiskKind.REPEAL):
        assert checks[kind].status is CheckStatus.NOT_CHECKED
        assert "полномочий на принятие акта в целом" in checks[kind].reason
    assert len(final["review_result"].findings) == 1
    assert_block_finding(final["review_result"].findings[0], COMPETENCE)
    assert "завершена досрочно" in final["review_summary"]


async def test_missing_authority_basis_does_not_stop_other_checks(monkeypatch, detector, internal):
    """Отсутствие основания полномочий не отменяет остальные проверки."""
    calls, _ = detector
    installed = install(monkeypatch, external=[], legal=[verdict(COMPLIANCE_ISSUE)], competence_source=None)
    final = await run()

    assert len(calls) == 1 and len(internal) == 1 and installed.expert.seen == []  # competence: no call
    checks = checks_of(final)
    assert checks[RiskKind.COMPETENCE].status is CheckStatus.NOT_CHECKED
    assert "Не получен текст ни одного источника" in checks[RiskKind.COMPETENCE].reason
    assert "Положение об органе-издателе не установлено" in checks[RiskKind.COMPETENCE].reason
    assert checks[RiskKind.INTERNAL].status is CheckStatus.LIMITED
    for kind in (RiskKind.COMPLIANCE, RiskKind.REPEAL):
        assert checks[kind].status is CheckStatus.COMPLETED
    assert "досрочно" not in final["review_summary"]


async def test_failed_legal_check_preserves_other_findings(monkeypatch, detector):
    """Сбой проверки законодательства сохраняет результаты остальных направлений."""
    install(
        monkeypatch,
        external=[competence(CompetenceScope.PROVISIONS, COMPETENCE)],
        legal=[RuntimeError("provider down")],
    )
    final = await run()
    checks = checks_of(final)
    findings = final["review_result"].findings
    assert_block_finding(next(f for f in findings if f.kind is RiskKind.COMPETENCE), COMPETENCE)
    assert INTERNAL in findings
    assert [f.citations[0].quote for f in findings if f.kind is RiskKind.REPEAL] == [CLAUSE]
    assert checks[RiskKind.COMPLIANCE].status is CheckStatus.LIMITED
    assert "Сверка не выполнена" in checks[RiskKind.COMPLIANCE].reason
    # repeal is the detector's alone: a failed compliance check does not touch it
    assert checks[RiskKind.REPEAL].status is CheckStatus.COMPLETED


async def test_cancellation_stops_review(monkeypatch, detector):
    """Отмена экспертизы прекращает новые вызовы модели."""
    _, options = detector
    blocked = asyncio.Event()

    async def hang():
        blocked.set()
        await asyncio.Event().wait()

    options["before"] = hang
    installed = install(monkeypatch, external=[competence(CompetenceScope.NONE)])
    installed.legal.before = hang
    task = asyncio.create_task(run())
    await asyncio.wait_for(blocked.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(installed.expert.seen) == 1 and len(installed.legal.seen) <= 1


DEAD_REFERENCE = "4. Документы предоставляются в соответствии с пунктом 9 настоящего Порядка."


PROJECT_WITH_DEAD_REFERENCE = PROJECT.replace("ПРИЛОЖЕНИЕ № 1", DEAD_REFERENCE + "\nПРИЛОЖЕНИЕ № 1", 1)


@pytest.fixture
def real_internal_detector(monkeypatch):
    """The real scan of the internal detector instead of the scripted boundary (it runs on the project text)."""
    monkeypatch.setattr(internal_node, "scan_internal", scan_internal)


async def test_missing_document_reference_is_reported(monkeypatch, detector, real_internal_detector):
    """Настоящий детектор находит ссылку на отсутствующий пункт и показывает границы проверки."""
    install(monkeypatch, external=[competence(CompetenceScope.NONE)], legal=[verdict(COMPLIANCE_ISSUE)])
    final = await run(initial=state(PROJECT_WITH_DEAD_REFERENCE))

    (found,) = [finding for finding in final["review_result"].findings if finding.kind is RiskKind.INTERNAL]
    assert found.citations[0].source == "project" and "пунктом 9 настоящего Порядка" in found.citations[0].quote
    assert found.citations[0].location == "пункт 4"
    checks = checks_of(final)
    assert checks[RiskKind.INTERNAL].status is CheckStatus.LIMITED
    assert checks[RiskKind.INTERNAL].reason == settings.agents.internal.limit_reason
    assert "смысловые противоречия не проверялись" in checks[RiskKind.INTERNAL].reason
    for kind in (RiskKind.COMPETENCE, RiskKind.COMPLIANCE, RiskKind.REPEAL):
        assert checks[kind].status is CheckStatus.COMPLETED

    assert final["review_result"].findings[0] is found


async def test_authority_generation_failure_preserves_refs_and_read_sources(monkeypatch, detector):
    install(monkeypatch, external=[RuntimeError("provider unavailable")])
    final = await run()
    assert checks_of(final)[RiskKind.COMPETENCE].status is CheckStatus.NOT_CHECKED
    assert final["competence_refs"].region == "Ульяновской области"
    assert final["competence_refs"].issuer == "Министерство социального развития Ульяновской области"
    source = next(s for s in final["legal_sources"] if s.id == LAW_SOURCE_ID)
    assert source.text == LAW_ACT.full_text
    assert source.edition["currentness"] == "CURRENT"
    assert source.edition["edition"] == LAW_ACT.edition
    assert checks_of(final)[RiskKind.COMPLIANCE].status is CheckStatus.COMPLETED


async def test_authority_source_failure_preserves_refs_and_continues(monkeypatch, detector):
    installed = install(monkeypatch, external=[], competence_source=RuntimeError("invalid source response"))
    final = await run()
    assert final["competence_refs"].region == "Ульяновской области"
    assert installed.expert.seen == []
    check = checks_of(final)[RiskKind.COMPETENCE]
    assert check.status is CheckStatus.NOT_CHECKED
    assert "Технический сбой получения источников" in check.reason
    assert checks_of(final)[RiskKind.COMPLIANCE].status is CheckStatus.COMPLETED


async def test_authority_parse_failure_continues_without_inventing_refs(monkeypatch, detector):
    installed = install(monkeypatch, external=[])
    monkeypatch.setitem(llm_client.llm._models, "competence_parse", StructuredModel([RuntimeError("parse failed")]))
    final = await run()
    assert final["competence_refs"] is None
    assert installed.expert.seen == []
    check = checks_of(final)[RiskKind.COMPETENCE]
    assert check.status is CheckStatus.NOT_CHECKED and "разбор шапки" in check.reason
    assert checks_of(final)[RiskKind.REPEAL].status is CheckStatus.COMPLETED


async def test_unconfirmed_authority_violation_does_not_stop_graph(monkeypatch, detector, internal):
    invented = risk(RiskKind.COMPETENCE, cite("ПРИКАЗ"), cite("Несуществующая норма.", LAW_SOURCE_ID))
    install(monkeypatch, external=[competence(CompetenceScope.ACT, invented)])
    final = await run()
    assert final["competence_review"].findings == []
    assert final["competence_review"].scope is CompetenceScope.UNKNOWN
    assert checks_of(final)[RiskKind.COMPETENCE].status is CheckStatus.LIMITED
    assert internal and detector[0]


async def test_partial_authority_source_keeps_finding_without_early_stop(monkeypatch, detector, internal):
    coverage = ReferenceContentCoverage(
        status=ReferenceCoverageStatus.TRUNCATED_CONFIGURED,
        requested_ranges=[ReferenceParagraphRange(start=1, end=1)],
        fetched_ranges=[ReferenceParagraphRange(start=1, end=1)],
    )
    version = _version(1, LAW_ACT.full_text, edition="10.01.2026", in_force=True, coverage=coverage, par_count=2)
    source = LAW_ACT.model_copy(update={"version": version, "par_count": 2, "currentness_receipt": live_current_receipt(version)})
    install(monkeypatch, external=[competence(CompetenceScope.ACT, COMPETENCE)], competence_source=source)
    final = await run()
    assert final["competence_review"].scope is CompetenceScope.ACT
    assert final["competence_review"].findings
    assert checks_of(final)[RiskKind.COMPETENCE].status is CheckStatus.LIMITED
    assert internal and detector[0]


@pytest.mark.parametrize("role", ["competence_parse", "competence"])
async def test_authority_cancellation_propagates(monkeypatch, role):
    install(monkeypatch, external=[])
    model = StructuredModel([asyncio.CancelledError()])
    monkeypatch.setitem(llm_client.llm._models, role, model)
    with pytest.raises(asyncio.CancelledError):
        await run()
