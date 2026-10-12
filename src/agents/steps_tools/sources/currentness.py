from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from src.agents.enums.legal_base.currentness import (
    ReferenceCoverageStatus,
    ReferenceCurrentnessOutcome,
    ReferenceCurrentnessReason,
)
from src.agents.schemas.legal_base.currentness import (
    ReferenceActIdentity,
    ReferenceCurrentnessReceipt,
    ReferenceLiveFetchEvidence,
    ReferenceVersionFingerprint,
    _live_version_supports_current,
    reference_content_fingerprint,
    validate_reference_currentness_receipt,
)
from src.agents.steps_tools.sources.normalization import (
    canonicalize_reference_date,
)
from src.agents.steps_tools.sources.trust import currentness_receipt_is_trusted


def reference_currentness_allows_publication(
    receipt: ReferenceCurrentnessReceipt,
    *,
    act_content_fingerprint: str | None,
    reference_content_fingerprint: str | None,
    act_identity: ReferenceActIdentity | None = None,
) -> bool:
    "Return whether receipt and both downstream raw fingerprints bind one version."
    try:
        canonical = validate_reference_currentness_receipt(receipt.model_dump(mode="python"))
    except (TypeError, ValueError):
        return False
    if canonical.outcome is not ReferenceCurrentnessOutcome.CURRENT or canonical.observed is None:
        return False
    version = canonical.observed
    if act_identity is not None and act_identity != version.identity:
        return False
    if not act_content_fingerprint or not reference_content_fingerprint:
        return False
    return hmac.compare_digest(version.content_fingerprint, act_content_fingerprint) and hmac.compare_digest(
        version.content_fingerprint, reference_content_fingerprint
    )


def current_analysis_date(*, now: datetime | None = None) -> str:
    "Дата анализа «действующее право сегодня» по ТОМУ ЖЕ правилу часового пояса."
    moment = now if now is not None else datetime.now(UTC)
    return moment.astimezone(UTC).date().isoformat()


def analysis_date_is_current(analysis_date: str | None, *, now: datetime) -> bool:
    "Whether the requested analysis date is the current-law mode this policy proves."
    if analysis_date is None or not str(analysis_date).strip():
        return True
    canonical = canonicalize_reference_date(analysis_date)
    return canonical == now.astimezone(UTC).date().isoformat()


def reference_metadata_fingerprint(info: dict[str, object]) -> str:
    """Fingerprint the version-relevant document metadata, excluding volatile fields."""
    payload = {
        key: info.get(key)
        for key in (
            "title",
            "kinds_of_doc",
            "numbers",
            "organs",
            "dates",
            "last_edition_dates",
            "parts_date",
            "begin_date",
            "status_search",
            "par_count",
        )
        if info.get(key) is not None
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def decide_live_currentness(
    version: ReferenceVersionFingerprint,
    live: ReferenceLiveFetchEvidence,
) -> tuple[ReferenceCurrentnessOutcome, ReferenceCurrentnessReason]:
    "Pure live-policy decision for one freshly fetched version."
    failed = set() if live.all_checks_passed else set(live.failed_check_names())
    if "requested_requisites" in failed:
        return ReferenceCurrentnessOutcome.CONFLICT, ReferenceCurrentnessReason.IDENTITY_REQUISITE_MISMATCH
    if "title_identity" in failed:
        return ReferenceCurrentnessOutcome.CONFLICT, ReferenceCurrentnessReason.TITLE_IDENTITY_MISMATCH
    if version.in_force is False:
        return ReferenceCurrentnessOutcome.STALE, ReferenceCurrentnessReason.REVOKED
    if version.coverage.status is ReferenceCoverageStatus.INCOMPLETE:
        return ReferenceCurrentnessOutcome.TECHNICAL_FAILURE, ReferenceCurrentnessReason.INCOMPLETE_FETCH
    if not live.analysis_date_supported:
        return ReferenceCurrentnessOutcome.UNKNOWN, ReferenceCurrentnessReason.ANALYSIS_DATE_UNSUPPORTED
    if not live.metadata_stable:
        return ReferenceCurrentnessOutcome.CONFLICT, ReferenceCurrentnessReason.SOURCE_CHANGED_DURING_FETCH
    if not live.all_checks_passed:
        if "edition_in_force_at_analysis_date" in failed:
            return ReferenceCurrentnessOutcome.STALE, ReferenceCurrentnessReason.NOT_YET_IN_FORCE
        return ReferenceCurrentnessOutcome.UNKNOWN, ReferenceCurrentnessReason.INCOMPLETE_METADATA
    if not _live_version_supports_current(version):
        return ReferenceCurrentnessOutcome.UNKNOWN, ReferenceCurrentnessReason.INCOMPLETE_METADATA
    reason = (
        ReferenceCurrentnessReason.LIVE_CURRENT_CARD_TRUNCATED
        if version.coverage.status is ReferenceCoverageStatus.TRUNCATED_CONFIGURED
        else ReferenceCurrentnessReason.LIVE_CURRENT_CARD
    )
    return ReferenceCurrentnessOutcome.CURRENT, reason


def has_currentness_evidence(raw: Any) -> bool:
    "Require one canonical CURRENT receipt bound to the exact fetched content."
    if raw.version is None or raw.currentness_receipt is None or not raw.full_text:
        return False
    if not currentness_receipt_is_trusted(raw.currentness_receipt):
        return False
    return reference_currentness_allows_publication(
        raw.currentness_receipt,
        act_content_fingerprint=raw.version.content_fingerprint,
        reference_content_fingerprint=reference_content_fingerprint(raw.full_text),
        act_identity=raw.version.identity,
    )


def confirmed_current_text(raw: Any) -> bool:
    "Общий предохранитель ВСЕХ потребителей полного текста (компетенция, детектор"
    if not has_currentness_evidence(raw):
        return False
    return raw.version is not None and raw.version.coverage.status is ReferenceCoverageStatus.COMPLETE
