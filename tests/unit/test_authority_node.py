from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.agents.enums.common.review import CheckStatus
from src.agents.enums.competence.review import CompetenceScope
from src.agents.enums.legal_base.source_reading import ReadingStatus
from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.schemas.common.review import LegalSource
from src.agents.schemas.competence.parse import CompetenceParse
from src.agents.schemas.competence.sources import CompetenceSources
from src.agents.schemas.legal_base.retrieval import RawAct
from src.agents.schemas.legal_base.source_reading import SourceReading
from src.agents.schemas.legal_base.source_refs import SourceRef
from src.agents.schemas.parse.parsed_document import ParsedDocument
from src.agents.steps import check_authority_node as authority
from src.agents.steps_tools.authority import progress as authority_progress
from src.agents.steps_tools.document.structure import build_document_structure_tree
from src.common.config.config import settings


async def test_missing_header_finishes_without_model_or_source_calls(monkeypatch):
    generate = AsyncMock()
    collect = AsyncMock()
    events = Mock(wraps=authority.emit_ui_event)
    monkeypatch.setattr(authority, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(authority, "collect_competence_sources", collect)
    monkeypatch.setattr(authority, "emit_ui_event", events)
    monkeypatch.setattr(authority_progress, "emit_ui_event", events)
    existing = LegalSource(id="existing", text="Сохранённый источник.")

    result = await authority.check_authority_node({"document_text": "Проект.", "legal_sources": [existing]})

    check = result["competence_review"].checks[0]
    assert check.status is CheckStatus.NOT_CHECKED
    assert "Не получена шапка" in check.reason
    assert result["competence_review"].scope is CompetenceScope.UNKNOWN
    assert result["competence_refs"] is None
    assert result["legal_sources"] == [existing]
    generate.assert_not_awaited()
    collect.assert_not_awaited()
    assert [call.kwargs["status"] for call in events.call_args_list] == ["in_progress", "success"]
    assert events.call_args.kwargs["outcome"] == "warn"


@pytest.mark.parametrize("has_source_text", [False, True], ids=["no_source_text", "over_budget"])
async def test_unusable_materials_skip_verdict_and_preserve_refs(monkeypatch, has_source_text):
    document = "Министерство социального развития Ульяновской области\nПРИКАЗ\nУтвердить порядок."
    parsed = ParsedDocument(enriched_text=document, tree=build_document_structure_tree(document))
    requisites = CompetenceParse(
        issuer="Министерство социального развития Ульяновской области",
        region="Ульяновской области",
        form="приказ",
        action="approve",
        target_act=None,
        cited_acts=[],
    )
    readings = ()
    if has_source_text:
        readings = (
            SourceReading(
                ref=SourceRef(
                    kind="federal_law", number="1-ФЗ", date="01.01.2026", role=SourceRole.STATUTE, origin=SourceOrigin.HEADER
                ),
                status=ReadingStatus.COMPLETE,
                raw=RawAct(query="", base="LAW", doc_num=1, title="Закон", full_text="Полномочия органа."),
            ),
        )
    generate = AsyncMock(return_value=SimpleNamespace(value=requisites))
    collect = AsyncMock(return_value=CompetenceSources(readings=readings, limitations=("Ограничение чтения.",), statute=None))
    events = Mock(wraps=authority.emit_ui_event)
    monkeypatch.setattr(authority, "llm", SimpleNamespace(generate_structured=generate))
    monkeypatch.setattr(authority, "collect_competence_sources", collect)
    monkeypatch.setattr(authority, "emit_ui_event", events)
    monkeypatch.setattr(authority_progress, "emit_ui_event", events)
    monkeypatch.setattr(
        authority,
        "settings",
        {
            "agents.competence.read_timeout_s": 1,
            "agents.competence.verdict_input_chars": len(document) - 1,
            "agents.competence.review.no_source_text": settings["agents.competence.review.no_source_text"],
            "agents.competence.review.input_over_budget": settings["agents.competence.review.input_over_budget"],
        },
    )

    result = await authority.check_authority_node({"document_text": document, "parsed_document": parsed})

    review = result["competence_review"]
    assert review.checks[0].status is CheckStatus.NOT_CHECKED
    assert review.findings == []
    assert result["competence_refs"].issuer == requisites.issuer
    assert result["competence_refs"].region == requisites.region
    reason_key = "input_over_budget" if has_source_text else "no_source_text"
    assert settings[f"agents.competence.review.{reason_key}"] in review.checks[0].reason
    if not has_source_text:
        assert "Ограничение чтения." in review.checks[0].reason
    assert generate.await_count == 1
    assert generate.call_args.kwargs["role"] == "competence_parse"
    collect.assert_awaited_once()
    assert [call.kwargs["status"] for call in events.call_args_list] == ["in_progress", "success"]
    assert events.call_args.kwargs["outcome"] == "warn"
