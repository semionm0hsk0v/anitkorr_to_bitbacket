from enum import StrEnum


class FrameProvenance(StrEnum):
    TREE = "tree"
    LEXICAL = "lexical"
    INHERITED = "inherited"
    LEGACY = "legacy"


class GroundingStatus(StrEnum):
    EXACT = "exact"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    LEGACY_LOCAL = "legacy_local"


class EvidenceSourceKind(StrEnum):
    PROJECT = "project"
    REFERENCE_NORM = "reference_norm"


class AddressSpace(StrEnum):
    CORE = "core"
    APPENDIX = "appendix"
    TABLE = "table"
    AMENDED_ACT = "amended_act"
    UNKNOWN = "unknown"


class SourceRole(StrEnum):
    OPERATIVE_NORM = "operative_norm"
    DEFINITION = "definition"
    PREAMBLE = "preamble"
    FORM = "form"
    APPENDIX = "appendix"
    TABLE = "table"
    UNKNOWN = "unknown"


class RegulatedVariableKind(StrEnum):
    DURATION = "duration"
    DEADLINE = "deadline"
    MONETARY_AMOUNT = "monetary_amount"
    SHARE = "share"
    AGE = "age"
    AREA = "area"
    RESULT_KIND = "result_kind"
    DOCUMENT_RESULT = "document_result"
    DOCUMENT_DELIVERY = "document_delivery"
    REGISTRY_TARGET = "registry_target"
    INFORMATION_SYSTEM_TARGET = "information_system_target"
    REGISTRATION_START_EVENT = "registration_start_event"
    REGISTRATION_COMPLETION_EVENT = "registration_completion_event"
    QUEUE_WAITING_TIME = "queue_waiting_time"
    SET_IDENTITY = "set_identity"
    LIST_IDENTITY = "list_identity"
    SUBMISSION_CHANNEL = "submission_channel"
    SUBMISSION_FORM = "submission_form"
    UNKNOWN = "unknown"


class ContextBlockRole(StrEnum):
    """Typed structural relation of a context block to the primary norm."""

    PARENT_INTRO = "parent_intro"
    ANCESTOR_HEADING = "ancestor_heading"
    LIST_INTRO = "list_intro"
    LIST_SIBLING = "list_sibling"
    DOCUMENT_SUBJECT = "document_subject"


class FrameRelation(StrEnum):
    """Доказанное отношение одной semantic dimension пары frames."""

    SAME = "same"
    GENERAL_TO_SPECIFIC = "general_to_specific"
    SPECIFIC_TO_GENERAL = "specific_to_general"
    DISJOINT = "disjoint"
    UNKNOWN = "unknown"


class FramePairOutcome(StrEnum):
    """Recall-safe итог frame gate; это не вывод о юридическом нарушении."""

    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"
