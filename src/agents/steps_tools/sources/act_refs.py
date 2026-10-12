"""Распознавание реквизитов, вида и нормативного характера актов."""

from __future__ import annotations

import re
from collections.abc import Sequence

from src.agents.schemas.legal_base.act_refs import ActRequisites
from src.agents.schemas.legal_base.source_refs import ActKind
from src.agents.steps_tools.sources.normalization import (
    _FEDERAL_RE,
    act_authority_hint,
    card_number_identity,
    card_vid,
    normal_identity,
)
from src.common.config.config import settings

_ACT_NUMBER_DIGIT_SUFFIX = r"[1-9](?![0-9A-Za-zА-Яа-яЁё])"


_ACT_NUMBER_FRACTION = r"(?:\s*/\s*\d{1,5})?"
_ACT_NUMBER_TOKEN = (
    r"\d{1,5}"
    + _ACT_NUMBER_FRACTION
    + r"(?:\s*-\s*(?:[А-Яа-яЁёA-Za-z]{1,4}|"
    + _ACT_NUMBER_DIGIT_SUFFIX
    + r")|[А-Яа-яЁёA-Za-z]{1,2})?"
)


ACT_PATTERNS: tuple[str, ...] = (
    r"\b\d{1,4}\s*-?\s*ФЗ\b",  # 79-ФЗ, 210ФЗ, 414 ФЗ
    r"\b\d{1,4}\s*-?\s*ЗО\b",  # 136-ЗО (закон области)
    rf"Указ[а-яё]*\s+Губернатора[^№]{{0,80}}?№\s*{_ACT_NUMBER_TOKEN}",
    rf"постановлени[а-яё]*\s+Правительств[а-яё]*[^№]{{0,80}}?№\s*{_ACT_NUMBER_TOKEN}",
    rf"\bПП\s*РФ\s*№?\s*{_ACT_NUMBER_TOKEN}",
    rf"Закон[а-яё]*\s+(?:РФ\b|РСФСР\b|Росси[йи][а-яё]*\s+Федерации)[^№]{{0,60}}?№\s*{_ACT_NUMBER_TOKEN}",
)


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def canon_act(s: str) -> str:
    "Канонизировать ссылку на акт → дедуп разных написаний одного акта."
    m = re.search(r"(\d{1,4})\s*-?\s*(ФЗ|ЗО)\b", s, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)}-{m.group(2).upper()}"
    num = re.search(
        r"№\s*(\d+(?:\s*/\s*\d+)?(?:\s*-\s*(?:[а-яёa-z]{1,4}|" + _ACT_NUMBER_DIGIT_SUFFIX + r")|[а-яёa-z]{1,2})?)",
        s,
        flags=re.IGNORECASE,
    )
    n = re.sub(r"\s*/\s*", "/", re.sub(r"\s*-\s*", "-", num.group(1))) if num else "?"
    low = _norm_ws(s).lower()
    if "губернатор" in low:
        return f"Указ Губернатора № {n}"
    if "правительств" in low or re.search(r"\bпп\s*рф", low):
        federal = bool(re.search(r"\bрф\b", low)) or "российской федерации" in low
        return f"ПП РФ № {n}" if federal else f"ПП (рег.) № {n}"

    if "закон" in low and _FEDERAL_RE.search(low) and num:
        return f"Закон РФ № {card_number_identity(n)}"
    return _norm_ws(s)


def referenced_acts(*texts: str) -> list[str]:
    "Вышестоящие акты, названные в переданных текстах (канонизированные, дедуп)."
    hay = " ".join(t for t in texts if t)
    hits: list[tuple[int, str]] = []
    for pat in ACT_PATTERNS:
        for m in re.finditer(pat, hay, flags=re.IGNORECASE):
            if canon := canon_act(m.group(0)):
                hits.append((m.start(), canon))
    out: list[str] = []
    for _, canon in sorted(hits, key=lambda hit: hit[0]):
        if canon not in out:
            out.append(canon)
    return out


#: Номер акта: «83-п», «298-П», «210-ФЗ», «1024н», «835», «56-ЗАО».
_NUMBER_RE = _ACT_NUMBER_TOKEN

_DATE_RE = r"\d{1,2}\.\d{1,2}\.\d{4}"

MONTH_GENITIVE: dict[str, str] = {
    "января": "01",
    "февраля": "02",
    "марта": "03",
    "апреля": "04",
    "мая": "05",
    "июня": "06",
    "июля": "07",
    "августа": "08",
    "сентября": "09",
    "октября": "10",
    "ноября": "11",
    "декабря": "12",
}


_WORD_DATE_RE = r"\d{1,2}\s+(?:" + "|".join(MONTH_GENITIVE) + r")\s+\d{4}"

_ANY_DATE_RE = rf"(?:{_DATE_RE}|{_WORD_DATE_RE})"

_QUOTE_REQUISITES_RE = re.compile(
    rf"(?:\bот\s+(?P<date>{_ANY_DATE_RE})\s*(?:года|году|г\.?)?\s*)?[№N]\s*(?P<num>{_NUMBER_RE})",
    re.IGNORECASE,
)


def quote_requisites(quote: str) -> tuple[str, str] | None:
    """Номер и дата единственного акта в цитате; неоднозначную ссылку не принимать."""
    matches = list(_QUOTE_REQUISITES_RE.finditer(_norm_ws(quote)))
    if len(matches) != 1:
        return None
    match = matches[0]
    return match["num"], _normalize_ref_date(match["date"] or "")


def _normalize_ref_date(raw: str) -> str:
    """«30 августа 2024» → «30.08.2024»; цифровая дата возвращается как есть (Ф4)."""
    word = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", _norm_ws(raw), flags=re.IGNORECASE)
    if not word:
        return raw
    month = MONTH_GENITIVE.get(word.group(2).lower())
    return f"{int(word.group(1)):02d}.{month}.{word.group(3)}" if month else raw


_GLUED_OT_BEFORE_DATE_RE = re.compile(r"([а-яё])от(?=\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{4})", re.IGNORECASE)


def _deglue_ot_before_date(text: str) -> str:
    text = _GLUED_KIND_ISSUER_RE.sub(r"\1 ", text)
    return _GLUED_OT_BEFORE_DATE_RE.sub(r"\1 от", text)


_ACT_KIND_FORMS = (
    r"(?:приказ(?:а|у|ом|е|ы|ов|ам|ами|ах)?"
    r"|(?:постановлени|распоряжени)(?:е|я|ю|ем|и|й|ям|ями|ях)"
    r"|указ(?:а|у|ом|е|ы|ов|ам|ами|ах)?"
    r"|(?:федеральн(?:ый|ого|ому|ым|ом|ые|ых|ыми)\s*)?закон(?:а|у|ом|е|ы|ов|ам|ами|ах)?)"
)
_ACT_KIND_TOKEN = rf"\b{_ACT_KIND_FORMS}\b"


_GLUED_KIND_ISSUER_RE = re.compile(
    rf"(\b{_ACT_KIND_FORMS})(?=Правительств|Министерств|Минздрав|Минтруд|Губернатор|Президент|Администрац|Российск)",
    re.IGNORECASE,
)


_REF_MID = rf"(?P<mid>(?:(?!{_ACT_KIND_TOKEN}|[«»\"№]).){{0,160}}?)"


_REF_RE = re.compile(
    rf"(?P<kind>{_ACT_KIND_TOKEN})" + _REF_MID + r"(?:\s+от\s+(?P<date>" + _ANY_DATE_RE + r")\s*(?:г\.?)?)?"
    r"\s*[№N]\s*(?P<num>" + _NUMBER_RE + r")",
    re.IGNORECASE | re.DOTALL,
)


_SUFFIX_REF_RE = re.compile(
    rf"(?P<kind>{_ACT_KIND_TOKEN})" + _REF_MID + r"(?:\s+от\s+(?P<date>" + _ANY_DATE_RE + r")\s*(?:г\.?)?)?"
    r"\s*(?:[№N]\s*)?(?P<num>\d{1,4}\s*-\s*[А-Яа-яЁёA-Za-z]{1,4})(?![А-Яа-яЁёA-Za-z])",
    re.IGNORECASE | re.DOTALL,
)
#: Название акта в кавычках сразу после номера — материал для поля COMMON_DOCNAME.
_TITLE_AFTER_RE = re.compile(r"^\s*[«\"]([^»\"]{8,300})[»\"]")


_TITLE_BEFORE_REF_RE = re.compile(
    rf"(?P<kind>{_ACT_KIND_TOKEN})"
    + _REF_MID
    + r'(?P<title_before>«[^«»]{8,300}»|"[^"\n]{8,300}")'
    + r"\s+от\s+(?P<date>"
    + _ANY_DATE_RE
    + r")\s*(?:г\.?)?\s*[№N]\s*(?P<num>"
    + _NUMBER_RE
    + r")",
    re.IGNORECASE | re.DOTALL,
)

_REF_REGION_RE = re.compile(r"([А-ЯЁ][а-яё]+?(?:-[А-ЯЁ][а-яё]+)?)\s*(области|обл\.|края|автономного округа)")


def _build(match: re.Match, text: str) -> ActRequisites | None:
    num = " ".join((match.group("num") or "").split())
    if not num:
        return None
    kind_word = match.group("kind") or ""
    mid = match.group("mid") or ""
    head = f"{kind_word} {mid}"
    reg = _REF_REGION_RE.search(head)
    title_m = _TITLE_AFTER_RE.match(text[match.end() :])
    title_before = match.groupdict().get("title_before") or ""
    # Уровень: «РФ»/«Российской Федерации»/«федеральный» ИЛИ суффикс «-ФЗ» → федеральный.
    federal = bool(_FEDERAL_RE.search(head)) or num.upper().replace(" ", "").endswith("-ФЗ")
    return ActRequisites(
        kind=card_vid(kind_word),
        number=num,
        date=_normalize_ref_date(match.group("date") or ""),
        region=f"{reg.group(1)} {reg.group(2)}" if reg and not federal else "",
        federal=federal,
        title=title_before[1:-1].strip() if title_before else title_m.group(1).strip() if title_m else "",
        raw=" ".join(match.group(0).split()),
    )


def find_act_requisites(text: str) -> list[ActRequisites]:
    "Все ссылки на акты → реквизиты с recall-safe issuer/jurisdiction dedup."
    text = _deglue_ot_before_date(text or "")
    out: list[ActRequisites] = []
    seen: set[tuple[str, str, str, bool, str, str]] = set()
    matches = [m for pattern in (_TITLE_BEFORE_REF_RE, _REF_RE, _SUFFIX_REF_RE) for m in pattern.finditer(text)]
    for m in sorted(matches, key=lambda match: (match.start(), -match.end())):
        req = _build(m, text)
        if req is None:
            continue
        key = (
            req.kind,
            req.identity_number,
            req.card_date,
            req.federal,
            normal_identity(req.region),
            act_authority_hint(req),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(req)
    return out


_PAGE_NUMBER_MAX_DIGITS = int(settings["agents.legal.act_refs.page_number_max_digits"])
#: Документы нумеруют со второй страницы: первая страница номера не несёт.
_FIRST_NUMBERED_PAGE = int(settings["agents.legal.act_refs.first_numbered_page"])


def page_number_lines(text: str) -> frozenset[int]:
    "Индексы строк-колонтитулов: короткое число одно на строке в сквозной нумерации страниц."
    standalone = [
        (i, int(line.strip()))
        for i, line in enumerate(text.split("\n"))
        if line.strip().isdigit() and len(line.strip()) <= _PAGE_NUMBER_MAX_DIGITS
    ]
    numeric = [(i, value) for i, value in standalone if value <= len(standalone) + 1]
    in_run = {
        k
        for k in range(len(numeric))
        if (k and numeric[k - 1][1] == numeric[k][1] - 1) or (k + 1 < len(numeric) and numeric[k + 1][1] == numeric[k][1] + 1)
    }
    return frozenset(i for k, (i, value) in enumerate(numeric) if k in in_run or (value == _FIRST_NUMBERED_PAGE and in_run))


def issuer_without_page_numbers(raw: str, document: str) -> str:
    "Ключ издателя ссылки без колонтитула, разорвавшего её текст в документе; иначе пусто."
    tokens = raw.split()
    inside = [k for k, token in enumerate(tokens[1:-1], 1) if token.isdigit() and len(token) <= _PAGE_NUMBER_MAX_DIGITS]
    if not inside:
        return ""
    text = _deglue_ot_before_date(document or "")
    pages = page_number_lines(text)
    if not pages:
        return ""
    placed = [(token, i) for i, line in enumerate(text.split("\n")) for token in line.split()]
    words = [token for token, _ in placed]
    for start in (s for s, word in enumerate(words) if word.endswith(tokens[0])):
        found = words[start : start + len(tokens)]
        # Крайние токены ``raw`` могут быть обрезаны по знакам препинания текста («45-п.»).
        if len(found) != len(tokens) or found[1:-1] != tokens[1:-1] or not found[-1].startswith(tokens[-1]):
            continue
        drop = {k for k in inside if placed[start + k][1] in pages}
        if drop:
            refs = find_act_requisites(" ".join(t for k, t in enumerate(tokens) if k not in drop))
            return act_authority_hint(refs[0]) if refs else ""
    return ""


def parse_act_requisites(text: str) -> ActRequisites | None:
    "Реквизиты ПЕРВОЙ ссылки в тексте (самая полная из совпавших) или ``None``."
    found = find_act_requisites(text)
    if not found:
        return None
    with_date = [r for r in found if r.date]
    return with_date[0] if with_date else found[0]


#: Обрыв головы заголовка: дата принятия, номер, кавычка названия, перевод строки.
_TITLE_HEAD_SPLIT = re.compile(r"\s+от\s+\d{1,2}\.\d{1,2}\.\d{4}|\s+[NN№]\s|[«\"]|\n")


_COURT_RE = re.compile(
    r"\bсуд(?:а|е|у|ом|ы|ов|ах|ебн\w*)?\b|\bВАС\b|\bВС\s*РФ\b|\bКС\s*РФ\b|\bУФАС\b",
    re.IGNORECASE,
)


_NON_NORMATIVE_PREFIXES: tuple[str, ...] = (
    "апелляционное определение",
    "кассационное определение",
    "надзорное определение",
    "определение",
    "приговор",
    "обзор",
    "вопрос",
    "справочная информация",
    "информационное письмо",
    "письмо",
    "информация",
    "разъяснени",
    "заключение",
    "пояснительная записка",
    "проект",
    "статья:",
    "интервью",
    "консультация",
    "типовая ситуация",
    "готовое решение",
    "путеводитель",
    "форма:",
    "ситуация:",
)


NON_NORMATIVE_KINDS: frozenset[str] = frozenset(
    {
        "Определение",
        "Приговор",
        "Обзор",
        "Вопрос-ответ",
        "Разъяснение",
        "Справочная информация",
        "Информация",
        "Письмо",
        "Пояснительная записка",
        "Заключение",
        "Проект",
        "Статья",
        "Консультация",
        "Интервью",
    }
)


def act_title_head(title: str) -> str:
    "Голова заголовка К+ — вид документа и орган, до даты/номера/названия в кавычках."
    text = " ".join((title or "").replace("<", " ").replace(">", " ").split()).lstrip("«\"' ")
    return _TITLE_HEAD_SPLIT.split(text, maxsplit=1)[0].strip()[:200]


def _quoted_title(title: str) -> bool:
    'Начинается ли заголовок К+ прямо с названия документа в кавычках («"ГОСТ Р …"»,'
    return (title or "").lstrip().replace("<", "").lstrip()[:1] in ("«", '"')


def non_normative_reason(title: str, kinds: Sequence[str] = ()) -> str:
    "Почему документ НЕ является НПА (пусто — является/не установлено)."
    if kinds and str(kinds[0]).strip() in NON_NORMATIVE_KINDS:
        return f"вид документа «{str(kinds[0]).strip()}» — не НПА"
    head = act_title_head(title)
    if not head:
        return ""
    low = head.lower().replace("ё", "е")

    if not _quoted_title(title) and _COURT_RE.search(head):
        return f"судебный/квазисудебный акт («{head[:80]}»)"
    for prefix in _NON_NORMATIVE_PREFIXES:
        if low.startswith(prefix):
            return f"не НПА, вид «{prefix}» («{head[:80]}»)"
    return ""


def kind_from_requisites(req: ActRequisites) -> ActKind:
    vid = (req.kind or "").casefold()
    number = (req.number or "").upper().replace(" ", "")

    if number.endswith("ЗО"):
        return "regional_law"
    if number.endswith("ФЗ"):
        return "federal_law"
    if vid == "закон":
        if not req.federal:
            return "regional_law"
        return "rf_law"
    if vid == "постановление":
        return "rf_government_resolution" if req.federal else "regional_government_resolution"
    if vid == "указ":
        if "губернатор" in (req.raw or "").casefold():
            return "governor_decree"
        return "president_decree" if req.federal else "other"
    if vid == "приказ":
        return "federal_order" if req.federal else "regional_order"
    if vid == "распоряжение":
        return "rf_directive" if req.federal else "regional_directive"
    return "other"
