from collections.abc import Mapping

from src.agents.enums.common.review import RiskKind
from src.agents.schemas.common.review import PROJECT_SOURCE_ID, Risk


def quote_in_source(quote: str, text: str) -> bool:
    """Accept one continuous quote; normalize whitespace without changing legal wording."""
    normalized = " ".join(quote.split())
    return bool(normalized) and normalized in " ".join(text.split())


def grounding_failure(risk: Risk, document: str, sources: Mapping[str, str]) -> tuple[str, str] | None:
    "Why the risk's citations do not hold — ``(category, detail)`` — or ``None`` when they do."
    lookup = {**sources, PROJECT_SOURCE_ID: document}
    citations = risk.citations
    if not citations or citations[0].source != PROJECT_SOURCE_ID:
        return "malformed_citations", ""
    for citation in citations:
        if citation.source not in lookup:
            return "unknown_source_id", citation.source
        if not quote_in_source(citation.quote, lookup[citation.source]):
            category = "project_quote_unconfirmed" if citation.source == PROJECT_SOURCE_ID else "external_quote_unconfirmed"
            return category, citation.source
    external = any(c.source != PROJECT_SOURCE_ID for c in citations)
    if risk.kind is RiskKind.INTERNAL and external:
        return "unexpected_external_citation", ""
    if risk.kind is not RiskKind.INTERNAL and not external:
        return "missing_external_citation", ""
    return None


def grounded(risk: Risk, document: str, sources: Mapping[str, str]) -> bool:
    "The first citation is the project; every quote is a substring of its own source."
    failure = grounding_failure(risk, document, sources)
    return failure is None
