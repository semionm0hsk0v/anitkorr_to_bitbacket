"""Допуск и выбор поисковых кандидатов по реквизитам, банку и предмету запроса."""

from __future__ import annotations

import re
from collections.abc import Sequence

from src.agents.schemas.legal_base.act_refs import ActRequisites
from src.agents.schemas.legal_base.card_search import CardHit, CardQuery
from src.agents.steps_tools.sources.act_refs import act_title_head, non_normative_reason, parse_act_requisites
from src.agents.steps_tools.sources.normalization import (
    card_number_variants,
)
from src.agents.steps_tools.sources.region_banks import FEDERAL_BANK, bank_for_region, foreign_region, is_non_legislation_bank
from src.common.utils.logger import get_logger

log = get_logger(__name__)


def act_hygiene_reject(bank: str, title: str, *, region: str = "", kinds: Sequence[str] = ()) -> str:
    "Причина, по которой документ НЕ может быть вышестоящим актом (пусто — может)."
    if is_non_legislation_bank(bank):
        return f"банк {bank} — не законодательство (практика/разъяснения)"
    reason = non_normative_reason(title, kinds)
    if reason:
        return reason
    other = foreign_region(bank, act_title_head(title), region)
    if other:
        return f"акт другого субъекта РФ ({other}) — для проекта не вышестоящий"
    return ""


async def keep_fit_items(items: list, region: str) -> list:
    "Выдача свободного поиска → только пригодные кандидаты (остальные — в лог поимённо)."
    kept: list = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get("doc_par_label") or {}
        bank = str(label.get("bank_name") or "")
        reason = act_hygiene_reject(bank, str(item.get("title") or ""), region=region)
        if reason:
            await log.ainfo(
                "reference_act_rejected", bank=bank, title=str(item.get("title") or "")[:120], reason=reason, path="free"
            )
            continue
        kept.append(item)
    return kept


async def keep_fit_hits(hits: list[CardHit], region: str) -> list[CardHit]:
    "То же для карточной выдачи: реквизиты сужают поиск, но не гарантируют вид акта"
    kept: list[CardHit] = []
    for hit in hits:
        reason = act_hygiene_reject(hit.bank, hit.title, region=region)
        if reason:
            await log.ainfo("reference_act_rejected", bank=hit.bank, title=hit.title[:120], reason=reason, path="card")
            continue
        kept.append(hit)
    return kept


def query_tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"\w+", (s or "").lower().replace("ё", "е")) if len(t) >= 5}


_KIND_RE = re.compile(r"(Указ\w*|Федеральн\w+\s+закон\w*|постановлени\w+|приказ\w*|закон\w+)", re.IGNORECASE)


def fallback_queries(query: str) -> list[str]:
    "Упрощённые запросы, когда богатый запрос вернул ПУСТО (recall-резерв)."
    out: list[str] = []
    qm = re.search(r"[«\"]([^»\"]{6,150})[»\"]", query)
    if qm:
        out.append(qm.group(1).strip())
    # Номер АКТА (после «№» или из «N-ФЗ/ЗО»), а НЕ первое число (это была бы дата).
    nm = re.search(r"№\s*(\d+)", query) or re.search(r"(\d+)\s*-\s*(?:ФЗ|ЗО)", query, re.IGNORECASE)
    if nm:
        num = nm.group(1)
        km = _KIND_RE.search(query)
        mini = f"{km.group(0)} № {num}".strip() if km else ""
        if mini:
            out.append(mini)
        out.append(num)
    seen: set[str] = set()
    res: list[str] = []
    qn = query.strip().lower()
    for q in out:
        ql = q.strip().lower()
        if ql and ql != qn and ql not in seen:
            seen.add(ql)
            res.append(q.strip())
    return res[:2]


def expected_number(query: str) -> str | None:
    """Официальный номер акта из запроса (после «№» или из «N-ФЗ/ЗО») — для сверки выдачи."""
    m = re.search(r"№\s*(\d+)", query) or re.search(r"(\d+)\s*-\s*(?:ФЗ|ЗО)", query, re.IGNORECASE)
    return m.group(1) if m else None


def items_have_number(items: list, num: str | None, limit: int) -> bool:
    "Есть ли среди топ-выдачи акт с нужным НОМЕРОМ (токен на границе нецифры, в названии)."
    if not num:
        return True
    titles = [(it.get("title") or "") for it in items[:limit] if isinstance(it, dict)]
    if not any(titles):
        return True  # нечем сверять — recall-safe: не отбрасываем
    pat = re.compile(rf"(?<!\d){re.escape(num)}(?!\d)")
    return any(pat.search(t) for t in titles)


def candidate_score(item: dict, qtok: set[str]) -> int:
    "Релевантность кандидата запросу-КОНЦЕПТУ (семантический путь, top-K)."
    lbl = item.get("doc_par_label") or {}
    title = (item.get("title") or "").lower().replace("ё", "е")
    score = len({t for t in re.findall(r"\w+", title) if len(t) >= 5} & qtok)
    if lbl.get("bank_name") == "LAW":  # федеральный консолидированный банк (не региональный/вопросы)
        score += 2
    if "правительства российской федерации" in title:
        score += 2
    return score


_TITLE_DATE_RE = re.compile(r"от\s+(\d{2}\.\d{2}\.\d{4})")


def act_banks(req: ActRequisites, region: str = "") -> tuple[str, ...]:
    "Банки для поиска акта: федеральный — ``LAW``, региональный — банк субъекта."
    if req.federal:
        return (FEDERAL_BANK,)
    bank = bank_for_region(req.region or region)
    return (bank,) if bank else ()


def card_queries(req: ActRequisites, banks: tuple[str, ...], *, limit: int = 20) -> list[CardQuery]:
    "Реквизиты → лесенка карточных запросов от самого точного к самому широкому."
    out: list[CardQuery] = []
    for number in card_number_variants(req.number):
        for vid in [req.kind, ""] if req.kind else [""]:
            for date in [req.card_date, ""] if req.card_date else [""]:
                q = CardQuery(number=number, vid_doc=vid, date=date, banks=banks, limit=limit)
                if q not in out:
                    out.append(q)
    return out


def hit_date(title: str) -> str:
    m = _TITLE_DATE_RE.search(title or "")
    return m.group(1) if m else ""


def _sortable_date(title: str) -> str:
    """Дата акта в сортируемом виде «ГГГГММДД». Сравнивать «ДД.ММ.ГГГГ» строкой нельзя:
    «22.11.2023» > «19.06.2026» лексикографически — «свежайший» выбирался бы наоборот."""
    d = hit_date(title)
    return f"{d[6:10]}{d[3:5]}{d[0:2]}" if d else ""


_STOP_STEMS = frozenset(
    w[:6]
    for w in (
        "приказ",
        "приказом",
        "постановление",
        "постановлением",
        "распоряжение",
        "указ",
        "закон",
        "законом",
        "область",
        "области",
        "областной",
        "край",
        "края",
        "краевой",
        "республика",
        "республики",
        "россии",
        "российской",
        "федерации",
        "федеральный",
        "министерство",
        "министерства",
        "утверждении",
        "внесении",
        "изменений",
        "административного",
        "регламента",
        "предоставления",
        "государственной",
        "услуги",
    )
)


def _hit_score(hit: CardHit, req: ActRequisites) -> tuple[int, str]:
    "Насколько хит похож на искомый акт: (совпадение органа/названия, дата акта)."

    def stems(text: str) -> set[str]:
        low = (text or "").lower().replace("ё", "е")
        return {w[:6] for w in re.findall(r"[а-яa-z]+", low) if len(w) >= 5} - _STOP_STEMS

    overlap = len(stems(hit.title) & stems(f"{req.raw} {req.title}"))
    return overlap, _sortable_date(hit.title)


def hit_matches_requisites(hit: CardHit, req: ActRequisites, banks: tuple[str, ...]) -> bool:
    """Provider response must itself confirm the requested number, kind, and bank."""
    if banks and hit.bank not in banks:
        return False
    found = parse_act_requisites(hit.title)
    if found is None or found.card_number not in card_number_variants(req.number):
        return False
    return not (req.kind and found.kind and req.kind != found.kind)


def pick_hit(hits: list[CardHit], req: ActRequisites) -> CardHit | None:
    "Лучший кандидат карточной выдачи или ``None``, если уверенности нет."
    if not hits:
        return None
    if req.date:
        exact = [h for h in hits if hit_date(h.title) == req.date]
        if exact:
            return exact[0]
    ranked = sorted(hits, key=lambda h: _hit_score(h, req), reverse=True)
    best = ranked[0]
    overlap, _ = _hit_score(best, req)
    if overlap >= 2:
        return best
    if len(hits) == 1 and not req.date:
        return best  # единственный акт с таким номером и видом — двусмысленности нет
    return None
