"""Выполнение попыток генерации: таймауты, повторы, отмена и журналирование."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from pydantic import BaseModel

from src.agents.llm.config import _ESCALATE_CAP, _ESCALATE_FACTOR, _TOKEN_STEP, retry_policy, validate_call_options
from src.agents.llm.errors import LLMDeadlineError, OutputTruncationError, _is_quota_exhausted
from src.agents.llm.result import AttemptInfo
from src.agents.llm.structured import _call_llm_structured_async_once
from src.agents.llm.usage import (
    _record_usage_from_message,
    attempt_metadata,
    attempt_metadata_scope,
    begin_attempt,
    normalized_usage,
    record_attempt,
    record_token_usage,
    sum_usage,
)
from src.common.utils.logger import get_logger
from src.infrastructure.llm.context import llm_call_scope
from src.infrastructure.llm.errors import exception_status_code, llm_error_kind, llm_error_log_fields
from src.infrastructure.llm.transport import copy_model, sdk_length_error_message

log = get_logger(__name__)


def _retry_wait_s(attempt_index: int, waits: tuple[float, ...]) -> float:
    """Пауза перед повтором номер ``attempt_index + 1``; тесты подменяют её нулём."""
    return waits[min(attempt_index, len(waits) - 1)]


def _retryable(exc: BaseException) -> bool:
    if _is_quota_exhausted(exc):
        return False
    kind = llm_error_kind(exc)
    # SDK parse errors may carry the successful HTTP 200 response; result
    # suitability must be decided before treating an HTTP status as a failure.
    if kind in {"invalid_json", "invalid_schema", "empty_output", "truncated_output"}:
        return True
    status = exception_status_code(exc)
    if status is not None:
        return status in {408, 409, 429} or 500 <= status < 600
    return kind in {
        "timeout",
        "transport_error",
        "incomplete_stream",
        "provider_error",
        "rate_limit",
    }


def _retry_after_s(exc: BaseException, cap: float) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    value = headers.get("retry-after") if headers is not None else None
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            delay = (date - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return min(delay, cap) if math.isfinite(delay) and delay >= 0 else None


def _attempt_timeout_s(timeout_s: float | None, deadline: float | None) -> float | None:
    """Потолок одной попытки: ``timeout_s``, но не дальше абсолютного ``deadline`` вызова."""
    if deadline is None:
        return timeout_s
    remaining = deadline - time.perf_counter()
    return remaining if timeout_s is None or timeout_s <= 0 else min(timeout_s, remaining)


def _model_name(llm: Any) -> str:
    for attr in ("model_name", "model", "deployment_name"):
        value = getattr(llm, attr, None)
        if value:
            return str(value)
    return type(llm).__name__


def _escalated_llm(llm: Any) -> Any | None:
    "Клон LLM с увеличенным max_tokens — для повтора при обрыве JSON."
    current = getattr(llm, "max_tokens", None)
    if not isinstance(current, int) or current <= 0 or current >= _ESCALATE_CAP:
        return None
    raw = int(current * _ESCALATE_FACTOR)
    # округление вверх до кратного _TOKEN_STEP
    bumped = min(_ESCALATE_CAP, ((raw + _TOKEN_STEP - 1) // _TOKEN_STEP) * _TOKEN_STEP)
    if bumped <= current:
        return None
    try:
        return copy_model(llm, max_tokens=bumped)
    except Exception:
        return None


async def _invoke_with_retries[T](  # noqa: PLR0915 - one bounded attempt state machine
    llm: Any,
    attempt_call: Callable[[Any], Awaitable[T]],
    *,
    on_attempt: Callable[[bool, BaseException | None, str, float], None] | None = None,
    escalate_output_limit: bool = False,
    timeout_s: float | None = None,
    retries: int | None = None,
    deadline: float | None = None,
    min_attempt_s: float = 0.0,
    retry_allowed: Callable[[], bool] | None = None,
) -> T:
    "Один цикл повторов на все ошибки; потолок ``timeout_s`` — на КАЖДУЮ попытку."
    validate_call_options(timeout_s=timeout_s, retries=retries, deadline=deadline, min_attempt_s=min_attempt_s)
    if getattr(llm, "max_retries", 0) != 0:
        raise ValueError("SDK max_retries must be 0")
    budget, waits = retry_policy()
    if retries is not None:
        budget = retries
    wait_budget = sum(waits[min(i, len(waits) - 1)] for i in range(budget))
    waited = 0.0
    target = llm
    escalated = False
    for attempt_index in range(budget + 1):
        begin_attempt()
        started = time.perf_counter()
        attempt_timeout_s = _attempt_timeout_s(timeout_s, deadline)
        if deadline is not None and (
            (attempt_timeout_s is not None and attempt_timeout_s <= 0) or deadline - started < min_attempt_s
        ):
            # Nothing is reserved or sent past the caller's deadline.
            raise LLMDeadlineError("llm call deadline reached before the attempt")
        try:
            once = attempt_call(target)
            result = (
                await asyncio.wait_for(once, timeout=attempt_timeout_s)
                if attempt_timeout_s is not None and attempt_timeout_s > 0
                else await once
            )
        except Exception as exc:
            length_message = sdk_length_error_message(exc)
            if length_message is not None:
                _record_usage_from_message(length_message)
                original = exc
                exc = OutputTruncationError("provider output limit reached")
                exc.__cause__ = original
            _report_attempt(on_attempt, target, False, exc, time.perf_counter() - started)

            if attempt_index >= budget or not _retryable(exc) or (retry_allowed is not None and not retry_allowed()):
                raise exc
            wait_s = _retry_wait_s(attempt_index, waits)
            retry_after = _retry_after_s(exc, max(waits))
            if retry_after is not None:
                wait_s = max(wait_s, retry_after)
            if wait_s > wait_budget - waited:
                raise exc
            if deadline is not None and deadline - time.perf_counter() - wait_s <= max(min_attempt_s, 0.0):
                raise exc
            if escalate_output_limit and isinstance(exc, OutputTruncationError) and not escalated:
                bumped = _escalated_llm(target)
                if bumped is not None:
                    escalated = True
                    target = bumped
                    await log.awarning(
                        "llm_call_escalate",
                        model=_model_name(llm),
                        error_type=type(exc).__name__,
                        max_tokens=getattr(bumped, "max_tokens", None),
                    )
            await log.awarning(
                "llm_retry",
                attempt=attempt_index + 1,
                retries=budget,
                sleep_s=round(wait_s, 2),
                **llm_error_log_fields(exc),
            )
            await asyncio.sleep(wait_s)
            waited += wait_s
            continue
        except BaseException as exc:
            # Cancellation propagates unchanged and never starts a retry.
            try:
                _report_attempt(on_attempt, target, False, exc, time.perf_counter() - started)
            except Exception as report_error:
                log.error("llm_attempt_report_failed", error_type=type(report_error).__name__)
            raise
        _report_attempt(on_attempt, target, True, None, time.perf_counter() - started)
        return result
    raise AssertionError("unreachable retry state")


def _report_attempt(
    on_attempt: Callable[[bool, BaseException | None, str, float], None] | None,
    target: Any,
    success: bool,
    exc: BaseException | None,
    duration_s: float,
) -> None:
    """Исход попытки наружу; ``target`` — модель именно этой попытки (после подъёма ``max_tokens`` это клон)."""
    state = attempt_metadata()
    record_token_usage(*state.usage)
    if on_attempt is None:
        return
    on_attempt(success, exc, _model_name(target), duration_s)


async def invoke_attempts[T](
    llm: Any,
    attempt_call: Callable[[Any], Awaitable[T]],
    *,
    timeout_s: float | None = None,
    operation: str,
    batch_id: str | None = None,
    retries: int | None = None,
    deadline: float | None = None,
    min_attempt_s: float = 0.0,
    retry_allowed: Callable[[], bool] | None = None,
    observer: Callable[[AttemptInfo], None] | None = None,
    escalate_output_limit: bool = False,
) -> T:
    "Вызов с таймаутом, повторами и безопасным журналом попыток."
    op = operation
    model = _model_name(llm)
    attempts_recorded = 0
    ids_recorded = 0
    attempt_infos: list[AttemptInfo] = []

    def _record_attempt(
        success: bool,
        exc: BaseException | None,
        attempt_model: str | None,
        duration_s: float = 0.0,
    ) -> None:
        nonlocal attempts_recorded, ids_recorded
        attempts_recorded += 1
        state = attempt_metadata()
        input_tokens, output_tokens, total_tokens, cache_read_tokens = state.usage
        cost, actual_provider, finish_reason = state.cost, state.provider, state.finish_reason
        tokens = normalized_usage(*state.usage)
        info = AttemptInfo(
            model=state.model,
            requested_model=attempt_model or model,
            phase=state.phase,
            success=success,
            duration_s=duration_s,
            usage=tokens,
            cost=cost,
            provider=actual_provider,
            finish_reason=finish_reason,
            request_ids=tuple(request_ids[ids_recorded:]),
            error_type=type(exc).__name__ if exc is not None else None,
        )
        ids_recorded = len(request_ids)
        attempt_infos.append(info)
        record_attempt(info)
        if observer is not None:
            observer(info)
        log.info(
            "llm_attempt",
            operation=op,
            batch_id=batch_id,
            model=info.model,
            requested_model=info.requested_model,
            attempt=attempts_recorded,
            status="ok" if success else "error",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens=total_tokens,
            cache_read_tokens=cache_read_tokens,
            cost=cost,
            provider=actual_provider,
            finish_reason=finish_reason,
            error_type=type(exc).__name__ if exc is not None else None,
        )

    with llm_call_scope(operation=op) as request_ids, attempt_metadata_scope():
        begin_attempt()
        try:
            result = await _invoke_with_retries(
                llm,
                attempt_call,
                on_attempt=_record_attempt,
                escalate_output_limit=escalate_output_limit,
                timeout_s=timeout_s,
                retries=retries,
                deadline=deadline,
                min_attempt_s=min_attempt_s,
                retry_allowed=retry_allowed,
            )
        except Exception as exc:
            await log.aerror(
                "llm_call",
                operation=op,
                model=model,
                status="error",
                attempts=attempts_recorded,
                request_ids=request_ids,
                **llm_error_log_fields(exc),
            )
            raise
        except BaseException as exc:
            log.warning(
                "llm_call",
                operation=op,
                model=model,
                status="cancelled",
                attempts=attempts_recorded,
                request_ids=request_ids,
                error_type=type(exc).__name__,
            )
            raise

        known = sum_usage([info.usage for info in attempt_infos if info.usage is not None])
        input_tokens, output_tokens, total_tokens, cache_read_tokens = (
            (known.input_tokens, known.output_tokens, known.total_tokens, known.cache_read_tokens)
            if known is not None
            else (None, None, None, None)
        )
        await log.ainfo(
            "llm_call",
            operation=op,
            model=model,
            status="ok",
            attempts=attempts_recorded,
            request_ids=request_ids,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens=total_tokens,
            cache_read_tokens=cache_read_tokens,
        )
    return result


async def invoke_structured[T: BaseModel](
    llm: Any,
    messages_or_prompt: Any,
    schema: type[T],
    *,
    structured_method: Literal["json_schema", "json_mode"] | None = None,
    operation: str | None = None,
    timeout_s: float | None = None,
    retries: int | None = None,
    deadline: float | None = None,
    min_attempt_s: float = 0.0,
    batch_id: str | None = None,
    observer: Callable[[AttemptInfo], None] | None = None,
) -> T:
    """Bind schema parsing to the format-neutral attempt executor."""

    async def once(target: Any) -> T:
        return await _call_llm_structured_async_once(
            target,
            messages_or_prompt,
            schema,
            structured_method=structured_method,
        )

    return await invoke_attempts(
        llm,
        once,
        operation=operation or schema.__name__,
        timeout_s=timeout_s,
        retries=retries,
        deadline=deadline,
        min_attempt_s=min_attempt_s,
        batch_id=batch_id,
        observer=observer,
        escalate_output_limit=True,
    )
