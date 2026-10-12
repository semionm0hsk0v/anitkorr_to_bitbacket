from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CATALOG_PATH = Path(__file__).resolve().parent / "social_matrix_catalog.yaml"


class AnchorLaw(BaseModel):
    """Якорный акт строки матрицы — реквизиты для резолва в Консультант+."""

    ref_text: str = Field(description="Человекочитаемая ссылка целиком")
    act_type: str | None = Field(default=None, description="Вид акта: федеральный закон, кодекс, указ…")
    number: str | None = Field(default=None, description="Номер акта: 210-ФЗ, 1244-1, 49…")
    date: str | None = Field(default=None, description="Дата ДД.ММ.ГГГГ")
    title: str | None = Field(default=None, description="Наименование акта")
    article: str | None = Field(default=None, description="Статья, напр. 78, 78.1")
    #: Пред-резолв (тир 1): если задан — резолвим по адресу без поиска.
    consultant_doc_num: int | None = Field(default=None)
    consultant_base: str | None = Field(default=None)


class SocialMatrixRow(BaseModel):
    """Строка классификатора: вид НПА → предмет → якорные ФЗ."""

    id: str
    npa_type: str
    subject: str
    detection_hint: str = ""
    anchor_laws: list[AnchorLaw] = Field(default_factory=list)
    #: True только для type_13 (172-ФЗ) — добавляется ко всем in-scope документам.
    applies_always: bool = False

    in_scope: bool = True
    #: Строка 5: помимо фиксированных ФЗ — семантический поиск по предмету.
    semantic_search_required: bool = False
    semantic_keywords: list[str] = Field(default_factory=list)
    regional_scope: str | None = None

    regional_laws: list[AnchorLaw] = Field(default_factory=list)
    #: Строка 6 (тарифы): профильный отраслевой ФЗ в матрице не задан.
    unresolved_anchor: bool = False


_EXPECTED_IDS: frozenset[str] = frozenset(
    {
        "type_01_admin_reglament",
        "type_02_control_supervision",
        "type_03_subsidy_producers",
        "type_04_subsidy_nko",
        "type_05_social_support",
        "type_06_tariffs",
        "type_07_citizen_appeals",
        "type_08_personal_data",
        "type_09_authority_powers",
        "type_10_procurement",
        "type_11_education",
        "type_12_livestock_breeding",
        "type_13_universal_172fz",
    }
)


def _load_raw(path: Path | None = None) -> dict:
    source = path or CATALOG_PATH
    if not source.exists():
        raise FileNotFoundError(f"Каталог матрицы не найден: {source}")
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{source}: ожидался словарь, получено {type(data).__name__}")
    return data


@lru_cache(maxsize=1)
def load_social_matrix(path: Path | None = None) -> dict[str, SocialMatrixRow]:
    """Загружает каталог из YAML. Ключ — id строки (`type_01_…`..`type_13_…`)."""
    data = _load_raw(path)
    items = data.get("types")
    if not isinstance(items, list):
        raise ValueError("YAML: ожидался список types")

    catalog: dict[str, SocialMatrixRow] = {}
    for item in items:
        row = SocialMatrixRow(**item)
        if row.id in catalog:
            raise ValueError(f"Дублирующийся id строки: {row.id}")
        catalog[row.id] = row

    missing = _EXPECTED_IDS - catalog.keys()
    extra = catalog.keys() - _EXPECTED_IDS
    if missing or extra:
        raise ValueError(f"Каталог не соответствует матрице: отсутствуют={sorted(missing)}, лишние={sorted(extra)}")
    return catalog


def in_scope_type_ids() -> list[str]:
    "Id видов НПА, допущенных в анализ (in_scope=True) — без универсального type_13."
    return [rid for rid, row in load_social_matrix().items() if row.in_scope and not row.applies_always]
