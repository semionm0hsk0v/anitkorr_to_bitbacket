from __future__ import annotations

from collections.abc import Callable

from src.agents.enums.internal.detector import InternalCheck
from src.agents.schemas.internal.detector import DetectorInput, DetectorScan
from src.agents.schemas.internal.findings import DetectorFinding
from src.agents.schemas.internal.frames.project_frames import BuiltProjectFrame
from src.agents.steps_tools.document.norm_fragments import iter_normative_fragments
from src.agents.steps_tools.internal.checks.appendices import appendix_findings
from src.agents.steps_tools.internal.checks.references import reference_findings
from src.agents.steps_tools.internal.checks.same_variable import same_variable_findings
from src.agents.steps_tools.internal.checks.service_title import service_title_findings
from src.agents.steps_tools.internal.checks.time_limits import time_limit_findings
from src.agents.steps_tools.internal.frames.project_frames import prepare_project_frames
from src.common.utils.logger import get_logger

log = get_logger(__name__)


Check = Callable[[DetectorInput], list[DetectorFinding]]


def _references(data: DetectorInput) -> list[DetectorFinding]:
    return reference_findings(data.parsed.tree, table_geometry=data.table_geometry)


def _time_limits(data: DetectorInput) -> list[DetectorFinding]:
    return time_limit_findings(detector_frames(data), document_text=data.document_text)


def _same_variable(data: DetectorInput) -> list[DetectorFinding]:
    return same_variable_findings(detector_frames(data), document_text=data.document_text)


def _appendices(data: DetectorInput) -> list[DetectorFinding]:
    return appendix_findings(data.document_text, data.parsed)


def _service_title(data: DetectorInput) -> list[DetectorFinding]:
    return service_title_findings(data.document_text, data.parsed)


#: Порядок проверок задаёт порядок находок в выдаче.
CHECKS: tuple[tuple[InternalCheck, Check], ...] = (
    (InternalCheck.REFERENCE, _references),
    (InternalCheck.TIME_LIMIT, _time_limits),
    (InternalCheck.SAME_VARIABLE, _same_variable),
    (InternalCheck.APPENDIX, _appendices),
    (InternalCheck.SERVICE_TITLE, _service_title),
)


def scan_internal(data: DetectorInput) -> DetectorScan:
    scan = DetectorScan()
    for check, run in CHECKS:
        try:
            scan.findings.extend(run(data))
        except Exception as exc:
            scan.failed[check] = type(exc).__name__
            log.warning("internal_check_failed", check=check.value, error_class=type(exc).__name__)
    return scan


def detector_frames(data: DetectorInput) -> list[BuiltProjectFrame]:
    """Операнды норм, построенные кодом (без LLM); считаются один раз на проект и делятся между проверками."""
    if data._frames is None:
        data._frames = prepare_project_frames(
            iter_normative_fragments(data.parsed.tree.root), document_text=data.document_text, doc_title=data.source_name
        )
    return data._frames
