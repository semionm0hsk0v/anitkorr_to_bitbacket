from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.schemas.legal_base.act_refs import ActRequisites


_VID_BY_STEM: tuple[tuple[str, str], ...] = (
    ("приказ", "Приказ"),
    ("постановлени", "Постановление"),
    ("распоряжени", "Распоряжение"),
    ("указ", "Указ"),
    ("закон", "Закон"),
)


_FEDERAL_RE = re.compile(r"\bРФ\b|Росси[йи]\w*\s+Федерации|\bРоссии\b|федеральн|\bРСФСР\b", re.IGNORECASE)


_CARD_NUMBER_RE = re.compile(
    r"(?P<digits>\d{1,5})"
    r"(?:\s*/\s*(?P<frac>\d{1,5}))?"
    r"(?:\s*-\s*(?P<digit>[1-9])(?![0-9A-Za-zА-Яа-яЁё])"
    r"|\s*(?P<dash>-?)\s*(?P<letters>[А-Яа-яЁёA-Za-z]{1,4}))?"
)


#: Тот же разбор, но для УЖЕ нормализованного вывода ``card_number``.
_CARD_NUMBER_PARTS_RE = re.compile(r"(?P<digits>\d{1,5})(?:/(?P<frac>\d{1,5}))?(?P<sep>-?)(?P<suffix>[А-ЯЁA-Z]{1,4}|[1-9])?")


_ARABIC_BY_ROMAN: dict[str, str] = {
    "I": "1",
    "II": "2",
    "III": "3",
    "IV": "4",
    "V": "5",
    "VI": "6",
    "VII": "7",
    "VIII": "8",
    "IX": "9",
}


_ROMAN_BY_ARABIC: dict[str, str] = {arabic: roman for roman, arabic in _ARABIC_BY_ROMAN.items()}


def card_number(number: str) -> str:
    "Номер акта → написание, которое ХРАНИТ карточка К+: «83-п» → «83-П», «1024н» →"
    m = _CARD_NUMBER_RE.search((number or "").strip())
    if not m:
        return ""
    digits = m.group("digits")
    base = f"{digits}/{m.group('frac')}" if m.group("frac") else digits
    if digit_suffix := m.group("digit"):
        return f"{base}-{digit_suffix}"
    letters = m.group("letters")
    if not letters:
        return base
    return f"{base}{m.group('dash') or ''}{letters.upper()}"


def _number_alias_forms(primary: str) -> list[str]:
    "Написания ОДНОГО номера: латинский римский суффикс ↔ его арабская запись."
    parts = _CARD_NUMBER_PARTS_RE.fullmatch(primary)
    if parts is None:
        return [primary]
    suffix = parts.group("suffix") or ""
    alias = _ARABIC_BY_ROMAN.get(suffix) or _ROMAN_BY_ARABIC.get(suffix)
    if alias is None:
        return [primary]
    base = f"{parts.group('digits')}/{parts.group('frac')}" if parts.group("frac") else parts.group("digits")
    return [primary, f"{base}-{alias}"]


def _separator_forms(form: str) -> list[str]:
    "Написания номера со сменой разделителя — только для БУКВЕННОГО суффикса."
    parts = _CARD_NUMBER_PARTS_RE.fullmatch(form)
    suffix = (parts.group("suffix") or "") if parts is not None else ""
    if not suffix or suffix.isdigit():
        return [form]
    alt = form.replace("-", "") if "-" in form else re.sub(r"(\d)([А-ЯЁA-Z])", r"\1-\2", form)
    return [form] if alt == form else [form, alt]


def card_number_variants(number: str) -> list[str]:
    "Написания номера для перебора: как в источнике, затем эквивалентные ему."
    primary = card_number(number)
    if not primary:
        return []
    out: list[str] = []
    for form in _number_alias_forms(primary):
        for variant in _separator_forms(form):
            if variant not in out:
                out.append(variant)
    return out


def card_number_identity(number: str) -> str:
    "Номер → КЛЮЧ ТОЖДЕСТВА акта: «1244-I» и «1244-1» → «1244-1», «1024н» → «1024-н»."
    primary = card_number(number)
    if not primary:
        return ""
    parts = _CARD_NUMBER_PARTS_RE.fullmatch(primary)
    if parts is None:
        return primary.casefold().replace("ё", "е")
    base = f"{parts.group('digits')}/{parts.group('frac')}" if parts.group("frac") else parts.group("digits")
    suffix = parts.group("suffix") or ""
    if not suffix:
        return base
    canonical = _ARABIC_BY_ROMAN.get(suffix, suffix).casefold().replace("ё", "е")
    return f"{base}-{canonical}"


def card_vid(kind_word: str) -> str:
    "Слово-вид из текста → значение COMMON_VID_DOC («приказом» → «Приказ»)."
    low = (kind_word or "").lower().replace("ё", "е")
    for stem, vid in _VID_BY_STEM:
        if stem in low:
            return vid
    return ""


def card_date(value: str) -> str:
    """``1.04.2024`` → ``01.04.2024``; уже корректная дата и мусор возвращаются как есть."""
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", value or "")
    if not m:
        return (value or "").strip()
    day, month, year = m.groups()
    return f"{int(day):02d}.{int(month):02d}.{year}"


def normal_identity(value: str) -> str:
    return re.sub(r"[\s\-]+", "", (value or "").casefold().replace("ё", "е"))


_FEDERAL_JURISDICTION_RE = re.compile(r"российск[а-яё]*\s+федераци[а-яё]*|росси[а-яё]*|\bрф\b", re.IGNORECASE)


_MINISTRY_WORD_RE = re.compile(r"\bминистерств[а-яё]*\b", re.IGNORECASE)


_MINISTRY_ABBREVIATION_RE = re.compile(r"^мин(?=[а-яё]{3,})", re.IGNORECASE)


def _authority_stem(authority: str) -> str:
    "Полное и сокращённое имя одного ведомства → общий стебель; разные ведомства не трогаем."
    text = _FEDERAL_JURISDICTION_RE.sub(" ", authority)
    text = _MINISTRY_WORD_RE.sub(" ", text)
    stem = normal_identity(text)
    return _MINISTRY_ABBREVIATION_RE.sub("", stem)


def issuer_key(organ: str) -> str:
    "Название органа (шапка проекта) → тот же ключ издателя, что ``act_authority_hint``."
    stem = _authority_stem((organ or "").casefold().replace("ё", "е"))
    return f"authority:{stem}" if stem else ""


def act_authority_hint(req: ActRequisites) -> str:
    "Conservative issuer identity from explicit requisites text."

    raw = re.split(r'[«"]', (req.raw or "").casefold().replace("ё", "е"), maxsplit=1)[0].rstrip()
    for needle, value in (
        ("губернатор", "governor"),
        ("правительств", "government"),
        ("президент", "president"),
    ):
        if needle in raw:
            return value
    if normal_identity(req.kind) == "закон" and "федеральн" in raw:
        return "federal"

    named_authority = re.search(
        r"^\s*(?:приказ|постановлени|распоряжени|указ|закон)[а-я]*\s+(?!от\s+\d)(.+?)"
        r"(?=\s+от\s+\d{1,2}\.\d{1,2}\.\d{4}|\s*[№n]|$)",
        raw,
    )
    if named_authority:
        if normal_identity(req.kind) == "закон" and _FEDERAL_RE.search(named_authority.group(1)):
            return "federal"
        if authority := _authority_stem(named_authority.group(1)):
            return f"authority:{authority}"
    return ""


_WHITESPACE_RE = re.compile(r"\s+")


_EDITION_SUFFIX_RE = re.compile(r"\s*\(\s*ред\.?(?:акция)?\s*от\s*[^)]*\)\s*$", re.IGNORECASE)


_DASHES_RE = re.compile(r"[‐‑‒–—―]")


_PUNCTUATION_SPACE_RE = re.compile(r"\s*([№/.-])\s*")


def canonicalize_reference_title(value: str) -> str:
    """Normalize a title into a comparison identity, excluding edition suffixes."""
    normalized = _DASHES_RE.sub("-", str(value).replace("Ё", "Е").replace("ё", "е"))
    normalized = _EDITION_SUFFIX_RE.sub("", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip().casefold()


def canonicalize_reference_requisite(value: str) -> str:
    """Normalize a non-date requisite without inferring missing legal fields."""
    normalized = _DASHES_RE.sub("-", str(value).replace("Ё", "Е").replace("ё", "е"))
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip().casefold()
    return _PUNCTUATION_SPACE_RE.sub(r"\1", normalized)


def canonicalize_reference_date(value: str | None) -> str | None:
    """Return a canonical ISO date; unknown dates stay unknown, malformed dates fail."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError("reference date must be YYYY-MM-DD or DD.MM.YYYY")
