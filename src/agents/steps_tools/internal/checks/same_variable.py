from __future__ import annotations

from collections.abc import Iterable

from src.agents.enums.internal.detector import InternalCheck
from src.agents.enums.internal.frames.norm_frame import FramePairOutcome
from src.agents.schemas.internal.checks.same_variable import SameVariableHit, _Fact, _Overlap
from src.agents.schemas.internal.findings import DetectorFinding
from src.agents.schemas.internal.frames.norm_frame import (
    EvidenceSourceKind,
    EvidenceSpan,
    FrameSlot,
    GroundingStatus,
    NormFrame,
    RegulatedVariableKind,
    content_fingerprint,
)
from src.agents.schemas.internal.frames.project_frames import BuiltProjectFrame
from src.agents.steps_tools.document.locus_labels import humanize_locus
from src.agents.steps_tools.document.segmentation import UNLABELED_PROVISION
from src.agents.steps_tools.internal.findings import detector_finding
from src.agents.steps_tools.internal.frames.context_policy import eligible_evidence_spans
from src.agents.steps_tools.internal.frames.frame_match import match_frames
from src.agents.steps_tools.internal.frames.general_overlap import (
    SPECIFIC_CHANNELS,
    canonical_channel,
    categorical_registration_scope,
    normalize_material_value,
    specific_registration_branch,
    system_is_registration_operand,
    text_mentions_material,
)
from src.common.config.config import settings

_SAME_VARIABLE_KINDS = frozenset(
    {
        RegulatedVariableKind.INFORMATION_SYSTEM_TARGET,
        RegulatedVariableKind.REGISTRY_TARGET,
        RegulatedVariableKind.DURATION,
        RegulatedVariableKind.REGISTRATION_START_EVENT,
        RegulatedVariableKind.REGISTRATION_COMPLETION_EVENT,
    }
)
#: Доказанные слоты, совпадение которых требуется от обеих сторон.
_OPERATION_FIELDS: tuple[str, ...] = ("action", "object", "procedure_stage")

_VARIABLE_SLOTS = frozenset({"start_event", "completion_event", "target_system", "duration", "registry_target"})


def _slot_fact(slot: FrameSlot | None, *, role_proven: bool = False) -> _Fact:
    """Факт из слота. ``role_proven`` — слот рамки операндом становится, только если производитель доказал его роль."""
    if slot is None or (role_proven and not slot.role_is_proven()):
        return _Fact(value=None, spans=())
    return _Fact(value=slot.normalized_value, spans=tuple(slot.evidence_span_ids))


def _variable_fact(frame: NormFrame, attribute: str) -> _Fact:
    variable = frame.regulated_variable
    value = getattr(variable, attribute, None)
    if attribute in _VARIABLE_SLOTS:
        return _slot_fact(value)
    return _Fact(value=getattr(value, "value", value), spans=tuple(variable.evidence_span_ids))


def _text_fact(span: EvidenceSpan) -> _Fact:
    return _Fact(value=" ".join(span.text.split()), spans=(span.span_id,))


def _orient(a: BuiltProjectFrame, b: BuiltProjectFrame) -> tuple[BuiltProjectFrame, BuiltProjectFrame]:
    "Локус раньше по тексту — слева; порядок не задаёт роль нормы в отношении."
    primary_a, primary_b = a.frame.evidence.primary_span, b.frame.evidence.primary_span
    ka = ((primary_a.char_start or 0) if primary_a is not None else 0, a.leaf.element_id)
    kb = ((primary_b.char_start or 0) if primary_b is not None else 0, b.leaf.element_id)
    return (a, b) if ka <= kb else (b, a)


def _same_exact_slot(left: BuiltProjectFrame, right: BuiltProjectFrame, field: str) -> str | None:
    "Return an exact shared scalar, never deriving identity from text similarity."
    left_slot: FrameSlot = getattr(left.frame, field)
    right_slot: FrameSlot = getattr(right.frame, field)
    value = left_slot.proven_value()
    if not value or value != right_slot.proven_value():
        return None
    return value


def _exact_variable_values(left: BuiltProjectFrame, right: BuiltProjectFrame, attribute: str) -> tuple[_Fact, _Fact] | None:
    """Two exact, distinct variable facets; absence is never a value."""
    a, b = _variable_fact(left.frame, attribute), _variable_fact(right.frame, attribute)
    if not a.value or not b.value or a.value == b.value or not a.spans or not b.spans:
        return None
    return a, b


def _same_variable_slot(left: BuiltProjectFrame, right: BuiltProjectFrame, name: str, *, required: bool) -> bool:
    """Compare a context facet without treating two omissions as semantic identity."""
    a, b = _variable_fact(left.frame, name), _variable_fact(right.frame, name)
    if not a.value and not b.value:
        return not required
    return bool(a.value and a.value == b.value and a.spans and b.spans)


def _same_duration_metadata(left: BuiltProjectFrame, right: BuiltProjectFrame) -> bool:
    "Durations are comparable only with an explicit common unit and qualifier."
    left_variable = left.frame.regulated_variable
    right_variable = right.frame.regulated_variable
    return bool(
        left_variable.unit
        and left_variable.unit == right_variable.unit
        and left_variable.calendar_kind == right_variable.calendar_kind
        and left_variable.qualifier
        and left_variable.qualifier == right_variable.qualifier
    )


def _shared_variable_context(left: BuiltProjectFrame, right: BuiltProjectFrame, attribute: str) -> tuple[_Fact, _Fact] | None:
    """An exact bilateral prerequisite: the same value with its own evidence on both sides."""
    a, b = _variable_fact(left.frame, attribute), _variable_fact(right.frame, attribute)
    if not a.value or a.value != b.value or not a.spans or not b.spans:
        return None
    return a, b


def _target_system_facts(built: BuiltProjectFrame) -> list[_Fact]:
    """Each independently exact system operand, without flattening routes."""
    variable = built.frame.regulated_variable
    options = [_slot_fact(variable.target_system), *(_slot_fact(candidate) for candidate in variable.target_system_candidates)]
    exact: list[_Fact] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    primary = built.frame.evidence.primary_span
    clause_text = primary.text if primary is not None else built.leaf.text
    for fact in options:
        if not fact.value or not fact.spans or not isinstance(fact.value, str):
            continue
        key = (normalize_material_value(fact.value), fact.spans)
        if key in seen or not system_is_registration_operand(clause_text, fact.value):
            continue
        exact.append(fact)
        seen.add(key)
    return exact


def _general_overlap(left: BuiltProjectFrame, right: BuiltProjectFrame) -> _Overlap | None:
    """Общая норма о регистрации в целом охватывает частную ветку, названную своим каналом."""
    for general, special, general_is_left in ((left, right, True), (right, left, False)):
        general_primary = general.frame.evidence.primary_span
        special_primary = special.frame.evidence.primary_span
        if general_primary is not None and special_primary is not None and categorical_registration_scope(general_primary.text):
            specific_channel = next(
                (key for key in SPECIFIC_CHANNELS if specific_registration_branch(special_primary.text, key)), None
            )
            if specific_channel is not None:
                return _Overlap(
                    general_is_left=general_is_left,
                    canonical_channel=specific_channel,
                    scope=_text_fact(general_primary),
                    channel=_text_fact(special_primary),
                )
    return None


def _temporal_context_matches(left: BuiltProjectFrame, right: BuiltProjectFrame, *, counterpart: str) -> bool:
    """A temporal facet cannot borrow identity from a missing endpoint/context."""
    if not _same_variable_slot(left, right, counterpart, required=True):
        return False

    if not _same_variable_slot(left, right, "duration", required=False):
        return False
    if not _same_variable_slot(left, right, "target_system", required=False):
        return False
    return not (
        left.frame.regulated_variable.duration
        and right.frame.regulated_variable.duration
        and not _same_duration_metadata(left, right)
    )


def _context_attributes(facet: str, left: BuiltProjectFrame, right: BuiltProjectFrame) -> list[str]:
    """Предпосылки значения, которые обязаны совпасть у сторон: у срока — единица и события, у события — парное событие."""
    if facet in {"target_system", "registry_target"}:
        return []
    if facet == "duration":
        return ["unit", "qualifier", "start_event", "completion_event"]
    variables = (left.frame.regulated_variable, right.frame.regulated_variable)
    attributes = ["completion_event" if facet == "start_event" else "start_event"]
    if any(variable.duration is not None for variable in variables):
        attributes.extend(["duration", "unit", "qualifier"])
    if any(variable.target_system is not None for variable in variables):
        attributes.append("target_system")
    return attributes


def _deterministic_same_variable_hits(anchor: BuiltProjectFrame, candidate: BuiltProjectFrame) -> list[SameVariableHit]:
    "Exact same-variable candidates for independent legal facets."
    left, right = _orient(anchor, candidate)
    scenario_field, scenario = next(
        ((name, value) for name in ("subservice", "service", "measure") if (value := _same_exact_slot(left, right, name))),
        (None, None),
    )
    action = _same_exact_slot(left, right, "action")
    object_ = _same_exact_slot(left, right, "object")
    stage = _same_exact_slot(left, right, "procedure_stage")
    if scenario is None or action is None or object_ is None or stage is None:
        return []
    assert scenario_field is not None
    left_variable = left.frame.regulated_variable
    right_variable = right.frame.regulated_variable

    facet_kinds = {
        "target_system": RegulatedVariableKind.INFORMATION_SYSTEM_TARGET,
        "registry_target": RegulatedVariableKind.REGISTRY_TARGET,
        "duration": RegulatedVariableKind.DURATION,
        "start_event": RegulatedVariableKind.REGISTRATION_START_EVENT,
        "completion_event": RegulatedVariableKind.REGISTRATION_COMPLETION_EVENT,
    }
    operation_scope = tuple(
        (_slot_fact(getattr(left.frame, name), role_proven=True), _slot_fact(getattr(right.frame, name), role_proven=True))
        for name in (scenario_field, "action", "object", "procedure_stage")
    )
    overlap = _general_overlap(left, right)
    hits: list[SameVariableHit] = []
    for facet, expected_kind in facet_kinds.items():
        if left_variable.kind is not expected_kind or right_variable.kind is not expected_kind:
            continue
        if facet == "target_system":
            values_a = _target_system_facts(left)
            values_b = _target_system_facts(right)
            if (
                not values_a
                or not values_b
                or not any(
                    normalize_material_value(str(a.value)) != normalize_material_value(str(b.value))
                    for a in values_a
                    for b in values_b
                )
            ):
                continue
        else:
            exact = _exact_variable_values(left, right, facet)
            if exact is None:
                continue
            values_a, values_b = [exact[0]], [exact[1]]
        if facet == "duration" and not (
            _same_duration_metadata(left, right)
            and _same_variable_slot(left, right, "start_event", required=True)
            and _same_variable_slot(left, right, "completion_event", required=True)
        ):
            continue
        if facet == "start_event" and not _temporal_context_matches(left, right, counterpart="completion_event"):
            continue
        if facet == "completion_event" and not _temporal_context_matches(left, right, counterpart="start_event"):
            continue
        shared = [_shared_variable_context(left, right, attribute) for attribute in _context_attributes(facet, left, right)]
        if any(item is None for item in shared):
            continue
        kind_pair = (
            _Fact(value=expected_kind.value, spans=tuple(left_variable.evidence_span_ids), named=False),
            _Fact(value=expected_kind.value, spans=tuple(right_variable.evidence_span_ids), named=False),
        )
        hits.append(
            SameVariableHit(
                left=left,
                right=right,
                facet=facet,
                scope=(*operation_scope, kind_pair),
                context=tuple(item for item in shared if item is not None),
                values_a=tuple(values_a),
                values_b=tuple(values_b),
                overlap=overlap,
            )
        )
    return hits


def same_variable_conflicts(frames: Iterable[BuiltProjectFrame]) -> list[SameVariableHit]:
    "Самостоятельный проход: пары с одной доказанной операцией → точные кандидаты."
    candidates = [built for built in frames if built.frame.regulated_variable.kind in _SAME_VARIABLE_KINDS]
    groups: dict[tuple[str, ...], list[BuiltProjectFrame]] = {}
    for built in candidates:
        key = tuple(getattr(built.frame, field).proven_value() or "" for field in _OPERATION_FIELDS)
        if not all(key):
            continue
        groups.setdefault(key, []).append(built)
    hits: list[SameVariableHit] = []
    for members in groups.values():
        for index, left in enumerate(members):
            for right in members[index + 1 :]:
                if left.leaf.element_id == right.leaf.element_id:
                    continue
                outcome = match_frames(left.frame, right.frame)
                if outcome is not FramePairOutcome.COMPATIBLE:
                    continue
                produced = _deterministic_same_variable_hits(left, right)
                hits.extend(produced)
    return hits


class _Evidence:
    """Факты одной нормы против текста проекта: точные координаты и значение, названное в тексте спанов."""

    def __init__(self, built: BuiltProjectFrame, document_text: str) -> None:
        self.document_text = document_text
        self.fingerprint = content_fingerprint(document_text)
        self.source_id = built.leaf.element_id
        self.spans = eligible_evidence_spans(built.frame)

    def exact(self, fact: _Fact) -> bool:
        """Операнд точный: все его спаны — доказательства рамки, это текст проекта с этого узла, и координаты верны."""
        if fact.value is None or not fact.spans:
            return False
        for span_id in fact.spans:
            span = self.spans.get(span_id)
            if (
                span is None
                or span.source_kind is not EvidenceSourceKind.PROJECT
                or span.source_id != self.source_id
                or span.grounding_status is not GroundingStatus.EXACT
                or span.content_fingerprint != self.fingerprint
                or span.char_start is None
                or span.char_end is None
                or not 0 <= span.char_start < span.char_end <= len(self.document_text)
                or self.document_text[span.char_start : span.char_end] != span.text
            ):
                return False
        return True

    def named(self, fact: _Fact) -> bool:
        """Значение названо в тексте своих спанов (вызывается после ``exact``)."""
        if not fact.named or not isinstance(fact.value, str):
            return True
        return text_mentions_material(" ".join(self.spans[span_id].text for span_id in fact.spans), fact.value)

    def holds(self, fact: _Fact) -> bool:
        return self.exact(fact) and self.named(fact)


def _overlap_is_proven(overlap: _Overlap, left: _Evidence, right: _Evidence) -> bool:
    """Общая норма охватывает частную ветку: операнды точные и названы, охват подтверждён самой формулировкой."""
    general, special = (left, right) if overlap.general_is_left else (right, left)
    scope, channel = overlap.scope, overlap.channel
    if not (general.exact(scope) and special.exact(channel)):
        return False
    if not isinstance(scope.value, str) or not isinstance(channel.value, str):
        return False
    if canonical_channel(channel.value) != overlap.canonical_channel:
        return False
    covered = categorical_registration_scope(scope.value) and specific_registration_branch(
        channel.value, overlap.canonical_channel
    )
    return covered and general.named(scope) and special.named(channel)


def _applicability_is_proven(hit: SameVariableHit, left: _Evidence, right: _Evidence) -> bool:
    "Нормы применяются вместе: общая норма о регистрации в целом доказанно охватывает частную ветку."
    return hit.overlap is not None and _overlap_is_proven(hit.overlap, left, right)


def _hit_is_proven(hit: SameVariableHit, document_text: str) -> bool:
    left, right = _Evidence(hit.left, document_text), _Evidence(hit.right, document_text)
    if not all(left.holds(a) and right.holds(b) for a, b in (*hit.scope, *hit.context)):
        return False
    if not all(left.holds(fact) for fact in hit.values_a) or not all(right.holds(fact) for fact in hit.values_b):
        return False
    left_values = {normalize_material_value(str(fact.value)) for fact in hit.values_a if fact.value is not None}
    right_values = {normalize_material_value(str(fact.value)) for fact in hit.values_b if fact.value is not None}
    if not left_values or not right_values or left_values & right_values:
        return False
    return _applicability_is_proven(hit, left, right)


def same_variable_findings(frames: Iterable[BuiltProjectFrame], *, document_text: str) -> list[DetectorFinding]:
    "Находки по точным отношениям одной переменной."
    hits = same_variable_conflicts(frames)
    findings = []
    for hit in hits:
        if not _hit_is_proven(hit, document_text):
            continue
        primary_left = hit.left.frame.evidence.primary_span
        primary_right = hit.right.frame.evidence.primary_span
        if primary_left is None or primary_right is None:
            continue
        second_place = humanize_locus(hit.right.leaf.label or UNLABELED_PROVISION) or hit.right.leaf.element_id
        findings.append(
            detector_finding(
                InternalCheck.SAME_VARIABLE,
                place=humanize_locus(hit.left.leaf.label or UNLABELED_PROVISION),
                quote=primary_left.text,
                rationale=settings["agents.internal.messages.same_variable_rationale"],
                seconds=[(second_place, primary_right.text)],
                related_places=[second_place],
            )
        )
    return findings
