from langgraph.errors import GraphRecursionError

from src.common.config.config import settings
from src.infrastructure.llm.errors import _exception_chain, llm_error_kind


def failure_reason(exc: BaseException) -> str:
    """A reason for a person; provider text and project content never enter it."""
    if isinstance(exc, GraphRecursionError):
        return settings["agents.review.failures.recursion"]
    if any(getattr(cause, "code", None) == "context_length_exceeded" for cause in _exception_chain(exc)):
        return settings["agents.review.failures.context_length"]
    return settings["agents.review.failures.reasons"].get(llm_error_kind(exc), settings["agents.review.failures.default_reason"])
