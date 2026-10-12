"""Подтверждение идентичности, редакции и полноты прочитанного правового источника."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from src.agents.schemas.legal_base.act_refs import ActRequisites
from src.agents.schemas.legal_base.currentness import (
    ReferenceActIdentity,
    ReferenceCheckResult,
    ReferenceContentCoverage,
    ReferenceCoverageStatus,
    ReferenceCurrentnessReceipt,
    ReferenceLiveFetchEvidence,
    ReferenceSourceCoordinates,
    ReferenceVersionFingerprint,
    reference_content_fingerprint,
)
from src.agents.schemas.legal_base.retrieval import RawAct
from src.agents.steps_tools.sources.act_refs import act_title_head, parse_act_requisites
from src.agents.steps_tools.sources.currentness import (
    analysis_date_is_current,
)
from src.agents.steps_tools.sources.normalization import (
    canonicalize_reference_date,
    canonicalize_reference_title,
    card_number_variants,
    card_vid,
)
from src.agents.steps_tools.sources.selection import (
    hit_date,
)
from src.common.config.config import settings

_EDITION_RE = re.compile(r"\(ред\.\s*от\s*([\d.]+)", re.IGNORECASE)


_DEAD_MARKERS = ("утратил силу", "утратила силу", "утратили силу", "признан утратившим", "не действует")


def edition_from_title(title: str) -> str | None:
    m = _EDITION_RE.search(title or "")
    return m.group(1) if m else None


def _is_in_force(title: str, info: dict) -> bool:
    "Return only whether the title lacks positive revocation evidence."
    low = (title or "").lower()
    return not any(mark in low for mark in _DEAD_MARKERS)


def authoritative_in_force(info: dict[str, Any], title: str) -> bool | None:
    "Read only a positively contracted document-status field."
    if not _is_in_force(title, info):
        return False
    value = info.get("in_force")
    return value if isinstance(value, bool) else None


def issued_from_info(info: dict[str, Any]) -> str | None:
    explicit = info.get("issued")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    dates = info.get("dates")
    if isinstance(dates, list) and dates and isinstance(dates[0], str):
        return dates[0]
    return None


def _reference_identity(title: str) -> ReferenceActIdentity:
    "Реквизиты акта из названия карточки; у названий длиннее пределов схемы лишнее отсекается."
    requisites = parse_act_requisites(title)
    return ReferenceActIdentity(
        title=title if len(title) <= _MAX_IDENTITY_TITLE else canonicalize_reference_title(title)[:_MAX_IDENTITY_TITLE],
        act_kind=_capped(requisites.kind if requisites is not None else None, _MAX_IDENTITY_REQUISITE),
        act_number=_capped(requisites.number if requisites is not None else None, _MAX_IDENTITY_REQUISITE),
        issuer=_capped(act_title_head(title), _MAX_IDENTITY_ISSUER),
        jurisdiction=_capped(requisites.region if requisites is not None else None, _MAX_IDENTITY_ISSUER),
    )


_MAX_IDENTITY_TITLE = int(settings["agents.legal_base.retrieval.max_identity_title"])


_MAX_IDENTITY_REQUISITE = int(settings["agents.legal_base.retrieval.max_identity_requisite"])


_MAX_IDENTITY_ISSUER = int(settings["agents.legal_base.retrieval.max_identity_issuer"])


def _capped(value: str | None, limit: int) -> str | None:
    return value[:limit] if value is not None else None


def build_reference_version(
    *,
    base: str,
    doc_num: int,
    title: str,
    edition: str | None,
    issued: str | None,
    in_force: bool | None,
    par_count: int,
    coverage: ReferenceContentCoverage,
    content: str,
) -> ReferenceVersionFingerprint:
    return ReferenceVersionFingerprint(
        source=ReferenceSourceCoordinates(base=base, doc_num=doc_num),
        identity=_reference_identity(title),
        edition=edition,
        issued=issued,
        in_force=in_force,
        par_count=par_count,
        coverage=coverage,
        content_fingerprint=reference_content_fingerprint(content),
    )


def _info_dates(info: dict[str, Any]) -> set[str]:
    """Все ISO-даты карточки: дата принятия, даты редакций, даты частей."""
    out: set[str] = set()
    for key in ("dates", "last_edition_dates", "parts_date"):
        values = info.get(key)
        if isinstance(values, list):
            for item in values:
                if isinstance(item, str):
                    try:
                        canonical = canonicalize_reference_date(item)
                    except ValueError:
                        continue
                    if canonical:
                        out.add(canonical)
    return out


def _safe_date(value: str | None) -> str | None:
    try:
        return canonicalize_reference_date(value)
    except ValueError:
        return None


def _requisite_identity_check(info: dict[str, Any], title: str, requisites: ActRequisites | None) -> ReferenceCheckResult:
    "Explicit requisites must be confirmed by the card itself, never by title similarity."
    if requisites is None:
        return ReferenceCheckResult(name="requested_requisites", passed=True, detail="no_explicit_requisites")
    found = parse_act_requisites(title)
    if requisites.number:
        variants = card_number_variants(requisites.number)
        card_numbers = {str(item).strip().upper() for item in (info.get("numbers") or []) if str(item).strip()}
        title_number = found.card_number if found is not None else ""
        if title_number not in variants and not (card_numbers & set(variants)):
            return ReferenceCheckResult(name="requested_requisites", passed=False, detail="number_mismatch")
    if requisites.date:
        wanted = _safe_date(requisites.date)
        available = _info_dates(info) | {value for value in {_safe_date(hit_date(title))} if value}
        if wanted and available and wanted not in available:
            return ReferenceCheckResult(name="requested_requisites", passed=False, detail="date_mismatch")
        if wanted and not available:
            return ReferenceCheckResult(name="requested_requisites", passed=False, detail="date_unconfirmed")

    if requisites.kind and found is not None and found.kind:
        wanted_kind, found_kind = card_vid(requisites.kind) or requisites.kind, card_vid(found.kind) or found.kind
        if wanted_kind != found_kind:
            return ReferenceCheckResult(name="requested_requisites", passed=False, detail="kind_mismatch")
    return ReferenceCheckResult(name="requested_requisites", passed=True)


def _future_edition_check(info: dict[str, Any], *, now: datetime) -> ReferenceCheckResult:
    "Reject a selected version whose own start date has not arrived."
    begin = _safe_date(info.get("begin_date") if isinstance(info.get("begin_date"), str) else None)
    if begin is None:
        return ReferenceCheckResult(name="edition_in_force_at_analysis_date", passed=True, detail="begin_date_absent")
    if begin > now.astimezone(UTC).date().isoformat():
        return ReferenceCheckResult(name="edition_in_force_at_analysis_date", passed=False, detail="begin_date_future")
    return ReferenceCheckResult(name="edition_in_force_at_analysis_date", passed=True)


def live_identity_checks(
    *,
    info: dict[str, Any],
    card_title: str,
    effective_title: str,
    requisites: ActRequisites | None,
    now: datetime,
) -> list[ReferenceCheckResult]:
    info_title = str(info.get("title") or "").strip()
    title_consistent = not (
        info_title and card_title and canonicalize_reference_title(info_title) != canonicalize_reference_title(card_title)
    )
    return [
        ReferenceCheckResult(name="title_identity", passed=title_consistent),
        _requisite_identity_check(info, effective_title, requisites),
        ReferenceCheckResult(
            name="revocation_marker_absent",
            passed=_is_in_force(effective_title, info),
            detail=None if _is_in_force(effective_title, info) else "title_marks_revocation",
        ),
        _future_edition_check(info, now=now),
    ]


def live_version_checks(
    *, version: ReferenceVersionFingerprint, content: str, metadata_stable: bool
) -> list[ReferenceCheckResult]:
    return [
        ReferenceCheckResult(name="metadata_stable", passed=metadata_stable),
        ReferenceCheckResult(
            name="requested_range_fetched",
            passed=version.coverage.status is not ReferenceCoverageStatus.INCOMPLETE,
            detail=version.coverage.status.value,
        ),
        ReferenceCheckResult(name="content_present", passed=bool(content) and version.par_count > 0),
    ]


def build_live_evidence(
    *,
    transport: str,
    fetched_at: datetime,
    analysis_date: str | None,
    metadata_before: str,
    metadata_after: str,
    refetch_attempts: int,
    identity_checks: list[ReferenceCheckResult],
    version_checks: list[ReferenceCheckResult],
) -> ReferenceLiveFetchEvidence:
    return ReferenceLiveFetchEvidence(
        transport=transport,
        fetched_at=fetched_at,
        analysis_date=analysis_date,
        analysis_date_supported=analysis_date_is_current(analysis_date, now=fetched_at),
        metadata_fingerprint_before=metadata_before,
        metadata_fingerprint_after=metadata_after,
        refetch_attempts=refetch_attempts,
        identity_checks=tuple(identity_checks),
        version_checks=tuple(version_checks),
    )


def status_search_of(info: dict[str, Any] | None) -> str | None:
    """``status_search`` дословно, если К+ вообще отдал метаданные в этом запросе."""
    if not info:
        return None
    value = info.get("status_search")
    return value if isinstance(value, str) and value.strip() else None


def raw_act_from_version(
    *,
    query: str,
    title: str,
    content: str,
    version: ReferenceVersionFingerprint,
    receipt: ReferenceCurrentnessReceipt,
    status_search: str | None = None,
) -> RawAct:
    return RawAct(
        query=query,
        base=version.source.base,
        doc_num=version.source.doc_num,
        title=title,
        edition=version.edition,
        issued=version.issued,
        in_force=version.in_force,
        par_count=version.par_count,
        full_text=content,
        version=version,
        currentness_receipt=receipt,
        status_search=status_search,
    )
