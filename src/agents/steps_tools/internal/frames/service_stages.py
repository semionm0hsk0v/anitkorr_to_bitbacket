"""Стадии предоставления услуги и область срока, объявленная вводной конструкцией."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from src.agents.schemas.internal.frames.norm_frame import (
    WHOLE_SERVICE_DEADLINE_SCOPE,
)
from src.agents.schemas.internal.frames.service_stages import ServiceStage
from src.agents.steps_tools.document.morphology import (
    LETTERS,
    first_clause,
)


def _stage(
    name: str,
    search: str,
    noun: str,
    family: tuple[str, str] | None,
    *,
    nested: bool = True,
) -> ServiceStage:
    return ServiceStage(
        name=name,
        search=re.compile(search, re.IGNORECASE),
        noun=re.compile(noun, re.IGNORECASE),
        family=(family[0], re.compile(family[1])) if family else None,
        nested=nested,
    )


SERVICE_STAGES: tuple[ServiceStage, ...] = (
    _stage("регистрация", r"регистрац", r"\bрегистраци(?:я|и|ю|ей)\b", ("registration", r"\bрегистрац")),
    _stage("приём", r"\bпри[её]м(?:а|у|ом|е)?\b", r"\bпри[её]м(?:а|у|ом|е)?\b", ("intake", r"\bприем")),
    _stage("решение", r"\bрешени", r"\bрешени(?:е|я|ю|ем|и)\b", ("decision", r"\bрешени")),
    _stage("отказ", r"\bотказ", r"\bотказ(?:а|у|ом|е)?\b", ("refusal", r"\bотказ")),
    _stage("результат", r"результат", r"\bрезультат(?:а|у|ом|е|ы|ов)?\b", ("result", r"\bрезультат")),
    _stage("выдача", r"\bвыдач|\bвыда[её]т", r"\bвыдач(?:а|и|у|ей)\b", ("issue", r"\bвыдач")),
    _stage("исправление", r"исправлен|опечат|ошиб", r"\bисправлени(?:е|я|ю|ем|и)\b", None),
    _stage("обжалование", r"обжалован|досудебн", r"\bобжаловани(?:е|я|ю|ем|и)\b", ("appeal", r"\bобжалован"), nested=False),
)

#: Название этапа → ``search``, в порядке таблицы.
STAGE_SEARCH: tuple[tuple[str, re.Pattern[str]], ...] = tuple((stage.name, stage.search) for stage in SERVICE_STAGES)
#: Название этапа → словоформы существительного.
STAGE_NOUNS: dict[str, re.Pattern[str]] = {stage.name: stage.noun for stage in SERVICE_STAGES}

STAGE_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("general_service", re.compile(r"\b(?:услуг|предоставлени)")),
    *(stage.family for stage in SERVICE_STAGES if stage.family is not None),
)
#: Названия этапов, входящих в предоставление услуги.
NESTED_STAGE_NAMES = frozenset(stage.name for stage in SERVICE_STAGES if stage.nested)


CLAUSE_NUMBER_PREFIX = re.compile(r"^\s*\d+(?:\.\d+)*\.\s*")


_STAGE_TITLE_PREFIX = re.compile(r"^(?:срок|порядок|правила|этап)\b", re.IGNORECASE)


DEADLINE_HEAD = re.compile(r"^(?:максимальн\w+\s+|общ\w+\s+|предельн\w+\s+)?срок(?:и)?\b", re.IGNORECASE)


WHOLE_SERVICE_HEAD = re.compile(
    r"^(?:предоставлени|оказани)\w*"
    r"(?:\s*[(«]?\s*[^\W\d_]+(?:ого|его|ой|ей|ых|их)[)»]?)*"
    r"\s*[(«]?\s*услуг\w*",
    re.IGNORECASE,
)


STAGE_CONSTITUTING_CONSTRUCTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("решение", re.compile(r"^принят(?:ие|ия|ию|ием)\s+решени(?:е|я|ю|ем|и)\b", re.IGNORECASE)),
)


def resolved_stage(own_texts: Iterable[str], heading_texts: Iterable[str]) -> str | None:
    "Стадия нормы, определённая однозначно и независимо от порядка ``STAGE_SEARCH``."
    for scope in (own_texts, heading_texts):
        names = {name for name, pattern in STAGE_SEARCH if any(pattern.search(item) for item in scope if item)}
        if len(names) == 1:
            return next(iter(names))
        if names:
            return None
    return None


def stage_noun(word: str) -> str | None:
    """Название этапа, если ИМЕННО это слово им является; иначе ``None``."""
    for name, pattern in STAGE_NOUNS.items():
        if pattern.fullmatch(word):
            return name
    return None


def _deadline_scope_of_line(line: str) -> str | None | Literal[False]:
    "Что измеряет срок, объявленный ЭТОЙ вводной строкой."
    intro = CLAUSE_NUMBER_PREFIX.sub("", line.strip())
    head = DEADLINE_HEAD.match(intro)
    if head is None:
        return None
    clause = first_clause(intro[head.end() :])
    if WHOLE_SERVICE_HEAD.match(clause):
        return WHOLE_SERVICE_DEADLINE_SCOPE
    words = [match.group() for match in LETTERS.finditer(clause)]
    if not words:
        return False
    stage = stage_noun(words[0])
    if stage is not None:
        return stage

    for name, pattern in STAGE_CONSTITUTING_CONSTRUCTIONS:
        if pattern.match(clause):
            return name
    return False


def deadline_scope(introducing_lines: Iterable[str]) -> tuple[str, str] | None:
    "Область срока и строка, которая её вводит; ближайшая конструкция побеждает."
    for line in introducing_lines:
        if not line.strip():
            continue
        scope = _deadline_scope_of_line(line)
        if scope is False:
            return None
        if scope is not None:
            return scope, line
    return None


def stage_role_is_proven(stage: str | None, own_text: str) -> bool:
    "Prove a stage only when the norm's own structural intro names it."
    noun = STAGE_NOUNS.get(stage or "")
    if noun is None or not own_text:
        return False
    intro = CLAUSE_NUMBER_PREFIX.sub("", own_text.split("\n", 1)[0]).strip()
    match = noun.search(intro)
    if match is None:
        return False
    return match.start() == 0 or bool(_STAGE_TITLE_PREFIX.fullmatch(intro[: match.start()].strip()))
