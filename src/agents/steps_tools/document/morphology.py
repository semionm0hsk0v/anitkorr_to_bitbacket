from __future__ import annotations

import re

from src.common.config.config import settings


def normalized_text(value: str | None) -> str:
    "Регистр, «ё» и пробелы приведены к единому виду для сравнения; состав слов сохранён."
    return " ".join((value or "").casefold().replace("ё", "е").split())


WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")

LETTERS = re.compile(r"[^\W\d_]+")

ALNUM = re.compile(r"[^\W_]+")

ALNUM_LOWER = re.compile(r"[a-zа-яё0-9]+")


_ENDINGS: tuple[str, ...] = (
    "иями",
    "ями",
    "ами",
    "ого",
    "ему",
    "ому",
    "ах",
    "ях",
    "ой",
    "ей",
    "ый",
    "ий",
    "ая",
    "яя",
    "ое",
    "ее",
    "ов",
    "ев",
    "ам",
    "ям",
    "ом",
    "ем",
    "у",
    "ю",
    "а",
    "я",
    "ы",
    "и",
    "е",
)

#: Ниже этой длины основу не режем: короткие слова («дня», «или») теряют различимость.
_MIN_STEM = int(settings["agents.parse.morphology.min_stem"])


_SENTENCE_END_MARKS = r".;!?\n"

SENTENCE_SPLIT = re.compile(rf"[{_SENTENCE_END_MARKS}]")

QUALIFIER_STOP = re.compile(rf"[{_SENTENCE_END_MARKS}:]")

CLAUSE_BOUNDARY = re.compile(rf"[{_SENTENCE_END_MARKS},:()«»\"]")

CLAUSE_HEAD_END = re.compile(r"[,:;.–—-]")


def first_clause(text: str) -> str:
    """Текст до первого знака ``CLAUSE_HEAD_END``, без пробелов по краям."""
    return CLAUSE_HEAD_END.split(text, maxsplit=1)[0].strip()


PREPOSITIONS = frozenset(
    {
        "в",
        "во",
        "на",
        "над",
        "надо",
        "под",
        "подо",
        "о",
        "об",
        "обо",
        "от",
        "ото",
        "до",
        "из",
        "изо",
        "к",
        "ко",
        "с",
        "со",
        "у",
        "за",
        "по",
        "при",
        "про",
        "для",
        "без",
        "безо",
        "через",
        "чрез",
        "перед",
        "передо",
        "пред",
        "предо",
        "между",
        "меж",
        "среди",
        "средь",
        "около",
        "возле",
        "подле",
        "вокруг",
        "вдоль",
        "вне",
        "внутри",
        "ради",
        "сквозь",
        "сверх",
        "близ",
        "против",
        "после",
        "накануне",
        "насчёт",
        "насчет",
        "ввиду",
        "вследствие",
        "путём",
        "путем",
        "спустя",
    }
)
#: Союзы сочинения — признак ПЕРЕЧИСЛЕНИЯ (операций или объектов одной операции).
COORDINATORS = frozenset({"и", "или", "либо", "а", "также"})

NEGATION_PARTICLE = "не"
NEGATION_PARTICLES = frozenset({NEGATION_PARTICLE, "ни"})

VOWELS = frozenset("аеёиоуыэюя")


_GENITIVE_ATTRIBUTE_ENDINGS = "ого|его|ой|ей|ых|их"

GENITIVE_ATTRIBUTE_TAIL = re.compile(rf"(?:{_GENITIVE_ATTRIBUTE_ENDINGS})$")


NOMINAL_TAIL = re.compile(rf"(?:{_GENITIVE_ATTRIBUTE_ENDINGS}|ою|ов|ев|ий|ии|ия|ья|а|я|и|ы)$", re.IGNORECASE)

_FINITE_VERB_ENDINGS = "тся|ает|яет|ует|ают|яют|уют"


_AMBIGUOUS_VERB_ENDINGS = "ат|ят|ет|ёт|ит|ют"
FINITE_VERB_TAIL = re.compile(rf"(?:{_FINITE_VERB_ENDINGS})$", re.IGNORECASE)
AMBIGUOUS_VERB_TAIL = re.compile(rf"(?:{_AMBIGUOUS_VERB_ENDINGS})$", re.IGNORECASE)
#: Личная форма ИЛИ омонимичное ей окончание: там, где отказ должен быть щедрым.
PREDICATE_TAIL = re.compile(rf"(?:{_FINITE_VERB_ENDINGS}|{_AMBIGUOUS_VERB_ENDINGS})$", re.IGNORECASE)


ATTRIBUTIVE_OBJECT_TAIL = re.compile(r"(?:ое|ее|ую|юю|ый|ые|ого|его|ых|их|ом|ем|ой)$", re.IGNORECASE)


ATTRIBUTIVE_OBLIQUE_PLURAL = re.compile(r"(?:ых|их)$", re.IGNORECASE)
#: Порядковое числительное словом («третьим», «второй»): основа и падежное окончание.
ORDINAL_WORD = (
    r"(?:перв|втор|трет|четв[её]рт|пят|шест|седьм|восьм|девят|десят|одиннадцат|двенадцат|тринадцат"
    r"|четырнадцат|пятнадцат|шестнадцат|семнадцат|восемнадцат|девятнадцат|двадцат)"
    r"(?:ый|ий|ой|ым|им|ом|ем|ого|его|ому|ему|ая|яя|ое|ее|ую|юю|ей|ые|ие|ых|их|ыми|ими)"
)


def ends_with_vowel(word: str) -> bool:
    """Гласная на конце словоформы: окончание есть и само отвечает на вопрос о падеже."""
    return word[-1].casefold() in VOWELS


def canonical_token(token: str) -> str:
    """Основа слова по закрытому списку окончаний; короткие слова не трогаются."""
    for ending in _ENDINGS:
        if len(token) - len(ending) >= _MIN_STEM and token.endswith(ending):
            return token[: -len(ending)]
    return token


def canonical_tokens(value: str) -> tuple[str, ...]:
    """Последовательность основ слов текста в порядке появления."""
    return tuple(canonical_token(token) for token in WORD.findall(normalized_text(value)))
