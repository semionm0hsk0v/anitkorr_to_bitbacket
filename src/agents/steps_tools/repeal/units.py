from __future__ import annotations

import re
from collections.abc import Sequence

from src.agents.enums.repeal.units import UnitRole, UnitScopeReason, UnitScopeStatus
from src.agents.schemas.repeal.units import AmenderUnit, BaseActIdentity, UnitScopeAssessment
from src.agents.steps_tools.sources.normalization import card_date

_NUM = r"\d+\s*-\s*[А-Яа-яЁёA-Za-z]+"
_DATE = r"\d{1,2}\.\d{1,2}\.\d{4}"


_UNIT_LABEL = re.compile(r"^[ \t]*(?P<label>\d{1,3})\.(?!\d)", re.MULTILINE)


_AMENDMENT_FORMULA = re.compile(
    r"(?:о\s+)?внесени[еий]\w*\s+изменени|внести\s+(?:в\b|изменени)|дополнить\b|"
    r"изложить\s+в\s+(?:новой\s+)?редакции|призна[а-яё]*\s+утратив|заменить\b|исключить\b",
    re.IGNORECASE,
)


_DATED_ACT_REFERENCE = re.compile(rf"от\s+{_DATE}\s*(?:г\.?\s*)?(?:[№N]\s*)?{_NUM}", re.IGNORECASE)


_ACT_KINDS: dict[str, str] = {
    "приказ": r"приказ[а-яё]*",
    "постановление": r"постановлени[а-яё]*",
    "распоряжение": r"распоряжени[а-яё]*",
    "решение": r"решени[а-яё]*",
    "указ": r"указ(?:ом|а|у|е|ы|ов|ам|ами|ах)?\b",
    "закон": r"закон(?:ом|а|у|е|ы|ов|ам|ами|ах)?\b",
}


_ACT_ANCHOR = re.compile(
    r"(?P<kind>" + "|".join(_ACT_KINDS.values()) + r")(?P<issuer>[^;«»№N]{0,200}?)"
    rf"\s*от\s+(?P<date>{_DATE})\s*(?:г\.?\s*)?(?:[№N]\s*)?(?P<num>{_NUM})",
    re.IGNORECASE,
)


_ENUMERATION_LINK = re.compile(r"^[\s,;]*(?:и|а\s+также|также)?[\s,;]*(?:во?|на)?[\s,;]*$", re.IGNORECASE)

#: Родовые основы, не различающие орган: они есть в заголовке любого регионального акта.
_GENERIC_ISSUER_STEMS: frozenset[str] = frozenset(
    word[:6]
    for word in (
        "министерства",
        "министерством",
        "департамента",
        "управления",
        "агентства",
        "области",
        "областного",
        "района",
        "города",
        "российской",
        "федерации",
        "отдельные",
        "нормативные",
        "правовые",
        "государственной",
        "административного",
        "регламента",
        "предоставления",
        "утверждении",
        "изменений",
    )
)


_CLOSING_CLAUSE = re.compile(
    r"[ \t]*\d{1,3}\.[ \t]*(?:[^\n]{0,80}?)(?:вступает\s+в\s+силу|контрол[ья]\s+за\s+исполнением|"
    r"подлежит\s+официальному\s+опубликован)",
    re.IGNORECASE,
)


_UNIT_LOST_FORCE = re.compile(r"^[ \t]*\d{1,3}\.\s*(?:[-–—]\s*)?утратил[аио]?\s+силу\b", re.IGNORECASE)


TRAILING_REGION = "tail"


def normalize_unit_label(raw: str) -> str:
    "«п. 18.» / « 18 » → «18»; неадресуемое обозначение остаётся пустым."
    match = re.fullmatch(r"[ \t]*(?:п\.?|пункт[а-яё]*)?[ \t]*(\d{1,3})[.)]?[ \t]*", raw or "", re.IGNORECASE)
    return match.group(1) if match else ""


def parse_unit_labels(raw: str) -> tuple[tuple[str, ...], bool]:
    "Цель отмены проекта → ``(адреса, разобрана ли она полностью)``."
    body = re.sub(r"^[ \t]*(?:под)?пункт[а-яё]*\b", "", raw or "", flags=re.IGNORECASE)
    parts = [part for part in re.split(r"[,;]|\bи\b", body) if part.strip()]
    if not parts:
        return (), False
    labels = [normalize_unit_label(part) for part in parts]
    return tuple(label for label in labels if label), all(labels)


def known_act_date(value: str | None) -> str | None:
    """Нормализованная дата или ``None``: отсутствующая дата остаётся неизвестной."""
    normalized = card_date(value or "")
    return normalized if re.fullmatch(_DATE, normalized) else None


def normalize_act_kind(raw: str) -> str:
    "Слово-вид из текста → канон («приказом» → «приказ»); неизвестное — пусто."
    low = (raw or "").casefold().replace("ё", "е")
    for canonical in _ACT_KINDS:
        if low.startswith(canonical[:6]):
            return canonical
    return ""


def _issuer_stems(text: str) -> set[str]:
    """Различающие орган основы: родовые слова («министерства», «области») выброшены."""
    low = (text or "").casefold().replace("ё", "е")
    low = re.sub(r"\bминсоцразвития\b", "министерства социального развития", low)
    return {word[:6] for word in re.findall(r"[а-яa-z]+", low) if len(word) >= 3} - _GENERIC_ISSUER_STEMS


def issuer_relation(base_issuer: str, reference_issuer: str) -> bool | None:
    "``True`` — тот же орган, ``False`` — положительно другой, ``None`` — не доказано."
    if not (base_issuer or "").strip():
        return True
    kinds = []
    for name in (base_issuer, reference_issuer):
        normalized = re.sub(r"\bминсоцразвития\b", "министерство", name.casefold())
        kinds.append(next((kind for kind in ("министер", "департамент", "управлен", "агентств") if kind in normalized), None))
    if all(kinds) and kinds[0] != kinds[1]:
        return False
    base_stems, reference_stems = _issuer_stems(base_issuer), _issuer_stems(reference_issuer)
    if not base_stems or not reference_stems:
        return None
    if base_stems == reference_stems:
        return True

    if base_stems & reference_stems:
        return None
    return False


def normalize_act_number(raw: str) -> str:
    """«11-П» → «11-п» (ё→е, суффикс вниз) — тот же канон, что у ``repeal_check``."""
    match = re.search(r"(\d+)\s*-\s*([А-Яа-яЁёA-Za-z]+)", raw or "")
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2).lower().replace('ё', 'е')}"


def _inside_quotation(text: str, position: int, *, start: int = 0) -> bool:
    "Лежит ли позиция внутри кавычек — то есть в цитируемом тексте другого акта."
    endings: list[str] = []
    opening = {"«": "»", "„": "“", "“": "”"}
    for char in text[start:position]:
        if endings and char == endings[-1]:
            endings.pop()
        elif char in opening:
            endings.append(opening[char])
    return bool(endings)


def split_top_level_units(text: str) -> tuple[tuple[AmenderUnit, ...], tuple[str, ...], str]:
    "Разбор адресного пространства редактора: ``(пункты, пропуски ряда, хвост)``."
    body = text or ""
    accepted: list[tuple[int, int]] = []
    tail_start: int | None = None
    for match in _UNIT_LABEL.finditer(body):
        label = int(match.group("label"))
        if _inside_quotation(body, match.start(), start=accepted[-1][1] if accepted else 0):
            # Нумерация цитируемой новой редакции принадлежит изменяемому акту.
            continue
        if _CLOSING_CLAUSE.match(body, match.start()):
            tail_start = match.start()
            break
        if not accepted or label > accepted[-1][0]:
            accepted.append((label, match.start()))
        elif tail_start is None:
            tail_start = match.start()
            break
    if not accepted:
        return (), (), body
    if tail_start is None:
        tail_start = len(body)
    units: list[AmenderUnit] = []
    for index, (label, start) in enumerate(accepted):
        end = accepted[index + 1][1] if index + 1 < len(accepted) else tail_start
        units.append(AmenderUnit(label=str(label), role=UnitRole.UNRESOLVED, start=start, end=end))
    observed = {label for label, _start in accepted}
    gaps = tuple(str(number) for number in range(1, accepted[-1][0] + 1) if number not in observed)
    return tuple(units), gaps, body[tail_start:]


def issuer_from_reference(text: str) -> str:
    "Орган из ПЕРВОЙ полной ссылки на акт: «Приказ <орган> от ДАТА N НОМЕР»."
    match = _ACT_ANCHOR.search(text or "")
    return " ".join(match.group("issuer").split()) if match is not None else ""


def _governing_text(body: str, anchor_start: int, previous_ends: list[int]) -> str:
    "Текст предписания, управляющего ИМЕННО этой ссылкой."
    window_start = body.rfind("\n", 0, anchor_start) + 1
    for end in sorted((end for end in previous_ends if end <= anchor_start), reverse=True):
        if end < window_start:
            break
        if not _ENUMERATION_LINK.match(body[end:anchor_start]):
            return body[end:anchor_start]
        anchor_start = end
    return body[window_start:anchor_start]


def _classify_unit(body: str, base: BaseActIdentity) -> tuple[UnitRole, str]:
    "Роль одного пункта относительно базы и вид доказательства связи."
    if _UNIT_LOST_FORCE.match(body):
        return UnitRole.LOST_FORCE, ""
    anchors = list(_ACT_ANCHOR.finditer(body))
    anchor_ends = [anchor.end() for anchor in anchors]
    resolved_spans: list[tuple[int, int]] = []
    unresolved = False
    for anchor in anchors:
        if normalize_act_number(anchor.group("num")) != base.number:
            resolved_spans.append(anchor.span())  # другой номер — другой акт
            continue
        if known_act_date(anchor.group("date")) != base.date:
            resolved_spans.append(anchor.span())  # тот же номер другой даты — другой акт
            continue
        kind = normalize_act_kind(anchor.group("kind"))
        if base.kind and kind and kind != base.kind:
            resolved_spans.append(anchor.span())  # тот же номер и дата другого вида
            continue
        issuer = issuer_relation(base.issuer, anchor.group("issuer"))
        if issuer is False:
            resolved_spans.append(anchor.span())  # тот же номер и дата другого органа
            continue
        resolved_spans.append(anchor.span())
        if not base.exact or not kind or issuer is None:
            unresolved = True
            continue
        if _AMENDMENT_FORMULA.search(_governing_text(body, anchor.start(), anchor_ends)):
            return UnitRole.AMENDS_BASE, "unit_amendment_formula"

        unresolved = True
    for match in re.finditer(_NUM, body, re.IGNORECASE):
        if normalize_act_number(match.group(0)) != base.number:
            continue
        if any(start <= match.start() < end for start, end in resolved_spans):
            continue
        # Наш номер назван без полных реквизитов: ни доказать, ни исключить.
        unresolved = True
        break
    if unresolved:
        return UnitRole.UNRESOLVED, ""
    if _AMENDMENT_FORMULA.search(body) and not _DATED_ACT_REFERENCE.search(body):
        return UnitRole.UNRESOLVED, ""
    return UnitRole.OTHER_SUBJECT, ""


def assess_unit_scope(
    *,
    amender_text: str,
    base_number: str,
    base_date: str | None,
    repealed_units: Sequence[str],
    base_kind: str = "",
    base_issuer: str = "",
    source_complete: bool = True,
) -> UnitScopeAssessment:
    "Достаточен ли объём отмены проекта, чтобы снять изменения редактора в эту базу."
    normalized_base = normalize_act_number(base_number)
    normalized_date = known_act_date(base_date)
    if not normalized_base or normalized_date is None:
        return UnitScopeAssessment(status=UnitScopeStatus.UNKNOWN, reason=UnitScopeReason.BASE_IDENTITY_NOT_EXACT)
    base = BaseActIdentity(
        number=normalized_base,
        date=normalized_date,
        kind=normalize_act_kind(base_kind),
        issuer=base_issuer,
    )
    if not source_complete:
        return UnitScopeAssessment(status=UnitScopeStatus.UNKNOWN, reason=UnitScopeReason.SOURCE_TEXT_INCOMPLETE)

    parsed, gaps, tail = split_top_level_units(amender_text)
    if not parsed:
        return UnitScopeAssessment(status=UnitScopeStatus.UNKNOWN, reason=UnitScopeReason.UNIT_SEQUENCE_NOT_FOUND)

    body = amender_text or ""
    units: list[AmenderUnit] = []
    for unit in parsed:
        role, proof = _classify_unit(body[unit.start : unit.end], base)
        units.append(AmenderUnit(label=unit.label, role=role, start=unit.start, end=unit.end, relation_proof=proof))

    unresolved = [*gaps, *(unit.label for unit in units if unit.role is UnitRole.UNRESOLVED)]
    if any(normalize_act_number(m.group(0)) == base.number for m in re.finditer(_NUM, tail, re.IGNORECASE)):
        unresolved.append(TRAILING_REGION)

    bound = tuple(unit.label for unit in units if unit.role is UnitRole.AMENDS_BASE)
    labels = {unit.label for unit in units}
    project_units: set[str] = set()
    targets_parsed = True
    for item in repealed_units:
        parsed_labels, complete = parse_unit_labels(item)
        project_units.update(parsed_labels)
        targets_parsed = targets_parsed and complete
    covered = tuple(label for label in bound if label in project_units)
    uncovered = tuple(label for label in bound if label not in project_units)
    frozen_units = tuple(units)
    frozen_unresolved = tuple(dict.fromkeys(unresolved))

    def _result(status: UnitScopeStatus, reason: UnitScopeReason) -> UnitScopeAssessment:
        return UnitScopeAssessment(
            status=status,
            reason=reason,
            bound_units=bound,
            covered_units=covered,
            uncovered_units=uncovered,
            unresolved_units=frozen_unresolved,
            units=frozen_units,
        )

    if not base.exact:
        return _result(UnitScopeStatus.UNKNOWN, UnitScopeReason.BASE_IDENTITY_NOT_EXACT)
    if not targets_parsed:
        return _result(UnitScopeStatus.UNKNOWN, UnitScopeReason.PROJECT_UNIT_NOT_PARSED)
    if not project_units <= labels:
        return _result(UnitScopeStatus.UNKNOWN, UnitScopeReason.PROJECT_UNIT_NOT_LOCATED)
    if uncovered:
        return _result(UnitScopeStatus.PROVEN_INCOMPLETE, UnitScopeReason.BOUND_UNITS_NOT_REPEALED)
    if gaps:
        return _result(UnitScopeStatus.UNKNOWN, UnitScopeReason.UNIT_SEQUENCE_HAS_GAPS)
    if frozen_unresolved:
        return _result(UnitScopeStatus.UNKNOWN, UnitScopeReason.UNRESOLVED_UNITS_PRESENT)
    if not bound:
        return _result(UnitScopeStatus.UNKNOWN, UnitScopeReason.NO_UNIT_BINDS_BASE)
    return _result(UnitScopeStatus.PROVEN_COMPLETE, UnitScopeReason.ALL_BOUND_UNITS_REPEALED)
