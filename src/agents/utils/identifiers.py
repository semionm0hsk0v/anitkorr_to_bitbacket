import hashlib
import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.replace("Ё", "Е").replace("ё", "е")).strip()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=_canonical_json)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.blake2b(_canonical_json(value).encode("utf-8"), digest_size=16)
    return f"{prefix}:{digest.hexdigest()}"
