from enum import StrEnum


class ReferenceCurrentnessOutcome(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


class ReferenceCurrentnessReason(StrEnum):
    #: Complete fresh fetch of the exact current card.
    LIVE_CURRENT_CARD = "live_current_card"
    #: Fresh fetch with an explicit, configured semantic range cap.
    LIVE_CURRENT_CARD_TRUNCATED = "live_current_card_truncated"
    TITLE_IDENTITY_MISMATCH = "title_identity_mismatch"
    REVOKED = "revoked"
    #: The selected version has not entered into force at the analysis date.
    NOT_YET_IN_FORCE = "not_yet_in_force"
    INCOMPLETE_METADATA = "incomplete_metadata"
    INCOMPLETE_FETCH = "incomplete_fetch"
    #: Document metadata moved between the pre-fetch and post-fetch reads twice.
    SOURCE_CHANGED_DURING_FETCH = "source_changed_during_fetch"
    #: Requested requisites (typically an explicit date) do not identify this card.
    IDENTITY_REQUISITE_MISMATCH = "identity_requisite_mismatch"
    #: A non-current analysis date cannot be proven by the current card.
    ANALYSIS_DATE_UNSUPPORTED = "analysis_date_unsupported"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CACHE_READ_ERROR = "cache_read_error"


class ReferenceCoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    TRUNCATED_CONFIGURED = "TRUNCATED_CONFIGURED"
    INCOMPLETE = "INCOMPLETE"
