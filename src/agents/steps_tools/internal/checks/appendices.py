from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any

from src.agents.enums.internal.checks.appendices import (
    AppendixArtifactKind,
    AppendixIssueKind,
    AppendixOwnerKind,
    AppendixSpanRole,
)
from src.agents.enums.internal.detector import InternalCheck
from src.agents.schemas.internal.checks.appendices import (
    AppendixAnalysis,
    AppendixArtifact,
    AppendixDeclaration,
    AppendixIssue,
    AppendixSpan,
)
from src.agents.schemas.internal.findings import DetectorFinding
from src.agents.steps_tools.document.morphology import normalized_text
from src.agents.steps_tools.internal.addressing.address_space import amendment_spans, is_amendment_project
from src.agents.steps_tools.internal.findings import detector_finding
from src.agents.steps_tools.sources.act_refs import find_act_requisites
from src.common.config.config import settings

_APPENDIX_BOUNDARY = r"\n\s*\d+(?:\.\d+)*\.\s+|\n[ \t_]*(?:ПРИЛОЖЕНИЕ|Приложение)(?![а-яё])|\n{2,}|$"


_FORM_WORD = re.compile(r"\bформ\w*\b", re.IGNORECASE)
_FORM_PROXIMITY_WINDOW = int(settings["agents.internal.appendix_check.form_proximity_window"])
_DECLARATION_GROUP = re.compile(
    r"приложени\w*\s*(?P<body>№?\s*\d{1,3}(?:\s*[-–]\s*№?\s*\d{1,3}|(?:\s*,\s*№?\s*\d{1,3})*(?:\s+и\s+№?\s*\d{1,3})?))",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"(?P<header>ПРИЛОЖЕНИЕ|Приложение)(?![а-яё])"
    r"[ \t]*(?:№[ \t]*)?(?P<number>\d{1,3})\b(?P<rest>[^\n]*)"
)


_GLUED_PAGE_TAIL = re.compile(
    r"(?:_{3,}|(?:подпис\w*|специалист\w*|представител\w*|фио|дата)[^\n]{0,160}\))\s*$",
    re.IGNORECASE,
)
_PROJECT_DECLARATION = re.compile(r"к\s+настоящ\w*\s+(?:административн\w*\s+)?(?:регламент\w*|положени\w*|порядк\w*)", re.I)
_PROJECT_HEADER = re.compile(r"к\s+административн\w*\s+регламент\w*", re.I)
_EXTERNAL_HEADER = re.compile(r"^\s*к\s+(?:приказ\w*|порядк\w*)[\s\S]{0,300}?№\s*\d", re.I)
_EXTERNAL_OWNER = re.compile(r"(?:утвержд\w*|приказ\w*|постановлени\w*)[\s\S]{0,300}?№\s*\d", re.I)


_FORM_GENUS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("заявление", r"форма\s+заявлени(?:я|е)\b|заявлени(?:е|я)(?:\s*$|\s+(?:об|о|на|для|в|по)\b)"),
    ("согласие", r"форма\s+согласи(?:я|е)\b|согласи(?:е|я)(?:\s*$|\s+(?:на|об|о)\b)"),
    ("обязательство", r"форма\s+обязательств(?:а|о)\b|обязательств(?:о|а)(?:\s*$|\s+(?:заявителя|об|о|по)\b)"),
    ("анкета", r"форма\s+анкеты\b|анкета(?:\s+(?:заявителя|гражданина))?\s*$"),
    ("запрос", r"форма\s+запроса\b|запрос(?:\s*$|\s+(?:о|на|об)\b)"),
    ("расписка-уведомление", r"расписк[аи]-?уведомлени[ея]\s*$"),
)
_FORM_TITLE = re.compile(r"(?mi)^\s*(?:" + "|".join(pattern for _, pattern in _FORM_GENUS_PATTERNS) + ")")
_FORM_GENUS_MATCHERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rf"(?mi)^\s*(?:{pattern})")) for name, pattern in _FORM_GENUS_PATTERNS
)
_NON_FORM_TITLE = re.compile(r"(?mi)^\s*(?:перечень|методика|порядок|правила|инструкция|таблица)\b")


_GENUS_STEM: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("заявление", re.compile(r"заявлени\w*", re.IGNORECASE)),
    ("согласие", re.compile(r"согласи\w*", re.IGNORECASE)),
    ("обязательство", re.compile(r"обязательств\w*", re.IGNORECASE)),
    ("анкета", re.compile(r"анкет\w*", re.IGNORECASE)),
    ("запрос", re.compile(r"запрос\w*", re.IGNORECASE)),
    ("расписка-уведомление", re.compile(r"расписк\w*", re.IGNORECASE)),
)


_GENUS_ANCHOR_WORD = re.compile(
    r"\b(?:(?:форм|бланк|образц)(?:а|ы|е|у|ой|ою|ом|и|ов|ам|ами|ах)?|образец)\b",
    re.IGNORECASE,
)
_GENUS_ANCHOR_GAP = int(settings["agents.internal.appendix_check.genus_anchor_gap"])


_GENUS_CONNECTOR = re.compile(r"^(?:\s*,\s*|\s+и\s+|\s+или\s+)$", re.IGNORECASE)
_GENUS_CONNECTOR_MAX_GAP = int(settings["agents.internal.appendix_check.genus_connector_max_gap"])


def _form_genus(title_text: str) -> str | None:
    """Genus of an artifact already classified FORM, read off its own matched title text."""
    for name, pattern in _FORM_GENUS_MATCHERS:
        if pattern.match(title_text):
            return name
    return None


_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=\s+[А-ЯЁA-Z]|\s*$)")


def _sentence_start(chunk: str, position: int) -> int:
    "Start of the sentence containing ``position``, or 0. A ``_paragraphs`` clause can hold"
    boundary_end = 0
    for match in _SENTENCE_BOUNDARY.finditer(chunk[:position]):
        boundary_end = match.end()
    return boundary_end


def _sentence_end(chunk: str, position: int, limit: int) -> int:
    "End of the sentence containing ``position``, capped at ``limit`` (never crosses into a"
    region = chunk[position:limit]
    match = _SENTENCE_BOUNDARY.search(region)
    return position + match.end() if match else limit


def _declared_genus_mentions(clause_prefix: str) -> frozenset[str]:
    'Genus/genera a declaration clause\'s own prose names, if any (empty = unscoped/"формы").'
    leading = clause_prefix.lstrip()
    lead_offset = len(clause_prefix) - len(leading)
    anchors = list(_GENUS_ANCHOR_WORD.finditer(clause_prefix))
    all_matches = sorted(
        ((name, match) for name, pattern in _GENUS_STEM for match in pattern.finditer(clause_prefix)),
        key=lambda item: item[1].start(),
    )
    mentions: set[str] = set()
    anchored_ends: list[int] = []
    for name, match in all_matches:
        is_anchored = match.start() == lead_offset or any(
            0 <= match.start() - anchor.end() <= _GENUS_ANCHOR_GAP for anchor in anchors
        )
        if not is_anchored:
            is_anchored = any(
                0 <= match.start() - end <= _GENUS_CONNECTOR_MAX_GAP
                and _GENUS_CONNECTOR.match(clause_prefix[end : match.start()])
                for end in anchored_ends
            )
        if is_anchored:
            mentions.add(name)
            anchored_ends.append(match.end())
    return frozenset(mentions)


_AMBIGUOUS_GENUS = object()


def _resolve_declared_genus(local_declarations: list[AppendixDeclaration]) -> str | None:
    'None = unscoped ("формы", compare against every FORM artifact); ``_AMBIGUOUS_GENUS``'
    if len(local_declarations) != 1:
        return None
    mentions = local_declarations[0].declared_genus
    if len(mentions) > 1:
        return _AMBIGUOUS_GENUS  # type: ignore[return-value]
    return next(iter(mentions), None)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _numbers(body: str) -> tuple[int, ...]:
    values = [int(value) for value in re.findall(r"\d{1,3}", body)]
    if not values:
        return ()
    if ("-" in body or "–" in body) and len(values) >= 2:
        return tuple(range(values[0], values[-1] + 1))
    return tuple(dict.fromkeys(values))


def _tree_path(parsed: Any, start: int, end: int) -> list[str]:
    root = getattr(getattr(parsed, "tree", None), "root", None)
    if root is None:
        return []

    def visit(node: Any, path: list[str]) -> list[str] | None:
        current = [*path, getattr(node, "element_id", "")]
        if not (getattr(node, "char_start", 0) <= start and end <= getattr(node, "char_end", 0)):
            return None
        for child in getattr(node, "children", None) or []:
            if result := visit(child, current):
                return result
        return [item for item in current if item]

    return visit(root, []) or []


def _span(
    document_text: str,
    parsed: Any,
    start: int,
    end: int,
    *,
    role: AppendixSpanRole,
    owner_kind: AppendixOwnerKind,
    owner_id: str,
    numbers: tuple[int, ...] = (),
    artifact_kind: AppendixArtifactKind = AppendixArtifactKind.UNKNOWN,
) -> AppendixSpan:
    return AppendixSpan(
        role=role,
        char_start=start,
        char_end=end,
        tree_path_element_ids=tuple(_tree_path(parsed, start, end)),
        owner_kind=owner_kind,
        owner_id=owner_id,
        appendix_numbers=tuple(sorted(set(numbers))),
        artifact_kind=artifact_kind,
    )


def _inside(spans: list[tuple[int, int]], offset: int) -> bool:
    return any(start <= offset < end for start, end in spans)


def _trusted_amendment_spans(text: str) -> list[tuple[int, int]]:
    """Keep amendment-space protection, but reject a form's request to amend personal data."""
    spans = amendment_spans(text)
    if is_amendment_project(text):
        return spans
    trusted: list[tuple[int, int]] = []
    for start, end in spans:
        prefix = text[max(0, start - 300) : start]
        if re.search(r"(?:^|\n)\s*\d+\.\s*внести\s+в\s+(?:административн\w*|положени\w*|порядок)", prefix, re.I):
            trusted.append((start, end))
    return trusted


def _header_matches(text: str) -> list[re.Match[str]]:
    "Return physical-header candidates, excluding nominative inline references."
    matches: list[re.Match[str]] = []
    for match in _HEADER.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_prefix = text[line_start : match.start()].strip(" \t\f")
        prefix = line_prefix.strip("_")
        rest = (match.group("rest") or "").strip(" \t_.")
        if prefix:
            attachment = text[match.end() : match.end() + 400]
            attached = bool(re.match(r"\s*к\s+(?:административн\w*|приказ\w*|порядк\w*)", attachment, re.I))
            if rest or not attached or _GLUED_PAGE_TAIL.search(line_prefix) is None:
                continue
        matches.append(match)
    return matches


def _external_owner_id(qualifier: str) -> str:
    normalized = normalized_text(qualifier)
    requisites = find_act_requisites(qualifier)
    number_key = requisites[0].number.replace(" ", "") if requisites and requisites[0].number else "unknown"
    return f"external-act:{number_key}:{_sha(normalized)[:16]}"


def _declaration_owner(
    document_text: str,
    parsed: Any,
    qualifier_start: int,
    qualifier_end: int,
    numbers: tuple[int, ...],
    amendment: list[tuple[int, int]],
) -> tuple[AppendixOwnerKind, str, AppendixSpan | None]:
    qualifier = document_text[qualifier_start:qualifier_end]
    fingerprint = _sha(document_text)
    if _inside(amendment, qualifier_start):
        return AppendixOwnerKind.AMENDED_ACT, f"amended-act:{fingerprint}", None
    if match := _PROJECT_DECLARATION.search(qualifier):
        owner_kind, owner_id = AppendixOwnerKind.PROJECT, f"project:{fingerprint}"
        start, end = qualifier_start + match.start(), qualifier_start + match.end()
        return (
            owner_kind,
            owner_id,
            _span(
                document_text,
                parsed,
                start,
                end,
                role=AppendixSpanRole.OWNER_QUALIFIER,
                owner_kind=owner_kind,
                owner_id=owner_id,
                numbers=numbers,
            ),
        )
    if match := _EXTERNAL_OWNER.search(qualifier):
        owner_kind, owner_id = AppendixOwnerKind.EXTERNAL_ACT, _external_owner_id(match.group(0))
        start, end = qualifier_start + match.start(), qualifier_start + match.end()
        return (
            owner_kind,
            owner_id,
            _span(
                document_text,
                parsed,
                start,
                end,
                role=AppendixSpanRole.OWNER_QUALIFIER,
                owner_kind=owner_kind,
                owner_id=owner_id,
                numbers=numbers,
            ),
        )
    return AppendixOwnerKind.UNKNOWN, f"unknown:{_sha(qualifier)[:16]}", None


def _classify(context: str) -> tuple[AppendixArtifactKind, re.Match[str] | None]:
    matches = [(match.start(), AppendixArtifactKind.FORM, match) for match in _FORM_TITLE.finditer(context)]
    matches.extend((match.start(), AppendixArtifactKind.NON_FORM, match) for match in _NON_FORM_TITLE.finditer(context))
    if not matches:
        return AppendixArtifactKind.UNKNOWN, None
    _start, kind, match = min(matches, key=lambda item: item[0])
    return kind, match


def _appendix_body_spans(header_matches: list[re.Match[str]], text_length: int) -> list[tuple[int, int]]:
    "Character ranges owned by an appendix's own body: from one physical header to the"
    spans: list[tuple[int, int]] = []
    for index, match in enumerate(header_matches):
        end = header_matches[index + 1].start() if index + 1 < len(header_matches) else text_length
        spans.append((match.start(), end))
    return spans


def _paragraphs(text: str) -> list[tuple[int, int]]:
    "Clause/paragraph spans, delimited exactly like an appendix body boundary (a numbered"
    boundaries = list(re.finditer(_APPENDIX_BOUNDARY, text))
    starts = [0, *(match.end() for match in boundaries)]
    ends = [match.start() for match in boundaries] + [len(text)]
    return [(start, end) for start, end in zip(starts, ends, strict=True) if start < end]


def _extract_declarations(
    document_text: str,
    parsed: Any,
    amendment: list[tuple[int, int]],
    appendix_bodies: list[tuple[int, int]],
) -> list[AppendixDeclaration]:
    declarations: list[AppendixDeclaration] = []
    for para_start, para_end in _paragraphs(document_text):
        if _inside(appendix_bodies, para_start):
            continue
        chunk = document_text[para_start : min(para_end, para_start + 1800)]
        groups = list(_DECLARATION_GROUP.finditer(chunk))
        for index, group in enumerate(groups):
            numbers = _numbers(group.group("body"))
            if len(numbers) < 2:
                continue
            end = para_start + group.end()

            window_start = max(0, group.start() - _FORM_PROXIMITY_WINDOW)
            form_match = _FORM_WORD.search(chunk, window_start, group.start())
            span_start = para_start + (form_match.start() if form_match is not None else group.start())
            qualifier_start = end
            qualifier_end = para_start + (groups[index + 1].start() if index + 1 < len(groups) else len(chunk))
            owner_kind, owner_id, owner_span = _declaration_owner(
                document_text, parsed, qualifier_start, qualifier_end, numbers, amendment
            )

            next_group_start = groups[index + 1].start() if index + 1 < len(groups) else len(chunk)
            genus_window_start = max(_sentence_start(chunk, group.start()), groups[index - 1].end() if index > 0 else 0)
            genus_window_end = min(next_group_start, _sentence_end(chunk, group.end(), next_group_start))
            declared_genus = _declared_genus_mentions(chunk[genus_window_start:genus_window_end])
            declarations.append(
                AppendixDeclaration(
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    numbers=numbers,
                    declaration_span=_span(
                        document_text,
                        parsed,
                        span_start,
                        end,
                        role=AppendixSpanRole.DECLARATION,
                        owner_kind=owner_kind,
                        owner_id=owner_id,
                        numbers=numbers,
                    ),
                    owner_span=owner_span,
                    declared_genus=declared_genus,
                )
            )
    return declarations


def _extract_artifacts(
    document_text: str,
    parsed: Any,
    amendment: list[tuple[int, int]],
    matches: list[re.Match[str]],
) -> list[AppendixArtifact]:
    artifacts: list[AppendixArtifact] = []
    fingerprint = _sha(document_text)
    for index, match in enumerate(matches):
        number = int(match.group("number"))
        start, line_end = match.start(), match.end()
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(document_text)
        context_end = min(next_start, start + 1800)
        context = document_text[start:context_end]
        owner_kind = AppendixOwnerKind.AMENDED_ACT if _inside(amendment, start) else AppendixOwnerKind.UNKNOWN
        owner_id = (
            f"amended-act:{fingerprint}" if owner_kind is AppendixOwnerKind.AMENDED_ACT else f"unknown:{_sha(context[:300])[:16]}"
        )
        attachment = document_text[line_end : min(next_start, line_end + 400)]
        if owner_kind is not AppendixOwnerKind.AMENDED_ACT and _PROJECT_HEADER.search(attachment):
            owner_kind, owner_id = AppendixOwnerKind.PROJECT, f"project:{fingerprint}"
        elif owner_kind is not AppendixOwnerKind.AMENDED_ACT and _EXTERNAL_HEADER.search(attachment):
            owner_kind, owner_id = AppendixOwnerKind.EXTERNAL_ACT, _external_owner_id(attachment)
        classification, title = _classify(context)
        form_genus = _form_genus(title.group(0)) if title is not None and classification is AppendixArtifactKind.FORM else None
        header_span = _span(
            document_text,
            parsed,
            start,
            line_end,
            role=AppendixSpanRole.HEADER,
            owner_kind=owner_kind,
            owner_id=owner_id,
            numbers=(number,),
            artifact_kind=classification,
        )
        classification_span = None
        if title is not None:
            classification_span = _span(
                document_text,
                parsed,
                start + title.start(),
                start + title.end(),
                role=AppendixSpanRole.CLASSIFICATION,
                owner_kind=owner_kind,
                owner_id=owner_id,
                numbers=(number,),
                artifact_kind=classification,
            )
        artifacts.append(
            AppendixArtifact(
                owner_kind=owner_kind,
                owner_id=owner_id,
                number=number,
                classification=classification,
                header_span=header_span,
                classification_span=classification_span,
                form_genus=form_genus,
            )
        )
    return artifacts


def _all_exact(spans: list[AppendixSpan]) -> bool:
    return bool(spans) and all(span.tree_path_element_ids for span in spans)


def _span_numbers_are_readable(span: AppendixSpan, document_text: str) -> bool:
    "Номера приложений операнда читаются в его собственном срезе тем же извлечением."
    exact = document_text[span.char_start : span.char_end]
    if not exact:
        return False
    if span.role is AppendixSpanRole.DECLARATION:
        groups = list(_DECLARATION_GROUP.finditer(exact))
        # Срез объявления кончается на СВОЕЙ группе номеров: берётся последняя.
        return bool(groups) and sorted(set(_numbers(groups[-1].group("body")))) == list(span.appendix_numbers)
    if span.role is AppendixSpanRole.HEADER:
        header = _HEADER.match(exact)
        return header is not None and [int(header.group("number"))] == list(span.appendix_numbers)
    if span.role is AppendixSpanRole.CLASSIFICATION:
        if span.artifact_kind is AppendixArtifactKind.FORM:
            return _FORM_TITLE.match(exact) is not None
        if span.artifact_kind is AppendixArtifactKind.NON_FORM:
            return _NON_FORM_TITLE.match(exact) is not None
    return True


def appendix_values_are_readable(issue: AppendixIssue, document_text: str) -> bool:
    """Номера и вид приложений находки выведены из срезов, а не заявлены."""
    return all(
        _span_numbers_are_readable(span, document_text)
        for span in (*issue.declaration_spans, *issue.owner_spans, *issue.material_spans)
    )


def _issue(
    kind: AppendixIssueKind,
    declarations: list[AppendixDeclaration],
    artifacts: list[AppendixArtifact],
    affected: set[int],
    *,
    uncertainty: bool,
) -> AppendixIssue:
    declaration_spans = [item.declaration_span for item in declarations]
    owner_spans = [item.owner_span for item in declarations if item.owner_span is not None]
    material: list[AppendixSpan] = []
    for artifact in artifacts:
        if artifact.number in affected:
            material.append(artifact.header_span)
            if artifact.classification_span is not None:
                material.append(artifact.classification_span)
    publishable = (
        kind is AppendixIssueKind.MISSING_FORM_REFERENCE
        and not uncertainty
        and _all_exact([*declaration_spans, *owner_spans, *material])
    )
    return AppendixIssue(
        issue_kind=kind,
        affected_numbers=tuple(sorted(affected)),
        declaration_spans=declaration_spans,
        owner_spans=owner_spans,
        material_spans=material,
        publishable=publishable,
    )


def analyze_appendices(document_text: str, parsed: Any = None) -> AppendixAnalysis:
    """Extract all appendix groups and observed headers without cross-owner merging."""
    body = document_text or ""
    amendment = _trusted_amendment_spans(body)
    header_matches = _header_matches(body)
    appendix_bodies = _appendix_body_spans(header_matches, len(body))
    artifacts = _extract_artifacts(body, parsed, amendment, header_matches)
    analysis = AppendixAnalysis(declarations=_extract_declarations(body, parsed, amendment, appendix_bodies), artifacts=artifacts)
    local_declarations = [item for item in analysis.declarations if item.owner_kind is AppendixOwnerKind.PROJECT]
    local_artifacts = [item for item in analysis.artifacts if item.owner_kind is AppendixOwnerKind.PROJECT]
    if not local_declarations:
        return analysis
    owner_ids = {item.owner_id for item in local_declarations}
    if len(owner_ids) != 1:
        return analysis
    declared = {number for item in local_declarations for number in item.numbers}
    artifacts_by_number: dict[int, list[AppendixArtifact]] = defaultdict(list)
    for artifact in local_artifacts:
        artifacts_by_number[artifact.number].append(artifact)
    ambiguous_numbers = {number for number, items in artifacts_by_number.items() if len(items) > 1}

    resolved_genus = _resolve_declared_genus(local_declarations)
    if resolved_genus is _AMBIGUOUS_GENUS:
        return analysis

    form_artifacts = [
        item
        for item in local_artifacts
        if item.number not in ambiguous_numbers
        and item.classification is AppendixArtifactKind.FORM
        and (resolved_genus is None or item.form_genus == resolved_genus)
    ]
    actual = {item.number for item in form_artifacts}
    missing = actual - declared
    if missing:
        analysis.issues.append(
            _issue(
                AppendixIssueKind.MISSING_FORM_REFERENCE,
                local_declarations,
                [item for item in form_artifacts if item.number in missing],
                missing,
                uncertainty=False,
            )
        )
    invalid = (declared & set(artifacts_by_number)) - actual
    if invalid:
        relevant = [item for number in sorted(invalid) for item in artifacts_by_number.get(number, [])]
        certain = (
            not ambiguous_numbers.intersection(invalid)
            and all(len(artifacts_by_number.get(number, [])) == 1 for number in invalid)
            and len(relevant) == len(invalid)
            and all(item.classification is AppendixArtifactKind.NON_FORM for item in relevant)
        )
        analysis.issues.append(
            _issue(
                AppendixIssueKind.INVALID_DECLARED_RANGE,
                local_declarations,
                relevant,
                invalid,
                uncertainty=not certain,
            )
        )
    return analysis


def _affected_artifact_kinds(issue: AppendixIssue) -> set[AppendixArtifactKind]:
    return {span.artifact_kind for span in issue.material_spans if span.role is AppendixSpanRole.HEADER}


def appendix_issue_to_finding(issue: AppendixIssue, document_text: str) -> DetectorFinding:
    "Convert one typed issue to a finding."
    declaration = issue.declaration_spans[0]
    quote = document_text[declaration.char_start : declaration.char_end]
    numbers = ", ".join(f"№ {number}" for number in issue.affected_numbers)
    kinds = _affected_artifact_kinds(issue)
    if issue.issue_kind is AppendixIssueKind.MISSING_FORM_REFERENCE:
        if kinds == {AppendixArtifactKind.FORM}:
            rationale = f"В перечне форм не указаны приложения с формами: {numbers}."
        else:
            rationale = f"В перечне форм не указаны приложения: {numbers}."
        recommendation = f"Дополнить ссылку на приложения: {numbers}."
    else:
        if kinds == {AppendixArtifactKind.NON_FORM}:
            rationale = f"Объявленный перечень включает приложения, не являющиеся формами: {numbers}."
        else:
            rationale = f"Объявленный перечень включает приложения, вид которых не установлен: {numbers}."
        recommendation = "Привести объявленный диапазон приложений в соответствие с фактическим составом форм."
    # Конфликтующее место — сами приложения: цитируются их заголовки.
    headers = [
        span
        for span in issue.material_spans
        if span.role is AppendixSpanRole.HEADER and document_text[span.char_start : span.char_end].strip()
    ]
    return detector_finding(
        InternalCheck.APPENDIX,
        place="проект: перечень приложений с формами",
        quote=quote,
        rationale=rationale,
        remedy=recommendation,
        seconds=[
            (f"приложение № {span.appendix_numbers[0]}", document_text[span.char_start : span.char_end]) for span in headers
        ],
        related_places=[f"приложение № {number}" for number in issue.affected_numbers],
    )


def appendix_findings(document_text: str, parsed: Any = None) -> list[DetectorFinding]:
    """Находки по составу приложений. Публикуется только то, что доказано точными операндами проекта и читается в срезах."""
    analysis = analyze_appendices(document_text, parsed)
    return [
        appendix_issue_to_finding(issue, document_text)
        for issue in analysis.issues
        if issue.publishable and appendix_values_are_readable(issue, document_text)
    ]
