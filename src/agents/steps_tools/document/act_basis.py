"""Основания проекта из преамбулы и резервное извлечение из пояснительной записки."""

from __future__ import annotations

import re

from src.agents.schemas.parse.parsed_document import ActBasis, ActBasisRef
from src.agents.steps_tools.sources.act_refs import canon_act, find_act_requisites, referenced_acts
from src.common.config.config import settings

_ENACT_VERBS = (
    "приказываю",
    "приказывает",
    "постановляю",
    "постановляет",
    "решаю",
    "решает",
    "решил",
    "повелеваю",
)


def _spaced(word: str) -> str:
    """Паттерн слова, терпимый к разрядке: «приказываю» и «п р и к а з ы в а ю»."""
    return r"\s?".join(re.escape(c) for c in word)


_ENACT = re.compile("(" + "|".join(_spaced(v) for v in _ENACT_VERBS) + ")", re.IGNORECASE)
#: Окно символов до формулы, где живёт основание («В соответствии с…»).
_PREAMBLE_WINDOW = int(settings["agents.parse.act_basis.preamble_window"])


_PREAMBLE_TAIL = int(settings["agents.parse.act_basis.preamble_tail"])


_PREAMBLE_ANCHOR_RE = re.compile(r"В\s+соответствии|В\s+целях|На\s+основании", re.IGNORECASE)


def _act_number(canon: str) -> str | None:
    """Голый номер акта для матча с находкой («67», «835», «79»)."""
    m = re.search(r"\d+", canon)
    return m.group(0) if m else None


def _preamble_text(window: str) -> str:
    "Хвост окна преамбулы — с начала фразы основания, а не с середины слова (Ф8)."
    stripped = window.strip()
    tail = stripped[-_PREAMBLE_TAIL:]
    if len(tail) == len(stripped):
        return tail
    if anchor := _PREAMBLE_ANCHOR_RE.search(tail):
        return tail[anchor.start() :]
    newline = tail.find("\n")
    return tail[newline + 1 :].lstrip() if newline != -1 else tail


def _basis_dates(window: str) -> dict[str, str]:
    "Дата ссылки по её канону (Ф4) — переиспользует существующий разбор реквизитов"
    by_canon: dict[str, set[str]] = {}
    for req in find_act_requisites(window):
        if not req.date:
            continue
        by_canon.setdefault(canon_act(req.raw), set()).add(req.date)
    return {canon: next(iter(dates)) for canon, dates in by_canon.items() if len(dates) == 1}


def enacting_formula_end(text: str) -> int | None:
    """Позиция сразу после формулы издания («приказываю», «п р и к а з ы в а ю») или ``None``."""
    m = _ENACT.search(text or "")
    return m.end() if m else None


def extract_act_basis(text: str) -> ActBasis | None:
    "Преамбула → ``ActBasis`` (формула + акты-основания). ``None``, если формулы нет."
    m = _ENACT.search(text or "")
    if not m:
        return None
    window = text[max(0, m.start() - _PREAMBLE_WINDOW) : m.start()]
    dates = _basis_dates(window)
    refs = [ActBasisRef(raw=a, number=_act_number(a), date=dates.get(a), role="basis") for a in referenced_acts(window)]
    return ActBasis(
        enacting_formula=re.sub(r"\s+", "", m.group(1)),  # «п р и к а з ы в а ю» → «приказываю»
        preamble_text=_preamble_text(window),
        basis_refs=refs,
    )


_NOTE_HEAD_CHARS = int(settings["agents.parse.context_basis.note_head_chars"])
_NOTE_BASIS_LIMIT = int(settings["agents.parse.context_basis.note_basis_limit"])


def basis_from_context(context_documents: list | None, existing: ActBasis | None) -> ActBasis | None:
    "Акт-основание из ПОЯСНИТЕЛЬНОЙ записки — только когда проект его не назвал."
    if existing and existing.basis_refs:
        return existing
    heads = [(d.get("text") or "")[:_NOTE_HEAD_CHARS] for d in (context_documents or []) if isinstance(d, dict)]
    if not heads:
        return existing
    acts = referenced_acts(*heads)[:_NOTE_BASIS_LIMIT]
    if not acts:
        return existing
    refs = [ActBasisRef(raw=a, number=(m.group(0) if (m := re.search(r"\d+", a)) else None), role="basis") for a in acts]
    return ActBasis(
        enacting_formula=existing.enacting_formula if existing else "",
        preamble_text=existing.preamble_text if existing else "",
        basis_refs=refs,
    )
