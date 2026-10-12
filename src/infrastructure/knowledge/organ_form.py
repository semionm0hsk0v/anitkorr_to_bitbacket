from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CATALOG_PATH = Path(__file__).resolve().parent / "organ_form_catalog.yaml"


class BodyForm(BaseModel):
    """Строка каталога: вид органа + якоря распознавания + допустимые формы актов."""

    kind: str
    label: str
    anchors: list[str] = Field(default_factory=list)
    forms: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def load_organ_form() -> list[BodyForm]:
    """Каталог «орган→форма» из YAML (порядок = приоритет разрешения неоднозначности)."""
    data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
    return [BodyForm.model_validate(row) for row in (data.get("bodies") or [])]


def classify_organ_kind(text: str) -> str:
    "Канон вида органа по тексту (шапка проекта / название органа) → kind | 'unknown'."
    norm = " ".join((text or "").lower().replace("ё", "е").split())
    if not norm:
        return "unknown"
    for body in load_organ_form():
        for anchor in body.anchors:
            a = " ".join((anchor or "").lower().replace("ё", "е").split())
            if a and a in norm:
                return body.kind
    return "unknown"


def forms_for_kind(kind: str) -> list[str]:
    """Допустимые формы актов для вида органа (пусто, если вид неизвестен)."""
    for body in load_organ_form():
        if body.kind == kind:
            return list(body.forms)
    return []


def form_matches_kind(form: str, kind: str) -> bool:
    "Допустима ли форма акта ``form`` для вида органа ``kind``."
    if kind == "unknown" or not form or form == "unknown":
        return True
    return " ".join((form or "").lower().replace("ё", "е").split()) in {
        " ".join((f or "").lower().replace("ё", "е").split()) for f in forms_for_kind(kind)
    }


def label_for_kind(kind: str) -> str:
    """Человекочитаемая метка вида органа (для формулировки находки)."""
    for body in load_organ_form():
        if body.kind == kind:
            return body.label
    return kind
