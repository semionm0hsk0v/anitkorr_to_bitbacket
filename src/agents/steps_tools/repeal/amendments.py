"""Чистые правила связи изменяющих актов с базовым и покрытия цепочки отменой проекта."""

from __future__ import annotations

import re
from collections.abc import Mapping

from src.agents.enums.repeal.units import UnitScopeStatus
from src.agents.schemas.repeal.detector import (
    Amender,
    OrphanResult,
    RepealTarget,
    UnitScopeGap,
    UnitScopeUnknown,
)
from src.agents.schemas.repeal.units import UnitScopeAssessment
from src.agents.steps_tools.repeal.clauses import (
    _DATE,
    _NUM,
    norm_act_num,
)
from src.agents.steps_tools.repeal.units import (
    known_act_date,
)
from src.agents.steps_tools.sources.normalization import card_date

_EDITORIAL_BLOCK = re.compile(
    r"\((?P<body>[^()]*(?:в\s+ред\.|введен[аы]?|изменен[аы]?|дополнен[аы]?|изложен[аы]?)[^()]*)\)",
    re.IGNORECASE | re.DOTALL,
)


_RED_ITEM = re.compile(rf"от\s+(?P<date>{_DATE})\s*(?:г\.?\s*)?[№N]\s*(?P<num>{_NUM})", re.IGNORECASE)


def parse_amendment_chain(act_full_text: str) -> list[Amender]:
    "Шаг 2 (парсер): цепочка изменявших из шапки «(в ред. приказов … )» базового акта."
    out: list[Amender] = []
    seen: set[tuple[str, str]] = set()
    for block in _EDITORIAL_BLOCK.finditer(act_full_text or ""):
        if not re.search(r"\bприказ[а-яё]*\b", block.group("body"), re.IGNORECASE):
            continue
        for m in _RED_ITEM.finditer(block.group("body")):
            number = norm_act_num(m.group("num"))
            date = card_date(m.group("date"))
            key = (date, number)
            if not number or key in seen:
                continue
            seen.add(key)
            out.append(Amender(number=number, date=date, raw=m.group(0).strip()))
    return out


def diff_orphans(
    base_number: str,
    repealed: list[RepealTarget],
    amenders: list[Amender],
    *,
    base_date: str | None = None,
    unit_scope: Mapping[tuple[str, str], UnitScopeAssessment] | None = None,
) -> OrphanResult:
    "Шаг 3 (ядро): изменявшие базовый акт минус отменённые проектом = орфаны."
    normalized_base_number = norm_act_num(base_number)
    normalized_base_date = known_act_date(base_date)

    def _coverage(amender: Amender) -> tuple[str, tuple[RepealTarget, ...]]:
        normalized_number = norm_act_num(amender.number)
        targets = [target for target in repealed if norm_act_num(target.number) == normalized_number]
        amender_date = known_act_date(amender.date)
        exact_targets = tuple(
            target for target in targets if amender_date is not None and known_act_date(target.date) == amender_date
        )
        if exact_targets:
            if any(not target.is_subpoint for target in exact_targets):
                return "covered", ()

            assessment = (unit_scope or {}).get((amender_date or "", normalized_number))
            if assessment is not None and assessment.status is UnitScopeStatus.PROVEN_COMPLETE:
                return "covered", ()
            if assessment is not None and assessment.status is UnitScopeStatus.PROVEN_INCOMPLETE:
                return "unit_incomplete", exact_targets
            return "unit_scope_unknown", exact_targets
        undated_subpoints = tuple(target for target in targets if target.is_subpoint and known_act_date(target.date) is None)
        if undated_subpoints:
            return "unit_scope_unknown", undated_subpoints
        if amender_date is not None and any(known_act_date(target.date) is not None for target in targets):
            return "uncovered", ()
        same_number_amenders = {
            (known_act_date(item.date) or "", norm_act_num(item.number))
            for item in amenders
            if norm_act_num(item.number) == normalized_number
        }
        same_number_targets = {(known_act_date(item.date) or "", norm_act_num(item.number)) for item in targets}
        if len(same_number_amenders) == len(same_number_targets) == 1:
            if any(not target.is_subpoint for target in targets):
                return "covered", ()
            if targets:
                return "unit_scope_unknown", tuple(targets)
        return "uncovered", ()

    def _is_base_act(amender: Amender) -> bool:
        return bool(
            normalized_base_number
            and normalized_base_date
            and norm_act_num(amender.number) == normalized_base_number
            and known_act_date(amender.date) == normalized_base_date
        )

    coverage = [(amender, *_coverage(amender)) for amender in amenders]
    orphans = [amender for amender, outcome, _targets in coverage if outcome == "uncovered" and not _is_base_act(amender)]
    unit_scope_unknown = [
        UnitScopeUnknown(amender=amender, project_targets=targets)
        for amender, outcome, targets in coverage
        if outcome == "unit_scope_unknown"
    ]
    unit_incomplete = [
        UnitScopeGap(
            amender=amender,
            assessment=(unit_scope or {})[(known_act_date(amender.date) or "", norm_act_num(amender.number))],
        )
        for amender, outcome, _targets in coverage
        if outcome == "unit_incomplete"
    ]
    return OrphanResult(
        base_number=normalized_base_number,
        orphans=orphans,
        unit_scope_unknown=unit_scope_unknown,
        unit_incomplete=unit_incomplete,
    )


_AMEND_FORMULA = re.compile(
    r"(?:о\s+)?внесени[еий]\w*\s+изменени|внести\s+(?:в\b|изменени)|дополнить\b|"
    r"изложить\s+в\s+(?:новой\s+)?редакции|призна[а-яё]*\s+утратив",
    re.IGNORECASE,
)


def _base_act_references(text: str, number: str) -> list[int]:
    """Позиции ссылок на базовый акт (по нормализованному номеру) в тексте."""
    return [m.start() for m in re.finditer(_NUM, text or "", re.IGNORECASE) if norm_act_num(m.group(0)) == number]


_DATE_ATTACHED_TO_NUMBER = re.compile(
    rf"\bот\s+(?P<date>{_DATE})\s*(?:г\.?\s*)?[№N]\s*$",
    re.IGNORECASE,
)


def _date_attached_to_number(text: str, position: int) -> str | None:
    """Date directly bound to the number at ``position``, never to a nearby act."""
    prefix = text[max(0, position - 64) : position]
    match = _DATE_ATTACHED_TO_NUMBER.search(prefix)
    return known_act_date(match.group("date")) if match is not None else None


def _enclosing_paragraph(text: str, position: int) -> str:
    "Абзац, внутри которого стоит ссылка: естественная структурная граница пункта."
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return text[start : end if end != -1 else len(text)]


def amends_base_act(title: str, full_text: str, base: RepealTarget) -> bool | None:
    "Доказан ли ПОЛОЖИТЕЛЬНО факт «этот акт изменял базовый акт» его СОБСТВЕННЫМ текстом."
    text = f"{title or ''}\n{full_text or ''}"
    base_number = norm_act_num(base.number)
    if not base_number:
        return False
    base_date = known_act_date(base.date)
    has_undated_formula = False
    for position in _base_act_references(text, base_number):
        if not _AMEND_FORMULA.search(_enclosing_paragraph(text, position)):
            continue
        reference_date = _date_attached_to_number(text, position)
        if base_date is not None and reference_date is not None:
            if reference_date == base_date:
                return True
            continue
        has_undated_formula = True
    return None if has_undated_formula else False


def _issuer_stems(title: str) -> set[str]:
    """Основы значимых слов заголовка — грубая, но устойчивая подпись органа."""
    low = (title or "").lower().replace("ё", "е")
    return {word[:6] for word in re.findall(r"[а-яa-z]+", low) if len(word) >= 6} - _GENERIC_STEMS


_GENERIC_STEMS = frozenset(
    word[:6]
    for word in (
        "приказ",
        "приказа",
        "приказом",
        "приказов",
        "внесении",
        "изменений",
        "изменения",
        "утверждении",
        "административного",
        "регламента",
        "предоставления",
        "государственной",
        "отдельные",
        "нормативные",
        "правовые",
        "признании",
        "утратившими",
    )
)


def same_issuer(base_title: str, amender_title: str) -> bool:
    "Отклоняем только ПОЛОЖИТЕЛЬНОЕ расхождение органов, а не любое несовпадение."
    base_stems, amender_stems = _issuer_stems(base_title), _issuer_stems(amender_title)
    if not base_stems or not amender_stems:
        return True
    return bool(base_stems & amender_stems)


def unit_targets_for(amender: Amender, repealed: list[RepealTarget]) -> tuple[RepealTarget, ...]:
    "Положения ИМЕННО этого редактора, которые проект признаёт утратившими силу."
    number, date = norm_act_num(amender.number), known_act_date(amender.date)
    if date is None:
        return ()
    exact = [target for target in repealed if norm_act_num(target.number) == number and known_act_date(target.date) == date]
    if any(not target.is_subpoint for target in exact):
        return ()
    return tuple(target for target in exact if target.is_subpoint)
