from __future__ import annotations

from src.agents.enums.parse.npa_type import NpaTypeDetection
from src.agents.schemas.parse.npa_type import NpaType
from src.agents.schemas.parse.parsed_document import ParsedDocument
from src.agents.steps_tools.document.act_basis import basis_from_context, extract_act_basis
from src.agents.steps_tools.document.references import collect_internal_addresses
from src.agents.steps_tools.document.structure import build_document_structure_tree, count_structure_nodes
from src.agents.utils.streaming import FRAGMENT_PREPARE_DOCUMENT, emit_ui_event, progress_text
from src.common.utils.logger import get_logger
from src.infrastructure.knowledge.social_matrix import in_scope_type_ids

log = get_logger(__name__)

STEP_NAME = "prepare_document"
FRAGMENT_ID = FRAGMENT_PREPARE_DOCUMENT
LABEL = progress_text("parse")


async def prepare_document_node(state) -> dict:
    emit_ui_event(state, STEP_NAME, FRAGMENT_ID, status="in_progress", content=LABEL)

    document_text = state.get("document_text") or ""
    if not document_text.strip():
        emit_ui_event(state, STEP_NAME, FRAGMENT_ID, status="success", content=progress_text("parse_none"), outcome="warn")
        return {"npa_type": NpaType(out_of_scope=True)}

    tree = build_document_structure_tree(document_text)

    act_basis = basis_from_context(state.get("context_documents"), extract_act_basis(document_text))
    parsed = ParsedDocument(enriched_text=document_text, tree=tree, act_basis=act_basis, table_cell_count=0)
    npa_type = NpaType(matched_types=in_scope_type_ids(), out_of_scope=False, detection=NpaTypeDetection.DISABLED)

    basis_refs = [r.raw for r in act_basis.basis_refs] if act_basis else []
    await log.ainfo(
        "prepare_document",
        nodes=count_structure_nodes(tree.root),
        addresses=len(collect_internal_addresses(tree)),
        types=npa_type.matched_types,
        basis_refs=basis_refs,
    )
    done = (
        progress_text("parse_done_basis").format(basis=", ".join(basis_refs))
        if basis_refs
        else progress_text("parse_done_no_basis")
    )
    emit_ui_event(state, STEP_NAME, FRAGMENT_ID, status="success", content=done, outcome=("clear" if basis_refs else "warn"))
    return {"parsed_document": parsed, "npa_type": npa_type}
