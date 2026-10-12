from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.common.config.config import settings

CATALOG_PATH = Path(__file__).resolve().parent / "organ_statutes.yaml"

#: Знаки, которые отрезаются по краям слова: кавычки, скобки, пунктуация.
_EDGE_CHARS = "«»\"'“”„()[],.;:!?"

_EXACT_STEM_MAX_LEN = int(settings["infrastructure.knowledge.organ_statutes.exact_stem_max_len"])


class StatuteRequisites(BaseModel):
    """Реквизиты постановления Правительства области «О Министерстве …»."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str = Field(min_length=1)
    date: str = Field(pattern=r"^\d{2}\.\d{2}\.\d{4}$")
    title: str = Field(min_length=1)


class OrganStatute(BaseModel):
    """Строка справочника: орган-издатель, якоря его названия и его Положение."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    anchors: tuple[str, ...] = Field(min_length=1)
    statute: StatuteRequisites


class AuthorityMarkers(BaseModel):
    "Признаки структурной единицы акта, говорящей о полномочиях."

    model_config = ConfigDict(extra="forbid", frozen=True)

    heading_stems: tuple[str, ...] = Field(min_length=1)
    heading_organs: tuple[str, ...] = Field(min_length=1)
    text_organs: tuple[str, ...] = Field(min_length=1)


class OrganStatuteCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    region: str
    statutes: tuple[OrganStatute, ...]
    authority_markers: AuthorityMarkers


def _words(text: str) -> list[str]:
    """Слова текста в нормализованном виде (lower, ё→е, без пунктуации по краям)."""
    words = ((text or "").lower().replace("ё", "е")).split()
    return [w for w in (word.strip(_EDGE_CHARS) for word in words) if w]


def _stem_matches(stem: str, word: str) -> bool:
    return word == stem if len(stem) <= _EXACT_STEM_MAX_LEN else word.startswith(stem)


def mentions(anchor: str, text: str) -> bool:
    """Назван ли в тексте ``anchor`` — последовательность основ, падеж не важен."""
    return _first_position(anchor, _words(text)) is not None


def _first_position(anchor: str, words: list[str]) -> int | None:
    stems = _words(anchor)
    if not stems:
        return None
    for start in range(len(words) - len(stems) + 1):
        if all(_stem_matches(stem, words[start + i]) for i, stem in enumerate(stems)):
            return start
    return None


@lru_cache(maxsize=1)
def load_organ_statutes() -> OrganStatuteCatalog:
    """Справочник Положений из YAML."""
    data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
    return OrganStatuteCatalog(
        region=str((data.get("meta") or {}).get("region", "")),
        statutes=tuple(OrganStatute.model_validate(row) for row in data.get("statutes") or []),
        authority_markers=AuthorityMarkers.model_validate(data.get("authority_markers") or {}),
    )


def find_organ_statute(text: str) -> OrganStatute | None:
    "Орган, названный в тексте РАНЬШЕ остальных, → его строка справочника."
    words = _words(text)
    best: tuple[int, OrganStatute] | None = None
    for row in load_organ_statutes().statutes:
        for anchor in row.anchors:
            position = _first_position(anchor, words)
            if position is not None and (best is None or position < best[0]):
                best = (position, row)
    return best[1] if best is not None else None
