"""Метаданные ответа провайдера и учёт токенов в контексте узла."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from src.infrastructure.llm.metadata import (
    _extract_cache_read,
    _response_cost,
    _response_finish_reason,
    _response_model,
    _response_provider,
)

if TYPE_CHECKING:
    from src.agents.llm.result import AttemptInfo


class TokenUsage(BaseModel):
    """Разбивка расхода токенов одного или нескольких вызовов LLM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None = None

    @property
    def complete(self) -> bool:
        return all(value is not None for value in (self.input_tokens, self.output_tokens, self.total_tokens))


def normalized_usage(inp: int | None, out: int | None, total: int | None, cache: int | None) -> TokenUsage | None:
    if total is None and inp is not None and out is not None:
        total = inp + out
    if all(value is None for value in (inp, out, total, cache)):
        return None
    return TokenUsage(input_tokens=inp, output_tokens=out, total_tokens=total, cache_read_tokens=cache)


def sum_usage(usages: list[TokenUsage]) -> TokenUsage | None:
    """Sum known subtotals; completeness is tracked separately from the sum."""
    if not usages:
        return None

    def subtotal(field: str) -> int | None:
        values = [value for usage in usages if (value := getattr(usage, field)) is not None]
        return sum(values) if values else None

    return TokenUsage(**{key: subtotal(key) for key in TokenUsage.model_fields})


@dataclass(slots=True)
class AttemptMetadata:
    usage: tuple[int | None, int | None, int | None, int | None] = (None, None, None, None)
    finish_reason: str | None = None
    cost: float | None = None
    provider: str | None = None
    model: str | None = None
    phase: str = "response"


_ATTEMPT_METADATA: contextvars.ContextVar[AttemptMetadata | None] = contextvars.ContextVar("llm_attempt_metadata", default=None)


@contextmanager
def attempt_metadata_scope() -> Iterator[None]:
    """Restore the caller's attempt even for nested callbacks, errors and cancellation.

    begin_attempt creates one mutable object before wait_for spawns its task;
    that object remains shared with the attempt reporter in the parent task.
    """
    token = _ATTEMPT_METADATA.set(None)
    try:
        yield
    finally:
        _ATTEMPT_METADATA.reset(token)


def begin_attempt() -> None:
    _ATTEMPT_METADATA.set(AttemptMetadata())


def attempt_metadata() -> AttemptMetadata:
    return _ATTEMPT_METADATA.get() or AttemptMetadata()


def _record_usage_from_message(msg: Any) -> None:
    "Записать расход токенов прямо с AIMessage, минуя колбэк LangChain."
    if msg is None:
        return
    metadata = _ATTEMPT_METADATA.get()
    if metadata is not None:
        metadata.finish_reason = _response_finish_reason(msg)
        metadata.cost = _response_cost(msg)
        metadata.provider = _response_provider(msg)
        metadata.model = _response_model(msg)
    rm = getattr(msg, "response_metadata", None) or {}
    um = (
        rm.get("source_usage") or {}
        if "source_usage" in rm
        else rm.get("token_usage") or rm.get("usage") or getattr(msg, "usage_metadata", None) or {}
    )
    if not um:
        return
    inp = um.get("input_tokens", um.get("prompt_tokens"))
    out = um.get("output_tokens", um.get("completion_tokens"))
    total = um.get("total_tokens")
    cache_read = _extract_cache_read(um)
    if total is None and inp is not None and out is not None:
        total = inp + out
    if metadata is not None:
        metadata.usage = (inp, out, total, cache_read)


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    usage: TokenUsage | None
    complete: bool


@dataclass(frozen=True, slots=True)
class InterruptedUsage:
    usage: TokenUsage | None
    attempts: tuple[AttemptInfo, ...]
    complete: bool = False


@dataclass(slots=True)
class NodeUsage:
    entries: list[TokenUsage | None] = field(default_factory=list)
    attempts: list[AttemptInfo] = field(default_factory=list)


_TOKEN_BUFFER: contextvars.ContextVar[NodeUsage | None] = contextvars.ContextVar("token_usage_buffer", default=None)


def record_attempt(info: AttemptInfo) -> None:
    buf = _TOKEN_BUFFER.get()
    if buf is not None:
        buf.attempts.append(info)


def interrupted_usage() -> InterruptedUsage:
    buf = _TOKEN_BUFFER.get()
    return InterruptedUsage(current_token_breakdown(), tuple(buf.attempts) if buf is not None else ())


def current_usage_snapshot() -> UsageSnapshot:
    buf = _TOKEN_BUFFER.get()
    return UsageSnapshot(current_token_breakdown(), buf is None or all(u is not None and u.complete for u in buf.entries))


def merge_stage_usage(left: dict[str, UsageSnapshot], right: dict[str, UsageSnapshot]) -> dict[str, UsageSnapshot]:
    """Idempotent stage snapshots; never sum repeated cumulative snapshots."""
    return {**left, **right}


def open_token_scope() -> contextvars.Token:
    """Открыть свежий буфер токенов узла. Вернуть токен для :func:`reset_token_scope`."""
    return _TOKEN_BUFFER.set(NodeUsage())


def reset_token_scope(token: contextvars.Token) -> None:
    """Закрыть буфер, восстановив предыдущий контекст."""
    _TOKEN_BUFFER.reset(token)


def record_token_usage(
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    cache_read_tokens: int | None = None,
) -> None:
    """Записать расход токенов одного вызова LLM в буфер текущего узла."""
    usage = normalized_usage(input_tokens, output_tokens, total_tokens, cache_read_tokens)
    buf = _TOKEN_BUFFER.get()
    if buf is not None:
        buf.entries.append(usage)


def current_token_breakdown() -> TokenUsage | None:
    """Разбивка суммарного расхода в текущем узле (``None`` — если ещё ничего)."""
    buf = _TOKEN_BUFFER.get()
    if buf is None:
        return None
    return sum_usage([u for u in buf.entries if u is not None])


def current_token_usage() -> int | None:
    """Суммарный расход токенов (total) в текущем узле — для UI-фрагмента."""
    breakdown = current_token_breakdown()
    return breakdown.total_tokens if breakdown else None
