"""Результат генерации и безопасные сведения обо всех попытках."""

from __future__ import annotations

from dataclasses import dataclass

from src.agents.llm.usage import TokenUsage, sum_usage


@dataclass(frozen=True, slots=True)
class AttemptInfo:
    """One client attempt; request IDs and usage do not enumerate hidden gateway hops."""

    model: str | None
    success: bool
    duration_s: float
    usage: TokenUsage | None
    cost: float | None
    provider: str | None
    finish_reason: str | None
    request_ids: tuple[str, ...]
    error_type: str | None
    requested_model: str | None = None
    phase: str = "response"


@dataclass(frozen=True, slots=True)
class LLMResult[T]:
    value: T
    usage: TokenUsage | None
    attempts: tuple[AttemptInfo, ...]

    @property
    def usage_complete(self) -> bool:
        """All client attempts supplied main token fields; hidden server usage is unknown."""
        return bool(self.attempts) and all(attempt.usage is not None and attempt.usage.complete for attempt in self.attempts)

    @property
    def cache_usage_complete(self) -> bool:
        return bool(self.attempts) and all(a.usage is not None and a.usage.cache_read_tokens is not None for a in self.attempts)

    @property
    def cost(self) -> float | None:
        if not self.attempts or any(attempt.cost is None for attempt in self.attempts):
            return None
        return sum(attempt.cost for attempt in self.attempts if attempt.cost is not None)


def total_usage(attempts: list[AttemptInfo]) -> TokenUsage | None:
    known = [attempt.usage for attempt in attempts if attempt.usage is not None]
    return sum_usage(known)
