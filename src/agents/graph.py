from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.agents.schemas.common.branch_output import ComplianceBranchOutput, LegalBranchOutput
from src.agents.schemas.legal.state import LegalReviewState
from src.agents.state import PipelineState
from src.agents.steps.build_report_node import build_report_node
from src.agents.steps.check_authority_node import check_authority_node
from src.agents.steps.check_incomplete_repeal_node import check_incomplete_repeal_node
from src.agents.steps.check_internal_consistency_node import check_internal_consistency_node
from src.agents.steps.check_legal_compliance_node import check_legal_compliance_node
from src.agents.steps.plan_legal_search_node import plan_legal_search_node
from src.agents.steps.prepare_document_node import prepare_document_node
from src.agents.steps.retrieve_legal_sources_node import retrieve_legal_sources_node
from src.agents.steps_tools.authority.verdict import stops_early
from src.agents.steps_tools.sources.trust import trusted_currentness_scope
from src.agents.utils.accounting import accounted_node
from src.agents.utils.graph_state import ACCUMULATING_CHANNELS
from src.agents.utils.node_logging import logged_node
from src.agents.utils.streaming import FRAGMENT_REVIEW_SUMMARY
from src.common.schemas.models import Message, MessageFragment

#: Branch node of the external chain and its declared output channels.
EXTERNAL_REVIEW_BRANCH = "external_review"


def _stage(fn: Any, name: str, *, usage_channel: str | None = None) -> Any:
    """Audit, token accounting and lifecycle log — the same around every stage."""
    return accounted_node(logged_node(fn, name=name), stage=name, usage_channel=usage_channel)


async def prepare_document(state: PipelineState) -> dict:
    """Preparation: the complete project is required; its tree feeds the repeal detector."""
    return await prepare_document_node(state)


def _compliance_review_branch() -> Any:
    "Подготовка запросов → поиск и отбор актов → сверка, с логами и учётом токенов каждого шага."
    stages = [
        ("plan_legal_search", plan_legal_search_node),
        ("retrieve_legal_sources", retrieve_legal_sources_node),
        ("check_legal_compliance", check_legal_compliance_node),
    ]
    sub: StateGraph = StateGraph(LegalReviewState, input_schema=PipelineState, output_schema=ComplianceBranchOutput)
    for name, fn in stages:
        sub.add_node(name, _stage(fn, name, usage_channel="legal_stage_usage"))
    sub.add_edge(START, "plan_legal_search")
    sub.add_conditional_edges(
        "plan_legal_search",
        lambda state: "check_legal_compliance" if state.get("legal_skip_reason") else "retrieve_legal_sources",
        ["retrieve_legal_sources", "check_legal_compliance"],
    )
    sub.add_edge("retrieve_legal_sources", "check_legal_compliance")
    sub.add_edge("check_legal_compliance", END)
    return sub.compile(name="compliance_review")


def _external_review_branch() -> Any:
    "Отмена актов ∥ весь подграф сверки: цепочка не ждёт завершения детектора отмены."
    sub: StateGraph = StateGraph(PipelineState)
    sub.add_node("check_incomplete_repeal", _stage(check_incomplete_repeal_node, "check_incomplete_repeal"))
    sub.add_node("compliance_review", _compliance_review_branch())
    for name in ("check_incomplete_repeal", "compliance_review"):
        sub.add_edge(START, name)
        sub.add_edge(name, END)
    compiled = sub.compile(name=EXTERNAL_REVIEW_BRANCH)
    owned = frozenset(LegalBranchOutput.__annotations__)

    async def external_review(state: PipelineState) -> dict:

        upstream = {key: value for key, value in state.items() if key not in ACCUMULATING_CHANNELS}
        result = await compiled.ainvoke(upstream)
        return {key: value for key, value in result.items() if key in owned}

    return logged_node(external_review, name=EXTERNAL_REVIEW_BRANCH)


def _route_after_authority_check(entries: list[str]):
    def route(state: PipelineState) -> list[str] | str:
        return "build_report" if stops_early(state.get("competence_review")) else entries

    return route


def build_graph() -> StateGraph:
    async def build_report(state: PipelineState) -> dict:
        return await build_report_node(state)

    graph: StateGraph = StateGraph(PipelineState)
    graph.add_node("prepare_document", _stage(prepare_document, "prepare_document"))
    graph.add_node("build_report", _stage(build_report, "build_report"))
    graph.add_node("check_internal_consistency", _stage(check_internal_consistency_node, "check_internal_consistency"))
    graph.add_node(EXTERNAL_REVIEW_BRANCH, _external_review_branch())
    graph.add_node("check_authority", _stage(check_authority_node, "check_authority"))
    entries = ["check_internal_consistency", EXTERNAL_REVIEW_BRANCH]
    graph.add_edge(START, "prepare_document")
    graph.add_edge("prepare_document", "check_authority")
    graph.add_conditional_edges("check_authority", _route_after_authority_check(entries), ["build_report", *entries])
    # The report starts once, after every started branch has finished.
    graph.add_edge(entries, "build_report")
    graph.add_edge("build_report", END)
    return graph


async def astream_events(app, initial_state: PipelineState) -> AsyncGenerator[MessageFragment | Message | dict[str, Any]]:
    "MCP review stream; a failed review never emits a successful completion."
    report = None
    note = None
    competence_refs = None
    parsed_document = None
    with trusted_currentness_scope():
        async for _namespace, mode, payload in app.astream(initial_state, stream_mode=["custom", "updates"], subgraphs=True):
            if not isinstance(payload, dict):
                continue
            for delta in payload.values():
                if not isinstance(delta, dict):
                    continue
                if mode == "custom":
                    for event in delta.get("events") or []:
                        yield event
                else:
                    if delta.get("competence_refs") is not None:
                        competence_refs = delta["competence_refs"]
                    if delta.get("parsed_document") is not None:
                        parsed_document = delta["parsed_document"]
                    if delta.get("review_result"):
                        report = delta["review_result"]
                    if delta.get("review_summary"):
                        note = delta["review_summary"]

    if not note or not report:
        raise RuntimeError("Review completed without a report")
    message = initial_state.get("output_message")
    if isinstance(message, Message):
        yield message
    yield {"review": report, "summary": note, "competence_refs": competence_refs, "parsed_document": parsed_document}
    yield MessageFragment(fragment_id=FRAGMENT_REVIEW_SUMMARY, fragment_type="response", status="success", content=note)
