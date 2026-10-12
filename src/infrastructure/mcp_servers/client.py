from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any, cast

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

from src.common.config.config import settings
from src.infrastructure.mcp_servers.auth import keycloak_auth

logger = logging.getLogger(__name__)


_CONNECT_RETRIES = int(settings["infrastructure.mcp_servers.client.connect_retries"])


def _http_client(
    headers: dict[str, str] | None = None, timeout: httpx.Timeout | None = None, auth: httpx.Auth | None = None
) -> httpx.AsyncClient:
    """``create_mcp_http_client`` из ``mcp`` плюс повтор установки соединения."""
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout if timeout is not None else httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
        auth=auth,
        transport=httpx.AsyncHTTPTransport(retries=_CONNECT_RETRIES),
    )


class McpClient:
    def __init__(
        self,
        base_url: str,
        mcp_url: dict[str, str],
        auth_token: str | None,
        auth: Any | None = None,
        *,
        timeout_s: float = 90.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.mcp_url = mcp_url
        self.auth_token = auth_token
        self.auth = auth
        self.timeout_s = float(timeout_s)

        self.connections: dict[str, dict[str, Any]] = {}
        self._client: MultiServerMCPClient | None = None
        self.tools: dict[tuple[str, str], Any] = {}

    async def init(self) -> None:
        for server_name, path in self.mcp_url.items():
            conn: dict[str, Any] = {
                "url": f"{self.base_url}/{path.lstrip('/')}",
                "transport": "streamable_http",
                "timeout": self.timeout_s,
                "httpx_client_factory": _http_client,
            }
            if self.auth is not None:
                conn["auth"] = self.auth
            else:
                conn["headers"] = {"X-API-KEY": self.auth_token} if self.auth_token else {}
            self.connections[server_name] = conn

        self._client = MultiServerMCPClient(connections=cast(dict[str, Connection], self.connections))
        await self.warmup_tools()

    @property
    def instance(self) -> MultiServerMCPClient:
        if self._client is None:
            raise ValueError("MCP client is not initialized: call .init()")
        return self._client

    async def warmup_tools(self) -> None:
        logger.info("Warming up MCP tools for servers: %s", list(self.connections.keys()))
        results = await asyncio.gather(
            *[self.instance.get_tools(server_name=n) for n in self.connections],
            return_exceptions=True,
        )

        for name, res in zip(self.connections.keys(), results, strict=False):
            if isinstance(res, BaseException):
                logger.warning(
                    "Failed to warmup tools for %s:\n%s",
                    name,
                    "".join(traceback.format_exception(type(res), res, res.__traceback__)),
                )
                excs = getattr(res, "exceptions", None)
                if excs:
                    for i, sub in enumerate(excs, 1):
                        logger.warning(
                            "Warmup sub-exception #%d for %s: %r\n%s",
                            i,
                            name,
                            sub,
                            "".join(traceback.format_exception(type(sub), sub, sub.__traceback__)),
                        )
                continue
            for tool in res:
                self.tools[(name, tool.name)] = tool

        logger.info("MCP tools warmup complete: %d tools cached", len(self.tools))

    def ensure_tool_exists(self, server_name: str, tool_name: str) -> None:
        if (server_name, tool_name) not in self.tools:
            raise RuntimeError(f"Tool '{tool_name}' not found on server '{server_name}'. (tools are warmed up at startup)")


mcp_client = McpClient(
    base_url=settings.get("BASE_MCP_URL", ""),
    mcp_url=dict(settings.get("mcp.urls", {}) or {}),
    auth_token=settings.get("KB_MCP_AUTH_TOKEN"),
    auth=keycloak_auth if settings.get("USE_KEYCLOAK", False) else None,
    timeout_s=float(settings.get("MCP_TIMEOUT_S", 45)),
)
