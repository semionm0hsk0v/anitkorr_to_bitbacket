"""Ссылки проекта, сведения о кандидатах и правила отбора актов для сверки."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from src.agents.enums.legal_base.source_refs import SourceOrigin, SourceRole
from src.agents.schemas.legal.inventory import NamedAct
from src.agents.schemas.legal.norm import Norm
from src.agents.schemas.legal.plan import (
    NamedActChoice,
    PlanDecision,
    PlanOutcome,
    PlannedSource,
    SourceSelection,
)
from src.agents.schemas.legal_base.act_refs import ActRequisites
from src.agents.schemas.legal_base.source_refs import CARD_VID_BY_KIND, SourceRef
from src.agents.schemas.parse.parsed_document import ParsedDocument
from src.agents.steps_tools.repeal.clauses import collect_repeal_targets, norm_act_num
from src.agents.steps_tools.sources.act_refs import (
    _deglue_ot_before_date,
    find_act_requisites,
    issuer_without_page_numbers,
    kind_from_requisites,
    parse_act_requisites,
)
from src.agents.steps_tools.sources.normalization import (
    act_authority_hint,
    card_date,
    card_number_identity,
    card_vid,
    issuer_key,
)
from src.agents.steps_tools.sources.region_banks import region_key
from src.agents.steps_tools.sources.selection import act_hygiene_reject
from src.common.config.config import settings
from src.common.utils.logger import get_logger

log = get_logger(__name__)

#: Виды, у которых ``identity()`` хранит именованного издателя.
_NAMED_ISSUER_KINDS = frozenset({"federal_order", "regional_order", "other"})


def normalized_document(document: str) -> str:
    """Исправить OCR-склейки и привести пробелы к единому виду."""
    text_without_glued_words = _deglue_ot_before_date(document)
    return " ".join(text_without_glued_words.split())


def extract_cited_acts(document: str) -> list[NamedAct]:
    """Найти упомянутые в проекте акты и сохранить контекст упоминания."""
    normalized_text = normalized_document(document)
    extracted_requisites = find_act_requisites(document)

    # Разные шаблоны парсера могут распознать одно и то же упоминание.
    unique_requisites: dict[str, ActRequisites] = {}
    for requisites in extracted_requisites:
        unique_requisites.setdefault(requisites.raw, requisites)

    ordered_requisites = sorted(unique_requisites.values(), key=lambda act: normalized_text.find(act.raw))
    cited_acts = []
    for requisites in ordered_requisites:
        act_kind = kind_from_requisites(requisites)
        act_date = card_date(requisites.date) if requisites.date else ""

        position = normalized_text.find(requisites.raw)
        quote_start = max(0, position - 180)
        quote_end = position + len(requisites.raw) + 250
        mention_quote = normalized_text[quote_start:quote_end]
        cited_act = NamedAct(
            kind=act_kind,
            number=requisites.number,
            date=act_date,
            title=requisites.title,
            raw=requisites.raw,
            role=SourceRole.CITED,
            origin=SourceOrigin.BODY,
            quote=mention_quote,
        )

        cited_acts.append(cited_act)

    return cited_acts


def selection_title(title: str) -> str:
    """Заголовок карточки для отбора без перечня приложений и сведений о регистрации."""
    return re.split(r"\n\((?:вместе с|Зарегистрировано)", title, maxsplit=1)[0]


def title_mentions(title: str, document: str) -> list[str]:
    """Дословные названия в кавычках карточки, присутствующие в проекте; без вывода о применимости."""
    normalized = normalized_document(document).casefold()
    names = re.findall(r'(?=["«]([^"«»\n]+)["»])', title)
    return list(
        dict.fromkeys(name for name in names if len(name.split()) >= 2 and normalized_document(name).casefold() in normalized)
    )


def selection_card(norm: SourceRef, document: str) -> dict[str, object]:
    """Компактные сведения для отбора; исходная карточка не меняется."""
    candidate = norm.model_dump(include={"kind", "number", "date", "title", "quote"})
    candidate["title"] = selection_title(norm.title)
    # Полный заголовок уже содержит реквизиты; неполный получает отдельные поля.
    if parse_act_requisites(norm.title.splitlines()[0] if norm.title else "") is not None:
        for field in ("kind", "number", "date"):
            candidate.pop(field)
    quote = getattr(norm, "quote", "")
    if not quote:
        candidate.pop("quote", None)
    mentions = title_mentions(norm.title, document)
    if mentions:
        candidate["project_mentions"] = mentions
    raw = " ".join(norm.raw.split())
    description = " ".join(f"{norm.title} {quote} {norm.number} {norm.date}".split())
    if raw and raw not in description:
        candidate["raw"] = norm.raw
    return candidate


def selection_quote_matches(quote: str, document: str) -> bool:
    """Привязка отбора допускает склеенные OCR-пробелы, но не изменение символов."""
    needle = "".join(quote.split())
    return bool(needle) and needle in "".join(document.split())


def project_blocks(document: str) -> dict[str, str]:
    """Нумерованные фрагменты без потери текста и разрыва строк проекта."""
    target = int(settings["agents.legal.selection_block_chars"])
    blocks: dict[str, str] = {}
    lines: list[str] = []
    size = 0
    for line in document.splitlines(keepends=True):
        lines.append(line)
        size += len(line)
        if size >= target:
            blocks[f"B{len(blocks) + 1}"] = "".join(lines)
            lines, size = [], 0
    if lines:
        blocks[f"B{len(blocks) + 1}"] = "".join(lines)
    return blocks


def selection_material(document: str, questions: Sequence[Sequence[str]], cited_quotes: Sequence[str]) -> str:
    """Исходные фрагменты по вопросам; неизвестные ID сохраняют полный проект."""
    blocks = project_blocks(document)
    if not questions or any(not ids or any(identity not in blocks for identity in ids) for ids in questions):
        return document
    selected = {identity for ids in questions for identity in ids}
    fragments = [f"⟦{identity}⟧\n{text}" for identity, text in blocks.items() if identity in selected]
    normalized = normalized_document(document)
    for quote in dict.fromkeys(cited_quotes):
        if quote and normalized_document(quote) in normalized:
            fragments.append(quote)
    return "\n\n".join(fragments)


def identity(ref: SourceRef, *, region: str = "") -> tuple[str, ...]:
    """Уровень, территория и явный издатель не теряются за общим видом карточки."""
    reqs = [r for r in find_act_requisites(ref.raw) if r.identity_number == card_number_identity(ref.number)]
    source_region = reqs[0].region if reqs else ""
    territory = region_key(source_region or ("" if ref.federal else region))
    issuer = ""
    if ref.kind in _NAMED_ISSUER_KINDS or ref.kind in {"rf_directive", "regional_directive"}:
        issuer = act_authority_hint(reqs[0]) if reqs else ""
        # Неизвестный издатель не доказывает тождество актов разных органов.
        issuer = issuer or " ".join((ref.raw or "").split()).casefold()
    return (ref.kind, card_number_identity(ref.number), card_date(ref.date), territory, issuer)


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(card_date(value), "%d.%m.%Y")
    except ValueError:
        return False
    return True


def _grounded(ref: Norm, document: str) -> bool:
    quote = normalized_document(ref.raw)
    if not quote or quote not in normalized_document(document):
        return False
    # Короткие цитаты «от ... № ...» допустимы, но 23 больше не совпадает с 123.
    reqs = find_act_requisites(quote) or find_act_requisites(f"{CARD_VID_BY_KIND[ref.kind]} {quote}")
    return any(
        r.identity_number == card_number_identity(ref.number)
        and r.card_date == card_date(ref.date)
        and kind_from_requisites(r) == ref.kind
        for r in reqs
    )


def _rejection_reason(ref: Norm, document: str, *, region: str) -> str:
    if not (ref.number.strip() and _valid_date(ref.date)):
        return "missing_or_invalid_requisites"
    if ref.origin is not SourceOrigin.SEARCH and ref.raw.strip() and not _grounded(ref, document):
        return "quote_or_requisites_not_in_project"
    return act_hygiene_reject(ref.bank, ref.title, region=region)


def _duplicate_notes(previous: Norm, ref: Norm) -> list[str]:
    """Разные названия одного акта не становятся подтверждёнными после дедупликации."""
    prev_name, new_name = _name_key(previous), _name_key(ref)
    if prev_name and new_name and not _compatible_names(prev_name, new_name):
        return [
            f"Акт {ref.label} назван по-разному: «{' '.join((_name_text(previous) or '').split())}» "
            f"и «{' '.join((_name_text(ref) or '').split())}»; "
            "реквизиты сведены в один источник, название не подтверждено."
        ]
    return []


#: Столько же требует извлечение названия из текста (``act_refs._TITLE_AFTER_RE``).
_MIN_NAME_LETTERS = int(settings["agents.legal.plan.min_name_letters"])


_SAME_NAME_PREFIX_SHARE = float(settings["agents.legal.plan.same_name_prefix_share"])


def _name_text(ref: SourceRef) -> str:
    "Название акта без реквизитов и редакции; пусто — название ничем не подтверждено."
    if ref.origin is SourceOrigin.MODEL:
        return ""
    title = ref.title or ""
    if isinstance(ref, Norm) or ref.origin is SourceOrigin.SEARCH:
        start = title.find('"')
        if start < 0:
            return ""
        title = "\n".join(line for line in title[start:].splitlines() if not line.lstrip().startswith("("))
    return title


def _name_key(ref: SourceRef) -> str:
    letters = "".join(ch for ch in _name_text(ref).casefold().replace("ё", "е") if ch.isalpha())
    return letters if len(letters) >= _MIN_NAME_LETTERS else ""


def _compatible_names(a: str, b: str) -> bool:
    """Одно название могло быть обрезано до другого: расхождения смысла не доказано."""
    return a in b or b in a


def _same_name(a: str, b: str) -> bool:
    """Достаточно для вывода «это одно и то же название»: равенство или почти полный префикс."""
    short, long = sorted((a, b), key=len)
    return long.startswith(short) and len(short) >= _SAME_NAME_PREFIX_SHARE * len(long)


def _conflict_scope(ref: SourceRef) -> tuple[str, ...]:
    """Вид, территория и издатель: реквизиты сравниваются только внутри одного органа."""
    kind, _number, _date, region, issuer = identity(ref)
    return (kind, region, issuer)


def _conflicting(a: SourceRef, name_a: str, b: SourceRef, name_b: str) -> bool:
    """Одно название одного органа под разным номером (при той же дате) или разной датой."""
    if _conflict_scope(a) != _conflict_scope(b):
        return False
    if not (name_a and name_b and _same_name(name_a, name_b)):
        return False
    same_number = card_number_identity(a.number) == card_number_identity(b.number)
    same_date = card_date(a.date) == card_date(b.date)
    return same_number != same_date


def _card_label(card: NamedAct | Norm) -> str:
    """Координаты найденного документа или название ещё не найденной ссылки."""
    return f"{card.bank}:{card.doc_num}" if isinstance(card, Norm) else card.label


def _confirming_card(ref: SourceRef, cards: Sequence[NamedAct | Norm]) -> str:
    """ID карточки К+, реквизиты и название которой совпали с этим вариантом; пусто — не подтверждён."""
    if ref.origin is SourceOrigin.SEARCH:
        return next((_card_label(c) for c in cards if identity(c) == identity(ref) and c.title == ref.title), ref.label)
    name = _name_key(ref)
    for card in cards:
        card_name = _name_key(card)
        if identity(card) == identity(ref) and name and card_name and _same_name(name, card_name):
            return _card_label(card)
    return ""


def _resolve_requisite_conflicts(
    sources: tuple[PlannedSource, ...],
    decisions: list[PlanDecision],
    cards: Sequence[NamedAct | Norm],
) -> tuple[tuple[PlannedSource, ...], list[PlanDecision], list[str]]:
    "Противоречивые реквизиты одного акта: явный статус и проверяемый переход, без угадывания."
    candidates = [d for d in decisions if d.outcome in {"accepted", "deferred"} and d.ref is not None]

    names = [_name_key(d.ref) for d in candidates]  # type: ignore[arg-type]
    links: dict[int, set[int]] = {i: set() for i in range(len(candidates))}
    for i, a in enumerate(candidates):
        for j in range(i + 1, len(candidates)):
            if _conflicting(a.ref, names[i], candidates[j].ref, names[j]):  # type: ignore[arg-type]
                links[i].add(j)
                links[j].add(i)
    groups: list[list[int]] = []
    seen: set[int] = set()
    for start, linked in links.items():
        if start in seen or not linked:
            continue
        stack, group = [start], []
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                group.append(node)
                stack.extend(links[node] - seen)
        groups.append(sorted(group))
    if not groups:
        return sources, decisions, []

    by_ref = {id(s.ref): s for s in sources}
    updated_sources = {id(s.ref): s for s in sources}
    replaced: dict[int, PlanDecision] = {}
    notes: list[str] = []
    for group in groups:
        members = [candidates[i] for i in group]
        cards_by_member = {id(m): _confirming_card(m.ref, cards) for m in members}  # type: ignore[arg-type]
        labels = [m.ref.label for m in members]  # type: ignore[union-attr]
        for member in members:
            ref = member.ref
            assert ref is not None
            others = tuple(o.ref.label for o in members if o is not member)  # type: ignore[union-attr]
            card = cards_by_member[id(member)]
            if member.outcome == "accepted":
                reason = (
                    f"requisites_confirmed_by_card:{card}; {member.reason}"
                    if card
                    else f"unconfirmed_requisites: conflicts with {', '.join(others)}; {member.reason}"
                )
                replaced[id(member)] = (member).model_copy(
                    update={"outcome": "accepted" if card else "unconfirmed", "reason": reason}
                )
                source = by_ref.get(id(ref))
                if source is not None:
                    updated_sources[id(ref)] = (source).model_copy(update={"requisites_conflict": others, "confirmed_by": card})
        confirmed = [f"{m.ref.label} ({cards_by_member[id(m)]})" for m in members if m.ref is not None and cards_by_member[id(m)]]
        notes.append(
            f"Противоречивые реквизиты одного акта «{' '.join((_name_text(members[0].ref) or '').split())}»: "  # type: ignore[arg-type]
            f"{'; '.join(labels)}. "
            + (
                f"Карточкой К+ подтверждены: {', '.join(confirmed)}; остальные варианты не приняты как подтверждённые "
                "и переданы на точную проверку реквизитов в L2."
                if confirmed
                else "Ни один вариант не подтверждён карточкой К+; все переданы на точную проверку реквизитов в L2, "
                "до неё ни один не считается подтверждённым источником."
            )
        )
    new_sources = tuple(updated_sources[id(s.ref)] for s in sources)
    return new_sources, [replaced.get(id(d), d) for d in decisions], notes


_CURRENTNESS_REASON = re.compile(
    r"утрат\w*\s+сил|не\s*действу|недейств|отмен[её]\w*|замен[её]\w*|"
    r"взамен|вместо|устар\w*|нов\w*\s+редакц|срок\w*\s+действ|"
    r"не\s+вступ\w*\s+в\s+сил|repeal|supersed|expir|no\s+longer\s+in\s+force",
    re.IGNORECASE,
)


RepealTarget = tuple[str, str, str, str]


def project_repeal_targets(parsed: ParsedDocument | None) -> frozenset[RepealTarget]:
    "Акты, которые проект признаёт утратившими силу (целиком или в части), — по структуре."
    tree = getattr(parsed, "tree", None)
    if tree is None:
        return frozenset()
    targets: set[RepealTarget] = set()
    for target, _clause, _text in collect_repeal_targets(tree):
        if target.date:
            issuer = _issuer_letters(target.raw, target.number)
            if issuer:
                targets.add((target.number, card_date(target.date), card_vid(target.kind), issuer))
    return frozenset(targets)


#: Однозначное сокращение слова «область» в заголовках карточек К+ («Ульяновской обл.»).
_REGION_WORD = {"обл.": "области", "обл": "области"}


def _issuer_letters(raw: str, number: str) -> str:
    "Издатель ссылки только буквами: номер страницы («Ульяновской 2 области») — шум текста."
    raw = " ".join(_REGION_WORD.get(token.casefold(), token) for token in (raw or "").split())
    reqs = [r for r in find_act_requisites(raw) if norm_act_num(r.number) == number]
    hint = act_authority_hint(reqs[0]) if reqs else ""
    return "".join(ch for ch in hint.removeprefix("authority:") if ch.isalpha()) if hint.startswith("authority:") else ""


def _in_repeal_clause(ref: SourceRef, targets: frozenset[RepealTarget]) -> bool:
    number = norm_act_num(ref.number)
    key = (number, card_date(ref.date), CARD_VID_BY_KIND.get(ref.kind, ""), _issuer_letters(ref.raw, number))
    return bool(number and key[3]) and key in targets


def project_issuer_key(organ: str, region: str) -> str:
    """Ключ издателя проекта по его шапке; регион дописывается, если в названии органа его нет."""
    if not organ.strip():
        return ""
    if region and region.casefold() not in organ.casefold():
        organ = f"{organ} {region}"
    return issuer_key(organ)


def _same_issuer(ref: SourceRef, project_issuer: str, document: str = "") -> bool:
    "Тот же явно названный орган, что издаёт проект: акт равной силы, не вышестоящий."
    if not project_issuer or ref.kind not in _NAMED_ISSUER_KINDS:
        return False
    if identity(ref)[4] == project_issuer:
        return True
    # Номер страницы внутри названия органа снимается только по вёрстке документа.
    return bool(document) and issuer_without_page_numbers(ref.raw, document) == project_issuer


def prepare_candidates(
    items: Sequence[Norm],
    document: str,
    *,
    project_issuer: str = "",
    region: str = "",
    repeal_targets: frozenset[RepealTarget] = frozenset(),
) -> tuple[list[Norm], list[PlannedSource], list[PlanDecision], list[str]]:
    """До LLM исключить явные несоответствия и выделить названные акты того же органа."""
    candidates: list[Norm] = []
    context: list[PlannedSource] = []
    decisions: list[PlanDecision] = []
    limitations: list[str] = []
    seen: dict[tuple[str, ...], Norm] = {}
    for ref in items:
        reason = _rejection_reason(ref, document, region=region)
        if reason:
            decisions.append(PlanDecision(outcome="dropped", reason=reason, ref=ref))
            limitations.append(settings["agents.legal.plan.default_rejection_text"].format(label=ref.label, reason=reason))
            continue
        key = identity(ref, region=region)
        if key in seen:
            limitations.extend(_duplicate_notes(seen[key], ref))
            decisions.append(PlanDecision(outcome="duplicate", reason="merged_identity", ref=ref))
            continue
        seen[key] = ref
        if _same_issuer(ref, project_issuer, document):
            if ref.origin is SourceOrigin.BODY and ref.kind == "regional_order" and not _in_repeal_clause(ref, repeal_targets):
                context.append(PlannedSource(ref=ref))
            else:
                reason = "not_higher_force_same_issuer"
                decisions.append(PlanDecision(outcome="dropped", reason=reason, ref=ref))
            continue
        candidates.append(ref)
    return candidates, context, decisions, limitations


def accept_selection(
    selection: Sequence[NamedActChoice | None],
    items: Sequence[Norm],
    *,
    context: Sequence[PlannedSource] = (),
    repeal_targets: frozenset[RepealTarget] = frozenset(),
) -> PlanOutcome:
    """Применить смысловой отбор и лимиты единого чтения к подготовленным кандидатам."""
    decisions: list[PlanDecision] = []
    limitations: list[str] = []
    sources: list[PlannedSource] = []
    context_sources: list[PlannedSource] = []
    dropped = 0
    for ref, choice in zip(items, selection, strict=True):
        model_reason = choice.reason if choice is not None else "selection_failed: резервная проверка найденного акта"
        # Актуальность устанавливает чтение: предположение модели не исключает акт.
        if (
            choice is not None
            and not choice.keep
            and (not _CURRENTNESS_REASON.search(choice.reason) or _in_repeal_clause(ref, repeal_targets))
        ):
            decisions.append(PlanDecision(outcome="excluded", reason=model_reason, ref=ref))
            continue

        if len(sources) >= int(settings["agents.legal.max_acts"]):
            reason = "acts_cap_reached"
            dropped += 1
            decisions.append(PlanDecision(outcome="deferred", reason=reason, ref=ref))
            limitations.append(f"Кандидат L1 отложен из-за лимита: {ref.label} ({reason}).")
            continue
        sources.append(PlannedSource(ref=ref))
        decisions.append(PlanDecision(outcome="accepted", reason=model_reason, ref=ref))

    for source in context:
        if len(sources) + len(context_sources) >= int(settings["agents.legal.max_acts"]):
            dropped += 1
            decisions.append(PlanDecision(outcome="deferred", reason="acts_cap_reached", ref=source.ref))
            limitations.append(f"Контекстный акт отложен из-за лимита: {source.ref.label} (acts_cap_reached).")
            continue
        context_sources.append(source)
        decisions.append(PlanDecision(outcome="context", reason="named_act_same_issuer", ref=source.ref))

    cards = [item for item in items if item.origin is SourceOrigin.SEARCH]
    resolved, decisions, conflict_notes = _resolve_requisite_conflicts(tuple(sources), decisions, cards)
    failed = sum(choice is None for choice in selection)
    return PlanOutcome(
        sources=resolved,
        context_sources=tuple(context_sources),
        source="parser_fallback" if failed and failed == len(selection) else "model_partial" if failed else "model",
        dropped=dropped,
        limitations=tuple(dict.fromkeys([*limitations, *conflict_notes])),
        decisions=tuple(decisions),
    )


def accept_source_selection(
    selection: SourceSelection | None,
    items: Sequence[Norm],
    document: str,
    *,
    context: Sequence[PlannedSource] = (),
    repeal_targets: frozenset[RepealTarget] = frozenset(),
) -> PlanOutcome:
    """Связать ID ответа с карточками, проверить цитаты, затем применить общий допуск."""
    if selection is None:
        fallback = accept_selection([None] * len(items), items, context=context, repeal_targets=repeal_targets)
        return fallback.model_copy(update={"source": "parser_fallback"})
    inventory = [*items, *(s.ref for s in context)]
    chosen: dict[int, str] = {}
    limitations: list[str] = []
    for selected in selection.selected:
        index = selected.candidate_id - 1
        if not 0 <= index < len(inventory):
            limitations.append(f"Отбор вернул неизвестный ID карточки: {selected.candidate_id}.")
            continue
        if index in chosen:
            log.warning("legal_selection_duplicate_id", candidate_id=selected.candidate_id)
            continue
        chosen[index] = selected.reason
        if not selection_quote_matches(selected.project_quote, document):
            log.warning("legal_selection_unverified_quote", candidate_id=selected.candidate_id)
    if selection.selected and not chosen:
        fallback = accept_selection([None] * len(items), items, context=context, repeal_targets=repeal_targets)
        return fallback.model_copy(update={"source": "parser_fallback", "limitations": (*fallback.limitations, *limitations)})
    choices = [
        NamedActChoice(keep=i in chosen, reason=chosen.get(i, "Не выбран совместным отбором обязательных требований."))
        for i in range(len(items))
    ]
    selected_context = [s for i, s in enumerate(context, len(items)) if i in chosen]
    result = accept_selection(choices, items, context=selected_context, repeal_targets=repeal_targets)
    excluded_context = [
        PlanDecision(outcome="excluded", reason="Контекст не нужен для понимания проверяемых положений.", ref=s.ref)
        for i, s in enumerate(context, len(items))
        if i not in chosen
    ]
    return result.model_copy(
        update={"limitations": (*result.limitations, *limitations), "decisions": (*result.decisions, *excluded_context)}
    )
