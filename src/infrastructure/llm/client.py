"""Соединение с внешним OpenAI-совместимым провайдером; без ролей и политик генерации."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from src.infrastructure.llm.context import HEADER_SERVICE_NAME, attach_gateway_headers, attach_gateway_headers_async

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI


def _is_cluster_internal(host: str) -> bool:
    """Адрес внутри кластера: короткое имя Service или имя в зоне ``.svc`` / ``.cluster.local``."""
    name = host.lower().rstrip(".")
    return "." not in name or name.endswith((".svc", ".cluster.local"))


@functools.lru_cache(maxsize=1)
def _cost_aware_chat_openai() -> type[ChatOpenAI]:
    "`ChatOpenAI`, не теряющий провайдерский ``cost`` при ``streaming = true``."
    from langchain_openai import ChatOpenAI  # noqa: PLC0415 - lazy provider boundary

    class CostAwareChatOpenAI(ChatOpenAI):  # type: ignore[misc]
        def _create_chat_result(self, response: Any, generation_info: dict | None = None) -> Any:
            result = super()._create_chat_result(response, generation_info)
            original = response if isinstance(response, dict) else response.model_dump(exclude_unset=True)
            for generation in result.generations:
                metadata = generation.message.response_metadata
                metadata["source_usage"] = original.get("usage")
                metadata["source_model"] = original.get("model")
                for key in (
                    "model",
                    "provider",
                    "provider_name",
                    "provider_slug",
                    "upstream_provider",
                    "cost",
                    "total_cost",
                    "cost_usd",
                ):
                    if original.get(key) is not None:
                        metadata[key] = original[key]
            return result

        def _convert_chunk_to_generation_chunk(
            self,
            chunk: dict,
            default_chunk_class: type,
            base_generation_info: dict | None,
        ) -> Any:
            generation_chunk = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
            if generation_chunk is None:
                return None
            usage = chunk.get("usage") if isinstance(chunk, dict) else None
            metadata = generation_chunk.message.response_metadata
            if isinstance(usage, dict):
                metadata["source_usage"] = usage
            for key in (
                "model",
                "provider",
                "provider_name",
                "provider_slug",
                "upstream_provider",
                "cost",
                "total_cost",
                "cost_usd",
            ):
                if chunk.get(key) is not None:
                    metadata[key] = chunk[key]
            cost = usage.get("cost") if isinstance(usage, dict) else None
            if not isinstance(cost, int | float) or isinstance(cost, bool):
                return generation_chunk
            metadata = generation_chunk.message.response_metadata
            token_usage = metadata.get("token_usage")
            if isinstance(token_usage, dict):
                token_usage.setdefault("cost", float(cost))
            else:
                metadata["token_usage"] = {"cost": float(cost)}
            return generation_chunk

    return CostAwareChatOpenAI


class ProviderClient:
    """Владеет ленивыми HTTP-клиентами одного подключения; модели создаёт из готовых параметров."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        http_timeout_s: float,
        proxy_url: str | None = None,
        service_name: str,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.http_timeout_s = http_timeout_s
        self.proxy_url = proxy_url
        self.service_name = service_name
        self._clients: tuple[httpx.Client, httpx.AsyncClient] | None = None

    def http_clients(self) -> tuple[httpx.Client, httpx.AsyncClient]:
        """Не создаёт соединения с сетью; повторный вызов возвращает ту же пару."""
        if self._clients is None:
            from openai import DefaultAsyncHttpxClient, DefaultHttpxClient  # noqa: PLC0415

            host = urlparse(self.base_url).hostname or ""
            mounts = {f"all://{host}": None} if self.proxy_url and host and _is_cluster_internal(host) else None
            common: dict[str, Any] = {"proxy": self.proxy_url, "timeout": self.http_timeout_s, "mounts": mounts}
            sync_client = DefaultHttpxClient(event_hooks={"request": [attach_gateway_headers]}, **common)
            try:
                async_client = DefaultAsyncHttpxClient(event_hooks={"request": [attach_gateway_headers_async]}, **common)
            except BaseException:
                sync_client.close()
                raise
            self._clients = sync_client, async_client
        return self._clients

    def build_model(self, options: dict[str, Any]) -> ChatOpenAI:
        if options.get("max_retries", 0) != 0:
            raise ValueError("SDK max_retries must be 0")
        options = {**options, "max_retries": 0}
        sync_client, async_client = self.http_clients()
        return _cost_aware_chat_openai()(
            **options,
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=sync_client,
            http_async_client=async_client,
            default_headers={HEADER_SERVICE_NAME: self.service_name},
        )

    async def aclose(self) -> None:
        clients, self._clients = self._clients, None
        if clients is None:
            return
        sync_client, async_client = clients
        try:
            sync_client.close()
        finally:
            await async_client.aclose()
