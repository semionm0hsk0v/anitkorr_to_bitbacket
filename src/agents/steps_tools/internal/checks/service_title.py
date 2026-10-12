from __future__ import annotations

import re
import unicodedata
from typing import Any

from src.agents.enums.internal.addressing.address_space import AddressSpace
from src.agents.enums.internal.checks.service_title import ServiceTitleSourceRole
from src.agents.enums.internal.detector import InternalCheck
from src.agents.schemas.internal.checks.service_title import ServiceTitleRepresentation, TitleMismatch, TitleSpan
from src.agents.schemas.internal.findings import DetectorFinding
from src.agents.steps_tools.document.locus_labels import humanize_locus
from src.agents.steps_tools.document.morphology import canonical_token, normalized_text
from src.agents.steps_tools.internal.addressing.address_space import build_address_book
from src.agents.steps_tools.internal.findings import detector_finding

_FORMULAS: dict[ServiceTitleSourceRole, re.Pattern[str]] = {
    ServiceTitleSourceRole.ACT_HEADING: re.compile(
        r"об\s+утверждении\s+административного\s+регламента.{0,500}?(?:государственной|муниципальной)\s+услуг[^«]*", re.I | re.S
    ),
    ServiceTitleSourceRole.APPROVAL_CLAUSE: re.compile(
        r"утвердить.{0,500}?административн[а-я]*\s+регламент.{0,500}?(?:государственной|муниципальной)\s+услуг[^«]*", re.I | re.S
    ),
    ServiceTitleSourceRole.SUBJECT_CLAUSE: re.compile(
        r"наименование.{0,120}?(?:государственной|муниципальной)\s+услуг[^«]*", re.I | re.S
    ),
}
_OPERATION_ROOTS = {
    "назнач": "назначение",
    "предостав": "предоставление",
    "выда": "выдача",
    "оформ": "оформление",
    "установ": "установление",
    "выплат": "выплата",
}
_STOP = frozenset(
    {
        "государственн",
        "муниципальн",
        "услуг",
        "по",
        "в",
        "и",
        "на",
        "с",
        "для",
        "предоставлени",
        "назначени",
        "административн",
        "регламент",
    }
)
_ALIASES = {"гос": "государственной", "мун": "муниципальной", "адм": "административный"}


def _tokens(value: str) -> list[str]:
    value = normalized_text(unicodedata.normalize("NFKC", value))
    value = value.replace("—", "-").replace("–", "-")
    return [canonical_token(_ALIASES.get(token, token)) for token in re.findall(r"[\w-]+", value)]


def _balanced_quote(text: str, start: int) -> tuple[int, int] | None:
    candidates = [(text.find(mark, start), mark, close) for mark, close in (("«", "»"), ('"', '"'), ("“", "”"))]
    open_at, opener, closer = min((item for item in candidates if item[0] >= 0), default=(-1, "", ""))
    if open_at < 0:
        return None
    if opener == closer:
        close_at = text.find(closer, open_at + 1)
        return (open_at, close_at + 1) if close_at >= 0 else None
    depth = 0
    for index in range(open_at, len(text)):
        if text[index] == opener:
            depth += 1
        elif text[index] == closer:
            depth -= 1
            if depth == 0:
                return open_at, index + 1
    return None


def _path(parsed: Any, start: int, end: int) -> tuple[str, ...]:
    root = getattr(getattr(parsed, "tree", None), "root", None)
    if root is None:
        return ()
    best: list[str] | None = None

    def visit(node: Any, parent: list[str]) -> None:
        nonlocal best
        if getattr(node, "char_start", 1) <= start and end <= getattr(node, "char_end", -1):
            here = [*parent, getattr(node, "element_id", "")]
            if best is None or len(here) > len(best):
                best = here
            for child in getattr(node, "children", []) or []:
                visit(child, here)

    visit(root, [])
    return tuple(item for item in (best or []) if item)


_CLAUSE_CASES = ("пункт {}", "пункта {}", "пункте {}")
#: Локус без доказанного номера: формулу называем, номер не выдумываем.
_UNPROVEN_CASES = ("формула утверждения регламента", "утверждения", "формуле утверждения")
#: Узлы дерева, которые вообще могут нести номер пункта резолютивной части.
_CLAUSE_NODE_KINDS = frozenset({"clause", "subclause"})


def _node_by_id(parsed: Any, element_id: str) -> Any:
    root = getattr(getattr(parsed, "tree", None), "root", None)
    stack = [root] if root is not None else []
    while stack:
        node = stack.pop()
        if getattr(node, "element_id", "") == element_id:
            return node
        stack.extend(getattr(node, "children", []) or [])
    return None


def _tree_locus(span: TitleSpan, parsed: Any) -> str:
    """Реквизит узла дерева, которому принадлежит формула (как у соседних детекторов)."""
    if not span.tree_path_element_ids:
        return ""
    node = _node_by_id(parsed, span.tree_path_element_ids[-1])
    if node is None or not getattr(node, "address_resolved", False):
        return ""
    if getattr(node, "kind", "") not in _CLAUSE_NODE_KINDS:
        return ""  # документ/преамбула — контейнер, номера пункта он не даёт
    return humanize_locus(getattr(node, "label", "") or "")


def _address_locus(span: TitleSpan, document_text: str, parsed: Any) -> str:
    """Реквизит по адресному пространству основного текста: пункт, ФИЗИЧЕСКИ содержащий формулу."""
    tree = getattr(parsed, "tree", None)
    numbers = {
        number
        for region in build_address_book(document_text, tree).regions
        if region.space is AddressSpace.CORE
        for number, (low, high) in region.item_spans.items()
        if low <= span.char_start < high
    }
    return humanize_locus(f"п. {numbers.pop()}") if len(numbers) == 1 else ""


def _approval_locus_cases(span: TitleSpan, document_text: str, parsed: Any) -> tuple[str, str, str]:
    "Локус формулы утверждения в трёх падежах."
    proved = {locus for locus in (_tree_locus(span, parsed), _address_locus(span, document_text, parsed)) if locus}
    if len(proved) != 1:
        return _UNPROVEN_CASES
    nominative = proved.pop()
    number = nominative.removeprefix("пункт ")
    if number == nominative:
        return _UNPROVEN_CASES
    nominative_case, genitive_case, prepositional_case = _CLAUSE_CASES
    return nominative_case.format(number), genitive_case.format(number), prepositional_case.format(number)


def _slice_values(exact: str) -> tuple[list[str], list[str]] | None:
    "Операции и ядро наименования, прочитанные в ОДНОМ срезе: до кавычек и внутри них."
    quoted = _balanced_quote(exact, 0)
    if quoted is None:
        return None
    all_tokens = _tokens(exact[: quoted[0]])
    operations = sorted({name for root, name in _OPERATION_ROOTS.items() if any(token.startswith(root) for token in all_tokens)})
    core = sorted({token for token in _tokens(exact[quoted[0] : quoted[1]]) if token not in _STOP})
    return operations, core


def service_title_values_are_readable(evidence: TitleMismatch, document_text: str) -> bool:
    "Значения и дельта доказательства выведены из срезов документа, а не заявлены."
    for span in (evidence.left, evidence.right):
        exact = document_text[span.char_start : span.char_end]
        values = _slice_values(exact) if exact else None
        if values is None:
            return False
        if tuple(values[0]) != span.normalized_operations or tuple(values[1]) != span.normalized_service_components:
            return False
    left_ops, right_ops = set(evidence.left.normalized_operations), set(evidence.right.normalized_operations)
    left_core = set(evidence.left.normalized_service_components)
    right_core = set(evidence.right.normalized_service_components)
    return (
        evidence.added_operations == sorted(right_ops - left_ops)
        and evidence.removed_operations == sorted(left_ops - right_ops)
        and evidence.added_service_components == sorted(right_core - left_core)
        and evidence.removed_service_components == sorted(left_core - right_core)
    )


def _extract(role: ServiceTitleSourceRole, text: str, parsed: Any) -> ServiceTitleRepresentation | None:
    found: list[tuple[int, int]] = []
    for match in _FORMULAS[role].finditer(text):
        # The act heading is the document heading, not a later recitation of it.
        if role is ServiceTitleSourceRole.ACT_HEADING and match.start() > 500:
            continue
        quoted = _balanced_quote(text, match.start())
        if quoted is not None and quoted[0] <= match.end():
            found.append((match.start(), quoted[1]))
    if len(found) != 1:
        return None
    start, end = found[0]
    exact = text[start:end]
    values = _slice_values(exact)
    assert values is not None
    operations, core = values
    return ServiceTitleRepresentation(
        span=TitleSpan(
            role=role,
            char_start=start,
            char_end=end,
            tree_path_element_ids=_path(parsed, start, end),
            normalized_operations=tuple(operations),
            normalized_service_components=tuple(core),
        ),
        quote=exact,
    )


def detect_service_title_mismatch(document_text: str, parsed: Any = None) -> TitleMismatch | None:
    reps = {role: _extract(role, document_text or "", parsed) for role in ServiceTitleSourceRole}
    heading, approval = reps[ServiceTitleSourceRole.ACT_HEADING], reps[ServiceTitleSourceRole.APPROVAL_CLAUSE]
    if heading is None or approval is None:
        return None
    left, right = heading.span, approval.span
    common = set(left.normalized_service_components) & set(right.normalized_service_components)
    if len(common) < 3:
        return None

    union = set(left.normalized_service_components) | set(right.normalized_service_components)
    if len(common) / max(1, len(union)) < 0.60:
        return None
    subject_matches = list(_FORMULAS[ServiceTitleSourceRole.SUBJECT_CLAUSE].finditer(document_text or ""))
    subject = reps[ServiceTitleSourceRole.SUBJECT_CLAUSE]
    if subject_matches and subject is None:
        return None
    if subject is not None:
        subject_core = set(subject.span.normalized_service_components)
        if (
            len(subject_core & set(left.normalized_service_components)) < 3
            or len(subject_core & set(right.normalized_service_components)) < 3
        ):
            return None
    added_ops = sorted(set(right.normalized_operations) - set(left.normalized_operations))
    removed_ops = sorted(set(left.normalized_operations) - set(right.normalized_operations))
    added_core = sorted(set(right.normalized_service_components) - set(left.normalized_service_components))
    removed_core = sorted(set(left.normalized_service_components) - set(right.normalized_service_components))
    if not (added_ops or removed_ops or added_core or removed_core):
        return None
    return TitleMismatch(
        left=left,
        right=right,
        added_operations=added_ops,
        removed_operations=removed_ops,
        added_service_components=added_core,
        removed_service_components=removed_core,
    )


def service_title_to_finding(evidence: TitleMismatch, document_text: str, parsed: Any = None) -> DetectorFinding:
    quote = document_text[evidence.left.char_start : evidence.left.char_end]
    right = document_text[evidence.right.char_start : evidence.right.char_end]
    nominative, genitive, prepositional = _approval_locus_cases(evidence.right, document_text, parsed)
    explanations = []
    if evidence.added_operations:
        operations = "», «".join(evidence.added_operations)
        explanations.append(
            f"В формуле {genitive} к наименованию регламента добавлена операция «{operations}», "
            "отсутствующая в наименовании проекта."
        )
    if evidence.removed_operations:
        operations = "», «".join(evidence.removed_operations)
        explanations.append(f"В формуле {genitive} отсутствует операция «{operations}», указанная в наименовании проекта.")
    if evidence.added_service_components:
        components = "», «".join(evidence.added_service_components)
        explanations.append(f"В формуле {genitive} добавлен содержательный компонент «{components}».")
    if evidence.removed_service_components:
        components = "», «".join(evidence.removed_service_components)
        explanations.append(f"В формуле {genitive} утрачен содержательный компонент «{components}».")
    return detector_finding(
        InternalCheck.SERVICE_TITLE,
        place="наименование проекта",
        quote=quote,
        rationale=" ".join(explanations),
        remedy=(
            f"Привести наименование административного регламента в {prepositional} в точное соответствие с "
            "наименованием проекта и утверждаемого регламента."
        ),
        seconds=[(nominative, right)],
        related_places=[f"{nominative}: {right}"],
    )


def service_title_findings(document_text: str, parsed: Any = None) -> list[DetectorFinding]:
    """Расхождение наименования; публикуется, только если значения доказательства читаются в срезах документа."""
    mismatch = detect_service_title_mismatch(document_text, parsed)
    if mismatch is None or not service_title_values_are_readable(mismatch, document_text):
        return []
    return [service_title_to_finding(mismatch, document_text, parsed)]
