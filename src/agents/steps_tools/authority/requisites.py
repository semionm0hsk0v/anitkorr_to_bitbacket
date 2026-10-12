"""Подготовка входа компетенции, подтверждение реквизитов и факт о форме проекта."""

from __future__ import annotations

import re

from src.agents.enums.competence.sources import ProjectAction
from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.schemas.competence.parse import CitedAct, CompetenceParse
from src.agents.schemas.competence.sources import CompetenceRefs
from src.agents.schemas.legal_base.source_refs import SourceRef
from src.agents.schemas.parse.parsed_document import ParsedDocument
from src.agents.steps_tools.document.act_basis import enacting_formula_end
from src.agents.steps_tools.sources.act_refs import (
    canon_act,
    find_act_requisites,
    kind_from_requisites,
    quote_requisites,
    referenced_acts,
)
from src.agents.steps_tools.sources.normalization import card_date
from src.agents.steps_tools.sources.region_banks import bank_for_region, region_from_text
from src.common.config.config import settings
from src.infrastructure.knowledge.organ_form import classify_organ_kind, form_matches_kind, forms_for_kind, label_for_kind
from src.infrastructure.knowledge.organ_statutes import load_organ_statutes

_PREAMBLE_MARKERS = ("в соответствии", "во исполнение", "в целях", "руководствуясь", "приказыва", "постановля", "распоряжа")


def parse_input(parsed: ParsedDocument | None) -> str:
    "Начало проекта до формулы издания включительно, не длиннее ``parse_input_chars``."
    limit = int(settings["agents.competence.parse_input_chars"])
    text = parsed.enriched_text if parsed is not None else ""
    end = enacting_formula_end(text)
    return text[: min(end if end is not None else len(text), limit)]


def project_title_from_header(header: str) -> str:
    """Заголовок между отдельной строкой вида акта и преамбулой; иной формат — модели."""
    preamble = re.search(r"(?im)^\s*(?:В соответствии с|Во исполнение|Руководствуясь|В целях)\b", header)
    if preamble is None:
        return ""
    zone = header[: preamble.start()]
    titles = list(re.finditer(r"(?m)^[ \t]*(?:О|Об)\s+\S", zone))
    if len(titles) != 1:
        return ""
    start = titles[0].start()
    if not re.search(r"(?im)^\s*(?:приказ|постановление|распоряжение|указ|закон)\s*$", zone[:start]):
        return ""
    return " ".join(zone[start:].split())


def refs_from_header(header: str) -> CompetenceRefs | None:
    """Разобрать стандартную шапку известного органа; иной формат оставить модели."""
    form = re.search(r"(?im)^\s*(приказ|постановление|распоряжение|указ|закон)\s*$", header)
    title = project_title_from_header(header)
    if form is None or not title:
        return None
    issuer = " ".join(re.sub(r"(?im)^\s*проект\s*$", "", header[: form.start()]).split())
    catalog = load_organ_statutes()
    if not any(issuer.casefold() == row.issuer.casefold() for row in catalog.statutes):
        return None
    region = region_from_text(issuer)
    region_match = re.search(re.escape(region), issuer, re.IGNORECASE) if region else None
    if region_match is None:
        return None

    if title.startswith(("Об утверждении ", "О утверждении ")):
        action = ProjectAction.APPROVE
    elif title.startswith("О внесении изменений "):
        action = ProjectAction.AMEND
    else:
        return None
    if "кодекс" in header.casefold():
        return None
    requisites = find_act_requisites(header)
    if len(requisites) != len(re.findall(r"[№N]\s*\d", header)):
        return None
    if set(referenced_acts(header)) - {canon_act(ref.raw) for ref in requisites}:
        return None
    if any(not ref.date or kind_from_requisites(ref) == "other" for ref in requisites):
        return None
    targets = find_act_requisites(title)
    if action is ProjectAction.AMEND and len(targets) != 1:
        return None
    if action is ProjectAction.APPROVE and targets:
        return None
    target_key = (targets[0].number, targets[0].date) if targets else None
    target = None
    cited = []
    for ref in requisites:
        is_target = (ref.number, ref.date) == target_key
        kind = kind_from_requisites(ref)
        source = SourceRef(
            kind=kind,
            number=ref.number,
            date=card_date(ref.date),
            title=ref.title if kind == "regional_government_resolution" else "",
            raw=ref.raw,
            role=SourceRole.TARGET if is_target else SourceRole.CITED,
            origin=SourceOrigin.HEADER if is_target else SourceOrigin.PREAMBLE,
        )
        if is_target:
            target = source
        else:
            cited.append(source)
    return CompetenceRefs(
        project_title=title,
        issuer=issuer,
        region=region_match[0],
        form=form[1].lower(),
        action=action,
        target_act=target,
        cited_acts=tuple(cited),
    )


def _grounded_ref(act: CitedAct, source: str, *, role: SourceRole, origin: SourceOrigin) -> SourceRef | None:
    """Принимать реквизиты только из одной дословной ссылки во входном тексте."""
    quote = " ".join(act.quote.split())
    if not quote or quote not in " ".join(source.split()):
        return None
    if act.kind == "code":
        number, date = "", ""
    else:
        requisites = quote_requisites(quote)
        if requisites is None:
            return None
        number, date = requisites
    titles = {
        ref.title
        for ref in find_act_requisites(source)
        if act.kind == "regional_government_resolution"
        and ref.number == number
        and card_date(ref.date) == card_date(date)
        and ref.title
    }
    return SourceRef(
        kind=act.kind,
        number=number,
        date=card_date(date) if date else "",
        title=titles.pop() if len(titles) == 1 else "",
        raw=quote,
        role=role,
        origin=origin,
    )


def refs_from_model(result: CompetenceParse, source: str) -> tuple[CompetenceRefs, int]:
    """Ответ модели → контракт C2; незаземлённые ссылки отбрасываются. Возвращает и их число."""
    dropped = 0
    target: SourceRef | None = None
    if result.target_act is not None:
        target = _grounded_ref(result.target_act, source, role=SourceRole.TARGET, origin=SourceOrigin.HEADER)
        if target is None:
            dropped += 1
    cited: list[SourceRef] = []
    for act in result.cited_acts:
        ref = _grounded_ref(act, source, role=SourceRole.CITED, origin=SourceOrigin.PREAMBLE)
        if ref is None:
            dropped += 1
        else:
            cited.append(ref)

    zone = " ".join(issuer_zone(source).split())
    issuer_text = " ".join(result.issuer.split())
    issuer = issuer_text if issuer_text and issuer_text in zone else ""
    region_text = " ".join(result.region.split())
    region = region_text if region_text and region_text in zone and bank_for_region(region_text) else ""
    project_title = " ".join(result.project_title.split())
    refs = CompetenceRefs(
        project_title=project_title if project_title and project_title in " ".join(source.split()) else "",
        issuer=issuer,
        form=result.form if result.form != "иное" else "",
        action=ProjectAction(result.action),
        target_act=target,
        cited_acts=tuple(cited),
        region=region,
    )
    return refs, dropped


def issuer_zone(header: str) -> str:
    """Часть шапки ДО преамбулы/формулы принятия (где стоят реквизиты органа-инициатора)."""
    low = (header or "").lower().replace("ё", "е")
    cut = len(header)
    for m in _PREAMBLE_MARKERS:
        i = low.find(m)
        if i != -1:
            cut = min(cut, i)
    return header[:cut]


def form_fact(refs: CompetenceRefs) -> str:
    """§5.3: допустимость вида акта для вида органа — по справочнику, до модели."""
    kind = classify_organ_kind(refs.issuer)
    form = refs.form
    if kind == "unknown" or not form:
        return "Вид органа-издателя или вид проекта по справочнику не установлен."
    allowed = ", ".join(forms_for_kind(kind))
    if form_matches_kind(form, kind):
        return (
            f"По справочнику вид акта «{form}» входит в допустимые для вида органа «{label_for_kind(kind)}» ({allowed}). "
            "Это не закрывает вопрос: решение может быть закреплено за другим органом или требовать иной формы."
        )
    return (
        f"УСТАНОВЛЕНО по справочнику: вид акта «{form}» не входит в допустимые для вида органа "
        f"«{label_for_kind(kind)}» ({allowed})."
    )
