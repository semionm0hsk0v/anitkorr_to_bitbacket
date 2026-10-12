"""Метки положений документа и их представление в человекочитаемом адресе."""

from __future__ import annotations

import re

from src.agents.schemas.parse.locus_labels import LocusLabel

APPENDIX = "прил."
ARTICLE = "ст."
PART = "ч."
CLAUSE = "п."
SUBCLAUSE = "пп."
PARAGRAPH = "абз."


LOCUS_LABELS: tuple[LocusLabel, ...] = (
    LocusLabel(abbreviation=APPENDIX, nominative="приложение № {}", genitive="приложения № {}"),
    LocusLabel(abbreviation=ARTICLE, nominative="статья {}", genitive="статьи {}"),
    LocusLabel(abbreviation=PART, nominative="часть {}", genitive="части {}"),
    LocusLabel(abbreviation=SUBCLAUSE, nominative="подпункт {}", genitive="подпункта {}", value_pattern=r"(.+?)\)?$"),
    LocusLabel(abbreviation=CLAUSE, nominative="пункт {}", genitive="пункта {}"),
    LocusLabel(abbreviation=PARAGRAPH, nominative="абзац {}", genitive="абзаца {}"),
)


def locus(abbreviation: str, value: object) -> str:
    """Метка места: сокращение и значение через пробел."""
    return f"{abbreviation} {value}"


_LOCUS_PARTS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(rf"^{re.escape(label.abbreviation)}\s*{label.value_pattern}", re.IGNORECASE), label.nominative, label.genitive)
    for label in LOCUS_LABELS
]


def humanize_locus(label: str) -> str:
    "«прил. 4, абз. 1, абз. 3» → «абзац 3 приложения № 4»."
    raw = (label or "").strip()
    if not raw:
        return ""
    chunks = [c.strip() for c in raw.split(",") if c.strip()]
    parsed: list[tuple[str, str, str]] = []  # (тег, им.п., род.п.)
    for chunk in chunks:
        for pattern, nom, gen in _LOCUS_PARTS:
            m = pattern.match(chunk)
            if m:
                value = m.group(1).strip().rstrip(").")
                parsed.append((pattern.pattern, nom.format(value), gen.format(value)))
                break
        else:
            return raw
    if not parsed:
        return raw
    # схлопываем цепочку одноимённых уровней («абз. 1, абз. 3») до самого внутреннего
    collapsed: list[tuple[str, str, str]] = []
    for part in parsed:
        if collapsed and collapsed[-1][0] == part[0]:
            collapsed[-1] = part
        else:
            collapsed.append(part)
    head = collapsed[-1][1]
    tail = [p[2] for p in reversed(collapsed[:-1])]
    return " ".join([head, *tail])
