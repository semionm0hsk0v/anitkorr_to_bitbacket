"""Генерация текста и данных по схеме; политика агента поверх транспорта провайдера."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing, asynccontextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel

from src.agents.llm.config import _LLM_ROLES, _option, model_kwargs, retry_policy, validate_call_options
from src.agents.llm.errors import (
    EmptyOutputError,
    LLMCallbackError,
    LLMGenerationError,
    LlmOptionError,
    OutputTruncationError,
)
from src.agents.llm.result import AttemptInfo, LLMResult, total_usage
from src.agents.llm.retry import invoke_attempts, invoke_structured
from src.agents.llm.structured import _raw_output_was_truncated, _response_text
from src.agents.llm.usage import _record_usage_from_message, attempt_metadata
from src.common.config.config import settings
from src.common.config.required import require_settings
from src.common.utils.logger import get_logger
from src.infrastructure.llm.client import ProviderClient
from src.infrastructure.llm.context import SERVICE_NAME
from src.infrastructure.llm.errors import ProviderFinishReasonError, llm_error_kind
from src.infrastructure.llm.metadata import _response_finish_reason, stream_finished
from src.infrastructure.llm.transport import IncompleteStreamError, copy_model, merge_chunks, request_message, stream_messages
from src.infrastructure.prompts.metadata import PromptIdentity, contract_hash

if TYPE_CHECKING:
    from langchain_core.language_models import LanguageModelInput
    from langchain_openai import ChatOpenAI

log = get_logger(__name__)


class LLMClient:
    """Own the default provider; an injected provider is borrowed from its caller.

    aclose clears cached roles and closes only owned pools. Subsequent calls may
    reopen owned pools. Shutdown requires completed/cancelled generation tasks;
    concurrent close and generation is unsupported. The owner of a borrowed
    provider must keep it open until all clients using it have finished.
    """

    def __init__(self, *, provider: ProviderClient | None = None) -> None:
        self._transport = provider
        self._owns_provider = provider is None
        self._models: dict[str, ChatOpenAI] = {}

    @property
    def _provider(self) -> ProviderClient:
        if self._transport is None:
            require_settings("ROUTER_AI_API_KEY", "ROUTER_AI_BASE_URL")
            self._transport = ProviderClient(
                base_url=str(settings.ROUTER_AI_BASE_URL),
                api_key=str(settings.ROUTER_AI_API_KEY),
                http_timeout_s=float(settings.llm.http_timeout_s),
                proxy_url=settings.get("PROXY_URL") or None,
                service_name=SERVICE_NAME,
            )
        return self._transport

    def _build_model(self, config: Any) -> ChatOpenAI:
        options = model_kwargs(config)
        return self._provider.build_model(options)

    def _get_model(self, role: str, streaming: bool | None = None) -> ChatOpenAI:
        if role not in _LLM_ROLES:
            raise LlmOptionError(f"неизвестная LLM-роль {role!r}; роли: {', '.join(sorted(_LLM_ROLES))}")
        if role not in self._models:
            self._models[role] = self._build_model(getattr(settings.llm, role))
        model = self._models[role]
        if streaming is not None and getattr(model, "streaming", False) != streaming:
            model = copy_model(model, streaming=streaming)
        return model

    async def generate_structured[T: BaseModel](
        self,
        *,
        role: str,
        messages: LanguageModelInput | Sequence[Mapping[str, Any]],
        schema: type[T],
        streaming: bool | None = None,
        operation: str | None = None,
        timeout_s: float | None = None,
        retries: int | None = None,
        deadline: float | None = None,
        min_attempt_s: float = 0.0,
        batch_id: str | None = None,
        structured_method: Literal["json_schema", "json_mode"] | None = None,
        prompt: PromptIdentity | None = None,
    ) -> LLMResult[T]:
        """Даже при потоковом транспорте вернуть только целиком проверенный объект."""
        validate_call_options(timeout_s=timeout_s, retries=retries, deadline=deadline, min_attempt_s=min_attempt_s)
        model = self._get_model(role, streaming)
        attempts: list[AttemptInfo] = []
        contract = None
        if prompt is not None:
            contract = {
                "call_id": uuid4().hex,
                "prompt": asdict(prompt),
                "schema_sha256": contract_hash(schema.model_json_schema()),
                "messages_sha256": contract_hash(messages),
                "config_sha256": contract_hash(
                    {
                        "model": model_kwargs(getattr(settings.llm, role)),
                        "retry_policy": retry_policy(),
                        "call_options": {
                            "streaming": streaming,
                            "timeout_s": timeout_s,
                            "retries": retries,
                            "deadline": deadline,
                            "min_attempt_s": min_attempt_s,
                            "structured_method": structured_method,
                        },
                    }
                ),
            }
        status = "cancelled"
        try:
            value = await invoke_structured(
                model,
                messages,
                schema,
                operation=operation,
                timeout_s=timeout_s,
                retries=retries,
                deadline=deadline,
                min_attempt_s=min_attempt_s,
                batch_id=batch_id,
                structured_method=structured_method,
                observer=attempts.append,
            )
            status = "ok"
        except Exception as exc:
            status = "error"
            raise LLMGenerationError(llm_error_kind(exc), tuple(attempts)) from exc
        finally:
            if contract is not None:
                log.info(
                    "llm_generation_contract",
                    **contract,
                    role=role,
                    operation=operation,
                    batch_id=batch_id,
                    status=status,
                    request_ids=[request_id for attempt in attempts for request_id in attempt.request_ids],
                    requested_models=list(dict.fromkeys(attempt.requested_model for attempt in attempts)),
                    actual_models=list(dict.fromkeys(attempt.model for attempt in attempts)),
                )
        return LLMResult(value=value, usage=total_usage(attempts), attempts=tuple(attempts))

    async def generate_text(
        self,
        *,
        role: str,
        messages: LanguageModelInput | Sequence[Mapping[str, Any]],
        streaming: bool | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        operation: str | None = None,
        timeout_s: float | None = None,
        retries: int | None = None,
        deadline: float | None = None,
        min_attempt_s: float = 0.0,
        batch_id: str | None = None,
    ) -> LLMResult[str]:
        """Вернуть полный текст; on_delta необязателен и используется только в потоке."""
        if role not in _LLM_ROLES:
            raise LlmOptionError(f"неизвестная LLM-роль {role!r}")
        if on_delta is not None and streaming is False:
            raise ValueError("on_delta requires streaming=True")
        stream = streaming if streaming is not None else bool(_option(getattr(settings.llm, role), "streaming", False))
        if on_delta is not None and not stream:
            raise ValueError("on_delta requires streaming=True")
        validate_call_options(timeout_s=timeout_s, retries=retries, deadline=deadline, min_attempt_s=min_attempt_s)
        model = self._get_model(role, stream)
        attempts: list[AttemptInfo] = []
        delivered = False

        async def once(target: Any) -> str:
            nonlocal delivered
            raw: Any = None
            if stream:
                async with aclosing(stream_messages(target, messages, on_partial=_record_usage_from_message)) as chunks:
                    async for chunk in chunks:
                        raw = merge_chunks(raw, chunk)
                        text = _response_text(getattr(chunk, "content", ""))
                        if text and on_delta is not None:
                            # Callback may deliver and then fail: the request must not be replayed.
                            delivered = True
                            attempt_metadata().phase = "callback"
                            try:
                                await on_delta(text)
                            except Exception as callback_error:
                                raise LLMCallbackError("delta callback failed") from callback_error
                            attempt_metadata().phase = "response"
            else:
                raw = await request_message(target, messages, on_partial=_record_usage_from_message)
            reason = _response_finish_reason(raw)
            if stream and not stream_finished(raw):
                raise IncompleteStreamError("missing terminal finish reason")
            if reason and reason.casefold() in {"error", "failed", "failure", "content_filter", "tool_calls", "function_call"}:
                raise ProviderFinishReasonError(reason)
            if _raw_output_was_truncated(raw):
                raise OutputTruncationError("text output truncated")
            text = _response_text(getattr(raw, "content", raw if isinstance(raw, str) else ""))
            if text is None or not text.strip():
                raise EmptyOutputError("empty text output")
            return text

        try:
            value = await invoke_attempts(
                model,
                once,
                operation=operation or "generate_text",
                timeout_s=timeout_s,
                retries=retries,
                deadline=deadline,
                min_attempt_s=min_attempt_s,
                batch_id=batch_id,
                retry_allowed=lambda: not delivered,
                observer=attempts.append,
            )
        except Exception as exc:
            raise LLMGenerationError(llm_error_kind(exc), tuple(attempts)) from exc
        return LLMResult(value=value, usage=total_usage(attempts), attempts=tuple(attempts))

    async def aclose(self) -> None:
        self._models.clear()
        if self._owns_provider and self._transport is not None:
            await self._transport.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()


llm = LLMClient()


@asynccontextmanager
async def client_lifespan():
    """Общий экземпляр приложения; вход не создаёт HTTP-клиентов."""
    async with llm:
        yield llm
