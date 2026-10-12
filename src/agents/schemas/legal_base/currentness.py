from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.agents.enums.legal_base.currentness import (
    ReferenceCoverageStatus,
    ReferenceCurrentnessOutcome,
    ReferenceCurrentnessReason,
)
from src.agents.steps_tools.sources.normalization import (
    canonicalize_reference_date,
    canonicalize_reference_requisite,
    canonicalize_reference_title,
)

REFERENCE_CURRENTNESS_SCHEMA_VERSION: Literal[1] = 1

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_BASE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class _StrictReferenceCurrentnessModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def reference_content_fingerprint(content: str) -> str:
    """Return the content-integrity fingerprint used by versioned raw caches."""
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


# Short alias for codecs which do not otherwise import the NormFrame helper.
content_fingerprint = reference_content_fingerprint


class ReferenceSourceCoordinates(_StrictReferenceCurrentnessModel):
    """Exact, safe Consultant document coordinates (no query or provider payload)."""

    schema_version: Literal[1] = Field(default=REFERENCE_CURRENTNESS_SCHEMA_VERSION, frozen=True)
    base: str = Field(min_length=1, max_length=128)
    doc_num: int = Field(gt=0)

    @field_validator("base")
    @classmethod
    def _normalize_base(cls, value: str) -> str:
        normalized = canonicalize_reference_requisite(value).upper()
        if not _SOURCE_BASE_RE.fullmatch(normalized):
            raise ValueError("source base must contain only safe uppercase bank identifier characters")
        return normalized


class ReferenceActIdentity(_StrictReferenceCurrentnessModel):
    """Normalized requisites identifying an act independently of its edition."""

    schema_version: Literal[1] = Field(default=REFERENCE_CURRENTNESS_SCHEMA_VERSION, frozen=True)
    title: str = Field(min_length=1, max_length=2_000)
    act_kind: str | None = Field(default=None, max_length=256)
    act_number: str | None = Field(default=None, max_length=256)
    issuer: str | None = Field(default=None, max_length=1_024)
    jurisdiction: str | None = Field(default=None, max_length=1_024)

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str) -> str:
        normalized = canonicalize_reference_title(value)
        if not normalized:
            raise ValueError("reference title must not be blank")
        return normalized

    @field_validator("act_kind", "act_number", "issuer", "jurisdiction")
    @classmethod
    def _normalize_optional_requisite(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = canonicalize_reference_requisite(value)
        return normalized or None


class ReferenceParagraphRange(_StrictReferenceCurrentnessModel):
    """Inclusive paragraph range in one source document."""

    schema_version: Literal[1] = Field(default=REFERENCE_CURRENTNESS_SCHEMA_VERSION, frozen=True)
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> ReferenceParagraphRange:
        if self.end < self.start:
            raise ValueError("paragraph range end must not precede start")
        return self


def _canonical_ranges(values: list[ReferenceParagraphRange]) -> list[ReferenceParagraphRange]:
    unique = {(item.start, item.end): item for item in values}
    return [unique[key] for key in sorted(unique)]


def _ranges_cover(requested: list[ReferenceParagraphRange], fetched: list[ReferenceParagraphRange]) -> bool:
    """Whether fetched ranges cover every requested paragraph, allowing overlap."""
    for target in requested:
        cursor = target.start
        for item in fetched:
            if item.end < cursor:
                continue
            if item.start > cursor:
                break
            cursor = max(cursor, item.end + 1)
            if cursor > target.end:
                break
        if cursor <= target.end:
            return False
    return True


class ReferenceContentCoverage(_StrictReferenceCurrentnessModel):
    """Requested versus fetched source paragraphs, including explicit configured caps."""

    schema_version: Literal[1] = Field(default=REFERENCE_CURRENTNESS_SCHEMA_VERSION, frozen=True)
    status: ReferenceCoverageStatus
    requested_ranges: list[ReferenceParagraphRange] = Field(default_factory=list, max_length=10_000)
    fetched_ranges: list[ReferenceParagraphRange] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def _validate_coverage(self) -> ReferenceContentCoverage:
        self.requested_ranges = _canonical_ranges(self.requested_ranges)
        self.fetched_ranges = _canonical_ranges(self.fetched_ranges)
        if not self.requested_ranges:
            if self.status is not ReferenceCoverageStatus.INCOMPLETE or self.fetched_ranges:
                raise ValueError("only incomplete coverage may omit requested and fetched paragraph ranges")
            return self
        covered = _ranges_cover(self.requested_ranges, self.fetched_ranges)
        if self.status is ReferenceCoverageStatus.INCOMPLETE:
            if covered:
                raise ValueError("incomplete coverage must leave part of a requested range unfetched")
        elif not covered:
            raise ValueError("complete or configured-truncation coverage must cover every requested range")
        return self


def build_reference_version_fingerprint(version: ReferenceVersionFingerprint) -> str:
    """Build the canonical content-and-metadata identity of one source version."""
    payload = json.dumps(
        version.model_dump(mode="json", exclude={"version_fingerprint"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class ReferenceVersionFingerprint(_StrictReferenceCurrentnessModel):
    """Versioned identity of persisted raw reference content, not a freshness claim."""

    schema_version: Literal[1] = Field(default=REFERENCE_CURRENTNESS_SCHEMA_VERSION, frozen=True)
    source: ReferenceSourceCoordinates
    identity: ReferenceActIdentity
    edition: str | None = Field(default=None, max_length=32)
    issued: str | None = Field(default=None, max_length=32)
    in_force: bool | None
    par_count: int = Field(ge=0)
    coverage: ReferenceContentCoverage
    content_fingerprint: str = Field(min_length=71, max_length=71)
    version_fingerprint: str = Field(default="", min_length=71, max_length=71)

    @field_validator("edition", "issued")
    @classmethod
    def _normalize_date(cls, value: str | None) -> str | None:
        return canonicalize_reference_date(value)

    @field_validator("content_fingerprint")
    @classmethod
    def _validate_content_fingerprint(cls, value: str) -> str:
        if not _FINGERPRINT_RE.fullmatch(value):
            raise ValueError("content_fingerprint must be a sha256 fingerprint")
        return value

    @model_validator(mode="after")
    def _validate_and_assign_fingerprint(self) -> ReferenceVersionFingerprint:
        requested_end = max((item.end for item in self.coverage.requested_ranges), default=0)
        if self.par_count == 0:
            if self.coverage.status is not ReferenceCoverageStatus.INCOMPLETE:
                raise ValueError("unknown paragraph count cannot claim complete coverage")
        else:
            if requested_end > self.par_count:
                raise ValueError("requested paragraph range exceeds par_count")
            if any(item.end > self.par_count for item in self.coverage.fetched_ranges):
                raise ValueError("fetched paragraph range exceeds par_count")
        if self.coverage.status is ReferenceCoverageStatus.COMPLETE:
            if self.par_count < 1 or self.coverage.requested_ranges != [ReferenceParagraphRange(start=1, end=self.par_count)]:
                raise ValueError("complete coverage must request the full paragraph range")
        elif self.coverage.status is ReferenceCoverageStatus.TRUNCATED_CONFIGURED and (
            self.par_count < 1 or requested_end >= self.par_count
        ):
            raise ValueError("configured truncation must stop before the available final paragraph")

        expected = build_reference_version_fingerprint(self)
        if self.version_fingerprint and self.version_fingerprint != expected:
            raise ValueError("version_fingerprint must match canonical version identity")
        self.version_fingerprint = expected
        return self


def _stable_id(prefix: str, value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{prefix}:{hashlib.blake2b(serialized.encode('utf-8'), digest_size=16).hexdigest()}"


#: Reasons a receipt carrying live fetch evidence may state, by outcome.
_LIVE_REASONS_BY_OUTCOME: dict[ReferenceCurrentnessOutcome, set[ReferenceCurrentnessReason]] = {
    ReferenceCurrentnessOutcome.CURRENT: {
        ReferenceCurrentnessReason.LIVE_CURRENT_CARD,
        ReferenceCurrentnessReason.LIVE_CURRENT_CARD_TRUNCATED,
    },
    ReferenceCurrentnessOutcome.STALE: {
        ReferenceCurrentnessReason.REVOKED,
        ReferenceCurrentnessReason.NOT_YET_IN_FORCE,
    },
    ReferenceCurrentnessOutcome.CONFLICT: {
        ReferenceCurrentnessReason.TITLE_IDENTITY_MISMATCH,
        ReferenceCurrentnessReason.SOURCE_CHANGED_DURING_FETCH,
        ReferenceCurrentnessReason.IDENTITY_REQUISITE_MISMATCH,
    },
    ReferenceCurrentnessOutcome.UNKNOWN: {
        ReferenceCurrentnessReason.ANALYSIS_DATE_UNSUPPORTED,
        ReferenceCurrentnessReason.INCOMPLETE_METADATA,
    },
    ReferenceCurrentnessOutcome.TECHNICAL_FAILURE: {
        ReferenceCurrentnessReason.INCOMPLETE_FETCH,
        ReferenceCurrentnessReason.PROVIDER_UNAVAILABLE,
    },
}


_PREFETCH_REASONS_BY_OUTCOME: dict[ReferenceCurrentnessOutcome, set[ReferenceCurrentnessReason]] = {
    ReferenceCurrentnessOutcome.STALE: {ReferenceCurrentnessReason.REVOKED},
    ReferenceCurrentnessOutcome.UNKNOWN: {ReferenceCurrentnessReason.INCOMPLETE_METADATA},
    ReferenceCurrentnessOutcome.TECHNICAL_FAILURE: {
        ReferenceCurrentnessReason.PROVIDER_UNAVAILABLE,
        ReferenceCurrentnessReason.CACHE_READ_ERROR,
    },
}

#: Coverage a CURRENT receipt may claim, keyed by its reason.
_LIVE_CURRENT_COVERAGE: dict[ReferenceCurrentnessReason, ReferenceCoverageStatus] = {
    ReferenceCurrentnessReason.LIVE_CURRENT_CARD: ReferenceCoverageStatus.COMPLETE,
    ReferenceCurrentnessReason.LIVE_CURRENT_CARD_TRUNCATED: ReferenceCoverageStatus.TRUNCATED_CONFIGURED,
}


class ReferenceCheckResult(_StrictReferenceCurrentnessModel):
    """One named identity/version check outcome; carries no provider payload."""

    name: str = Field(min_length=1, max_length=128)
    passed: bool
    detail: str | None = Field(default=None, max_length=256)


class ReferenceLiveFetchEvidence(_StrictReferenceCurrentnessModel):
    """What the retrieval boundary actually observed during one live fetch."""

    transport: str = Field(min_length=1, max_length=64)
    #: Clock read once at the retrieval boundary; comparison functions stay pure.
    fetched_at: datetime
    analysis_date: str | None = Field(default=None, max_length=32)
    analysis_date_supported: bool
    metadata_fingerprint_before: str = Field(min_length=71, max_length=71)
    metadata_fingerprint_after: str = Field(min_length=71, max_length=71)
    refetch_attempts: int = Field(default=0, ge=0, le=2)
    identity_checks: tuple[ReferenceCheckResult, ...] = ()
    version_checks: tuple[ReferenceCheckResult, ...] = ()

    @field_validator("metadata_fingerprint_before", "metadata_fingerprint_after")
    @classmethod
    def _validate_fingerprint(cls, value: str) -> str:
        if not _FINGERPRINT_RE.fullmatch(value):
            raise ValueError("live fetch fingerprints must be sha256 fingerprints")
        return value

    @field_validator("analysis_date")
    @classmethod
    def _normalize_analysis_date(cls, value: str | None) -> str | None:
        return canonicalize_reference_date(value)

    @field_validator("fetched_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("fetched_at must be timezone aware")
        return value.astimezone(UTC)

    @property
    def metadata_stable(self) -> bool:
        return hmac.compare_digest(self.metadata_fingerprint_before, self.metadata_fingerprint_after)

    @property
    def all_checks_passed(self) -> bool:
        checks = (*self.identity_checks, *self.version_checks)
        return bool(checks) and all(item.passed for item in checks)

    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in (*self.identity_checks, *self.version_checks) if not item.passed)


def _live_version_supports_current(version: ReferenceVersionFingerprint) -> bool:
    "Sufficiency for a positive decision: identity, real content and a fetched required range."
    return bool(
        version.identity.title
        and version.par_count > 0
        and version.content_fingerprint
        and version.in_force is not False
        and version.coverage.status is not ReferenceCoverageStatus.INCOMPLETE
    )


class ReferenceCurrentnessReceipt(_StrictReferenceCurrentnessModel):
    """Stable, trace-safe currentness decision; it contains neither text nor query.

    ``live`` carries the evidence of a completed fetch and is required for every receipt that
    got as far as one.  A receipt without it only records why the act was never read
    (provider unavailable, unreadable cache, revoked or empty card) and is never CURRENT.
    """

    receipt_id: str = ""
    outcome: ReferenceCurrentnessOutcome
    reason: ReferenceCurrentnessReason
    source: ReferenceSourceCoordinates
    expected: ReferenceVersionFingerprint | None = None
    observed: ReferenceVersionFingerprint | None = None
    live: ReferenceLiveFetchEvidence | None = None

    @model_validator(mode="after")
    def _validate_and_assign_id(self) -> ReferenceCurrentnessReceipt:
        allowed = _LIVE_REASONS_BY_OUTCOME if self.live is not None else _PREFETCH_REASONS_BY_OUTCOME
        if self.reason not in allowed.get(self.outcome, set()):
            raise ValueError(f"reason {self.reason.value} is incompatible with outcome {self.outcome.value}")
        if self.expected is not None and self.expected.source != self.source:
            raise ValueError("receipt source must match the expected version source")
        if self.observed is not None and self.observed.source != self.source:
            raise ValueError("receipt source must match the observed version source")

        if self.live is not None:
            if self.outcome is ReferenceCurrentnessOutcome.CURRENT:
                self._validate_current(self.live)
            else:
                self._validate_noncurrent(self.live)
        elif self.reason is ReferenceCurrentnessReason.REVOKED and (self.observed is None or self.observed.in_force is not False):
            raise ValueError("revoked receipt requires observed in_force=False")

        identity = self.model_dump(
            mode="json",
            exclude={"receipt_id": True, "live": {"fetched_at", "refetch_attempts", "analysis_date"}},
        )
        expected_id = _stable_id("reference-currentness", identity)
        if self.receipt_id and self.receipt_id != expected_id:
            raise ValueError("receipt_id must match canonical currentness receipt")
        self.receipt_id = expected_id
        return self

    def _validate_current(self, live: ReferenceLiveFetchEvidence) -> None:
        if self.expected is None or self.observed is None:
            raise ValueError("current receipt requires expected and observed versions")
        if self.expected.version_fingerprint != self.observed.version_fingerprint:
            raise ValueError("current receipt requires matching canonical version fingerprints")
        if not _live_version_supports_current(self.observed):
            raise ValueError("current receipt requires identity, content and a fetched required range")
        if self.observed.coverage.status is not _LIVE_CURRENT_COVERAGE[self.reason]:
            raise ValueError("current receipt coverage must match its reason")
        if not live.analysis_date_supported:
            raise ValueError("current receipt cannot answer an unsupported analysis date")
        if not live.metadata_stable:
            raise ValueError("current receipt requires identical metadata before and after the fetch")
        if not live.all_checks_passed:
            raise ValueError("current receipt requires every recorded identity and version check to pass")

    def _validate_noncurrent(self, live: ReferenceLiveFetchEvidence) -> None:
        if self.reason is ReferenceCurrentnessReason.REVOKED and (self.observed is None or self.observed.in_force is not False):
            raise ValueError("revoked receipt requires observed in_force=False")
        if self.reason is ReferenceCurrentnessReason.INCOMPLETE_FETCH:
            version = self.observed or self.expected
            if version is None or version.coverage.status is not ReferenceCoverageStatus.INCOMPLETE:
                raise ValueError("incomplete fetch receipt requires incomplete coverage")
        if self.reason is ReferenceCurrentnessReason.ANALYSIS_DATE_UNSUPPORTED and live.analysis_date_supported:
            raise ValueError("unsupported analysis date receipt requires an unsupported analysis date")
        if self.reason is ReferenceCurrentnessReason.SOURCE_CHANGED_DURING_FETCH and live.metadata_stable:
            raise ValueError("source-changed receipt requires differing metadata fingerprints")
        if (
            self.reason
            in {
                ReferenceCurrentnessReason.IDENTITY_REQUISITE_MISMATCH,
                ReferenceCurrentnessReason.TITLE_IDENTITY_MISMATCH,
                ReferenceCurrentnessReason.NOT_YET_IN_FORCE,
            }
            and live.all_checks_passed
        ):
            raise ValueError("a rejected identity or version claim must record the failed check")


def validate_reference_content(version: ReferenceVersionFingerprint, content: str) -> bool:
    """Check persisted content integrity without turning a mismatch into currentness."""
    return hmac.compare_digest(version.content_fingerprint, reference_content_fingerprint(content))


def validate_reference_currentness_receipt(payload: object) -> ReferenceCurrentnessReceipt:
    """Revalidate a receipt from its dump (the stable ID and every invariant are recomputed)."""
    return ReferenceCurrentnessReceipt.model_validate(payload)
