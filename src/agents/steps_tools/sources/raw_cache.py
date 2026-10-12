"""Атомарное хранение неизменяемых версий сырых актов и проверка целостности кэша."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import ValidationError

from src.agents.schemas.legal_base.currentness import (
    ReferenceSourceCoordinates,
    ReferenceVersionFingerprint,
)
from src.agents.schemas.legal_base.retrieval import RawReferenceCacheEnvelope
from src.common.utils.logger import get_logger

log = get_logger(__name__)


_REPO_ROOT = Path(__file__).resolve().parents[4]


_CACHE_DIR = _REPO_ROOT / ".cache" / "consultant_acts"


def _cache_path(version: ReferenceVersionFingerprint) -> Path:
    digest = version.version_fingerprint.removeprefix("sha256:")
    return _CACHE_DIR / f"{version.source.base}_{version.source.doc_num}_{digest}.json"


def cache_candidates(base: str, doc_num: int) -> list[Path]:
    source = ReferenceSourceCoordinates(base=base, doc_num=doc_num)
    return sorted(_CACHE_DIR.glob(f"{source.base}_{source.doc_num}_*.json"))


def _write_raw_cache(envelope: RawReferenceCacheEnvelope) -> Path:
    """Atomically persist one immutable version; incompatible revisions never overwrite."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = _cache_path(envelope.version)
    if target.exists():
        # Revalidate an existing path rather than treating filename equality as integrity.
        existing = RawReferenceCacheEnvelope.model_validate_json(target.read_text(encoding="utf-8"))
        if existing != envelope:
            raise ValueError("cache path collision for incompatible reference payloads")
        return target
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=_CACHE_DIR,
        delete=False,
    ) as pending:
        pending.write(envelope.model_dump_json(indent=1))
        pending_path = Path(pending.name)
    pending_path.replace(target)
    return target


def read_raw_cache(path: Path) -> RawReferenceCacheEnvelope:
    envelope = RawReferenceCacheEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
    if path.name != _cache_path(envelope.version).name:
        raise ValueError("cache path does not match embedded source/version fingerprint")
    return envelope


async def try_write_cache(
    *, version: ReferenceVersionFingerprint, title: str, content: str, source: ReferenceSourceCoordinates
) -> None:
    try:
        envelope = RawReferenceCacheEnvelope(version=version, title=title, content=content)
        _write_raw_cache(envelope)
    except (OSError, ValidationError, ValueError) as exc:
        await log.awarning(
            "reference_cache_write_failed",
            base=source.base,
            doc_num=source.doc_num,
            error_class=type(exc).__name__,
        )
