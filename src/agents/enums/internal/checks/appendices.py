from enum import StrEnum


class AppendixOwnerKind(StrEnum):
    """Owner of an appendix namespace, not its physical document container."""

    PROJECT = "project"
    EXTERNAL_ACT = "external_act"
    AMENDED_ACT = "amended_act"
    UNKNOWN = "unknown"


class AppendixArtifactKind(StrEnum):
    FORM = "form"
    NON_FORM = "non_form"
    UNKNOWN = "unknown"


class AppendixIssueKind(StrEnum):
    MISSING_FORM_REFERENCE = "missing_form_reference"
    INVALID_DECLARED_RANGE = "invalid_declared_range"


class AppendixSpanRole(StrEnum):
    DECLARATION = "declaration"
    OWNER_QUALIFIER = "owner_qualifier"
    HEADER = "header"
    CLASSIFICATION = "classification"
