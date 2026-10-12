from copy import deepcopy

import pytest

from src.agents.enums.common.review import CheckStatus
from src.agents.enums.competence.fragments import SourceMode
from src.agents.enums.competence.review import CompetenceScope
from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.schemas.competence.assessment import CompetenceAssessment
from src.agents.schemas.competence.fragments import SourceView
from src.agents.schemas.legal_base.retrieval import RawAct
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.agents.schemas.legal_base.source_refs import SourceRef
from src.agents.steps_tools.authority.evidence import assessment_schema
from src.agents.steps_tools.authority.verdict import competence_verdict, stops_early

BLOCKS = {
    "P": {"source": "project", "quote": "Утвердить порядок.", "location": "пункт 1"},
    "N": {"source": "LAW:1", "quote": "Порядок утверждает Правительство.", "location": "статья 1"},
}
WIRE = {
    "authority": {
        "project_decision": "substantive_rule",
        "governed_decision": "substantive_rule",
        "project_evidence": ["P"],
        "norm_evidence": ["N"],
        "issuer_link_evidence": ["P"],
        "counterevidence": [],
        "resolution": "",
        "answer": "violated",
        "violation_scope": "act",
        "explanation": "Решение закреплено за другим органом.",
        "recommendation": "Передать проект Правительству.",
    },
    "form": {"answer": "compliant", "explanation": "Форма допустима.", "evidence": ["N"]},
    "limitations": [],
}


def review(wire):
    return competence_verdict(CompetenceAssessment.model_validate(wire), blocks=BLOCKS, document=BLOCKS["P"]["quote"], views=[])


@pytest.mark.parametrize("field", ["project_evidence", "norm_evidence", "issuer_link_evidence", "counterevidence"])
def test_unknown_authority_id_rejects_response(field):
    wire = deepcopy(WIRE)
    wire["authority"][field] = ["invented"]
    with pytest.raises(ValueError, match="invented authority evidence ID"):
        review(wire)


def test_unknown_form_id_rejects_response():
    wire = deepcopy(WIRE)
    wire["form"]["evidence"] = ["invented"]
    with pytest.raises(ValueError, match="invented answer evidence ID"):
        review(wire)


@pytest.mark.parametrize(
    "update",
    [
        {"project_evidence": ["N"]},
        {"norm_evidence": ["P"]},
        {"norm_evidence": []},
        {"governed_decision": "individual_decision"},
        {"counterevidence": ["N"], "resolution": ""},
        {"violation_scope": None},
        {"recommendation": ""},
    ],
)
def test_incomplete_or_contradictory_authority_cannot_confirm_violation(update):
    wire = deepcopy(WIRE)
    wire["authority"].update(update)
    result = review(wire)
    assert result.findings == [] and result.scope is CompetenceScope.UNKNOWN
    assert result.checks[0].status is CheckStatus.LIMITED
    assert not stops_early(result)


def test_form_violation_without_recommendation_is_withheld():
    wire = deepcopy(WIRE)
    wire["authority"].update(answer="not_established", violation_scope=None)
    wire["form"].update(answer="violated", evidence=["P", "N"])
    result = review(wire)
    assert result.findings == [] and result.scope is CompetenceScope.UNKNOWN
    assert result.checks[0].status is CheckStatus.LIMITED


def test_dynamic_schema_restricts_all_evidence_to_sent_blocks():
    schema = assessment_schema(BLOCKS).model_json_schema()
    reference = {"$ref": "#/$defs/AllowedBlock"}
    assert schema["$defs"]["AllowedBlock"]["enum"] == ["P", "N"]
    authority = schema["$defs"]["AuthorityAssessment"]["properties"]
    assert all(
        authority[field]["items"] == reference
        for field in ("project_evidence", "norm_evidence", "issuer_link_evidence", "counterevidence")
    )
    assert schema["$defs"]["FormAssessment"]["properties"]["evidence"]["items"] == reference


def source_view(complete):
    return SourceView(
        reading=SourceReading(
            ref=SourceRef(
                kind="federal_law", number="1-ФЗ", date="01.01.2026", role=SourceRole.CITED, origin=SourceOrigin.PREAMBLE
            ),
            status=ReadingStatus.COMPLETE if complete else ReadingStatus.TRUNCATED,
            raw=RawAct(query="", base="LAW", doc_num=1, title="Закон", full_text=BLOCKS["N"]["quote"]),
        ),
        mode=SourceMode.FULL,
        text=BLOCKS["N"]["quote"],
    )


@pytest.mark.parametrize("scope", ["act", "provisions"])
@pytest.mark.parametrize("complete", [False, True])
def test_grounded_violation_preserves_scope_and_early_stop(scope, complete):
    wire = deepcopy(WIRE)
    wire["authority"]["violation_scope"] = scope
    result = competence_verdict(
        CompetenceAssessment.model_validate(wire),
        blocks=BLOCKS,
        document=BLOCKS["P"]["quote"],
        views=[source_view(complete)],
    )
    assert result.scope is CompetenceScope(scope)
    assert len(result.findings) == 1
    assert [c.source for c in result.findings[0].citations] == ["project", "LAW:1"]
    assert result.checks[0].status is (CheckStatus.COMPLETED if complete else CheckStatus.LIMITED)
    assert stops_early(result) is (complete and scope == "act")


@pytest.mark.parametrize("question", ["authority", "form", "context", "input"])
def test_only_decisive_limitations_prevent_early_stop(question):
    wire = deepcopy(WIRE)
    wire["limitations"] = [{"question": question, "text": "Нет дополнительного материала."}]
    result = competence_verdict(
        CompetenceAssessment.model_validate(wire),
        blocks=BLOCKS,
        document=BLOCKS["P"]["quote"],
        views=[source_view(True)],
    )
    assert result.scope is CompetenceScope.ACT
    assert stops_early(result) is (question in {"form", "context"})
